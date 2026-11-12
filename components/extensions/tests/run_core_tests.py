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
import json
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


@pytest.mark.parametrize("roi", [None, [12, 8, 44, 32]])
def test_registered_interactor_native_routing_permissions_prompts_and_mask(
    user, policy, monkeypatch, roi,
):
    import numpy as np
    from components.model_registry.registry.schema import InvokeRequest, Manifest, function_metadata
    from components.model_registry.registry.codec import decode_image, encode_interactor
    from django.conf import settings
    from cvat.apps.engine.permissions import JobPermission
    from cvat.apps.lambda_manager.permissions import LambdaPermission
    from cvat.apps.lambda_manager.views import LambdaGateway, TaskFrameProvider

    manifest = Manifest.model_validate_json(
        (ROOT / "components/model_registry/examples/prompting/manifest.json").read_text()
    )
    metadata = function_metadata("a" * 20, "b" * 16, manifest.model_dump(), "nuclio")
    fid = metadata["metadata"]["name"]
    data = models.Data.objects.create(size=1, start_frame=0, stop_frame=0, chunk_size=1,
                                     image_quality=100, original_chunk_type="imageset",
                                     compressed_chunk_type="imageset")
    task = models.Task.objects.create(name="prompt image", owner=user, data=data,
                                      dimension="2d", mode="annotation", media_type="image")
    models.Image.objects.create(data=data, frame=0, path="sample.png", width=64, height=48)
    segment = models.Segment.objects.create(task=task, start_frame=0, stop_frame=0)
    job = models.Job.objects.create(segment=segment)
    models.Label.objects.create(task=task, name="chosen target", type="mask")
    calls = []
    def http(self, **kwargs):
        calls.append(kwargs)
        if kwargs.get("method", "get") == "get":
            return metadata
        payload = kwargs["data"]
        image = decode_image(payload["image"])
        InvokeRequest.model_validate(payload).prompts_for(manifest, image.shape[1], image.shape[0])
        return encode_interactor(image.mean(axis=2) > 128, image.shape)
    monkeypatch.setattr(LambdaGateway, "_http", http)
    monkeypatch.setitem(settings.NUCLIO, "INVOKE_METHOD", "dashboard")
    monkeypatch.setattr(TaskFrameProvider, "get_frame", lambda *args: SimpleNamespace(
        data=io.BytesIO((ROOT / "components/model_registry/examples/prompting/sample.png").read_bytes())
    ))
    # Deployment event storage is outside this isolated HTTP/permission test.
    monkeypatch.setattr("cvat.apps.lambda_manager.views.handle_function_call", lambda *args, **kwargs: None)
    path = reverse("lambda_function-detail", kwargs={"func_id": fid})
    assert resolve(path).func.cls.__name__ == "FunctionViewSet"
    info = dispatch("get", path, user)
    assert info.status_code == 200
    assert info.data["kind"] == "interactor" and info.data["startswith_box_optional"] is True
    assert info.data["box_satisfies_min_pos_points"] is True
    request = {"task": task.id, "job": job.id, "frame": 0,
               "pos_points": [[14, 10]], "neg_points": [[13, 9]],
               "obj_bbox": [[12, 8], [44, 32]]}
    if roi:
        request["roi"] = roi
    response = dispatch("post", path, user, data=json.dumps(request), content_type="application/json")
    assert response.status_code == 200, response.data
    payload = calls[-1]["data"]
    assert payload["pos_points"] == ([[2, 2]] if roi else [[14, 10]])
    assert payload["neg_points"] == ([[1, 1]] if roi else [[13, 9]])
    assert payload["obj_bbox"] == ([[0, 0], [32, 24]] if roi else [[12, 8], [44, 32]])
    assert any(isinstance(perm, LambdaPermission) and perm.scope == LambdaPermission.Scopes.CALL_ONLINE
               for perm in policy.permissions)
    assert any(isinstance(perm, JobPermission) and perm.scope == JobPermission.Scopes.VIEW_DATA
               and perm.obj.id == job.id for perm in policy.permissions)
    mask = np.asarray(response.data["mask"])
    assert mask.shape == (48, 64) and mask.sum() == 32 * 24 - 8 * 8
    assert mask[18, 22] == 0 and mask[10, 14] == 1
    box_only = {**request, "pos_points": [], "neg_points": []}
    response = dispatch("post", path, user, data=json.dumps(box_only), content_type="application/json")
    assert response.status_code == 200, response.data
    assert calls[-1]["data"]["pos_points"] == []
    assert calls[-1]["data"]["obj_bbox"] == payload["obj_bbox"]
    # CVAT's AI tools send an empty box when only points are placed.
    points_only = {**request, "obj_bbox": []}
    response = dispatch("post", path, user, data=json.dumps(points_only), content_type="application/json")
    assert response.status_code == 200, response.data
    assert calls[-1]["data"]["obj_bbox"] == []
    before = len(calls)
    policy.allow = False
    assert dispatch("post", path, user, data=json.dumps(request), content_type="application/json").status_code == 403
    assert len(calls) == before
    assert dispatch("post", path, data=json.dumps(request), content_type="application/json").status_code == 401
    assert len(calls) == before


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


