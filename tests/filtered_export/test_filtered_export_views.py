# SPDX-License-Identifier: MIT
"""Request-manager unit tests; native IAM and Redis are boundary doubles."""
import io
import json
import types
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qsl, urlencode

from test_filtered_export_service import PACKAGE, Task, load, ValidationError
from test_filtered_export_selection import rule


class RequestId:
    def __init__(self, **fields): self.__dict__.update(fields)
    def render(self): return urlencode(self.__dict__)
    @classmethod
    def parse_and_validate_queue(cls, value, **kwargs):
        fields = dict(parse_qsl(value))
        for key in ("target_id", "user_id"):
            fields[key] = int(fields[key])
        return cls(**fields)


class NativeExporter:
    QUEUE_NAME = "export"
    def __init__(self, *, request, db_instance):
        self.request, self.db_instance = request, db_instance
        self.user_id, self.target = request.user.id, "task"
    def init_request_args(self):
        self.export_args = types.SimpleNamespace(format=self.request.query_params["format"],
            save_images=self.request.query_params.get("save_images", False),
            location_config=types.SimpleNamespace(location=self.request.query_params.get("location", "local")))
    def validate_request(self): assert self.request.method == "POST"
    def build_request_id(self):
        return RequestId(action="export", target=self.target, target_id=self.db_instance.pk,
                         user_id=self.user_id, subresource="dataset", format=self.export_args.format).render()
    def validate_request_id(self, value):
        parsed = RequestId.parse_and_validate_queue(value)
        if parsed.target_id != self.db_instance.pk or parsed.target != self.target:
            raise ValueError("Wrong resource")
    def get_result_filename(self): return "native.zip"
    def enqueue_job(self): return "native queue"
    def get_downloader(self): return types.SimpleNamespace(download_file=lambda: "native download")


