# SPDX-License-Identifier: MIT
"""Use the real extension manager with Docker commands replaced by test doubles."""

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

from components.extensions.catalog import register
from components.extensions.tests.test_client_plugins import cli, model_for

ROOT = Path(__file__).resolve().parents[3]
FEATURE = "project_annotations"
PLUGIN = "plugins/project-annotations"
OVERLAY = "components/project_annotations/docker-compose.project-annotations.yml"


@pytest.fixture
def make_manager(cli, tmp_path):
    register(cli)
    root = tmp_path / "checkout"
    root.mkdir()
    for relative in (*cli.COMPOSE_FILES, "Dockerfile", "Dockerfile.ui", ".dockerignore"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    for plugin in cli.EXTENSION_PLUGINS.values():
        target = root / f"cvat-ui/{plugin}/src/ts/index.tsx"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env = tmp_path / "deployment.env"

    def create(selected=FEATURE, extra=""):
        env.write_text(f"CVAT_EXTENSIONS={selected}\nCOMPOSE_PROJECT_NAME=test\n{extra}")
        env.chmod(0o600)
        instance = cli.Manager(root, env, state)
        instance.identity = lambda: {
            "project": "test",
            "network": "test_cvat",
            "namespace": "nuclio",
            "daemon": "test",
        }
        return instance

    return create


def configure(cli, manager, monkeypatch):
    monkeypatch.setattr(cli, "source_tag", lambda root: "fixed-source")
    monkeypatch.setattr(manager, "version_check", lambda: None)

    def run(command, **kwargs):
        assert command[-3:] == ["config", "--format", "json"]
        model = model_for(cli, manager)
        if FEATURE in manager.extensions:
            overlay = yaml.safe_load((ROOT / OVERLAY).read_text())
            for name, value in overlay["services"].items():
                model["services"][name].update(value)
        return json.dumps(model)

    monkeypatch.setattr(manager, "run", run)
    return manager.configuration()


def test_descriptor_uses_only_existing_services_and_no_storage():
    descriptor = json.loads((ROOT / "components/project_annotations/extension.json").read_text())
    assert descriptor == {
        "name": FEATURE,
        "client_plugin": PLUGIN,
        "compose_files": [OVERLAY],
    }
    overlay = yaml.safe_load((ROOT / OVERLAY).read_text())
    assert overlay == {
        "services": {
            name: {"environment": {"CVAT_PROJECT_ANNOTATIONS_ENABLED": "1"}}
            for name in ("cvat_server", "cvat_worker_import")
        }
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_selection_controls_backend_overlay_and_ui_together(cli, make_manager, enabled):
    manager = make_manager(
        FEATURE if enabled else "",
        f"CVAT_CLIENT_PLUGINS=custom/extra:{PLUGIN}\n",
    )
    assert manager.values["CVAT_CLIENT_PLUGINS"].split(":") == [
        "custom/extra",
        *([PLUGIN] if enabled else []),
    ]
    command = manager.compose("config")
    files = [command[index + 1] for index, value in enumerate(command) if value == "-f"]
    assert (str(manager.root / OVERLAY) in files) is enabled
    assert str(manager.root / cli.SERVERLESS_COMPOSE) not in files
    assert FEATURE not in cli.SERVERLESS_EXTENSIONS
    assert FEATURE not in cli.FUNCTIONS
    assert manager.function_environment() == {}
    assert manager.model_sources() == {}


def test_dicom_import_is_independently_selected(cli, make_manager):
    manager = make_manager(FEATURE)
    assert manager.extensions == (FEATURE,)
    assert manager.values["CVAT_CLIENT_PLUGINS"] == PLUGIN
    combined = make_manager("dicom_import,project_annotations,task_context")
    assert set(combined.extensions) == {"dicom_import", FEATURE, "task_context"}
    assert set(combined.values["CVAT_CLIENT_PLUGINS"].split(":")) == {
        "plugins/dicom-import",
        PLUGIN,
        "plugins/task-context",
    }


@pytest.mark.parametrize("command", ["config", "doctor"])
@pytest.mark.parametrize("enabled", [False, True])
def test_config_and_doctor_report_the_resolved_extension(
    cli, make_manager, monkeypatch, capsys, command, enabled
):
    manager = make_manager(FEATURE if enabled else "")
    model = configure(cli, manager, monkeypatch)
    monkeypatch.setattr(cli, "Manager", lambda *args: manager)
    monkeypatch.setattr(manager, "configuration", lambda: model)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cvatctl",
            "--env-file",
            str(manager.env_file),
            "--state-dir",
            str(manager.state),
            command,
        ],
    )
    cli.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["extensions"] == ([FEATURE] if enabled else [])
    assert summary["client_plugins"] == ([PLUGIN] if enabled else [])
    for service in ("cvat_server", "cvat_worker_import"):
        assert (
            model["services"][service]
            .get("environment", {})
            .get("CVAT_PROJECT_ANNOTATIONS_ENABLED")
            == "1"
        ) is enabled


def test_configuration_rejects_missing_ui_source(cli, make_manager, monkeypatch):
    manager = make_manager()
    (manager.root / f"cvat-ui/{PLUGIN}/src/ts/index.tsx").unlink()
    monkeypatch.setattr(manager, "version_check", lambda: None)
    with pytest.raises(cli.OperationError, match="Missing source"):
        manager.configuration()


