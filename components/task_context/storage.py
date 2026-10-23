# SPDX-License-Identifier: MIT
"""Persistence shared by the isolated endpoints and native task backup hooks."""

from django.db import transaction

from cvat.apps.engine.models import Task

from .models import TaskContext
from .validation import validate_context


class ContextConflict(Exception):
    pass


def read_context(task):
    context = TaskContext.objects.filter(task_id=task.pk).first()
    return {
        "task_id": task.pk,
        "fields": context.fields if context else [],
        "notes": context.notes if context else "",
        "version": context.version if context else 0,
    }


def replace_context(task, payload):
    document = validate_context(payload, with_version=True)
    with transaction.atomic():
        # Lock the parent so two first writes cannot both create version 1.
        task = Task.objects.select_for_update().get(pk=task.pk)
        context = TaskContext.objects.filter(task_id=task.pk).first()
        current_version = context.version if context else 0
        if document["version"] != current_version:
            raise ContextConflict("Task details changed. Reload before saving.")
        TaskContext.objects.update_or_create(
            task=task,
            defaults={
                "fields": document["fields"],
                "notes": document["notes"],
                "version": current_version + 1,
            },
        )
        # Native backup caching uses the task timestamp, not extension timestamps.
        task.touch()
        if task.project_id:
            task.project.touch()
        return read_context(task)


def export_context(task):
    """Return a portable document, or None when the task never had details."""
    context = TaskContext.objects.filter(task_id=task.pk).first()
    if context is None:
        return None
    return {"fields": context.fields, "notes": context.notes}


def restore_context(task, payload):
    """Restore a validated document onto a newly imported task."""
    document = validate_context(payload)
    with transaction.atomic():
        TaskContext.objects.create(task=task, version=1, **document)
        task.touch()
        if task.project_id:
            task.project.touch()
