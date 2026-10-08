#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Opt-in SAM3.1 deployment through existing CVAT extension settings.

No CVAT core patch, credentials in build layers, or automatic model downloads.
Uses the extension manager's Compose validation, ownership guards and log redaction.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SHARED = "serverless/pytorch/facebookresearch/sam2/nuclio"
FUNCTION = "pth-sam31-tracker"


def stage_sources(destination: Path, root: Path = ROOT) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for source in sorted((root / "components/sam31/nuclio").glob("*.py")):
        shutil.copyfile(source, destination / source.name)
    # Reuse only generic image/contour/Redis transport helpers, NOT the SAM2 model,
    # memory implementation, state codec, checkpoint or Python environment.
    for name in ("protocol.py", "geometry.py", "redis_store.py"):
        source = root / SHARED / name
        text = source.read_text().replace("SAM2", "SAM31").replace("sam2", "sam31")
        (destination / name).write_text(text)


def validate_settings(values: dict[str, str], root: Path = ROOT) -> tuple[Path, str]:
    if "plugins/sam31" not in values.get("CVAT_CLIENT_PLUGINS", "").split(":"):
        raise ValueError("Add plugins/sam31 to CVAT_CLIENT_PLUGINS and rebuild CVAT UI first")
    overlays = [p.strip() for p in values.get("CVAT_EXTRA_COMPOSE_FILES", "").split(";")]
    if "components/sam31/docker-compose.sam31.yml" not in overlays:
        raise ValueError("Add components/sam31/docker-compose.sam31.yml to CVAT_EXTRA_COMPOSE_FILES")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", values.get("SAM31_REDIS_PASSWORD", "")):
        raise ValueError("SAM31_REDIS_PASSWORD must contain 32..128 ASCII letters, digits, _ or -")
    if not re.fullmatch(r"[0-9]+|GPU-[A-Za-z0-9-]+", values.get("SAM31_GPU_DEVICE", "0")):
        raise ValueError("SAM31_GPU_DEVICE must be one numeric device ID or GPU UUID")
    digest = values.get("SAM31_CHECKPOINT_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Set SAM31_CHECKPOINT_SHA256 to the approved checkpoint's SHA256")
    supplied = values.get("SAM31_CHECKPOINT_HOST", "")
    if not supplied:
        raise ValueError("Set SAM31_CHECKPOINT_HOST to an already approved local checkpoint")
    path = Path(supplied).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_file():
        raise ValueError("SAM31_CHECKPOINT_HOST must name an existing file")
    if path.is_relative_to(root.resolve()):
        raise ValueError("Store SAM3.1 weights outside the CVAT checkout/build context")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise ValueError("Checkpoint SHA256 mismatch")
    limit = int(values.get("SAM31_STATE_MAX_BYTES", str(256 * 1024**2)))
    ttl = int(values.get("SAM31_SESSION_TTL_SECONDS", "28800"))
    if not 1024 <= limit <= 512 * 1024**2 or not 60 <= ttl <= 604800:
        raise ValueError("Invalid SAM3.1 state size or TTL")
    return path, digest


