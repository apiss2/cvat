# SPDX-License-Identifier: MIT
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from cvat.apps.engine.models import DimensionType, MediaType, Project, Task, ValidationMode

from .models import TaskCalibration
from .validation import (
    MAX_REVISION,
    empty_document,
    restrict_to_segment,
    validate_document,
    validate_update,
)


class CalibrationConflict(APIException):
    status_code = 409
    default_detail = "Calibration was changed by another user. Reload it before saving."
    default_code = "calibration_conflict"


def unavailable_reason(task: Task) -> str | None:
    if not task.data_id or not task.data.size:
        return "Task media has not been initialized"
    if task.dimension != DimensionType.DIM_2D or task.media_type != MediaType.IMAGE:
        return "Physical measurements require a 2D image or video task"
    if task.data.validation_mode == ValidationMode.GT_POOL:
        return "Physical measurements are not supported for tasks using a ground truth pool"
    return None


def get_snapshot(task: Task, *, job=None) -> dict:
    record = TaskCalibration.objects.filter(task_id=task.pk).first()
    reason = unavailable_reason(task)
    document = record.document if record else empty_document()
    if record:
        try:
            document = validate_document(
                document, frame_count=task.data.size if task.data_id else 0
            )
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
    if job and task.data_id:
        document = restrict_to_segment(
            document,
            absolute_frames=job.segment.frame_set,
            start_frame=task.data.start_frame,
            frame_step=task.data.get_frame_step(),
        )
    return {
        **document,
        "revision": record.revision if record else 0,
        "frame_count": task.data.size if task.data_id else 0,
        "supported": reason is None,
        "unavailable_reason": reason,
    }


@transaction.atomic
def replace_calibration(task: Task, payload: dict) -> dict:
    # Lock the parent even when no calibration row exists yet. Every writer takes
    # this lock, so two initial writes cannot both pass expected_revision=0.
    task = Task.objects.select_for_update().get(pk=task.pk)
    if reason := unavailable_reason(task):
        raise ValidationError({"detail": reason})
    try:
        document, expected_revision = validate_update(payload, frame_count=task.data.size)
    except ValueError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    record = TaskCalibration.objects.filter(task_id=task.pk).first()
    revision = record.revision if record else 0
    if revision != expected_revision:
        raise CalibrationConflict()
    if revision == MAX_REVISION:
        raise ValidationError({"detail": "Calibration revision limit reached"})
    TaskCalibration.objects.update_or_create(
        task_id=task.pk,
        defaults={"document": document, "revision": revision + 1},
    )
    # Native task/project backup caches are keyed by their modification times.
    now = timezone.now()
    Task.objects.filter(pk=task.pk).update(updated_date=now)
    if task.project_id:
        Project.objects.filter(pk=task.project_id).update(updated_date=now)
    return get_snapshot(task)