def test_configuration_rejects_lost_ui_plugin(cli, make_manager, monkeypatch):
    manager = make_manager()
    model = configure(cli, manager, monkeypatch)
    model["services"]["cvat_ui"]["build"]["args"]["CLIENT_PLUGINS"] = ""
    with pytest.raises(cli.OperationError, match="CLIENT_PLUGINS argument lost"):
        cli.validate_model(model, manager.env)


def test_build_and_no_build_up_do_not_run_annotation_or_gpu_work(cli, make_manager, monkeypatch):
    manager = make_manager()
    model = configure(cli, manager, monkeypatch)
    calls = []
    monkeypatch.setattr(manager, "configuration", lambda: model)
    monkeypatch.setattr(manager, "ensure_network", lambda: None)
    monkeypatch.setattr(manager, "restore_functions", lambda: set())
    monkeypatch.setattr(manager, "nuctl_check", lambda *a: pytest.fail("No model deployment"))
    monkeypatch.setattr(manager, "ensure_volume", lambda *a: pytest.fail("No extension storage"))
    monkeypatch.setattr(manager, "deploy", lambda *a, **k: pytest.fail("No model deployment"))

    def run(command, **kwargs):
        calls.append(command)
        return "sha256:fixture" if command[:3] == ["docker", "image", "inspect"] else ""

    monkeypatch.setattr(manager, "run", run)
    manager.build()
    assert calls == [manager.compose("build", "cvat_server", "cvat_ui")]
    calls.clear()
    manager.up(no_build=True)
    assert manager.compose("up", "-d", "--no-build") in calls
    assert not any("build" in command or "exec" in command for command in calls)
    saved = cli.read_json(manager.state / "active.json")
    assert saved["extensions"] == [FEATURE]
    assert manager.matches_configuration(saved, model)
    changed = copy.deepcopy(model)
    changed["services"]["cvat_ui"]["build"]["args"]["CLIENT_PLUGINS"] = ""
    assert not manager.matches_configuration(saved, changed)


def test_disabling_changes_ui_image_and_runtime_configuration(cli, make_manager, monkeypatch):
    previous = make_manager()
    enabled = configure(cli, previous, monkeypatch)
    previous.freeze(enabled)
    saved = cli.read_json(previous.state / "active.json")
    current = make_manager("")
    disabled = configure(cli, current, monkeypatch)
    assert (
        enabled["services"]["cvat_server"]["image"] == disabled["services"]["cvat_server"]["image"]
    )
    assert enabled["services"]["cvat_ui"]["image"] != disabled["services"]["cvat_ui"]["image"]
    assert not current.matches_configuration(saved, disabled)


def test_down_preserves_data_and_uses_recorded_configuration(cli, make_manager, monkeypatch):
    previous = make_manager()
    previous.freeze(configure(cli, previous, monkeypatch))
    current = make_manager("")
    calls = []
    monkeypatch.setattr(current, "functions", lambda: [])
    monkeypatch.setattr(current, "run", lambda command, **kwargs: calls.append(command) or "")
    current.down()
    assert current.compose("stop", "-t", "60", "cvat_ui", frozen=True) in calls
    assert current.compose("down", "--timeout", "120", frozen=True) in calls
    assert all(
        "--volumes" not in call and "-v" not in call and "exec" not in call for call in calls
    )
    assert cli.read_json(current.state / "active.json")["extensions"] == [FEATURE]


def test_status_uses_recorded_configuration_after_disable(cli, make_manager, monkeypatch):
    previous = make_manager()
    previous.freeze(configure(cli, previous, monkeypatch))
    current = make_manager("")
    calls = []
    monkeypatch.setattr(cli, "Manager", lambda *args: current)
    monkeypatch.setattr(current, "functions", lambda: [])
    monkeypatch.setattr(current, "configuration", lambda: pytest.fail("Use recorded configuration"))
    monkeypatch.setattr(current, "run", lambda command, **kwargs: calls.append(command) or "")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cvatctl",
            "--env-file",
            str(current.env_file),
            "--state-dir",
            str(current.state),
            "status",
        ],
    )
    cli.main()
    assert calls == [current.compose("ps", "--all", frozen=True)]


@pytest.mark.parametrize("wrong_ui_image", [False, True])
def test_check_verifies_recorded_images_without_importing(
    cli, make_manager, monkeypatch, wrong_ui_image
):
    previous = make_manager()
    model = configure(cli, previous, monkeypatch)
    previous.freeze(
        model, {service["image"]: "sha256:recorded" for service in model["services"].values()}
    )
    current = make_manager("")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return "sha256:recorded"
        if command[:2] == ["docker", "inspect"]:
            return (
                "sha256:wrong" if wrong_ui_image and command[2] == "cvat_ui" else "sha256:recorded"
            )
        for service in model["services"]:
            if command == current.compose("ps", "-q", service, frozen=True):
                return service
        pytest.fail(f"Unexpected command: {command}")

    monkeypatch.setattr(current, "run", run)
    monkeypatch.setattr(current, "configuration", lambda: pytest.fail("Use recorded configuration"))
    if wrong_ui_image:
        with pytest.raises(cli.OperationError, match="cvat_ui: running image ID differs"):
            current.check()
    else:
        current.check()
    assert all("exec" not in call and "up" not in call and "build" not in call for call in calls)
