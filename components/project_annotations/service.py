# SPDX-License-Identifier: MIT
"""Independent project annotation imports with one transaction per existing task."""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

import rq
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Trim
from django.http import HttpRequest
from rq.timeouts import JobTimeoutException

from cvat.apps.dataset_manager.annotation import AnnotationIR
from cvat.apps.dataset_manager.bindings import CvatImportError
from cvat.apps.dataset_manager.formats.registry import make_importer
from cvat.apps.dataset_manager.task import PatchAction, TaskAnnotation
from cvat.apps.dataset_manager.util import TmpDirManager
from cvat.apps.engine import models
from cvat.apps.engine.permissions import ProjectPermission, TaskPermission
from cvat.apps.engine.rq import ImportRQMeta
from cvat.apps.engine.serializers import LabelSerializer
from cvat.apps.engine.utils import av_scan_paths
from cvat.apps.iam.middleware import get_organization
from cvat.apps.iam.permissions import get_iam_context

from . import DICOM_FORMAT, PROTECTED_LABEL, ProjectAnnotationImportError, enabled, supported_format
from .annotations import apply_task, stage_native, validate_payload
from .archive import Budget, inspect_zip, plan_folders, task_source
from .dicom import (
    IDENTITY_NAMES,
    MAX_GENERATED_ATTRIBUTE_VALUES,
    MAX_GENERATED_TAGS,
    FrameIdentity,
    TaskSnapshot,
    plan_records,
    read_records,
    record_sop_uids,
)

logger = logging.getLogger(__name__)


def import_context(project, meta):
    if meta.user is None:
        raise ProjectAnnotationImportError("Import requires an authenticated user")
    user = get_user_model().objects.filter(pk=meta.user.id, is_active=True).first()
    if user is None or project.organization_id != meta.org_id:
        raise ProjectAnnotationImportError("Import user or project workspace changed")
    request = HttpRequest()
    request.user = user
    request.iam_context = get_organization(request)
    iam_context = get_iam_context(request, project)
    permission = ProjectPermission.create_base_perm(
        request, None, ProjectPermission.Scopes.IMPORT_DATASET, iam_context, obj=project
    )
    if not permission.check_access().allow:
        raise ProjectAnnotationImportError("Project import permission denied")
    return request


def locked_task(project_id, task_id, meta):
    # Match the existing extension's project lock. The native per-job writers
    # then execute inside this task's transaction, including their timestamps.
    project = models.Project.objects.select_for_update().get(pk=project_id)
    request = import_context(project, meta)
    task = models.Task.objects.select_for_update().get(pk=task_id, project_id=project_id)
    if task.data_id is None or not task.data.size:
        raise ProjectAnnotationImportError("Task has no initialized media")
    permission = TaskPermission.create_base_perm(
        request,
        None,
        TaskPermission.Scopes.IMPORT_ANNOTATIONS,
        get_iam_context(request, task),
        obj=task,
    )
    if not permission.check_access().allow:
        raise ProjectAnnotationImportError("Task annotation import permission denied")
    annotation = TaskAnnotation(task_id, write_only=True)
    # Lock the native overlap/ground-truth jobs while validating and writing.
    # Native editors have their own transaction ordering; a conflict rolls back
    # this task rather than retrying an import that may already be partially done.
    list(annotation.db_jobs.select_for_update().values_list("id", flat=True))
    return project, annotation


def project_labels(project):
    return list(LabelSerializer(project.label_set.filter(parent=None), many=True).data)