def enqueue_task_guides(project, user, queue, format_name, members):
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    path = reverse("project-dataset", kwargs={"pk": project.pk})
    response = dispatch(
        "post",
        path + "?" + urlencode({"format": format_name}),
        user,
        data={"dataset_file": SimpleUploadedFile("guides.zip", contents.getvalue())},
    )
    assert response.status_code == 202, response.data
    job = queue.fetch_job(response.data["rq_id"])
    assert job.func is import_dataset_as_project
    return job


@pytest.mark.parametrize(
    "format_name",
    [
        "Task Guide JSON 1.0",
        "Task Guide Markdown Zip 1.0",
        "Task Guide DICOM Report Import 1.0",
    ],
)
def test_task_guides_save_large_bodies_through_the_native_import_job(
    project, user, queue, monkeypatch, format_name
):
    tasks = [
        models.Task.objects.create(project=project, owner=user, name=name)
        for name in ("scan-one", "scan-two")
    ]
    original = models.AnnotationGuide.objects.create(task=tasks[0], markdown="Manual notes\n")
    project_guide = models.AnnotationGuide.objects.create(project=project, markdown="Project notes")
    old_dates = [project.updated_date, *(task.updated_date for task in tasks)]
    text = "x" * 1_000_001
    is_report = format_name == "Task Guide DICOM Report Import 1.0"
    if is_report:
        label = models.Label.objects.create(project=project, name="DicomTag", type="tag")
        spec = models.AttributeSpec.objects.create(
            label=label, name="StudyInstanceUID", input_type="text", mutable=False
        )
        for task in tasks:
            data = models.Data.objects.create(
                size=1,
                start_frame=0,
                stop_frame=0,
                chunk_size=1,
                image_quality=100,
                original_chunk_type="imageset",
                compressed_chunk_type="imageset",
            )
            task.data = data
            task.mode = "annotation"
            task.save(update_fields=["data", "mode"])
            models.Image.objects.create(data=data, frame=0, path="scan.png", width=1, height=1)
            segment = models.Segment.objects.create(task=task, start_frame=0, stop_frame=0)
            tag = models.LabeledImage.objects.create(
                job=models.Job.objects.create(segment=segment), label=label, frame=0
            )
            models.LabeledImageAttributeVal.objects.create(
                image=tag, job_id=tag.job_id, spec=spec, value="1.2.3"
            )
        members = {
            "reports.json": json.dumps(
                [{"input": {"study_uid": "1.2.3"}, "result": {"findings": text}}]
            )
        }
    elif format_name == "Task Guide JSON 1.0":
        members = {"guides.json": json.dumps({task.name: text for task in tasks})}
    else:
        members = {f"{task.name}.md": text for task in tasks}
    job = enqueue_task_guides(project, user, queue, format_name, members)
    monkeypatch.setattr(rq, "get_current_job", lambda: job)
    job.func(*job.args, **job.kwargs)
    guides = list(models.AnnotationGuide.objects.filter(task__in=tasks).order_by("task_id"))
    assert len(guides) == 2
    assert guides[0].pk == original.pk
    if is_report:
        assert guides[0].markdown.startswith("Manual notes\n")
        for guide in guides:
            assert text in guide.markdown
            assert guide.markdown.count("<!-- cvat:study-report:start -->") == 1
    else:
        assert [guide.markdown for guide in guides] == [text, text]
    project_guide.refresh_from_db()
    assert project_guide.markdown == "Project notes"
    for instance in (project, *tasks):
        instance.refresh_from_db()
    changed_dates = [project.updated_date, *(task.updated_date for task in tasks)]
    assert all(after > before for before, after in zip(old_dates, changed_dates))
    assert "1 created, 1 updated, 0 unchanged" in job.meta["status"]

    # Reimport uses the native save path without touching already identical guides.
    guide_dates = [guide.updated_date for guide in guides]
    job.func(*job.args, **job.kwargs)
    for instance in (project, *tasks, *guides):
        instance.refresh_from_db()
    assert [project.updated_date, *(task.updated_date for task in tasks)] == changed_dates
    assert [guide.updated_date for guide in guides] == guide_dates
    assert "0 created, 0 updated, 2 unchanged" in job.meta["status"]


