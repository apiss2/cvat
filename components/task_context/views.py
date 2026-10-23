# SPDX-License-Identifier: MIT
from django.conf import settings
from django.db import transaction
from rest_framework import viewsets
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.response import Response

from cvat.apps.engine.models import Job, Task
from cvat.apps.engine.renderers import CVATAPIRenderer

from .permissions import JobContextPermission, TaskContextPermission, can_edit_context
from .storage import ContextConflict, read_context, replace_context


class Conflict(APIException):
    status_code = 409
    default_code = "conflict"


class ContextViewSet(viewsets.GenericViewSet):
    parser_classes = [JSONParser]
    renderer_classes = [CVATAPIRenderer]
    metadata_class = None
    filter_backends = []
    schema = None
    iam_supports_organization_params = True

    def initial(self, request, *args, **kwargs):
        if not settings.CVAT_TASK_CONTEXT_ENABLED:
            raise NotFound()
        super().initial(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def context_response(self, request, task, data=None):
        return Response(
            {
                **(read_context(task) if data is None else data),
                "can_edit": can_edit_context(request, task),
            }
        )


class TaskContextViewSet(ContextViewSet):
    queryset = Task.objects.select_related("project", "organization")
    iam_permission_class = TaskContextPermission

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "update":
            queryset = queryset.select_for_update(of=("self",))
        return queryset

    def retrieve(self, request, pk):
        return self.context_response(request, self.get_object())

    @transaction.atomic
    def update(self, request, pk):
        task = self.get_object()
        try:
            data = replace_context(task, request.data)
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        except ContextConflict as exc:
            raise Conflict(str(exc)) from exc
        return self.context_response(request, task, data)


class JobContextViewSet(ContextViewSet):
    queryset = Job.objects.select_related("segment__task", "segment__task__project")
    iam_permission_class = JobContextPermission

    def retrieve(self, request, pk):
        job = self.get_object()  # Job-only assignees may read without task VIEW access.
        return self.context_response(request, job.segment.task)
