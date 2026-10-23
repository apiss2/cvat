# SPDX-License-Identifier: MIT
"""Client-extension lifecycle contracts; Docker and registry setup are test doubles."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
CLIENTS = {
    "frame_navigation_filter": "plugins/frame-navigation-filter",
    "selective_ai": "plugins/selective-ai",
    "filtered_export": "plugins/filtered-export",
    "task_context": "plugins/task-context",
}


@pytest.fixture
def cli(monkeypatch):
    # Match the existing manager tests; these helpers must not be used here.
    def unexpected(*args, **kwargs):
        pytest.fail("Client-only extensions must not prepare model-registry storage")

    registry = ModuleType("registryctl")
    registry.connection_values = registry.public_address = registry.purge_deleted = unexpected
    storage = ModuleType("storage")
    storage.default_home = storage.prepare_storage = storage.storage_paths = unexpected
    monkeypatch.setitem(sys.modules, "registryctl", registry)
    monkeypatch.setitem(sys.modules, "storage", storage)
    spec = importlib.util.spec_from_file_location(
        "client_plugin_cvatctl", ROOT / "components/extensions/manage.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def make_manager(cli, tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    # Only existence is relevant here. Compose itself is not run in these tests.
    for relative in (*cli.COMPOSE_FILES, "Dockerfile", "Dockerfile.ui", ".dockerignore"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for plugin in cli.EXTENSION_PLUGINS.values():
        path = root / f"cvat-ui/{plugin}/src/ts/index.tsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for relative in (
        "components/task_context/apps.py",
        "components/task_context/models.py",
        "components/task_context/urls.py",
        "components/task_context/migrations/0001_initial.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env = tmp_path / "deployment.env"

    def create(selected="", extra=""):
        env.write_text(f"CVAT_EXTENSIONS={selected}\nCOMPOSE_PROJECT_NAME=test\n{extra}")
        env.chmod(0o600)
        instance = cli.Manager(root, env, state)
        instance.identity = lambda: {
            "project": "test", "network": "test_cvat", "namespace": "nuclio", "daemon": "test"
        }
        return instance

    return create


def model_for(cli, manager):
    env = manager.env
    services = {
        name: {"image": env["CVAT_EXT_SERVER_IMAGE"], "pull_policy": "never"}
        for name in ("cvat_server", "cvat_worker_import", "cvat_worker_export", "cvat_worker_annotation")
    }
    services["cvat_ui"] = {
        "image": env["CVAT_EXT_UI_IMAGE"], "pull_policy": "never",
        "build": {"dockerfile": "Dockerfile.ui", "args": {"CLIENT_PLUGINS": env["CVAT_CLIENT_PLUGINS"]}},
    }
    if set(manager.extensions) & cli.SERVERLESS_EXTENSIONS:
        services["nuclio"] = {"image": "nuclio:test"}
        for name in ("cvat_server", "cvat_worker_annotation"):
            services[name]["environment"] = {
                "CVAT_NUCLIO_INVOKE_METHOD": "dashboard",
                "CVAT_NUCLIO_FUNCTION_NAMESPACE": "nuclio",
            }
    services["cvat_server"].setdefault("environment", {})["CVAT_TASK_CONTEXT_ENABLED"] = (
        env["CVAT_TASK_CONTEXT_ENABLED"]
    )
    return {"services": services, "networks": {"cvat": {"external": True, "name": "test_cvat"}}}


def configure(cli, manager, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "source_tag", lambda root: "fixed-source")
    monkeypatch.setattr(manager, "version_check", lambda: None)

    def run(command, **kwargs):
        calls.append(command)
        assert command[-3:] == ["config", "--format", "json"]
        return json.dumps(model_for(cli, manager))

    monkeypatch.setattr(manager, "run", run)
    return manager.configuration(), calls


@pytest.mark.parametrize("selected,expected", [
    ("", []),
    ("frame_navigation_filter", [CLIENTS["frame_navigation_filter"]]),
    ("selective_ai", [CLIENTS["selective_ai"]]),
    ("filtered_export", [CLIENTS["filtered_export"]]),
    ("task_context", [CLIENTS["task_context"]]),
    ("task_context,filtered_export", [CLIENTS["filtered_export"], CLIENTS["task_context"]]),
    ("selective_ai,frame_navigation_filter", [CLIENTS["frame_navigation_filter"], CLIENTS["selective_ai"]]),
    ("filtered_export,frame_navigation_filter", [CLIENTS["frame_navigation_filter"], CLIENTS["filtered_export"]]),
    ("filtered_export,selective_ai", [CLIENTS["selective_ai"], CLIENTS["filtered_export"]]),
    (",".join(CLIENTS), list(CLIENTS.values())),
])
def test_named_selection(cli, make_manager, selected, expected):
    manager = make_manager(selected)
    assert manager.values["CVAT_CLIENT_PLUGINS"].split(":") == (expected or [""])
    assert manager.extensions == tuple(name for name in CLIENTS if CLIENTS[name] in expected)
    assert manager.function_environment() == {}
    assert manager.model_sources() == {}
    for name in CLIENTS:
        assert cli.EXTENSION_COMPOSE[name] == ()
        assert name not in cli.FUNCTIONS


@pytest.mark.parametrize("selected", ["", *CLIENTS, ",".join(CLIENTS)])
def test_selection_overrides_stale_paths_and_preserves_custom_plugins(make_manager, selected):
    paths = ":".join(["custom/extra", *CLIENTS.values(), "plugins/sam2", "custom/extra"])
    manager = make_manager(selected, f"CVAT_CLIENT_PLUGINS={paths}\n")
    expected = ["custom/extra", *(path for name, path in CLIENTS.items() if name in selected.split(","))]
    assert manager.values["CVAT_CLIENT_PLUGINS"].split(":") == expected


@pytest.mark.parametrize("selected,expected", [("", "0"), ("task_context", "1")])
@pytest.mark.parametrize("stale", ["0", "1"])
def test_task_context_api_setting_follows_selection(make_manager, monkeypatch, selected, expected, stale):
    monkeypatch.setenv("CVAT_TASK_CONTEXT_ENABLED", stale)
    manager = make_manager(selected, f"CVAT_TASK_CONTEXT_ENABLED={stale}\n")
    assert manager.values["CVAT_TASK_CONTEXT_ENABLED"] == expected
    assert manager.env["CVAT_TASK_CONTEXT_ENABLED"] == expected


@pytest.mark.parametrize("selected", ["unknown", "selective-ai", "selective_ai,selective_ai"])
def test_invalid_selection_reports_all_choices(cli, make_manager, selected):
    with pytest.raises(cli.OperationError) as error:
        make_manager(selected)
    for name in cli.EXTENSION_COMPOSE:
        assert name in str(error.value)


def test_all_existing_extensions_can_be_combined(cli, make_manager):
    selected = ",".join(cli.EXTENSION_COMPOSE)
    manager = make_manager(selected,
        "SAM2_REDIS_PASSWORD=" + "a" * 48 + "\nSAM31_REDIS_PASSWORD=" + "b" * 48 + "\n"
        "CVAT_MODEL_REGISTRY_URL=https://registry.example.test\n"
        "CVAT_MODEL_GATEWAY_SECRETS_DIR=/test/secrets\n")
    assert manager.extensions == tuple(cli.EXTENSION_COMPOSE)
    assert manager.values["CVAT_CLIENT_PLUGINS"].split(":") == list(cli.EXTENSION_PLUGINS.values())
    assert manager.values["CVAT_AI_GATEWAY_HOST"] == "model-gateway"
    assert manager.values["CVAT_MODEL_REGISTRY_ENABLED"] == "1"
    assert set(manager.function_environment()) == set(cli.OWNERS)


@pytest.mark.parametrize("selected,serverless", [
    ("frame_navigation_filter", False), ("selective_ai", True),
    ("frame_navigation_filter,selective_ai", True),
    ("filtered_export", False),
    ("frame_navigation_filter,filtered_export", False),
    ("task_context", False),
    ("task_context,filtered_export", False),
    (",".join(CLIENTS), True),
])
def test_compose_uses_only_required_existing_overlays(cli, make_manager, selected, serverless):
    manager = make_manager(selected)
    command = manager.compose("config")
    files = [command[index + 1] for index, value in enumerate(command) if value == "-f"]
    expected = ["docker-compose.yml"]
    if serverless:
        expected.append(cli.SERVERLESS_COMPOSE)
    expected.append(cli.FINAL_COMPOSE)
    assert files == [str(manager.root / path) for path in expected]
    assert "model_registry" not in manager.extensions


@pytest.mark.parametrize("feature", CLIENTS)
def test_configuration_rejects_missing_plugin_source(cli, make_manager, monkeypatch, feature):
    manager = make_manager(feature)
    path = manager.root / f"cvat-ui/{CLIENTS[feature]}/src/ts/index.tsx"
    path.unlink()
    monkeypatch.setattr(manager, "version_check", lambda: None)
    monkeypatch.setattr(manager, "run", lambda *a, **k: pytest.fail("Must reject before Compose config"))
    with pytest.raises(cli.OperationError, match="Missing source"):
        manager.configuration()


@pytest.mark.parametrize("relative", [
    "components/task_context/apps.py",
    "components/task_context/models.py",
    "components/task_context/urls.py",
    "components/task_context/migrations/0001_initial.py",
])
def test_configuration_rejects_missing_task_context_backend(make_manager, monkeypatch, cli, relative):
    manager = make_manager("task_context")
    (manager.root / relative).unlink()
    monkeypatch.setattr(manager, "version_check", lambda: None)
    monkeypatch.setattr(manager, "run", lambda *a, **k: pytest.fail("Must reject before Compose config"))
    with pytest.raises(cli.OperationError, match="Missing source"):
        manager.configuration()


@pytest.mark.parametrize("feature", CLIENTS)
def test_compose_validation_rejects_lost_plugin(cli, make_manager, monkeypatch, feature):
    manager = make_manager(feature)
    model, _ = configure(cli, manager, monkeypatch)
    model["services"]["cvat_ui"]["build"]["args"]["CLIENT_PLUGINS"] = ""
    with pytest.raises(cli.OperationError, match="CLIENT_PLUGINS argument lost"):
        cli.validate_model(model, manager.env)


@pytest.mark.parametrize("selected,actual", [("task_context", "0"), ("task_context", None), ("", "1")])
def test_compose_validation_rejects_wrong_task_context_api_state(cli, make_manager, monkeypatch, selected, actual):
    manager = make_manager(selected)
    model, _ = configure(cli, manager, monkeypatch)
    environment = model["services"]["cvat_server"]["environment"]
    if actual is None:
        environment.pop("CVAT_TASK_CONTEXT_ENABLED")
    else:
        environment["CVAT_TASK_CONTEXT_ENABLED"] = actual
    with pytest.raises(cli.OperationError, match="task context API setting"):
        cli.validate_model(model, manager.env)


def test_selective_ai_requires_serverless_service(cli, make_manager, monkeypatch):
    manager = make_manager("selective_ai")
    model, _ = configure(cli, manager, monkeypatch)
    del model["services"]["nuclio"]
    with pytest.raises(cli.OperationError, match="Missing required services"):
        cli.validate_model(model, manager.env)


def test_selection_changes_only_ui_image_tag_for_same_source(cli, make_manager, monkeypatch):
    images = []
    for selected in ("", *CLIENTS, ",".join(CLIENTS)):
        manager = make_manager(selected)
        model, _ = configure(cli, manager, monkeypatch)
        images.append((model["services"]["cvat_server"]["image"], model["services"]["cvat_ui"]["image"]))
    assert len({backend for backend, _ in images}) == 1
    assert len({ui for _, ui in images}) == len(CLIENTS) + 2


@pytest.mark.parametrize("command", ["config", "doctor"])
@pytest.mark.parametrize("selected", ["", ",".join(CLIENTS)])
def test_cli_reports_resolved_client_plugins(cli, make_manager, monkeypatch, capsys, command, selected):
    manager = make_manager(selected)
    model, _ = configure(cli, manager, monkeypatch)
    monkeypatch.setattr(cli, "Manager", lambda *args: manager)
    monkeypatch.setattr(manager, "configuration", lambda: model)
    monkeypatch.setattr(sys, "argv", [
        "cvatctl", "--env-file", str(manager.env_file), "--state-dir", str(manager.state), command,
    ])
    cli.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["extensions"] == list(manager.extensions)
    assert summary["client_plugins"] == (list(CLIENTS.values()) if selected else [])
    assert summary["task_context_enabled"] == ("task_context" in manager.extensions)


@pytest.mark.parametrize("selected", CLIENTS)
def test_build_and_up_share_ui_lifecycle_without_gpu(cli, make_manager, monkeypatch, selected):
    manager = make_manager(selected)
    model, _ = configure(cli, manager, monkeypatch)
    calls = []
    monkeypatch.setattr(manager, "configuration", lambda: model)
    monkeypatch.setattr(manager, "ensure_network", lambda: None)
    monkeypatch.setattr(manager, "restore_functions", lambda: set())
    monkeypatch.setattr(manager, "nuctl_check", lambda *a: pytest.fail("No GPU function to deploy"))
    monkeypatch.setattr(manager, "ensure_volume", lambda *a: pytest.fail("No tracker Redis needed"))
    monkeypatch.setattr(manager, "deploy", lambda *a, **k: pytest.fail("No GPU function to deploy"))

    def run(command, **kwargs):
        calls.append(command)
        return "sha256:fixture" if command[:3] == ["docker", "image", "inspect"] else ""

    monkeypatch.setattr(manager, "run", run)
    manager.build()
    assert calls == [manager.compose("build", "cvat_server", "cvat_ui")]
    calls.clear()
    manager.up(no_build=True)
    assert manager.compose("up", "-d", "--no-build") in calls
    assert not any("build" in command for command in calls)
    saved = cli.read_json(manager.state / "active.json")
    assert saved["extensions"] == [selected]
    assert saved["model"]["services"]["cvat_ui"]["build"]["args"]["CLIENT_PLUGINS"] == CLIENTS[selected]
    assert saved["model"]["services"]["cvat_server"]["environment"]["CVAT_TASK_CONTEXT_ENABLED"] == (
        "1" if selected == "task_context" else "0"
    )
    assert manager.matches_configuration(saved, model)
    changed = copy.deepcopy(model)
    changed["services"]["cvat_ui"]["build"]["args"]["CLIENT_PLUGINS"] = ""
    assert not manager.matches_configuration(saved, changed)


def test_down_uses_recorded_ui_even_after_selection_changes(cli, make_manager, monkeypatch):
    previous = make_manager(",".join(CLIENTS))
    model, _ = configure(cli, previous, monkeypatch)
    previous.freeze(model)
    manager = make_manager("")
    calls = []
    monkeypatch.setattr(manager, "functions", lambda: [])
    monkeypatch.setattr(manager, "run", lambda command, **kwargs: calls.append(command) or "")
    manager.down()
    assert manager.compose("stop", "-t", "60", "cvat_ui", frozen=True) in calls
    assert manager.compose("down", "--timeout", "120", frozen=True) in calls
    assert all("--volumes" not in call and "-v" not in call for call in calls)
    assert cli.read_json(manager.state / "active.json")["extensions"] == list(CLIENTS)


@pytest.mark.parametrize("previous_selection,current_selection", [("task_context", ""), ("", "task_context")])
def test_check_uses_recorded_task_context_selection(
    cli, make_manager, monkeypatch, previous_selection, current_selection,
):
    previous = make_manager(previous_selection)
    model, _ = configure(cli, previous, monkeypatch)
    previous.freeze(model)
    manager = make_manager(current_selection)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"] or command[:2] == ["docker", "inspect"]:
            return "sha256:fixture"
        if "ps" in command:
            return "container-id"
        return "OK"

    monkeypatch.setattr(manager, "run", run)
    manager.check()
    checks = [command for command in calls if "shell" in command]
    if previous_selection:
        assert len(checks) == 1
        check = checks[0]
        assert check == manager.compose(
            "exec", "-T", "cvat_server", "python3", "-m", "django",
            "shell", "-c", check[-1], frozen=True,
        )
        assert "settings.CVAT_TASK_CONTEXT_ENABLED" in check[-1]
        assert "connection.introspection.table_names()" in check[-1]
    else:
        assert checks == []


def test_status_uses_recorded_task_context_configuration(cli, make_manager, monkeypatch):
    previous = make_manager("task_context")
    model, _ = configure(cli, previous, monkeypatch)
    previous.freeze(model)
    manager = make_manager("")
    calls = []
    monkeypatch.setattr(cli, "Manager", lambda *args: manager)
    monkeypatch.setattr(manager, "functions", lambda: [])
    monkeypatch.setattr(manager, "run", lambda command, **kwargs: calls.append(command) or "")
    monkeypatch.setattr(manager, "configuration", lambda: pytest.fail("Use the recorded configuration"))
    monkeypatch.setattr(sys, "argv", [
        "cvatctl", "--env-file", str(manager.env_file), "--state-dir", str(manager.state), "status",
    ])
    cli.main()
    assert calls == [manager.compose("ps", "--all", frozen=True)]
