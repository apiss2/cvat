# SPDX-License-Identifier: MIT
"""Adapter unit tests with explicit DB, interpolation and exporter test doubles."""
import copy
from contextlib import nullcontext
from datetime import datetime, timezone
import importlib.util
import sys
import tempfile
import types
import pytest
from unittest.mock import Mock, patch

from test_filtered_export_selection import (
    DIRECTORY,
    PACKAGE,
    selection,
    AnnotationFilter,
    LABELS,
    TAGS,
    SHAPES,
    rule,
)


def load(name, replacements):
    modules = {f"{PACKAGE}.selection": selection}
    for key, value in replacements.items():
        modules[key] = (
            value if isinstance(value, types.ModuleType) else types.SimpleNamespace(**value)
        )
    for key in list(modules):
        parts = key.split(".")
        for n in range(1, len(parts)):
            parent = ".".join(parts[:n])
            if parent not in modules and parent != PACKAGE:
                module = types.ModuleType(parent)
                module.__path__ = []
                modules[parent] = module
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", DIRECTORY / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    modules[spec.name] = result
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(result)
    return result


class ValidationError(Exception):
    pass


class Rows(list):
    def exclude(self, **kwargs):
        return self

    def filter(self, **kwargs):
        return self

    def prefetch_related(self, *args):
        return self

    def select_related(self, *args):
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return self


class Task:
    def __init__(self, pk=1):
        self.pk = self.id = pk
        self.name = f"Task {pk}"
        self.updated_date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.organization_id = 10
        self.project_id = None
        self.subset = "test"
        self.status = "annotation"
        self.segment_set = Rows([])
        self.dimension = "2d"
        self.media_type = "image"
        self.data_id = pk
        self.data = types.SimpleNamespace(
            size=5,
            start_frame=100,
            stop_frame=112,
            frame_filter="step=3",
            deleted_frames=[4],
            validation_mode=None,
            get_frame_step=lambda: 3,
        )
        self.tags = copy.deepcopy(TAGS)
        self.shapes = copy.deepcopy(SHAPES[:2])
        self.tracks = [{"id": 300, "shapes": [{"frame": 0}, {"frame": 4}]}]


class Project:
    def __init__(self, tasks):
        self.pk = self.id = 20
        self.tasks = Rows(tasks)
        self.updated_date = tasks[0].updated_date
        self.organization_id = 10


class TaskData:
    def __init__(self, annotation_ir, db_task, *, included_frames=None, host=""):
        self.ir, self.task, self.included_frames = annotation_ir, db_task, included_frames

    def get_included_frames(self):
        available = set(range(self.task.data.size)) - set(self.task.data.deleted_frames)
        return available if self.included_frames is None else available & set(self.included_frames)


class ProjectData:
    def __init__(self, annotation_irs, db_project, host=""):
        self._annotation_irs = annotation_irs
        self._db_tasks = {t.pk: t for t in db_project.tasks}
        self._host = host
        self._tasks_data = {}


