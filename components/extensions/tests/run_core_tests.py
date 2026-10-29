# SPDX-License-Identifier: MIT
"""Explicit tests of extension hooks in the real CVAT HTTP and serializer code.

Run with CVAT's server dependencies and fakeredis in a separate process:
    python -m pytest -q components/extensions/tests/run_core_tests.py

DRF dispatch, routing, permission scopes, SQLite models, TUS files, import
request preparation and serializers are real. OPA decisions, Redis and queue
locks are isolated; workers, app startup hooks and deployment are not exercised.
"""

from __future__ import annotations

import base64
import io
import shutil
import sys
import zipfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from components.extensions.tests.cvat_backend import configure_cvat

_RUNTIME = configure_cvat(prefix="cvat-core-integration-")

import fakeredis
import rq
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.urls import resolve, reverse
from rest_framework.test import APIRequestFactory, force_authenticate
from rq.job import JobStatus

from components.project_annotations import DICOM_FORMAT, service
from components.project_annotations.background import ProjectAnnotationImporter
from cvat.apps.dataset_manager.formats.registry import IMPORT_FORMATS, make_importer
from cvat.apps.dataset_manager.project import import_dataset_as_project
from cvat.apps.engine import models
from cvat.apps.engine.permissions import ProjectPermission
from cvat.apps.engine.rq import ExportRequestId, ImportRequestId
from cvat.apps.engine.serializers import JobReadSerializer
from cvat.apps.engine.tus import TusFile
from cvat.apps.engine.views import ProjectViewSet
from cvat.apps.iam.models import User
from cvat.apps.iam.permissions import OpenPolicyAgentPermission, PermissionResult
from cvat.apps.redis_handler.rq import CustomRQJob
from cvat.apps.redis_handler.serializers import RequestSerializer


@pytest.fixture(autouse=True)
def isolated_database():
    with transaction.atomic():
        yield
        transaction.set_rollback(True)


@pytest.fixture
def user():
    return User.objects.create(username="core-user", is_active=True)


@pytest.fixture
def project(user):
    project = models.Project.objects.create(name="existing tasks", owner=user)
    yield project
    shutil.rmtree(project.get_dirname(), ignore_errors=True)


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    policy = SimpleNamespace(allow=True, permissions=[])

    def decide(permission):
        policy.permissions.append(permission)
        return PermissionResult(allow=policy.allow)

    monkeypatch.setattr(OpenPolicyAgentPermission, "check_access", decide)
    return policy


@pytest.fixture
def queue(monkeypatch):
    from cvat.apps.redis_handler import background

    connection = fakeredis.FakeRedis()
    queue = rq.Queue(ProjectAnnotationImporter.QUEUE_NAME, connection=connection)
    monkeypatch.setattr("django_rq.get_queue", lambda *args, **kwargs: queue)
    # These two locks coordinate external Redis workers, not HTTP/file handling.
    monkeypatch.setattr(background, "get_rq_lock_for_job", lambda *args: nullcontext())
    monkeypatch.setattr(background, "get_rq_lock_by_user", lambda *args: nullcontext())
    monkeypatch.setattr(background, "define_dependent_job", lambda *args, **kwargs: None)
    yield queue
    connection.close()


def dispatch(method, path, user=None, *, data=None, content_type=None, **headers):
    factory = APIRequestFactory()
    if content_type:
        request = factory.generic(method.upper(), path, data or b"", content_type, **headers)
    else:
        request = getattr(factory, method.lower())(path, data=data, **headers)
    request.uuid = str(uuid4())
    request.iam_context = {
        "organization": None,
        "organization_specified": False,
        "privilege": "user",
    }
    if user is not None:
        force_authenticate(request, user=user)
    match = resolve(urlsplit(path).path)
    return match.func(request, *match.args, **match.kwargs)


def annotations_url(project, **query):
    path = reverse("project-annotations", kwargs={"pk": project.pk})
    return path + ("?" + urlencode(query) if query else "")


def chunk_url(project, user):
    return reverse(
        "project-append-annotations-chunk",
        kwargs={"pk": project.pk, "file_id": TusFile.FileID(user_id=user.id).as_str},
    )


def create_upload(project, user, payload):
    response = dispatch(
        "post",
        annotations_url(project),
        user,
        HTTP_UPLOAD_LENGTH=str(len(payload)),
        HTTP_UPLOAD_METADATA="filename " + base64.b64encode(b"annotations.zip").decode(),
    )
    assert response.status_code == 201
    path = urlsplit(response["Location"]).path
    assert resolve(path).url_name == "project-append-annotations-chunk"
    assert resolve(path).kwargs["pk"] == str(project.pk)
    return path, response["Upload-Filename"]


