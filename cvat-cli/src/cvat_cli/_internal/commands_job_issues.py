# SPDX-License-Identifier: MIT

import argparse

from cvat_sdk import Client
from cvat_sdk.core.helpers import get_paginated_collection

from .command_base import CommandGroup
from .common import CriticalError
from .job_cli_common import finite_float, nonnegative_int, positive_id, print_json

COMMANDS = CommandGroup(description="Manage issues belonging to a CVAT job.")


def _get_issue(client, job_id, issue_id):
    issue = client.issues.retrieve(issue_id)
    if issue.job != job_id:
        raise CriticalError(f"Issue {issue_id} does not belong to job {job_id}")
    return issue


def _configure_issue(parser):
    parser.add_argument("job_id", type=positive_id)
    parser.add_argument("--issue-id", required=True, type=positive_id)


def _nonempty_message(message):
    if not message.strip():
        raise CriticalError("Issue message must not be empty")
    return message


@COMMANDS.command_class("ls")
class IssueList:
    description = "List issues as JSON."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=positive_id)
        status = parser.add_mutually_exclusive_group()
        status.add_argument("--unresolved", dest="resolved", action="store_false", default=None)
        status.add_argument("--resolved", dest="resolved", action="store_true")
        parser.add_argument("--frame", type=nonnegative_int)

    def execute(self, client: Client, *, job_id: int, resolved=None, frame=None) -> None:
        job = client.jobs.retrieve(job_id)
        filters = {}
        if resolved is not None:
            filters["resolved"] = resolved
        if frame is not None:
            filters["frame_id"] = frame
        result = get_paginated_collection(
            client.api_client.issues_api.list_endpoint, org_id=job.organization,
            job_id=job_id, return_json=True, **filters,
        )
        print_json(result)


@COMMANDS.command_class("create")
class IssueCreate:
    description = "Create an issue and its initial comment."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=positive_id)
        parser.add_argument("--frame", type=nonnegative_int, required=True)
        parser.add_argument("--position", type=finite_float, nargs="+", required=True)
        parser.add_argument("--message", required=True)
        parser.add_argument("--assignee", type=positive_id)

    def execute(
        self, client: Client, *, job_id: int, frame: int, position: list,
        message: str, assignee=None
    ) -> None:
        if len(position) < 2 or len(position) % 2:
            raise CriticalError("--position requires one or more x/y coordinate pairs")
        _nonempty_message(message)
        job = client.jobs.retrieve(job_id)
        if not job.start_frame <= frame <= job.stop_frame:
            raise CriticalError(f"Frame {frame} is outside job {job_id}")
        data = {"job": job_id, "frame": frame, "position": position, "message": message}
        if assignee is not None:
            data["assignee"] = assignee
        issue = client.issues.create(data)
        print(issue.id)


class IssueSetResolved:
    resolved: bool

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        _configure_issue(parser)

    def execute(self, client: Client, *, job_id: int, issue_id: int) -> None:
        _get_issue(client, job_id, issue_id)
        client.api_client.issues_api.partial_update(
            id=issue_id, patched_issue_write_request={"resolved": self.resolved}
        )


@COMMANDS.command_class("resolve")
class IssueResolve(IssueSetResolved):
    description = "Mark an issue resolved."
    resolved = True


@COMMANDS.command_class("reopen")
class IssueReopen(IssueSetResolved):
    description = "Reopen a resolved issue."
    resolved = False


@COMMANDS.command_class("comment")
class IssueComment:
    description = "Add a comment to an issue."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        _configure_issue(parser)
        parser.add_argument("--message", required=True)

    def execute(self, client: Client, *, job_id: int, issue_id: int, message: str) -> None:
        _nonempty_message(message)
        _get_issue(client, job_id, issue_id)
        comment = client.comments.create({"issue": issue_id, "message": message})
        print(comment.id)


@COMMANDS.command_class("comments")
class IssueComments:
    description = "Read all comments of an issue as JSON."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        _configure_issue(parser)

    def execute(self, client: Client, *, job_id: int, issue_id: int) -> None:
        _get_issue(client, job_id, issue_id)
        job = client.jobs.retrieve(job_id)
        result = get_paginated_collection(
            client.api_client.comments_api.list_endpoint, org_id=job.organization,
            issue_id=issue_id, return_json=True,
        )
        print_json(result)
