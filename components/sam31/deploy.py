#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""SAM3.1 packaging helpers for the common cvatctl manager.

This module never creates a second manager or an independent deployment state.
Its command-line entry point delegates to cvatctl and its operation lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FUNCTION = "pth-sam31-tracker"
CHECKPOINT = "/models/sam3.1_multiplex.pt"
SHARED = "serverless/pytorch/facebookresearch/sam2/nuclio"
DEFAULTS = {
    "SAM31_GPU_DEVICE": "0",
    "SAM31_REDIS_MAXMEMORY": "2gb",
    "SAM31_STATE_MAX_BYTES": str(256 * 1024**2),
    "SAM31_SESSION_TTL_SECONDS": "28800",
}


def sources(root: Path) -> dict[str, bytes]:
    """Exactly the reviewed Python files sent to the function image builder."""
    files = {path.name: path.read_bytes() for path in
             sorted((root / "components/sam31/nuclio").glob("*.py"))}
    if "main.py" not in files:
        raise ValueError("Missing SAM3.1 function handler")
    for name in ("protocol.py", "geometry.py", "redis_store.py"):
        text = (root / SHARED / name).read_text()
        files[name] = text.replace("SAM2", "SAM31").replace("sam2", "sam31").encode()
    return files


def stage_sources(destination: Path, root: Path = ROOT) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in sources(root).items():
        (destination / name).write_bytes(content)


def function_image(version: str, root: Path = ROOT) -> str:
    digest = hashlib.sha256(version.encode())
    files = sources(root)
    for relative in ("components/sam31/function-gpu.json", "components/sam31/deploy.py",
                     "components/extensions/manage.py"):
        files[relative] = (root / relative).read_bytes()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + content)
    return f"cvat.sam31-tracker:{digest.hexdigest()[:20]}-gpu"


