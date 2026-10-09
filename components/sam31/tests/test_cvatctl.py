# SPDX-License-Identifier: MIT
"""Common CLI orchestration contracts, using command doubles rather than Docker."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def cli(monkeypatch):
    # Registry collaborators are unrelated to SAM3.1; the actual manager is imported.
    registry = ModuleType("registryctl")
    registry.connection_values = lambda values: values
    registry.public_address = lambda value: (value, "localhost")
    registry.purge_deleted = lambda *args: None
    storage = ModuleType("storage")
    storage.default_home = lambda root: root / "cvat-model-registry"
    storage.prepare_storage = lambda *args: None
    storage.storage_paths = lambda root, home: (home, None, None)
    monkeypatch.setitem(sys.modules, "registryctl", registry)
    monkeypatch.setitem(sys.modules, "storage", storage)
    spec = importlib.util.spec_from_file_location("review_cvatctl", ROOT / "components/extensions/manage.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.fixture
def manager(cli, tmp_path):
    checkpoint = tmp_path / "model.pt"; checkpoint.write_bytes(b"approved test fixture")
    values = {
        "CVAT_EXTENSIONS": "sam31", "COMPOSE_PROJECT_NAME": "test",
        "SAM31_CHECKPOINT_HOST": str(checkpoint),
        "SAM31_CHECKPOINT_SHA256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "SAM31_REDIS_PASSWORD": "s"*48,
    }
    env = tmp_path / "deployment.env"; env.write_text("\n".join(f"{key}={value}" for key,value in values.items()))
    env.chmod(0o600)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    instance = cli.Manager(ROOT, env, state)
    instance.identity = lambda: {"project":"test", "network":"test_cvat", "daemon":"test-daemon"}
    return instance


def test_selection_adds_plugin_serverless_and_separate_volume(cli, manager, monkeypatch, tmp_path):
    assert manager.extensions == ("sam31",)
    assert manager.values["CVAT_CLIENT_PLUGINS"] == "plugins/sam31"
    assert manager.values["SAM31_REDIS_VOLUME"] == "test_sam31_redis_data"
    assert cli.FUNCTION_OWNERS["pth-sam31-tracker"] == "sam31"
    assert cli.FUNCTION_OWNERS["pth-sam31-interactor"] == "sam31"
    manager.root = tmp_path / "checkout"; manager.root.mkdir()
    for name in ("docker-compose.yml", cli.SERVERLESS_COMPOSE, cli.FINAL_COMPOSE, *cli.EXTENSION_COMPOSE["sam31"]):
        path = manager.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.touch()
    command = manager.compose("config")
    assert str(manager.root/cli.EXTENSION_COMPOSE["sam31"][0]) in command
    assert str(manager.root/cli.SERVERLESS_COMPOSE) in command


def test_parent_environment_cannot_override_sam31(cli, manager, monkeypatch):
    monkeypatch.setenv("SAM31_REDIS_PASSWORD", "untrusted")
    monkeypatch.setenv("SAM31_SURPRISE", "unexpected")
    instance = cli.Manager(ROOT, manager.env_file, manager.state)
    assert instance.env["SAM31_REDIS_PASSWORD"] == "s"*48
    assert "SAM31_SURPRISE" not in instance.env


def test_build_source_is_not_runtime_configuration(manager):
    manager.freeze({"services": {}}, {})
    saved = json.loads((manager.state / "active.json").read_text())
    assert manager.matches_configuration(saved, saved["model"])
    manager.values["SAM31_CHECKPOINT_HOST"] = "/different/model.pt"
    assert manager.matches_configuration(saved, saved["model"])
    for key,value in (("SAM31_GPU_DEVICE", "2"),
                      ("SAM31_CHECKPOINT_SHA256", "c"*64), ("SAM31_REDIS_PASSWORD", "other"*10),
                      ("SAM31_STATE_MAX_BYTES", "1024")):
        old = manager.values[key]; manager.values[key] = value
        assert not manager.matches_configuration(saved, saved["model"]), key
        manager.values[key] = old


def test_checkpoint_required_for_deploy_but_not_for_stop_settings(cli, manager):
    checkpoint = Path(manager.values["SAM31_CHECKPOINT_HOST"])
    assert cli.sam31_deploy.validate_settings(manager.values, ROOT) == checkpoint
    checkpoint.unlink()
    assert cli.settings(manager.env_file)["SAM31_CHECKPOINT_HOST"] == str(checkpoint)
    with pytest.raises(ValueError, match="existing checkpoint"):
        cli.sam31_deploy.validate_settings(manager.values, ROOT)


def test_checkpoint_checksum_checked_before_nuctl(cli, manager):
    Path(manager.values["SAM31_CHECKPOINT_HOST"]).write_bytes(b"changed")
    manager.nuctl_check = lambda model: pytest.fail("must reject checkpoint first")
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        manager.deploy({}, feature="sam31")


def test_helpers_and_template_share_image_fingerprint(cli, manager, tmp_path):
    root = tmp_path/"source"; root.mkdir()
    for relative in ("components/sam31", "serverless/pytorch/facebookresearch/sam2/nuclio"):
        shutil.copytree(ROOT/relative, root/relative, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    target = root/"components/extensions/manage.py"; target.parent.mkdir(parents=True); shutil.copyfile(ROOT/"components/extensions/manage.py", target)
    manager.root = root
    first = manager.function_images("1.16.3")["pth-sam31-tracker"]
    assert first == cli.sam31_deploy.function_image(
        "1.16.3", root, checkpoint_sha256=manager.values["SAM31_CHECKPOINT_SHA256"])
    assert set(manager.function_images("1.16.3")) == set(cli.sam31_deploy.FUNCTIONS)
    (root/cli.sam31_deploy.SHARED/"geometry.py").write_text("# Changed shared helper\n")
    assert first != manager.function_images("1.16.3")["pth-sam31-tracker"]
    assert first != cli.sam31_deploy.function_image(
        "1.17.0", ROOT, checkpoint_sha256=manager.values["SAM31_CHECKPOINT_SHA256"])


def test_no_build_deploy_uses_manager_lock_context_image_and_private_config(cli, manager):
    calls = []
    manager.nuctl_check = lambda model: "1.16.3"
    manager.functions = lambda: []
    manager.guard_function_ownership = lambda names: calls.append(("owner", names))
    manager.verify_functions = lambda names, **kwargs: calls.append(("verified", names, kwargs))
    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["nuctl", "get", "projects"]:
            return '{"project":{"meta":{"name":"cvat"}}}'
        if command[:2] == ["nuctl", "deploy"]:
            source = Path(command[command.index("--path")+1]); config = Path(command[command.index("--file")+1])
            definition = json.loads(config.read_text())
            assert config.stat().st_mode & 0o077 == 0
            assert source not in config.parents
            assert not any(manager.values["SAM31_REDIS_PASSWORD"] in p.read_text() for p in source.glob("*.py"))
            assert "SAM2_" not in (source/"protocol.py").read_text()
            assert "volumes" not in definition["spec"]
            assert not (source / Path(cli.sam31_deploy.CHECKPOINT).name).exists()
            assert "--run-image" in command and "--no-pull" in command
            assert command[command.index("--run-image")+1] == manager.function_images("1.16.3")[definition["metadata"]["name"]]
        return ""
    manager.run = run
    manager.deploy({}, feature="sam31", no_build=True)
    assert calls[0] == ("owner", list(cli.sam31_deploy.FUNCTIONS))
    assert calls[-1][0] == "verified"
    assert not list(manager.state.glob("sam31-*"))


def test_unchanged_function_is_reused_only_without_checkpoint_mount(cli, manager):
    image = manager.function_images("1.16.3")["pth-sam31-tracker"]
    current = {"Id":"running", "State":{"Running":True},
               "Config":{"Image":image,"Labels":{"nuclio.io/function-name":"pth-sam31-tracker"},
                         "Env":[f"{key}={value}" for key,value in manager.function_environment()["pth-sam31-tracker"].items()]},
               "HostConfig":{"PortBindings":{}},
               "Mounts":[]}
    manager.functions = lambda: [current]
    manager.guard_function_ownership = lambda names: None
    manager.nuctl_check = lambda model: "1.16.3"
    def run(command, **kwargs):
        assert command[0] == "docker" and "inspect" in command
        return "sha256:cached"
    manager.run = run
    manager.deploy({}, "tracker", feature="sam31")
    current["Mounts"].append({"Destination": cli.sam31_deploy.CHECKPOINT, "RW": False})
    with pytest.raises(cli.OperationError, match="shadowed"):
        manager.verify_functions(["pth-sam31-tracker"])


def test_disabled_sam31_is_not_restarted(cli, manager):
    manager.extensions = ()
    suspended = {"identity":manager.identity(), "containers":{"old":{"function":"pth-sam31-tracker", "restart":{"Name":"unless-stopped"}}}}
    cli.atomic_json(manager.state/"suspended.json", suspended)
    manager.functions = lambda: [{"Id":"old"}]
    manager.run = lambda *args, **kwargs: pytest.fail("disabled function must stay stopped")
    manager.restore_functions()
    assert json.loads((manager.state/"suspended.json").read_text()) == suspended


def test_up_starts_redis_before_restoring_and_deploys_sam31(manager):
    calls = []
    manager.prepare_registry = lambda: None
    manager.configuration = lambda: {"services":{}}
    manager.nuctl_check = lambda model: "1.16.3"
    manager.ensure_registry_available = lambda model: None
    manager.local_image_ids = lambda model: {}
    manager.ensure_network = lambda: None
    manager.ensure_volume = lambda feature: calls.append(("volume",feature))
    manager.compose = lambda *args: ["compose",*args]
    manager.restore_functions = lambda: calls.append(("restore",))
    manager.deploy = lambda model, **kwargs: calls.append(("deploy",kwargs))
    def run(args, **kwargs):
        calls.append(args)
        return "sha256:image" if args[:3] == ["docker","image","inspect"] else ""
    manager.run = run
    manager.up(no_build=True)
    wait = next(i for i,args in enumerate(calls) if "--wait" in args)
    restore = calls.index(("restore",))
    assert "sam31_redis" in calls[wait] and wait < restore
    assert ("volume","sam31") in calls
    assert ("deploy",{"feature":"sam31","no_build":True}) in calls
    saved = json.loads((manager.state/"active.json").read_text())
    assert saved["sam31_checkpoint"] is None
    assert any(name.startswith("cvat.sam31-") for name in saved["images"])


@pytest.mark.parametrize("mode", ["all", "image", "tracker"])
def test_sam31_modes_forward_to_shared_deployer(cli, manager, monkeypatch, mode):
    calls = []
    monkeypatch.setattr(cli.sam31_deploy, "deploy", lambda *args, **kwargs: calls.append(kwargs))
    manager.deploy({}, mode, feature="sam31", no_build=True)
    assert calls == [{"force": False, "no_build": True, "mode": mode}]