def zip_upload(name="annotation_file"):
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        archive.writestr(
            "scan/annotations.xml", "<annotations><version>1.1</version></annotations>"
        )
    return {name: SimpleUploadedFile("annotations.zip", contents.getvalue())}


@pytest.mark.parametrize(
    "name",
    [
        "DICOM 1.0",
        DICOM_FORMAT,
        "ITGformat 1.0",
        "Task Guide JSON 1.0",
        "Task Guide Markdown Zip 1.0",
        "Task Guide DICOM Report Import 1.0",
    ],
)
def test_custom_formats_are_registered_in_the_native_registry(name):
    assert name in IMPORT_FORMATS
    assert make_importer(name).DISPLAY_NAME == name


@pytest.mark.parametrize("target", ["project", "task"])
@pytest.mark.parametrize(
    "operation,method,action",
    [
        ("options", "get", "retrieve"),
        ("preview", "post", "initiate_dataset_export"),
        ("export", "post", "initiate_dataset_export"),
        ("download", "get", "download_dataset"),
    ],
)
def test_filtered_export_routes_are_reachable_from_the_root_urlconf(
    target, operation, method, action
):
    name = f"filtered-{target}-{operation}"
    path = reverse(name, kwargs={"pk": 37})
    assert path == f"/api/filtered-export/{target}s/37/{operation}"
    route = resolve(path)
    assert route.func.actions[method] == action
    assert route.kwargs["pk"] == 37
    assert route.kwargs.get("preview_only", False) == (operation == "preview")


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("trailing_slash", [False, True])
def test_legacy_annotation_download_keeps_its_gone_response(
    project, user, queue, method, trailing_slash
):
    path = annotations_url(project) + ("/" if trailing_slash else "")
    response = dispatch(method, path, user)
    assert response.status_code == 410
    if method == "get":
        assert b"/api/projects/id/dataset/export?save_images=False" in response.content
    assert queue.count == 0


def test_tus_options_exposes_the_native_protocol(project, user, queue):
    response = dispatch("options", annotations_url(project), user)
    assert response.status_code == 204
    assert response["Tus-Resumable"] == "1.0.0"
    assert "PATCH" in response["Access-Control-Allow-Methods"]
    assert queue.count == 0


@pytest.mark.parametrize("method", ["post", "head", "patch"])
def test_annotation_upload_requires_authentication(project, user, queue, policy, method):
    path = annotations_url(project) if method == "post" else chunk_url(project, user)
    response = dispatch(method, path, HTTP_UPLOAD_START="1")
    assert response.status_code == 401
    assert policy.permissions == []
    assert queue.count == 0


@pytest.mark.parametrize("method", ["post", "head", "patch"])
def test_annotation_upload_checks_project_import_permission(project, user, queue, policy, method):
    policy.allow = False
    path = annotations_url(project) if method == "post" else chunk_url(project, user)
    response = dispatch(method, path, user, HTTP_UPLOAD_START="1")
    assert response.status_code == 403
    assert [(p.scope, p.obj.pk) for p in policy.permissions] == [
        (ProjectPermission.Scopes.IMPORT_DATASET, project.pk)
    ]
    assert queue.count == 0
    assert not project.get_dirname().exists()


@pytest.mark.parametrize("method", ["post", "head", "patch", "options"])
def test_disabled_annotation_upload_rejects_before_touching_files(
    project, user, queue, monkeypatch, method
):
    monkeypatch.setenv("CVAT_PROJECT_ANNOTATIONS_ENABLED", "0")
    path = annotations_url(project) if method in ("post", "options") else chunk_url(project, user)
    response = dispatch(method, path, user, HTTP_UPLOAD_START="1")
    assert response.status_code == 404
    assert "disabled" in str(response.data).lower()
    assert queue.count == 0
    assert not project.get_dirname().exists()


