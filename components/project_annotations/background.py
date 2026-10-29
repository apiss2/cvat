# SPDX-License-Identifier: MIT
"""Use the native upload, queue, request tracking and cleanup mechanisms."""

from rest_framework.exceptions import MethodNotAllowed, ValidationError

from cvat.apps.dataset_manager.task import AnnotationImportMode
from cvat.apps.engine.background import DatasetImporter
from cvat.apps.engine.models import RequestAction, RequestSubresource, RequestTarget
from cvat.apps.engine.rq import ImportRequestId
from cvat.apps.engine.serializers import AnnotationFileSerializer

from . import enabled, supported_format


class ProjectAnnotationImporter(DatasetImporter):
    SUPPORTED_TARGETS = {RequestTarget.PROJECT}

    @property
    def job_retry(self):
        # Some tasks may already have committed. Retrying append would add them again.
        return None

    def init_request_args(self):
        super().init_request_args()
        try:
            self.import_args.import_mode = AnnotationImportMode(
                self.request.query_params.get("import_mode", "replace")
            ).value
        except ValueError:
            raise ValidationError("import_mode must be replace or append") from None

    def validate_request(self):
        if not enabled():
            raise MethodNotAllowed(
                self.request.method, detail="Project annotation import is disabled"
            )
        if not supported_format(self.import_args.format):
            raise ValidationError("This format is available through Import dataset only")
        super().validate_request()

    def _get_payload_file(self):
        serializer = AnnotationFileSerializer(data=self.request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data["annotation_file"]

    def _init_callback_with_params(self):
        from .service import import_project_annotations

        self.callback = import_project_annotations
        self.callback_args = (
            str(self.tmp_dir / self.import_args.filename),
            self.db_instance.pk,
            self.import_args.format,
            self.import_args.conv_mask_to_poly,
        )
        self.callback_kwargs = {"import_mode": self.import_args.import_mode}

    def build_request_id(self):
        return ImportRequestId(
            action=RequestAction.IMPORT,
            target=RequestTarget.PROJECT,
            target_id=self.db_instance.pk,
            subresource=RequestSubresource.ANNOTATIONS,
        ).render()
