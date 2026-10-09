#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Manage the selected CVAT extensions and their Compose deployment.

Requires Python >=3.10, Git, Docker Compose >=2.24.4; SAM2/UltraSAM/SAM3.1 require nuctl.
Settings are literal assignments in .env. Runtime snapshots live OUTSIDE the
build context. No shell evaluation, volume deletion, or automatic DB rollback.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "components/model_registry"))
from registryctl import (
    connection_values,
    public_address,
    purge_deleted as purge_registry_model,
)
from storage import default_home, prepare_storage, storage_paths

sys.path.insert(0, str(ROOT))
from components.sam31 import deploy as sam31_deploy

SERVERLESS_COMPOSE = "components/serverless/docker-compose.serverless.yml"
LOCAL_REGISTRY_COMPOSE = (
    "components/model_registry/docker-compose.registry.yml",
    "components/model_registry/docker-compose.local.yml",
)
EXTENSION_COMPOSE = {
    "itgformat": ("components/itgformat/docker-compose.itgformat.yml",),
    "sam2": (
        "components/sam2/docker-compose.redis.yml",
        "components/sam2/docker-compose.sam2.yml",
    ),
    "ultrasam": ("components/ultrasam/docker-compose.ultrasam.yml",),
    "sam31": ("components/sam31/docker-compose.sam31.yml",),
    "model_registry": ("components/model_registry/docker-compose.gateway.yml",),
}
EXTENSION_PLUGINS = {
    "sam2": "plugins/sam2",
    "sam31": "plugins/sam31",
    "model_registry": "plugins/model-registry",
}
FINAL_COMPOSE = "components/extensions/docker-compose.extensions.yml"
COMPOSE_FILES = (
    "docker-compose.yml",
    SERVERLESS_COMPOSE,
    *(path for paths in EXTENSION_COMPOSE.values() for path in paths),
    *LOCAL_REGISTRY_COMPOSE,
    FINAL_COMPOSE,
)
FUNCTION_FILES = {
    "pth-sam2-interactor": "function-gpu.yaml",
    "pth-sam2-tracker": "tracker-gpu.yaml",
}
NUCLIO_SOURCE = "serverless/pytorch/facebookresearch/sam2/nuclio"
ULTRASAM_SOURCE = "serverless/pytorch/camma/ultrasam/nuclio"
FUNCTION_SOURCES = {"sam2": NUCLIO_SOURCE, "ultrasam": ULTRASAM_SOURCE}
FUNCTION_DEFINITIONS = {
    "sam2": FUNCTION_FILES,
    "ultrasam": {"pth-ultrasam-interactor": "function-gpu.yaml"},
    "sam31": {name: "function-gpu.json" for name in sam31_deploy.FUNCTIONS},
}
FUNCTION_OWNERS = {
    name: feature for feature, files in FUNCTION_DEFINITIONS.items() for name in files
}
SERVERLESS_EXTENSIONS = {"sam2", "ultrasam", "sam31", "model_registry"}
DEFAULTS = {
    **sam31_deploy.DEFAULTS,
    "CVAT_HOST": "localhost",
    "COMPOSE_PROJECT_NAME": "cvat",
    "NUCLIO_NAMESPACE": "nuclio",
    "NUCLIO_DASHBOARD_PORT": "8070",
    "SAM2_REDIS_IMAGE": "redis:7.2.11-alpine",
    "SAM2_REDIS_MAXMEMORY": "2gb",
    "ULTRASAM_GPU_DEVICE": "0",
    "CVAT_EXTRA_COMPOSE_FILES": "",
}


class OperationError(RuntimeError):
    pass


def enabled_extensions(values: dict[str, str]) -> tuple[str, ...]:
    # Retain the previous deployment when an existing .env has not been updated.
    raw = values.get("CVAT_EXTENSIONS")
    if raw is None:
        raw = "itgformat,sam2"
        if values.get("CVAT_MODEL_REGISTRY_ENABLED") == "1":
            raw += ",model_registry"
    selected = [item.strip() for item in raw.split(",")] if raw.strip() else []
    if len(set(selected)) != len(selected) or any(
        item not in EXTENSION_COMPOSE for item in selected
    ):
        raise OperationError(
            "CVAT_EXTENSIONS must contain unique names: itgformat,sam2,ultrasam,sam31,model_registry"
        )
    return tuple(name for name in EXTENSION_COMPOSE if name in selected)


