# SPDX-License-Identifier: MIT
"""Optional task-backup sidecar, also used by nested project task backups."""

import json
from zipfile import ZipFile

from rest_framework.exceptions import ValidationError

from .archive import backup_member, read_backup_document
from .models import TaskCalibration
from .validation import validate_document


def export_task_calibration(task, archive: ZipFile, prefix: str = "") -> None:
    record = TaskCalibration.objects.filter(task_id=task.pk).first()
    if record is None:
        return
    try:
        document = validate_document(record.document, frame_count=task.data.size)
    except ValueError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    archive.writestr(
        backup_member(prefix),
        json.dumps(document, separators=(",", ":"), allow_nan=False),
    )


def import_task_calibration(task, archive: ZipFile | str, prefix: str | None = None) -> None:
    try:
        if isinstance(archive, str):
            with ZipFile(archive) as opened:
                document = read_backup_document(opened, prefix)
        else:
            document = read_backup_document(archive, prefix)
        if document is None:
            return  # Backups made without this extension retain native behavior.
        # Preserve previously saved calibration even if this task has since
        # switched to an unsupported mode. Reads expose supported=false and the
        # canvas keeps it inactive; a legitimate native backup must still restore.
        document = validate_document(document, frame_count=task.data.size)
    except ValueError as exc:
        raise ValidationError({"detail": str(exc)}) from exc
    TaskCalibration.objects.create(task_id=task.pk, document=document, revision=1)
