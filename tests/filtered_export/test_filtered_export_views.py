# SPDX-License-Identifier: MIT
"""Request-manager unit tests with the CVAT/Django boundary explicitly doubled."""
import io
import json
import types
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qsl, urlencode

from test_filtered_export_service import load, ValidationError


class RequestId:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def render(self):
        return urlencode(self.__dict__)

    @classmethod
    def parse_and_validate_queue(cls, value, **kwargs):
        fields = dict(parse_qsl(value))
        for key in ("target_id", "user_id"):
            fields[key] = int(fields[key])
        return cls(**fields)


class ExportArgs(types.SimpleNamespace):
    pass


class NativeExporter:
    QUEUE_NAME = "export"

    def __init__(self, *, request, db_instance):
        self.request = request
        self.db_instance = db_instance
        self.user_id = request.user.id
        self.target = "task"

    def init_request_args(self):
        self.export_args = ExportArgs(format=self.request.query_params["format"],
            save_images=self.request.query_params.get("save_images", False),
            location_config=types.SimpleNamespace(location=self.request.query_params.get("location", "local")))

    def validate_request(self):
        assert self.request.method == "POST"

    def build_request_id(self):
        return RequestId(action="export", target=self.target, target_id=self.db_instance.pk,
                         user_id=self.user_id, subresource="dataset", format=self.export_args.format).render()

    def validate_request_id(self, value):
        parsed = RequestId.parse_and_validate_queue(value)
        if parsed.target_id != self.db_instance.pk or parsed.target != self.target:
            raise ValueError("Wrong resource")

    def get_result_filename(self):
        return "native.zip"

    def enqueue_job(self):
        return "native queue"

    def get_downloader(self):
        return types.SimpleNamespace(download_file=lambda: "native download")


class JSONParser:
    def parse(self, stream, *args):
        return json.load(stream)


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.preview = Mock(return_value={"count": 2})
        self.check = Mock()
        self.worker = Mock()
        self.views = load("views", {
            "rest_framework.exceptions": {"ValidationError": ValidationError, "ParseError": ValidationError},
            "rest_framework.parsers": {"JSONParser": JSONParser},
            "rest_framework.response": {"Response": lambda data: data},
            "rest_framework.reverse": {"reverse": lambda name, **kwargs: name},
            "cvat.apps.engine.background": {"DatasetExporter": NativeExporter},
            "cvat.apps.engine.location": {"Location": types.SimpleNamespace(LOCAL="local")},
            "cvat.apps.engine.rq": {"ExportRequestId": RequestId},
            "cvat.apps.engine.views": {"ProjectViewSet": type("ProjectViewSet", (), {}), "TaskViewSet": type("TaskViewSet", (), {})},
            "filtered_export_tested.service": {"check_revision": self.check, "export_filtered": self.worker,
                "get_options": lambda instance: {"labels": []}, "preview": self.preview},
        })
        self.request = types.SimpleNamespace(method="POST", user=types.SimpleNamespace(id=7),
            data={"filter": {"mode": "any", "tag_ids": [1]}, "expected_revision": "a" * 64},
            query_params={"format": "CVAT for images 1.1", "save_images": True},
            build_absolute_uri=lambda path: "http://localhost" + path)
        self.instance = types.SimpleNamespace(pk=1)

    def manager(self, **kwargs):
        manager = self.views.FilteredDatasetExporter(request=self.request, db_instance=self.instance, **kwargs)
        manager.init_request_args()
        return manager

    def test_validates_revision_before_enqueue(self):
        self.manager().validate_request()
        self.check.assert_called_once()

    def test_preview_does_not_require_revision_or_enqueue(self):
        del self.request.data["expected_revision"]
        view = self.views.FilteredTaskViewSet()
        view.get_object = Mock(return_value=self.instance)
        self.assertEqual(view.initiate_dataset_export(self.request, 1, preview_only=True), {"count": 2})
        view.get_object.assert_called_once()
        self.check.assert_not_called()
        self.worker.assert_not_called()

    def test_export_delegates_to_native_queue_after_object_permission_check(self):
        view = self.views.FilteredTaskViewSet()
        view.get_object = Mock(return_value=self.instance)
        self.assertEqual(view.initiate_dataset_export(self.request, 1), "native queue")
        view.get_object.assert_called_once()

    def test_download_requires_get_object_and_native_downloader(self):
        view = self.views.FilteredTaskViewSet()
        view.get_object = Mock(return_value=self.instance)
        self.assertEqual(view.download_dataset(self.request, 1), "native download")
        view.get_object.assert_called_once()

    def test_malformed_body_or_missing_preview_is_rejected(self):
        for body in ([], {}, {"filter": {"mode": "all_frames"}, "extra": 1},
                     {"filter": {"mode": "all_frames"}, "omit_tags": "false"},
                     {"filter": {"mode": "all_frames"}, "expected_revision": 12}):
            self.request.data = body
            with self.subTest(body=body), self.assertRaises(ValidationError):
                self.manager()

    def test_json_array_rejected_during_parsing_before_native_iam(self):
        with self.assertRaises(ValidationError):
            self.views.ObjectJSONParser().parse(io.StringIO("[]"))
        self.assertEqual(self.views.ObjectJSONParser().parse(io.StringIO('{"filter":{}}')), {"filter": {}})

    def test_requires_supported_format_local_storage_and_itg_tag_omission(self):
        for params in ({"format": "CVAT for video 1.1"},
                       {"format": "CVAT for images 1.1", "location": "cloud_storage"},
                       {"format": "ITGformat 1.0"}):
            self.request.query_params = params
            with self.subTest(params=params), self.assertRaises(ValidationError):
                self.manager().validate_request()

    def test_request_id_isolated_and_actual_export_format_unchanged(self):
        manager = self.manager()
        manager.validate_request()
        request_id = manager.build_request_id()
        self.assertNotEqual(request_id, NativeExporter.build_request_id(manager))
        manager.validate_request_id(request_id)
        manager._init_callback_with_params()
        self.assertEqual(manager.callback_kwargs["format_name"], "CVAT for images 1.1")
        self.assertEqual(manager.callback_kwargs["signature"], manager.signature)
        self.assertEqual(manager.get_result_endpoint_url(), "filtered-task-download")
        self.assertEqual(manager.get_result_filename(), "filtered_native.zip")

    def test_other_users_resources_and_normal_request_ids_are_rejected(self):
        manager = self.manager()
        request_id = manager.build_request_id()
        for changed in (request_id.replace("user_id=7", "user_id=8"),
                        request_id.replace("target_id=1", "target_id=2"),
                        NativeExporter.build_request_id(manager)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                manager.validate_request_id(changed)

    def test_cache_and_request_signature_includes_every_export_option(self):
        original = self.manager().signature
        mutations = [lambda: self.request.data["filter"].update(tag_ids=[2]),
                     lambda: self.request.data.update(expected_revision="b" * 64),
                     lambda: self.request.data.update(omit_tags=True),
                     lambda: self.request.query_params.update(save_images=False),
                     lambda: self.request.query_params.update(format="COCO 1.0"),
                     lambda: setattr(self.request.user, "id", 9),
                     lambda: setattr(self.instance, "pk", 3)]
        for mutate in mutations:
            mutate()
            changed = self.manager().signature
            self.assertNotEqual(changed, original)
            original = changed


if __name__ == "__main__":
    unittest.main()
