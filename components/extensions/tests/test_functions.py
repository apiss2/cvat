# SPDX-License-Identifier: MIT
"""CPU contracts for the common definitions/deployer; network and Docker are doubles."""
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType, SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from components.extensions import functions as f


@pytest.fixture
def values():
    return {**f.DEFAULTS, "CVAT_NETWORK_NAME": "test_cvat", "NUCLIO_NAMESPACE": "test",
            "SAM2_REDIS_PASSWORD": "a" * 48, "SAM31_REDIS_PASSWORD": "b" * 48,
            "HF_TOKEN": "hf_private_test_token"}


@pytest.fixture
def source_root(tmp_path):
    # Recipes and the generator are real; placeholder entry points isolate packaging.
    root = tmp_path / "checkout"
    for feature, relative in f.SOURCES.items():
        directory = root / relative
        directory.mkdir(parents=True)
        for handler in f.FUNCTIONS[feature].values():
            (directory / (handler.split(":")[0] + ".py")).write_text("# handler fixture\n")
        recipe = root / f"components/{feature}/build.json"
        recipe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / f"components/{feature}/build.json", recipe)
    for name in ("protocol.py", "geometry.py", "redis_store.py"):
        shutil.copyfile(ROOT / f.SHARED / name, root / f.SHARED / name)
    target = root / "components/extensions/functions.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "components/extensions/functions.py", target)
    return root


@pytest.fixture
def deployment(values, source_root, tmp_path, monkeypatch):
    calls, definitions, downloads = [], [], []
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    manager = SimpleNamespace(root=source_root, values=values, state=state,
                              nuctl_check=lambda model: "1.16.3", functions=lambda: [])
    manager.guard_function_ownership = lambda names: calls.append(["owner", *names])
    manager.verify_functions = lambda names, **kwargs: calls.append(["verify", *names])
    manager.function_environment = lambda: {name: f.runtime_environment(name, values) for name in f.OWNERS}
    def run(command, **kwargs):
        calls.append(command)
        assert values["HF_TOKEN"] not in repr(command)
        if command[:3] == ["nuctl", "get", "projects"]:
            return '{"project":{"meta":{"name":"cvat"}}}'
        if command[:2] == ["nuctl", "deploy"]:
            config = Path(command[command.index("--file")+1])
            source = Path(command[command.index("--path")+1])
            assert config.parent == state and source not in config.parents
            assert config.stat().st_mode & 0o077 == 0
            body = json.loads(config.read_text()); definitions.append(body)
            assert "volumes" not in body["spec"] and values["HF_TOKEN"] not in config.read_text()
            assert all(values["HF_TOKEN"] not in p.read_text() for p in source.glob("*.py"))
            name = body["metadata"]["name"]
            if f.OWNERS[name] in ("sam2", "sam31"):
                for file in ("protocol.py", "geometry.py", "redis_store.py"):
                    assert (source/file).read_bytes() == (source_root/f.SHARED/file).read_bytes()
                env = f.model_environment(f.OWNERS[name], values)
                target = source / Path(env[f.OWNERS[name].upper()+"_CHECKPOINT"]).relative_to("/opt/nuclio")
                assert target.exists() == ("--run-image" not in command)
            return ""
        return "sha256:image" if command[0] == "docker" else ""
    def download(feature, config, source, root):
        downloads.append(feature)
        target = source / Path(f.model_environment(feature, config)[feature.upper()+"_CHECKPOINT"]).relative_to("/opt/nuclio")
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b"weight fixture")
    manager.run = run
    monkeypatch.setattr(f, "download_checkpoint", download)
    return manager, calls, definitions, downloads


@pytest.mark.parametrize("model", list(f.SAM2_MODELS))
def test_sam2_named_model_pair(model, values):
    values["SAM2_MODEL"] = model
    environment = f.model_environment("sam2", values)
    assert environment["SAM2_CONFIG"] == f.SAM2_MODELS[model]
    assert environment["SAM2_CHECKPOINT"].endswith(model + ".pt")


@pytest.mark.parametrize("feature", ["sam2", "sam31"])
@pytest.mark.parametrize("value", [None, ""])
def test_default_model(feature, value, values):
    key = feature.upper()+"_MODEL"
    values.pop(key)
    if value is not None: values[key] = value
    assert f.model_environment(feature, values)[key] == f.DEFAULTS[key]


@pytest.mark.parametrize("model", ["facebook/sam3.1", "team/ultrasound-multiplex"])
def test_sam31_named_repository(model, values):
    values["SAM31_MODEL"] = model
    assert f.model_environment("sam31", values)["SAM31_MODEL"] == model
    assert "SAM31_CHECKPOINT_HOST" not in values


@pytest.mark.parametrize("feature,model", [("sam2", "unknown"), ("sam31", "/tmp/model.pt"),
    ("sam31", "https://host/model"), ("sam31", "owner/model/extra"), ("sam31", "../model")])
