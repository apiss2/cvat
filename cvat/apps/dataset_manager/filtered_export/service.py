# SPDX-License-Identifier: MIT
"""Frame selection and native export are separate read-only passes."""

import re
import shutil
from pathlib import Path

from rest_framework.exceptions import ValidationError

from cvat.apps.dataset_manager.annotation import AnnotationIR, AnnotationManager
from cvat.apps.dataset_manager.bindings import ProjectData, TaskData
from cvat.apps.dataset_manager.formats.registry import EXPORT_FORMATS, make_exporter
from cvat.apps.dataset_manager.project import ProjectAnnotation
from cvat.apps.dataset_manager.task import TaskAnnotation
from cvat.apps.dataset_manager.util import (
    ExportCacheManager, TmpDirManager, extend_export_file_lifetime, get_export_cache_lock,
)
from cvat.apps.dataset_manager.views import (
    EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT, EXPORT_CACHE_LOCK_TTL,
)
from cvat.apps.engine.models import Project, Task
from cvat.utils import django_database as db_utils

from .selection import AnnotationFilter, fingerprint

# These exporters consume the native image/frame iterator. This is an adapter
# capability list, NOT a rule for selecting or removing annotation types.
# CVAT for video, for example, accesses whole tracks instead of selected images.
IMAGE_EXPORT_FORMATS = (
    "CVAT for images 1.1", "COCO 1.0", "YOLO 1.1",
    "Ultralytics YOLO Detection 1.0", "Ultralytics YOLO Segmentation 1.0",
    "Ultralytics YOLO Oriented Bounding Boxes 1.0", "Ultralytics YOLO Pose 1.0",
    "ITGformat 1.0",
)


class FilteredProjectData(ProjectData):
    """Reuse project filename disambiguation, subsets and frame offsets."""

    def __init__(self, *args, selected_frames, **kwargs):
        self.selected_frames = selected_frames
        super().__init__(*args, **kwargs)

    def init_task_data(self, task_id):
        data = TaskData(
            annotation_ir=self._annotation_irs[task_id], db_task=self._db_tasks[task_id],
            host=self._host, included_frames=self.selected_frames[task_id],
        )
        self._tasks_data[task_id] = data
        return data


def get_instance(target, pk):
    if target not in ("task", "project"):
        raise ValidationError("Only tasks and projects are supported")
    return (Task if target == "task" else Project).objects.get(pk=pk)


def get_labels(instance):
    owner = instance.project if isinstance(instance, Task) and instance.project_id else instance

    def serialize(label):
        return {
            "id": label.pk, "name": label.name, "type": label.type, "sublabels": [],
            "attributes": [{"id": a.pk, "name": a.name, "input_type": a.input_type,
                            "default_value": a.default_value, "values": a.values.splitlines()}
                           for a in label.attributespec_set.all()],
        }

    return [{**serialize(label), "sublabels": [serialize(child) for child in label.sublabels.all()]}
            for label in owner.label_set.filter(parent=None).prefetch_related(
                "attributespec_set", "sublabels__attributespec_set",
            ).order_by("id")]


def get_options(instance):
    return {
        "formats": [name for name in IMAGE_EXPORT_FORMATS
                    if name in EXPORT_FORMATS and EXPORT_FORMATS[name].ENABLED],
        "labels": get_labels(instance),
    }


def _tasks(instance):
    tasks = [instance] if isinstance(instance, Task) else list(
        instance.tasks.exclude(data=None).select_related("data").order_by("id")
    )
    if not tasks:
        raise ValidationError("The export scope contains no tasks with media")
    for task in tasks:
        if task.data_id is None or task.dimension != "2d" or task.media_type != "image":
            raise ValidationError(f"Task {task.pk} does not contain 2D image/video data")
        if task.data.validation_mode == "gt_pool":
            raise ValidationError("GT-pool tasks are not supported by filtered export")
    return tasks


def source_revision(instance, tasks, labels, filters):
    return fingerprint({
        "instance": [instance.__class__.__name__, instance.pk,
                     instance.updated_date.isoformat(), instance.organization_id],
        "labels": labels, "filters": filters.to_list(),
        "tasks": [[task.pk, task.updated_date.isoformat(), task.data_id,
                   task.data.size, task.data.start_frame, task.data.stop_frame,
                   task.data.frame_filter, sorted(task.data.deleted_frames)] for task in tasks],
    })


