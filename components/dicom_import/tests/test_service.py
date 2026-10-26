# SPDX-License-Identifier: MIT
"""Service boundaries without a database, Redis or OPA process.

Directory methods come from the real CVAT model source so a fake model cannot
accidentally supply a method that Data/Task/Job do not implement.
"""

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from components.dicom_import import DicomImportError
from components.dicom_import.conversion import Plan, PreparedTask

ROOT = Path(__file__).resolve().parents[3]


def directory_models(settings):
    tree = ast.parse((ROOT / "cvat/apps/engine/models.py").read_text())
    result = SimpleNamespace()
    for definition in tree.body:
        if not isinstance(definition, ast.ClassDef) or definition.name not in {
            "Data",
            "Task",
            "Job",
        }:
            continue
        methods = [
            node
            for node in definition.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {"get_data_dirname", "get_dirname"}
        ]
        namespace = {"Path": Path, "settings": settings}
        exec(
            compile(ast.Module(body=methods, type_ignores=[]), "<CVAT directory methods>", "exec"),
            namespace,
        )
        setattr(
            result,
            definition.name,
            type(definition.name, (), {method.name: namespace[method.name] for method in methods}),
        )
    return result


class Signal:
    def __init__(self):
        self.receivers = []

    def connect(self, receiver, *, sender, weak):
        self.receivers.append((receiver, sender))

    def disconnect(self, receiver, *, sender):
        self.receivers.remove((receiver, sender))

    def send(self, sender, instance, *, created=True):
        for receiver, expected in self.receivers:
            if expected is sender:
                receiver(sender, instance, created)


@pytest.fixture
def service(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        MEDIA_DATA_ROOT=tmp_path / "data",
        TASKS_ROOT=tmp_path / "tasks",
        JOBS_ROOT=tmp_path / "jobs",
    )
    models = directory_models(settings)
    models.Project = SimpleNamespace(objects=MagicMock())
    signal = Signal()
    permission = MagicMock()
    permission.Scopes.IMPORT_DATASET = "import:dataset"
    permission.create_base_perm.return_value.check_access.return_value.allow = True
    user = SimpleNamespace(id=7)
    users = MagicMock()
    users.objects.filter.return_value.first.return_value = user
    meta = SimpleNamespace(user=user, org_id=None, save=MagicMock())
    serializers = {
        name: MagicMock()
        for name in (
            "DataSerializer",
            "LabelSerializer",
            "LabeledDataSerializer",
            "ProjectWriteSerializer",
            "TaskWriteSerializer",
        )
    }
    imports = {
        "rq": {"get_current_job": lambda: object()},
        "django.conf": {"settings": settings},
        "django.contrib.auth": {"get_user_model": lambda: users},
        "django.db": {"connection": SimpleNamespace(in_atomic_block=True)},
        "django.db.models.signals": {"post_save": signal},
        "django.http": {"HttpRequest": SimpleNamespace},
        "cvat.apps.dataset_manager.task": {"patch_task_data": MagicMock()},
        "cvat.apps.engine": {"models": models},
        "cvat.apps.engine.permissions": {"ProjectPermission": permission},
        "cvat.apps.engine.rq": {"ImportRQMeta": SimpleNamespace(for_job=lambda _: meta)},
        "cvat.apps.engine.serializers": serializers,
        "cvat.apps.engine.task": {"initialize_task": MagicMock()},
        "cvat.apps.engine.utils": {"av_scan_paths": MagicMock()},
        "cvat.apps.iam.middleware": {"get_organization": lambda _: {"privilege": "user"}},
        "cvat.apps.iam.permissions": {"get_iam_context": lambda *_: {"user_id": user.id}},
    }
    for name, values in imports.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        "components.dicom_import._service_test", ROOT / "components/dicom_import/service.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    loaded.test_meta = meta
    loaded.test_users = users
    monkeypatch.setenv("CVAT_DICOM_IMPORT_ENABLED", "1")
    return loaded


def allocate(service, kind, identifier=1):
    model = getattr(service.models, kind)
    instance = model()
    instance.id = identifier
    service.post_save.send(model, instance)
    path = instance.get_data_dirname() if kind == "Data" else instance.get_dirname()
    path.mkdir(parents=True)
    (path / "created").write_text("new media")
    return instance, path


def project(service):
    instance = SimpleNamespace(id=3, organization_id=None, tasks=MagicMock())
    instance.tasks.values_list.return_value = []
    instance.tasks.exclude.return_value.exists.return_value = False
    service.models.Project.objects.select_for_update.return_value.get.return_value = instance
    return instance


