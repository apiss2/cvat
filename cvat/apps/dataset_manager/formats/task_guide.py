# SPDX-License-Identifier: MIT
"""Import task Markdown and study reports through the standard project import queue."""

from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from uuid import UUID

from components.task_guide.staging import TextStore
from cvat.apps.dataset_manager.bindings import CvatImportError, ProjectData
from cvat.apps.dataset_manager.formats.registry import importer

from .task_guide_core import (
    IDENTITY_TAGS,
    GuideImportError,
    InvalidRecord,
    TaskIdentity,
    iter_markdown_zip,
    iter_named_json,
    iter_reports,
    merge_markdown,
    open_json_zip,
    plan_named_updates,
    plan_updates,
)


def _import_context(project):
    import rq
    from django.contrib.auth import get_user_model
    from django.http import HttpRequest

    from cvat.apps.engine.rq import ImportRQMeta
    from cvat.apps.iam.middleware import get_organization
    from cvat.apps.iam.permissions import get_iam_context

    job = rq.get_current_job()
    if job is None:
        raise GuideImportError("Task guides must be imported through the project import queue")
    meta = ImportRQMeta.for_job(job)
    if meta.user is None:
        raise GuideImportError("Import request has no authenticated user")
    user = get_user_model().objects.filter(pk=meta.user.id, is_active=True).first()
    if user is None:
        raise GuideImportError("Import user is no longer active")
    request = HttpRequest()
    request.user = user
    request.iam_context = get_organization(request)
    return request, get_iam_context(request, project), meta


def _stage_reports(reports, store):
    # Only hash matches need to read an earlier body back from disk.
    staged = {}
    duplicates = 0
    invalid = []
    for report in reports:
        if isinstance(report, InvalidRecord):
            invalid.append(report.row)
            continue
        digest = sha256(report.markdown.encode("utf-8")).digest()
        group = staged.setdefault((*report.dedup_key, digest), [])
        if any(store.matches(previous.markdown, report.markdown) for previous in group):
            duplicates += 1
        else:
            group.append(replace(report, markdown=store.write(report.markdown)))
    return [report for group in staged.values() for report in group], duplicates, invalid


def _stage_named_guides(guides, store):
    return [replace(guide, markdown=store.write(guide.markdown)) for guide in guides], 0, []


def _stage_entries(source, input_format, store):
    # Finish the complete ZIP/JSON stream, including its CRC and trailing syntax,
    # before checking or changing any guide. Only compact text references survive.
    if input_format in ("dicom", "json"):
        with open_json_zip(source) as member:
            if input_format == "dicom":
                return _stage_reports(iter_reports(member), store)
            return _stage_named_guides(iter_named_json(member), store)
    if input_format == "zip":
        with closing(iter_markdown_zip(source)) as guides:
            return _stage_named_guides(guides, store)
    raise GuideImportError("Unsupported task guide format")


def _prepare_guide(task, guide, markdown, input_format, request, iam_context):
    from cvat.apps.engine import models
    from cvat.apps.engine.permissions import AnnotationGuidePermission

    scope = (
        AnnotationGuidePermission.Scopes.UPDATE
        if guide
        else AnnotationGuidePermission.Scopes.CREATE
    )
    permission = AnnotationGuidePermission.create_base_perm(
        request,
        None,
        scope,
        iam_context,
        obj=guide,
        task_id=task.id,
        project_id=None,
    )
    if not permission.check_access().allow:
        raise GuideImportError(f"Task {task.id}: annotation guide write permission denied")
    if input_format == "dicom":
        try:
            return merge_markdown(guide.markdown if guide else "", markdown)
        except GuideImportError as exc:
            raise GuideImportError(f"Task {task.id}: {exc}") from exc

    asset_ids = {
        UUID(value) for value in models.AnnotationGuide.get_asset_ids_from_markdown(markdown)
    }
    if asset_ids:
        owned_assets = (
            set(guide.assets.filter(uuid__in=asset_ids).values_list("uuid", flat=True))
            if guide
            else set()
        )
        if asset_ids != owned_assets:
            raise GuideImportError(
                f"Task {task.id}: Markdown may reference only assets already owned by its guide"
            )
    return markdown


def _prepare_updates(tasks, updates, store, input_format, request, iam_context):
    from cvat.apps.engine import models

    guides = (
        models.AnnotationGuide.objects.select_for_update()
        .filter(task_id__in=updates)
        .order_by("task_id")
    )
    prepared = []
    unchanged = 0
    # Avoid both a queryset result cache and a list of existing guide bodies.
    with closing(guides.iterator(chunk_size=1)) as existing:
        guide = next(existing, None)
        for task in tasks:
            if task.id not in updates:
                continue
            current = guide if guide is not None and guide.task_id == task.id else None
            reference = updates[task.id]
            markdown = _prepare_guide(
                task, current, store.read(reference), input_format, request, iam_context
            )
            if current is not None and current.markdown == markdown:
                unchanged += 1
            else:
                if input_format == "dicom":
                    reference = store.write(markdown)
                prepared.append((task, current.id if current is not None else None, reference))
            if current is not None:
                guide = next(existing, None)
    return prepared, unchanged


