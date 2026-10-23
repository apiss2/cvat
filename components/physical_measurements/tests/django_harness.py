# SPDX-License-Identifier: MIT
"""Run actual extension ORM/DRF code with minimal engine relations in a subprocess.

This isolates the tests from CVAT's PostgreSQL, Redis and OPA deployment. Engine
view doubles check delegation to get_object; they do not simulate OPA policy.
"""

import io
import json
import sys
import types
import unittest
from types import SimpleNamespace
from zipfile import ZipFile

import django
from django.conf import settings

settings.configure(
    SECRET_KEY="isolated-physical-measurements-tests",
    INSTALLED_APPS=[
        "components.physical_measurements.tests.django_app.apps.EngineTestConfig",
        "components.physical_measurements",
    ],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    USE_TZ=True,
    REST_FRAMEWORK={
        "UNAUTHENTICATED_USER": None,
        "DEFAULT_AUTHENTICATION_CLASSES": [],
        "DEFAULT_PERMISSION_CLASSES": [],
    },
)
django.setup()

from django.db import connection
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory

from components.physical_measurements.models import TaskCalibration
from components.physical_measurements.tests.django_app import models as engine

sys.modules["cvat.apps.engine.models"] = engine

from components.physical_measurements.backup import (
    export_task_calibration,
    import_task_calibration,
)
from components.physical_measurements.service import (
    CalibrationConflict,
    get_snapshot,
    replace_calibration,
)
from components.physical_measurements.validation import MAX_DOCUMENT_BYTES, empty_document


class EngineTaskPermission:
    class Scopes:
        UPDATE_METADATA = "update:metadata"
        VIEW_METADATA = "view:metadata"

    @classmethod
    def _get_scopes(cls, request, view, obj):
        return {("metadata", "GET"): [cls.Scopes.VIEW_METADATA]}[(view.action, request.method)]


class EngineTaskViewSet(viewsets.ViewSet):
    def get_object(self):
        if self.request.headers.get("X-Test-Deny"):
            raise PermissionDenied("Native object check denied access")
        return engine.Task.objects.get(pk=self.kwargs["pk"])


class EngineJobViewSet(EngineTaskViewSet):
    def get_object(self):
        task = super().get_object()
        return SimpleNamespace(segment=SimpleNamespace(task=task, frame_set={103, 109}))


native_views = types.ModuleType("cvat.apps.engine.views")
native_views.TaskViewSet = EngineTaskViewSet
native_views.JobViewSet = EngineJobViewSet
native_permissions = types.ModuleType("cvat.apps.engine.permissions")
native_permissions.TaskPermission = EngineTaskPermission
sys.modules["cvat.apps.engine.views"] = native_views
sys.modules["cvat.apps.engine.permissions"] = native_permissions

from components.physical_measurements.views import (
    CalibrationTaskPermission,
    JobCalibrationViewSet,
    TaskCalibrationViewSet,
)

with connection.schema_editor() as editor:
    for model in (engine.Project, engine.Data, engine.Task, TaskCalibration):
        editor.create_model(model)


class CalibrationFixture:
    def setUp(self):
        engine.Task.objects.all().delete()
        engine.Data.objects.all().delete()
        engine.Project.objects.all().delete()
        self.project = engine.Project.objects.create()
        self.task = engine.Task.objects.create(
            data=engine.Data.objects.create(), project=self.project
        )
        self.document = {
            "version": 1,
            "default": {"x": 0.5, "y": 0.8, "unit": "mm"},
            "frames": {"1": {"x": 1, "y": 2, "unit": "mm"}, "3": None, "5": None},
        }

    def save(self, revision=0, document=None):
        return replace_calibration(
            self.task,
            {**(self.document if document is None else document), "expected_revision": revision},
        )