def test_native_data_task_job_directory_contract_and_rollback(service):
    paths = []
    with pytest.raises(RuntimeError, match="failure"):
        with service.created_directories():
            for kind in ("Data", "Task", "Job"):
                _, path = allocate(service, kind)
                paths.append(path)
            raise RuntimeError("failure")
    assert not any(path.exists() for path in paths)
    assert not service.post_save.receivers


def test_success_keeps_created_media(service):
    with service.created_directories():
        _, path = allocate(service, "Data")
    assert (path / "created").read_text() == "new media"
    assert not service.post_save.receivers


def test_stale_directory_is_never_removed(service):
    path = service.settings.MEDIA_DATA_ROOT / "1"
    path.mkdir(parents=True)
    (path / "existing").write_text("keep")
    with pytest.raises(DicomImportError, match="already exists"):
        with service.created_directories():
            allocate(service, "Data")
    assert (path / "existing").read_text() == "keep"


def test_queued_request_rechecks_standard_project_permission(service, monkeypatch, tmp_path):
    denied = service.ProjectPermission.create_base_perm.return_value.check_access
    denied.return_value.allow = False
    read_upload = MagicMock()
    monkeypatch.setattr(service, "read_upload", read_upload)
    current_project = project(service)
    with pytest.raises(DicomImportError, match="permission"):
        service.import_project(None, tmp_path, current_project)
    read_upload.assert_not_called()
    args = service.ProjectPermission.create_base_perm.call_args.args
    assert args[2] == "import:dataset" and args[4] is current_project


def test_project_dimension_is_read_from_tasks_not_a_nonexistent_model_field(service):
    current_project = project(service)
    assert service.import_user(service.test_meta, current_project).id == 7
    current_project.tasks.exclude.assert_called_with(dimension__in=("", "2d"))
    current_project.tasks.exclude.return_value.exists.return_value = True
    with pytest.raises(DicomImportError, match="2D project"):
        service.import_user(service.test_meta, current_project)


def test_permissions_are_rechecked_after_conversion_and_lock(service, monkeypatch, tmp_path):
    access = service.ProjectPermission.create_base_perm.return_value.check_access
    access.side_effect = [SimpleNamespace(allow=True), SimpleNamespace(allow=False)]
    monkeypatch.setattr(service, "read_upload", lambda *_: ("SYNTHETIC", []))
    monkeypatch.setattr(service, "prepare", lambda *_args, **_kwargs: Plan())
    ensure = MagicMock()
    monkeypatch.setattr(service, "ensure_label", ensure)
    with pytest.raises(DicomImportError, match="permission"):
        service.import_project(None, tmp_path, project(service))
    ensure.assert_not_called()
    service.models.Project.objects.select_for_update.assert_called_once()


@pytest.mark.parametrize("kind", ["missing", "inactive", "changed_workspace"])
def test_invalid_queue_identity_is_refused(service, kind):
    current_project = project(service)
    if kind == "missing":
        service.test_meta.user = None
    elif kind == "inactive":
        service.test_users.objects.filter.return_value.first.return_value = None
    else:
        service.test_meta.org_id = 8
    with pytest.raises(DicomImportError):
        service.import_user(service.test_meta, current_project)
    service.ProjectPermission.create_base_perm.assert_not_called()


def test_final_status_failure_cleans_new_media(service, monkeypatch, tmp_path):
    prepared = PreparedTask("study", True, [tmp_path / "movie.mp4"], {"movie.mp4": {}}, 1)
    monkeypatch.setattr(service, "read_upload", lambda *_: ("SYNTHETIC", []))
    monkeypatch.setattr(service, "prepare", lambda *_args, **_kwargs: Plan(tasks=[prepared]))
    label = SimpleNamespace(id=2, attributespec_set=MagicMock())
    label.attributespec_set.values_list.return_value = []
    monkeypatch.setattr(service, "ensure_label", lambda *_: label)
    paths = []

    def create_task(*_):
        data, data_path = allocate(service, "Data")
        task, task_path = allocate(service, "Task")
        task.data = data
        paths.extend((data_path, task_path))
        return task

    monkeypatch.setattr(service, "create_task", create_task)
    monkeypatch.setattr(service, "tag_batches", lambda *_: [])

    def save_status():
        if service.test_meta.status.startswith("DICOM:"):
            raise RuntimeError("Redis unavailable at final status")

    service.test_meta.save.side_effect = save_status
    with pytest.raises(RuntimeError, match="Redis"):
        service.import_project(None, tmp_path, project(service))
    assert paths and not any(path.exists() for path in paths)
