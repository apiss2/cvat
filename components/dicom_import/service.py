# SPDX-License-Identifier: MIT
"""DICOM conversion and registration using the native import queue and models."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import rq
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.signals import post_save
from django.http import HttpRequest
from rest_framework.exceptions import ValidationError

from cvat.apps.dataset_manager.task import patch_task_data
from cvat.apps.engine import models
from cvat.apps.engine.permissions import ProjectPermission
from cvat.apps.engine.rq import ImportRQMeta
from cvat.apps.engine.serializers import (
    DataSerializer,
    LabeledDataSerializer,
    LabelSerializer,
    ProjectWriteSerializer,
    TaskWriteSerializer,
)
from cvat.apps.engine.task import initialize_task
from cvat.apps.engine.utils import av_scan_paths
from cvat.apps.iam.middleware import get_organization
from cvat.apps.iam.permissions import get_iam_context

from . import DicomImportError
from .archive import read_upload
from .conversion import prepare
from .registration import LABEL_NAME, image_frames, missing_attributes, tag_batches

logger = logging.getLogger(__name__)


class ImportProgress:
    """Keep phase boundaries visible without one Redis write per input file."""

    def __init__(self, meta):
        self.meta = meta
        self.phase = None
        self.saved_at = 0.0

    def update(self, phase, done, total, start, span):
        now = monotonic()
        if phase != self.phase or done == total or now - self.saved_at >= 0.5:
            self.meta.status = f"{phase}: {done}/{total}"
            fraction = done / total if total else 1.0
            self.meta.progress = float(start + span * fraction)
            self.meta.save()
            self.phase, self.saved_at = phase, now


def discard_converted_media(prepared, converted_directory):
    """CVAT owns a separate copy after registration; release scratch media promptly."""
    for folder in {path.parent for path in prepared.media}:
        # Conversion creates one immediate child directory per task. Never delete
        # another storage root if this internal contract is changed accidentally.
        if folder.parent == converted_directory:
            try:
                shutil.rmtree(folder)
            except OSError:
                logger.warning("Could not remove a temporary DICOM conversion directory")
    prepared.media.clear()
    prepared.tags.clear()


@contextmanager
def created_directories():
    """DB rollback does not remove media. Track only this thread's new resources."""
    paths = []
    owner_thread = threading.get_ident()

    def created(sender, instance, created, **kwargs):
        if not created or threading.get_ident() != owner_thread:
            return
        path = Path(
            instance.get_data_dirname() if sender is models.Data else instance.get_dirname()
        )
        if path.exists():
            # Never delete a stale/pre-existing directory, even if its DB ID is reused.
            raise DicomImportError("A media directory already exists for a newly allocated ID")
        paths.append(path)

    senders = (models.Data, models.Task, models.Job)
    for sender in senders:
        post_save.connect(created, sender=sender, weak=False)
    try:
        yield
    except BaseException:
        for path in reversed(paths):
            try:
                if path.exists():
                    shutil.rmtree(path)
            except OSError:
                logger.exception("Could not remove a newly created DICOM import resource")
        raise
    finally:
        for sender in senders:
            post_save.disconnect(created, sender=sender)


def ensure_label(project, names):
    label = project.label_set.filter(name=LABEL_NAME, parent=None).first()
    existing = LabelSerializer(label).data if label else None
    additions = missing_attributes(existing, names)
    if label and not additions:
        return label
    payload = {"name": LABEL_NAME, "type": label.type if label else "tag", "attributes": additions}
    if label:
        payload["color"] = label.color
    serializer = LabelSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data)
    if label:
        data["id"] = label.id
    label = LabelSerializer.update_label(data, "", [], parent_instance=project)
    project.touch()
    ProjectWriteSerializer(project).update_child_objects_on_labels_update(project)
    return label


