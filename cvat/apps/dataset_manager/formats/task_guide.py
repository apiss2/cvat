# SPDX-License-Identifier: MIT
"""Import study reports through the standard project dataset import queue."""

from cvat.apps.dataset_manager.bindings import CvatImportError, ProjectData
from cvat.apps.dataset_manager.formats.registry import importer

from .task_guide_core import (
    IDENTITY_TAGS,
    GuideImportError,
    TaskIdentity,
    merge_markdown,
    plan_updates,
    read_reports,
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


def import_project_guides(source, project):
    from django.db import transaction

    from cvat.apps.engine import models
    from cvat.apps.engine.permissions import AnnotationGuidePermission, ProjectPermission

    reports, duplicates = read_reports(source)
    with transaction.atomic():
        # Serialize these imports and PR11's importer, without modifying standard models.
        project = models.Project.objects.select_for_update().get(pk=project.pk)
        request, iam_context, meta = _import_context(project)
        if project.organization_id != meta.org_id:
            raise GuideImportError("Project workspace changed after the import was requested")
        permission = ProjectPermission.create_base_perm(
            request, None, ProjectPermission.Scopes.IMPORT_DATASET, iam_context, obj=project
        )
        if not permission.check_access().allow:
            raise GuideImportError("Project dataset import permission denied")
        tasks = list(
            models.Task.objects.select_for_update()
            .filter(project_id=project.pk)
            .exclude(data=None)
            .order_by("id")
        )
        identities = {task.id: TaskIdentity(task.id, task.name) for task in tasks}
        # Only three scalar DicomTag attributes are read; frame/overlap duplicates collapse in SQL.
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
        updates, unmatched = plan_updates(reports, list(identities.values()))
        guides = {
            guide.task_id: guide
            for guide in models.AnnotationGuide.objects.select_for_update()
            .filter(task_id__in=updates)
            .order_by("task_id")
        }

        # Validate *all* identities, permissions and merged text before the first write.
        # Project dataset-import permission alone does not grant task-guide write permission.
        prepared = []
        for task in tasks:
            if task.id not in updates:
                continue
            guide = guides.get(task.id)
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
            try:
                markdown = merge_markdown(guide.markdown if guide else "", updates[task.id])
            except GuideImportError as exc:
                raise GuideImportError(f"Task {task.id}: {exc}") from exc
            prepared.append((task, guide, markdown))

        created = updated = unchanged = 0
        for task, guide, markdown in prepared:
            if guide is None:
                models.AnnotationGuide.objects.create(task_id=task.id, markdown=markdown)
                created += 1
            elif guide.markdown == markdown:
                unchanged += 1
                continue
            else:
                guide.markdown = markdown
                guide.save(update_fields=["markdown", "updated_date"])
                updated += 1
            # Standard task and project backups use their target's updated_date as a cache key.
            task.touch()
        if created or updated:
            project.touch()
        # Counts and record positions only: never put patient identifiers/reports in request logs.
        meta.status = (
            f"Task guides: {created} created, {updated} updated, {unchanged} unchanged; "
            f"{len(unmatched)} unmatched records, {duplicates} duplicate records"
        )
        if unmatched:
            meta.status += "; unmatched positions: " + ", ".join(map(str, unmatched[:20]))
            if len(unmatched) > 20:
                meta.status += ", ..."
        meta.progress = 1.0
        meta.save()


@importer(name="Task guide JSON", version="1.0", ext="JSON")
def import_task_guides(source, temp_dir, instance_data, load_data_callback=None, **options):
    # Reject task/job import before touching any guide or annotation.
    if not isinstance(instance_data, ProjectData):
        raise CvatImportError("Task guide JSON is available for project imports only")
    from django.db import IntegrityError

    try:
        import_project_guides(source, instance_data.db_project)
    except GuideImportError as exc:
        raise CvatImportError(str(exc)) from exc
    except IntegrityError as exc:
        raise CvatImportError("Concurrent guide changes detected; retry the import") from exc