def snapshots(project_id, *, task_id=None, sop_uids=None):
    """Read only task media and stored tag identities, never infer from task names."""
    task_query = models.Task.objects.filter(project_id=project_id).exclude(data=None)
    if sop_uids is not None:
        # Rechecking one task only needs its records and current possible
        # matches. The planner still checks the full Study/SOP pair, patient
        # consistency and ambiguity; this query does not infer any identity.
        candidates = (
            models.LabeledImageAttributeVal.objects.filter(
                image__job__segment__task__project_id=project_id,
                image__label__project_id=project_id,
                image__label__name=PROTECTED_LABEL,
                spec__label__project_id=project_id,
                spec__label__name=PROTECTED_LABEL,
                spec__name="SOPInstanceUID",
            )
            .annotate(normalized_sop=Trim("value"))
            # SQL Trim removes spaces, whereas Python also strips other
            # whitespace. Include noncanonical stored values conservatively
            # so unusual padding cannot conceal a second matching task.
            .filter(Q(normalized_sop__in=sop_uids) | Q(value__regex=r"[^0-9. ]"))
            .values_list("image__job__segment__task_id", flat=True)
        )
        task_query = task_query.filter(Q(id=task_id) | Q(id__in=candidates))
    tasks = list(task_query.select_related("data").order_by("id"))
    selected_jobs = Q(job__type=models.JobType.ANNOTATION) | Q(
        job__type=models.JobType.GROUND_TRUTH,
        job__segment__task__data__validation_layout__mode=models.ValidationMode.GT_POOL,
    )
    tags = models.LabeledImage.objects.filter(
        selected_jobs,
        job__segment__task_id__in=[task.id for task in tasks],
        label__project_id=project_id,
    )
    counts = defaultdict(lambda: defaultdict(Counter))
    for task_id, job_id, frame, label_id in (
        tags.order_by()
        .values_list("job__segment__task_id", "job_id", "frame", "label_id")
        .iterator()
    ):
        counts[task_id][job_id][(frame, label_id)] += 1
    identities = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    values = (
        models.LabeledImageAttributeVal.objects.filter(
            image__in=tags.filter(label__name=PROTECTED_LABEL),
            spec__label__project_id=project_id,
            spec__label__name=PROTECTED_LABEL,
            spec__name__in=IDENTITY_NAMES,
        )
        .order_by()
        .values_list("image__job__segment__task_id", "image__frame", "spec__name", "value")
    )
    for task_id, frame, name, value in values.iterator():
        identities[task_id][frame][name].add(value)
    result = []
    for task in tasks:
        # The same logical frame is stored in both jobs when segments overlap.
        # Retain the maximum within-job multiplicity, not the replica sum.
        logical = Counter()
        for job_counts in counts[task.id].values():
            logical |= job_counts
        existing = tuple(
            {"frame": frame, "label_id": label_id}
            for (frame, label_id), count in logical.items()
            for _ in range(count)
        )
        result.append(
            TaskSnapshot(
                id=task.id,
                frames=tuple(range(task.data.size)),
                is_video=task.mode == models.TaskMode.INTERPOLATION,
                identities=tuple(
                    FrameIdentity(frame=frame, values=values)
                    for frame, values in identities[task.id].items()
                ),
                existing_tags=existing,
            )
        )
    return result


def result_message(succeeded, failed, unmatched, invalid=0):
    warning = "WARNING: " if failed or unmatched or invalid else ""
    return (
        f"{warning}Project annotations: {succeeded} tasks succeeded; {failed} tasks failed; "
        f"{unmatched} unmatched records; {invalid} invalid records"
    )


def _failed(task_id, exc):
    # Native importer errors can contain patient IDs, file paths and attribute
    # values. Neither their text nor chained tracebacks enter request status/logs.
    logger.warning("Project annotation import: task %s failed (%s)", task_id, type(exc).__name__)


def _progress(meta, completed, total):
    meta.status = f"Project annotation imports processed: {completed}/{total}"
    meta.progress = completed / max(total, 1)
    meta.save()


def _tag_counts(payload):
    tags = payload["tags"]
    return len(tags), sum(len(tag.get("attributes", [])) for tag in tags)


