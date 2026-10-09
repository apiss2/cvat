# SPDX-License-Identifier: MIT
"""Model selection, structured Nuclio definitions and one deployment path.

Only the standard library is needed on the deployment host. Named SAM weights
are downloaded into a private temporary build source, never mounted at runtime.
Hugging Face credentials are used by the downloader, not by the image builder.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {
    "sam2": "serverless/pytorch/facebookresearch/sam2/nuclio",
    "sam31": "components/sam31/nuclio",
    "ultrasam": "serverless/pytorch/camma/ultrasam/nuclio",
}
# Values are entry points, not filenames to be rewritten as YAML strings.
FUNCTIONS = {
    "sam2": {"pth-sam2-interactor": "main:handler", "pth-sam2-tracker": "tracker_main:handler"},
    "sam31": {"pth-sam31-interactor": "image_main:handler", "pth-sam31-tracker": "main:handler"},
    "ultrasam": {"pth-ultrasam-interactor": "main:handler"},
}
OWNERS = {name: feature for feature, functions in FUNCTIONS.items() for name in functions}
NAMES = {"sam2": "SAM2", "sam31": "SAM3.1", "ultrasam": "UltraSAM"}
SHARED = SOURCES["sam2"]
SAM2_MODELS = {
    "sam2.1_hiera_tiny": "configs/sam2.1/sam2.1_hiera_t.yaml",
    "sam2.1_hiera_small": "configs/sam2.1/sam2.1_hiera_s.yaml",
    "sam2.1_hiera_base_plus": "configs/sam2.1/sam2.1_hiera_b+.yaml",
    "sam2.1_hiera_large": "configs/sam2.1/sam2.1_hiera_l.yaml",
}
DEFAULTS = {
    "SAM2_MODEL": "sam2.1_hiera_small", "SAM2_CHECKPOINT_HOST": "",
    "SAM31_MODEL": "facebook/sam3.1", "SAM31_GPU_DEVICE": "0",
    "SAM31_REDIS_MAXMEMORY": "2gb", "SAM31_STATE_MAX_BYTES": str(256 * 1024**2),
    "SAM31_SESSION_TTL_SECONDS": "28800", "ULTRASAM_GPU_DEVICE": "0",
}


def model_environment(feature: str, values: dict[str, str]) -> dict[str, str]:
    if feature == "sam2":
        name = values.get("SAM2_MODEL") or DEFAULTS["SAM2_MODEL"]
        if name not in SAM2_MODELS:
            raise ValueError("SAM2_MODEL must be one of: " + ", ".join(SAM2_MODELS))
        return {"SAM2_MODEL": name, "SAM2_CONFIG": SAM2_MODELS[name],
                "SAM2_CHECKPOINT": f"/opt/nuclio/checkpoints/{name}.pt"}
    if feature == "sam31":
        name = values.get("SAM31_MODEL") or DEFAULTS["SAM31_MODEL"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name):
            raise ValueError("SAM31_MODEL must be a Hugging Face model name: owner/repository")
        return {"SAM31_MODEL": name, "SAM31_CHECKPOINT": "/opt/nuclio/sam3.1_multiplex.pt"}
    if feature != "ultrasam":
        raise ValueError(f"Unknown function family: {feature}")
    return {}


def local_checkpoint(values: dict[str, str], root: Path, *, verify_file=False) -> Path | None:
    """An optional SAM2 fine-tuning input; SAM3.1 has no host-checkpoint setting."""
    raw = values.get("SAM2_CHECKPOINT_HOST", "")
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("SAM2_CHECKPOINT_HOST must be an absolute path outside the checkout")
    path = path.resolve()
    if path == root.resolve() or root.resolve() in path.parents:
        raise ValueError("Keep source checkpoint weights outside the checkout")
    if verify_file and (not path.is_file() or not path.stat().st_size):
        raise ValueError("SAM2_CHECKPOINT_HOST must name a nonempty checkpoint file")
    return path


def validate_settings(feature: str, values: dict[str, str], root: Path = ROOT) -> None:
    values.update(model_environment(feature, values))
    if feature == "sam2":
        checkpoint = local_checkpoint(values, root)
        values["SAM2_CHECKPOINT_HOST"] = str(checkpoint) if checkpoint else ""
    prefix = feature.upper()
    if feature in ("sam31", "ultrasam"):
        values[prefix + "_GPU_DEVICE"] = values.get(prefix + "_GPU_DEVICE") or "0"
        if not re.fullmatch(r"[0-9]+|GPU-[A-Za-z0-9-]+", values[prefix + "_GPU_DEVICE"]):
            raise ValueError(f"{prefix}_GPU_DEVICE must be one numeric device ID or GPU UUID")
    if feature in ("sam2", "sam31"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", values.get(prefix + "_REDIS_PASSWORD", "")):
            raise ValueError(f"{prefix}_REDIS_PASSWORD must contain 32..128 ASCII letters, digits, _ or -")
        if not re.fullmatch(r"[1-9][0-9]*(?:[kKmMgG][bB]?)?", values.get(prefix + "_REDIS_MAXMEMORY") or "2gb"):
            raise ValueError(f"Invalid {prefix}_REDIS_MAXMEMORY")
    if feature == "sam31":
        for key, low, high in (("SAM31_STATE_MAX_BYTES", 1024, 512 * 1024**2),
                               ("SAM31_SESSION_TTL_SECONDS", 60, 604800)):
            value = values.get(key) or DEFAULTS[key]
            if not value.isdigit() or not low <= int(value) <= high:
                raise ValueError(f"Invalid {key}: expected {low}..{high}")


def runtime_environment(name: str, values: dict[str, str]) -> dict[str, str]:
    feature = OWNERS[name]
    prefix = feature.upper()
    result = model_environment(feature, values)
    if feature in ("sam31", "ultrasam"):
        result["CUDA_VISIBLE_DEVICES"] = values.get(prefix + "_GPU_DEVICE") or "0"
    if feature == "sam31":
        result["HF_HUB_OFFLINE"] = "1"
    if name.endswith("tracker"):
        result.update({
            prefix + "_REDIS_HOST": feature + "_redis", prefix + "_REDIS_PORT": "6379",
            prefix + "_REDIS_PREFIX": f"cvat:{feature}:", prefix + "_REDIS_PASSWORD": values[prefix + "_REDIS_PASSWORD"],
            prefix + "_STATE_MAX_BYTES": (values.get("SAM31_STATE_MAX_BYTES") or DEFAULTS["SAM31_STATE_MAX_BYTES"]) if feature == "sam31" else str(256 * 1024**2),
            prefix + "_SESSION_TTL_SECONDS": (values.get("SAM31_SESSION_TTL_SECONDS") or DEFAULTS["SAM31_SESSION_TTL_SECONDS"]) if feature == "sam31" else "28800",
        })
    return result


def render_function(name: str, values: dict[str, str], image: str, root: Path = ROOT) -> dict:
    feature = OWNERS[name]
    recipe = json.loads((root / f"components/{feature}/build.json").read_text())
    tracker = name.endswith("tracker")
    annotations = {
        "name": NAMES[feature] + (" polygon tracker" if tracker else "") + " (GPU" + (", experimental)" if feature == "sam31" else ")"),
        "type": "tracker" if tracker else "interactor", "version": "2", "spec": "[]",
    }
    if tracker:
        annotations["supported_shape_types"] = "polygon"
    else:
        annotations.update(min_pos_points="1", min_neg_points="0")
        if feature == "ultrasam":
            annotations["help_message"] = "Select an ultrasound object with positive points and exclude regions with negative points. Up to 128 total points. Image annotation only."
        else:
            annotations.update(startswith_box="true", startswith_box_optional="true",
                               help_message="Select an object with positive points; exclude regions with negative points. Optional box prompt. GPU inference returns a full CVAT mask.")
    build = copy.deepcopy(recipe["build"])
    build["image"] = image
    build["directives"]["preCopy"][:0] = [
        {"kind": "ENV", "value": "DEBIAN_FRONTEND=noninteractive"},
        {"kind": "ENV", "value": "NVIDIA_VISIBLE_DEVICES=all"},
        {"kind": "ENV", "value": "NVIDIA_DRIVER_CAPABILITIES=compute,utility"},
    ]
    return {
        "metadata": {"name": name, "namespace": values["NUCLIO_NAMESPACE"], "annotations": annotations},
        "spec": {
            "runtime": recipe["runtime"], "handler": FUNCTIONS[feature][name], "description": annotations["name"],
            "eventTimeout": "120s", "readinessTimeoutSeconds": 300, "minReplicas": 1, "maxReplicas": 1,
            "build": build,
            "triggers": {"http": {"kind": "http", "maxWorkers": 1, "workerAvailabilityTimeoutMilliseconds": 10000,
                                  "attributes": {"maxRequestBodySize": 33554432, "disablePortPublishing": True}}},
            "resources": {"limits": {"nvidia.com/gpu": 1}},
            "platform": {"attributes": {"restartPolicy": {"name": "unless-stopped"}, "mountMode": "volume"}},
            "env": [{"name": key, "value": value} for key, value in runtime_environment(name, values).items()],
        },
    }


def sources(feature: str, root: Path = ROOT) -> dict[str, bytes]:
    directory = root / SOURCES[feature]
    files = {path.name: path.read_bytes() for path in sorted(directory.iterdir())
             if path.is_file() and path.suffix in (".py", ".txt")}
    if feature in ("sam2", "sam31"):
        for name in ("protocol.py", "geometry.py", "redis_store.py"):
            files[name] = (root / SHARED / name).read_bytes()
    for handler in FUNCTIONS[feature].values():
        filename = handler.split(":")[0] + ".py"
        if filename not in files:
            raise ValueError(f"Missing function handler: {filename}")
    return files


def stage_sources(destination: Path, feature: str, root: Path = ROOT) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in sources(feature, root).items():
        (destination / name).write_bytes(content)  # Shared code is copied byte-for-byte.


def image_names(version: str, feature: str, values: dict[str, str], root: Path = ROOT) -> dict[str, str]:
    selection = model_environment(feature, values)
    if feature == "sam2":
        selection["source"] = values.get("SAM2_CHECKPOINT_HOST", "")
    digest = hashlib.sha256(version.encode() + b"\0" + json.dumps(selection, sort_keys=True).encode())
    files = sources(feature, root)
    for relative in ("components/extensions/functions.py", f"components/{feature}/build.json"):
        files[relative] = (root / relative).read_bytes()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0" + content)
    return {name: f"cvat.{name.removeprefix('pth-')}:{digest.hexdigest()[:20]}-gpu" for name in FUNCTIONS[feature]}


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("Refusing a non-HTTPS model download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_checkpoint(feature: str, values: dict[str, str], destination: Path, root: Path = ROOT) -> None:
    environment = model_environment(feature, values)
    target = destination / PurePosixPath(environment[feature.upper() + "_CHECKPOINT"]).relative_to("/opt/nuclio")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        local = local_checkpoint(values, root, verify_file=True) if feature == "sam2" else None
        if local:
            shutil.copyfile(local, target)
        else:
            if feature == "sam2":
                url = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/" + environment["SAM2_MODEL"] + ".pt"
            else:
                url = "https://huggingface.co/" + quote(environment["SAM31_MODEL"], safe="/") + "/resolve/main/sam3.1_multiplex.pt"
            request = Request(url)
            if feature == "sam31" and values.get("HF_TOKEN"):
                # urllib does not forward unredirected headers to signed CDN URLs.
                request.add_unredirected_header("Authorization", "Bearer " + values["HF_TOKEN"])
            with build_opener(HTTPSRedirectHandler()).open(request, timeout=60) as response, target.open("xb") as output:
                expected = response.headers.get("Content-Length")
                shutil.copyfileobj(response, output, length=8 * 1024**2)
            if expected is not None and target.stat().st_size != int(expected):
                raise ValueError("Model download ended before the advertised content length")
        if not target.stat().st_size:
            raise ValueError("Downloaded checkpoint is empty")
        target.chmod(0o444)
    except (HTTPError, URLError) as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, HTTPError) and exc.code in (401, 403):
            raise ValueError("Model access denied. Obtain access on Hugging Face and set HF_TOKEN in the cvatctl environment file.") from None
        raise ValueError("Model download failed; check connectivity and the selected model name, then retry.") from None
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def checkpoint_matches(container: dict, environment: dict[str, str]) -> bool:
    checkpoint = next((PurePosixPath(value) for key, value in environment.items() if key.endswith("_CHECKPOINT")), None)
    return checkpoint is None or not any(
        PurePosixPath(mount.get("Destination", "")) in (checkpoint, *checkpoint.parents)
        for mount in container.get("Mounts", [])
    )


def selected_functions(feature: str, mode: str) -> list[str]:
    if feature not in FUNCTIONS or mode not in ("all", "image", "tracker"):
        raise ValueError("Unknown function family or deployment mode")
    selected = [name for name in FUNCTIONS[feature]
                if mode == "all" or name.endswith("interactor" if mode == "image" else "tracker")]
    if not selected:
        raise ValueError("UltraSAM supports image prompts only; tracker is unavailable")
    return selected


def contains_project(value) -> bool:
    if isinstance(value, dict):
        meta = value.get("meta", value.get("metadata", {}))
        return (isinstance(meta, dict) and meta.get("name") == "cvat") or any(contains_project(item) for item in value.values())
    return isinstance(value, list) and any(contains_project(item) for item in value)


def deploy(manager, model: dict, *, feature: str, mode="all", force=False, no_build=False) -> None:
    """The caller holds cvatctl's deployment lock and owns Compose lifecycle/state."""
    selected = selected_functions(feature, mode)
    validate_settings(feature, manager.values, manager.root)
    if feature == "sam2":
        local_checkpoint(manager.values, manager.root, verify_file=not no_build)
    images = image_names(manager.nuctl_check(model), feature, manager.values, manager.root)
    manager.guard_function_ownership(selected)
    if no_build:
        for name in selected:
            manager.run(["docker", "image", "inspect", images[name], "--format", "{{.Id}}"])
    current = {item["Config"]["Labels"]["nuclio.io/function-name"]: item for item in manager.functions()}
    pending = []
    can_reuse = no_build or not (feature == "sam2" and manager.values.get("SAM2_CHECKPOINT_HOST"))
    for name in selected:
        item, env = current.get(name), runtime_environment(name, manager.values)
        if (not force and can_reuse and item and item["State"]["Running"] and item["Config"]["Image"] == images[name]
                and all(f"{key}={value}" in item["Config"].get("Env", []) for key, value in env.items())
                and checkpoint_matches(item, env)):
            expected = manager.run(["docker", "image", "inspect", images[name], "--format", "{{.Id}}"])
            actual = manager.run(["docker", "inspect", item["Id"], "--format", "{{.Image}}"])
            if expected.strip() == actual.strip():
                print(f"{name}: source/model/config unchanged, reuse the running function")
                continue
        pending.append(name)
    if pending:
        common = ["--platform", "local", "--namespace", manager.values["NUCLIO_NAMESPACE"]]
        with tempfile.TemporaryDirectory(dir=manager.state, prefix=feature + "-source-") as directory:
            source = Path(directory)
            stage_sources(source, feature, manager.root)
            if not no_build and feature in ("sam2", "sam31"):
                download_checkpoint(feature, manager.values, source, manager.root)
            output = manager.run(["nuctl", "get", "projects", *common, "-o", "json"]).strip()
            projects = [] if output == "No projects found" else json.loads(output)
            if not contains_project(projects):
                manager.run(["nuctl", "create", "project", "cvat", *common], stream=True)
            for name in pending:
                definition = render_function(name, manager.values, images[name], manager.root)
                fd, config = tempfile.mkstemp(dir=manager.state, prefix=feature + "-", suffix=".json")
                try:
                    with os.fdopen(fd, "w") as target:
                        json.dump(definition, target)
                    manager.run([
                        "nuctl", "deploy", "--project-name", "cvat", "--path", str(source), "--file", config,
                        *(["--run-image", images[name], "--no-pull"] if no_build else []), *common,
                        "--platform-config", json.dumps({"attributes": {"network": manager.values["CVAT_NETWORK_NAME"]}}),
                    ], stream=True)
                finally:
                    Path(config).unlink(missing_ok=True)
    manager.verify_functions(selected, environments=manager.function_environment())