def test_tus_upload_round_trip_uses_annotation_chunk_route_and_queue(project, user, queue, policy):
    payload = b"uploaded annotation archive"
    path, filename = create_upload(project, user, payload)
    response = dispatch("head", path, user)
    assert response.status_code == 200
    assert int(response["Upload-Offset"]) == 0
    assert int(response["Upload-Length"]) == len(payload)

    conflict = dispatch(
        "patch",
        path,
        user,
        data=b"bad",
        content_type="application/offset+octet-stream",
        HTTP_UPLOAD_OFFSET="5",
    )
    assert conflict.status_code == 409
    for offset, chunk in ((0, payload[:5]), (5, payload[5:])):
        response = dispatch(
            "patch",
            path,
            user,
            data=chunk,
            content_type="application/offset+octet-stream",
            HTTP_UPLOAD_OFFSET=str(offset),
        )
        assert response.status_code == 204
        assert int(response["Upload-Offset"]) == offset + len(chunk)
    uploaded = project.get_tmp_dirname() / filename
    assert uploaded.read_bytes() == payload
    assert not uploaded.with_name(filename + ".meta").exists()
    assert queue.count == 0

    response = dispatch(
        "post",
        annotations_url(project, format="CVAT 1.1", filename=filename, import_mode="append"),
        user,
        HTTP_UPLOAD_FINISH="1",
    )
    assert response.status_code == 202
    job = queue.fetch_job(response.data["rq_id"])
    assert job.func is service.import_project_annotations
    assert job.args == (str(uploaded), project.pk, "CVAT 1.1", True)
    assert job.kwargs == {"import_mode": "append"}
    assert job.meta["request_manager_cls"] == (
        "components.project_annotations.background.ProjectAnnotationImporter"
    )
    assert job.meta["project_id"] == project.pk
    assert job.meta["user"]["id"] == user.pk
    assert all(p.scope == ProjectPermission.Scopes.IMPORT_DATASET for p in policy.permissions)


@pytest.mark.parametrize("method", ["head", "patch"])
def test_tus_file_owner_is_checked_even_when_project_access_is_allowed(
    project, user, queue, method
):
    path, filename = create_upload(project, user, b"original")
    before = (project.get_tmp_dirname() / filename).read_bytes()
    other_user = User.objects.create(username="another-uploader", is_active=True)
    response = dispatch(
        method,
        path,
        other_user,
        data=b"changed",
        content_type="application/offset+octet-stream",
        HTTP_UPLOAD_OFFSET="0",
    )
    assert response.status_code == 403
    assert (project.get_tmp_dirname() / filename).read_bytes() == before
    assert queue.count == 0


@pytest.mark.parametrize(
    "format_name",
    [
        "Task Guide JSON 1.0",
        "Task Guide Markdown Zip 1.0",
        "Task Guide DICOM Report Import 1.0",
        "DICOM 1.0",
    ],
)
def test_dataset_only_formats_are_rejected_by_annotation_endpoint(
    project, user, queue, format_name
):
    response = dispatch(
        "post",
        annotations_url(project, format=format_name),
        user,
        data=zip_upload(),
    )
    assert response.status_code == 400
    assert "Import dataset only" in str(response.data)
    assert queue.count == 0


def test_task_guide_format_remains_available_through_dataset_import(project, user, queue):
    path = reverse("project-dataset", kwargs={"pk": project.pk})
    response = dispatch(
        "post",
        path + "?" + urlencode({"format": "Task Guide Markdown Zip 1.0"}),
        user,
        data=zip_upload("dataset_file"),
    )
    assert response.status_code == 202
    job = queue.fetch_job(response.data["rq_id"])
    assert job.func is import_dataset_as_project
    assert job.args[1:3] == (project.pk, "Task Guide Markdown Zip 1.0")
    assert job.meta["request_manager_cls"] == "cvat.apps.engine.background.DatasetImporter"


def test_project_backup_still_uses_the_native_backup_importer(user, queue):
    from cvat.apps.engine import backup

    response = dispatch(
        "post",
        reverse("project-import-backup"),
        user,
        data=zip_upload("project_file"),
    )
    assert response.status_code == 202
    job = queue.fetch_job(response.data["rq_id"])
    assert job.func is backup.import_project
    assert job.args[1:] == (user.pk, None)
    assert Path(job.args[0]).is_relative_to(backup.get_backup_dirname())
    assert job.meta["request_manager_cls"] == "cvat.apps.engine.background.BackupImporter"


@pytest.mark.parametrize("mode", ["merge", "APPEND", ""])
def test_annotation_endpoint_rejects_invalid_import_modes_before_queue(project, user, queue, mode):
    response = dispatch(
        "post",
        annotations_url(project, format="CVAT 1.1", import_mode=mode),
        user,
        data=zip_upload(),
    )
    assert response.status_code == 400
    assert "replace or append" in str(response.data)
    assert queue.count == 0


@pytest.mark.parametrize("action", ["dataset", "annotations", "append_annotations_chunk"])
def test_project_upload_directory_is_kept_inside_its_project(project, action):
    view = ProjectViewSet(action=action)
    view._object = project
    assert view.get_upload_dir() == project.get_dirname() / "tmp"


def test_project_backup_upload_directory_is_unchanged(project):
    from cvat.apps.engine import backup

    view = ProjectViewSet(action="import_backup")
    view._object = project
    assert view.get_upload_dir() == backup.get_backup_dirname()
    assert not view.get_upload_dir().is_relative_to(project.get_dirname())


