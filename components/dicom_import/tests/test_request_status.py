# SPDX-License-Identifier: MIT
"""Execute the native serializer method to check final import messages without Redis."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def message():
    tree = ast.parse((ROOT / "cvat/apps/redis_handler/serializers.py").read_text())
    serializer = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RequestSerializer"
    )
    method = next(
        node
        for node in serializer.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_message"
    )
    method.decorator_list = []
    namespace = {
        "CustomRQJob": object,
        "RQJobStatus": SimpleNamespace(STARTED="started", FINISHED="finished", FAILED="failed"),
        "models": SimpleNamespace(RequestAction=SimpleNamespace(IMPORT="import")),
        "parse_exception_message": lambda value: f"parsed: {value}",
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), "<native get_message>", "exec"),
        namespace,
    )

    def get(
        summary, status="finished", action="import", target="project", formatted_exception=None
    ):
        # DatasetImporter uses ImportRequestId, which intentionally has no format field.
        request = SimpleNamespace(
            get_status=lambda **kwargs: status,
            parsed_id=SimpleNamespace(action=action, target=target),
            exc_info="original exception",
        )
        serializer = SimpleNamespace(
            _base_rq_job_meta=SimpleNamespace(
                status=summary,
                formatted_exception=formatted_exception,
            )
        )
        return namespace["get_message"](serializer, request)

    return get


@pytest.mark.parametrize(
    "summary",
    [
        "DICOM: 2 tasks created; 0 tasks failed",
        "WARNING: DICOM: 1 tasks created; 1 tasks failed",
    ],
)
def test_finished_extension_project_import_preserves_final_counts(message, summary):
    assert message(summary) == summary


@pytest.mark.parametrize(
    "summary",
    [None, "", "Dataset import has been started...", "Task guides: 1 created", "Unrelated status"],
)
def test_ordinary_finished_request_behavior_is_unchanged(message, summary):
    assert message(summary) == ""


@pytest.mark.parametrize(
    "action,target", [("export", "project"), ("import", "task"), ("create", "task")]
)
def test_extension_summary_scope_does_not_affect_other_operations(message, action, target):
    assert message("WARNING: DICOM: counts", action=action, target=target) == ""


def test_failed_request_uses_error_message_and_started_request_uses_status(message):
    assert message("anything", status="started") == "anything"
    assert message("DICOM: counts", status="failed") == "parsed: original exception"
    assert (
        message("DICOM: counts", status="failed", formatted_exception="all tasks failed")
        == "all tasks failed"
    )
