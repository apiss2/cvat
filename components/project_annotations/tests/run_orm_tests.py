# SPDX-License-Identifier: MIT
"""Explicit integration harness using real CVAT models, importers, and SQLite.

Run from a checkout with the normal CVAT Python dependencies and fakeredis:
    python -m pytest -q components/project_annotations/tests/run_orm_tests.py

The database and filesystem are temporary. Existing model definitions create
the test schema; repository models and migrations are never changed. Startup
signals, HTTP, PostgreSQL row-lock behavior, real Redis workers, and OPA are not
covered. OPA decisions and the current worker are doubled; RQ metadata, the
native CVAT importer, serializers, annotation writers, SQL, and rollback are real.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from components.extensions.tests.cvat_backend import configure_cvat

_RUNTIME = configure_cvat(prefix="cvat-project-annotations-orm-")

import fakeredis
import rq
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.utils import timezone
from rest_framework.exceptions import MethodNotAllowed, ValidationError

from components.project_annotations import DICOM_FORMAT, service
from components.project_annotations.background import ProjectAnnotationImporter
from cvat.apps.dataset_manager.formats.registry import make_importer
from cvat.apps.dataset_manager.project import import_dataset_as_project
from cvat.apps.dataset_manager.task import TaskAnnotation, patch_task_data
from cvat.apps.engine import models
from cvat.apps.engine.background import DatasetImporter
from cvat.apps.engine.rq import ImportRequestId
from cvat.apps.engine.serializers import LabeledDataSerializer
from cvat.apps.iam.models import User
from cvat.apps.redis_handler.apps import initialize_mappings


def payload(*, tags=(), shapes=(), tracks=()):
    return {"tags": list(tags), "shapes": list(shapes), "tracks": list(tracks), "intervals": []}


def cvat_xml(*, frame=0, label="ImageQuality", attributes=None, protected=False):
    root = Element("annotations")
    SubElement(root, "version").text = "1.1"
    image = SubElement(
        root, "image", id=str(frame), name=f"image_{frame:04d}.png", width="100", height="100"
    )
    tag = SubElement(image, "tag", label=label, source="manual")
    for name, value in (attributes or {"quality": "good", "contact": "true"}).items():
        SubElement(tag, "attribute", name=name).text = value
    if protected:
        tag = SubElement(image, "tag", label="DicomTag", source="manual")
        SubElement(tag, "attribute", name="PatientID").text = "must-not-replace"
    return tostring(root, encoding="utf-8", xml_declaration=True)


class TestProjectAnnotationsORM:
    @pytest.fixture(autouse=True)
    def prepare(self, request, subtests):
        self.subtests = subtests
        self.stack = ExitStack()
        request.addfinalizer(self.stack.close)
        self.directory = Path(
            self.stack.enter_context(tempfile.TemporaryDirectory(dir=_RUNTIME.name))
        )
        number = User.objects.count()
        self.user = User.objects.create(
            username=f"importer-{number}", is_active=True, is_superuser=True
        )
        self.project = models.Project.objects.create(name=f"project-{number}", owner=self.user)
        self.protected_label = models.Label.objects.create(
            project=self.project, name="DicomTag", type="tag"
        )
        self.identity_specs = {
            name: models.AttributeSpec.objects.create(
                label=self.protected_label,
                name=name,
                mutable=False,
                input_type="text",
                default_value="",
                values="",
            )
            for name in ("StudyInstanceUID", "SOPInstanceUID", "PatientID", "StudyDate")
        }
        self.quality = models.Label.objects.create(
            project=self.project, name="ImageQuality", type="tag"
        )
        self.quality_specs = {
            "quality": models.AttributeSpec.objects.create(
                label=self.quality,
                name="quality",
                mutable=False,
                input_type="select",
                default_value="good",
                values="good\npoor",
            ),
            "contact": models.AttributeSpec.objects.create(
                label=self.quality,
                name="contact",
                mutable=False,
                input_type="checkbox",
                default_value="true",
                values="",
            ),
        }
        self.old = models.Label.objects.create(project=self.project, name="Old", type="any")
        self.other = models.Label.objects.create(project=self.project, name="Reviewed", type="tag")
        self.job = rq.job.Job(id=f"orm-{number}", connection=fakeredis.FakeRedis())
        self.job.meta = {
            "user": {
                "id": self.user.id,
                "username": self.user.username,
                "email": "",
                "first_name": "",
                "last_name": "",
                "url": "",
            },
            "org_id": None,
            "project_id": self.project.id,
            "request": {"uuid": "orm-test", "timestamp": timezone.now()},
        }
        self.stack.enter_context(patch("rq.get_current_job", return_value=self.job))
        allowed = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=True))
        self.stack.enter_context(
            patch.object(service.ProjectPermission, "create_base_perm", return_value=allowed)
        )
        self.stack.enter_context(
            patch.object(service.TaskPermission, "create_base_perm", return_value=allowed)
        )
        self.failures = []
        self.stack.enter_context(
            patch.object(
                service, "_failed", side_effect=lambda task, exc: self.failures.append((task, exc))
            )
        )

    def task(self, name, *, size=2, video=False, sop="1.2.3.4", overlap=False):
        data = models.Data.objects.create(
            size=size,
            start_frame=0,
            stop_frame=size - 1,
            chunk_size=size,
            image_quality=100,
            original_chunk_type="imageset",
            compressed_chunk_type="imageset",
        )
        task = models.Task.objects.create(
            project=self.project,
            owner=self.user,
            data=data,
            name=name,
            dimension="2d",
            mode="interpolation" if video else "annotation",
            media_type="image",
            overlap=int(overlap),
        )
        if video:
            models.Video.objects.create(data=data, path="video.mp4", width=100, height=100)
        else:
            models.Image.objects.bulk_create(
                [
                    models.Image(
                        data=data,
                        frame=frame,
                        path=f"image_{frame:04d}.png",
                        width=100,
                        height=100,
                    )
                    for frame in range(size)
                ]
            )
        segments = [(0, size - 1)] if not overlap else [(0, size - 2), (size - 2, size - 1)]
        for start, stop in segments:
            segment = models.Segment.objects.create(task=task, start_frame=start, stop_frame=stop)
            models.Job.objects.create(segment=segment)
        tags = []
        for frame in range(size):
            values = {
                "StudyInstanceUID": "1.2.3",
                "SOPInstanceUID": sop if video else f"{sop}.{frame}",
                "PatientID": "P001",
                "StudyDate": "20250101",
            }
            tags.append(
                {
                    "frame": frame,
                    "label_id": self.protected_label.id,
                    "group": 0,
                    "source": "manual",
                    "attributes": [
                        {"spec_id": self.identity_specs[key].id, "value": value}
                        for key, value in values.items()
                    ],
                }
            )
        tags.append(
            {
                "frame": size - 1,
                "label_id": self.old.id,
                "group": 0,
                "source": "manual",
                "attributes": [],
            }
        )
        shape = {
            "frame": size - 1,
            "label_id": self.old.id,
            "group": 0,
            "source": "manual",
            "attributes": [],
            "type": "rectangle",
            "occluded": False,
            "z_order": 0,
            "rotation": 0,
            "points": [0, 0, 10, 10],
        }
        self.create(task, payload(tags=tags, shapes=[shape]))
        return task

    def gt_pool_task(self, name, *, sop="1.2.3.4"):
        task = self.task(name, size=3, sop=sop)
        annotation_job = models.Job.objects.get(segment__task=task)
        models.Segment.objects.filter(pk=annotation_job.segment_id).update(stop_frame=1)
        gt_segment = models.Segment.objects.create(
            task=task,
            start_frame=2,
            stop_frame=2,
            type=models.SegmentType.SPECIFIC_FRAMES,
            frames=[2],
        )
        gt_job = models.Job.objects.create(segment=gt_segment, type=models.JobType.GROUND_TRUTH)
        models.ValidationLayout.objects.create(
            task_data=task.data,
            mode=models.ValidationMode.GT_POOL,
            frames=[2],
            frames_per_job_count=1,
        )
        models.Image.objects.filter(data=task.data, frame=1).update(
            is_placeholder=True, real_frame=2
        )
        # Only the GT source has a saved DICOM identity. Its placeholder still
        # receives tags through the native GT pool preprocessing on creation.
        placeholder_tags = self.tags(task, self.protected_label).filter(frame=1)
        models.LabeledImageAttributeVal.objects.filter(image__in=placeholder_tags).delete()
        placeholder_tags.delete()
        gt_tags = self.tags(task).filter(frame=2)
        models.LabeledImageAttributeVal.objects.filter(image__in=gt_tags).update(job=gt_job)
        gt_tags.update(job=gt_job)
        gt_shapes = models.LabeledShape.objects.filter(job=annotation_job, frame=2)
        models.LabeledShapeAttributeVal.objects.filter(shape__in=gt_shapes).update(job=gt_job)
        gt_shapes.update(job=gt_job)
        return task

    def create(self, task, data):
        serializer = LabeledDataSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        patch_task_data(task.id, serializer.validated_data, "create")

    def protected(self, task):
        tags = models.LabeledImage.objects.filter(
            job__segment__task=task, label=self.protected_label
        )
        return (
            list(tags.order_by("id").values()),
            list(
                models.LabeledImageAttributeVal.objects.filter(image__in=tags)
                .order_by("id")
                .values()
            ),
        )

    def state(self, task):
        return {
            kind: list(model.objects.filter(job__segment__task=task).order_by("id").values())
            for kind, model in (
                ("tags", models.LabeledImage),
                ("tag_attributes", models.LabeledImageAttributeVal),
                ("shapes", models.LabeledShape),
                ("shape_attributes", models.LabeledShapeAttributeVal),
                ("tracks", models.LabeledTrack),
                ("track_attributes", models.LabeledTrackAttributeVal),
            )
        }

    def tags(self, task, label=None):
        query = models.LabeledImage.objects.filter(job__segment__task=task)
        return query.filter(label=label) if label else query

    def upload(self, files, *, mode="replace", format_name="CVAT 1.1"):
        path = self.directory / "upload.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        service.import_project_annotations(
            str(path), self.project.id, format_name, False, import_mode=mode
        )
        return self.job.meta["status"]

    def dicom(self, records, mode="replace"):
        return self.upload(
            {"sample.json": json.dumps({"records": records})}, mode=mode, format_name=DICOM_FORMAT
        )

    def record(self, sop="1.2.3.4.0", *, label="ImageQuality", attributes=None):
        return {
            "study_instance_uid": "1.2.3",
            "sop_instance_uid": sop,
            "tags": [
                {
                    "label": label,
                    "attributes": attributes or {"quality": "good", "contact": "true"},
                }
            ],
        }

    def assert_success(self, message, count=1):
        assert f'{count} tasks succeeded; 0 tasks failed' in message, repr(self.failures)

    def request(self, *, query=None, data=None):
        return SimpleNamespace(
            method="POST",
            user=self.user,
            query_params={"format": "CVAT 1.1", "location": "local", **(query or {})},
            data=data or {},
        )

    def test_request_manager_annotation_upload_modes_and_native_callback(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("scan/annotations.xml", cvat_xml())
        for format_name in ("CVAT 1.1", DICOM_FORMAT):
            for mode in (None, "replace", "append"):
                with self.subtests.test(format=format_name, mode=mode):
                    query = {"format": format_name, "conv_mask_to_poly": "false"}
                    if mode is not None:
                        query["import_mode"] = mode
                    uploaded = SimpleUploadedFile("annotations.zip", content.getvalue())
                    dataset_file = SimpleUploadedFile("dataset.zip", b"unused dataset input")
                    request = self.request(
                        query=query,
                        data={"annotation_file": uploaded, "dataset_file": dataset_file},
                    )
                    manager = ProjectAnnotationImporter(request=request, db_instance=self.project)
                    manager.init_request_args()
                    manager.validate_request()
                    assert manager._get_payload_file() is uploaded
                    assert manager.import_args.import_mode == (mode or 'replace')
                    manager._set_default_callback_params()
                    manager.init_callback_with_params()
                    uploaded_path = manager.tmp_dir / manager.import_args.filename
                    assert uploaded_path.read_bytes() == content.getvalue()
                    assert manager.callback is service.import_project_annotations
                    assert manager.callback_args == (str(uploaded_path), self.project.id, format_name, False)
                    assert manager.callback_kwargs == {'import_mode': mode or 'replace'}
                    request.data = {"dataset_file": dataset_file}
                    with pytest.raises(ValidationError) as raised:
                        manager._get_payload_file()
                    assert 'annotation_file' in raised.value.detail

    def test_request_manager_invalid_mode_and_disabled_extension_stop_before_queue(self):
        with patch("django_rq.get_queue") as get_queue:
            for mode in ("merge", "REPLACE", ""):
                with self.subtests.test(mode=mode):
                    manager = ProjectAnnotationImporter(
                        request=self.request(query={"import_mode": mode}),
                        db_instance=self.project,
                    )
                    with pytest.raises(ValidationError, match='replace or append'):
                        manager.enqueue_job()
            with patch.dict(os.environ, {"CVAT_PROJECT_ANNOTATIONS_ENABLED": "0"}):
                manager = ProjectAnnotationImporter(
                    request=self.request(), db_instance=self.project
                )
                with pytest.raises(MethodNotAllowed, match='disabled'):
                    manager.enqueue_job()
            get_queue.assert_not_called()

    def test_request_manager_rejects_dataset_only_formats_before_queue(self):
        with patch("django_rq.get_queue") as get_queue:
            for format_name in (
                "Task Guide JSON 1.0",
                "Task Guide Markdown Zip 1.0",
                "Task Guide DICOM Report Import 1.0",
                "DICOM 1.0",
            ):
                with self.subtests.test(format=format_name):
                    manager = ProjectAnnotationImporter(
                        request=self.request(query={"format": format_name}),
                        db_instance=self.project,
                    )
                    with pytest.raises(ValidationError, match='Import dataset only'):
                        manager.enqueue_job()
            get_queue.assert_not_called()

    def test_request_manager_disables_retries_and_request_id_round_trips(self):
        manager = ProjectAnnotationImporter(request=self.request(), db_instance=self.project)
        assert manager.job_retry is None
        initialize_mappings()
        request_id = manager.build_request_id()
        parsed, queue = ImportRequestId.parse(request_id)
        assert parsed.action == models.RequestAction.IMPORT
        assert parsed.target == models.RequestTarget.PROJECT
        assert parsed.target_id == self.project.id
        assert parsed.subresource == models.RequestSubresource.ANNOTATIONS
        assert queue == manager.QUEUE_NAME
        assert parsed.render() == request_id

    def _assert_request_callback_removes_input(self, *, failed):
        from cvat.apps.redis_handler.signals import request_failed, request_succeeded

        uploaded = SimpleUploadedFile("annotations.zip", b"uploaded annotations")
        manager = ProjectAnnotationImporter(
            request=self.request(data={"annotation_file": uploaded}), db_instance=self.project
        )
        manager.init_request_args()
        manager.validate_request()
        manager._set_default_callback_params()
        manager.init_callback_with_params()
        manager.init_job_callbacks()
        source = manager.tmp_dir / manager.import_args.filename
        unrelated = manager.tmp_dir / "unrelated-upload.zip"
        unrelated.write_bytes(b"another request")
        job = rq.job.Job.create(
            manager.callback,
            args=manager.callback_args,
            kwargs=manager.callback_kwargs,
            id=manager.build_request_id(),
            connection=self.job.connection,
            origin=manager.QUEUE_NAME,
            meta={
                **self.job.meta,
                "request_manager_cls": (f"{type(manager).__module__}.{type(manager).__qualname__}"),
            },
        )
        signal = request_failed if failed else request_succeeded
        notifications = []

        def observe(sender, **kwargs):
            notifications.append((sender, kwargs["rq_job"], kwargs["status"]))

        signal.connect(observe, sender=ProjectAnnotationImporter)
        try:
            assert source.is_file()
            if failed:
                error = ValueError("test import failure")
                manager.job_on_failure_callback.func(job, job.connection, type(error), error, None)
                assert 'test import failure' in job.meta['formatted_exception']
            else:
                manager.job_on_success_callback.func(job, job.connection, None)
        finally:
            signal.disconnect(observe, sender=ProjectAnnotationImporter)
        assert not source.exists()
        assert unrelated.read_bytes() == b'another request'
        assert notifications == [
            (
                ProjectAnnotationImporter,
                job,
                rq.job.JobStatus.FAILED if failed else rq.job.JobStatus.FINISHED,
            ),
        ]

    def test_request_success_callback_removes_input_and_keeps_completion_signal(self):
        self._assert_request_callback_removes_input(failed=False)

    def test_request_failure_callback_removes_input_and_keeps_failure_metadata(self):
        self._assert_request_callback_removes_input(failed=True)

    def _assert_job_timeout_aborts_remaining_tasks(self, *, dicom):
        from rq.timeouts import JobTimeoutException

        first = self.task("before-timeout", sop="1.2.3.4")
        interrupted = self.task("during-timeout", sop="2.3.4.5")
        remaining = self.task("after-timeout", sop="3.4.5.6")
        protected = self.protected(first)
        before_interrupted = self.state(interrupted)
        before_remaining = self.state(remaining)
        native_apply = service.apply_task
        attempted = []

        def timeout_after_write(annotation, *args, **kwargs):
            attempted.append(annotation.db_task.id)
            native_apply(annotation, *args, **kwargs)
            if annotation.db_task.id == interrupted.id:
                raise JobTimeoutException("test job deadline exceeded")

        with (
            patch.object(service, "apply_task", side_effect=timeout_after_write),
            pytest.raises(JobTimeoutException, match='test job deadline exceeded'),
        ):
            if dicom:
                self.dicom([self.record(sop) for sop in ("1.2.3.4.0", "2.3.4.5.0", "3.4.5.6.0")])
            else:
                self.upload(
                    {
                        f"{task.name}/annotations.xml": cvat_xml()
                        for task in (first, interrupted, remaining)
                    }
                )

        assert attempted == [first.id, interrupted.id]
        assert self.tags(first, self.quality).count() == 1
        assert not self.tags(first, self.old).exists()
        assert self.protected(first) == protected
        assert self.state(interrupted) == before_interrupted
        assert self.state(remaining) == before_remaining
        assert self.failures == []

    def test_native_job_timeout_rolls_back_current_task_and_aborts_remaining_tasks(self):
        self._assert_job_timeout_aborts_remaining_tasks(dicom=False)

    def test_dicom_job_timeout_rolls_back_current_task_and_aborts_remaining_tasks(self):
        self._assert_job_timeout_aborts_remaining_tasks(dicom=True)

    def test_standard_project_dataset_manager_keeps_dataset_upload_and_callback(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("annotations.xml", cvat_xml())
        uploaded = SimpleUploadedFile("dataset.zip", content.getvalue())
        request = self.request(
            query={"import_mode": "append"},
            data={
                "dataset_file": uploaded,
                "annotation_file": SimpleUploadedFile("annotations.zip", b"unused input"),
            },
        )
        manager = DatasetImporter(request=request, db_instance=self.project)
        manager.init_request_args()
        manager.validate_request()
        assert manager.import_args.import_mode is None
        assert manager._get_payload_file() is uploaded
        manager._set_default_callback_params()
        manager.init_callback_with_params()
        uploaded_path = manager.tmp_dir / manager.import_args.filename
        assert uploaded_path.read_bytes() == content.getvalue()
        assert manager.callback is import_dataset_as_project
        assert manager.callback_args == (str(uploaded_path), self.project.id, 'CVAT 1.1', True)
        assert manager.callback_kwargs is None
        initialize_mappings()
        parsed, _ = ImportRequestId.parse(manager.build_request_id())
        assert parsed.subresource == models.RequestSubresource.DATASET
        request.data = {"annotation_file": SimpleUploadedFile("annotations.zip", b"unused")}
        with pytest.raises(ValidationError) as raised:
            manager._get_payload_file()
        assert 'dataset_file' in raised.value.detail

    def test_native_replace_reimport_preserves_dicom_rows_and_clears_absent_frames(self):
        task = self.task("scan")
        protected = self.protected(task)
        for _ in range(2):
            self.assert_success(self.upload({"scan/annotations.xml": cvat_xml(protected=True)}))
            assert self.protected(task) == protected
            assert list(self.tags(task, self.quality).values_list('frame', flat=True)) == [0]
            assert not self.tags(task, self.old).exists()
            assert not models.LabeledShape.objects.filter(job__segment__task=task).exists()

    def test_native_append_reimport_preserves_existing_and_allows_duplicate_tags(self):
        task = self.task("scan")
        protected = self.protected(task)
        for count in (1, 2):
            self.assert_success(
                self.upload({"scan/annotations.xml": cvat_xml(protected=True)}, mode="append")
            )
            assert self.tags(task, self.quality).count() == count
            assert self.tags(task, self.old).exists()
            assert models.LabeledShape.objects.filter(job__segment__task=task).exists()
            assert self.protected(task) == protected

    def test_native_common_parent_and_nested_native_zip_keep_format_layout(self):
        task = self.task("scan")
        protected = self.protected(task)
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("annotations.xml", cvat_xml())
        self.assert_success(self.upload({"windows-wrapper/scan/native.zip": inner.getvalue()}))
        assert self.tags(task, self.quality).count() == 1
        assert self.protected(task) == protected

    def test_native_shapes_and_tracks_use_the_existing_importer(self):
        task = self.task("scan")
        protected = self.protected(task)
        root = Element("annotations")
        SubElement(root, "version").text = "1.1"
        image = SubElement(root, "image", id="0", name="image_0000.png", width="100", height="100")
        SubElement(
            image, "polygon", label="Old", points="0,0;10,0;10,10", occluded="0", z_order="0"
        )
        SubElement(
            image,
            "box",
            label="Old",
            xtl="0",
            ytl="0",
            xbr="10",
            ybr="10",
            occluded="0",
            z_order="0",
        )
        track = SubElement(root, "track", id="0", label="Old")
        for frame in (0, 1):
            SubElement(
                track,
                "box",
                frame=str(frame),
                xtl="0",
                ytl="0",
                xbr="10",
                ybr="10",
                outside="0",
                occluded="0",
                keyframe="1",
                z_order="0",
            )
        self.assert_success(self.upload({"scan/annotations.xml": tostring(root)}))
        shapes = models.LabeledShape.objects.filter(job__segment__task=task)
        assert set(shapes.values_list('type', flat=True)) == {'polygon', 'rectangle'}
        assert models.LabeledTrack.objects.filter(job__segment__task=task).count() == 1
        assert self.protected(task) == protected

    def test_native_invalid_attribute_fails_one_task_and_commits_other(self):
        bad, good = self.task("bad"), self.task("good", sop="1.2.9")
        before = self.state(bad)
        message = self.upload(
            {
                "bad/annotations.xml": cvat_xml(attributes={"quality": "invalid"}),
                "good/annotations.xml": cvat_xml(),
            }
        )
        assert '1 tasks succeeded; 1 tasks failed' in message, repr(self.failures)
        assert self.state(bad) == before
        assert self.tags(good, self.quality).count() == 1

    def test_native_sql_failure_after_create_rolls_back_delete_and_insert(self):
        bad, good = self.task("bad"), self.task("good", sop="1.2.9")
        before = self.state(bad)
        native_create = TaskAnnotation.create

        def fail_after_write(annotation, data):
            native_create(annotation, data)
            if annotation.db_task.id == bad.id:
                raise DatabaseError("injected write failure")

        with patch.object(TaskAnnotation, "create", fail_after_write):
            message = self.upload(
                {
                    "bad/annotations.xml": cvat_xml(),
                    "good/annotations.xml": cvat_xml(),
                }
            )
        assert '1 tasks succeeded; 1 tasks failed' in message, repr(self.failures)
        assert self.state(bad) == before
        assert self.tags(good, self.quality).count() == 1
        assert not transaction.get_connection().in_atomic_block

    def test_native_replace_clears_overlap_jobs_without_recreating_protected_rows(self):
        task = self.task("scan", size=4, overlap=True)
        self.create(task, payload(tags=[{"frame": 2, "label_id": self.old.id, "attributes": []}]))
        assert self.tags(task, self.old).filter(frame=2).count() == 2
        protected = self.protected(task)
        self.assert_success(self.upload({"scan/annotations.xml": cvat_xml()}))
        assert not self.tags(task, self.old).exists()
        assert self.protected(task) == protected

    def test_dicom_invalid_record_aborts_whole_task_but_commits_unrelated_task(self):
        bad, good = self.task("bad"), self.task("good", sop="1.2.9")
        before = self.state(bad)
        message = self.dicom(
            [
                self.record(),
                self.record("1.2.3.4.1", label="Missing"),
                self.record("1.2.9.0"),
            ]
        )
        assert '1 tasks succeeded; 1 tasks failed' in message, repr(self.failures)
        assert self.state(bad) == before
        assert self.tags(good, self.quality).count() == 1

    def test_dicom_video_all_frames_append_duplicate_and_replace_reimport(self):
        task = self.task("video", size=4, video=True)
        protected = self.protected(task)
        record = self.record("1.2.3.4")
        self.assert_success(self.dicom([record], mode="append"))
        assert list(
            self.tags(task, self.quality).order_by('frame').values_list('frame', flat=True)
        ) == [0, 1, 2, 3]
        before = self.state(task)
        assert '0 tasks succeeded; 1 tasks failed' in self.dicom([record], mode='append')
        assert self.state(task) == before
        for _ in range(2):
            self.assert_success(self.dicom([record], mode="replace"))
            assert self.tags(task, self.quality).count() == 4
            assert not self.tags(task, self.old).exists()
            assert self.protected(task) == protected

    def test_dicom_gt_pool_rejects_duplicate_on_native_copy_destination(self):
        bad = self.gt_pool_task("gt-duplicate")
        good = self.task("unrelated", sop="1.2.9")
        self.create(bad, payload(tags=[{"frame": 1, "label_id": self.quality.id}]))
        before = self.state(bad)
        message = self.dicom([self.record("1.2.3.4.2"), self.record("1.2.9.0")], mode="append")
        assert '1 tasks succeeded; 1 tasks failed' in message, repr(self.failures)
        assert self.state(bad) == before
        assert self.tags(good, self.quality).count() == 1

    def test_dicom_gt_pool_copies_once_and_preserves_dicomtag_rows(self):
        task = self.gt_pool_task("gt-valid")
        protected = self.protected(task)
        for mode in ("append", "replace", "replace"):
            with self.subtests.test(mode=mode):
                self.assert_success(self.dicom([self.record("1.2.3.4.2")], mode=mode))
                assert list(
                    self.tags(task, self.quality).order_by('frame').values_list('frame', flat=True)
                ) == [1, 2]
                assert self.protected(task) == protected

    def test_dicom_gt_pool_expansion_limits_release_failed_task_budget(self):
        for index, (constant, limit) in enumerate(
            (("MAX_GENERATED_TAGS", 2), ("MAX_GENERATED_ATTRIBUTE_VALUES", 4))
        ):
            with self.subtests.test(limit=constant):
                bad = self.gt_pool_task(f"gt-limit-{index}", sop=f"1.2.8.{index}")
                good = self.gt_pool_task(f"gt-next-{index}", sop=f"1.2.9.{index}")
                before = self.state(bad)
                protected = self.protected(good)
                with patch.object(service, constant, limit):
                    message = self.dicom(
                        [self.record(f"1.2.8.{index}.2"), self.record(f"1.2.9.{index}.2")]
                    )
                assert '1 tasks succeeded; 1 tasks failed' in message, repr(self.failures)
                assert self.state(bad) == before
                assert self.tags(good, self.quality).count() == 2
                assert self.protected(good) == protected

    def test_dicom_gt_pool_limits_are_checked_before_generating_copies(self):
        for index, (constant, limit) in enumerate(
            (("MAX_GENERATED_TAGS", 1), ("MAX_GENERATED_ATTRIBUTE_VALUES", 2))
        ):
            with self.subtests.test(limit=constant):
                task = self.gt_pool_task(f"gt-preflight-{index}", sop=f"1.2.7.{index}")
                before = self.state(task)
                with (
                    patch.object(service, constant, limit),
                    patch.object(
                        TaskAnnotation,
                        "_preprocess_input_annotations_for_gt_pool_task",
                        side_effect=AssertionError("Expansion must be rejected before copying"),
                    ) as preprocess,
                ):
                    message = self.dicom([self.record(f"1.2.7.{index}.2")])
                assert '0 tasks succeeded; 1 tasks failed' in message
                preprocess.assert_not_called()
                assert self.state(task) == before

    def test_dicom_ambiguous_pair_does_not_modify_either_task(self):
        first, second = self.task("first"), self.task("second")
        before = self.state(first), self.state(second)
        assert '0 tasks succeeded; 2 tasks failed' in self.dicom([self.record()])
        assert (self.state(first), self.state(second)) == before

    def test_dicom_narrowed_snapshot_retains_whitespace_padded_duplicate_candidates(self):
        first = self.task("first")
        second = self.task("second")
        for value in ("\t1.2.3.4.0\n", " 1.2.3.4.0 "):
            with self.subtests.test(value=value):
                models.LabeledImageAttributeVal.objects.filter(
                    image__job__segment__task=second,
                    image__frame=0,
                    spec=self.identity_specs["SOPInstanceUID"],
                ).update(value=value)
                candidates = service.snapshots(
                    self.project.id, task_id=first.id, sop_uids={"1.2.3.4.0"}
                )
                assert {item.id for item in candidates} == {first.id, second.id}

    def test_dicom_distinct_labels_on_same_image_are_allowed(self):
        task = self.task("scan")
        second = self.record(label="Reviewed")
        second["tags"][0]["attributes"] = {}
        self.assert_success(self.dicom([self.record(), second]))
        assert self.tags(task, self.quality).count() == 1
        assert self.tags(task, self.other).count() == 1

    def test_dicom_attribute_and_protected_input_errors_preserve_task(self):
        task = self.task("scan")
        before = self.state(task)
        for record in (
            self.record(attributes={"missing": "value"}),
            self.record(attributes={"contact": "not-a-boolean"}),
            self.record(label="DicomTag"),
        ):
            with self.subtests.test(record=record):
                assert '0 tasks succeeded; 1 tasks failed' in self.dicom([record])
                assert self.state(task) == before

    def test_standard_task_import_replace_still_deletes_dicomtag(self):
        task = self.task("scan")
        with transaction.atomic():
            TaskAnnotation(task.id).import_annotations(
                io.BytesIO(cvat_xml()),
                make_importer("CVAT 1.1"),
                import_mode="replace",
            )
        assert not self.tags(task, self.protected_label).exists()
        assert self.tags(task, self.quality).count() == 1

    def test_standard_task_import_append_still_allows_duplicate_tags(self):
        task = self.task("scan")
        protected = self.protected(task)
        for _ in range(2):
            with transaction.atomic():
                TaskAnnotation(task.id).import_annotations(
                    io.BytesIO(cvat_xml()),
                    make_importer("CVAT 1.1"),
                    import_mode="append",
                )
        assert self.tags(task, self.quality).count() == 2
        assert self.protected(task) == protected