class PersistenceTests(CalibrationFixture, unittest.TestCase):
    def test_absent_configuration_has_no_units_and_creates_no_row(self):
        result = get_snapshot(self.task)
        self.assertEqual(result["revision"], 0)
        self.assertIsNone(result["default"])
        self.assertEqual(result["frames"], {})
        self.assertEqual(result["frame_count"], 6)
        self.assertTrue(result["supported"])
        self.assertEqual(TaskCalibration.objects.count(), 0)

    def test_replacement_persists_and_invalidates_task_and_project_backup_caches(self):
        old_task_time, old_project_time = self.task.updated_date, self.project.updated_date
        result = self.save()
        self.task.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(result["revision"], 1)
        self.assertEqual(TaskCalibration.objects.get(task=self.task).document, self.document)
        self.assertGreater(self.task.updated_date, old_task_time)
        self.assertGreater(self.project.updated_date, old_project_time)

    def test_stale_writer_cannot_overwrite_newer_configuration(self):
        self.save()
        with self.assertRaises(CalibrationConflict):
            self.save(revision=0, document=empty_document())
        self.assertEqual(TaskCalibration.objects.get(task=self.task).document, self.document)
        self.assertEqual(get_snapshot(self.task)["revision"], 1)

    def test_clear_keeps_revision_history_to_prevent_a_stale_initial_write(self):
        self.save()
        result = self.save(revision=1, document=empty_document())
        self.assertEqual(result["revision"], 2)
        self.assertIsNone(result["default"])
        self.assertEqual(result["frames"], {})
        with self.assertRaises(CalibrationConflict):
            self.save(revision=0)

    def test_invalid_replacement_does_not_change_data_or_timestamps(self):
        self.save()
        self.task.refresh_from_db()
        timestamp = self.task.updated_date
        with self.assertRaises(ValidationError):
            self.save(revision=1, document={**self.document, "frames": {"6": None}})
        self.task.refresh_from_db()
        self.assertEqual(self.task.updated_date, timestamp)
        self.assertEqual(get_snapshot(self.task)["revision"], 1)

    def test_unsupported_tasks_cannot_save(self):
        for field, value in (("dimension", "3d"), ("media_type", "audio"), ("data", None)):
            with self.subTest(field=field):
                original = getattr(self.task, field)
                setattr(self.task, field, value)
                self.task.save()
                self.assertFalse(get_snapshot(self.task)["supported"])
                with self.assertRaises(ValidationError):
                    self.save()
                setattr(self.task, field, original)
                self.task.save()
        self.task.data.validation_mode = "gt_pool"
        self.task.data.save()
        self.assertIn("ground truth pool", get_snapshot(self.task)["unavailable_reason"])
        with self.assertRaises(ValidationError):
            self.save()
        self.assertEqual(TaskCalibration.objects.count(), 0)

    def test_job_view_uses_task_indices_but_exposes_only_job_frames(self):
        self.save()
        job = SimpleNamespace(segment=SimpleNamespace(task=self.task, frame_set={103, 109}))
        result = get_snapshot(self.task, job=job)
        self.assertEqual(result["frames"], {"1": self.document["frames"]["1"], "3": None})
        self.assertEqual(result["default"], self.document["default"])
        self.assertEqual(result["revision"], 1)

    def test_task_deletion_cascades_to_calibration(self):
        self.save()
        self.task.delete()
        self.assertEqual(TaskCalibration.objects.count(), 0)

    def test_backup_round_trip_for_root_and_project_task_members(self):
        self.save()
        for prefix in ("", "task_0"):
            with self.subTest(prefix=prefix), ZipFile(io.BytesIO(), "w") as archive:
                restored = engine.Task.objects.create(data=engine.Data.objects.create())
                export_task_calibration(self.task, archive, prefix)
                import_task_calibration(restored, archive, prefix)
                self.assertEqual(TaskCalibration.objects.get(task=restored).document, self.document)
                self.assertEqual(get_snapshot(restored)["revision"], 1)

    def test_backup_without_config_and_old_backup_remain_empty(self):
        with ZipFile(io.BytesIO(), "w") as archive:
            export_task_calibration(self.task, archive)
            self.assertEqual(archive.namelist(), [])
            archive.writestr("task.json", "{}")
            import_task_calibration(self.task, archive)
        self.assertEqual(TaskCalibration.objects.count(), 0)

    def test_invalid_backup_never_creates_calibration(self):
        for document in (
            {**self.document, "frames": {"100": None}},
            {**self.document, "default": {"x": True, "y": 1, "unit": "mm"}},
            {**self.document, "version": 2},
        ):
            with self.subTest(document=document), ZipFile(io.BytesIO(), "w") as archive:
                archive.writestr("physical_measurements.json", json.dumps(document))
                with self.assertRaises(ValidationError):
                    import_task_calibration(self.task, archive)
        self.assertEqual(TaskCalibration.objects.count(), 0)

    def test_backup_preserves_existing_calibration_in_an_unsupported_task_as_inactive(self):
        self.save()
        self.task.data.validation_mode = "gt_pool"
        self.task.data.save()
        with ZipFile(io.BytesIO(), "w") as archive:
            export_task_calibration(self.task, archive)
            restored = engine.Task.objects.create(
                data=engine.Data.objects.create(validation_mode="gt_pool"),
            )
            import_task_calibration(restored, archive)
        snapshot = get_snapshot(restored)
        self.assertEqual(snapshot["default"], self.document["default"])
        self.assertFalse(snapshot["supported"])
        with self.assertRaises(ValidationError):
            replace_calibration(restored, {**self.document, "expected_revision": 1})


