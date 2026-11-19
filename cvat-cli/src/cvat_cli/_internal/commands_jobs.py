# SPDX-License-Identifier: MIT

import argparse
import json

from cvat_sdk import Client
from cvat_sdk.core.helpers import get_paginated_collection

from .command_base import CommandGroup
from .commands_job_annotations import COMMANDS as ANNOTATION_COMMANDS
from .commands_job_issues import COMMANDS as ISSUE_COMMANDS
from .common import CriticalError
from .job_cli_common import positive_id, print_json, response_json

COMMANDS = CommandGroup(description="Perform operations on CVAT jobs.")
COMMANDS.add_command("annotations", ANNOTATION_COMMANDS)
COMMANDS.add_command("issue", ISSUE_COMMANDS)


@COMMANDS.command_class("ls")
class JobList:
    description = "List jobs in basic or JSON format."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--json", action="store_true", dest="use_json_output")
        parser.add_argument("--task-id", type=positive_id)
        parser.add_argument("--project-id", type=positive_id)

    def execute(self, client: Client, *, use_json_output=False, task_id=None, project_id=None):
        filters = {
            key: value for key, value in {"task_id": task_id, "project_id": project_id}.items()
            if value is not None
        }
        results = client.jobs.list(return_json=use_json_output, **filters)
        if use_json_output:
            print_json(json.loads(results))
        else:
            for job in results:
                print(job.id)


@COMMANDS.command_class("get")
class JobGet:
    description = "Read job details as JSON."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=positive_id)

    def execute(self, client: Client, *, job_id: int) -> None:
        _, response = client.api_client.jobs_api.retrieve(id=job_id)
        print_json(response_json(response))


@COMMANDS.command_class("labels")
class JobLabels:
    description = "Read all job label and attribute definitions as JSON."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=positive_id)

    def execute(self, client: Client, *, job_id: int) -> None:
        job = client.jobs.retrieve(job_id)
        result = get_paginated_collection(
            client.api_client.labels_api.list_endpoint,
            org_id=job.organization, job_id=job_id, return_json=True,
        )
        print_json(result)


@COMMANDS.command_class("update")
class JobUpdate:
    description = "Update the assignee, stage, or state of a CVAT job."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=positive_id, help="job ID")
        assignee = parser.add_mutually_exclusive_group()
        assignee.add_argument(
            "--assignee", type=positive_id, default=argparse.SUPPRESS, help="assignee user ID"
        )
        assignee.add_argument(
            "--unassign",
            dest="assignee",
            action="store_const",
            const=None,
            default=argparse.SUPPRESS,
            help="remove the current assignee",
        )
        parser.add_argument(
            "--stage",
            choices=("annotation", "validation", "acceptance"),
            default=argparse.SUPPRESS,
            help="new job stage (changing stage alone resets state to new)",
        )
        parser.add_argument(
            "--state",
            choices=("new", "in progress", "completed", "rejected"),
            default=argparse.SUPPRESS,
            help="new job state",
        )

    def execute(self, client: Client, *, job_id: int, **changes) -> None:
        if not changes:
            raise CriticalError("Specify at least one of --assignee, --unassign, --stage or --state")

        # Send only explicitly requested fields, including null when unassigning.
        # The server handles authorization and stage/state transitions.
        client.api_client.jobs_api.partial_update(
            id=job_id, patched_job_write_request=changes
        )
