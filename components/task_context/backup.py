# SPDX-License-Identifier: MIT
"""Optional, bounded metadata in native task and project backup archives."""

import json
from pathlib import PurePosixPath

from rest_framework.exceptions import ValidationError

from .storage import export_context
from .validation import MAX_DOCUMENT_BYTES, validate_context

CONTEXT_FILENAME = "task_context.json"


def write_backup_context(zip_object, task, target_dir=""):
    document = export_context(task)
    if document is not None:
        zip_object.writestr(
            str(PurePosixPath(target_dir, CONTEXT_FILENAME)),
            json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )


def read_backup_context(zip_object, target_dir=""):
    filename = str(PurePosixPath(target_dir, CONTEXT_FILENAME))
    entries = [entry for entry in zip_object.infolist() if entry.filename == filename]
    if not entries:
        return None
    if len(entries) != 1 or entries[0].file_size > MAX_DOCUMENT_BYTES:
        raise ValidationError("Invalid task context backup file.")

    with zip_object.open(entries[0]) as stream:
        content = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValidationError("Task context backup exceeds 64 KiB.")
    try:
        return validate_context(json.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError("Invalid task context backup content.") from exc
