# SPDX-License-Identifier: MIT
"""Common CLI contracts. External commands and registry collaborators are doubles."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


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
    spec = importlib.util.spec_from_file_location("review_cvatctl", ROOT / "components/extensions/manage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manager(cli, tmp_path):
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"checkpoint fixture, not real weights")
    values = {"CVAT_EXTENSIONS": "sam31", "COMPOSE_PROJECT_NAME": "test",
              "SAM31_CHECKPOINT_HOST": str(checkpoint), "SAM31_REDIS_PASSWORD": "s" * 48}
    env = tmp_path / "deployment.env"
    env.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    env.chmod(0o600)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    instance = cli.Manager(ROOT, env, state)
    instance.identity = lambda: {"project": "test", "network": "test_cvat", "daemon": "test"}
    return instance


def test_selection_and_compose(cli, manager, tmp_path):
    assert manager.extensions == ("sam31",)
    assert manager.values["CVAT_CLIENT_PLUGINS"] == "plugins/sam31"
    assert manager.values["SAM31_REDIS_VOLUME"] == "test_sam31_redis_data"
    assert all(cli.FUNCTION_OWNERS[name] == "sam31" for name in cli.sam31_deploy.FUNCTIONS)
    manager.root = tmp_path / "checkout"
    for name in ("docker-compose.yml", cli.SERVERLESS_COMPOSE, cli.FINAL_COMPOSE, *cli.EXTENSION_COMPOSE["sam31"]):
        path = manager.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    command = manager.compose("config")
    assert str(manager.root / cli.EXTENSION_COMPOSE["sam31"][0]) in command
    assert str(manager.root / cli.SERVERLESS_COMPOSE) in command


def test_shell_cannot_override_persistent_settings(cli, manager, monkeypatch):
    monkeypatch.setenv("SAM31_REDIS_PASSWORD", "untrusted")
    monkeypatch.setenv("SAM31_SURPRISE", "unexpected")
    instance = cli.Manager(ROOT, manager.env_file, manager.state)
    assert instance.env["SAM31_REDIS_PASSWORD"] == "s" * 48
    assert "SAM31_SURPRISE" not in instance.env


@pytest.mark.parametrize("key,value", [("SAM31_GPU_DEVICE", "2"),
    ("SAM31_CHECKPOINT_HOST", "/different/model.pt"), ("SAM31_REDIS_PASSWORD", "other" * 10),
    ("SAM31_STATE_MAX_BYTES", "1024")])
def test_model_source_and_runtime_changes_require_stop(manager, key, value):
    manager.freeze({"services": {}}, {})
    saved = json.loads((manager.state / "active.json").read_text())
    assert manager.matches_configuration(saved, saved["model"])
    manager.values[key] = value
    assert not manager.matches_configuration(saved, saved["model"])


def test_source_required_only_for_build(cli, manager):
    checkpoint = Path(manager.values["SAM31_CHECKPOINT_HOST"])
    assert cli.sam31_deploy.validate_settings(manager.values, ROOT) == checkpoint
    checkpoint.unlink()
    assert cli.settings(manager.env_file)["SAM31_CHECKPOINT_HOST"] == str(checkpoint)
    assert cli.sam31_deploy.validate_settings(manager.values, ROOT, verify_file=False) == checkpoint
    with pytest.raises(ValueError, match="existing checkpoint"):
        cli.sam31_deploy.validate_settings(manager.values, ROOT)


@pytest.mark.parametrize("saved", [False, True])
def test_status_requires_no_checkpoint_metadata(cli, manager, monkeypatch, saved):
    manager.env_file.write_text("CVAT_EXTENSIONS=sam31\nSAM31_REDIS_PASSWORD=" + "s" * 48)
    if saved:
        cli.atomic_json(manager.state / "active.json", {"identity": {}})
    calls = []
    monkeypatch.setattr(cli.Manager, "assert_identity", lambda self, record: None)
    monkeypatch.setattr(cli.Manager, "configuration", lambda self: {})
    monkeypatch.setattr(cli.Manager, "compose", lambda self, *args, **kwargs: ["compose", *args])
    monkeypatch.setattr(cli.Manager, "run", lambda self, command, **kwargs: calls.append(command))
    monkeypatch.setattr(cli.Manager, "functions", lambda self: [])
    monkeypatch.setattr(sys, "argv", ["cvatctl", "--env-file", str(manager.env_file),
                                     "--state-dir", str(manager.state), "status"])
    cli.main()
    assert calls == [["compose", "ps", "--all"]]


def test_missing_checkpoint_fails_before_commands(cli, manager):
    Path(manager.values["SAM31_CHECKPOINT_HOST"]).unlink()
    manager.nuctl_check = lambda model: pytest.fail("must reject missing checkpoint first")
    with pytest.raises(ValueError, match="existing checkpoint"):
        manager.deploy({}, feature="sam31")


def test_source_fingerprint(cli, manager, tmp_path):
    root = tmp_path / "source"
    for relative in ("components/sam31", cli.NUCLIO_SOURCE):
        shutil.copytree(ROOT / relative, root / relative, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    target = root / "components/extensions/manage.py"
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "components/extensions/manage.py", target)
    manager.root = root
    first = manager.function_images("1.16.3")["pth-sam31-tracker"]
    assert first == cli.sam31_deploy.function_image("1.16.3", root,
        checkpoint_source=manager.values["SAM31_CHECKPOINT_HOST"])
    assert set(manager.function_images("1.16.3")) == set(cli.sam31_deploy.FUNCTIONS)
    (root / cli.NUCLIO_SOURCE / "geometry.py").write_text("# changed\n")
    assert first != manager.function_images("1.16.3")["pth-sam31-tracker"]


def test_no_build_uses_private_config_without_source_weights(cli, manager):
    Path(manager.values["SAM31_CHECKPOINT_HOST"]).unlink()
    calls = []
    manager.nuctl_check = lambda model: "1.16.3"
    manager.functions = lambda: []
    manager.guard_function_ownership = lambda names: calls.append(("owner", names))
    manager.verify_functions = lambda names, **kwargs: calls.append(("verified", names))
    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["nuctl", "get", "projects"]:
            return '{"project":{"meta":{"name":"cvat"}}}'
        if command[:2] == ["nuctl", "deploy"]:
            source = Path(command[command.index("--path") + 1])
            config = Path(command[command.index("--file") + 1])
            definition = json.loads(config.read_text())
            assert config.stat().st_mode & 0o077 == 0 and source not in config.parents
            assert "SAM2_" not in (source / "protocol.py").read_text()
            assert "volumes" not in definition["spec"]
            assert not (source / Path(cli.sam31_deploy.CHECKPOINT).name).exists()
            assert "--run-image" in command and "--no-pull" in command
            assert command[command.index("--run-image") + 1] == manager.function_images("1.16.3")[definition["metadata"]["name"]]
        return ""
    manager.run = run
    manager.deploy({}, feature="sam31", no_build=True)
    assert calls[0] == ("owner", list(cli.sam31_deploy.FUNCTIONS))
    assert calls[-1][0] == "verified" and not list(manager.state.glob("sam31-*"))


def test_unchanged_no_build_reuses_only_unshadowed_checkpoint(cli, manager):
    image = manager.function_images("1.16.3")["pth-sam31-tracker"]
    current = {"Id": "running", "State": {"Running": True}, "Mounts": [],
        "Config": {"Image": image, "Labels": {"nuclio.io/function-name": "pth-sam31-tracker"},
                   "Env": [f"{key}={value}" for key, value in manager.function_environment()["pth-sam31-tracker"].items()]},
        "HostConfig": {"PortBindings": {}}}
    manager.functions = lambda: [current]
    manager.guard_function_ownership = lambda names: None
    manager.nuctl_check = lambda model: "1.16.3"
    def run(command, **kwargs):
        assert command[0] == "docker" and "inspect" in command
        return "sha256:cached"
    manager.run = run
    manager.deploy({}, "tracker", feature="sam31", no_build=True)
    current["Mounts"].append({"Destination": cli.sam31_deploy.CHECKPOINT, "RW": False})
    with pytest.raises(cli.OperationError, match="shadowed"):
        manager.verify_functions(["pth-sam31-tracker"])


def test_disabled_sam31_is_not_restarted(cli, manager):
    manager.extensions = ()
    suspended = {"identity": manager.identity(), "containers": {"old": {
        "function": "pth-sam31-tracker", "restart": {"Name": "unless-stopped"}}}}
    cli.atomic_json(manager.state / "suspended.json", suspended)
    manager.functions = lambda: [{"Id": "old"}]
    manager.run = lambda *args, **kwargs: pytest.fail("disabled function must stay stopped")
    manager.restore_functions()
    assert json.loads((manager.state / "suspended.json").read_text()) == suspended


def test_redis_starts_before_functions(manager):
    calls = []
    manager.prepare_registry = lambda: None
    manager.configuration = lambda: {"services": {}}
    manager.nuctl_check = lambda model: "1.16.3"
    manager.ensure_registry_available = lambda model: None
    manager.local_image_ids = lambda model: {}
    manager.ensure_network = lambda: None
    manager.ensure_volume = lambda feature: calls.append(("volume", feature))
    manager.compose = lambda *args: ["compose", *args]
    manager.restore_functions = lambda: calls.append(("restore",))
    manager.deploy = lambda model, **kwargs: calls.append(("deploy", kwargs))
    def run(args, **kwargs):
        calls.append(args)
        return "sha256:image" if args[:3] == ["docker", "image", "inspect"] else ""
    manager.run = run
    manager.up(no_build=True)
    wait = next(i for i, args in enumerate(calls) if "--wait" in args)
    assert "sam31_redis" in calls[wait] and wait < calls.index(("restore",))
    assert ("deploy", {"feature": "sam31", "no_build": True}) in calls
    saved = json.loads((manager.state / "active.json").read_text())
    assert saved["model_sources"] == manager.model_sources()
    assert any(name.startswith("cvat.sam31-") for name in saved["images"])


@pytest.mark.parametrize("mode", ["all", "image", "tracker"])
def test_sam31_modes(cli, manager, monkeypatch, mode):
    calls = []
    monkeypatch.setattr(cli.sam31_deploy, "deploy", lambda *args, **kwargs: calls.append(kwargs))
    manager.deploy({}, mode, feature="sam31", no_build=True)
    assert calls == [{"force": False, "no_build": True, "mode": mode}]


@pytest.mark.parametrize("name,suffix", [
    ("sam2.1_hiera_tiny", "t"), ("sam2.1_hiera_small", "s"),
    ("sam2.1_hiera_base_plus", "b+"), ("sam2.1_hiera_large", "l")])
@pytest.mark.parametrize("mode", ["all", "image", "tracker"])
def test_sam2_selection_reaches_both_build_recipe_and_runtime(cli, manager, name, suffix, mode):
    manager.extensions = ("sam2",)
    manager.values.update(SAM2_MODEL=name, SAM2_REDIS_PASSWORD="s" * 48)
    manager.nuctl_check = lambda model: "1.16.3"
    manager.functions = lambda: []
    manager.guard_function_ownership = lambda names: None
    manager.verify_functions = lambda *args, **kwargs: None
    definitions = []
    def run(command, **kwargs):
        if command[:3] == ["nuctl", "get", "projects"]:
            return '{"meta":{"name":"cvat"}}'
        if command[:2] == ["nuctl", "deploy"]:
            definition = yaml.safe_load(Path(command[command.index("--file") + 1]).read_text())
            definitions.append(definition)
            env = {item["name"]: item["value"] for item in definition["spec"]["env"]}
            assert env["SAM2_CONFIG"] == f"configs/sam2.1/sam2.1_hiera_{suffix}.yaml"
            assert env["SAM2_CHECKPOINT"] == f"/opt/nuclio/checkpoints/{name}.pt"
            recipe = json.dumps(definition["spec"]["build"])
            assert f"092824/{name}.pt" in recipe and "sha256sum" not in recipe
            if name != "sam2.1_hiera_small":
                assert "sam2.1_hiera_small" not in recipe
        return ""
    manager.run = run
    manager.deploy({}, mode, feature="sam2")
    assert len(definitions) == (2 if mode == "all" else 1)


def test_sam2_default_and_unknown_selection(cli):
    assert cli.sam2_models.environment({})["SAM2_MODEL"] == "sam2.1_hiera_small"
    assert cli.sam2_models.environment({"SAM2_MODEL": ""})["SAM2_MODEL"] == "sam2.1_hiera_small"
    with pytest.raises(ValueError, match="SAM2_MODEL"):
        cli.sam2_models.environment({"SAM2_MODEL": "not-a-model"})


def test_sam2_selection_changes_images_and_saved_configuration(cli, manager):
    manager.extensions = ("sam2",)
    manager.values["SAM2_REDIS_PASSWORD"] = "s" * 48
    first = manager.function_images("1.16.3")
    manager.freeze({"services": {}}, {})
    saved = json.loads((manager.state / "active.json").read_text())
    manager.values["SAM2_MODEL"] = "sam2.1_hiera_large"
    assert manager.function_images("1.16.3") != first
    assert not manager.matches_configuration(saved, saved["model"])


def test_sam2_custom_checkpoint_copied_once_without_download(cli, manager):
    manager.extensions = ("sam2",)
    manager.values.update(SAM2_MODEL="sam2.1_hiera_large", SAM2_REDIS_PASSWORD="s" * 48,
        SAM2_CHECKPOINT_HOST=manager.values["SAM31_CHECKPOINT_HOST"])
    checkpoint = Path(manager.values["SAM2_CHECKPOINT_HOST"])
    checkpoint.write_bytes(b"updated at same path")
    manager.nuctl_check = lambda model: "1.16.3"
    images = manager.function_images("1.16.3")
    manager.functions = lambda: [{"Id": name, "State": {"Running": True}, "Config": {
        "Image": image, "Labels": {"nuclio.io/function-name": name},
        "Env": [f"{k}={v}" for k, v in manager.function_environment()[name].items()]}}
        for name, image in images.items()]
    manager.guard_function_ownership = lambda names: None
    manager.verify_functions = lambda *args, **kwargs: None
    paths = []
    def run(command, **kwargs):
        if command[:3] == ["nuctl", "get", "projects"]:
            return '{"meta":{"name":"cvat"}}'
        if command[:2] == ["nuctl", "deploy"]:
            path = Path(command[command.index("--path") + 1])
            paths.append(path)
            assert (path / "checkpoints/sam2.1_hiera_large.pt").read_bytes() == b"updated at same path"
            definition = yaml.safe_load(Path(command[command.index("--file") + 1]).read_text())
            assert "092824" not in json.dumps(definition["spec"]["build"])
            assert "--run-image" not in command
        return "sha256:fixture" if command[0] == "docker" else ""
    manager.run = run
    manager.deploy({}, feature="sam2")
    assert len(paths) == 2 and paths[0] == paths[1] and not paths[0].exists()


@pytest.mark.parametrize("feature", ["sam2", "sam31"])
def test_source_path_validation_preserved(cli, manager, feature):
    values = dict(manager.values)
    values[feature.upper() + "_CHECKPOINT_HOST"] = "relative/model.pt"
    validator = cli.sam2_models.checkpoint_source if feature == "sam2" else cli.sam31_deploy.validate_settings
    with pytest.raises(ValueError, match="absolute path"):
        validator(values, ROOT, verify_file=False)


def test_sam2_no_build_does_not_require_original_weights(cli, manager, tmp_path):
    manager.extensions = ("sam2",)
    manager.values.update(SAM2_MODEL="sam2.1_hiera_small", SAM2_REDIS_PASSWORD="s" * 48,
                          SAM2_CHECKPOINT_HOST=str(tmp_path / "deleted.pt"))
    manager.nuctl_check = lambda model: "1.16.3"
    manager.functions = lambda: []
    manager.guard_function_ownership = lambda names: None
    manager.verify_functions = lambda names, **kwargs: None
    calls = []
    def run(command, **kwargs):
        if command[:3] == ["nuctl", "get", "projects"]:
            return '{"project":{"meta":{"name":"cvat"}}}'
        if command[:2] == ["nuctl", "deploy"]:
            calls.append(command)
            assert "--run-image" in command and "--no-pull" in command
            assert Path(command[command.index("--path") + 1]) == manager.root / cli.NUCLIO_SOURCE
        return ""
    manager.run = run
    manager.deploy({}, feature="sam2", no_build=True)
    assert len(calls) == 2
