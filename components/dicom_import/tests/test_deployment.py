# SPDX-License-Identifier: MIT
"""DICOM limits follow the existing cvatctl lifecycle without starting imports."""

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from components.dicom_import import DicomImportError
from components.dicom_import.settings import DEFAULTS, limits
from components.extensions.catalog import register
from components.extensions.tests.test_client_plugins import cli, model_for

ROOT = Path(__file__).resolve().parents[3]
OVERLAY = "components/dicom_import/docker-compose.dicom.yml"


@pytest.mark.parametrize("key", DEFAULTS)
@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "true", "1 TiB", str(2**53)])
def test_invalid_limits_are_rejected(key, value):
    with pytest.raises(DicomImportError, match=key):
        limits({key: value})


def test_large_folders_fit_default_limits_and_environment_override(monkeypatch):
    defaults = limits({})
    assert defaults["max_upload_bytes"] >= 200 * 1024**3
    assert defaults["max_expanded_bytes"] >= 200 * 1024**3
    assert defaults["max_upload_files"] == 100000
    monkeypatch.setenv("CVAT_DICOM_MAX_UPLOAD_BYTES", str(250 * 1024**3))
    assert limits()["max_upload_bytes"] == 250 * 1024**3


@pytest.fixture
def deployment(cli, tmp_path, monkeypatch):
    register(cli)
    root = tmp_path / "checkout"
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
    monkeypatch.setattr(cli, "source_tag", lambda root: "fixture")

    def make(enabled=True, extra=""):
        env.write_text(f"CVAT_EXTENSIONS={'dicom_import' if enabled else ''}\n{extra}")
        env.chmod(0o600)
        instance = cli.Manager(root, env, state)
        instance.identity = lambda: {
            "project": "cvat",
            "network": "cvat_cvat",
            "namespace": "nuclio",
            "daemon": "test",
        }
        instance.version_check = lambda: None

        def configure(command, **kwargs):
            assert command[-3:] == ["config", "--format", "json"]
            model = model_for(cli, instance)
            model["networks"]["cvat"]["name"] = "cvat_cvat"
            if enabled:
                overlay = yaml.safe_load((ROOT / OVERLAY).read_text())
                assert set(overlay) == {"services"}
                assert set(overlay["services"]) == {"cvat_server", "cvat_worker_import"}
                for name, service in overlay["services"].items():
                    resolved = {}
                    for key, value in service["environment"].items():
                        match = re.fullmatch(r"\$\{([A-Z_]+):-([0-9]+)\}", str(value))
                        resolved[key] = instance.env.get(match[1], match[2]) if match else value
                    model["services"][name]["environment"] = resolved
            return json.dumps(model)

        instance.run = configure
        return instance, instance.configuration()

    return make


@pytest.mark.parametrize("doctor", [False, True])
def test_config_and_doctor_show_effective_limits(cli, deployment, doctor):
    manager, model = deployment(extra="CVAT_DICOM_MAX_UPLOAD_BYTES=268435456000\n")
    manager.configuration = lambda: model
    summary = cli.configuration_summary(manager, doctor=doctor)
    assert summary["dicom_import_limits"]["max_upload_bytes"] == 250 * 1024**3
    assert summary["client_plugins"] == ["plugins/dicom-import"]
    for name in ("cvat_server", "cvat_worker_import"):
        assert (
            model["services"][name]["environment"]["CVAT_DICOM_MAX_UPLOAD_BYTES"] == "268435456000"
        )
    assert manager.function_environment() == {}


@pytest.mark.parametrize("service", ["cvat_server", "cvat_worker_import"])
def test_configuration_rejects_lost_dicom_setting(cli, deployment, service):
    manager, model = deployment()
    del model["services"][service]["environment"]["CVAT_DICOM_MAX_UPLOAD_BYTES"]
    with pytest.raises(cli.OperationError, match="DICOM setting"):
        cli.validate_model(model, manager.env)


def test_invalid_setting_is_checked_only_when_enabled(cli, deployment):
    with pytest.raises(cli.OperationError, match="CVAT_DICOM_MAX_UPLOAD_BYTES"):
        deployment(extra="CVAT_DICOM_MAX_UPLOAD_BYTES=0\n")
    manager, model = deployment(False, "CVAT_DICOM_MAX_UPLOAD_BYTES=0\n")
    manager.configuration = lambda: model
    assert "dicom_import_limits" not in cli.configuration_summary(manager)


def test_build_and_reuse_keep_existing_services_without_imports(cli, deployment):
    manager, model = deployment()
    manager.configuration = lambda: model
    manager.ensure_network = lambda: None
    manager.restore_functions = lambda: set()
    manager.deploy = lambda *a, **k: pytest.fail("DICOM does not deploy models")
    manager.ensure_volume = lambda *a: pytest.fail("DICOM does not add storage volumes")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return "sha256:fixture" if command[:3] == ["docker", "image", "inspect"] else ""

    manager.run = run
    manager.build()
    assert calls == [manager.compose("build", "cvat_server", "cvat_ui")]
    calls.clear()
    manager.up(no_build=True)
    assert manager.compose("up", "-d", "--no-build") in calls
    assert not any("exec" in call or "build" in call for call in calls)
    saved = cli.read_json(manager.state / "active.json")
    changed = copy.deepcopy(model)
    changed["services"]["cvat_worker_import"]["environment"]["CVAT_DICOM_MAX_UPLOAD_BYTES"] = "1"
    assert not manager.matches_configuration(saved, changed)


def test_disable_retains_recorded_status_stop_and_image_checks(cli, deployment):
    previous, model = deployment()
    previous.freeze(
        model, {service["image"]: "sha256:fixture" for service in model["services"].values()}
    )
    current, disabled = deployment(False)
    assert "environment" not in disabled["services"]["cvat_server"]
    assert disabled["services"]["cvat_ui"]["image"] != model["services"]["cvat_ui"]["image"]
    calls = []
    current.functions = lambda: []
    current.configuration = lambda: pytest.fail("Use recorded deployment")

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["docker", "image", "inspect"] or command[:2] == ["docker", "inspect"]:
            return "sha256:fixture"
        for service in model["services"]:
            if command == current.compose("ps", "-q", service, frozen=True):
                return service
        return ""

    current.run = run
    cli.show_status(current)
    current.check()
    current.down()
    assert current.compose("ps", "--all", frozen=True) in calls
    assert current.compose("down", "--timeout", "120", frozen=True) in calls
    assert not any("exec" in call or "--volumes" in call or "-v" in call for call in calls)
