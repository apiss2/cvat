# SPDX-License-Identifier: MIT
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser
from rest_framework.response import Response

from cvat.apps.engine.permissions import TaskPermission
from cvat.apps.engine.views import JobViewSet, TaskViewSet

from .service import get_snapshot, replace_calibration
from .validation import MAX_DOCUMENT_BYTES, parse_document_bytes


class CalibrationJSONParser(BaseParser):
    media_type = "application/json"

    def parse(self, stream, media_type=None, parser_context=None):
        try:
            return parse_document_bytes(stream.read(MAX_DOCUMENT_BYTES + 1))
        except ValueError as exc:
            raise ParseError(str(exc)) from exc


class CalibrationTaskPermission(TaskPermission):
    @classmethod
    def _get_scopes(cls, request, view, obj):
        if view.action == "metadata" and request.method == "PUT":
            return [cls.Scopes.UPDATE_METADATA]
        return super()._get_scopes(request, view, obj)


class TaskCalibrationViewSet(TaskViewSet):
    parser_classes = [CalibrationJSONParser]
    iam_permission_class = CalibrationTaskPermission
    http_method_names = ["get", "put", "options"]
    schema = None

    def metadata(self, request, pk):
        task = self.get_object()  # native task and organization permissions
        if request.method == "PUT":
            return Response(replace_calibration(task, request.data))
        return Response(get_snapshot(task))


class JobCalibrationViewSet(JobViewSet):
    http_method_names = ["get", "options"]
    schema = None

    def metadata(self, request, pk):
        job = self.get_object()  # job assignees do not need task-level access
        return Response(get_snapshot(job.segment.task, job=job))
