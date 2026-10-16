# SPDX-License-Identifier: MIT
"""Separate endpoints using the native export actions and permission classes."""

import re

from rest_framework.exceptions import ParseError, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.reverse import reverse

from cvat.apps.engine.background import DatasetExporter
from cvat.apps.engine.location import Location
from cvat.apps.engine.rq import ExportRequestId
from cvat.apps.engine.views import ProjectViewSet, TaskViewSet

from .selection import AnnotationFilter, fingerprint
from .service import IMAGE_EXPORT_FORMATS, check_revision, export_filtered, get_options, preview


def read_filters(body, *, exporting=False):
    allowed = {"filters", "expected_revision"} if exporting else {"filters"}
    if not isinstance(body, dict) or set(body) - allowed or "filters" not in body:
        raise ValidationError(f"Expected fields: {', '.join(sorted(allowed))}")
    try:
        return AnnotationFilter.parse(body["filters"])
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


class FilteredDatasetExporter(DatasetExporter):
    def init_request_args(self):
        super().init_request_args()
        self.filters = read_filters(self.request.data, exporting=True)
        self.expected_revision = self.request.data.get("expected_revision", "")
        if not isinstance(self.expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", self.expected_revision):
            raise ValidationError("Preview the selection before exporting")
        self.signature = fingerprint({
            "target": str(self.target), "id": self.db_instance.pk, "user": self.user_id,
            "filters": self.filters.to_list(), "revision": self.expected_revision,
            "format": self.export_args.format, "save_images": self.export_args.save_images,
        })

    def validate_request(self):
        super().validate_request()
        if self.export_args.format not in IMAGE_EXPORT_FORMATS:
            raise ValidationError("This exporter does not support the selected-image adapter")
        if self.export_args.location_config.location != Location.LOCAL:
            raise ValidationError("Filtered export currently supports local downloads only")
        check_revision(str(self.target), self.db_instance.pk, self.filters, self.expected_revision)

    def build_request_id(self):
        parsed = ExportRequestId.parse_and_validate_queue(
            super().build_request_id(), expected_queue=self.QUEUE_NAME,
        )
        return ExportRequestId(
            action=parsed.action, target=parsed.target, target_id=parsed.target_id,
            user_id=parsed.user_id, subresource=parsed.subresource,
            format=f"{self.export_args.format} filtered {self.signature}",
        ).render()

    def validate_request_id(self, request_id, /):
        super().validate_request_id(request_id)
        parsed = ExportRequestId.parse_and_validate_queue(request_id, expected_queue=self.QUEUE_NAME)
        if parsed.user_id != self.user_id or not re.fullmatch(r".+ filtered [0-9a-f]{64}", parsed.format or ""):
            raise ValueError("This is not a filtered export belonging to this user")

    def _init_callback_with_params(self):
        self.callback = export_filtered
        self.callback_args = ()
        self.callback_kwargs = {
            "target": str(self.target), "pk": self.db_instance.pk,
            "filters_spec": self.filters.to_list(), "expected_revision": self.expected_revision,
            "format_name": self.export_args.format, "save_images": self.export_args.save_images,
            "signature": self.signature, "server_url": self.request.build_absolute_uri("/").rstrip("/"),
        }

    def get_result_filename(self):
        return "filtered_" + super().get_result_filename()

    def get_result_endpoint_url(self):
        return reverse(f"filtered-{self.target}-download", args=[self.db_instance.pk], request=self.request)


class ObjectJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        data = super().parse(stream, media_type, parser_context)
        if not isinstance(data, dict):
            raise ParseError("The request body must be a JSON object")
        return data


class FilteredExportMixin:
    parser_classes = [ObjectJSONParser]
    schema = None

    def retrieve(self, request, pk):
        return Response(get_options(self.get_object()))

    def initiate_dataset_export(self, request, pk, preview_only=False):
        instance = self.get_object()  # native export and organization permissions
        if preview_only:
            # No exporter, format or output-content option participates in matching.
            return Response(preview(instance.__class__.__name__.lower(), instance.pk, read_filters(request.data)))
        return FilteredDatasetExporter(request=request, db_instance=instance).enqueue_job()

    def download_dataset(self, request, pk):
        instance = self.get_object()  # native export-job ownership permission
        return FilteredDatasetExporter(request=request, db_instance=instance).get_downloader().download_file()


class FilteredTaskViewSet(FilteredExportMixin, TaskViewSet):
    pass


class FilteredProjectViewSet(FilteredExportMixin, ProjectViewSet):
    pass