@pytest.mark.parametrize("failure", ["late_input", "second_save"])
def test_task_guide_native_job_keeps_all_guides_and_dates_on_failure(
    project, user, queue, monkeypatch, failure
):
    from cvat.apps.dataset_manager.bindings import CvatImportError

    first = models.Task.objects.create(project=project, owner=user, name="first")
    second = models.Task.objects.create(project=project, owner=user, name="second")
    guide = models.AnnotationGuide.objects.create(task=first, markdown="Keep this guide")
    before = [instance.updated_date for instance in (project, first, second, guide)]
    payload = '{"first":"Replacement","second":"New guide"}'
    if failure == "late_input":
        payload = payload[:-1] + ',"first":"Conflicting duplicate"}'
    else:
        save = models.AnnotationGuide.save

        def fail_after_second_save(instance, *args, **kwargs):
            save(instance, *args, **kwargs)
            if instance.task_id == second.pk:
                raise RuntimeError("Injected failure after the second guide save")

        monkeypatch.setattr(models.AnnotationGuide, "save", fail_after_second_save)
    job = enqueue_task_guides(project, user, queue, "Task Guide JSON 1.0", {"guides.json": payload})
    monkeypatch.setattr(rq, "get_current_job", lambda: job)
    expected = CvatImportError if failure == "late_input" else RuntimeError
    with pytest.raises(expected):
        job.func(*job.args, **job.kwargs)
    for instance in (project, first, second, guide):
        instance.refresh_from_db()
    assert guide.markdown == "Keep this guide"
    assert not models.AnnotationGuide.objects.filter(task=second).exists()
    assert [instance.updated_date for instance in (project, first, second, guide)] == before


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


@pytest.fixture
def dicom_enabled(monkeypatch):
    from cvat.apps.redis_handler.apps import initialize_mappings

    # App startup is isolated above; initialize the real request-ID registry
    # without starting its external services.
    initialize_mappings()
    monkeypatch.setenv("CVAT_DICOM_IMPORT_ENABLED", "1")
    monkeypatch.setattr(IMPORT_FORMATS["DICOM 1.0"], "ENABLED", True)


def dicom_request(project, user, operation, body=None, **query):
    import json

    path = (
        reverse("project-dataset", kwargs={"pk": project.pk})
        + "?"
        + urlencode(
            {"format": "DICOM 1.0", "location": "local", "dicom_upload": operation, **query}
        )
    )
    raw = operation == "chunk"
    return dispatch(
        "post",
        path,
        user,
        data=body if raw else json.dumps(body or {}).encode(),
        content_type="application/octet-stream" if raw else "application/json",
    )


def dicom_session(project, user, payload=b"synthetic DICOM", *, upload_id=None):
    from components.dicom_import.storage import UploadSession

    upload_id = upload_id or str(uuid4())
    response = dicom_request(
        project,
        user,
        "start",
        {
            "upload_id": upload_id,
            "prefix": "synthetic",
            "file_count": 1,
            "total_bytes": len(payload),
        },
    )
    assert response.status_code == 201, response.data
    response = dicom_request(
        project,
        user,
        "files",
        {
            "upload_id": upload_id,
            "start_index": 0,
            "files": [{"path": "nested/test.dcm", "size": len(payload)}],
        },
    )
    assert response.status_code == 200, response.data
    session = UploadSession.for_project(
        project.get_tmp_dirname(),
        upload_id,
        user_id=user.pk,
        project_id=project.pk,
        organization_id=project.organization_id,
    )
    return upload_id, session