def render_function(template: dict, values: dict[str, str], checkpoint: Path, image: str) -> dict:
    result = json.loads(json.dumps(template))
    result["metadata"]["namespace"] = values["NUCLIO_NAMESPACE"]
    result["spec"]["build"]["image"] = image
    runtime = {
        "SAM31_REDIS_PASSWORD": values["SAM31_REDIS_PASSWORD"],
        "SAM31_CHECKPOINT_SHA256": values["SAM31_CHECKPOINT_SHA256"],
        "SAM31_STATE_MAX_BYTES": values.get("SAM31_STATE_MAX_BYTES", str(256 * 1024**2)),
        "SAM31_SESSION_TTL_SECONDS": values.get("SAM31_SESSION_TTL_SECONDS", "28800"),
    }
    for entry in result["spec"]["env"]:
        if entry["name"] == "CUDA_VISIBLE_DEVICES":
            entry["value"] = values.get("SAM31_GPU_DEVICE", "0")
    result["spec"]["env"] += [{"name": name, "value": value} for name, value in runtime.items()]
    result["spec"]["volumes"] = [{
        "volume": {"name": "sam31-checkpoint", "hostPath": {"path": str(checkpoint), "type": "File"}},
        "volumeMount": {"name": "sam31-checkpoint", "mountPath": "/models/sam3.1_multiplex.pt", "readOnly": True},
    }]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--no-build", action="store_true", help="Reuse the exact source-fingerprinted local image")
    args = parser.parse_args()
    # This is deliberately an optional component, not another patch to manage.py.
    sys.path.insert(0, str(ROOT / "components/extensions"))
    from manage import Manager

    with tempfile.TemporaryDirectory(prefix="cvat-sam31-") as temporary:
        stage = Path(temporary)
        os.chmod(stage, 0o700)
        manager = Manager(ROOT, args.env_file.resolve(), stage)
        for key in list(manager.env):
            if key.startswith("SAM31_") and key not in manager.values:
                manager.env.pop(key)
        checkpoint, _ = validate_settings(manager.values)
        model = manager.configuration()
        if not {"nuclio", "sam31_redis"}.issubset(model["services"]):
            raise ValueError("Start CVAT with the serverless and SAM3.1 Compose overlays first")
        # Do not implicitly alter or restart the rest of CVAT during deployment.
        redis_ids = manager.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={manager.project}",
                                 "--filter", "label=com.docker.compose.service=sam31_redis"]).split()
        if len(redis_ids) != 1:
            raise ValueError("SAM3.1 Redis is not running; apply the Compose overlay with cvatctl first")
        redis_info = json.loads(manager.run(["docker", "inspect", redis_ids[0]]))[0]
        if redis_info["State"].get("Health", {}).get("Status") != "healthy":
            raise ValueError("SAM3.1 Redis is not healthy")
        version = manager.nuctl_check(model)
        manager.functions()  # Reject foreign functions on the chosen CVAT network.
        manager.guard_function_ownership([FUNCTION])
        namespace = manager.values["NUCLIO_NAMESPACE"]
        common = ["--platform", "local", "--namespace", namespace]
        output = manager.run(["nuctl", "get", "projects", *common, "-o", "json"]).strip()
        projects = [] if not output or output == "No projects found" else json.loads(output)
        def has_project(value):
            if isinstance(value, dict):
                metadata = value.get("meta", value.get("metadata", {}))
                return (isinstance(metadata, dict) and metadata.get("name") == "cvat") or any(has_project(v) for v in value.values())
            return isinstance(value, list) and any(has_project(v) for v in value)
        if not has_project(projects):
            manager.run(["nuctl", "create", "project", "cvat", *common], stream=True)
        source = stage / "source"
        stage_sources(source)
        template_path = HERE / "function-gpu.json"
        digest = hashlib.sha256(version.encode() + template_path.read_bytes())
        for path in sorted(source.glob("*.py")):
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
        digest.update(Path(__file__).read_bytes())
        image = f"cvat.sam31-tracker:{digest.hexdigest()[:20]}-gpu"
        definition = render_function(json.loads(template_path.read_text()), manager.values, checkpoint, image)
        config = stage / "function.json"
        config.write_text(json.dumps(definition, indent=2))
        os.chmod(config, 0o600)  # Contains runtime Redis credential, never copied into build context.
        manager.run([
            "nuctl", "deploy", "--project-name", "cvat", "--path", str(source), "--file", str(config),
            *(["--run-image", image, "--no-pull"] if args.no_build else []), *common,
            "--platform-config", json.dumps({"attributes": {"network": manager.values["CVAT_NETWORK_NAME"]}}),
        ], stream=True)
        print("SAM3.1 tracker deployed. Run the GPU smoke test before using production annotations.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
