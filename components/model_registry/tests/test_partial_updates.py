# SPDX-License-Identifier: MIT
"""Partial-update contract tests. No uploaded code, Docker or ONNX inference runs here."""
import importlib.util
import io
import json
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile


@pytest.fixture(scope="module")
def updates():
    # Use the production Manifest and implementation, but isolate service/auth
    # collaborators. This suite also runs without a Docker daemon or CVAT server.
    root = Path(__file__).resolve().parents[1] / "registry"
    prefix = "_partial_updates_contract"
    pkg = types.ModuleType(prefix)
    pkg.__path__ = [str(root)]
    sys.modules[prefix] = pkg
    def stub(name, **values):
        module = types.ModuleType(f"{prefix}.{name}")
        vars(module).update(values)
        sys.modules[module.__name__] = module
    class Conflict(RuntimeError): pass
    class Gone(RuntimeError): pass
    def owner(user, record):
        if not user.admin and user.name != record["owner"]:
            raise HTTPException(403, "Owner only")
    stub("auth", owner=owner)
    stub("packages", MAX_ARCHIVE=2*1024**3, MAX_EXPANDED=3*1024**3, MAX_FILES=40)
    stub("service", Gone=Gone)
    stub("store", Conflict=Conflict)
    name = prefix + ".partial_updates"
    spec = importlib.util.spec_from_file_location(name, root / "partial_updates.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    for key in list(sys.modules):
        if key == prefix or key.startswith(prefix + "."):
            del sys.modules[key]


@pytest.fixture
def package(tmp_path, updates):
    source = tmp_path / "packages" / ("a" * 20) / ("b" * 16)
    source.mkdir(parents=True)
    (tmp_path / "uploads").mkdir()
    manifest = updates.Manifest.model_validate({
        "name": "test", "description": "retained", "author_contact": "author@example.org",
        "weights": ["encoder.onnx", "decoder.onnx"],
        "labels": [{"id": 7, "name": "vessel", "type": "polygon"}],
        "polygon": {"min_distance_px": 3.0, "spacing_percent": 2.0, "min_area_px": 20.0},
    })
    files = {"encoder.onnx": b"old encoder", "decoder.onnx": b"old decoder",
             "model.py": b"class Model: pass", "helper.py": b"VALUE = 1", "sample.png": b"sample bytes",
             "manifest.json": manifest.model_dump_json().encode()}
    for name, data in files.items():
        (source / name).write_bytes(data)
    return tmp_path, source, manifest, files


def upload(name, data):
    return UploadFile(filename=name, file=io.BytesIO(data))


def read_zip(path):
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def test_onnx_only_retains_code_helper_sample_metadata_and_other_weight(updates, package):
    root, source, manifest, files = package
    target = root / "candidate.zip"
    updates.assemble_update(target, source, manifest, None, [upload("encoder.onnx", b"new")], None)
    result = read_zip(target)
    assert result["encoder.onnx"] == b"new"
    for name in ("model.py", "helper.py", "sample.png", "decoder.onnx"):
        assert result[name] == files[name]
    assert json.loads(result["manifest.json"]) == manifest.model_dump()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == files


def test_metadata_patch_merges_polygon_and_all_omitted_fields(updates, package):
    _, _, manifest, _ = package
    value = updates.merge_manifest(manifest.model_dump(), {"description": "", "polygon": {"min_area_px": 25.0}})
    assert value.description == ""
    assert value.author_contact == manifest.author_contact
    assert value.weights == manifest.weights
    assert value.polygon.min_area_px == 25
    assert value.polygon.min_distance_px == 3


@pytest.mark.parametrize("patch", [{"name": ""}, {"weights": []}, {"weights": ["../x.onnx"]}, {"surprise": True}, []])
def test_invalid_manifest_patch_rejected(updates, package, patch):
    with pytest.raises(ValueError):
        updates.merge_manifest(package[2].model_dump(), patch)


def test_sample_extension_change_removes_old_sample(updates, package):
    root, source, manifest, files = package
    target = root / "candidate.zip"
    updates.assemble_update(target, source, manifest, None, [], upload("new.jpeg", b"jpeg"))
    result = read_zip(target)
    assert result["sample.jpg"] == b"jpeg" and "sample.png" not in result
    assert (source / "sample.png").read_bytes() == files["sample.png"]


def test_code_only_keeps_all_weights(updates, package):
    root, source, manifest, _ = package
    target = root / "candidate.zip"
    updates.assemble_update(target, source, manifest, upload("model.py", b"new code"), [], None)
    assert read_zip(target)["model.py"] == b"new code"
    assert (source / "model.py").read_bytes() == b"class Model: pass"


@pytest.mark.parametrize("weights", [["decoder.onnx"], ["encoder.onnx", "decoder.onnx", "new.onnx"], ["renamed.onnx", "decoder.onnx"]])
def test_manifest_cannot_remove_add_or_rename_weights(updates, package, weights):
    with pytest.raises(ValueError, match="Weight filenames cannot change"):
        updates.merge_manifest(package[2].model_dump(), {"weights": weights})


def test_renamed_code_rejected_before_assembly(updates, package):
    root, source, manifest, files = package
    target = root / "candidate.zip"
    with pytest.raises(ValueError, match="filename must be model.py"):
        updates.assemble_update(target, source, manifest, upload("renamed.py", b"new code"), [], None)
    assert not target.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == files


@pytest.mark.parametrize("names", [["../escape.onnx"], ["other.onnx"], ["encoder.onnx", "encoder.onnx"]])
def test_bad_replacement_names_rejected(updates, package, names):
    root, source, manifest, _ = package
    with pytest.raises(ValueError):
        updates.assemble_update(root / "candidate.zip", source, manifest, None, [upload(n, b"x") for n in names], None)


def test_stored_symlink_rejected(updates, package):
    root, source, manifest, _ = package
    (source / "encoder.onnx").unlink()
    (source / "encoder.onnx").symlink_to(root / "outside")
    with pytest.raises(ValueError, match="link"):
        updates.assemble_update(root / "candidate.zip", source, manifest, None, [], None)


def test_size_limit_applies_to_inherited_files(updates, package, monkeypatch):
    root, source, manifest, _ = package
    monkeypatch.setattr(updates, "MAX_ARCHIVE", 500)
    with pytest.raises(HTTPException) as exc:
        updates.assemble_update(root / "candidate.zip", source, manifest, None, [], None)
    assert exc.value.status_code == 413


@pytest.fixture
def client(updates, package, monkeypatch):
    root, source, manifest, _ = package
    record = {"id": "a" * 20, "owner": "alice", "active_revision": "b" * 16, "deleted": False}
    revision = {"manifest": manifest.model_dump(), "path": str(source.relative_to(root))}
    state = SimpleNamespace(user="alice", admin=False, submissions=[], fail=False)
    class Store:
        def model(self, model_id):
            if model_id != record["id"]: raise KeyError(model_id)
            return dict(record)
        def revision(self, model_id, rev):
            assert rev == "b" * 16
            return revision
    class Service:
        store = Store()
        def submit(self, archive, actor, model_id, expected):
            if state.fail or expected != record["active_revision"]:
                raise updates.Conflict("concurrent update")
            state.submissions.append((read_zip(archive), actor, model_id, expected))
            archive.unlink()  # stand in for the registration worker consuming the candidate
            return {"id": "operation", "status": "queued"}
    app = FastAPI()
    @app.middleware("http")
    async def user(request, call_next):
        request.state.user = SimpleNamespace(name=state.user, admin=state.admin)
        return await call_next(request)
    for kind, status in [(updates.Conflict, 409), (updates.Gone, 410), (ValueError, 422), (KeyError, 404)]:
        async def handle(request, exc, code=status):
            return JSONResponse({"detail": str(exc)}, status_code=code)
        app.add_exception_handler(kind, handle)
    monkeypatch.setattr(updates.shutil, "disk_usage", lambda p: SimpleNamespace(free=20 * 1024**3))
    updates.install_partial_updates(app, Service(), SimpleNamespace(data_dir=root))
    with TestClient(app) as c:
        yield c, state, record, root


def test_endpoint_accepts_one_weight_without_manifest_code_sample(updates, client):
    c, state, record, _ = client
    response = c.post(f"/api/models/{record['id']}/update", data={"expected_revision": record["active_revision"]},
                      files={"weights": ("encoder.onnx", b"new")})
    assert response.status_code == 202, response.text
    bundle, actor, model_id, expected = state.submissions[0]
    assert bundle["model.py"] == b"class Model: pass"
    assert actor == "alice" and expected == "b" * 16


def test_endpoint_allows_metadata_only(client):
    c, state, record, _ = client
    response = c.post(f"/api/models/{record['id']}/update", data={"expected_revision": record["active_revision"], "manifest": '{"description":"new"}'})
    assert response.status_code == 202
    assert json.loads(state.submissions[0][0]["manifest.json"])["description"] == "new"


@pytest.mark.parametrize("field, filename", [("weights", "renamed.onnx"), ("weights", "Encoder.onnx"), ("code", "renamed.py")])
def test_endpoint_rejects_filename_mismatch(client, package, field, filename):
    c, state, record, root = client
    response = c.post(f"/api/models/{record['id']}/update",
                      data={"expected_revision": record["active_revision"]},
                      files={field: (filename, b"new")})
    assert response.status_code == 422
    assert not state.submissions and not list((root / "uploads").iterdir())
    assert {p.name: p.read_bytes() for p in package[1].iterdir()} == package[3]


def test_endpoint_rejects_manifest_weight_rename(client):
    c, state, record, root = client
    response = c.post(f"/api/models/{record['id']}/update",
                      data={"expected_revision": record["active_revision"], "manifest": '{"weights":["renamed.onnx","decoder.onnx"]}'},
                      files={"weights": ("renamed.onnx", b"new")})
    assert response.status_code == 422
    assert not state.submissions and not list((root / "uploads").iterdir())


@pytest.mark.parametrize("scenario, status", [("stale",409), ("missing-revision",422), ("non-owner",403), ("deleted",410), ("no-change",422)])
def test_endpoint_rejects_unsafe_or_empty_update(client, scenario, status):
    c, state, record, root = client
    data = {"expected_revision": record["active_revision"], "manifest": '{"description":"new"}'}
    if scenario == "stale": data["expected_revision"] = "c" * 16
    if scenario == "missing-revision": data.pop("expected_revision")
    if scenario == "non-owner": state.user = "bob"
    if scenario == "deleted": record["deleted"] = True
    if scenario == "no-change": data.pop("manifest")
    response = c.post(f"/api/models/{record['id']}/update", data=data)
    assert response.status_code == status, response.text
    assert not state.submissions and not list((root / "uploads").iterdir())


def test_submit_failure_removes_candidate_leaves_old_files(client, package):
    c, state, record, root = client
    state.fail = True
    response = c.post(f"/api/models/{record['id']}/update", data={"expected_revision": record["active_revision"]}, files={"weights": ("encoder.onnx", b"new")})
    assert response.status_code == 409
    assert record["active_revision"] == "b" * 16
    assert not list((root / "uploads").iterdir())
    assert (package[1] / "encoder.onnx").read_bytes() == b"old encoder"
