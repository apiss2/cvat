#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Host-side administrative operations. Requires only Python's standard library."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from storage import atomic, default_home, prepare_storage

HERE = Path(__file__).resolve().parent


def http_url(value: str, name: str):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", parsed.hostname)
    ):
        raise ValueError(
            f"{name} must be an absolute HTTP(S) URL without credentials, query or fragment"
        )
    parsed.port  # Validate malformed and out-of-range port numbers.
    return parsed


def public_address(value: str) -> tuple[str, str]:
    parsed = http_url(value, "MR_PUBLIC_URL")
    if parsed.path not in ("/model-registry", "/model-registry/"):
        raise ValueError(
            "MR_PUBLIC_URL must use the CVAT origin and /model-registry/ path"
        )
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    # Host routing excludes the port, while redirects and CSRF use the full URL.
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme, netloc, "/model-registry/", "", "")), host


def connection_values(values: dict[str, str]) -> None:
    if values.get("MR_AUTH_MODE", "cvat") != "cvat":
        raise ValueError(
            "MR_AUTH_MODE must be cvat; personal-token authentication has been removed"
        )
    cookie_name = values.get("MR_CVAT_SESSION_COOKIE", "sessionid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cookie_name):
        raise ValueError(
            "MR_CVAT_SESSION_COOKIE must be 1-128 letters, digits, underscores or hyphens"
        )
    values["MR_CVAT_SESSION_COOKIE"] = cookie_name
    public_url, public_host = public_address(values.get("MR_PUBLIC_URL", ""))
    values["MR_PUBLIC_URL"], values["MR_PUBLIC_HOST"] = public_url, public_host
    parsed = http_url(
        values.get("MR_CVAT_URL", "http://cvat_server:8080"), "MR_CVAT_URL"
    )
    if parsed.path not in ("", "/"):
        raise ValueError("MR_CVAT_URL must point to the CVAT server root")
    if not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:,[A-Za-z0-9_-]+)*",
        values.get("MR_TRAEFIK_ENTRYPOINTS", "web"),
    ):
        raise ValueError(
            "MR_TRAEFIK_ENTRYPOINTS must be comma-separated entrypoint names"
        )
    if values.get("MR_TRAEFIK_TLS", "false") not in ("true", "false"):
        raise ValueError("MR_TRAEFIK_TLS must be true or false")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", values.get("MR_CVAT_NETWORK", "cvat_cvat")
    ):
        raise ValueError(
            "MR_CVAT_NETWORK must be the existing CVAT Docker network name"
        )


def read_env(file: Path) -> dict[str, str]:
    if file.stat().st_mode & 0o077:
        raise ValueError(
            "registry.env contains configuration paths; run chmod 600 on it"
        )
    result = {}
    for original in file.read_text().splitlines():
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, raw = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise ValueError("Expected unique literal KEY=VALUE assignments")
        fields = shlex.split(raw, comments=True)
        if len(fields) > 1:
            raise ValueError("Quote values containing whitespace")
        value = fields[0] if fields else ""
        if "$" in value or "`" in value:
            raise ValueError("Shell expansion is not supported")
        result[key] = value
    return result


def run(*args, env=None):
    subprocess.run(list(args), env=env, check=True)