def load_env(path: Path) -> dict[str, str]:
    """Parse deliberately literal single-line dotenv assignments, never shell code."""
    if not path.is_file():
        raise OperationError(f"Missing {path}. Run cvatctl init first.")
    if path.stat().st_mode & 0o077:
        raise OperationError(f"{path} contains secrets; run chmod 600 on it.")
    values: dict[str, str] = {}
    for number, original in enumerate(path.read_text().splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if not match:
            raise OperationError(f"{path}:{number}: expected literal KEY=VALUE")
        key, raw = match.groups()
        if key in values:
            raise OperationError(f"{path}:{number}: duplicate {key}")
        parts = shlex.split(raw, comments=True, posix=True)
        if len(parts) > 1:
            raise OperationError(f"{path}:{number}: quote values containing spaces")
        value = parts[0] if parts else ""
        if "$" in value or "`" in value:
            raise OperationError(
                f"{path}:{number}: variable/command expansion is not supported"
            )
        values[key] = value
    return values


def settings(path: Path, root: Path = ROOT) -> dict[str, str]:
    values = DEFAULTS | load_env(path)
    selected = enabled_extensions(values)
    values["CVAT_EXTENSIONS"] = ",".join(selected)
    values["CVAT_MODEL_REGISTRY_ENABLED"] = "1" if "model_registry" in selected else "0"
    plugins = [
        p
        for p in values.get("CVAT_CLIENT_PLUGINS", "").split(":")
        if p and p not in EXTENSION_PLUGINS.values()
    ]
    plugins += [
        plugin for feature, plugin in EXTENSION_PLUGINS.items() if feature in selected
    ]
    values["CVAT_CLIENT_PLUGINS"] = ":".join(dict.fromkeys(plugins))
    values["CVAT_AI_GATEWAY_HOST"] = (
        "model-gateway" if "model_registry" in selected else "nuclio"
    )
    values["CVAT_AI_GATEWAY_PORT"] = "8070"
    values["CVAT_AI_GATEWAY_TIMEOUT"] = values.get("CVAT_AI_GATEWAY_TIMEOUT") or (
        "300" if "model_registry" in selected else "120"
    )
    for key in ("COMPOSE_PROJECT_NAME", "NUCLIO_NAMESPACE"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", values[key]):
            raise OperationError(f"Invalid {key}")
    project = values["COMPOSE_PROJECT_NAME"]
    values["CVAT_NETWORK_NAME"] = values.get("CVAT_NETWORK_NAME") or f"{project}_cvat"
    values["SAM2_REDIS_VOLUME"] = (
        values.get("SAM2_REDIS_VOLUME") or f"{project}_sam2_redis_data"
    )
    values["SAM31_REDIS_VOLUME"] = (
        values.get("SAM31_REDIS_VOLUME") or f"{project}_sam31_redis_data"
    )
    for key in ("CVAT_NETWORK_NAME", "SAM2_REDIS_VOLUME", "SAM31_REDIS_VOLUME"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", values[key]):
            raise OperationError(f"Invalid {key}")
    if "sam2" in selected and not re.fullmatch(
        r"[A-Za-z0-9_-]{32,128}", values.get("SAM2_REDIS_PASSWORD", "")
    ):
        raise OperationError(
            "SAM2_REDIS_PASSWORD must contain 32–128 ASCII letters, digits, _ or -"
        )
    if (
        not values["NUCLIO_DASHBOARD_PORT"].isdigit()
        or not 1 <= int(values["NUCLIO_DASHBOARD_PORT"]) <= 65535
    ):
        raise OperationError("Invalid NUCLIO_DASHBOARD_PORT")
    if "sam2" in selected and not re.fullmatch(
        r"[1-9][0-9]*(?:[kKmMgG][bB]?)?", values["SAM2_REDIS_MAXMEMORY"]
    ):
        raise OperationError("Invalid SAM2_REDIS_MAXMEMORY (example: 2gb)")
    if "sam31" in selected:
        checkpoint = sam31_deploy.validate_settings(
            values, root, verify_file=False, require_sha256=False,
        )
        values["SAM31_CHECKPOINT_HOST"] = str(checkpoint) if checkpoint is not None else ""
        if "sam2" in selected and values["SAM31_REDIS_VOLUME"] == values["SAM2_REDIS_VOLUME"]:
            raise OperationError("SAM2 and SAM3.1 must use separate Redis volumes")
    if "ultrasam" in selected:
        values["ULTRASAM_GPU_DEVICE"] = values.get("ULTRASAM_GPU_DEVICE") or "0"
        if not re.fullmatch(r"[0-9]+|GPU-[A-Za-z0-9-]+", values["ULTRASAM_GPU_DEVICE"]):
            raise OperationError(
                "ULTRASAM_GPU_DEVICE must be one numeric device ID or GPU UUID"
            )
    if "model_registry" in selected:
        values["CVAT_MODEL_REGISTRY_URL"] = (
            values.get("CVAT_MODEL_REGISTRY_URL") or "http://model-registry:8091"
        ).rstrip("/")
        if local_registry(values):
            home = Path(values.get("MR_HOME") or default_home(root)).expanduser()
            if not home.is_absolute():
                home = root / home
            home, _, _ = storage_paths(root, home)
            values["MR_HOME"] = str(home)
            derived = {
                "MR_HOST_DATA_DIR": str(home / "data"),
                "MR_CONFIG_DIR": str(home / "config"),
                "CVAT_MODEL_GATEWAY_SECRETS_DIR": str(home / "config/secrets"),
                "MR_CVAT_NETWORK": values["CVAT_NETWORK_NAME"],
                "MR_NAMESPACE": values["NUCLIO_NAMESPACE"],
            }
            for key, expected in derived.items():
                if values.get(key) and values[key] != expected:
                    raise OperationError(
                        f"{key} differs from the shared deployment setting. "
                        "Set MR_HOME to the existing registry home when adopting existing data; "
                        "do not copy or replace its service token."
                    )
                values[key] = expected
            values["MR_INSTANCE"] = values.get("MR_INSTANCE") or project
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", values["MR_INSTANCE"]):
                raise OperationError(
                    "MR_INSTANCE must be 1-31 lowercase letters, digits, _ or -"
                )
            gpu = values.get("MR_GPU_DEVICE", "")
            if gpu and not re.fullmatch(r"[0-9]+|GPU-[A-Za-z0-9-]+", gpu):
                raise OperationError(
                    "MR_GPU_DEVICE must be one numeric device ID or GPU UUID"
                )
            # The public origin can use HTTPS or a non-default port. Do not infer
            # it from CVAT_HOST, which only identifies a Traefik routing host.
            if not values.get("MR_PUBLIC_URL"):
                raise OperationError(
                    "Set MR_PUBLIC_URL to the CVAT browser URL followed by /model-registry/"
                )
            values["MR_PUBLIC_URL"], values["MR_PUBLIC_HOST"] = public_address(
                values["MR_PUBLIC_URL"]
            )
            connection_values(values)
        elif not values.get("CVAT_MODEL_GATEWAY_SECRETS_DIR"):
            raise OperationError(
                "A remote registry requires CVAT_MODEL_GATEWAY_SECRETS_DIR"
            )
    return values


def local_registry(values: dict[str, str]) -> bool:
    return (
        values.get("CVAT_MODEL_REGISTRY_URL", "http://model-registry:8091").rstrip("/")
        == "http://model-registry:8091"
    )


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text()) if path.is_file() else default


def source_tag(root: Path) -> str:
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], stderr=subprocess.PIPE
        )

    try:
        head = git("rev-parse", "HEAD").decode().strip()
        diff = git("diff", "HEAD", "--binary")
        names = git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    except subprocess.CalledProcessError as exc:
        raise OperationError(
            "A complete Git checkout with an initial commit is required"
        ) from exc
    h = hashlib.sha256(diff)
    dirty = bool(diff)
    for name in sorted(n for n in names if n):
        file = root / os.fsdecode(name)
        if file.is_file():
            dirty = True
            h.update(name + b"\0" + file.read_bytes())
    branch = git("branch", "--show-current").decode().strip()
    if branch == "release" and dirty:
        raise OperationError(
            "The release working tree is dirty. Commit reviewed source changes before building."
        )
    return head[:12] + (f"-work-{h.hexdigest()[:12]}" if dirty else "")


