# SPDX-License-Identifier: MIT

import argparse
from unittest.mock import Mock

import pytest
from cvat_cli._internal.commands_all import COMMANDS
from cvat_cli._internal.common import CriticalError
from cvat_sdk import exceptions


@pytest.fixture
def parser():
    parser = argparse.ArgumentParser()
    COMMANDS.configure_parser(parser)
    return parser


def execute(parser, client, args):
    params = vars(parser.parse_args(["job", "update", *args]))
    executor = params.pop("_executor")
    assert params.pop("_needs_client") is True
    executor(client, **params)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--assignee", "7"], {"assignee": 7}),
        (["--unassign"], {"assignee": None}),
        (["--stage", "annotation"], {"stage": "annotation"}),
        (["--stage", "validation"], {"stage": "validation"}),
        (["--stage", "acceptance"], {"stage": "acceptance"}),
        (["--state", "new"], {"state": "new"}),
        (["--state", "in progress"], {"state": "in progress"}),
        (["--state", "completed"], {"state": "completed"}),
        (["--state", "rejected"], {"state": "rejected"}),
        (
            ["--assignee", "7", "--stage", "validation", "--state", "new"],
            {"assignee": 7, "stage": "validation", "state": "new"},
        ),
        (
            ["--unassign", "--stage", "annotation", "--state", "rejected"],
            {"assignee": None, "stage": "annotation", "state": "rejected"},
        ),
    ],
)
def test_update_sends_only_specified_fields(parser, args, expected):
    client = Mock()
    execute(parser, client, ["42", *args])
    client.api_client.jobs_api.partial_update.assert_called_once_with(
        id=42, patched_job_write_request=expected
    )


def test_empty_update_does_not_call_api(parser):
    client = Mock()
    with pytest.raises(CriticalError, match="Specify at least one"):
        execute(parser, client, ["42"])
    client.api_client.jobs_api.partial_update.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["0", "--state", "new"],
        ["-1", "--state", "new"],
        ["abc", "--state", "new"],
        ["42", "--assignee", "0"],
        ["42", "--assignee", "abc"],
        ["42", "--assignee", "7", "--unassign"],
        ["42", "--stage", "completed"],
        ["42", "--state", "validation"],
    ],
)
def test_invalid_arguments_do_not_call_api(parser, args):
    client = Mock()
    with pytest.raises(SystemExit) as error:
        execute(parser, client, args)
    assert error.value.code == 2
    client.api_client.jobs_api.partial_update.assert_not_called()


@pytest.mark.parametrize("status", [400, 403, 404])
def test_api_errors_propagate_to_existing_cli_error_handler(parser, status):
    client = Mock()
    error = exceptions.ApiException(status=status, reason="update failed")
    client.api_client.jobs_api.partial_update.side_effect = error
    with pytest.raises(exceptions.ApiException) as caught:
        execute(parser, client, ["42", "--state", "completed"])
    assert caught.value is error
    client.api_client.jobs_api.partial_update.assert_called_once()


def test_job_update_help(parser, capsys):
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["job", "update", "--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    for flag in ("--assignee", "--unassign", "--stage", "--state"):
        assert flag in help_text
