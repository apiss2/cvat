#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build and deploy SAM3.1 image/tracker functions through the common manager.

Weights are build inputs, not runtime bind mounts. Source, model selection and
Nuclio version identify the image tag; normal deployment always rebuilds so a
replacement at the same source path cannot reuse stale weights. Authentication tokens and
Redis credentials are never staged into the image build context.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FUNCTION = "pth-sam31-tracker"
IMAGE_FUNCTION = "pth-sam31-interactor"
FUNCTIONS = (IMAGE_FUNCTION, FUNCTION)
CHECKPOINT = "/opt/nuclio/sam3.1_multiplex.pt"
SHARED = "serverless/pytorch/facebookresearch/sam2/nuclio"
DEFAULTS = {
    "SAM31_GPU_DEVICE": "0",
    "SAM31_REDIS_MAXMEMORY": "2gb",
    "SAM31_STATE_MAX_BYTES": str(256 * 1024**2),
    "SAM31_SESSION_TTL_SECONDS": "28800",
}


def sources(root: Path) -> dict[str, bytes]:
    files = {path.name: path.read_bytes() for path in
             sorted((root / "components/sam31/nuclio").glob("*.py"))}
    if not {"main.py", "image_main.py", "image_model.py"} <= files.keys():
        raise ValueError("Missing SAM3.1 image/tracker handlers")
    for name in ("protocol.py", "geometry.py", "redis_store.py"):
        text = (root / SHARED / name).read_text()
        files[name] = text.replace("SAM2", "SAM31").replace("sam2", "sam31").encode()
    return files


def stage_sources(destination: Path, root: Path = ROOT, *,
                  checkpoint: Path | None = None) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in sources(root).items():
        (destination / name).write_bytes(content)
    if checkpoint is not None:
        target = destination / Path(CHECKPOINT).name
        shutil.copyfile(checkpoint, target)
        target.chmod(0o444)


def function_image(version: str, root: Path = ROOT, *, checkpoint_source: str = "",
                   function: str = FUNCTION) -> str:
    if function not in FUNCTIONS:
        raise ValueError("Unknown SAM3.1 function")
    digest = hashlib.sha256(version.encode() + b"\0" + checkpoint_source.encode())
    files = sources(root)
    for relative in ("components/sam31/function-gpu.json", "components/sam31/deploy.py",
                     "components/extensions/manage.py"):
        files[relative] = (root / relative).read_bytes()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + content)
    return f"cvat.{function.removeprefix('pth-')}:{digest.hexdigest()[:20]}-gpu"


def validate_settings(values: dict[str, str], root: Path = ROOT, *,
                      verify_file: bool = True) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", values.get("SAM31_REDIS_PASSWORD", "")):
        raise ValueError("SAM31_REDIS_PASSWORD must contain 32..128 ASCII letters, digits, _ or -")
    if not re.fullmatch(r"[0-9]+|GPU-[A-Za-z0-9-]+", values.get("SAM31_GPU_DEVICE") or "0"):
        raise ValueError("SAM31_GPU_DEVICE must be one numeric device ID or GPU UUID")
    if not re.fullmatch(r"[1-9][0-9]*(?:[kKmMgG][bB]?)?", values.get("SAM31_REDIS_MAXMEMORY") or "2gb"):
        raise ValueError("Invalid SAM31_REDIS_MAXMEMORY")
    for key, low, high in (("SAM31_STATE_MAX_BYTES", 1024, 512 * 1024**2),
                           ("SAM31_SESSION_TTL_SECONDS", 60, 604800)):
        value = values.get(key) or DEFAULTS[key]
        if not value.isdigit() or not low <= int(value) <= high:
            raise ValueError(f"Invalid {key}: expected {low}..{high}")
    # Retain the existing variable name for migration, but it is build-only now.
    raw = values.get("SAM31_CHECKPOINT_HOST", "")
    if not raw:
        if verify_file:
            raise ValueError("SAM31_CHECKPOINT_HOST must name an existing checkpoint file for image builds")
        return None
    checkpoint = Path(raw).expanduser()
    if not checkpoint.is_absolute():
        raise ValueError("SAM31_CHECKPOINT_HOST must be an absolute path outside the checkout")
    checkpoint = checkpoint.resolve()
    if checkpoint == root.resolve() or root.resolve() in checkpoint.parents:
        raise ValueError("Keep source checkpoint weights outside the checkout")
    if verify_file:
        if not checkpoint.is_file() or not checkpoint.stat().st_size:
            raise ValueError("SAM31_CHECKPOINT_HOST must name an existing checkpoint file for image builds")
    return checkpoint