def validate_model(model: dict[str, Any], env: dict[str, str]) -> None:
    services = model["services"]
    selected = enabled_extensions(env)
    required = {
        "cvat_server",
        "cvat_ui",
        "cvat_worker_import",
        "cvat_worker_export",
        "cvat_worker_annotation",
    }
    if set(selected) & SERVERLESS_EXTENSIONS:
        required.add("nuclio")
    for feature in ("sam2", "sam31"):
        if feature in selected:
            required.add(feature + "_redis")
    if "model_registry" in selected:
        required.add("model-gateway")
        if local_registry(env):
            required.add("model-registry")
    if missing := required - services.keys():
        raise OperationError(f"Missing required services: {sorted(missing)}")
    backend = env["CVAT_EXT_SERVER_IMAGE"]
    for name, service in services.items():
        image = service.get("image", "")
        if (
            name == "cvat_server"
            or name.startswith("cvat_worker_")
            or image.startswith("cvat/server:")
        ):
            if image != backend:
                raise OperationError(
                    f"{name} does not use the selected backend image. Review the upstream service change."
                )
            if service.get("pull_policy") != "never":
                raise OperationError(
                    f"{name}: local backend must have pull_policy=never"
                )
    ui = services["cvat_ui"]
    if ui.get("image") != env["CVAT_EXT_UI_IMAGE"] or ui.get("pull_policy") != "never":
        raise OperationError("UI image override lost")
    if Path(ui.get("build", {}).get("dockerfile", "")).name != "Dockerfile.ui":
        raise OperationError("cvat_ui must use Dockerfile.ui")
    plugins = set(ui["build"].get("args", {}).get("CLIENT_PLUGINS", "").split(":"))
    for feature, plugin in EXTENSION_PLUGINS.items():
        if feature in selected and plugin not in plugins:
            raise OperationError(f"{feature}: CLIENT_PLUGINS argument lost")
    registry_enabled = "model_registry" in selected
    if registry_enabled:
        if "plugins/model-registry" not in plugins:
            raise OperationError("Model registry list-refresh plugin is missing")
        gateway = services.get("model-gateway", {})
        if not gateway or gateway.get("ports"):
            raise OperationError(
                "Model gateway must exist without published host ports"
            )
        if gateway.get("pull_policy") != "never":
            raise OperationError(
                "Build the model gateway locally; pull_policy must be never"
            )
        if (
            env.get("CVAT_MODEL_GATEWAY_IMAGE")
            and gateway.get("image") != env["CVAT_MODEL_GATEWAY_IMAGE"]
        ):
            raise OperationError("Model gateway image override lost")
        if local_registry(env):
            manager = services["model-registry"]
            if manager.get("ports"):
                raise OperationError("Model registry must not publish host ports")
            if manager.get("pull_policy") != "never" or manager.get("image") != env.get(
                "MR_MANAGER_IMAGE"
            ):
                raise OperationError("Model registry manager image override lost")
            if manager.get("environment", {}).get("MR_WORKER_IMAGE") != env.get(
                "MR_WORKER_IMAGE"
            ):
                raise OperationError("ONNX worker image override lost")
            if "cvat" not in manager.get("networks", {}):
                raise OperationError("Model registry must use the CVAT network")
        for service_name in ("cvat_server", "cvat_worker_annotation"):
            config = services[service_name].get("environment", {})
            if (
                config.get("CVAT_NUCLIO_HOST") != "model-gateway"
                or str(config.get("CVAT_NUCLIO_PORT")) != "8070"
            ):
                raise OperationError(f"{service_name}: model gateway route is missing")
            if int(config.get("CVAT_NUCLIO_DEFAULT_TIMEOUT", 0)) < 210:
                raise OperationError(
                    f"{service_name}: allow at least 210 seconds for cold model loads"
                )
    network = model.get("networks", {}).get("cvat", {})
    if (
        network.get("external") is not True
        or network.get("name") != env["CVAT_NETWORK_NAME"]
    ):
        raise OperationError("CVAT network must be the configured external network")
    for feature in ("sam2", "sam31"):
        if feature not in selected:
            continue
        volume = model.get("volumes", {}).get(feature + "_redis_data", {})
        if volume.get("external") is not True or volume.get("name") != env[feature.upper() + "_REDIS_VOLUME"]:
            raise OperationError(f"{feature} Redis must use the configured external volume")
        if services[feature + "_redis"].get("ports"):
            raise OperationError(f"{feature} Redis must not publish host ports")
    for port in services.get("nuclio", {}).get("ports", []):
        if not isinstance(port, dict) or port.get("host_ip") not in (
            "127.0.0.1",
            "::1",
        ):
            raise OperationError("Nuclio dashboard must only bind to loopback")
    for name in ("cvat_server", "cvat_worker_annotation"):
        if not set(selected) & SERVERLESS_EXTENSIONS:
            break
        e = services[name].get("environment", {})
        if e.get("CVAT_NUCLIO_INVOKE_METHOD") != "dashboard":
            raise OperationError(
                f"{name}: serverless functions require dashboard invocation"
            )
        if e.get("CVAT_NUCLIO_FUNCTION_NAMESPACE") != env["NUCLIO_NAMESPACE"]:
            raise OperationError(f"{name}: Nuclio namespace mismatch")


def render_function(
    source: str, namespace: str, image: str, password: str | None
) -> str:
    def replace_once(pattern: str, replacement: str, text: str) -> str:
        result, count = re.subn(
            pattern, lambda _: replacement, text, flags=re.MULTILINE
        )
        if count != 1:
            raise OperationError(
                "Nuclio YAML layout changed. Review render_function before deployment."
            )
        return result

    source = replace_once(r"^  namespace: .+$", f"  namespace: {namespace}", source)
    source = replace_once(r"^    image: .+$", f"    image: {image}", source)
    if password is not None:
        if "SAM2_REDIS_PASSWORD" in source or "\n  env:\n" not in source:
            raise OperationError("Unexpected tracker environment layout")
        source = (
            source.rstrip()
            + "\n  - name: SAM2_REDIS_PASSWORD\n    value: "
            + json.dumps(password)
            + "\n"
        )
    return source


def render_function_environment(source: str, name: str, value: str) -> str:
    """Set a runtime variable in the reviewed function YAML without extra packages."""
    entry = f"  - name: {name}\n    value: {json.dumps(value)}"
    pattern = rf"^  - name: {re.escape(name)}\n    value: [^\n]*$"
    rendered, count = re.subn(pattern, lambda _: entry, source, flags=re.MULTILINE)
    if count == 1:
        return rendered
    if count or re.search(rf"^\s*- name: {re.escape(name)}\s*$", source, re.MULTILINE):
        raise OperationError(f"Unexpected {name} environment layout")
    headers = list(re.finditer(r"^  env:\s*$", source, re.MULTILINE))
    if not headers:
        return source.rstrip() + "\n  env:\n" + entry + "\n"
    if len(headers) != 1 or not source[headers[0].end() :].startswith("\n  - name:"):
        raise OperationError("Unexpected function environment layout")
    position = headers[0].end()
    return source[:position] + "\n" + entry + source[position:]