def create_task(prepared, project, user):
    parent = prepared.media[0].parent
    serializer = DataSerializer(
        data={
            "server_files": [path.name for path in prepared.media],
            "use_cache": False,
            "use_zip_chunks": True,
            "image_quality": 100,
            "sorting_method": "lexicographical",
        }
    )
    serializer.is_valid(raise_exception=True)
    data = serializer.save()
    # This is the same creation entry point used by ProjectAnnotation.add_task.
    task = TaskWriteSerializer.create(
        None,
        {
            "name": prepared.name,
            "project_id": project.id,
            "data_id": data.id,
            "owner": user,
            "organization": project.organization,
        },
    )
    options = dict(serializer.data)
    for key in ("use_zip_chunks", "use_cache", "copy_data"):
        options[key] = serializer.validated_data[key]
    options["server_files_path"] = str(parent)
    options["stop_frame"] = None
    initialize_task(db_task=task, data=options)
    task.refresh_from_db()
    if task.data.size != prepared.frames:
        raise DicomImportError("CVAT frame count differs from the converted input")
    return task


def import_user(meta, project):
    if meta.user is None:
        raise DicomImportError("DICOM import requires an authenticated user")
    user = get_user_model().objects.filter(pk=meta.user.id, is_active=True).first()
    if user is None:
        raise DicomImportError("Import user is no longer active")
    if project.organization_id != meta.org_id:
        raise DicomImportError("Project workspace or dimension changed")
    # Project.dimension exists only in the API representation, not on the model.
    if project.tasks.exclude(dimension__in=("", "2d")).exists():
        raise DicomImportError("DICOM import requires a 2D project")
    request = HttpRequest()
    request.user = user
    request.iam_context = get_organization(request)
    permission = ProjectPermission.create_base_perm(
        request,
        None,
        ProjectPermission.Scopes.IMPORT_DATASET,
        get_iam_context(request, project),
        project,
    )
    if not permission.check_access().allow:
        raise DicomImportError("Project import permission is no longer available")
    return user


def check_existing_studies(project, incoming):
    """Return conflicting names without aborting unrelated incoming tasks."""
    if not incoming:
        return set()
    tasks = dict(project.tasks.filter(name__in=incoming).values_list("id", "name"))
    studies = {task_id: set() for task_id in tasks}
    values = (
        models.LabeledImageAttributeVal.objects.filter(
            image__job__segment__task_id__in=tasks,
            image__label__project_id=project.id,
            image__label__name=LABEL_NAME,
            spec__label__project_id=project.id,
            spec__label__name=LABEL_NAME,
            spec__name="StudyInstanceUID",
        )
        .order_by()
        .values_list("image__job__segment__task_id", "value")
        .distinct()
    )
    for task_id, value in values.iterator():
        studies[task_id].add(value.strip())
    return {name for task_id, name in tasks.items() if studies[task_id] != {incoming[name]}}


def result_message(plan, created, duplicates, failed, conflicts):
    """Counts only: task names and decoder exceptions may contain patient identifiers."""
    severity = "WARNING: " if created and failed else ""
    message = (
        f"{severity}DICOM: {created} tasks created; {len(failed)} tasks failed "
        f"({len(duplicates)} duplicate task names, {len(failed - duplicates)} other task errors); "
        f"{plan.duplicates} duplicate images skipped; {plan.high_bit_checked} HighBit checks passed"
    )
    if conflicts:
        message += (
            f"; {len(conflicts)} duplicate names have a different or missing StudyInstanceUID"
        )
    return message


