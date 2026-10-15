# SPDX-License-Identifier: MIT
"""Separate endpoints, with the native export actions and permission classes."""

import re

from rest_framework.exceptions import ParseError, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.reverse import reverse

from cvat.apps.engine.background import DatasetExporter
from cvat.apps.engine.location import Location
from cvat.apps.engine.rq import ExportRequestId
from cvat.apps.engine.views import ProjectViewSet, TaskViewSet

from .selection import FrameFilter, SUPPORTED_FORMATS, fingerprint
from .service import check_revision, export_filtered, get_options, preview


class FilteredDatasetExporter(DatasetExporter):
    def __init__(self, *, preview_only=False, **kwargs):
        super().__init__(**kwargs)
        self.preview_only = preview_only

    def init_request_args(self):
        super().init_request_args()
        body = self.request.data
        if not isinstance(body, dict) or set(body) - {"filter", "omit_tags", "expected_revision"}:
            raise ValidationError("Expected filter, omit_tags and expected_revision fields")
        try:
            self.frame_filter = FrameFilter.parse(body.get("filter"))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        self.omit_tags = body.get("omit_tags", False)
        if type(self.omit_tags) is not bool:
            raise ValidationError("omit_tags must be a boolean")
        self.expected_revision = body.get("expected_revision", "")
        if not self.preview_only and (
            not isinstance(self.expected_revision, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.expected_revision)
        ):
            raise ValidationError("Preview the selection before exporting")
        self.signature = fingerprint({
            "target": str(self.target), "id": self.db_instance.pk, "user": self.user_id,
            "filter": self.frame_filter.to_dict(), "revision": self.expected_revision,
            "format": self.export_args.format, "save_images": self.export_args.save_images,
            "omit_tags": self.omit_tags,
        })

    def validate_request(self):
        super().validate_request()
        if self.export_args.format not in SUPPORTED_FORMATS:
            raise ValidationError("This format is not supported for frame-filtered export")
        if self.export_args.location_config.location != Location.LOCAL:
            raise ValidationError("Filtered export currently supports local downloads only")
        if self.export_args.format == "ITGformat 1.0" and not self.omit_tags:
            raise ValidationError("ITGformat cannot represent tags; explicitly enable omit_tags")
        if not self.preview_only:
            check_revision(str(self.target), self.db_instance.pk,
                           self.frame_filter, self.expected_revision)

    def build_request_id(self):
        # Use the native request-ID schema. A distinct format token prevents
        # collision with ordinary exports and with other filter conditions.
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
        parsed = ExportRequestId.parse_and_validate_queue(
            request_id, expected_queue=self.QUEUE_NAME,
        )
        if parsed.user_id != self.user_id or not re.fullmatch(
            r".+ filtered [0-9a-f]{64}", parsed.format or "",
        ):
            raise ValueError("This is not a filtered export belonging to this user")

    def _init_callback_with_params(self):
        self.callback = export_filtered
        self.callback_args = ()
        self.callback_kwargs = {
            "target": str(self.target), "pk": self.db_instance.pk,
            "filter_spec": self.frame_filter.to_dict(), "expected_revision": self.expected_revision,
            "format_name": self.export_args.format, "save_images": self.export_args.save_images,
            "omit_tags": self.omit_tags, "signature": self.signature,
            "server_url": self.request.build_absolute_uri("/").rstrip("/"),
        }

    def get_result_filename(self):
        return "filtered_" + super().get_result_filename()

    def get_result_endpoint_url(self):
        return reverse(f"filtered-{self.target}-download", args=[self.db_instance.pk],
                       request=self.request)


class ObjectJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        data = super().parse(stream, media_type, parser_context)
        if not isinstance(data, dict):
            raise ParseError("The request body must be a JSON object")
        return data


class FilteredExportMixin:
    parser_classes = [ObjectJSONParser]
    schema = None  # Private extension API; documented with the plugin.

    # Retain the upstream action names: IAM maps these to view/export/download
    # permissions. In particular, preview is NOT an unauthorised annotation GET.
    def retrieve(self, request, pk):
        return Response(get_options(self.get_object()))

    def initiate_dataset_export(self, request, pk, preview_only=False):
        instance = self.get_object()  # applies native object and organization permissions
        manager = FilteredDatasetExporter(
            request=request, db_instance=instance, preview_only=preview_only,
        )
        if preview_only:
            manager.init_request_args()
            manager.validate_request()
            return Response(preview(str(manager.target), instance.pk, manager.frame_filter))
        return manager.enqueue_job()

    def download_dataset(self, request, pk):
        instance = self.get_object()  # checks the native export-job ownership permission
        return FilteredDatasetExporter(
            request=request, db_instance=instance,
        ).get_downloader().download_file()


class FilteredTaskViewSet(FilteredExportMixin, TaskViewSet):
    pass


class FilteredProjectViewSet(FilteredExportMixin, ProjectViewSet):
    pass