def runtime_environment(values: dict[str, str], function: str = FUNCTION) -> dict[str, str]:
    if function not in FUNCTIONS:
        raise ValueError("Unknown SAM3.1 function")
    result = {
        "SAM31_CHECKPOINT": CHECKPOINT,
        "HF_HUB_OFFLINE": "1",
        "CUDA_VISIBLE_DEVICES": values.get("SAM31_GPU_DEVICE") or "0",
    }
    if function == FUNCTION:
        result.update({
            "SAM31_REDIS_HOST": "sam31_redis",
            "SAM31_REDIS_PORT": "6379",
            "SAM31_REDIS_PREFIX": "cvat:sam31:",
            "SAM31_REDIS_PASSWORD": values["SAM31_REDIS_PASSWORD"],
            "SAM31_STATE_MAX_BYTES": values.get("SAM31_STATE_MAX_BYTES") or DEFAULTS["SAM31_STATE_MAX_BYTES"],
            "SAM31_SESSION_TTL_SECONDS": values.get("SAM31_SESSION_TTL_SECONDS") or DEFAULTS["SAM31_SESSION_TTL_SECONDS"],
        })
    return result


def render_function(template: dict, values: dict[str, str], image: str,
                    function: str = FUNCTION) -> dict:
    result = copy.deepcopy(template)
    result["metadata"].update(name=function, namespace=values["NUCLIO_NAMESPACE"])
    result["spec"]["build"]["image"] = image
    result["spec"]["env"] = [{"name": name, "value": value}
                             for name, value in runtime_environment(values, function).items()]
    result["spec"].pop("volumes", None)
    if function == IMAGE_FUNCTION:
        result["metadata"]["annotations"] = {
            "name": "SAM3.1 (GPU, experimental)", "type": "interactor",
            "version": "2", "spec": "[]", "min_pos_points": "1", "min_neg_points": "0",
            "startswith_box": "true", "startswith_box_optional": "true",
            "help_message": "Select an object with positive points; exclude regions with negative points. "
                            "Optional box prompt. GPU inference returns a full CVAT mask.",
        }
        result["spec"]["handler"] = "image_main:handler"
        result["spec"]["description"] = "SAM3.1 single-image point/box interactor"
    return result


def checkpoint_matches(container: dict) -> bool:
    """The approved image must not have its embedded checkpoint shadowed by a mount."""
    checkpoint = PurePosixPath(CHECKPOINT)
    for mount in container.get("Mounts", []):
        destination = PurePosixPath(mount.get("Destination", ""))
        if (destination == checkpoint or destination in checkpoint.parents
                or str(destination) == "/models/sam3.1_multiplex.pt"):
            return False
    return True


def deploy(manager, model: dict, *, force: bool = False, no_build: bool = False,
           mode: str = "all") -> None:
    if mode not in ("all", "image", "tracker"):
        raise ValueError("Function mode must be all, image or tracker")
    checkpoint = validate_settings(manager.values, manager.root, verify_file=not no_build)
    selected = list(FUNCTIONS) if mode == "all" else [IMAGE_FUNCTION if mode == "image" else FUNCTION]
    version = manager.nuctl_check(model)
    images = {name: function_image(version, manager.root,
                                  checkpoint_source=manager.values.get("SAM31_CHECKPOINT_HOST", ""), function=name)
              for name in selected}
    manager.guard_function_ownership(selected)
    if no_build:
        # Fail before any deployment if one of the transferred images is missing.
        for image in images.values():
            manager.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    current = {item["Config"]["Labels"]["nuclio.io/function-name"]: item for item in manager.functions()}
    pending = []
    for name in selected:
        item = current.get(name)
        environment = runtime_environment(manager.values, name)
        if (no_build and not force and item and item["State"]["Running"]
                and item["Config"]["Image"] == images[name]
                and all(f"{key}={value}" in item["Config"].get("Env", []) for key, value in environment.items())
                and checkpoint_matches(item)):
            expected = manager.run(["docker", "image", "inspect", images[name], "--format", "{{.Id}}"]).strip()
            actual = manager.run(["docker", "inspect", item["Id"], "--format", "{{.Image}}"]).strip()
            if expected == actual:
                print(f"{name}: source/checkpoint/config unchanged, reuse the running function")
                continue
        pending.append(name)
    if pending:
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
        with tempfile.TemporaryDirectory(dir=manager.state, prefix="sam31-source-") as staging:
            source = Path(staging)
            stage_sources(source, manager.root, checkpoint=None if no_build else checkpoint)
            for name in pending:
                rendered = render_function(template, manager.values, images[name], name)
                # Secret runtime configuration must stay outside the source directory.
                fd, config = tempfile.mkstemp(dir=manager.state, prefix="sam31-", suffix=".json")
                try:
                    with os.fdopen(fd, "w") as target:
                        json.dump(rendered, target)
                    manager.run([
                        "nuctl", "deploy", "--project-name", "cvat", "--path", str(source),
                        "--file", config, *(["--run-image", images[name], "--no-pull"] if no_build else []),
                        *common, "--platform-config", json.dumps({"attributes": {"network": manager.values["CVAT_NETWORK_NAME"]}}),
                    ], stream=True)
                finally:
                    Path(config).unlink(missing_ok=True)
    manager.verify_functions(selected, environments=manager.function_environment())


if __name__ == "__main__":
    os.execv(sys.executable, [sys.executable, str(ROOT / "components/extensions/manage.py"),
                             "deploy-sam31", *sys.argv[1:]])