def _save_updates(prepared, store):
    from cvat.apps.engine import models

    created = updated = 0
    for task, guide_id, reference in prepared:
        markdown = store.read(reference)
        if guide_id is None:
            models.AnnotationGuide.objects.create(task_id=task.id, markdown=markdown)
            created += 1
        else:
            # The guide row is still locked. Fetch only its identity so old bodies
            # are not retained throughout validation or read a second time here.
            guide = models.AnnotationGuide.objects.only("id").get(pk=guide_id)
            guide.markdown = markdown
            guide.save(update_fields=["markdown", "updated_date"])
            updated += 1
        # Standard backups use their target's updated_date as a cache key.
        task.touch()
    return created, updated


def import_project_guides(source, project, *, input_format="dicom", temp_dir=None):
    from django.db import transaction

    with TextStore(dir=temp_dir) as store:
        entries, duplicates, invalid = _stage_entries(source, input_format, store)
        with transaction.atomic():
            _apply_staged_guides(project, input_format, store, entries, duplicates, invalid)


def _positions(rows):
    text = ", ".join(map(str, rows[:20]))
    return text + (", ..." if len(rows) > 20 else "")


def _apply_staged_guides(project, input_format, store, entries, duplicates, invalid):
    from cvat.apps.engine import models
    from cvat.apps.engine.permissions import ProjectPermission

    # Serialize these imports and the DICOM importer using the standard project row.
    project = models.Project.objects.select_for_update().get(pk=project.pk)
    request, iam_context, meta = _import_context(project)
    if project.organization_id != meta.org_id:
        raise GuideImportError("Project workspace changed after the import was requested")
    permission = ProjectPermission.create_base_perm(
        request, None, ProjectPermission.Scopes.IMPORT_DATASET, iam_context, obj=project
    )
    if not permission.check_access().allow:
        raise GuideImportError("Project dataset import permission denied")
    task_query = models.Task.objects.select_for_update().filter(project_id=project.pk)
    if input_format == "dicom":
        task_query = task_query.exclude(data=None)
    tasks = list(task_query.order_by("id"))
    identities = {task.id: TaskIdentity(task.id, task.name) for task in tasks}
    if input_format == "dicom":
        # Read only the scalar identity attributes needed for report matching.
        values = (
            models.LabeledImageAttributeVal.objects.filter(
                image__job__segment__task__project_id=project.pk,
                image__label__project_id=project.pk,
                image__label__name="DicomTag",
                spec__label__project_id=project.pk,
                spec__label__name="DicomTag",
                spec__name__in=IDENTITY_TAGS,
            )
            .order_by()
            .values_list("image__job__segment__task_id", "spec__name", "value")
            .distinct()
        )
        for task_id, tag, value in values.iterator():
            if task_id in identities:
                identities[task_id].tags.setdefault(tag, set()).add(value)
        plan = plan_updates(entries, list(identities.values()))
        updates, unmatched, skipped_tasks = plan.updates, plan.unmatched, plan.skipped_tasks
    else:
        updates, unmatched = plan_named_updates(entries, list(identities.values()))
        skipped_tasks = []

    # Validate every identity, permission and merged body before the first write.
    # Project import permission alone does not grant task-guide write permission.
    prepared, unchanged = _prepare_updates(
        tasks, updates, store, input_format, request, iam_context
    )
    created, updated = _save_updates(prepared, store)
    if created or updated:
        project.touch()
    # Counts, record positions and task IDs only: never put patient identifiers or
    # report text in request logs.
    meta.status = (
        f"Task guides: {created} created, {updated} updated, {unchanged} unchanged; "
        f"{len(unmatched)} unmatched records, {duplicates} duplicate records"
    )
    if input_format == "dicom":
        meta.status += f", {len(invalid)} invalid records, {len(skipped_tasks)} skipped tasks"
        if unmatched or invalid or skipped_tasks:
            meta.status = "WARNING: " + meta.status
    if unmatched:
        meta.status += "; unmatched positions: " + _positions(unmatched)
    if invalid:
        meta.status += "; invalid positions: " + _positions(invalid)
    if skipped_tasks:
        meta.status += "; skipped task IDs: " + _positions(skipped_tasks)
    meta.progress = 1.0
    meta.save()


def _import_guides(source, instance_data, input_format, temp_dir):
    # Reject task/job import before touching any guide or annotation.
    if not isinstance(instance_data, ProjectData):
        raise CvatImportError("Task Guide formats are available for project imports only")
    from django.db import IntegrityError

    try:
        import_project_guides(
            source, instance_data.db_project, input_format=input_format, temp_dir=temp_dir
        )
    except GuideImportError as exc:
        raise CvatImportError(str(exc)) from exc
    except IntegrityError as exc:
        raise CvatImportError("Concurrent guide changes detected; retry the import") from exc


@importer(name="Task Guide JSON", version="1.0", ext="ZIP")
def import_task_guides(source, temp_dir, instance_data, load_data_callback=None, **options):
    _import_guides(source, instance_data, "json", temp_dir)


@importer(name="Task Guide Markdown Zip", version="1.0", ext="ZIP")
def import_task_guides_markdown_zip(
    source, temp_dir, instance_data, load_data_callback=None, **options
):
    _import_guides(source, instance_data, "zip", temp_dir)


@importer(name="Task Guide DICOM Report Import", version="1.0", ext="ZIP")
def import_task_guides_dicom_report(
    source, temp_dir, instance_data, load_data_callback=None, **options
):
    _import_guides(source, instance_data, "dicom", temp_dir)