def test_invalid_models_rejected(feature, model, values):
    values[feature.upper()+"_MODEL"] = model
    with pytest.raises(ValueError): f.model_environment(feature, values)


@pytest.mark.parametrize("name", list(f.OWNERS))
def test_structured_definition_shared_contract(name, values, source_root):
    body = f.render_function(name, values, "image:test", source_root)
    spec, annotations = body["spec"], body["metadata"]["annotations"]
    assert spec["build"]["image"] == "image:test"
    assert spec["handler"] == f.FUNCTIONS[f.OWNERS[name]][name]
    assert spec["triggers"]["http"]["attributes"]["disablePortPublishing"] is True
    assert spec["minReplicas"] == spec["maxReplicas"] == 1
    assert spec["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert annotations["type"] == ("tracker" if name.endswith("tracker") else "interactor")
    if name.endswith("interactor"):
        assert not any("REDIS" in entry["name"] for entry in spec["env"])
    assert values["HF_TOKEN"] not in json.dumps(body)
    assert f.render_function(name, values, "image:test", source_root) == body


@pytest.mark.parametrize("feature,mode", [(feature, mode) for feature in f.FUNCTIONS
    for mode in ("image", "tracker", "all") if not(feature == "ultrasam" and mode == "tracker")])
@pytest.mark.parametrize("no_build", [False, True])
def test_all_families_use_one_deploy_path(deployment, feature, mode, no_build):
    manager, calls, definitions, downloads = deployment
    f.deploy(manager, {}, feature=feature, mode=mode, no_build=no_build)
    selected = f.selected_functions(feature, mode)
    assert [body["metadata"]["name"] for body in definitions] == selected
    assert downloads == ([feature] if not no_build and feature != "ultrasam" else [])
    assert not list(manager.state.iterdir())
    assert calls[0] == ["owner", *selected] and calls[-1] == ["verify", *selected]


@pytest.mark.parametrize("feature", ["sam2", "sam31", "ultrasam"])
def test_existing_image_reused_without_download(deployment, feature):
    manager, calls, definitions, downloads = deployment
    images = f.image_names("1.16.3", feature, manager.values, manager.root)
    manager.functions = lambda: [{"Id": name, "State": {"Running": True}, "Mounts": [],
        "Config": {"Image": image, "Labels": {"nuclio.io/function-name": name},
        "Env": [f"{k}={v}" for k,v in f.runtime_environment(name, manager.values).items()]}}
        for name,image in images.items()]
    f.deploy(manager, {}, feature=feature)
    assert not definitions and not downloads
    f.deploy(manager, {}, feature=feature, force=True)
    assert len(definitions) == len(images)


def test_no_build_missing_image_fails_before_deploy(deployment):
    manager, calls, definitions, downloads = deployment
    manager.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("missing image"))
    with pytest.raises(RuntimeError): f.deploy(manager, {}, feature="sam31", no_build=True)
    assert not definitions and not downloads and not list(manager.state.iterdir())


def test_download_failure_cleans_staging_and_never_deploys(deployment, monkeypatch):
    manager, calls, definitions, downloads = deployment
    monkeypatch.setattr(f, "download_checkpoint", lambda *args: (_ for _ in ()).throw(ValueError("denied")))
    with pytest.raises(ValueError): f.deploy(manager, {}, feature="sam31")
    assert not definitions and not list(manager.state.iterdir())
    assert not any(c[0] == "nuctl" for c in calls)


def test_deploy_failure_cleans_private_configuration(deployment):
    manager, calls, definitions, downloads = deployment
    run = manager.run
    def fail(command, **kwargs):
        if command[:2] == ["nuctl", "deploy"]: raise RuntimeError("failed")
        return run(command, **kwargs)
    manager.run = fail
    with pytest.raises(RuntimeError): f.deploy(manager, {}, feature="sam31")
    assert not list(manager.state.iterdir())


def test_image_fingerprint_includes_model_source_recipe_not_token(values, source_root):
    first = f.image_names("1.16.3", "sam31", values, source_root)
    values["HF_TOKEN"] = "changed credential"
    assert first == f.image_names("1.16.3", "sam31", values, source_root)
    values["SAM31_MODEL"] = "team/finetuned"
    assert first != f.image_names("1.16.3", "sam31", values, source_root)
    values["SAM31_MODEL"] = f.DEFAULTS["SAM31_MODEL"]
    (source_root/f.SHARED/"geometry.py").write_text("# changed\n")
    assert first != f.image_names("1.16.3", "sam31", values, source_root)
    assert first != f.image_names("1.17.0", "sam31", values, source_root)


@pytest.mark.parametrize("destination", ["/", "/opt", "/opt/nuclio", "/opt/nuclio/sam3.1_multiplex.pt"])
def test_checkpoint_shadow_mount_rejected(values, destination):
    assert not f.checkpoint_matches({"Mounts": [{"Destination": destination}]}, f.model_environment("sam31", values))


