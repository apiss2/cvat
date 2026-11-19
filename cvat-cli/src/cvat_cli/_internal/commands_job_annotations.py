# SPDX-License-Identifier: MIT

import argparse
from copy import deepcopy

from cvat_sdk import Client

from .command_base import CommandGroup
from .common import CriticalError
from .job_cli_common import (
    finite_float,
    nonnegative_int,
    positive_id,
    print_json,
    read_json_object,
    response_json,
)

COMMANDS = CommandGroup(description="Read and modify annotations in a CVAT job.")
COLLECTIONS = {"tag": "tags", "shape": "shapes", "track": "tracks", "interval": "intervals"}


def _get_annotations(client: Client, job_id: int) -> dict:
    _, response = client.api_client.jobs_api.retrieve_annotations(id=job_id)
    return response_json(response)


def _patch(client: Client, job_id: int, action: str, data: dict) -> None:
    _, response = client.api_client.jobs_api.partial_update_annotations(
        id=job_id, action=action, patched_labeled_data_request=data
    )
    print_json(response_json(response))


def _frame_matches(item, frame, kind):
    if kind == "interval":
        # Audio intervals use time rather than image frame indices.
        return False
    if item.get("frame") == frame:
        return True
    if kind == "track":
        return any(shape["frame"] == frame for shape in item.get("shapes", [])) or any(
            _frame_matches(element, frame, kind) for element in item.get("elements", [])
        )
    return False


@COMMANDS.command_class("get")
class AnnotationsGet:
    description = "Read annotations as JSON, optionally filtering stored objects."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=positive_id)
        parser.add_argument("--type", dest="kind", choices=COLLECTIONS)
        parser.add_argument("--id", dest="annotation_id", type=positive_id)
        parser.add_argument("--frame", type=nonnegative_int, help="stored image frame/keyframe")
        parser.add_argument("--label-id", type=positive_id)

    def execute(
        self, client: Client, *, job_id: int, kind=None, annotation_id=None,
        frame=None, label_id=None
    ) -> None:
        if kind == "interval" and frame is not None:
            raise CriticalError("Audio intervals cannot be filtered by image frame")
        data = _get_annotations(client, job_id)
        if all(value is None for value in (kind, annotation_id, frame, label_id)):
            print_json(data)
            return
        result = {key: value for key, value in data.items() if key == "version"}
        for name, collection in COLLECTIONS.items():
            if collection not in data:
                continue
            result[collection] = [
                item for item in data[collection]
                if (kind is None or kind == name)
                and (annotation_id is None or item["id"] == annotation_id)
                and (label_id is None or item["label_id"] == label_id)
                and (frame is None or _frame_matches(item, frame, name))
            ]
        print_json(result)


def _has_ids(value):
    if isinstance(value, dict):
        return value.get("id") is not None or any(_has_ids(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_ids(v) for v in value)
    return False


@COMMANDS.command_class("create")
class AnnotationsCreate:
    description = "Create annotations from a CVAT annotation JSON object."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("job_id", type=positive_id)
        parser.add_argument("--file", required=True, help="JSON file, or - for stdin")

    def execute(self, client: Client, *, job_id: int, file: str) -> None:
        data = read_json_object(file)
        unknown = data.keys() - set(COLLECTIONS.values()) - {"version"}
        if unknown:
            raise CriticalError(f"Unknown annotation collections: {sorted(unknown)}")
        collections = [value for key, value in data.items() if key != "version"]
        if not all(
            isinstance(value, list) and all(isinstance(item, dict) for item in value)
            for value in collections
        ):
            raise CriticalError("Annotation collections must be lists of objects")
        if not any(collections):
            raise CriticalError("No annotations to create")
        if _has_ids(data):
            raise CriticalError("Created annotations must not have existing IDs")
        _patch(client, job_id, "create", data)


def _configure_target(parser):
    parser.add_argument("job_id", type=positive_id)
    parser.add_argument("--type", dest="kind", choices=COLLECTIONS, required=True)
    parser.add_argument("--id", dest="annotation_id", type=positive_id, required=True)


def _find_annotation(client, job_id, kind, annotation_id):
    data = _get_annotations(client, job_id)
    matches = [
        item for item in data.get(COLLECTIONS[kind], []) if item["id"] == annotation_id
    ]
    if len(matches) != 1:
        raise CriticalError(f"{kind} {annotation_id} not found in job {job_id}")
    return deepcopy(matches[0])


def _parse_attribute(value):
    spec, separator, content = value.partition("=")
    if not separator:
        raise argparse.ArgumentTypeError("expected SPEC_ID=VALUE")
    return positive_id(spec), content


def _merge_attributes(item, attributes):
    updates = dict(attributes)
    if len(updates) != len(attributes):
        raise CriticalError("Duplicate attribute spec IDs")
    existing = item.get("attributes", [])
    for attribute in existing:
        spec_id = attribute["spec_id"]
        if spec_id in updates:
            attribute["value"] = updates.pop(spec_id)
    item["attributes"] = existing + [
        {"spec_id": spec_id, "value": value} for spec_id, value in updates.items()
    ]


def _apply_file(item, changes):
    if "id" in changes and changes["id"] != item["id"]:
        raise CriticalError("JSON annotation ID does not match the target ID")
    changes = {key: value for key, value in changes.items() if key != "id"}
    if not changes:
        raise CriticalError("No fields to update")
    unknown = changes.keys() - item.keys()
    if unknown:
        raise CriticalError(f"Unknown fields for this annotation: {sorted(unknown)}")
    item.update(changes)


@COMMANDS.command_class("update")
class AnnotationsUpdate:
    description = "Update a single stored annotation without replacing the job."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        _configure_target(parser)
        parser.add_argument("--attribute", type=_parse_attribute, action="append", default=[])
        parser.add_argument("--points", type=finite_float, nargs="+")
        parser.add_argument("--file", help="JSON object of replacement fields, or - for stdin")

    def execute(
        self, client: Client, *, job_id: int, kind: str, annotation_id: int,
        attribute: list, points=None, file=None,
    ) -> None:
        if file is not None and (attribute or points is not None):
            raise CriticalError("--file cannot be combined with --attribute or --points")
        if file is None and not attribute and points is None:
            raise CriticalError("Specify --attribute, --points or --file")
        if points is not None and kind != "shape":
            raise CriticalError("--points is only supported for shapes; use --file for tracks")
        changes = read_json_object(file) if file is not None else None
        item = _find_annotation(client, job_id, kind, annotation_id)
        if changes is not None:
            _apply_file(item, changes)
        if attribute:
            _merge_attributes(item, attribute)
        if points is not None:
            item["points"] = points
        _patch(client, job_id, "update", {COLLECTIONS[kind]: [item]})


@COMMANDS.command_class("delete")
class AnnotationsDelete:
    description = "Delete only the specified stored annotation."

    def configure_parser(self, parser: argparse.ArgumentParser) -> None:
        _configure_target(parser)

    def execute(self, client: Client, *, job_id: int, kind: str, annotation_id: int) -> None:
        item = _find_annotation(client, job_id, kind, annotation_id)
        _patch(client, job_id, "delete", {COLLECTIONS[kind]: [item]})