class Manager:
    def __init__(self, root: Path, env_file: Path, state: Path):
        self.root, self.env_file, self.state = root, env_file, state
        self.values = settings(env_file, root)
        self.extensions = enabled_extensions(self.values)
        self.project = self.values["COMPOSE_PROJECT_NAME"]
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith(
                (
                    "CVAT_",
                    "COMPOSE_",
                    "SAM2_",
                    "SAM31_",
                    "ULTRASAM_",
                    "NUCLIO_",
                    "ITGFORMAT_",
                    "MR_",
                )
            ):
                self.env.pop(key)
        self.env.update(self.values)  # Persistent config wins over old shell exports.
        self.env["COMPOSE_IGNORE_ORPHANS"] = "true"

    def redact(self, text: str) -> str:
        for key, value in self.values.items():
            if value and any(
                word in key.upper()
                for word in ("PASSWORD", "TOKEN", "SECRET", "CREDENTIAL")
            ):
                text = text.replace(value, "<redacted>")
        return text

    def run(
        self, args: list[str], *, stream: bool = False, input_text: str | None = None
    ) -> str:
        if stream:
            with subprocess.Popen(
                args,
                cwd=self.root,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ) as process:
                assert process.stdout is not None
                for line in process.stdout:
                    print(self.redact(line), end="", flush=True)
                code = process.wait()
                if code:
                    raise OperationError(f"{args[0]} failed (exit {code})")
            return ""
        result = subprocess.run(
            args,
            cwd=self.root,
            env=self.env,
            input=input_text,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise OperationError(
                self.redact(result.stderr or result.stdout or f"{args[0]} failed")
            )
        return result.stdout

    def version_check(self) -> None:
        output = self.run(["docker", "compose", "version", "--short"])
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if not match or tuple(map(int, match.groups())) < (2, 24, 4):
            raise OperationError("Docker Compose >=2.24.4 is required")

    def compose(self, *args: str, frozen: bool = False) -> list[str]:
        command = [
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "-p",
            self.project,
        ]
        if frozen:
            command += [
                "--env-file",
                "/dev/null",
                "-f",
                str(self.state / "active.compose.json"),
            ]
        else:
            command += ["--env-file", str(self.env_file)]
            # Site configuration is loaded after upstream but before mandatory extension overrides.
            paths = ["docker-compose.yml"]
            if set(self.extensions) & SERVERLESS_EXTENSIONS:
                paths.append(SERVERLESS_COMPOSE)
            paths += [
                p.strip()
                for p in self.values["CVAT_EXTRA_COMPOSE_FILES"].split(";")
                if p.strip()
            ]
            for feature in self.extensions:
                paths.extend(EXTENSION_COMPOSE[feature])
            if "model_registry" in self.extensions and local_registry(self.values):
                paths.extend(LOCAL_REGISTRY_COMPOSE)
            paths.append(FINAL_COMPOSE)
            # Older .env files may still list the now automatic gateway overlay.
            for relative in dict.fromkeys(paths):
                file = (self.root / relative).resolve()
                if not file.is_file():
                    raise OperationError(f"Missing Compose file: {file}")
                command += ["-f", str(file)]
        return command + list(args)

    def configuration(self) -> dict[str, Any]:
        self.version_check()
        source_files = [
            "Dockerfile",
            "Dockerfile.ui",
            ".dockerignore",
        ]
        source_files += [
            f"cvat-ui/{plugin}/src/ts/index.tsx"
            for feature, plugin in EXTENSION_PLUGINS.items()
            if feature in self.extensions
        ]
        if "model_registry" in self.extensions:
            source_files.append("components/model_registry/Dockerfile")
            if local_registry(self.values):
                worker_file = (
                    "Dockerfile.worker.gpu"
                    if self.values.get("MR_GPU_DEVICE")
                    else "Dockerfile.worker"
                )
                source_files.append("components/model_registry/" + worker_file)
        for path in source_files:
            if not (self.root / path).is_file():
                raise OperationError(
                    f"Missing source: {path}. Use a complete CVAT checkout."
                )
        if "itgformat" in self.extensions:
            registry = (
                self.root / "cvat/apps/dataset_manager/formats/registry.py"
            ).read_text()
            if "import cvat.apps.dataset_manager.formats.itgformat" not in registry:
                raise OperationError("ITGformat registration is missing")
        tag = source_tag(self.root)
        plugin_tag = hashlib.sha256(
            self.env["CVAT_CLIENT_PLUGINS"].encode()
        ).hexdigest()[:8]
        self.env["CVAT_EXT_SERVER_IMAGE"] = f"cvat-local/server:extensions-{tag}"
        self.env["CVAT_EXT_UI_IMAGE"] = (
            f"cvat-local/ui:extensions-{tag}-plugins-{plugin_tag}"
        )
        if "model_registry" in self.extensions:
            self.env["CVAT_MODEL_GATEWAY_IMAGE"] = (
                f"cvat-local/model-gateway:extensions-{tag}"
            )
            if local_registry(self.values):
                self.env["MR_MANAGER_IMAGE"] = (
                    f"cvat-local/model-registry:extensions-{tag}"
                )
                variant = "gpu" if self.values.get("MR_GPU_DEVICE") else "cpu"
                self.env["MR_WORKER_IMAGE"] = (
                    f"cvat-local/onnx-worker:extensions-{tag}-{variant}"
                )
        model = json.loads(self.run(self.compose("config", "--format", "json")))
        validate_model(model, self.env)
        return model

    def identity(self) -> dict[str, str]:
        return {
            "project": self.project,
            "network": self.values["CVAT_NETWORK_NAME"],
            "namespace": self.values["NUCLIO_NAMESPACE"],
            "daemon": self.run(["docker", "info", "--format", "{{.ID}}"]).strip(),
        }

    def assert_identity(self, saved: dict[str, Any]) -> None:
        if saved["identity"] != self.identity():
            raise OperationError(
                "Docker daemon/project/network/namespace changed. Restore the previous settings first."
            )

    def freeze(
        self, model: dict[str, Any], images: dict[str, str] | None = None
    ) -> None:
        # Compose's canonical JSON already escapes literal dollars for reuse.
        atomic_json(self.state / "active.compose.json", model)
        atomic_json(
            self.state / "active.json",
            {
                "identity": self.identity(),
                "model": model,
                "extensions": self.extensions,
                "images": images or {},
                "function_environment": self.function_environment(),
                # A non-null legacy value denotes an external-checkpoint deployment.
                "sam31_checkpoint": None,
            },
        )

    def function_environment(self) -> dict[str, dict[str, str]]:
        environments = {}
        if "ultrasam" in self.extensions:
            environments["pth-ultrasam-interactor"] = {
                "CUDA_VISIBLE_DEVICES": self.values["ULTRASAM_GPU_DEVICE"],
            }
        if "sam31" in self.extensions:
            for name in sam31_deploy.FUNCTIONS:
                environments[name] = sam31_deploy.runtime_environment(self.values, name)
        return environments

    def matches_configuration(
        self, saved: dict[str, Any], model: dict[str, Any]
    ) -> bool:
        return (
            saved["model"] == model
            and tuple(saved.get("extensions", ("itgformat", "sam2"))) == self.extensions
            and saved.get("function_environment", {}) == self.function_environment()
            and saved.get("sam31_checkpoint") is None
        )

    def local_image_ids(self, model: dict[str, Any]) -> dict[str, str]:
        images = dict.fromkeys(
            service["image"]
            for service in model["services"].values()
            if service.get("pull_policy") == "never"
        )
        if manager := model["services"].get("model-registry"):
            images[manager["environment"]["MR_WORKER_IMAGE"]] = None
        return {
            image: self.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    image,
                    "--format",
                    "{{.Id}}",
                ]
            ).strip()
            for image in images
        }

    def functions(self) -> list[dict[str, Any]]:
        ids = self.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=nuclio.io/function-name",
                "--filter",
                f"network={self.values['CVAT_NETWORK_NAME']}",
            ]
        ).split()
        items = json.loads(self.run(["docker", "inspect", *ids])) if ids else []
        for item in items:
            labels = item["Config"].get("Labels") or {}
            if (
                labels.get("nuclio.io/project-name") != "cvat"
                or labels.get("nuclio.io/namespace") != self.values["NUCLIO_NAMESPACE"]
            ):
                raise OperationError(
                    "The selected network contains a foreign Nuclio function. Use a dedicated network or review its namespace."
                )
        return items

    def ensure_network(self) -> None:
        name = self.values["CVAT_NETWORK_NAME"]
        names = self.run(
            ["docker", "network", "ls", "--format", "{{.Name}}"]
        ).splitlines()
        if name not in names:
            self.run(["docker", "network", "create", name])
        else:
            item = json.loads(self.run(["docker", "network", "inspect", name]))[0]
            if item.get("Driver") != "bridge":
                raise OperationError(
                    "Only the local Docker bridge deployment is supported"
                )

    def ensure_volume(self, feature: str = "sam2") -> None:
        name = self.values[feature.upper() + "_REDIS_VOLUME"]
        users = self.run(["docker", "ps", "-q", "--filter", "volume=" + name]).split()
        if users:
            for item in json.loads(self.run(["docker", "inspect", *users])):
                labels = item["Config"].get("Labels") or {}
                if (
                    labels.get("com.docker.compose.project") != self.project
                    or labels.get("com.docker.compose.service") != feature + "_redis"
                ):
                    raise OperationError(
                        f"The {feature} Redis volume is used by another running container. Stop the old Redis first."
                    )
        names = self.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"]
        ).splitlines()
        if name not in names:
            self.run(["docker", "volume", "create", name])

    def build(self, model: dict[str, Any] | None = None) -> dict[str, Any]:
        if model is None:
            self.prepare_registry()
            model = self.configuration()
        self.run(self.compose("build", "cvat_server", "cvat_ui"), stream=True)
        if "model_registry" in self.extensions:
            context = self.root / "components/model_registry"
            self.run(
                [
                    "docker",
                    "build",
                    "-f",
                    str(context / "Dockerfile"),
                    "--target",
                    "gateway",
                    "-t",
                    model["services"]["model-gateway"]["image"],
                    str(context),
                ],
                stream=True,
            )
            if manager := model["services"].get("model-registry"):
                self.run(
                    [
                        "docker",
                        "build",
                        "-f",
                        str(context / "Dockerfile"),
                        "--target",
                        "manager",
                        "-t",
                        manager["image"],
                        str(context),
                    ],
                    stream=True,
                )
                worker_file = (
                    "Dockerfile.worker.gpu"
                    if self.values.get("MR_GPU_DEVICE")
                    else "Dockerfile.worker"
                )
                self.run(
                    [
                        "docker",
                        "build",
                        "-f",
                        str(context / worker_file),
                        "-t",
                        manager["environment"]["MR_WORKER_IMAGE"],
                        str(context),
                    ],
                    stream=True,
                )
        return model

    def prepare_registry(self) -> None:
        if "model_registry" in self.extensions and local_registry(self.values):
            prepare_storage(self.root, Path(self.values["MR_HOME"]))

    def ensure_registry_available(self, model: dict[str, Any]) -> None:
        manager = model["services"].get("model-registry")
        if not manager:
            return
        data = manager["environment"]["MR_HOST_DATA_DIR"]
        identifiers = set(
            self.run(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    "volume=" + data,
                ]
            ).split()
        )
        identifiers.update(
            self.run(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    "label=com.docker.compose.service=model-registry",
                    "--filter",
                    "network=" + self.values["CVAT_NETWORK_NAME"],
                ]
            ).split()
        )
        items = (
            json.loads(self.run(["docker", "inspect", *sorted(identifiers)]))
            if identifiers
            else []
        )
        for item in items:
            labels = item["Config"].get("Labels") or {}
            if (
                labels.get("com.docker.compose.project") != self.project
                or labels.get("com.docker.compose.service") != "model-registry"
            ):
                raise OperationError(
                    "The model data directory or CVAT registry address is in use. Stop the old registry before starting this deployment."
                )
        self.registry_workers(model)

    def registry_workers(self, model: dict[str, Any]) -> list[dict[str, Any]]:
        manager = model["services"].get("model-registry")
        if not manager:
            return []
        config = manager["environment"]
        identifiers = self.run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=org.cvat-model-registry.instance=" + config["MR_INSTANCE"],
            ]
        ).split()
        items = (
            json.loads(self.run(["docker", "inspect", *identifiers]))
            if identifiers
            else []
        )
        data = Path(config["MR_HOST_DATA_DIR"])
        for item in items:
            labels = item["Config"].get("Labels") or {}
            mounts = {
                mount["Destination"]: Path(mount.get("Source", ""))
                for mount in item.get("Mounts", [])
            }
            if (
                not labels.get("org.cvat-model-registry.model")
                or not labels.get("org.cvat-model-registry.revision")
                or any(
                    name not in mounts or not mounts[name].is_relative_to(data)
                    for name in ("/model", "/run/model")
                )
            ):
                raise OperationError(
                    "A foreign worker uses this MR_INSTANCE. Review the registry instance and data path before stopping it."
                )
        return items

    def down(self) -> None:
        saved = read_json(self.state / "active.json")
        if saved:
            self.assert_identity(saved)
            model = saved["model"]
        else:
            model = self.configuration()
            self.freeze(model)
        self.functions()  # Reject foreign functions before stopping any service.
        self.registry_workers(model)
        # Stop ingress and the reconciler BEFORE manually stopping function containers.
        for name in ("traefik", "cvat_ui", "model-gateway", "model-registry", "nuclio"):
            if name in model["services"]:
                self.run(
                    self.compose("stop", "-t", "60", name, frozen=True), stream=True
                )
        # Model workers are disposable containers outside Compose. Stop their
        # creator first, then include them in the same maintenance operation.
        for item in self.registry_workers(model):
            self.run(["docker", "update", "--restart=no", item["Id"]])
            if item["State"]["Running"]:
                self.run(["docker", "stop", "-t", "120", item["Id"]], stream=True)
            # They only mount stored model files; removing the disposable
            # container also allows a stopped deployment's storage to be moved.
            self.run(["docker", "rm", item["Id"]])
        suspended = read_json(
            self.state / "suspended.json",
            {"identity": self.identity(), "containers": {}},
        )
        self.assert_identity(suspended)
        for item in self.functions():
            if item["State"]["Running"]:
                suspended["containers"].setdefault(
                    item["Id"],
                    {
                        "function": item["Config"]["Labels"]["nuclio.io/function-name"],
                        "restart": item["HostConfig"]["RestartPolicy"],
                    },
                )
        atomic_json(
            self.state / "suspended.json", suspended
        )  # Before changing any restart policy.
        for identifier in suspended["containers"]:
            self.run(["docker", "update", "--restart=no", identifier])
            self.run(["docker", "stop", "-t", "120", identifier], stream=True)
        self.run(self.compose("down", "--timeout", "120", frozen=True), stream=True)
        if any(item["State"]["Running"] for item in self.functions()):
            raise OperationError(
                "A Nuclio function is still running; maintenance stop is incomplete"
            )
        if any(item["State"]["Running"] for item in self.registry_workers(model)):
            raise OperationError(
                "A model worker is still running; maintenance stop is incomplete"
            )
        print(
            "CVAT and selected extension services/workers stopped. Model data, volumes and shared network retained."
        )

    def restore_functions(self) -> None:
        suspended = read_json(self.state / "suspended.json")
        if not suspended:
            return
        self.assert_identity(suspended)
        available = {item["Id"] for item in self.functions()}
        for identifier, record in list(suspended["containers"].items()):
            owner = FUNCTION_OWNERS.get(record["function"])
            if owner and owner not in self.extensions:
                continue
            if identifier not in available:
                raise OperationError(
                    f"Stopped function container missing: {record['function']}. Restore/redeploy its definition before proceeding."
                )
            policy = record["restart"]
            restart = policy.get("Name") or "no"
            if restart == "on-failure" and policy.get("MaximumRetryCount"):
                restart += f":{policy['MaximumRetryCount']}"
            self.run(["docker", "update", "--restart=" + restart, identifier])
            self.run(["docker", "start", identifier], stream=True)
            del suspended["containers"][identifier]
            atomic_json(self.state / "suspended.json", suspended)
        if not suspended["containers"]:
            (self.state / "suspended.json").unlink(missing_ok=True)

    def nuctl_check(self, model: dict[str, Any]) -> str:
        image = model["services"]["nuclio"]["image"]
        match = re.search(r":(\d+\.\d+\.\d+)(?:-|$)", image)
        output = self.run(["nuctl", "version"])
        if not match or not re.search(
            r"(?<![\d.])" + re.escape(match[1]) + r"(?![\d.])", output
        ):
            raise OperationError(
                f"nuctl must match the Nuclio dashboard version: {image}"
            )
        return match[1]

    def function_images(
        self, version: str, feature: str | None = None
    ) -> dict[str, str]:
        selected = (feature,) if feature else self.extensions
        images = {}
        for extension in selected:
            if extension not in FUNCTION_DEFINITIONS:
                continue
            if extension == "sam31":
                for name in sam31_deploy.FUNCTIONS:
                    images[name] = sam31_deploy.function_image(
                        version, self.root,
                        checkpoint_sha256=self.values["SAM31_CHECKPOINT_SHA256"], function=name,
                    )
                continue
            source = self.root / FUNCTION_SOURCES[extension]
            if not source.is_dir():
                raise OperationError(f"Missing function source: {source}")
            if not (source / "main.py").is_file():
                raise OperationError(f"Missing function handler: {source / 'main.py'}")
            digest = hashlib.sha256(version.encode())
            digest.update(Path(__file__).read_bytes())
            for file in sorted(source.rglob("*")):
                if file.is_file() and file.suffix in (".py", ".yaml", ".txt", ".json"):
                    digest.update(
                        file.relative_to(source).as_posix().encode()
                        + b"\0"
                        + file.read_bytes()
                    )
            # Network, namespace, credentials and device selection are runtime settings.
            fingerprint = digest.hexdigest()[:20]
            for name, definition in FUNCTION_DEFINITIONS[extension].items():
                if not (source / definition).is_file():
                    raise OperationError(
                        f"Missing function definition: {source / definition}"
                    )
                images[name] = f"cvat.{name.removeprefix('pth-')}:{fingerprint}-gpu"
        return images

    def sam2_images(self, version: str) -> dict[str, str]:
        return self.function_images(version, "sam2")

    def ultrasam_images(self, version: str) -> dict[str, str]:
        return self.function_images(version, "ultrasam")

    def deploy(
        self,
        model: dict[str, Any],
        mode: str = "all",
        force: bool = False,
        *,
        no_build: bool = False,
        feature: str = "sam2",
    ) -> None:
        if feature not in FUNCTION_DEFINITIONS or feature not in self.extensions:
            raise OperationError(
                f"Add {feature} to CVAT_EXTENSIONS before deploying it"
            )
        if mode not in ("all", "image", "tracker"):
            raise OperationError("Function mode must be all, image or tracker")
        if feature == "ultrasam" and mode == "tracker":
            raise OperationError(
                "UltraSAM supports image prompts only; use deploy-ultrasam or deploy-ultrasam image"
            )
        if feature == "sam31":
            sam31_deploy.deploy(self, model, force=force, no_build=no_build, mode=mode)
            return
        images = self.function_images(self.nuctl_check(model), feature)
        source = self.root / FUNCTION_SOURCES[feature]
        namespace = self.values["NUCLIO_NAMESPACE"]
        common = ["--platform", "local", "--namespace", namespace]
        output = self.run(["nuctl", "get", "projects", *common, "-o", "json"]).strip()
        # nuctl 1.16.3 prints this exact sentence, even with -o json, for an empty store.
        projects = [] if output == "No projects found" else json.loads(output)

        def contains_project(value: Any) -> bool:
            if isinstance(value, dict):
                # ProjectConfig uses meta; function resources use metadata.
                meta = value.get("meta", value.get("metadata", {}))
                return (isinstance(meta, dict) and meta.get("name") == "cvat") or any(
                    contains_project(v) for v in value.values()
                )
            return isinstance(value, list) and any(contains_project(v) for v in value)

        if not contains_project(projects):
            self.run(["nuctl", "create", "project", "cvat", *common], stream=True)
        existing = {
            i["Config"]["Labels"]["nuclio.io/function-name"]: i
            for i in self.functions()
        }
        selected = list(FUNCTION_DEFINITIONS[feature])
        if mode != "all":
            selected = [
                name
                for name in selected
                if name.endswith("interactor" if mode == "image" else "tracker")
            ]
        self.guard_function_ownership(selected)
        for name in selected:
            kind = "interactor" if name.endswith("interactor") else "tracker"
            image = images[name]
            current = existing.get(name)
            image_matches = False
            if (
                current
                and current["Config"]["Image"] == image
                and current["State"]["Running"]
            ):
                expected = self.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        image,
                        "--format",
                        "{{.Id}}",
                    ]
                ).strip()
                actual = self.run(
                    [
                        "docker",
                        "inspect",
                        current["Id"],
                        "--format",
                        "{{.Image}}",
                    ]
                ).strip()
                image_matches = expected == actual
            config_matches = kind != "tracker" or (
                current
                and "SAM2_REDIS_PASSWORD=" + self.values["SAM2_REDIS_PASSWORD"]
                in current["Config"].get("Env", [])
            )
            if feature == "ultrasam":
                config_matches = bool(
                    current
                    and (
                        "CUDA_VISIBLE_DEVICES=" + self.values["ULTRASAM_GPU_DEVICE"]
                        in current["Config"].get("Env", [])
                    )
                )
            if not force and image_matches and config_matches:
                print(f"{name}: source/config unchanged, reuse the running function")
                continue
            rendered = render_function(
                (source / FUNCTION_DEFINITIONS[feature][name]).read_text(),
                namespace,
                image,
                self.values["SAM2_REDIS_PASSWORD"] if kind == "tracker" else None,
            )
            if feature == "ultrasam":
                rendered = render_function_environment(
                    rendered, "CUDA_VISIBLE_DEVICES", self.values["ULTRASAM_GPU_DEVICE"]
                )
            fd, temp = tempfile.mkstemp(
                dir=self.state, prefix=feature + "-", suffix=".yaml"
            )
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(rendered)
                self.run(
                    [
                        "nuctl",
                        "deploy",
                        "--project-name",
                        "cvat",
                        "--path",
                        str(source),
                        "--file",
                        temp,
                        *(["--run-image", image, "--no-pull"] if no_build else []),
                        *common,
                        "--platform-config",
                        json.dumps(
                            {
                                "attributes": {
                                    "network": self.values["CVAT_NETWORK_NAME"]
                                }
                            }
                        ),
                    ],
                    stream=True,
                )
            finally:
                os.unlink(temp)
        self.verify_functions(selected, environments=self.function_environment())

    def guard_function_ownership(self, names: list[str]) -> None:
        # nuctl local names are daemon-wide within a namespace, not network-scoped.
        for name in names:
            ids = self.run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=nuclio.io/function-name={name}",
                    "--filter",
                    f"label=nuclio.io/namespace={self.values['NUCLIO_NAMESPACE']}",
                ]
            ).split()
            items = json.loads(self.run(["docker", "inspect", *ids])) if ids else []
            for item in items:
                networks = item.get("NetworkSettings", {}).get("Networks", {})
                labels = item["Config"].get("Labels") or {}
                if (
                    self.values["CVAT_NETWORK_NAME"] not in networks
                    or labels.get("nuclio.io/project-name") != "cvat"
                ):
                    raise OperationError(
                        f"{name}: another deployment owns this function name on this Docker daemon"
                    )

    def verify_functions(
        self,
        selected: list[str],
        *,
        images: dict[str, str] | None = None,
        environments: dict[str, dict[str, str]] | None = None,
        sam31_checkpoint: str | None = None,
    ) -> None:
        functions = {
            i["Config"]["Labels"]["nuclio.io/function-name"]: i
            for i in self.functions()
        }
        for name in selected:
            item = functions.get(name)
            if not item or not item["State"]["Running"]:
                raise OperationError(
                    f"{name}: the function is missing or not running on the configured network"
                )
            if any((item["HostConfig"].get("PortBindings") or {}).values()):
                raise OperationError(
                    f"{name}: unexpected host port binding. Do not reopen external access."
                )
            if name in sam31_deploy.FUNCTIONS:
                if not sam31_deploy.checkpoint_matches(item):
                    raise OperationError("SAM3.1 embedded checkpoint is shadowed by a runtime mount")
            for key, value in (environments or {}).get(name, {}).items():
                if f"{key}={value}" not in item["Config"].get("Env", []):
                    raise OperationError(
                        f"{name}: {key} differs from the recorded function setting"
                    )
            if images and any(
                image.startswith(("cvat.sam2-", "cvat.ultrasam-", "cvat.sam31-")) for image in images
            ):
                expected = images.get(item["Config"]["Image"])
                actual = self.run(
                    [
                        "docker",
                        "inspect",
                        item["Id"],
                        "--format",
                        "{{.Image}}",
                    ]
                ).strip()
                if not expected or actual != expected:
                    raise OperationError(
                        f"{name}: image differs from the recorded startup image"
                    )

    def up(self, *, no_build: bool = False) -> None:
        self.prepare_registry()
        model = self.configuration()
        if "sam31" in self.extensions:
            sam31_deploy.validate_settings(self.values, self.root, verify_file=not no_build)
        # Fail before changing containers if managed GPU functions cannot be deployed.
        function_features = [
            name for name in self.extensions if name in FUNCTION_DEFINITIONS
        ]
        function_version = self.nuctl_check(model) if function_features else None
        expected_function_images = (
            self.function_images(function_version) if function_version else {}
        )
        active = read_json(self.state / "active.json")
        if active:
            self.assert_identity(active)
        self.ensure_registry_available(model)
        running = self.run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ]
        ).split()
        if running and (not active or not self.matches_configuration(active, model)):
            raise OperationError(
                "An existing/different configuration is running. Run down before changing deployment."
            )
        if not no_build:
            self.build(model)
        # In --no-build mode, a missing transferred image fails before any startup.
        images = self.local_image_ids(model)
        if no_build and function_version:
            for image in expected_function_images.values():
                images[image] = self.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        image,
                        "--format",
                        "{{.Id}}",
                    ]
                ).strip()
        self.ensure_network()
        redis_features = [feature for feature in ("sam2", "sam31") if feature in self.extensions]
        for feature in redis_features:
            self.ensure_volume(feature)
        self.freeze(
            model, images
        )  # A partial startup can subsequently be stopped with this exact configuration.
        if redis_features:
            self.run(
                self.compose(
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    *(feature + "_redis" for feature in redis_features),
                ),
                stream=True,
            )
        self.restore_functions()
        self.run(self.compose("up", "-d", "--no-build"), stream=True)
        for feature in function_features:
            self.deploy(model, no_build=no_build, feature=feature)
            for image in self.function_images(function_version, feature).values():
                images[image] = self.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        image,
                        "--format",
                        "{{.Id}}",
                    ]
                ).strip()
            self.freeze(model, images)
        print(
            "Containers started. Run cvatctl check and the documented browser acceptance tests before reopening service."
        )

    def check(self) -> None:
        saved = read_json(self.state / "active.json")
        if not saved:
            raise OperationError("No recorded startup configuration. Run up first.")
        self.assert_identity(saved)
        services = saved["model"]["services"]
        selected = saved.get("extensions", ("itgformat", "sam2"))
        recorded = saved.get("images", {})
        register = (
            "from cvat.apps.dataset_manager.formats.registry import IMPORT_FORMATS, EXPORT_FORMATS; "
            "assert IMPORT_FORMATS['ITGformat 1.0'].ENABLED; "
            "assert EXPORT_FORMATS['ITGformat 1.0'].ENABLED; print('ITGformat registration OK')"
        )
        for name, service in services.items():
            backend = name == "cvat_server" or name.startswith("cvat_worker_")
            if not backend and name not in (
                "cvat_ui",
                "model-gateway",
                "model-registry",
            ):
                continue
            image = service["image"]
            image_id = self.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    image,
                    "--format",
                    "{{.Id}}",
                ]
            ).strip()
            if recorded.get(image, image_id) != image_id:
                raise OperationError(f"{name}: image tag changed since startup")
            ids = self.run(self.compose("ps", "-q", name, frozen=True)).split()
            if not ids:
                raise OperationError(f"{name} is not running")
            for identifier in ids:
                actual = self.run(
                    [
                        "docker",
                        "inspect",
                        identifier,
                        "--format",
                        "{{.Image}}",
                    ]
                ).strip()
                if actual != image_id:
                    raise OperationError(
                        f"{name}: running image ID differs from the selected image"
                    )
            if backend and "itgformat" in selected:
                print(
                    self.run(
                        self.compose(
                            "exec",
                            "-T",
                            name,
                            "python3",
                            "-m",
                            "django",
                            "shell",
                            "-c",
                            register,
                            frozen=True,
                        )
                    ).strip()
                )
        for feature in selected:
            if feature in FUNCTION_DEFINITIONS:
                self.verify_functions(
                    list(FUNCTION_DEFINITIONS[feature]),
                    images=recorded,
                    environments=saved.get("function_environment", {}),
                    sam31_checkpoint=saved.get("sam31_checkpoint"),
                )
        if "itgformat" in selected:
            script = (self.root / "tests/itgformat/check_in_cvat.py").read_text()
            for name in ("cvat_server", "cvat_worker_import", "cvat_worker_export"):
                print(
                    self.run(
                        self.compose(
                            "exec",
                            "-T",
                            name,
                            "python3",
                            "-m",
                            "django",
                            "shell",
                            "-c",
                            'exec(__import__("sys").stdin.read())',
                            frozen=True,
                        ),
                        input_text=script,
                    )
                )
        if "model_registry" in selected:
            if manager := services.get("model-registry"):
                worker_image = manager["environment"]["MR_WORKER_IMAGE"]
                worker_id = self.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        worker_image,
                        "--format",
                        "{{.Id}}",
                    ]
                ).strip()
                if recorded.get(worker_image, worker_id) != worker_id:
                    raise OperationError("ONNX worker image tag changed since startup")
            health = (
                "import urllib.request; "
                "urllib.request.urlopen('http://localhost:8070/health', timeout=5); "
                "response=urllib.request.urlopen('http://localhost:8070/api/functions', timeout=20); "
                "assert not response.headers.get('X-Model-Catalog-Unavailable'), 'An AI catalog is unavailable'; "
                "print('Model gateway and registry authenticated catalog OK')"
            )
            print(
                self.run(
                    self.compose(
                        "exec",
                        "-T",
                        "model-gateway",
                        "python",
                        "-c",
                        health,
                        frozen=True,
                    )
                ).strip()
            )
        if set(selected) & SERVERLESS_EXTENSIONS:
            query = (
                "from cvat.apps.lambda_manager.views import LambdaGateway; "
                "functions={f.id for f in LambdaGateway().list()}; "
            )
            expected_functions = {
                name
                for feature in selected
                for name in FUNCTION_DEFINITIONS.get(feature, {})
            }
            if expected_functions:
                query += f"assert set({sorted(expected_functions)!r}) <= functions; "
            query += "print('AI function listing OK:', len(functions))"
            print(
                self.run(
                    self.compose(
                        "exec",
                        "-T",
                        "cvat_server",
                        "python3",
                        "-m",
                        "django",
                        "shell",
                        "-c",
                        query,
                        frozen=True,
                    )
                ).strip()
            )
        print(
            "Image and selected extension checks passed. GPU inference and browser behavior require the documented acceptance tests."
        )

    def purge_deleted(self, model_id: str | None, *, confirm: bool = False) -> None:
        if not confirm or not model_id or not re.fullmatch(r"[0-9a-f]{20}", model_id):
            raise OperationError(
                "Purge requires --model-id (20 lowercase hexadecimal characters) and --confirm"
            )
        saved = read_json(self.state / "active.json")
        if saved:
            self.assert_identity(saved)
            model = saved["model"]
        else:
            model = self.configuration()
        manager = model["services"].get("model-registry")
        if not manager:
            raise OperationError(
                "For a remote registry, run registryctl.py purge-deleted on its inference host"
            )
        data = Path(manager["environment"]["MR_HOST_DATA_DIR"])
        running = self.run(
            ["docker", "ps", "-q", "--filter", "volume=" + str(data)]
        ).split()
        if running or any(
            item["State"]["Running"] for item in self.registry_workers(model)
        ):
            raise OperationError("Run cvatctl down before purging stored model files")
        purge_registry_model(data, model_id)
        print(
            "Stored model files removed. The deletion audit and ownership record remain."
        )


