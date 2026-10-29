# SPDX-License-Identifier: MIT
"""Explicit integration harness using real CVAT models, importers, and SQLite.

Run from a checkout with the normal CVAT Python dependencies and fakeredis:
    python components/project_annotations/tests/run_orm_tests.py

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
import unittest
import zipfile
from contextlib import ExitStack
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree.ElementTree import Element, SubElement, tostring

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
_RUNTIME = tempfile.TemporaryDirectory(prefix="cvat-project-annotations-orm-")


def configure():
    os.environ["CVAT_BASE_DIR"] = _RUNTIME.name
    os.environ["DJANGO_SECRET_KEY"] = "isolated-orm-test-only"
    os.environ["CVAT_PROJECT_ANNOTATIONS_ENABLED"] = "1"
    from django.apps import AppConfig
    from django.conf import settings

    base = import_module("cvat.settings.base")
    defaults = {name: value for name, value in vars(base).items() if name.isupper()}
    # Model-only app registration avoids startup dependencies on external
    # deployment services. Every model and annotation method is unmodified.
    names = (
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.sites",
        "rest_framework.authtoken",
        "rest_framework_api_key",
        "allauth",
        "allauth.account",
        "allauth.socialaccount",
        "cvat.apps.iam",
        "cvat.apps.engine",
        "cvat.apps.organizations",
        "cvat.apps.dataset_manager",
        "cvat.apps.webhooks",
        "cvat.apps.access_tokens",
        "cvat.apps.quality_control",
        "cvat.apps.consensus",
    )
    defaults.update(
        INSTALLED_APPS=[],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        LOGGING_CONFIG=None,
        AUTHENTICATION_BACKENDS=["django.contrib.auth.backends.ModelBackend"],
        MIDDLEWARE=[],
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        IGNORE_TMP_FOLDER_CLEANUP_ERRORS=False,
    )
    for name in (
        "DATA_ROOT",
        "MEDIA_DATA_ROOT",
        "CACHE_ROOT",
        "EXPORT_CACHE_ROOT",
        "JOBS_ROOT",
        "TASKS_ROOT",
        "PROJECTS_ROOT",
        "SHARE_ROOT",
        "LOGS_ROOT",
        "MIGRATIONS_LOGS_ROOT",
        "CLOUD_STORAGE_ROOT",
        "TMP_FILES_ROOT",
        "ASSETS_ROOT",
    ):
        defaults[name] = Path(_RUNTIME.name) / name.lower()
        defaults[name].mkdir(parents=True, exist_ok=True)
    settings.configure(**defaults)
    engine_defaults = import_module("cvat.apps.engine.default_settings")
    for name, value in vars(engine_defaults).items():
        if name.isupper():
            setattr(settings, name, value)
    settings.INSTALLED_APPS = [AppConfig(name, import_module(name)) for name in names]
    import django

    django.setup()
    from django.apps import apps
    from django.db import connection

    with connection.schema_editor() as editor:
        for model in apps.get_models():
            editor.create_model(model)


configure()

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


class ProjectAnnotationsORMTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
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
        self.assertIn(f"{count} tasks succeeded; 0 tasks failed", message, repr(self.failures))

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
                with self.subTest(format=format_name, mode=mode):
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
                    self.assertIs(manager._get_payload_file(), uploaded)
                    self.assertEqual(manager.import_args.import_mode, mode or "replace")
                    manager._set_default_callback_params()
                    manager.init_callback_with_params()
                    uploaded_path = manager.tmp_dir / manager.import_args.filename
                    self.assertEqual(uploaded_path.read_bytes(), content.getvalue())
                    self.assertIs(manager.callback, service.import_project_annotations)
                    self.assertEqual(
                        manager.callback_args,
                        (str(uploaded_path), self.project.id, format_name, False),
                    )
                    self.assertEqual(manager.callback_kwargs, {"import_mode": mode or "replace"})
                    request.data = {"dataset_file": dataset_file}
                    with self.assertRaises(ValidationError) as raised:
                        manager._get_payload_file()
                    self.assertIn("annotation_file", raised.exception.detail)

    def test_request_manager_invalid_mode_and_disabled_extension_stop_before_queue(self):
        with patch("django_rq.get_queue") as get_queue:
            for mode in ("merge", "REPLACE", ""):
                with self.subTest(mode=mode):
                    manager = ProjectAnnotationImporter(
                        request=self.request(query={"import_mode": mode}),
                        db_instance=self.project,
                    )
                    with self.assertRaisesRegex(ValidationError, "replace or append"):
                        manager.enqueue_job()
            with patch.dict(os.environ, {"CVAT_PROJECT_ANNOTATIONS_ENABLED": "0"}):
                manager = ProjectAnnotationImporter(
                    request=self.request(), db_instance=self.project
                )
                with self.assertRaisesRegex(MethodNotAllowed, "disabled"):
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
                with self.subTest(format=format_name):
                    manager = ProjectAnnotationImporter(
                        request=self.request(query={"format": format_name}),
                        db_instance=self.project,
                    )
                    with self.assertRaisesRegex(ValidationError, "Import dataset only"):
                        manager.enqueue_job()
            get_queue.assert_not_called()

    def test_request_manager_disables_retries_and_request_id_round_trips(self):
        manager = ProjectAnnotationImporter(request=self.request(), db_instance=self.project)
        self.assertIsNone(manager.job_retry)
        initialize_mappings()
        request_id = manager.build_request_id()
        parsed, queue = ImportRequestId.parse(request_id)
        self.assertEqual(parsed.action, models.RequestAction.IMPORT)
        self.assertEqual(parsed.target, models.RequestTarget.PROJECT)
        self.assertEqual(parsed.target_id, self.project.id)
        self.assertEqual(parsed.subresource, models.RequestSubresource.ANNOTATIONS)
        self.assertEqual(queue, manager.QUEUE_NAME)
        self.assertEqual(parsed.render(), request_id)

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
        self.assertIsNone(manager.import_args.import_mode)
        self.assertIs(manager._get_payload_file(), uploaded)
        manager._set_default_callback_params()
        manager.init_callback_with_params()
        uploaded_path = manager.tmp_dir / manager.import_args.filename
        self.assertEqual(uploaded_path.read_bytes(), content.getvalue())
        self.assertIs(manager.callback, import_dataset_as_project)
        self.assertEqual(
            manager.callback_args,
            (str(uploaded_path), self.project.id, "CVAT 1.1", True),
        )
        self.assertIsNone(manager.callback_kwargs)
        initialize_mappings()
        parsed, _ = ImportRequestId.parse(manager.build_request_id())
        self.assertEqual(parsed.subresource, models.RequestSubresource.DATASET)
        request.data = {"annotation_file": SimpleUploadedFile("annotations.zip", b"unused")}
        with self.assertRaises(ValidationError) as raised:
            manager._get_payload_file()
        self.assertIn("dataset_file", raised.exception.detail)

    def test_native_replace_reimport_preserves_dicom_rows_and_clears_absent_frames(self):
        task = self.task("scan")
        protected = self.protected(task)
        for _ in range(2):
            self.assert_success(self.upload({"scan/annotations.xml": cvat_xml(protected=True)}))
            self.assertEqual(self.protected(task), protected)
            self.assertEqual(
                list(self.tags(task, self.quality).values_list("frame", flat=True)), [0]
            )
            self.assertFalse(self.tags(task, self.old).exists())
            self.assertFalse(models.LabeledShape.objects.filter(job__segment__task=task).exists())

    def test_native_append_reimport_preserves_existing_and_allows_duplicate_tags(self):
        task = self.task("scan")
        protected = self.protected(task)
        for count in (1, 2):
            self.assert_success(
                self.upload({"scan/annotations.xml": cvat_xml(protected=True)}, mode="append")
            )
            self.assertEqual(self.tags(task, self.quality).count(), count)
            self.assertTrue(self.tags(task, self.old).exists())
            self.assertTrue(models.LabeledShape.objects.filter(job__segment__task=task).exists())
            self.assertEqual(self.protected(task), protected)

    def test_native_common_parent_and_nested_native_zip_keep_format_layout(self):
        task = self.task("scan")
        protected = self.protected(task)
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("annotations.xml", cvat_xml())
        self.assert_success(self.upload({"windows-wrapper/scan/native.zip": inner.getvalue()}))
        self.assertEqual(self.tags(task, self.quality).count(), 1)
        self.assertEqual(self.protected(task), protected)

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
        self.assertEqual(set(shapes.values_list("type", flat=True)), {"polygon", "rectangle"})
        self.assertEqual(models.LabeledTrack.objects.filter(job__segment__task=task).count(), 1)
        self.assertEqual(self.protected(task), protected)

    def test_native_invalid_attribute_fails_one_task_and_commits_other(self):
        bad, good = self.task("bad"), self.task("good", sop="1.2.9")
        before = self.state(bad)
        message = self.upload(
            {
                "bad/annotations.xml": cvat_xml(attributes={"quality": "invalid"}),
                "good/annotations.xml": cvat_xml(),
            }
        )
        self.assertIn("1 tasks succeeded; 1 tasks failed", message, repr(self.failures))
        self.assertEqual(self.state(bad), before)
        self.assertEqual(self.tags(good, self.quality).count(), 1)

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
        self.assertIn("1 tasks succeeded; 1 tasks failed", message, repr(self.failures))
        self.assertEqual(self.state(bad), before)
        self.assertEqual(self.tags(good, self.quality).count(), 1)
        self.assertFalse(transaction.get_connection().in_atomic_block)

    def test_native_replace_clears_overlap_jobs_without_recreating_protected_rows(self):
        task = self.task("scan", size=4, overlap=True)
        self.create(task, payload(tags=[{"frame": 2, "label_id": self.old.id, "attributes": []}]))
        self.assertEqual(self.tags(task, self.old).filter(frame=2).count(), 2)
        protected = self.protected(task)
        self.assert_success(self.upload({"scan/annotations.xml": cvat_xml()}))
        self.assertFalse(self.tags(task, self.old).exists())
        self.assertEqual(self.protected(task), protected)

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
        self.assertIn("1 tasks succeeded; 1 tasks failed", message, repr(self.failures))
        self.assertEqual(self.state(bad), before)
        self.assertEqual(self.tags(good, self.quality).count(), 1)

    def test_dicom_video_all_frames_append_duplicate_and_replace_reimport(self):
        task = self.task("video", size=4, video=True)
        protected = self.protected(task)
        record = self.record("1.2.3.4")
        self.assert_success(self.dicom([record], mode="append"))
        self.assertEqual(
            list(self.tags(task, self.quality).order_by("frame").values_list("frame", flat=True)),
            [0, 1, 2, 3],
        )
        before = self.state(task)
        self.assertIn("0 tasks succeeded; 1 tasks failed", self.dicom([record], mode="append"))
        self.assertEqual(self.state(task), before)
        for _ in range(2):
            self.assert_success(self.dicom([record], mode="replace"))
            self.assertEqual(self.tags(task, self.quality).count(), 4)
            self.assertFalse(self.tags(task, self.old).exists())
            self.assertEqual(self.protected(task), protected)

    def test_dicom_ambiguous_pair_does_not_modify_either_task(self):
        first, second = self.task("first"), self.task("second")
        before = self.state(first), self.state(second)
        self.assertIn("0 tasks succeeded; 2 tasks failed", self.dicom([self.record()]))
        self.assertEqual((self.state(first), self.state(second)), before)

    def test_dicom_narrowed_snapshot_retains_whitespace_padded_duplicate_candidates(self):
        first = self.task("first")
        second = self.task("second")
        for value in ("\t1.2.3.4.0\n", " 1.2.3.4.0 "):
            with self.subTest(value=value):
                models.LabeledImageAttributeVal.objects.filter(
                    image__job__segment__task=second,
                    image__frame=0,
                    spec=self.identity_specs["SOPInstanceUID"],
                ).update(value=value)
                candidates = service.snapshots(
                    self.project.id, task_id=first.id, sop_uids={"1.2.3.4.0"}
                )
                self.assertEqual({item.id for item in candidates}, {first.id, second.id})

    def test_dicom_distinct_labels_on_same_image_are_allowed(self):
        task = self.task("scan")
        second = self.record(label="Reviewed")
        second["tags"][0]["attributes"] = {}
        self.assert_success(self.dicom([self.record(), second]))
        self.assertEqual(self.tags(task, self.quality).count(), 1)
        self.assertEqual(self.tags(task, self.other).count(), 1)

    def test_dicom_attribute_and_protected_input_errors_preserve_task(self):
        task = self.task("scan")
        before = self.state(task)
        for record in (
            self.record(attributes={"missing": "value"}),
            self.record(attributes={"contact": "not-a-boolean"}),
            self.record(label="DicomTag"),
        ):
            with self.subTest(record=record):
                self.assertIn("0 tasks succeeded; 1 tasks failed", self.dicom([record]))
                self.assertEqual(self.state(task), before)

    def test_standard_task_import_replace_still_deletes_dicomtag(self):
        task = self.task("scan")
        with transaction.atomic():
            TaskAnnotation(task.id).import_annotations(
                io.BytesIO(cvat_xml()),
                make_importer("CVAT 1.1"),
                import_mode="replace",
            )
        self.assertFalse(self.tags(task, self.protected_label).exists())
        self.assertEqual(self.tags(task, self.quality).count(), 1)

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
        self.assertEqual(self.tags(task, self.quality).count(), 2)
        self.assertEqual(self.protected(task), protected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