def purge_deleted(data_dir: Path, model_id: str) -> None:
    """Release a deleted model's files, keeping its ownership and audit record."""
    import fcntl

    if not re.fullmatch(r"[0-9a-f]{20}", model_id):
        raise ValueError("Invalid model ID")
    database = data_dir / "registry.sqlite3"
    if not database.is_file():
        raise ValueError(f"Registry database does not exist: {database}")
    with (data_dir / "manager.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(database) as db:
            row = db.execute("SELECT deleted FROM models WHERE id=?", (model_id,)).fetchone()
            if not row or not row[0]:
                raise ValueError("Only deleted models may be purged")
            directory = data_dir / "packages" / model_id
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=False)
            db.execute("DELETE FROM revisions WHERE model_id=?", (model_id,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "init",
            "build",
            "config",
            "up",
            "down",
            "status",
            "logs",
            "purge-deleted",
        ],
    )
    parser.add_argument("--home", type=Path, help="Model storage (default: repository/cvat-model-registry)")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--instance", default="team-models")
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Do not attach to a local CVAT network",
    )
    parser.add_argument(
        "--public-url",
        help="Required for init: the CVAT browser URL followed by /model-registry/",
    )
    parser.add_argument(
        "--cvat-url",
        default="http://cvat_server:8080",
        help="CVAT server root reachable from the manager",
    )
    # Keep existing init commands that explicitly selected CVAT authentication valid.
    parser.add_argument("--auth-mode", choices=["cvat"], default="cvat", help=argparse.SUPPRESS)
    parser.add_argument(
        "--cvat-session-cookie",
        default="sessionid",
        help="CVAT session cookie name, if changed from sessionid",
    )
    parser.add_argument(
        "--cvat-network",
        default="cvat_cvat",
        help="Existing CVAT Docker network (local deployment)",
    )
    parser.add_argument("--gpu-device", default="")
    parser.add_argument(
        "--target", choices=["all", "gateway", "manager", "worker"], default="all"
    )
    parser.add_argument("--model-id")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument(
        "--force-recreate",
        action="store_true",
        help="With up: recreate the manager container using the currently built image",
    )
    args = parser.parse_args()
    if args.force_recreate and args.command != "up":
        parser.error("--force-recreate is only supported with up")
    repo = HERE.parents[1] if HERE.parent.name == "components" else HERE
    requested_home = (args.home or default_home(repo)).expanduser()
    home = requested_home.resolve()
    if args.command == "init":
        if not args.public_url:
            parser.error(
                "init requires --public-url, for example https://cvat.example.internal/model-registry/"
            )
        connection = {
            "MR_AUTH_MODE": args.auth_mode,
            "MR_CVAT_URL": args.cvat_url.rstrip("/"),
            "MR_PUBLIC_URL": args.public_url,
            "MR_CVAT_SESSION_COOKIE": args.cvat_session_cookie,
            "MR_CVAT_NETWORK": args.cvat_network,
            "MR_TRAEFIK_ENTRYPOINTS": "web",
            "MR_TRAEFIK_TLS": "false",
        }
        connection_values(connection)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", args.instance):
            parser.error("Invalid instance name")
        if args.gpu_device and not re.fullmatch(
            r"[0-9]+|GPU-[A-Za-z0-9-]+", args.gpu_device
        ):
            parser.error("GPU device must be one numeric ID or GPU UUID")
        if (home / "registry.env").exists():
            parser.error(
                "Already initialized; existing secrets will not be overwritten"
            )
        home, config, data = prepare_storage(repo, requested_home)
        values = {
            "MR_INSTANCE": args.instance,
            "MR_HOST_DATA_DIR": str(data),
            "MR_CONFIG_DIR": str(config),
            "MR_MANAGER_IMAGE": "cvat-local/model-registry:1.0.0",
            "MR_GATEWAY_IMAGE": "cvat-local/model-gateway:1.0.0",
            "MR_WORKER_IMAGE": "cvat-local/onnx-worker:1.0.0-"
            + ("gpu" if args.gpu_device else "cpu"),
            "MR_GPU_DEVICE": args.gpu_device,
            "MR_NAMESPACE": "nuclio",
            "MR_ATTACH_CVAT": "0" if args.standalone else "1",
            **connection,
        }
        atomic(
            home / "registry.env",
            "\n".join(f"{k}={shlex.quote(v)}" for k, v in values.items()) + "\n",
        )
        print(
            f"Configuration: {home / 'registry.env'}\nBrowser URL: {connection['MR_PUBLIC_URL']}"
        )
        print(
            "Open the browser URL while signed in to CVAT. No separate registry login is required. "
            "Registry administrator rights follow CVAT."
        )
        print(
            "\nCVAT configuration values (merge with the existing .env; do not overwrite it):"
        )
        print("CVAT_EXTENSIONS=itgformat,sam2,model_registry")
        print("MR_HOME=" + shlex.quote(str(home)))
        print("MR_INSTANCE=" + args.instance)
        if args.standalone:
            print(
                "Set CVAT_MODEL_REGISTRY_URL to the protected HTTPS registry root reachable from the CVAT gateway."
            )
        else:
            print("CVAT_MODEL_REGISTRY_URL=http://model-registry:8091")
        print("CVAT_MODEL_GATEWAY_SECRETS_DIR=" + str(config / "secrets"))
        print(
            "For a local deployment, set MR_HOME to this directory and use cvatctl "
            "to build and manage CVAT together with all enabled extensions. "
            "registryctl is retained for standalone deployments and existing installations."
        )
        return
    file = (args.env_file or home / "registry.env").resolve()
    values = read_env(file)
    connection_values(values)
    environment = {
        k: v for k, v in os.environ.items() if not k.startswith(("MR_", "COMPOSE_"))
    }
    environment.update(values)
    project = "mr-" + values["MR_INSTANCE"]
    if not re.fullmatch(r"mr-[a-z0-9][a-z0-9_-]{0,30}", project):
        parser.error("Invalid MR_INSTANCE")
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(HERE),
        "--env-file",
        str(file),
        "-p",
        project,
        "-f",
        str(HERE / "docker-compose.registry.yml"),
    ]
    if values.get("MR_ATTACH_CVAT") == "1":
        compose += ["-f", str(HERE / "docker-compose.local.yml")]
    if args.command == "build":
        for target in ("gateway", "manager", "worker"):
            if args.target not in ("all", target):
                continue
            image = values[f"MR_{target.upper()}_IMAGE"]
            if target == "worker":
                filename = (
                    "Dockerfile.worker.gpu"
                    if values.get("MR_GPU_DEVICE")
                    else "Dockerfile.worker"
                )
                command = [
                    "docker",
                    "build",
                    "-f",
                    str(HERE / filename),
                    "-t",
                    image,
                    str(HERE),
                ]
            else:
                command = [
                    "docker",
                    "build",
                    "-f",
                    str(HERE / "Dockerfile"),
                    "--target",
                    target,
                    "-t",
                    image,
                    str(HERE),
                ]
            run(*command, env=environment)
        return
    if args.command == "up":
        run(
            *compose,
            "up",
            "-d",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "120",
            *(["--force-recreate"] if args.force_recreate else []),
            env=environment,
        )
    elif args.command == "down":
        run(*compose, "down", "--timeout", "120", env=environment)
        # Remove only this registry instance's managed workers, never CVAT/SAM2 containers.
        ids = subprocess.check_output(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "label=org.cvat-model-registry.instance=" + values["MR_INSTANCE"],
            ],
            env=environment,
            text=True,
        ).split()
        if ids:
            run("docker", "rm", "-f", *ids, env=environment)
    elif args.command == "status":
        run(*compose, "ps", env=environment)
    elif args.command == "logs":
        run(*compose, "logs", "--tail", "200", env=environment)
    elif args.command == "config":
        run(*compose, "config", env=environment)
    elif args.command == "purge-deleted":
        if (
            not args.confirm
            or not args.model_id
            or not re.fullmatch(r"[0-9a-f]{20}", args.model_id)
        ):
            parser.error("Purge requires --model-id and --confirm")
        running = subprocess.check_output(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ],
            text=True,
            env=environment,
        ).strip()
        if running:
            parser.error("Stop the registry with registryctl.py down before purging")
        purge_deleted(Path(values["MR_HOST_DATA_DIR"]), args.model_id)
        print(
            "Stored model files removed. The deletion audit and ownership record remain."
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