class JSONParser:
    def parse(self, stream, *args): return json.load(stream)


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.preview, self.check, self.worker = Mock(return_value={"count": 2}), Mock(), Mock()
        self.views = load("views", {
            "rest_framework.exceptions": {"ValidationError": ValidationError, "ParseError": ValidationError},
            "rest_framework.parsers": {"JSONParser": JSONParser},
            "rest_framework.response": {"Response": lambda data: data},
            "rest_framework.reverse": {"reverse": lambda name, **kwargs: name},
            "cvat.apps.engine.background": {"DatasetExporter": NativeExporter},
            "cvat.apps.engine.location": {"Location": types.SimpleNamespace(LOCAL="local")},
            "cvat.apps.engine.rq": {"ExportRequestId": RequestId},
            "cvat.apps.engine.views": {"ProjectViewSet": type("ProjectViewSet", (), {}), "TaskViewSet": type("TaskViewSet", (), {})},
            f"{PACKAGE}.service": {"check_revision": self.check, "export_filtered": self.worker,
                "get_options": lambda instance: {"labels": []}, "preview": self.preview,
                "IMAGE_EXPORT_FORMATS": ("CVAT for images 1.1", "COCO 1.0", "ITGformat 1.0")},
        })
        self.request = types.SimpleNamespace(method="POST", user=types.SimpleNamespace(id=7),
            data={"filters": [rule("type", "tag")], "expected_revision": "a" * 64},
            query_params={"format": "CVAT for images 1.1", "save_images": True},
            build_absolute_uri=lambda path: "http://localhost" + path)
        self.instance = Task()

    def manager(self):
        manager = self.views.FilteredDatasetExporter(request=self.request, db_instance=self.instance)
        manager.init_request_args()
        return manager

    def test_validates_revision_before_enqueue(self):
        self.manager().validate_request()
        self.check.assert_called_once()

    def test_preview_needs_no_format_export_options_or_revision(self):
        self.request.query_params = {"location": "local"}
        del self.request.data["expected_revision"]
        view = self.views.FilteredTaskViewSet()
        view.get_object = Mock(return_value=self.instance)
        self.assertEqual(view.initiate_dataset_export(self.request, 1, preview_only=True), {"count": 2})
        self.assertEqual(self.preview.call_args.args[0], "task")
        view.get_object.assert_called_once()
        self.check.assert_not_called()
        self.worker.assert_not_called()

    def test_preview_is_not_restricted_by_selected_exporter(self):
        self.request.data = {"filters": []}
        view = self.views.FilteredTaskViewSet()
        view.get_object = Mock(return_value=self.instance)
        for name in ("ITGformat 1.0", "no such exporter"):
            self.request.query_params = {"format": name}
            self.assertEqual(view.initiate_dataset_export(self.request, 1, preview_only=True), {"count": 2})

    def test_native_queue_and_download_are_after_object_permission_check(self):
        view = self.views.FilteredTaskViewSet()
        view.get_object = Mock(return_value=self.instance)
        self.assertEqual(view.initiate_dataset_export(self.request, 1), "native queue")
        self.assertEqual(view.download_dataset(self.request, 1), "native download")
        self.assertEqual(view.get_object.call_count, 2)
        view.get_object.side_effect = ValidationError("denied")
        with self.assertRaises(ValidationError):
            view.initiate_dataset_export(self.request, 1, preview_only=True)
        self.preview.assert_not_called()

    def test_removed_fields_and_missing_filter_fail_closed(self):
        for field in ("start", "stop", "step", "mode", "tag_ids", "filter", "omit_tags"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.views.read_filters({"filters": [], field: 1})
        for payload in ([], {}, {"filters": [1]}, {"filters": {"mode": "any"}}):
            with self.assertRaises(ValidationError):
                self.views.read_filters(payload)

    def test_json_arrays_rejected_before_native_permission_parser(self):
        with self.assertRaises(ValidationError):
            self.views.ObjectJSONParser().parse(io.StringIO("[]"))
        self.assertEqual(self.views.ObjectJSONParser().parse(io.StringIO('{"filters":[]}')), {"filters": []})

    def test_export_requires_preview_and_local_image_adapter(self):
        for revision in ("", None, 123, "not a revision"):
            self.request.data["expected_revision"] = revision
            with self.assertRaises(ValidationError): self.manager()
        self.request.data["expected_revision"] = "a" * 64
        for params in ({"format": "CVAT for video 1.1"}, {"format": "COCO 1.0", "location": "cloud_storage"}):
            self.request.query_params = params
            with self.assertRaises(ValidationError): self.manager().validate_request()

    def test_itg_is_not_subject_to_tag_removal_requirements(self):
        self.request.query_params["format"] = "ITGformat 1.0"
        manager = self.manager()
        manager.validate_request()
        manager._init_callback_with_params()
        self.assertEqual(manager.callback_kwargs["filters_spec"], self.request.data["filters"])
        self.assertEqual(manager.callback_kwargs["format_name"], "ITGformat 1.0")
        self.assertNotIn("omit_tags", manager.callback_kwargs)

    def test_request_and_cache_identifiers_are_isolated(self):
        manager = self.manager()
        request_id = manager.build_request_id()
        self.assertNotEqual(request_id, NativeExporter.build_request_id(manager))
        manager.validate_request_id(request_id)
        manager._init_callback_with_params()
        self.assertEqual(manager.callback_kwargs["signature"], manager.signature)
        self.assertEqual(manager.get_result_filename(), "filtered_native.zip")
        self.assertEqual(manager.get_result_endpoint_url(), "filtered-task-download")
        for changed in (request_id.replace("user_id=7", "user_id=8"),
                        request_id.replace("target_id=1", "target_id=2"),
                        NativeExporter.build_request_id(manager)):
            with self.assertRaises(ValueError): manager.validate_request_id(changed)

    def test_signature_accounts_for_filter_revision_owner_and_export_options(self):
        original = self.manager().signature
        for mutate in (lambda: self.request.data.update(filters=[rule("type", "shape")]),
                       lambda: self.request.data.update(expected_revision="b" * 64),
                       lambda: self.request.query_params.update(save_images=False),
                       lambda: self.request.query_params.update(format="COCO 1.0"),
                       lambda: setattr(self.request.user, "id", 8)):
            mutate()
            changed = self.manager().signature
            self.assertNotEqual(original, changed)
            original = changed


if __name__ == "__main__":
    unittest.main()
