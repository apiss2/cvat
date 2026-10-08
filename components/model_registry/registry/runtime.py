# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Settings


class RuntimeFailure(RuntimeError):
    pass


class Busy(RuntimeFailure):
    pass


def limited_json(client: httpx.Client, method: str, path: str, *, limit: int = 128 * 1024**2, **kwargs):
    with client.stream(method, path, **kwargs) as response:
        chunks = bytearray()
        for chunk in response.iter_bytes():
            if len(chunks) + len(chunk) > limit:
                raise RuntimeFailure("upstream response exceeds byte limit")
            chunks.extend(chunk)
        if response.status_code >= 400:
            raise RuntimeFailure(f"HTTP {response.status_code}: {chunks.decode('utf-8', 'replace')[:16000]}")
        try:
            return json.loads(chunks)
        except ValueError as exc:
            raise RuntimeFailure("upstream did not return valid JSON") from exc


class DockerEngine:
    def __init__(self, socket: str):
        self.client = httpx.Client(transport=httpx.HTTPTransport(uds=socket), base_url="http://docker", timeout=30, trust_env=False)
        self.prefix: str | None = None

    def request(self, method: str, path: str, **kwargs):
        if self.prefix is None:
            version = self.client.get("/version")
            version.raise_for_status()
            api = version.json()["ApiVersion"]
            if tuple(map(int, api.split("."))) < (1, 41):
                raise RuntimeFailure("Docker Engine API >= 1.41 is required")
            self.prefix = "/v" + api
        response = self.client.request(method, self.prefix + path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeFailure(f"Docker {method} {path}: {response.status_code} {response.text[:4000]}")
        return response.json() if response.content else None

    def logs(self, container: str) -> str:
        try:
            if self.prefix is None:
                return ""
            with self.client.stream("GET", self.prefix + f"/containers/{container}/logs", params={"stdout": "1", "stderr": "1", "tail": "100"}) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        break
            # Docker multiplexed stdout/stderr framing: 1 byte stream + 3 zero + uint32 size.
            output = bytearray()
            pos = 0
            while pos + 8 <= len(data) and data[pos:pos + 4] in (b"\x01\0\0\0", b"\x02\0\0\0"):
                size = int.from_bytes(data[pos + 4:pos + 8], "big")
                output.extend(data[pos + 8:pos + 8 + size])
                pos += 8 + size
            return (output if pos else data).decode("utf-8", "replace")[-16000:]
        except Exception:
            return "Docker worker logs could not be read"

    def close(self):
        self.client.close()


@dataclass
class Entry:
    model_id: str
    revision: str
    path: Path
    run_dir: Path
    busy: bool = True
    container: str | None = None
    last_used: float = 0.0


class DockerRuntime:
    """Bounded, per-revision container cache. One invocation per container at a time."""
    def __init__(self, settings: Settings, engine: DockerEngine | None = None):
        self.settings = settings
        self.engine = engine or DockerEngine(settings.docker_socket)
        self.entries: dict[str, Entry] = {}
        self.lock = threading.RLock()
        self.revoked: set[str] = set()
        self.log_sink = None

    def reconcile(self) -> None:
        filters = json.dumps({"label": [f"org.cvat-model-registry.instance={self.settings.instance}"]})
        for container in self.engine.request("GET", "/containers/json", params={"all": "1", "filters": filters}):
            self.engine.request("DELETE", "/containers/" + container["Id"], params={"force": "1"})

    def _stop(self, entry: Entry) -> None:
        if entry.container:
            self.engine.request("DELETE", f"/containers/{entry.container}", params={"force": "1"})
            entry.container = None
        shutil.rmtree(entry.run_dir, ignore_errors=True)

    def spec(self, entry: Entry) -> dict:
        relative = entry.path.resolve().relative_to(self.settings.data_dir.resolve())
        run_relative = entry.run_dir.resolve().relative_to(self.settings.data_dir.resolve())
        host_root = Path(self.settings.host_data_dir)
        providers = "CUDAExecutionProvider,CPUExecutionProvider" if self.settings.gpu_device else "CPUExecutionProvider"
        config = {
            "Image": self.settings.worker_image,
            "User": "65532:65532",
            "WorkingDir": "/app",
            "Cmd": ["python", "-m", "uvicorn", "registry.worker:app", "--uds", "/run/model/worker.sock", "--workers", "1", "--no-access-log"],
            "Env": [f"MR_PROVIDERS={providers}", "PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "NVIDIA_DRIVER_CAPABILITIES=compute,utility"],
            "Labels": {"org.cvat-model-registry.instance": self.settings.instance, "org.cvat-model-registry.model": entry.model_id, "org.cvat-model-registry.revision": entry.revision},
            "HostConfig": {
                "NetworkMode": "none", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 128,
                "Memory": self.settings.worker_memory, "MemorySwap": self.settings.worker_memory,
                "NanoCpus": self.settings.worker_cpus * 1_000_000_000, "Init": True,
                "RestartPolicy": {"Name": "no"},
                "Mounts": [
                    {"Type": "bind", "Source": str(host_root / relative), "Target": "/model", "ReadOnly": True},
                    {"Type": "bind", "Source": str(host_root / run_relative), "Target": "/run/model", "ReadOnly": False},
                ],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=268435456,uid=65532,gid=65532,mode=1777"},
                "LogConfig": {"Type": "json-file", "Config": {"max-size": "5m", "max-file": "3"}},
            },
        }
        if self.settings.gpu_device:
            config["HostConfig"]["DeviceRequests"] = [{"Driver": "nvidia", "DeviceIDs": [self.settings.gpu_device], "Capabilities": [["gpu"]]}]
        return config

    def _load(self, entry: Entry) -> None:
        if entry.container:
            return
        entry.run_dir.mkdir(parents=True, exist_ok=True)
        os.chown(entry.run_dir, 65532, 65532)
        entry.run_dir.chmod(0o700)
        socket = entry.run_dir / "worker.sock"
        socket.unlink(missing_ok=True)
        # Deliberately do not pull/build arbitrary images from submitted packages.
        self.engine.request("GET", "/images/" + quote(self.settings.worker_image, safe="") + "/json")
        result = self.engine.request("POST", "/containers/create", json=self.spec(entry))
        entry.container = result["Id"]
        self.engine.request("POST", f"/containers/{entry.container}/start")
        deadline = time.monotonic() + self.settings.load_timeout
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://worker", timeout=1, trust_env=False) as client:
            while time.monotonic() < deadline:
                status = self.engine.request("GET", f"/containers/{entry.container}/json")["State"]
                if not status["Running"]:
                    raise RuntimeFailure("Worker exited during load\n" + self.engine.logs(entry.container))
                try:
                    if client.get("/health").json().get("ready"):
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.2)
        raise RuntimeFailure("Worker load timeout\n" + self.engine.logs(entry.container))

    def call(self, model_id: str, revision: str, path: Path, payload: dict, request_id: str) -> list:
        key = model_id + "-" + revision
        with self.lock:
            if model_id in self.revoked:
                raise RuntimeFailure("model was deleted")
            entry = self.entries.get(key)
            if entry and entry.busy:
                raise Busy("This revision is currently busy; retry after the active request finishes")
            if entry is None:
                if len(self.entries) >= self.settings.max_loaded:
                    idle = [(k, e) for k, e in self.entries.items() if not e.busy]
                    if not idle:
                        raise Busy("All worker slots are busy; retry later")
                    old_key, old = min(idle, key=lambda pair: pair[1].last_used)
                    self._stop(old)
                    self.entries.pop(old_key)
                entry = Entry(model_id, revision, path, self.settings.data_dir / "run" / key)
                self.entries[key] = entry
            entry.busy = True
        try:
            self._load(entry)
            socket = entry.run_dir / "worker.sock"
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://worker", timeout=self.settings.infer_timeout, trust_env=False) as client:
                result = limited_json(client, "POST", "/predict", json=payload, headers={"x-request-id": request_id})
            if not isinstance(result, list):
                raise RuntimeFailure("worker returned a non-list result")
            if self.log_sink and entry.container:
                # Keep a bounded worker stdout/stderr snapshot even for successful requests.
                # Adjacent snapshots can overlap; request IDs identify individual invocations.
                text = self.engine.logs(entry.container)
                if text:
                    self.log_sink(model_id, revision, request_id, text)
            return result
        except Exception as exc:
            logs = self.engine.logs(entry.container) if entry.container else ""
            with self.lock:
                try:
                    self._stop(entry)
                finally:
                    self.entries.pop(key, None)
            raise RuntimeFailure(f"{exc}\n{logs}"[-20000:]) from exc
        finally:
            with self.lock:
                entry.busy = False
                entry.last_used = time.monotonic()
                if model_id in self.revoked and key in self.entries:
                    self._stop(entry)
                    self.entries.pop(key, None)

    def discard(self, model_id: str, revision: str) -> None:
        key = model_id + "-" + revision
        with self.lock:
            entry = self.entries.get(key)
            if entry and not entry.busy:
                self._stop(entry)
                self.entries.pop(key, None)

    def revoke(self, model_id: str) -> None:
        with self.lock:
            self.revoked.add(model_id)
            for key, entry in list(self.entries.items()):
                if entry.model_id == model_id and not entry.busy:
                    self._stop(entry)
                    self.entries.pop(key, None)

    def close(self) -> None:
        with self.lock:
            for entry in list(self.entries.values()):
                self._stop(entry)
            self.entries.clear()
            self.engine.close()