class EndpointTests(CalibrationFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.factory = APIRequestFactory()
        self.task_view = TaskCalibrationViewSet.as_view(
            {"get": "metadata", "put": "metadata"},
            basename="task",
            detail=True,
        )
        self.job_view = JobCalibrationViewSet.as_view(
            {"get": "metadata"},
            basename="job",
            detail=True,
        )

    def test_get_and_put_response_and_revision_conflict(self):
        response = self.task_view(self.factory.get("/"), pk=self.task.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["revision"], 0)
        payload = {**self.document, "expected_revision": 0}
        for expected in (200, 409):
            response = self.task_view(
                self.factory.put("/", payload, format="json"), pk=self.task.pk
            )
            self.assertEqual(response.status_code, expected)

    def test_native_object_check_is_required_for_both_reads_and_writes(self):
        for view, request in (
            (self.task_view, self.factory.get("/", HTTP_X_TEST_DENY="yes")),
            (self.job_view, self.factory.get("/", HTTP_X_TEST_DENY="yes")),
            (
                self.task_view,
                self.factory.put(
                    "/",
                    {**self.document, "expected_revision": 0},
                    format="json",
                    HTTP_X_TEST_DENY="yes",
                ),
            ),
        ):
            self.assertEqual(view(request, pk=self.task.pk).status_code, 403)
        self.assertEqual(TaskCalibration.objects.count(), 0)

    def test_put_has_metadata_update_scope_instead_of_per_field_task_update(self):
        view = SimpleNamespace(action="metadata")
        for method, expected in (("PUT", "update:metadata"), ("GET", "view:metadata")):
            self.assertEqual(
                CalibrationTaskPermission._get_scopes(
                    SimpleNamespace(method=method), view, self.task
                ),
                [expected],
            )

    def test_job_endpoint_filters_overrides_and_does_not_offer_writes(self):
        self.save()
        response = self.job_view(self.factory.get("/"), pk=self.task.pk)
        self.assertEqual(set(response.data["frames"]), {"1", "3"})
        response = self.job_view(self.factory.put("/", {}, format="json"), pk=self.task.pk)
        self.assertEqual(response.status_code, 405)

    def test_malformed_or_oversized_json_never_writes(self):
        for body in (b"[]", b'{"a":1,"a":2}', b" " * (MAX_DOCUMENT_BYTES + 1)):
            request = self.factory.put("/", body, content_type="application/json")
            self.assertEqual(self.task_view(request, pk=self.task.pk).status_code, 400)
        self.assertEqual(TaskCalibration.objects.count(), 0)

    def test_head_is_not_exposed_without_a_native_iam_mapping(self):
        for view in (self.task_view, self.job_view):
            self.assertEqual(view(self.factory.head("/"), pk=self.task.pk).status_code, 405)


if __name__ == "__main__":
    unittest.main(verbosity=2)
