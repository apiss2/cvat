# SPDX-License-Identifier: MIT
"""Unit tests of the adapter with explicit native-CVAT boundary doubles.

These are not database, Redis, exporter integration or browser tests.
"""
import copy
from contextlib import nullcontext
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_filtered_export_selection import selection, FrameFilter

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "filtered_export_tested"


def load(name, replacements):
    path = ROOT / "cvat/apps/dataset_manager/filtered_export" / f"{name}.py"
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(path.parent)]
    modules = {PACKAGE: package, f"{PACKAGE}.selection": selection}
    for key, value in replacements.items():
        modules[key] = value if isinstance(value, types.ModuleType) else types.SimpleNamespace(**value)
    # Include package parents without importing Django's actual application.
    for key in list(modules):
        parts = key.split(".")
        for n in range(1, len(parts)):
            parent = ".".join(parts[:n])
            if parent not in modules:
                module = types.ModuleType(parent)
                module.__path__ = []
                modules[parent] = module
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", path)
    result = importlib.util.module_from_spec(spec)
    modules[spec.name] = result
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(result)
    return result


class ValidationError(Exception):
    pass


class Rows(list):
    def filter(self, **kwargs):
        return self

    def exclude(self, **kwargs):
        return self

    def select_related(self, *args):
        return self

    def order_by(self, *args):
        return self


class Task:
    def __init__(self, pk=1, tags=None):
        self.pk = self.id = pk
        self.name = f"Task {pk}"
        self.updated_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.organization_id = 10
        self.project_id = None
        self.label_set = Rows([types.SimpleNamespace(id=1, pk=1, name="ready", type="tag")])
        self.dimension = "2d"
        self.media_type = "image"
        self.data_id = pk
        self.data = types.SimpleNamespace(size=5, start_frame=100, stop_frame=112,
                                          frame_filter="step=3", deleted_frames=[3], validation_mode=None)
        self.tags = tags if tags is not None else [{"frame": 1, "label_id": 1}]
        self.tracks = [{"shapes": [{"frame": 0, "points": [0, 0, 10, 10]},
                                   {"frame": 4, "points": [4, 4, 14, 14]}]}]


class Project:
    def __init__(self, tasks):
        self.pk = self.id = 20
        self.tasks = Rows(tasks)
        self.label_set = tasks[0].label_set
        self.updated_date = tasks[0].updated_date
        self.organization_id = 10


class TaskData:
    def __init__(self, annotation_ir, db_task, *, included_frames=None, host=""):
        self.ir = annotation_ir
        self.task = db_task
        self.included_frames = included_frames

    def get_included_frames(self):
        frames = set(range(self.task.data.size)) - set(self.task.data.deleted_frames)
        return frames if self.included_frames is None else frames & set(self.included_frames)


