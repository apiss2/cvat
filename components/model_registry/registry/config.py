# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def url_origin(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    suffix = f":{port}" if port and port != {"http": 80, "https": 443}.get(parsed.scheme) else ""
    return f"{parsed.scheme}://{host}{suffix}"


@dataclass(frozen=True)
class Settings:
    auth_mode: str = field(default_factory=lambda: os.getenv("MR_AUTH_MODE", "cvat"))
    cvat_url: str = field(default_factory=lambda: os.getenv("MR_CVAT_URL", "http://cvat_server:8080"))
    cvat_ca_file: str = field(default_factory=lambda: os.getenv("MR_CVAT_CA_FILE", ""))
    public_url: str = field(default_factory=lambda: os.getenv("MR_PUBLIC_URL", ""))
    cvat_session_cookie: str = field(default_factory=lambda: os.getenv("MR_CVAT_SESSION_COOKIE", "sessionid"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("MR_DATA_DIR", "/data")))
    host_data_dir: str = field(default_factory=lambda: os.getenv("MR_HOST_DATA_DIR", "/srv/cvat-model-registry"))
    service_token_file: Path = field(default_factory=lambda: Path(os.getenv("MR_SERVICE_TOKEN_FILE", "/run/secrets/service_token")))
    worker_image: str = field(default_factory=lambda: os.getenv("MR_WORKER_IMAGE", "cvat-local/onnx-worker:1.0.0-cpu"))
    docker_socket: str = field(default_factory=lambda: os.getenv("MR_DOCKER_SOCKET", "/var/run/docker.sock"))
    instance: str = field(default_factory=lambda: os.getenv("MR_INSTANCE", "team-models"))
    namespace: str = field(default_factory=lambda: os.getenv("MR_NAMESPACE", "nuclio"))
    load_timeout: int = 90
    infer_timeout: int = 90
    max_loaded: int = field(default_factory=lambda: int(os.getenv("MR_MAX_LOADED", "2")))
    worker_memory: int = field(default_factory=lambda: int(os.getenv("MR_WORKER_MEMORY_MIB", "4096")) * 1024**2)
    worker_cpus: int = field(default_factory=lambda: int(os.getenv("MR_WORKER_CPUS", "2")))
    gpu_device: str = field(default_factory=lambda: os.getenv("MR_GPU_DEVICE", ""))
    max_storage_bytes: int = field(default_factory=lambda: int(os.getenv("MR_STORAGE_GIB", "50")) * 1024**3)
    max_pending: int = 4

    def cvat_tls_verify(self) -> bool | ssl.SSLContext:
        if not self.cvat_ca_file:
            return True
        if not Path(self.cvat_ca_file).is_absolute():
            raise ValueError("MR_CVAT_CA_FILE must be an absolute path inside the manager container")
        try:
            return ssl.create_default_context(cafile=self.cvat_ca_file)
        except OSError as exc:
            raise ValueError("MR_CVAT_CA_FILE must be a readable PEM CA certificate bundle") from exc

    def validate(self) -> None:
        if self.auth_mode != "cvat":
            raise ValueError("MR_AUTH_MODE must be cvat; model registry users authenticate through CVAT")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.cvat_session_cookie):
            raise ValueError("MR_CVAT_SESSION_COOKIE must be a valid cookie name")
        self.cvat_tls_verify()
        for name, value in (("MR_CVAT_URL", self.cvat_url), ("MR_PUBLIC_URL", self.public_url)):
            parsed = urlsplit(value)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or any(c.isspace() for c in value)):
                raise ValueError(f"{name} must be an absolute HTTP(S) URL without credentials, query or fragment")
            # Force validation of malformed/out-of-range ports.
            url_origin(value)
        if urlsplit(self.cvat_url).path not in ("", "/"):
            raise ValueError("MR_CVAT_URL must point to the CVAT server root")
        path = urlsplit(self.public_url).path
        if not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*", path):
            raise ValueError("MR_PUBLIC_URL must have a safe path with a trailing slash, for example /model-registry/")
        if not (1 <= self.max_loaded <= 32 and 1 <= self.worker_cpus <= 128):
            raise ValueError("MR_MAX_LOADED must be 1..32 and MR_WORKER_CPUS must be 1..128")
        if self.worker_memory < 256 * 1024**2 or self.max_storage_bytes < 4 * 1024**3:
            raise ValueError("Worker memory must be >=256 MiB and model storage >=4 GiB")
        if not Path(self.host_data_dir).is_absolute() or ":" in self.host_data_dir:
            raise ValueError("MR_HOST_DATA_DIR must be an absolute Linux path without a colon")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", self.instance):
            raise ValueError("invalid MR_INSTANCE")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", self.namespace):
            raise ValueError("invalid MR_NAMESPACE")
        if self.gpu_device and not re.fullmatch(r"[0-9]+|GPU-[A-Za-z0-9-]+", self.gpu_device):
            raise ValueError("MR_GPU_DEVICE must be one numeric ID or GPU UUID, not 'all'")