def test_dicom_folder_route_streams_raw_chunks_and_uses_native_request_metadata(
    project,
    user,
    queue,
    policy,
    dicom_enabled,
    monkeypatch,
):
    from django.http import HttpRequest

    from components.dicom_import.upload import import_staged_dataset

    monkeypatch.setenv("CVAT_DICOM_IMPORT_TIMEOUT", "12345")
    payload = b"synthetic DICOM payload"
    upload_id, session = dicom_session(project, user, payload)
    # The raw-body parser must leave the stream unread until the permission has
    # been checked; a request.body access would materialize the complete chunk.
    with monkeypatch.context() as raw_chunks:
        raw_chunks.setattr(
            HttpRequest,
            "body",
            property(lambda request: pytest.fail("request.body must not be used")),
        )
        for offset, content in ((0, payload[:8]), (8, payload[8:]), (8, payload[8:])):
            response = dicom_request(
                project, user, "chunk", content, upload_id=upload_id, file_index=0, offset=offset
            )
            assert response.status_code == 200, response.data
            assert response.data == {"offset": offset + len(content)}
    assert (session.directory / "files" / "00000000.dcm").read_bytes() == payload
    assert queue.count == 0
    response = dicom_request(project, user, "finish", {"upload_id": upload_id})
    assert response.status_code == 202, response.data
    job = queue.fetch_job(response.data["rq_id"])
    assert job.func is import_staged_dataset
    assert job.args == (str(session.directory / "import.json"), project.pk, "DICOM 1.0", True)
    assert job.timeout == 12345
    assert not job.retries_left
    assert job.meta["project_id"] == project.pk
    assert job.meta["user"]["id"] == user.pk
    assert job.meta["request_manager_cls"] == "components.dicom_import.upload.DicomFolderImporter"
    assert all(p.scope == ProjectPermission.Scopes.IMPORT_DATASET for p in policy.permissions)
    parsed, _ = ImportRequestId.parse(job.id)
    assert parsed.target == "project" and parsed.subresource == "dataset"
    assert parsed.id is not None and parsed.user_id == user.pk


@pytest.mark.parametrize("rejection", ["anonymous", "denied", "disabled"])
def test_dicom_folder_start_rejections_do_not_create_upload_files(
    project,
    user,
    queue,
    policy,
    dicom_enabled,
    monkeypatch,
    rejection,
):
    if rejection == "denied":
        policy.allow = False
    if rejection == "disabled":
        monkeypatch.setenv("CVAT_DICOM_IMPORT_ENABLED", "0")
    response = dicom_request(
        project,
        None if rejection == "anonymous" else user,
        "start",
        {
            "upload_id": str(uuid4()),
            "prefix": "synthetic",
            "file_count": 1,
            "total_bytes": 4,
        },
    )
    assert response.status_code == {"anonymous": 401, "denied": 403, "disabled": 404}[rejection]
    assert not list(project.get_tmp_dirname().glob("dicom-upload-*"))
    assert queue.count == 0


@pytest.mark.parametrize("operation", ["chunk", "finish", "cancel", "status"])
def test_dicom_session_owner_is_checked_even_with_project_access(
    project,
    user,
    queue,
    dicom_enabled,
    operation,
):
    upload_id, session = dicom_session(project, user, b"data")
    other = User.objects.create(username="other-dicom-uploader", is_active=True)
    response = (
        dicom_request(
            project, other, operation, b"data", upload_id=upload_id, file_index=0, offset=0
        )
        if operation == "chunk"
        else dicom_request(project, other, operation, {"upload_id": upload_id})
    )
    assert response.status_code == 404
    assert session.read()["state"] == "uploading"
    assert queue.count == 0


def test_dicom_import_permission_is_rechecked_before_writing_each_chunk(
    project, user, queue, policy, dicom_enabled
):
    upload_id, session = dicom_session(project, user, b"data")
    policy.allow = False
    response = dicom_request(
        project, user, "chunk", b"data", upload_id=upload_id, file_index=0, offset=0
    )
    assert response.status_code == 403
    assert list((session.directory / "files").iterdir()) == []
    assert queue.count == 0