def default_state(env_file: Path) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return (
        base
        / "cvat-extensions"
        / hashlib.sha256(str(env_file).encode()).hexdigest()[:16]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="For up: use existing CVAT and extension images, including ONNX workers",
    )
    parser.add_argument(
        "--model-id", help="For purge-deleted: ID of the already deleted model"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="For purge-deleted: permanently remove its stored model files",
    )
    parser.add_argument(
        "command",
        choices=(
            "init",
            "config",
            "doctor",
            "build",
            "pull",
            "up",
            "down",
            "status",
            "check",
            "deploy-sam2",
            "deploy-ultrasam",
            "deploy-sam31",
            "purge-deleted",
        ),
    )
    parser.add_argument(
        "mode", nargs="?", choices=("image", "tracker", "all"), default="all"
    )
    args = parser.parse_args()
    if args.command == "deploy-ultrasam" and args.mode == "tracker":
        parser.error("UltraSAM supports image prompts only; tracker is unavailable")
    if args.no_build and args.command != "up":
        parser.error("--no-build is only supported with up")
    if (args.model_id or args.confirm) and args.command != "purge-deleted":
        parser.error("--model-id and --confirm are only supported with purge-deleted")
    env_file = args.env_file.expanduser().resolve()
    state = (args.state_dir or default_state(env_file)).expanduser().resolve()
    if state == ROOT or ROOT in state.parents:
        raise OperationError(
            "Runtime state must be outside the Git checkout and Docker build context"
        )
    if args.command == "init":
        if env_file.exists():
            raise OperationError(
                f"{env_file} already exists. Merge config.example.env manually; do not replace existing settings."
            )
        if ROOT in env_file.parents and env_file != ROOT / ".env":
            raise OperationError(
                "Use the ignored root .env, or store the configuration outside the checkout"
            )
        env_file.parent.mkdir(parents=True, exist_ok=True)
        text = (
            (ROOT / "components/extensions/config.example.env")
            .read_text()
            .replace("GENERATE_ON_INIT", secrets.token_hex(32))
            .replace("GENERATE_SAM31_ON_INIT", secrets.token_hex(32))
        )
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        initial = settings(env_file)
        if "model_registry" in enabled_extensions(initial) and local_registry(initial):
            prepare_storage(ROOT, Path(initial["MR_HOME"]))
        print(
            f"Created {env_file}. Review CVAT_HOST and existing volume/network names before starting."
        )
        return
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_mode & 0o077:
        raise OperationError(f"Runtime state must be private: chmod 700 {state}")
    with (state / "operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OperationError(
                "Another operation is using this deployment state"
            ) from exc
        manager = Manager(ROOT, env_file, state)
        if args.command in ("config", "doctor"):
            model = manager.configuration()
            summary = {
                "extensions": manager.extensions,
                "project": manager.project,
                "network": manager.values["CVAT_NETWORK_NAME"],
                "backend_image": model["services"]["cvat_server"]["image"],
                "ui_image": model["services"]["cvat_ui"]["image"],
                "backend_services": [
                    s
                    for s in model["services"]
                    if s == "cvat_server" or s.startswith("cvat_worker_")
                ],
                "state_directory": str(state),
            }
            if "sam2" in manager.extensions:
                summary["sam2_redis_volume"] = manager.values["SAM2_REDIS_VOLUME"]
            if "sam31" in manager.extensions:
                summary["sam31_redis_volume"] = manager.values["SAM31_REDIS_VOLUME"]
                summary["sam31_checkpoint_source"] = manager.values["SAM31_CHECKPOINT_HOST"]
                summary["sam31_checkpoint"] = sam31_deploy.CHECKPOINT
                summary["sam31_checkpoint_sha256"] = manager.values["SAM31_CHECKPOINT_SHA256"]
                summary["sam31_gpu_device"] = manager.values["SAM31_GPU_DEVICE"]
            if "ultrasam" in manager.extensions:
                summary["ultrasam_gpu_device"] = manager.values["ULTRASAM_GPU_DEVICE"]
            if any(feature in FUNCTION_DEFINITIONS for feature in manager.extensions):
                dashboard_image = model["services"]["nuclio"]["image"]
                version = re.search(r":(\d+\.\d+\.\d+)(?:-|$)", dashboard_image)
                if not version:
                    raise OperationError(
                        "Cannot determine the Nuclio version from its image tag"
                    )
                for feature in manager.extensions:
                    if feature in FUNCTION_DEFINITIONS:
                        summary[feature + "_images"] = manager.function_images(
                            version[1], feature
                        )
            if "model_registry" in manager.extensions:
                summary["model_gateway_image"] = model["services"]["model-gateway"][
                    "image"
                ]
                if registry := model["services"].get("model-registry"):
                    summary["model_registry_home"] = manager.values["MR_HOME"]
                    summary["model_registry_image"] = registry["image"]
                    summary["model_worker_image"] = registry["environment"][
                        "MR_WORKER_IMAGE"
                    ]
            if args.command == "doctor":
                if any(
                    feature in FUNCTION_DEFINITIONS for feature in manager.extensions
                ):
                    summary["nuclio_version"] = manager.nuctl_check(model)
                summary["docker_daemon"] = manager.identity()["daemon"]
            print(json.dumps(summary, indent=2))
        elif args.command == "build":
            manager.build()
        elif args.command == "pull":
            model = manager.configuration()
            services = [
                name
                for name, value in model["services"].items()
                if value.get("pull_policy") != "never"
            ]
            manager.run(manager.compose("pull", *services), stream=True)
        elif args.command == "up":
            manager.up(no_build=args.no_build)
        elif args.command == "down":
            manager.down()
        elif args.command == "check":
            manager.check()
        elif args.command == "purge-deleted":
            manager.purge_deleted(args.model_id, confirm=args.confirm)
        elif args.command == "status":
            if saved := read_json(state / "active.json"):
                manager.assert_identity(saved)
                manager.run(manager.compose("ps", "--all", frozen=True), stream=True)
            else:
                manager.configuration()
                manager.run(manager.compose("ps", "--all"), stream=True)
            for item in manager.functions():
                print(item["Name"], item["State"]["Status"])
        elif args.command in ("deploy-sam2", "deploy-ultrasam", "deploy-sam31"):
            feature = args.command.removeprefix("deploy-")
            if feature not in manager.extensions:
                raise OperationError(
                    f"Add {feature} to CVAT_EXTENSIONS before deploying it"
                )
            model = manager.configuration()
            saved = read_json(state / "active.json")
            if not saved or not manager.matches_configuration(saved, model):
                raise OperationError(
                    "Run down/up for the current source and configuration before manual redeployment"
                )
            manager.assert_identity(saved)
            version = manager.nuctl_check(model)
            if feature == "sam31":
                sam31_deploy.validate_settings(manager.values, manager.root)
            manager.ensure_network()
            if feature in ("sam2", "sam31"):
                manager.ensure_volume(feature)
            manager.run(
                manager.compose(
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "180",
                    *([feature + "_redis"] if feature in ("sam2", "sam31") else []),
                    "nuclio",
                ),
                stream=True,
            )
            manager.deploy(model, args.mode, force=True, feature=feature)
            images = manager.local_image_ids(model)
            for image in manager.function_images(version).values():
                images[image] = manager.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        image,
                        "--format",
                        "{{.Id}}",
                    ]
                ).strip()
            manager.freeze(model, images)


if __name__ == "__main__":
    try:
        main()
    except (OperationError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