def test_unrelated_mount_allowed(values):
    assert f.checkpoint_matches({"Mounts": [{"Destination": "/tmp"}]}, f.model_environment("sam31", values))


@pytest.mark.parametrize("feature", ["sam2", "sam31"])
def test_download_named_weights_and_token_not_redirected(feature, values, tmp_path, monkeypatch):
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        response = io.BytesIO(b"weights"); response.headers = {"Content-Length": "7"}
        return response
    monkeypatch.setattr(f, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    f.download_checkpoint(feature, values, tmp_path)
    request = requests[0]
    assert request.full_url.startswith("https://")
    assert request.has_header("Authorization") == (feature == "sam31")
    redirected = f.HTTPSRedirectHandler().redirect_request(request, None, 302, "", {}, "https://cdn.example/weights?signature=x")
    assert not redirected.has_header("Authorization")
    assert len(list(tmp_path.rglob("*.pt"))) == 1
    target = next(tmp_path.rglob("*.pt"))
    assert target.read_bytes() == b"weights" and target.stat().st_mode & 0o222 == 0
    if feature == "sam31": assert "/facebook/sam3.1/resolve/main/sam3.1_multiplex.pt" in request.full_url


def test_no_downgrade_redirect():
    with pytest.raises(ValueError, match="HTTPS"):
        f.HTTPSRedirectHandler().redirect_request(Request("https://huggingface.co/a/b"), None, 302, "", {}, "http://cdn.example/file")


@pytest.mark.parametrize("content,length", [(b"", "0"), (b"partial", "100"), (b"weights", "bad")])
def test_download_rejects_empty_truncated_invalid_length(values, tmp_path, monkeypatch, content, length):
    response = io.BytesIO(content); response.headers = {"Content-Length": length}
    monkeypatch.setattr(f, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **k: response))
    with pytest.raises(ValueError): f.download_checkpoint("sam31", values, tmp_path)
    assert not list(tmp_path.rglob("*.pt"))


@pytest.mark.parametrize("code", [401, 403, 404, 503])
def test_download_error_does_not_expose_token(values, tmp_path, monkeypatch, code):
    def fail(*args, **kwargs): raise HTTPError("https://host/?token="+values["HF_TOKEN"], code, "private", {}, None)
    monkeypatch.setattr(f, "build_opener", lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(ValueError) as error: f.download_checkpoint("sam31", values, tmp_path)
    assert values["HF_TOKEN"] not in str(error.value) and not list(tmp_path.rglob("*.pt"))


def test_optional_sam2_local_weights(values, tmp_path, monkeypatch):
    source = tmp_path/"custom.pt"; source.write_bytes(b"local")
    values["SAM2_CHECKPOINT_HOST"] = str(source)
    monkeypatch.setattr(f, "build_opener", lambda *args: pytest.fail("local model must not download"))
    target = tmp_path/"staging"
    f.download_checkpoint("sam2", values, target, tmp_path/"checkout")
    assert next(target.rglob("*.pt")).read_bytes() == b"local"


def test_optional_sam2_missing_file_allowed_only_for_no_build(values, tmp_path):
    values["SAM2_CHECKPOINT_HOST"] = str(tmp_path/"missing.pt")
    assert f.local_checkpoint(values, tmp_path/"checkout") is not None
    with pytest.raises(ValueError): f.local_checkpoint(values, tmp_path/"checkout", verify_file=True)


def test_shared_redis_namespace_is_explicit(monkeypatch, values):
    spec = importlib.util.spec_from_file_location("protocol", ROOT/f.SHARED/"protocol.py")
    module = importlib.util.module_from_spec(spec); monkeypatch.setitem(sys.modules, "protocol", module); spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location("shared_redis_test", ROOT/f.SHARED/"redis_store.py")
    store = importlib.util.module_from_spec(spec); monkeypatch.setitem(sys.modules, spec.name, store); spec.loader.exec_module(store)
    connections = []
    redis = ModuleType("redis")
    def connect(**kwargs): connections.append(kwargs); return SimpleNamespace(ping=lambda: True)
    redis.Redis = connect; redis.exceptions = SimpleNamespace(RedisError=RuntimeError)
    monkeypatch.setitem(sys.modules, "redis", redis)
    for key,value in values.items(): monkeypatch.setenv(key, value)
    sam2 = store.RedisStore.from_env("SAM2"); sam31 = store.RedisStore.from_env("SAM31")
    assert sam2.prefix == "cvat:sam2:" and sam31.prefix == "cvat:sam31:"
    assert connections[0]["host"] == "sam2_redis" and connections[1]["host"] == "sam31_redis"
    assert connections[0]["password"] == "a"*48 and connections[1]["password"] == "b"*48
    with pytest.raises(ValueError): store.RedisStore.from_env("unknown")
