# SPDX-License-Identifier: MIT
"""Common manager contracts. Commands and unrelated registry setup are doubled."""
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from components.extensions import functions as f
from components.extensions.tests.test_functions import source_root


@pytest.fixture
def cli(monkeypatch):
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
    spec = importlib.util.spec_from_file_location("review_cvatctl", ROOT/"components/extensions/manage.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.fixture
def manager(cli, source_root, tmp_path):
    checkpoint = tmp_path / "sam31.pt"
    checkpoint.write_bytes(b"local weights")
    env = tmp_path/"deployment.env"
    env.write_text("CVAT_EXTENSIONS=sam2,sam31,ultrasam\nCOMPOSE_PROJECT_NAME=test\n"
                   "SAM2_REDIS_PASSWORD="+"a"*48+"\nSAM31_REDIS_PASSWORD="+"b"*48+"\n"
                   "HF_TOKEN=hf_private_test_token\nSAM31_CHECKPOINT_HOST=" + str(checkpoint) + "\n")
    env.chmod(0o600)
    state = tmp_path/"state"; state.mkdir(mode=0o700)
    instance = cli.Manager(source_root, env, state)
    instance.identity = lambda: {"project": "test", "network": "test_cvat", "daemon": "test"}
    return instance


def test_default_names_and_separate_redis(cli, manager):
    assert manager.values["SAM2_MODEL"] == "sam2.1_hiera_small"
    assert manager.values["SAM31_CHECKPOINT_HOST"] == str(manager.env_file.parent / "sam31.pt")
    assert manager.values["SAM2_REDIS_VOLUME"] != manager.values["SAM31_REDIS_VOLUME"]
    assert manager.values["SAM2_REDIS_PASSWORD"] != manager.values["SAM31_REDIS_PASSWORD"]
    assert cli.OWNERS["pth-sam31-interactor"] == cli.OWNERS["pth-sam31-tracker"] == "sam31"
    assert manager.values["CVAT_CLIENT_PLUGINS"] == "plugins/sam2:plugins/sam31"


def test_shared_redis_volume_rejected(cli, manager):
    with manager.env_file.open("a") as stream: stream.write("SAM2_REDIS_VOLUME=same\nSAM31_REDIS_VOLUME=same\n")
    with pytest.raises(cli.OperationError, match="separate"):
        cli.settings(manager.env_file, manager.root)


def test_hf_credentials_never_reach_subprocesses(cli, manager, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "inherited-secret")
    monkeypatch.setenv("SAM31_CHECKPOINT_HOST", "/wrong/path")
    instance = cli.Manager(manager.root, manager.env_file, manager.state)
    assert instance.values["HF_TOKEN"] == "hf_private_test_token"
    assert "HF_TOKEN" not in instance.env
    assert instance.env["SAM31_CHECKPOINT_HOST"] == manager.values["SAM31_CHECKPOINT_HOST"]
    assert instance.redact("hf_private_test_token") == "<redacted>"
    assert "HF_TOKEN" not in repr(instance.function_environment())


@pytest.mark.parametrize("feature", ["sam2", "sam31", "ultrasam"])
@pytest.mark.parametrize("mode", ["image", "tracker", "all"])
def test_manager_delegates_all_families_to_one_deployer(cli, manager, monkeypatch, feature, mode):
    if feature == "ultrasam" and mode == "tracker":
        with pytest.raises(ValueError): manager.deploy({}, mode, feature=feature)
        return
    calls = []
    monkeypatch.setattr(f, "deploy", lambda *args, **kwargs: calls.append(kwargs))
    manager.deploy({}, mode, feature=feature, no_build=True)
    assert calls == [{"feature": feature, "mode": mode, "force": False, "no_build": True}]


def test_model_change_invalidates_config_but_token_change_does_not(manager):
    manager.freeze({"services": {}}, {})
    saved = json.loads((manager.state/"active.json").read_text())
    assert manager.matches_configuration(saved, saved["model"])
    manager.values["HF_TOKEN"] = "another token"
    assert manager.matches_configuration(saved, saved["model"])
    for key,value in (("SAM2_MODEL", "sam2.1_hiera_large"), ("SAM31_CHECKPOINT_HOST", manager.values["SAM31_CHECKPOINT_HOST"] + ".other"),
                      ("SAM31_GPU_DEVICE", "2"), ("SAM31_REDIS_PASSWORD", "c"*48)):
        old = manager.values[key]; manager.values[key] = value
        assert not manager.matches_configuration(saved, saved["model"]), key
        manager.values[key] = old
    assert saved["model_sources"]["sam31"] == manager.values["SAM31_CHECKPOINT_HOST"]


@pytest.mark.parametrize("restored", [set(), {"sam2"}])
def test_up_keeps_all_functions_and_starts_both_redis_before_restore(manager, restored):
    calls = []
    manager.prepare_registry = lambda: None
    manager.configuration = lambda: {"services": {}}
    manager.nuctl_check = lambda model: "1.16.3"
    manager.ensure_registry_available = lambda model: None
    manager.local_image_ids = lambda model: {}
    manager.ensure_network = lambda: None
    manager.ensure_volume = lambda feature: calls.append(["volume", feature])
    manager.compose = lambda *args: ["compose", *args]
    manager.restore_functions = lambda: calls.append(["restore"]) or restored
    manager.deploy = lambda model, **kwargs: calls.append(["deploy", kwargs])
    def run(command, **kwargs):
        calls.append(command)
        return "sha256:image" if command[:3] == ["docker", "image", "inspect"] else ""
    manager.run = run
    manager.up(no_build=True)
    waited = next(index for index,call in enumerate(calls) if "--wait" in call)
    assert "sam2_redis" in calls[waited] and "sam31_redis" in calls[waited]
    assert waited < calls.index(["restore"])
    assert {call[1]["feature"] for call in calls if call[0] == "deploy"} == {"sam2", "sam31", "ultrasam"}
    for call in calls:
        if call[0] == "deploy":
            assert call[1]["force"] == (call[1]["feature"] in restored)
            assert call[1]["no_build"] is True
    saved = json.loads((manager.state/"active.json").read_text())
    assert set(saved["function_environment"]) == set(f.OWNERS)


def test_disabled_family_not_restored(cli, manager):
    manager.extensions = ("sam2",)
    saved = {"identity": manager.identity(), "containers": {"old": {"function": "pth-sam31-tracker", "restart": {"Name": "unless-stopped"}}}}
    cli.atomic_json(manager.state/"suspended.json", saved)
    manager.functions = lambda: [{"Id": "old"}]
    manager.run = lambda *a, **k: pytest.fail("disabled function must remain stopped")
    assert manager.restore_functions() == set()
    assert json.loads((manager.state/"suspended.json").read_text()) == saved


def test_verify_uses_recorded_environment(cli, manager):
    name = "pth-sam31-tracker"
    environment = f.runtime_environment(name, manager.values)
    manager.functions = lambda: [{"State": {"Running": True}, "Config": {"Labels": {"nuclio.io/function-name": name},
        "Env": [f"{key}={value}" for key,value in environment.items()]}, "HostConfig": {}, "Mounts": []}]
    manager.values.pop("SAM31_REDIS_PASSWORD")
    manager.verify_functions([name], environments={name: environment})


def test_status_never_requires_weights_or_download_credentials(cli, manager, monkeypatch):
    manager.env_file.write_text("CVAT_EXTENSIONS=sam31\nSAM31_REDIS_PASSWORD="+"b"*48+"\n")
    calls = []
    monkeypatch.setattr(cli.Manager, "configuration", lambda self: {})
    monkeypatch.setattr(cli.Manager, "compose", lambda self,*a,**k: ["compose",*a])
    monkeypatch.setattr(cli.Manager, "run", lambda self,cmd,**k: calls.append(cmd))
    monkeypatch.setattr(cli.Manager, "functions", lambda self: [])
    monkeypatch.setattr(sys, "argv", ["cvatctl", "--env-file", str(manager.env_file), "--state-dir", str(manager.state), "status"])
    cli.main()
    assert calls == [["compose", "ps", "--all"]]


def test_sam31_missing_weights_fail_before_startup(manager):
    manager.prepare_registry = lambda: None
    manager.configuration = lambda: {"services": {}}
    manager.values["SAM31_CHECKPOINT_HOST"] = ""
    manager.run = lambda *a, **k: pytest.fail("Must reject missing weights before changing containers")
    with pytest.raises(ValueError, match="SAM31_CHECKPOINT_HOST"):
        manager.up()