class ProjectData:
    def __init__(self, annotation_irs, db_project, host=""):
        self._annotation_irs = annotation_irs
        self._db_tasks = {t.pk: t for t in db_project.tasks}
        self._host = host
        self._tasks_data = {}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task = Task()
        self.project = Project([self.task, Task(2, [{"frame": 2, "label_id": 1}])])
        self.tasks = {t.pk: t for t in self.project.tasks}
        Task.objects = types.SimpleNamespace(get=lambda pk: self.tasks[pk])
        Project.objects = types.SimpleNamespace(get=lambda pk: self.project)
        self.snapshots = []

        def task_annotation(pk):
            task = self.tasks[pk]
            ir = types.SimpleNamespace(tags=copy.deepcopy(task.tags), tracks=copy.deepcopy(task.tracks))
            self.snapshots.append(ir)
            return types.SimpleNamespace(ir_data=ir, init_from_db=lambda **kwargs: None)

        def project_annotation(pk):
            return types.SimpleNamespace(annotation_irs={t.pk: task_annotation(t.pk).ir_data for t in self.project.tasks},
                                         init_from_db=lambda **kwargs: None)

        self.calls = []

        def exporter(stream, temp, data, save_images=False):
            self.calls.append((data, save_images))
            stream.write(b"native exporter stand-in")

        self.formats = {name: types.SimpleNamespace(ENABLED=True) for name in selection.SUPPORTED_FORMATS}
        self.service = load("service", {
            "rest_framework.exceptions": {"ValidationError": ValidationError},
            "cvat.apps.dataset_manager.annotation": {"AnnotationIR": lambda dimension: types.SimpleNamespace(tags=[], tracks=[])},
            "cvat.apps.dataset_manager.bindings": {"TaskData": TaskData, "ProjectData": ProjectData},
            "cvat.apps.dataset_manager.formats.registry": {"EXPORT_FORMATS": self.formats, "make_exporter": lambda name: exporter},
            "cvat.apps.dataset_manager.project": {"ProjectAnnotation": project_annotation},
            "cvat.apps.dataset_manager.task": {"TaskAnnotation": task_annotation},
            "cvat.apps.dataset_manager.util": {
                "ExportCacheManager": types.SimpleNamespace(make_dataset_file_path=lambda **kwargs: str(Path(self.temp.name) / f"{kwargs['instance_type']}-{kwargs['instance_id']}-dataset-instance1-cvat.zip")),
                "TmpDirManager": types.SimpleNamespace(get_tmp_directory_for_export=lambda **kwargs: tempfile.TemporaryDirectory(dir=self.temp.name)),
                "extend_export_file_lifetime": Mock(), "get_export_cache_lock": lambda *args, **kwargs: nullcontext(),
            },
            "cvat.apps.dataset_manager.views": {"EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT": 10, "EXPORT_CACHE_LOCK_TTL": 10},
            "cvat.apps.engine.models": {"Task": Task, "Project": Project},
            "cvat.utils": {"django_database": types.SimpleNamespace(transaction_with_repeatable_read=lambda: lambda fn: fn)},
        })
        self.filter = FrameFilter.parse({"mode": "any", "tag_ids": [1]})

    def args(self, target="task", **changes):
        pk = 1 if target == "task" else 20
        revision = self.service.preview(target, pk, self.filter)["revision"]
        return {"target": target, "pk": pk, "filter_spec": self.filter.to_dict(),
                "expected_revision": revision, "format_name": "CVAT for images 1.1",
                "save_images": True, "omit_tags": False, "signature": "a" * 64, **changes}

    def test_task_relative_frame_selection_and_tracks_preserved(self):
        self.service.export_filtered(**self.args())
        data, images = self.calls[0]
        self.assertEqual(data.get_included_frames(), {1})
        self.assertEqual(data.ir.tracks, self.task.tracks)
        self.assertEqual(data.ir.tags, self.task.tags)
        self.assertTrue(images)

    def test_project_selection_is_separate_per_task(self):
        self.service.export_filtered(**self.args("project"))
        data = self.calls[0][0]
        self.assertEqual(data.init_task_data(1).get_included_frames(), {1})
        self.assertEqual(data.init_task_data(2).get_included_frames(), {2})
        self.assertIn(2, data._tasks_data)

    def test_nonmatching_project_task_remains_empty(self):
        self.tasks[2].tags = []
        self.service.export_filtered(**self.args("project"))
        data = self.calls[0][0]
        self.assertEqual(data.init_task_data(2).get_included_frames(), set())

    def test_explicit_tag_omission_does_not_change_source_or_selection(self):
        self.service.export_filtered(**self.args(format_name="ITGformat 1.0", omit_tags=True))
        data = self.calls[0][0]
        self.assertEqual(data.get_included_frames(), {1})
        self.assertEqual(data.ir.tags, [])
        self.assertEqual(self.task.tags, [{"frame": 1, "label_id": 1}])
        self.assertEqual(len(self.task.tracks[0]["shapes"]), 2)

    def test_itg_requires_explicit_omission(self):
        with self.assertRaises(ValidationError):
            self.service.export_filtered(**self.args(format_name="ITGformat 1.0"))
        self.assertFalse(self.calls)

    def test_unsupported_or_disabled_formats_fail_before_export(self):
        args = self.args()
        for name in ("CVAT for video 1.1", "unknown"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.service.export_filtered(**{**args, "format_name": name})
        self.formats["COCO 1.0"].ENABLED = False
        with self.assertRaises(ValidationError):
            self.service.export_filtered(**{**args, "format_name": "COCO 1.0"})
        del self.formats["COCO 1.0"]
        with self.assertRaises(ValidationError):
            self.service.export_filtered(**{**args, "format_name": "COCO 1.0"})

    def test_stale_preview_rejected_before_enqueue_and_in_worker(self):
        args = self.args()
        self.task.updated_date = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaises(ValidationError):
            self.service.check_revision("task", 1, self.filter, args["expected_revision"])
        with self.assertRaises(ValidationError):
            self.service.export_filtered(**args)
        self.assertFalse(self.calls)

    def test_deleted_frames_and_org_change_invalidate_preview(self):
        for change in (lambda: self.task.data.deleted_frames.append(1),
                       lambda: setattr(self.task, "organization_id", 99)):
            args = self.args()
            change()
            with self.assertRaises(ValidationError):
                self.service.check_revision("task", 1, self.filter, args["expected_revision"])

    def test_new_project_task_invalidates_preview(self):
        args = self.args("project")
        self.project.tasks.append(Task(3))
        with self.assertRaises(ValidationError):
            self.service.check_revision("project", 20, self.filter, args["expected_revision"])

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.preview("task", 1, FrameFilter.parse({"mode": "any", "tag_ids": [99]}))

    def test_special_media_and_gt_pool_are_rejected(self):
        for attr, value in (("dimension", "3d"), ("media_type", "audio"), ("data_id", None)):
            previous = getattr(self.task, attr)
            setattr(self.task, attr, value)
            with self.assertRaises(ValidationError):
                self.service.preview("task", 1, self.filter)
            setattr(self.task, attr, previous)
        self.task.data.validation_mode = "gt_pool"
        with self.assertRaises(ValidationError):
            self.service.preview("task", 1, self.filter)

    def test_empty_selection_preview_allowed_but_export_rejected(self):
        self.task.tags = []
        self.assertEqual(self.service.preview("task", 1, self.filter)["count"], 0)
        with self.assertRaises(ValidationError):
            self.service.export_filtered(**self.args())

    def test_cache_isolated_from_normal_and_other_filtered_exports(self):
        args = self.args()
        first = self.service.export_filtered(**args)
        again = self.service.export_filtered(**args)
        second = self.service.export_filtered(**{**args, "signature": "b" * 64})
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.calls), 2)
        self.service.extend_export_file_lifetime.assert_called_once()
        self.assertTrue(all("-filtered-" in file.name for file in Path(self.temp.name).iterdir()))

    def test_invalid_signature_cannot_escape_cache(self):
        with self.assertRaises(ValidationError):
            self.service.export_filtered(**self.args(signature="../bad"))

    def test_export_failure_cleans_temp_files(self):
        with patch.object(self.service, "make_exporter", return_value=Mock(side_effect=RuntimeError("bad exporter"))):
            with self.assertRaises(RuntimeError):
                self.service.export_filtered(**self.args())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