def import_project(source, temporary_directory, project):
    if os.environ.get("CVAT_DICOM_IMPORT_ENABLED") != "1":
        raise DicomImportError("DICOM import is disabled")
    if not connection.in_atomic_block:
        raise DicomImportError("DICOM import requires the native project import transaction")
    if getattr(settings, "DEFAULT_BACKING_CS_ID", None):
        raise DicomImportError("This importer currently requires local media storage")
    job = rq.get_current_job()
    if job is None:
        raise DicomImportError("DICOM import requires the native import queue")
    meta = ImportRQMeta.for_job(job)
    import_user(meta, project)
    meta.status = "Preparing DICOM input"
    meta.progress = 0.0
    meta.save()
    reporter = ImportProgress(meta)

    def scan_files(paths):
        reporter.update("Checking DICOM input files", 0, len(paths), 0, 0)
        # Do not pass a directory: the common clamscan helper is not recursive.
        # Batches also avoid the operating system's command-line length limit.
        for start in range(0, len(paths), 128):
            batch = paths[start : start + 128]
            av_scan_paths(*(str(path) for path in batch))
            reporter.update("Checking DICOM input files", start + len(batch), len(paths), 0, 0)

    prefix, files = read_upload(
        source,
        Path(temporary_directory),
        user_id=meta.user.id,
        project_id=project.id,
        organization_id=project.organization_id,
        scan_files=scan_files,
    )
    existing = project.tasks.values_list("name", flat=True)

    def progress(done, total):
        reporter.update("Converting DICOM", done, total, 0.1, 0.6)

    def inspection_progress(done, total):
        reporter.update("Inspecting DICOM headers", done, total, 0, 0.1)

    converted_directory = Path(temporary_directory) / "converted"
    plan = prepare(
        files,
        converted_directory,
        prefix,
        existing,
        progress=progress,
        inspection_progress=inspection_progress,
    )
    # Serialize this importer per project, then recheck names after conversion and waiting.
    project = models.Project.objects.select_for_update().get(pk=project.id)
    user = import_user(meta, project)
    existing = set(project.tasks.values_list("name", flat=True))
    conflicts = check_existing_studies(
        project, {name: uid for name, uid in plan.study_uids.items() if name in existing}
    )
    tasks = []
    for task in plan.tasks:
        if task.name in existing:
            plan.skipped_tasks.add(task.name)
            discard_converted_media(task, converted_directory)
        else:
            tasks.append(task)
    duplicates = plan.skipped_tasks
    failed = set(plan.failed_tasks) | duplicates
    created = 0
    if not tasks:
        raise DicomImportError(result_message(plan, created, duplicates, failed, conflicts))
    with created_directories():
        names = {name for task in tasks for values in task.tags.values() for name in values}
        label = ensure_label(project, names)
        ids = dict(label.attributespec_set.values_list("name", "id"))
        reporter.update("Registering DICOM tasks", 0, len(tasks), 0.7, 0.29)
        for index, prepared in enumerate(tasks):
            try:
                # A failed task rolls back its rows and newly allocated media together.
                # The outer native transaction still handles shared/system failures.
                with created_directories(), transaction.atomic():
                    task = create_task(prepared, project, user)
                    if prepared.video:
                        values = next(iter(prepared.tags.values()))
                        frames = ((frame, values) for frame in range(prepared.frames))
                    else:
                        frames = image_frames(
                            prepared.tags, task.data.images.values_list("path", "frame")
                        )
                    for batch in tag_batches(frames, label.id, ids):
                        serializer = LabeledDataSerializer(data=batch)
                        serializer.is_valid(raise_exception=True)
                        data = serializer.validated_data
                        # DRF trims CharField whitespace; retain the exact DICOM text.
                        for original, validated in zip(batch["tags"], data["tags"]):
                            for old, new in zip(original["attributes"], validated["attributes"]):
                                new["value"] = old["value"]
                        patch_task_data(task.id, data, "create")
            except (DicomImportError, DjangoValidationError, ValidationError, IntegrityError):
                failed.add(prepared.name)
            else:
                created += 1
            finally:
                discard_converted_media(prepared, converted_directory)
            reporter.update("Registering DICOM tasks", index + 1, len(tasks), 0.7, 0.29)
        meta.status = result_message(plan, created, duplicates, failed, conflicts)
        if not created:
            raise DicomImportError(meta.status)
        meta.progress = 1.0
        meta.save()
