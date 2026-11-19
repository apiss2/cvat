# SPDX-License-Identifier: MIT

import argparse

from cvat_sdk import Client

from .command_base import CommandGroup
from .common import CriticalError

COMMANDS = CommandGroup(description="Perform operations on CVAT jobs.")


def _positive_id(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer ID") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer ID")
    return result


@COMMANDS.command_class("update")
class JobUpdate:
    description = "Update the assignee, stage, or state of a CVAT job."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=_positive_id, help="job ID")
        assignee = parser.add_mutually_exclusive_group()
        assignee.add_argument(
            "--assignee", type=_positive_id, default=argparse.SUPPRESS, help="assignee user ID"
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