def _validate_filter(instance, filters):
    labels = get_labels(instance)
    try:
        filters.validate(labels)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return _tasks(instance), labels


@db_utils.transaction_with_repeatable_read()
def check_revision(target, pk, filters, expected_revision):
    instance = get_instance(target, pk)
    tasks, labels = _validate_filter(instance, filters)
    if expected_revision != source_revision(instance, tasks, labels, filters):
        raise ValidationError("Data or filter changed after preview. Preview the selection again.")


def _plan(instance, filters):
    tasks, labels = _validate_filter(instance, filters)
    revision = source_revision(instance, tasks, labels, filters)
    selected, counts = {}, []
    for task in tasks:
        available = TaskData(AnnotationIR(task.dimension), task).get_included_frames()
        if not filters.objects and not filters.elements:
            selected[task.pk] = available
        else:
            annotation = TaskAnnotation(task.pk)
            annotation.init_from_db(streaming=True)
            # This pass consumes the shape stream for matching only. Native
            # interpolation supplies frame-local track geometry and attributes.
            shapes = AnnotationManager(annotation.ir_data, dimension=task.dimension).to_shapes(
                task.data.size, included_frames=available, deleted_frames=task.data.deleted_frames,
                include_outside=False, use_server_track_ids=True,
            )
            selected[task.pk] = filters.select(available, annotation.ir_data.tags, shapes, labels)
        counts.append({"id": task.pk, "name": task.name,
                       "count": len(selected[task.pk]), "total": len(available)})
    return selected, counts, revision


@db_utils.transaction_with_repeatable_read()
def preview(target, pk, filters):
    _, counts, revision = _plan(get_instance(target, pk), filters)
    return {"tasks": counts, "count": sum(row["count"] for row in counts), "revision": revision}


@db_utils.transaction_with_repeatable_read()
def export_filtered(*, target, pk, filters_spec, expected_revision, format_name,
                    save_images, signature, server_url=""):
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValidationError("Invalid export signature")
    if (format_name not in IMAGE_EXPORT_FORMATS or format_name not in EXPORT_FORMATS
            or not EXPORT_FORMATS[format_name].ENABLED):
        raise ValidationError("Unsupported export format")
    instance = get_instance(target, pk)
    filters = AnnotationFilter.parse(filters_spec)
    selected, counts, revision = _plan(instance, filters)
    if revision != expected_revision:
        raise ValidationError("Data or filter changed after preview. Preview the selection again.")
    if not any(row["count"] for row in counts):
        raise ValidationError("No frames match the selected conditions")

    base = Path(ExportCacheManager.make_dataset_file_path(
        instance_type=target, instance_id=pk, instance_timestamp=instance.updated_date.timestamp(),
        save_images=save_images, format_name=format_name,
    ))
    output = base.with_name(f"{base.stem}-filtered-{signature}{base.suffix}")
    lock_args = {"ttl": EXPORT_CACHE_LOCK_TTL,
                 "acquire_timeout": EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT}
    with get_export_cache_lock(output, **lock_args):
        if output.exists():
            extend_export_file_lifetime(output)
            return str(output)

    # Reload within the SAME repeatable-read snapshot, since selection consumed
    # its annotation stream. Do not delete tags, shapes or track keyframes here.
    if isinstance(instance, Task):
        annotation = TaskAnnotation(pk)
        annotation.init_from_db(streaming=True)
        data = TaskData(annotation.ir_data, instance, host=server_url, included_frames=selected[pk])
    else:
        annotation = ProjectAnnotation(pk)
        annotation.init_from_db(streaming=True)
        data = FilteredProjectData(annotation.annotation_irs, instance, host=server_url,
                                   selected_frames=selected)
    with TmpDirManager.get_tmp_directory_for_export(instance_type=target) as temp:
        result, work = Path(temp) / "result.zip", Path(temp) / "data"
        work.mkdir()
        with result.open("wb") as stream:
            make_exporter(format_name)(stream, str(work), data, save_images=save_images)
        with get_export_cache_lock(output, **lock_args):
            shutil.move(str(result), str(output))
    return str(output)
