# SPDX-License-Identifier: MIT

import argparse
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cvat_cli._internal import commands_job_issues, commands_jobs
from cvat_cli._internal.commands_all import COMMANDS
from cvat_cli._internal.common import CriticalError


def run(client, *args):
    parser = argparse.ArgumentParser()
    COMMANDS.configure_parser(parser)
    params = vars(parser.parse_args(["job", *args]))
    execute = params.pop("_executor")
    assert params.pop("_needs_client") is True
    execute(client, **params)


@pytest.fixture
def client():
    client = Mock()
    client.jobs.retrieve.return_value = SimpleNamespace(
        id=42, organization=2, start_frame=100, stop_frame=199
    )
    client.issues.retrieve.return_value = SimpleNamespace(id=15, job=42)
    client.issues.create.return_value = SimpleNamespace(id=16)
    client.comments.create.return_value = SimpleNamespace(id=17)
    return client


def test_job_list_json_and_filters(client, capsys):
    client.jobs.list.return_value = '[{"id": 42}]'
    run(client, "ls", "--json", "--task-id", "5", "--project-id", "3")
    client.jobs.list.assert_called_once_with(return_json=True, task_id=5, project_id=3)
    assert json.loads(capsys.readouterr().out) == [{"id": 42}]


def test_job_list_default_ids(client, capsys):
    client.jobs.list.return_value = [SimpleNamespace(id=42)]
    run(client, "ls")
    client.jobs.list.assert_called_once_with(return_json=False)
    assert capsys.readouterr().out == "42\n"


def test_job_get_json(client, capsys):
    client.api_client.jobs_api.retrieve.return_value = (
        None, SimpleNamespace(data=b'{"id":42}')
    )
    run(client, "get", "42")
    client.api_client.jobs_api.retrieve.assert_called_once_with(id=42)
    assert json.loads(capsys.readouterr().out) == {"id": 42}


def test_job_labels_scope(client, monkeypatch, capsys):
    data = [{"id": 5, "attributes": [{"id": 3, "name": "Required"}]}]
    collection = Mock(return_value=data)
    monkeypatch.setattr(commands_jobs, "get_paginated_collection", collection)
    run(client, "labels", "42")
    collection.assert_called_once_with(
        client.api_client.labels_api.list_endpoint, org_id=2, job_id=42, return_json=True
    )
    assert json.loads(capsys.readouterr().out) == data


@pytest.mark.parametrize(
    ("flags", "filters"),
    [([], {}), (["--resolved"], {"resolved": True}),
     (["--unresolved", "--frame", "100"], {"resolved": False, "frame_id": 100})],
)
def test_issue_list_scope(client, flags, filters, monkeypatch, capsys):
    collection = Mock(return_value=[{"id": 15}])
    monkeypatch.setattr(commands_job_issues, "get_paginated_collection", collection)
    run(client, "issue", "ls", "42", *flags)
    collection.assert_called_once_with(
        client.api_client.issues_api.list_endpoint, org_id=2, job_id=42,
        return_json=True, **filters,
    )
    assert json.loads(capsys.readouterr().out) == [{"id": 15}]


def test_issue_create(client, capsys):
    run(client, "issue", "create", "42", "--frame", "100",
        "--position", "0", "0", "100", "100", "--message", "Missing tag", "--assignee", "7")
    client.issues.create.assert_called_once_with({
        "job": 42, "frame": 100, "position": [0, 0, 100, 100],
        "message": "Missing tag", "assignee": 7,
    })
    assert capsys.readouterr().out == "16\n"


@pytest.mark.parametrize(("frame", "position", "message"), [
    ("99", ["0", "0"], "Missing tag"),
    ("200", ["0", "0"], "Missing tag"),
    ("100", ["0"], "Missing tag"),
    ("100", ["0", "0"], " "),
])
def test_issue_create_rejects_invalid_data(client, frame, position, message):
    with pytest.raises(CriticalError):
        run(client, "issue", "create", "42", "--frame", frame,
            "--position", *position, "--message", message)
    client.issues.create.assert_not_called()


@pytest.mark.parametrize(("action", "resolved"), [("resolve", True), ("reopen", False)])
def test_issue_resolution(client, action, resolved):
    run(client, "issue", action, "42", "--issue-id", "15")
    client.issues.retrieve.assert_called_once_with(15)
    client.api_client.issues_api.partial_update.assert_called_once_with(
        id=15, patched_issue_write_request={"resolved": resolved}
    )


def test_issue_comment(client, capsys):
    run(client, "issue", "comment", "42", "--issue-id", "15", "--message", "Fixed")
    client.issues.retrieve.assert_called_once_with(15)
    client.comments.create.assert_called_once_with({"issue": 15, "message": "Fixed"})
    assert capsys.readouterr().out == "17\n"


def test_issue_comments_scope(client, monkeypatch, capsys):
    collection = Mock(return_value=[{"id": 17, "message": "Fixed"}])
    monkeypatch.setattr(commands_job_issues, "get_paginated_collection", collection)
    run(client, "issue", "comments", "42", "--issue-id", "15")
    collection.assert_called_once_with(
        client.api_client.comments_api.list_endpoint, org_id=2, issue_id=15, return_json=True
    )
    assert json.loads(capsys.readouterr().out) == [{"id": 17, "message": "Fixed"}]


@pytest.mark.parametrize(("action", "extra"), [
    ("resolve", []), ("reopen", []), ("comment", ["--message", "Fixed"]), ("comments", [])
])
def test_issue_cannot_operate_on_other_job(client, action, extra, monkeypatch):
    client.issues.retrieve.return_value.job = 43
    collection = Mock()
    monkeypatch.setattr(commands_job_issues, "get_paginated_collection", collection)
    with pytest.raises(CriticalError, match="does not belong"):
        run(client, "issue", action, "42", "--issue-id", "15", *extra)
    client.api_client.issues_api.partial_update.assert_not_called()
    client.comments.create.assert_not_called()
    collection.assert_not_called()