def test_dicom_incomplete_upload_cannot_be_queued_and_zero_byte_files_can(
    project, user, queue, dicom_enabled
):
    upload_id, session = dicom_session(project, user, b"data")
    response = dicom_request(project, user, "finish", {"upload_id": upload_id})
    assert response.status_code == 409
    assert queue.count == 0
    empty_id, empty_session = dicom_session(project, user, b"")
    response = dicom_request(project, user, "finish", {"upload_id": empty_id})
    assert response.status_code == 202
    assert (empty_session.directory / "files" / "00000000.dcm").stat().st_size == 0
    assert session.read()["state"] == "uploading"


def test_dicom_finish_is_idempotent_after_lost_response_and_native_job_expiry(
    project,
    user,
    queue,
    dicom_enabled,
    monkeypatch,
):
    from components.dicom_import.upload import DicomFolderImporter, _terminal_cleanup

    upload_id, session = dicom_session(project, user, b"")
    original = DicomFolderImporter.enqueue_job

    def lose_response(self):
        original(self)
        raise ConnectionError("synthetic response loss")

    monkeypatch.setattr(DicomFolderImporter, "enqueue_job", lose_response)
    with pytest.raises(ConnectionError, match="synthetic response loss"):
        dicom_request(project, user, "finish", {"upload_id": upload_id})
    assert queue.count == 1
    assert session.read()["state"] == "submitting"
    monkeypatch.setattr(DicomFolderImporter, "enqueue_job", original)
    response = dicom_request(project, user, "status", {"upload_id": upload_id})
    request_id = response.data["rq_id"]
    assert response.status_code == 200 and response.data["state"] == "queued"
    assert dicom_request(project, user, "finish", {"upload_id": upload_id}).data == {
        "rq_id": request_id
    }
    assert queue.count == 1
    job = queue.fetch_job(request_id)
    job.set_status(JobStatus.FINISHED)
    _terminal_cleanup(session, "finished")
    assert not (session.directory / "files").exists()
    # A new session in the same project has a different immutable request ID.
    next_id, _ = dicom_session(project, user, b"")
    next_response = dicom_request(project, user, "finish", {"upload_id": next_id})
    assert next_response.status_code == 202
    assert next_response.data["rq_id"] != request_id
    job.delete()
    before = queue.count
    assert dicom_request(project, user, "finish", {"upload_id": upload_id}).data == {
        "rq_id": request_id
    }
    assert queue.count == before
    assert queue.fetch_job(request_id) is None


def test_dicom_uncertain_finish_can_recover_when_enqueue_never_happened(
    project,
    user,
    queue,
    dicom_enabled,
    monkeypatch,
):
    from components.dicom_import.upload import DicomFolderImporter

    upload_id, session = dicom_session(project, user, b"")
    original = DicomFolderImporter.enqueue_job
    monkeypatch.setattr(
        DicomFolderImporter,
        "enqueue_job",
        lambda self: (_ for _ in ()).throw(ConnectionError("before enqueue")),
    )
    with pytest.raises(ConnectionError):
        dicom_request(project, user, "finish", {"upload_id": upload_id})
    assert queue.count == 0
    response = dicom_request(project, user, "status", {"upload_id": upload_id})
    assert response.data["state"] == "uploading" and "rq_id" not in response.data
    monkeypatch.setattr(DicomFolderImporter, "enqueue_job", original)
    assert dicom_request(project, user, "finish", {"upload_id": upload_id}).status_code == 202
    assert queue.count == 1


def test_dicom_cancel_removes_unsubmitted_data_but_preserves_accepted_imports(
    project, user, queue, dicom_enabled
):
    upload_id, session = dicom_session(project, user, b"data")
    assert (
        dicom_request(
            project, user, "chunk", b"data", upload_id=upload_id, file_index=0, offset=0
        ).status_code
        == 200
    )
    assert dicom_request(project, user, "cancel", {"upload_id": upload_id}).status_code == 204
    assert not (session.directory / "files").exists()
    assert session.read()["state"] == "cancelled"
    assert dicom_request(project, user, "finish", {"upload_id": upload_id}).status_code == 409
    next_id, accepted = dicom_session(project, user, b"")
    assert dicom_request(project, user, "finish", {"upload_id": next_id}).status_code == 202
    assert dicom_request(project, user, "cancel", {"upload_id": next_id}).status_code == 409
    assert (accepted.directory / "files").exists()