@pytest.mark.parametrize(
    "with_project,project_guide,task_guide",
    [
        (True, False, False),
        (True, True, False),
        (True, False, True),
        (True, True, True),
        (False, False, True),
    ],
)
def test_job_serializer_keeps_project_and_task_guides_distinct(
    project,
    user,
    with_project,
    project_guide,
    task_guide,
):
    data = models.Data.objects.create(
        size=1,
        start_frame=0,
        stop_frame=0,
        chunk_size=1,
        image_quality=100,
        original_chunk_type="imageset",
        compressed_chunk_type="imageset",
    )
    task = models.Task.objects.create(
        name="scan",
        owner=user,
        project=project if with_project else None,
        data=data,
        dimension="2d",
        mode="annotation",
        media_type="image",
    )
    segment = models.Segment.objects.create(task=task, start_frame=0, stop_frame=0)
    job = models.Job.objects.create(segment=segment)
    project_note = (
        models.AnnotationGuide.objects.create(project=project, markdown="project")
        if project_guide
        else None
    )
    task_note = (
        models.AnnotationGuide.objects.create(task=task, markdown="task") if task_guide else None
    )
    represented = JobReadSerializer(job, context={"request": None}).data
    assert represented["task_guide_id"] == (task_note.pk if task_note else None)
    inherited = project_note if with_project else task_note
    assert represented["guide_id"] == (inherited.pk if inherited else None)
    serializer = JobReadSerializer(job, data={"task_guide_id": 999}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert "task_guide_id" not in serializer.validated_data


@pytest.mark.parametrize(
    "status,action,target,message,formatted,exception,expected",
    [
        (JobStatus.STARTED, "import", "project", "Importing 2/3", None, None, "Importing 2/3"),
        (JobStatus.STARTED, "import", "project", None, None, None, ""),
        (JobStatus.QUEUED, "import", "project", "DICOM: old progress", None, None, ""),
        (JobStatus.DEFERRED, "import", "project", "waiting", None, None, ""),
        (JobStatus.SCHEDULED, "import", "project", "waiting", None, None, ""),
        (
            JobStatus.FINISHED,
            "import",
            "project",
            "DICOM: 3 tasks succeeded; 0 failed",
            None,
            None,
            "DICOM: 3 tasks succeeded; 0 failed",
        ),
        (
            JobStatus.FINISHED,
            "import",
            "project",
            "WARNING: DICOM: 2 succeeded; 1 failed",
            None,
            None,
            "WARNING: DICOM: 2 succeeded; 1 failed",
        ),
        (JobStatus.FINISHED, "import", "project", "regular import finished", None, None, ""),
        (JobStatus.FINISHED, "import", "project", None, None, None, ""),
        (JobStatus.FINISHED, "import", "task", "DICOM: task import", None, None, ""),
        (JobStatus.FINISHED, "export", "project", "DICOM: export progress", None, None, ""),
        (JobStatus.FINISHED, "create", "task", "DICOM: creation progress", None, None, ""),
        (
            JobStatus.FAILED,
            "import",
            "project",
            "DICOM: stale progress",
            "Rejected input",
            "ignored",
            "Rejected input",
        ),
        (
            JobStatus.FAILED,
            "import",
            "project",
            None,
            None,
            "ValueError: broken archive",
            "ValueError: broken archive",
        ),
        (JobStatus.FAILED, "import", "project", None, None, None, "Unknown error"),
    ],
)
def test_request_messages_keep_dicom_completion_and_existing_status_behavior(
    user,
    status,
    action,
    target,
    message,
    formatted,
    exception,
    expected,
):
    if action == "export":
        request_id = ExportRequestId(target=target, target_id=17, subresource="annotations")
    else:
        request_id = ImportRequestId(
            action=action,
            target=target,
            target_id=17,
            subresource="annotations" if action == "import" else None,
        )
    with fakeredis.FakeRedis() as connection:
        job = CustomRQJob(id=request_id.render(), connection=connection)
        job.parsed_id = request_id
        job.set_status(status)
        job.meta = {
            "status": message,
            "formatted_exception": formatted,
            "user": {
                "id": user.pk,
                "username": user.username,
                "email": "",
                "first_name": "",
                "last_name": "",
                "url": "",
            },
        }
        job._exc_info = exception
        serializer = RequestSerializer(job)
        represented = serializer.data
        assert represented["message"] == expected
        expected_status = (
            JobStatus.QUEUED if status in (JobStatus.DEFERRED, JobStatus.SCHEDULED) else status
        )
        assert represented["status"] == expected_status
        assert represented["operation"]["target"] == target
        assert represented["owner"] == {"id": user.pk, "username": user.username}
        with patch.object(job, "get_status", wraps=job.get_status) as get_status:
            assert serializer.get_message(job) == expected
        get_status.assert_called_once_with(refresh=False)
