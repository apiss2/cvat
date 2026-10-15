# SPDX-License-Identifier: MIT
"""Actual FastAPI registration/update routes with isolated auth/service doubles.

No Docker, ONNX model inference or background publisher is exercised here.
The manifest schema, upload assembly and partial-update implementation are real.
"""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import zipfile

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles
import pytest

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = ROOT / "components/model_registry/registry"


@pytest.fixture
def api(tmp_path, monkeypatch):
    prefix = "isolated_registration"
    package = ModuleType(prefix); package.__path__ = [str(REGISTRY)]
    monkeypatch.setitem(sys.modules, prefix, package)
    def stub(name, **members):
        module = ModuleType(prefix+"."+name)
        module.__dict__.update(members); monkeypatch.setitem(sys.modules, module.__name__, module)
    def owner(user, record):
        if user.name != record["owner"]: raise HTTPException(403, "not owner")
    class Guard(BaseHTTPMiddleware):
        def __init__(self, app, auth): super().__init__(app)
        async def dispatch(self, request, call_next):
            request.state.user = SimpleNamespace(name=request.headers.get("x-user", "alice"), admin=False)
            return await call_next(request)
    stub("auth", Auth=lambda settings: None, owner=owner)
    stub("http_limits", GuardMiddleware=Guard)
    stub("config", Settings=object)
    stub("packages", MAX_ARCHIVE=2*1024**3, MAX_EXPANDED=3*1024**3, MAX_FILES=40)
    stub("runtime", Busy=type("Busy", (Exception,), {}), DockerRuntime=object,
         RuntimeFailure=type("RuntimeFailure", (Exception,), {}))
    stub("service", Gone=type("Gone", (Exception,), {}), Service=object)
    stub("store", Conflict=type("Conflict", (Exception,), {}))
    modules = {}
    for name in ("schema", "partial_updates", "manager"):
        spec = importlib.util.spec_from_file_location(prefix+"."+name, REGISTRY/(name+".py"))
        module = importlib.util.module_from_spec(spec); monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module); modules[name] = module
    manager = modules["manager"]
    monkeypatch.setattr(manager, "StaticFiles", lambda **kwargs: StaticFiles(**kwargs, check_dir=False))
    monkeypatch.setattr(modules["partial_updates"].shutil, "disk_usage", lambda path: SimpleNamespace(free=100*1024**3))
    settings = SimpleNamespace(data_dir=tmp_path, validate=lambda: None)
    (tmp_path/"uploads").mkdir()
    stored = tmp_path/"packages/model/old"; stored.mkdir(parents=True)
    for name in ("model.py", "model.onnx", "sample.png"): (stored/name).write_bytes(b"old "+name.encode())
    manifest = {"name": "Example", "weights": ["model.onnx"], "labels": [{"id": 0, "name": "Organ", "type": "tag"}]}
    record = {"id": "model", "owner": "alice", "deleted": False, "active_revision": "old"}
    submitted = []
    def submit(path, *args):
        with zipfile.ZipFile(path) as archive: content = {name: archive.read(name) for name in archive.namelist()}
        submitted.append((args, content)); path.unlink()
        return {"id": "operation", "status": "queued"}
    service = SimpleNamespace(recover=lambda: None, close=lambda: None, submit=submit,
        store=SimpleNamespace(model=lambda mid: record, revision=lambda mid, rev: {"manifest": manifest, "path": "packages/model/old"}))
    app = manager.create_app(settings, service=service, reconcile=False)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, submitted=submitted, manifest=manifest, stored=stored, record=record, root=tmp_path, app=app)


def files():
    return [("code", ("model.py", b"new code")), ("weights", ("model.onnx", b"new weights")),
            ("sample", ("sample.png", b"new sample"))]


def test_registration_only_calls_new_model_service(api):
    response = api.client.post("/api/upload", data={"manifest": json.dumps(api.manifest)}, files=files())
    assert response.status_code == 202, response.text
    args, content = api.submitted[0]
    assert args == ("alice",) and content["model.onnx"] == b"new weights"
    assert not list((api.root/"uploads").iterdir())


@pytest.mark.parametrize("key", ["model_id", "expected_revision", "unknown"])
@pytest.mark.parametrize("value", ["model", ""])
def test_registration_rejects_update_and_unknown_arguments(api, key, value):
    response = api.client.post("/api/upload", data={"manifest": json.dumps(api.manifest), key: value}, files=files())
    assert response.status_code == 422 and not api.submitted
    assert not list((api.root/"uploads").iterdir())


@pytest.mark.parametrize("key", ["manifest", "code", "sample"])
def test_registration_rejects_duplicate_single_fields(api, key):
    parts = [("manifest", (None, json.dumps(api.manifest))), *files()]
    parts.append(next(item for item in parts if item[0] == key))
    response = api.client.post("/api/upload", files=parts)
    assert response.status_code == 422 and not api.submitted


def test_registration_still_accepts_multiple_distinct_weights(api):
    manifest = {**api.manifest, "weights": ["model.onnx", "encoder.onnx"]}
    response = api.client.post("/api/upload", data={"manifest": json.dumps(manifest)},
        files=files()+[("weights", ("encoder.onnx", b"encoder"))])
    assert response.status_code == 202 and api.submitted[0][0] == ("alice",)


def test_partial_update_is_only_update_route_and_retains_unmodified_files(api):
    response = api.client.post("/api/models/model/update", data={"expected_revision": "old"},
        files={"weights": ("model.onnx", b"updated weights")})
    assert response.status_code == 202, response.text
    args, content = api.submitted[0]
    assert args == ("alice", "model", "old")
    assert content["model.py"] == b"old model.py" and content["sample.png"] == b"old sample.png"
    assert content["model.onnx"] == b"updated weights"
    assert (api.stored/"model.onnx").read_bytes() == b"old model.onnx"


@pytest.mark.parametrize("user,revision,status", [("bob", "old", 403), ("alice", "stale", 409), ("alice", "", 422)])
def test_partial_update_preserves_owner_revision_checks(api, user, revision, status):
    response = api.client.post("/api/models/model/update", headers={"x-user": user}, data={"expected_revision": revision},
        files={"weights": ("model.onnx", b"new")})
    assert response.status_code == status and not api.submitted


def test_partial_update_rejects_renamed_file(api):
    response = api.client.post("/api/models/model/update", data={"expected_revision": "old"}, files={"weights": ("renamed.onnx", b"new")})
    assert response.status_code == 422 and not api.submitted
    assert not list((api.root/"uploads").iterdir())


def test_full_file_update_uses_partial_endpoint(api):
    response = api.client.post("/api/models/model/update", data={"expected_revision": "old", "manifest": json.dumps(api.manifest)}, files=files())
    assert response.status_code == 202 and api.submitted[0][0] == ("alice", "model", "old")


def test_registration_openapi_does_not_advertise_update_fields(api):
    schema = api.app.openapi()
    reference = schema["paths"]["/api/upload"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    properties = schema["components"]["schemas"][reference.rsplit("/", 1)[1]]["properties"]
    assert set(properties) == {"manifest", "code", "weights", "sample"}
