# SPDX-License-Identifier: MIT
"""Read-only adapter around CVAT's existing dataset bindings and exporters."""

import re
import shutil
from pathlib import Path

from rest_framework.exceptions import ValidationError

from cvat.apps.dataset_manager.annotation import AnnotationIR
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

from .selection import FrameFilter, SUPPORTED_FORMATS, fingerprint


class FilteredProjectData(ProjectData):
    """Reuse project filename disambiguation, subsets and frame offsets unchanged."""

    def __init__(self, *args, selected_frames, **kwargs):
        self.selected_frames = selected_frames
        super().__init__(*args, **kwargs)

    def init_task_data(self, task_id):
        data = TaskData(
            annotation_ir=self._annotation_irs[task_id],
            db_task=self._db_tasks[task_id],
            host=self._host,
            included_frames=self.selected_frames[task_id],
        )
        self._tasks_data[task_id] = data
        return data


def get_instance(target: str, pk: int):
    if target not in ("task", "project"):
        raise ValidationError("Only tasks and projects are supported")
    return (Task if target == "task" else Project).objects.get(pk=pk)


def get_labels(instance):
    owner = instance.project if isinstance(instance, Task) and instance.project_id else instance
    return list(owner.label_set.filter(parent=None).order_by("id"))


def get_options(instance):
    return {
        "formats": [name for name in SUPPORTED_FORMATS
                    if name in EXPORT_FORMATS and EXPORT_FORMATS[name].ENABLED],
        "labels": [{"id": label.id, "name": label.name}
                   for label in get_labels(instance) if label.type in ("tag", "any")],
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
        # Native GT-pool exclusion has version-specific frame-number semantics.
        # Do not silently produce a wrong training set for these special tasks.
        if task.data.validation_mode == "gt_pool":
            raise ValidationError("GT-pool tasks are not supported by filtered export")
    return tasks


def source_revision(instance, tasks, labels) -> str:
    return fingerprint({
        "instance": [instance.__class__.__name__, instance.pk,
                     instance.updated_date.isoformat(), instance.organization_id],
        "labels": [(label.pk, label.name, label.type) for label in labels],
        "tasks": [[task.pk, task.updated_date.isoformat(), task.data_id,
                   task.data.size, task.data.start_frame, task.data.stop_frame,
                   task.data.frame_filter, sorted(task.data.deleted_frames)] for task in tasks],
    })


def _validate_filter(instance, frame_filter):
    labels = get_labels(instance)
    allowed = {label.pk for label in labels if label.type in ("tag", "any")}
    if not set(frame_filter.tag_ids) <= allowed:
        raise ValidationError("A selected tag does not belong to this task/project")
    tasks = _tasks(instance)
    return tasks, labels


@db_utils.transaction_with_repeatable_read()
def check_revision(target, pk, frame_filter, expected_revision):
    instance = get_instance(target, pk)
    tasks, labels = _validate_filter(instance, frame_filter)
    revision = source_revision(instance, tasks, labels)
    if expected_revision != revision:
        raise ValidationError("Data changed after preview. Preview the selection again.")


def _load(instance, frame_filter, host=""):
    tasks, labels = _validate_filter(instance, frame_filter)
    revision = source_revision(instance, tasks, labels)
    if isinstance(instance, Task):
        annotation = TaskAnnotation(instance.pk)
        annotation.init_from_db(streaming=True)
        annotations = {instance.pk: annotation.ir_data}
    else:
        annotation = ProjectAnnotation(instance.pk)
        annotation.init_from_db(streaming=True)
        annotations = annotation.annotation_irs
    selected = {}
    counts = []
    for task in tasks:
        # Use an empty IR for the scope calculation. The actual IR (including
        # complete track keyframes) remains intact for the native interpolation.
        data = TaskData(AnnotationIR(task.dimension), task, host=host)
        available = data.get_included_frames()
        selected[task.pk] = frame_filter.select(available, annotations[task.pk].tags)
        counts.append({"id": task.pk, "name": task.name,
                       "count": len(selected[task.pk]), "total": len(available)})
    return annotations, selected, counts, revision


@db_utils.transaction_with_repeatable_read()
def preview(target, pk, frame_filter):
    instance = get_instance(target, pk)
    _, _, counts, revision = _load(instance, frame_filter)
    return {"tasks": counts, "count": sum(row["count"] for row in counts), "revision": revision}


@db_utils.transaction_with_repeatable_read()
def export_filtered(*, target, pk, filter_spec, expected_revision, format_name,
                    save_images, omit_tags, signature, server_url=""):
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValidationError("Invalid export signature")
    instance = get_instance(target, pk)
    frame_filter = FrameFilter.parse(filter_spec)
    if (format_name not in SUPPORTED_FORMATS or format_name not in EXPORT_FORMATS
            or not EXPORT_FORMATS[format_name].ENABLED):
        raise ValidationError("Unsupported export format")
    if format_name == "ITGformat 1.0" and not omit_tags:
        raise ValidationError("ITGformat cannot represent tags; explicitly enable omit_tags")
    annotations, selected, counts, revision = _load(instance, frame_filter, server_url)
    if revision != expected_revision:
        raise ValidationError("Data changed after preview. Preview the selection again.")
    if not any(row["count"] for row in counts):
        raise ValidationError("No frames match the selected conditions")
    if omit_tags:
        for ir in annotations.values():
            ir.tags = []  # In-memory export snapshot only. Never write annotations to the DB.
    if isinstance(instance, Task):
        data = TaskData(annotations[pk], instance, host=server_url, included_frames=selected[pk])
    else:
        data = FilteredProjectData(
            annotations, instance, host=server_url, selected_frames=selected,
        )
    # Keep the native cache naming convention for the existing cleanup job.
    # The signature includes the criteria, preview revision, format and owner.
    base = Path(ExportCacheManager.make_dataset_file_path(
        instance_type=target, instance_id=pk,
        instance_timestamp=instance.updated_date.timestamp(),
        save_images=save_images, format_name=format_name,
    ))
    output = base.with_name(f"{base.stem}-filtered-{signature}{base.suffix}")
    lock_args = {"ttl": EXPORT_CACHE_LOCK_TTL,
                 "acquire_timeout": EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT}
    with get_export_cache_lock(output, **lock_args):
        if output.exists():
            extend_export_file_lifetime(output)
            return str(output)
    with TmpDirManager.get_tmp_directory_for_export(instance_type=target) as temp:
        result = Path(temp) / "result.zip"
        work = Path(temp) / "data"
        work.mkdir()
        with result.open("wb") as stream:
            make_exporter(format_name)(stream, str(work), data, save_images=save_images)
        with get_export_cache_lock(output, **lock_args):
            shutil.move(str(result), str(output))
    return str(output)