def _expand_dicom_gt_pool(
    annotation, payload, snapshot, labels, import_mode, *, tag_budget, attribute_budget
):
    if annotation.db_task.data.validation_mode != models.ValidationMode.GT_POOL:
        return payload
    db_data = annotation.db_task.data
    gt_job = annotation.db_task.gt_job
    if gt_job is None:
        raise ProjectAnnotationImportError("GT pool task has no ground-truth job")
    gt_abs_frames = gt_job.segment.frame_set
    frame_step = db_data.get_frame_step()

    def relative_frame(frame):
        return (frame - db_data.start_frame) // frame_step

    gt_frames = {relative_frame(frame) for frame in gt_abs_frames}
    input_frames = {tag["frame"] for tag in payload["tags"]}
    if gt_frames.intersection(input_frames):
        # Count the native copies without creating tags or retaining lists of
        # placeholder destinations. Input tags on placeholders are replaced.
        copies = Counter()
        replaced_frames = set()
        for frame, real_frame in (
            db_data.images.filter(is_placeholder=True, real_frame__in=gt_abs_frames)
            .values_list("frame", "real_frame")
            .iterator(chunk_size=1000)
        ):
            copies[relative_frame(real_frame)] += 1
            frame = relative_frame(frame)
            if frame in input_frames:
                replaced_frames.add(frame)
        tag_count = attribute_count = 0
        for tag in payload["tags"]:
            count = int(tag["frame"] not in replaced_frames) + copies[tag["frame"]]
            tag_count += count
            attribute_count += count * len(tag.get("attributes", []))
            if tag_count > tag_budget or attribute_count > attribute_budget:
                raise ProjectAnnotationImportError(
                    "Expanded DICOM annotations exceed the import size limit"
                )
    expanded = AnnotationIR(annotation.db_task.dimension, payload)
    # Native create copies GT annotations to placeholder frames. Check those
    # actual destinations before writing; repeating this preprocessing in create
    # is idempotent because it replaces input annotations on the placeholders.
    annotation._preprocess_input_annotations_for_gt_pool_task(expanded, action=PatchAction.CREATE)
    protected = {label["id"] for label in labels if label["name"] == PROTECTED_LABEL}
    occupied = {
        (tag["frame"], tag["label_id"])
        for tag in snapshot.existing_tags
        if import_mode == "append" or tag["label_id"] in protected
    }
    for tag in expanded.tags:
        key = (tag["frame"], tag["label_id"])
        if key in occupied:
            raise ProjectAnnotationImportError("Duplicate tag label on the same frame")
        occupied.add(key)
    return expanded.serialize()


def _import_dicom(source, project, meta, import_mode):
    records = read_records(source)
    labels = project_labels(project)
    plan = plan_records(records, snapshots(project.id), labels, import_mode)
    failed, succeeded = set(plan.failed_tasks), 0
    counts = {task_id: _tag_counts(payload) for task_id, payload in plan.updates.items()}
    total_tags = sum(count[0] for count in counts.values())
    total_values = sum(count[1] for count in counts.values())
    for index, (task_id, expected) in enumerate(sorted(plan.updates.items()), 1):
        expected_tags, expected_values = counts[task_id]
        try:
            with transaction.atomic():
                current_project, annotation = locked_task(project.id, task_id, meta)
                labels = project_labels(current_project)
                # Recheck UIDs, labels and retained tags against current state,
                # including new ambiguity in another task since initial preflight.
                task_records = [records[row - 1] for row in plan.task_rows[task_id]]
                current_snapshots = snapshots(
                    project.id, task_id=task_id, sop_uids=record_sop_uids(task_records)
                )
                current = plan_records(
                    task_records,
                    current_snapshots,
                    labels,
                    import_mode,
                )
                if task_id in current.failed_tasks or current.updates.get(task_id) != expected:
                    raise ProjectAnnotationImportError("Task changed after DICOM preflight")
                payload = validate_payload(annotation, expected, labels)
                payload = _expand_dicom_gt_pool(
                    annotation,
                    payload,
                    next(snapshot for snapshot in current_snapshots if snapshot.id == task_id),
                    labels,
                    import_mode,
                    tag_budget=MAX_GENERATED_TAGS - total_tags + expected_tags,
                    attribute_budget=(
                        MAX_GENERATED_ATTRIBUTE_VALUES - total_values + expected_values
                    ),
                )
                expanded_tags, expanded_values = _tag_counts(payload)
                next_tags = total_tags - expected_tags + expanded_tags
                next_values = total_values - expected_values + expanded_values
                if next_tags > MAX_GENERATED_TAGS or next_values > MAX_GENERATED_ATTRIBUTE_VALUES:
                    raise ProjectAnnotationImportError(
                        "Expanded DICOM annotations exceed the import size limit"
                    )
                apply_task(annotation, payload, labels, import_mode)
        except JobTimeoutException:
            raise
        except Exception as exc:
            total_tags -= expected_tags
            total_values -= expected_values
            failed.add(task_id)
            _failed(task_id, exc)
        else:
            total_tags, total_values = next_tags, next_values
            succeeded += 1
        _progress(meta, index, len(plan.updates))
    return result_message(succeeded, len(failed), len(plan.unmatched), len(plan.invalid_records))