def validate_settings(values: dict[str, str], root: Path = ROOT, *, verify_file: bool = True) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", values.get("SAM31_REDIS_PASSWORD", "")):
        raise ValueError("SAM31_REDIS_PASSWORD must contain 32..128 ASCII letters, digits, _ or -")
    if not re.fullmatch(r"[0-9a-f]{64}", values.get("SAM31_CHECKPOINT_SHA256", "")):
        raise ValueError("Set SAM31_CHECKPOINT_SHA256 to the approved checkpoint SHA256")
    if not re.fullmatch(r"[0-9]+|GPU-[A-Za-z0-9-]+", values.get("SAM31_GPU_DEVICE") or "0"):
        raise ValueError("SAM31_GPU_DEVICE must be one numeric device ID or GPU UUID")
    if not re.fullmatch(r"[1-9][0-9]*(?:[kKmMgG][bB]?)?", values.get("SAM31_REDIS_MAXMEMORY") or "2gb"):
        raise ValueError("Invalid SAM31_REDIS_MAXMEMORY")
    for key, low, high in (("SAM31_STATE_MAX_BYTES", 1024, 512 * 1024**2),
                           ("SAM31_SESSION_TTL_SECONDS", 60, 604800)):
        value = values.get(key) or DEFAULTS[key]
        if not value.isdigit() or not low <= int(value) <= high:
            raise ValueError(f"Invalid {key}: expected {low}..{high}")
    raw = values.get("SAM31_CHECKPOINT_HOST", "")
    checkpoint = Path(raw).expanduser()
    if not raw or not checkpoint.is_absolute():
        raise ValueError("SAM31_CHECKPOINT_HOST must be an absolute path outside the checkout")
    checkpoint = checkpoint.resolve()
    if checkpoint == root.resolve() or root.resolve() in checkpoint.parents:
        raise ValueError("Keep SAM3.1 checkpoint weights outside the checkout/build context")
    # Stopping a deployment must still work if its checkpoint was moved/deleted.
    if verify_file:
        if not checkpoint.is_file():
            raise ValueError("SAM31_CHECKPOINT_HOST must name an existing checkpoint file")
        digest = hashlib.sha256()
        with checkpoint.open("rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024**2), b""):
                digest.update(chunk)
        if digest.hexdigest() != values["SAM31_CHECKPOINT_SHA256"]:
            raise ValueError("SAM3.1 checkpoint SHA256 mismatch")
    return checkpoint


def runtime_environment(values: dict[str, str]) -> dict[str, str]:
    return {
        "SAM31_REDIS_PASSWORD": values["SAM31_REDIS_PASSWORD"],
        "SAM31_CHECKPOINT_SHA256": values["SAM31_CHECKPOINT_SHA256"],
        "SAM31_STATE_MAX_BYTES": values.get("SAM31_STATE_MAX_BYTES") or DEFAULTS["SAM31_STATE_MAX_BYTES"],
        "SAM31_SESSION_TTL_SECONDS": values.get("SAM31_SESSION_TTL_SECONDS") or DEFAULTS["SAM31_SESSION_TTL_SECONDS"],
        "CUDA_VISIBLE_DEVICES": values.get("SAM31_GPU_DEVICE") or "0",
    }


def render_function(template: dict, values: dict[str, str], checkpoint: Path, image: str) -> dict:
    result = copy.deepcopy(template)
    result["metadata"]["namespace"] = values["NUCLIO_NAMESPACE"]
    result["spec"]["build"]["image"] = image
    env = {item["name"]: item["value"] for item in result["spec"].get("env", [])}
    env.update(runtime_environment(values))
    result["spec"]["env"] = [{"name": name, "value": value} for name, value in env.items()]
    result["spec"]["volumes"] = [{
        "volume": {"name": "sam31-checkpoint", "hostPath": {"path": str(checkpoint), "type": "File"}},
        "volumeMount": {"name": "sam31-checkpoint", "mountPath": CHECKPOINT, "readOnly": True},
    }]
    return result


def checkpoint_matches(container: dict, checkpoint: str | Path) -> bool:
    return any(mount.get("Type") == "bind" and mount.get("Source") == str(checkpoint)
               and mount.get("Destination") == CHECKPOINT and mount.get("RW") is False
               for mount in container.get("Mounts", []))


def deploy(manager, model: dict, *, force: bool = False, no_build: bool = False) -> None:
    """Called inside the shared manager's lock; never starts/stops Compose itself."""
    checkpoint = validate_settings(manager.values, manager.root)
    image = function_image(manager.nuctl_check(model), manager.root)
    manager.guard_function_ownership([FUNCTION])
    current = next((item for item in manager.functions()
                    if item["Config"]["Labels"]["nuclio.io/function-name"] == FUNCTION), None)
    environment = runtime_environment(manager.values)
    if (not force and current and current["State"]["Running"]
            and current["Config"]["Image"] == image
            and all(f"{key}={value}" in current["Config"].get("Env", []) for key, value in environment.items())
            and checkpoint_matches(current, checkpoint)):
        expected = manager.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"]).strip()
        actual = manager.run(["docker", "inspect", current["Id"], "--format", "{{.Image}}"]).strip()
        if expected == actual:
            manager.verify_functions([FUNCTION], environments=manager.function_environment(),
                                     sam31_checkpoint=str(checkpoint))
            print(f"{FUNCTION}: source/config unchanged, reuse the running function")
            return
    common = ["--platform", "local", "--namespace", manager.values["NUCLIO_NAMESPACE"]]
    output = manager.run(["nuctl", "get", "projects", *common, "-o", "json"]).strip()
    projects = [] if output == "No projects found" else json.loads(output)

    def contains_project(value):
        if isinstance(value, dict):
            meta = value.get("meta", value.get("metadata", {}))
            return (isinstance(meta, dict) and meta.get("name") == "cvat") or any(
                contains_project(item) for item in value.values())
        return isinstance(value, list) and any(contains_project(item) for item in value)

    if not contains_project(projects):
        manager.run(["nuctl", "create", "project", "cvat", *common], stream=True)
    template = json.loads((manager.root / "components/sam31/function-gpu.json").read_text())
    rendered = render_function(template, manager.values, checkpoint, image)
    # Credentials are outside the staged source directory and never enter the image.
    with tempfile.TemporaryDirectory(dir=manager.state, prefix="sam31-source-") as staging:
        source = Path(staging)
        stage_sources(source, manager.root)
        fd, config = tempfile.mkstemp(dir=manager.state, prefix="sam31-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as target:
                json.dump(rendered, target)
            manager.run([
                "nuctl", "deploy", "--project-name", "cvat", "--path", str(source),
                "--file", config, *(["--run-image", image, "--no-pull"] if no_build else []),
                *common, "--platform-config", json.dumps({"attributes": {"network": manager.values["CVAT_NETWORK_NAME"]}}),
            ], stream=True)
        finally:
            Path(config).unlink(missing_ok=True)
    manager.verify_functions([FUNCTION], environments=manager.function_environment(),
                             sam31_checkpoint=str(checkpoint))


if __name__ == "__main__":
    # Compatibility entry point, using the same persistent state and lock as cvatctl.
    os.execv(sys.executable, [sys.executable, str(ROOT / "components/extensions/manage.py"),
                             *sys.argv[1:], "deploy-sam31"])