def test_dicom_native_cleanup_preserves_active_uploads_and_queued_jobs(
    project, user, queue, dicom_enabled
):
    import os
    import time

    from components.dicom_import.storage import session_lock
    from cvat.apps.dataset_manager.cron import InstanceTmpDirectoriesCleaner

    upload_id, session = dicom_session(project, user, b"")
    expired = time.time() - 7 * 86400
    cleaner = InstanceTmpDirectoriesCleaner()
    os.utime(session.directory, (expired, expired))
    with session_lock(session.directory, shared=True):
        assert cleaner._remove_if_outdated(session.directory) is False
    response = dicom_request(project, user, "finish", {"upload_id": upload_id})
    os.utime(session.directory, (expired, expired))
    assert cleaner._remove_if_outdated(session.directory) is False
    assert session.directory.exists()
    job = queue.fetch_job(response.data["rq_id"])
    job.set_status(JobStatus.FAILED)
    os.utime(session.directory, (expired, expired))
    assert cleaner._remove_if_outdated(session.directory) is True
    assert not session.directory.exists()


@pytest.mark.parametrize("damage", ["lock", "metadata"])
def test_dicom_native_cleanup_removes_damaged_stale_sessions(project, user, dicom_enabled, damage):
    import os
    import time

    from cvat.apps.dataset_manager.cron import InstanceTmpDirectoriesCleaner

    _upload_id, session = dicom_session(project, user, b"")
    if damage == "lock":
        (session.directory / ".lock").unlink()
    else:
        session.state_path.write_text("{", encoding="utf-8")
    cleaner = InstanceTmpDirectoriesCleaner()
    assert cleaner._remove_if_outdated(session.directory) is False
    expired = time.time() - 7 * 86400
    os.utime(session.directory, (expired, expired))
    assert cleaner._remove_if_outdated(session.directory) is True
    assert not session.directory.exists()


@pytest.mark.parametrize("frame_count", [1, 4])
def test_dicom_staged_worker_reads_uploaded_files_and_creates_real_task(
    project,
    user,
    queue,
    dicom_enabled,
    tmp_path,
    monkeypatch,
    frame_count,
):
    import numpy as np
    from django.core.cache import caches

    from components.dicom_import.tests.test_integration import save_dicom
    from components.dicom_import.upload import import_staged_dataset
    from cvat.apps.engine.cache import MediaCache

    # Keep the real preview generation; replace only its external cache/lock.
    caches["default"].clear()
    monkeypatch.setattr(MediaCache, "_cache", staticmethod(lambda: caches["default"]))
    monkeypatch.setattr("cvat.apps.engine.cache.get_rq_lock_for_job", lambda *args: nullcontext())

    shape = (16, 16) if frame_count == 1 else (frame_count, 16, 16)
    source = save_dicom(
        tmp_path / "synthetic.dcm", 1, np.arange(frame_count * 256, dtype=np.uint16).reshape(shape)
    )
    payload = source.read_bytes()
    upload_id, session = dicom_session(project, user, payload)
    response = dicom_request(
        project, user, "chunk", payload, upload_id=upload_id, file_index=0, offset=0
    )
    assert response.status_code == 200
    accepted = dicom_request(project, user, "finish", {"upload_id": upload_id})
    job = queue.fetch_job(accepted.data["rq_id"])
    monkeypatch.setattr(rq, "get_current_job", lambda: job)
    import_staged_dataset(*job.args)
    task = project.tasks.get()
    assert task.data.size == frame_count
    assert task.data.images.count() == (1 if frame_count == 1 else 0)
    if frame_count > 1:
        assert (task.data.video.width, task.data.video.height) == (16, 16)
    assert task.data.get_upload_dirname().exists()
    assert (
        models.LabeledImage.objects.filter(job__segment__task=task, label__name="DicomTag").count()
        == frame_count
    )
    assert session.read()["state"] == "finished"
    assert not (session.directory / "files").exists()
    assert not (session.directory / "batches").exists()
    assert dicom_request(project, user, "finish", {"upload_id": upload_id}).data == accepted.data
    paths = [task.data.get_data_dirname(), task.get_dirname()]
    paths.extend(job.get_dirname() for job in models.Job.objects.filter(segment__task=task))
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)