def _import_folders(
    source, project, meta, importer, temporary_directory, import_mode, conv_mask_to_poly
):
    budget = Budget()
    members = inspect_zip(source, budget)
    tasks = list(project.tasks.values_list("id", "name"))
    plan = plan_folders(members, tasks)
    failed, succeeded = set(plan.failed_tasks), 0
    for index, (task_id, prefix) in enumerate(sorted(plan.targets.items()), 1):
        try:
            with TemporaryDirectory(dir=temporary_directory) as task_directory:
                native_source = task_source(
                    source, members, prefix, task_directory, importer.EXT, budget
                )
                parse_directory = Path(task_directory) / "parsed"
                parse_directory.mkdir()
                with transaction.atomic():
                    current_project, annotation = locked_task(project.id, task_id, meta)
                    # Renames and newly duplicated task names invalidate this target.
                    candidates = list(
                        current_project.tasks.filter(name=prefix[-1]).values_list("id", flat=True)
                    )
                    if candidates != [task_id]:
                        raise ProjectAnnotationImportError("Task name changed or became ambiguous")
                    if annotation.db_task.dimension != importer.DIMENSION:
                        raise ProjectAnnotationImportError(
                            "Format does not support this task dimension"
                        )
                    labels = project_labels(current_project)
                    staged = stage_native(
                        native_source,
                        importer,
                        annotation.db_task,
                        parse_directory,
                        conv_mask_to_poly=conv_mask_to_poly,
                    )
                    payload = validate_payload(annotation, staged, labels)
                    apply_task(annotation, payload, labels, import_mode)
        except JobTimeoutException:
            raise
        except Exception as exc:
            failed.add(task_id)
            _failed(task_id, exc)
        else:
            succeeded += 1
        _progress(meta, index, len(plan.targets))
    return result_message(succeeded, len(failed), plan.unmatched)


def import_project_annotations(
    src_file, project_id, format_name, conv_mask_to_poly, *, import_mode="replace"
):
    """RQ entry point. No outer transaction: an unrelated failure cannot undo success."""
    try:
        if not enabled() or not supported_format(format_name):
            raise ProjectAnnotationImportError("Project annotation import or format is disabled")
        if import_mode not in ("replace", "append"):
            raise ProjectAnnotationImportError("Unsupported annotation import mode")
        job = rq.get_current_job()
        if job is None:
            raise ProjectAnnotationImportError(
                "Project annotations require the native import queue"
            )
        meta = ImportRQMeta.for_job(job)
        project = models.Project.objects.get(pk=project_id)
        import_context(project, meta)
        importer = make_importer(format_name)
        if not importer.ENABLED:
            raise ProjectAnnotationImportError("Annotation import format is disabled")
        av_scan_paths(src_file)
        with (
            open(src_file, "rb") as source,
            TmpDirManager.get_tmp_directory() as temporary_directory,
        ):
            if format_name == DICOM_FORMAT:
                message = _import_dicom(source, project, meta, import_mode)
            else:
                message = _import_folders(
                    source,
                    project,
                    meta,
                    importer,
                    temporary_directory,
                    import_mode,
                    conv_mask_to_poly,
                )
        meta.status = message
        meta.progress = 1.0
        meta.save()
    except JobTimeoutException:
        # The RQ alarm fires once for the whole job. Preserve the timeout so
        # subsequent tasks are not written and the worker records a failure.
        raise
    except ProjectAnnotationImportError as exc:
        raise CvatImportError(str(exc)) from None
    except Exception:
        # Do not expose JSON, DICOM identities, file names or native decoder errors.
        raise CvatImportError(
            "Project annotation import could not validate the uploaded data"
        ) from None