class TestService:
    @pytest.fixture(autouse=True)
    def setup_context(self, tmp_path):
        self.temp = tmp_path
        self.task = Task()
        self.project = Project([self.task, Task(2)])
        self.tasks = {t.pk: t for t in self.project.tasks}
        self.tasks[2].tags = [dict(TAGS[0], frame=1)]
        Task.objects = types.SimpleNamespace(get=lambda pk: self.tasks[pk])
        Project.objects = types.SimpleNamespace(get=lambda pk: self.project)
        self.loads = []
        self.interpolation_calls = []
        self.calls = []

        def task_annotation(pk):
            task = self.tasks[pk]
            ir = types.SimpleNamespace(
                tags=copy.deepcopy(task.tags),
                tracks=copy.deepcopy(task.tracks),
                shapes=iter(copy.deepcopy(task.shapes)),
            )
            self.loads.append(ir)
            return types.SimpleNamespace(ir_data=ir, init_from_db=lambda **kwargs: None)

        def project_annotation(pk):
            return types.SimpleNamespace(
                annotation_irs={t.pk: task_annotation(t.pk).ir_data for t in self.project.tasks},
                init_from_db=lambda **kwargs: None,
            )

        def manager(ir, dimension):
            def to_shapes(end_frame, **kwargs):
                self.interpolation_calls.append((end_frame, kwargs))
                yield from ir.shapes
                if ir.tracks:
                    # Native interpolation is a boundary double here, not reimplemented.
                    yield copy.deepcopy(SHAPES[2])

            return types.SimpleNamespace(to_shapes=to_shapes)

        def exporter(stream, temp, data, save_images=False):
            self.calls.append((data, save_images))
            stream.write(b"native exporter stand-in")

        self.formats = {
            name: types.SimpleNamespace(ENABLED=True)
            for name in ("CVAT for images 1.1", "COCO 1.0", "ITGformat 1.0", "YOLO 1.1")
        }
        self.service = load(
            "service",
            {
                "rest_framework.exceptions": {"ValidationError": ValidationError},
                "cvat.apps.dataset_manager.annotation": {
                    "AnnotationIR": lambda dimension: types.SimpleNamespace(tags=[], tracks=[]),
                    "AnnotationManager": manager,
                },
                "cvat.apps.dataset_manager.bindings": {
                    "TaskData": TaskData,
                    "ProjectData": ProjectData,
                },
                "cvat.apps.dataset_manager.formats.registry": {
                    "EXPORT_FORMATS": self.formats,
                    "make_exporter": lambda name: exporter,
                },
                "cvat.apps.dataset_manager.project": {"ProjectAnnotation": project_annotation},
                "cvat.apps.dataset_manager.task": {"TaskAnnotation": task_annotation},
                "cvat.apps.dataset_manager.util": {
                    "ExportCacheManager": types.SimpleNamespace(
                        make_dataset_file_path=lambda **kw: str(
                            self.temp
                            / f"{kw['instance_type']}-{kw['instance_id']}-{kw['format_name']}.zip"
                        )
                    ),
                    "TmpDirManager": types.SimpleNamespace(
                        get_tmp_directory_for_export=lambda **kw: tempfile.TemporaryDirectory(
                            dir=self.temp
                        )
                    ),
                    "extend_export_file_lifetime": Mock(),
                    "get_export_cache_lock": lambda *args, **kwargs: nullcontext(),
                },
                "cvat.apps.dataset_manager.views": {
                    "EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT": 10,
                    "EXPORT_CACHE_LOCK_TTL": 10,
                },
                "cvat.apps.engine.models": {"Task": Task, "Project": Project},
                "cvat.utils": {
                    "django_database": types.SimpleNamespace(
                        transaction_with_repeatable_read=lambda: lambda fn: fn
                    )
                },
            },
        )
        self.original_get_labels = self.service.get_labels
        self.labels = copy.deepcopy(LABELS)
        self.service.get_labels = lambda instance: copy.deepcopy(self.labels)
        self.filters = AnnotationFilter.parse([rule("type", "tag")])

    def args(self, target="task", **changes):
        pk = 1 if target == "task" else 20
        revision = self.service.preview(target, pk, self.filters)["revision"]
        return {
            "target": target,
            "pk": pk,
            "filters_spec": self.filters.to_list(),
            "task_filter": self.filters.task_filter,
            "job_filter": self.filters.job_filter,
            "expected_revision": revision,
            "format_name": "CVAT for images 1.1",
            "save_images": True,
            "signature": "a" * 64,
            **changes,
        }

    def test_selected_images_keep_all_native_annotations_and_keyframes(self):
        before = copy.deepcopy((self.task.tags, self.task.shapes, self.task.tracks))
        self.service.export_filtered(**self.args())
        data, images = self.calls[0]
        assert data.get_included_frames() == {0, 2}
        assert data.ir.tags == self.task.tags
        assert data.ir.tracks == self.task.tracks
        assert list(data.ir.shapes) == self.task.shapes
        assert (self.task.tags, self.task.shapes, self.task.tracks) == before
        assert images

    def test_fresh_export_stream_after_selection_consumed_its_stream(self):
        args = self.args()
        count = len(self.loads)
        self.service.export_filtered(**args)
        assert len(self.loads) - count == 2
        assert list(self.loads[-2].shapes) == []
        assert list(self.loads[-1].shapes) == self.task.shapes

    def test_itg_receives_identical_annotations_without_filter_special_cases(self):
        args = self.args()
        for name in self.formats:
            self.service.export_filtered(**{**args, "format_name": name})
            data = self.calls[-1][0]
            assert data.get_included_frames() == {0, 2}
            assert data.ir.tags == self.task.tags
            assert data.ir.tracks == self.task.tracks
            assert list(data.ir.shapes) == self.task.shapes

    def test_native_exporter_rejection_is_not_suppressed_or_retried(self):
        exporter = Mock(side_effect=ValueError("native format rejects tags"))
        with patch.object(self.service, "make_exporter", return_value=exporter):
            with pytest.raises(ValueError, match="native format rejects tags"):
                self.service.export_filtered(**self.args(format_name="ITGformat 1.0"))
        exporter.assert_called_once()
        assert exporter.call_args.args[2].ir.tags == self.task.tags
        assert list(self.temp.iterdir()) == []

    def test_native_interpolation_contract_and_dynamic_width(self):
        self.filters = AnnotationFilter.parse(
            [{"and": [rule("type", "track"), rule("width", 25, ">")]}]
        )
        assert self.service.preview("task", 1, self.filters)["count"] == 1
        end, kwargs = self.interpolation_calls[-1]
        assert end == 5
        assert kwargs["included_frames"] == {0, 1, 2, 3}
        assert kwargs["use_server_track_ids"]
        assert not kwargs["include_outside"]
        assert kwargs["deleted_frames"] == [4]

    def test_project_frames_are_separate_and_empty_tasks_stay_empty(self):
        self.service.export_filtered(**self.args("project"))
        data = self.calls[-1][0]
        assert data.init_task_data(1).get_included_frames() == {0, 2}
        assert data.init_task_data(2).get_included_frames() == {1}
        self.tasks[2].tags = []
        self.service.export_filtered(**self.args("project", signature="b" * 64))
        assert self.calls[-1][0].init_task_data(2).get_included_frames() == set()

    def test_selection_is_bound_to_preview_not_only_source(self):
        args = self.args()
        different = AnnotationFilter.parse([rule("type", "shape")])
        with pytest.raises(ValidationError):
            self.service.check_revision("task", 1, different, args["expected_revision"])
        with pytest.raises(ValidationError):
            self.service.export_filtered(**{**args, "filters_spec": different.to_list()})

    def test_revision_detects_data_labels_attributes_and_org_changes(self):
        mutations = [
            lambda: self.task.data.deleted_frames.append(0),
            lambda: setattr(self.task, "updated_date", datetime(2026, 1, 2, tzinfo=timezone.utc)),
            lambda: self.labels[0]["attributes"][0].update(default_value="8"),
            lambda: setattr(self.task, "organization_id", 99),
        ]
        for mutate in mutations:
            args = self.args()
            mutate()
            with pytest.raises(ValidationError):
                self.service.check_revision("task", 1, self.filters, args["expected_revision"])
            with pytest.raises(ValidationError):
                self.service.export_filtered(**args)

    def test_new_project_task_invalidates_preview(self):
        args = self.args("project")
        self.project.tasks.append(Task(3))
        with pytest.raises(ValidationError):
            self.service.check_revision("project", 20, self.filters, args["expected_revision"])

    def test_unknown_fields_fail_before_annotations_are_read(self):
        with pytest.raises(ValidationError):
            self.service.preview("task", 1, AnnotationFilter.parse([rule("attr.ready.missing", 1)]))
        assert not self.loads

    def test_empty_filter_includes_unannotated_images_without_annotation_reads(self):
        self.filters = AnnotationFilter.parse([])
        assert self.service.preview("task", 1, self.filters)["count"] == 4
        assert not self.loads

    def test_zero_matches_cannot_start_output(self):
        self.task.tags = []
        assert self.service.preview("task", 1, self.filters)["count"] == 0
        with pytest.raises(ValidationError):
            self.service.export_filtered(**self.args())
        assert not self.calls

    def test_cache_separation_and_reuse(self):
        args = self.args()
        first = self.service.export_filtered(**args)
        assert first == self.service.export_filtered(**args)
        other = self.service.export_filtered(**{**args, "signature": "b" * 64})
        assert first != other
        assert len(self.calls) == 2
        self.service.extend_export_file_lifetime.assert_called_once()

    def test_unsupported_and_disabled_exporters_and_invalid_signature(self):
        args = self.args()
        for name in ("CVAT for video 1.1", "unknown"):
            with pytest.raises(ValidationError):
                self.service.export_filtered(**{**args, "format_name": name})
        self.formats["COCO 1.0"].ENABLED = False
        with pytest.raises(ValidationError):
            self.service.export_filtered(**{**args, "format_name": "COCO 1.0"})
        with pytest.raises(ValidationError):
            self.service.export_filtered(**{**args, "signature": "../bad"})

    def test_unsupported_media_and_gt_pool_fail(self):
        for attr, value in (("dimension", "3d"), ("media_type", "audio"), ("data_id", None)):
            previous = getattr(self.task, attr)
            setattr(self.task, attr, value)
            with pytest.raises(ValidationError):
                self.service.preview("task", 1, self.filters)
            setattr(self.task, attr, previous)
        self.task.data.validation_mode = "gt_pool"
        with pytest.raises(ValidationError):
            self.service.preview("task", 1, self.filters)

    def test_label_schema_includes_shapes_keypoints_and_attributes(self):
        def orm(label):
            attributes = [
                types.SimpleNamespace(
                    pk=a["id"],
                    name=a["name"],
                    input_type=a["input_type"],
                    default_value=a["default_value"],
                    values="\n".join(a["values"]),
                )
                for a in label["attributes"]
            ]
            return types.SimpleNamespace(
                pk=label["id"],
                name=label["name"],
                type=label["type"],
                attributespec_set=Rows(attributes),
                sublabels=Rows([orm(child) for child in label["sublabels"]]),
            )

        self.task.label_set = Rows([orm(label) for label in self.labels])
        assert self.original_get_labels(self.task) == self.labels
        assert self.service.get_options(self.task)["labels"] == self.labels

    def test_scope_filters_intersect_with_image_filter_and_keep_annotations(self):
        def segment(pk, frames, state, job_type="annotation"):
            return types.SimpleNamespace(
                pk=pk, start_frame=0, stop_frame=4, type="specific_frames",
                frames=frames, frame_set=frames,
                job_set=Rows([types.SimpleNamespace(
                    pk=pk, stage="acceptance", state=state, status="completed", type=job_type,
                )]),
            )

        self.task.segment_set = Rows([
            segment(1, [100, 103, 112], "completed"),
            segment(2, [103, 106], "completed"),
            segment(3, [109], "new"),
            segment(4, [109], "completed", "replica"),
            segment(5, [109], "completed", "ground_truth"),
        ])
        self.tasks[2].subset = "train"
        self.tasks[2].segment_set = Rows([segment(6, [100, 103], "completed")])
        self.filters = AnnotationFilter.parse(
            [], task_filter=rule("subset", "test"), job_filter=rule("state", "completed"),
        )
        preview = self.service.preview("project", 20, self.filters)
        assert [(row["count"], row["total"]) for row in preview["tasks"]] == [(3, 4), (0, 4)]
        assert not self.loads
        self.filters = AnnotationFilter.parse(
            [rule("type", "tag")], task_filter=rule("subset", "test"),
            job_filter=rule("state", "completed"),
        )
        args = self.args("project")
        self.service.export_filtered(**args)
        data = self.calls[-1][0]
        assert data.init_task_data(1).get_included_frames() == {0, 2}
        assert data.init_task_data(2).get_included_frames() == set()
        assert data.init_task_data(1).ir.tags == self.task.tags
        self.task.segment_set[0].job_set[0].state = "new"
        with pytest.raises(ValidationError):
            self.service.check_revision("project", 20, self.filters, args["expected_revision"])
        with pytest.raises(ValidationError):
            self.service.export_filtered(**args)

    def test_scope_revision_detects_subset_change_and_rejects_unknown_fields(self):
        self.filters = AnnotationFilter.parse([], task_filter=rule("subset", "test"))
        args = self.args()
        self.task.subset = "train"
        with pytest.raises(ValidationError):
            self.service.check_revision("task", 1, self.filters, args["expected_revision"])
        for kwargs in ({"task_filter": rule("state", "new")}, {"job_filter": rule("subset", "test")}):
            with pytest.raises(ValidationError):
                self.service.preview("task", 1, AnnotationFilter.parse([], **kwargs))
        assert not self.loads

    def test_revision_does_not_read_jobs_for_excluded_tasks(self):
        self.tasks[2].subset = "train"
        self.filters = AnnotationFilter.parse(
            [], task_filter=rule("subset", "test"), job_filter=rule("state", "completed"),
        )
        with patch.object(self.service, "_annotation_jobs", wraps=self.service._annotation_jobs) as load:
            self.service.preview("project", 20, self.filters)
        load.assert_called_once_with(self.task)

    def test_revision_uses_range_bounds_without_expanding_frames(self):
        class BoundsOnly:
            pk = 10
            start_frame = 0
            stop_frame = 999999
            type = "range"

            @property
            def frames(self):
                raise AssertionError("Range segments need no explicit frame list")

            @property
            def frame_set(self):
                raise AssertionError("Revision must not enumerate a range")

        segment = BoundsOnly()
        job = types.SimpleNamespace(pk=10, stage="annotation", state="completed", status="annotation")
        self.filters = AnnotationFilter.parse([], job_filter=rule("state", "completed"))
        args = (self.project, [self.task], self.labels, self.filters, {self.task.pk: [(segment, job)]})
        first = self.service.source_revision(*args)
        segment.stop_frame += 1
        assert self.service.source_revision(*args) != first
