# SPDX-License-Identifier: MIT

import argparse
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cvat_cli._internal.commands_all import COMMANDS
from cvat_cli._internal.common import CriticalError


def response(data):
    return None, SimpleNamespace(data=json.dumps(data).encode())


def run(client, *args):
    parser = argparse.ArgumentParser()
    COMMANDS.configure_parser(parser)
    params = vars(parser.parse_args(["job", *args]))
    execute = params.pop("_executor")
    assert params.pop("_needs_client") is True
    execute(client, **params)


@pytest.fixture
def annotations():
    return {
        "version": 0,
        "tags": [
            {"id": 7, "label_id": 2, "frame": 100, "group": 0, "source": "manual",
             "attributes": [{"spec_id": 3, "value": "old"}, {"spec_id": 4, "value": "keep"}]},
            {"id": 8, "label_id": 3, "frame": 101, "attributes": []},
        ],
        "shapes": [
            {"id": 7, "label_id": 2, "frame": 100, "type": "polygon",
             "points": [0, 0, 5, 0, 5, 5], "occluded": True, "rotation": 0,
             "attributes": [{"spec_id": 6, "value": "keep"}]}
        ],
        "tracks": [
            {"id": 9, "label_id": 4, "frame": 100, "attributes": [],
             "shapes": [{"id": 12, "frame": 100}, {"id": 13, "frame": 102}]}
        ],
        "intervals": [{"id": 10, "label_id": 4, "start": 100, "stop": 102}],
    }


@pytest.fixture
def client(annotations):
    client = Mock()
    api = client.api_client.jobs_api
    api.retrieve_annotations.return_value = response(annotations)
    api.partial_update_annotations.return_value = response({"version": 0})
    return client


def test_unfiltered_get_preserves_payload(client, annotations, capsys):
    run(client, "annotations", "get", "42")
    assert json.loads(capsys.readouterr().out) == annotations
    client.api_client.jobs_api.retrieve_annotations.assert_called_once_with(id=42)
    client.api_client.jobs_api.partial_update_annotations.assert_not_called()


@pytest.mark.parametrize(
    ("options", "expected_ids"),
    [
        (["--type", "tag"], {"tags": [7, 8]}),
        (["--id", "7"], {"tags": [7], "shapes": [7]}),
        (["--label-id", "2", "--frame", "100"], {"tags": [7], "shapes": [7]}),
        (["--type", "track", "--frame", "102"], {"tracks": [9]}),
        (["--type", "track", "--frame", "101"], {}),
        (["--type", "interval"], {"intervals": [10]}),
    ],
)
def test_filters_stored_objects(client, options, expected_ids, capsys):
    run(client, "annotations", "get", "42", *options)
    data = json.loads(capsys.readouterr().out)
    assert {
        key: [item["id"] for item in values]
        for key, values in data.items() if key != "version" and values
    } == expected_ids


def test_update_tag_merges_attributes_and_keeps_other_fields(client, annotations):
    run(client, "annotations", "update", "42", "--type", "tag", "--id", "7",
        "--attribute", "3=Yes", "--attribute", "5=a=b")
    expected = dict(annotations["tags"][0], attributes=[
        {"spec_id": 3, "value": "Yes"}, {"spec_id": 4, "value": "keep"},
        {"spec_id": 5, "value": "a=b"},
    ])
    client.api_client.jobs_api.partial_update_annotations.assert_called_once_with(
        id=42, action="update", patched_labeled_data_request={"tags": [expected]}
    )
    assert annotations["tags"][0]["attributes"][0]["value"] == "old"


def test_update_shape_preserves_other_data(client, annotations):
    run(client, "annotations", "update", "42", "--type", "shape", "--id", "7",
        "--points", "1", "1", "2", "1", "2", "2")
    expected = dict(annotations["shapes"][0], points=[1, 1, 2, 1, 2, 2])
    client.api_client.jobs_api.partial_update_annotations.assert_called_once_with(
        id=42, action="update", patched_labeled_data_request={"shapes": [expected]}
    )


def test_json_update_preserves_track_keyframes(client, annotations, tmp_path):
    path = tmp_path / "patch.json"
    path.write_text(json.dumps({"id": 9, "attributes": [{"spec_id": 5, "value": "x"}]}))
    run(client, "annotations", "update", "42", "--type", "track", "--id", "9",
        "--file", str(path))
    expected = dict(annotations["tracks"][0], attributes=[{"spec_id": 5, "value": "x"}])
    client.api_client.jobs_api.partial_update_annotations.assert_called_once_with(
        id=42, action="update", patched_labeled_data_request={"tracks": [expected]}
    )


def test_json_update_rejects_field_absent_from_retrieved_annotation(client, tmp_path):
    # The second tag has no group key, even though group is a valid API field.
    path = tmp_path / "patch.json"
    path.write_text(json.dumps({"group": 2}))
    with pytest.raises(CriticalError, match="Unknown fields for this annotation.*group"):
        run(client, "annotations", "update", "42", "--type", "tag", "--id", "8",
            "--file", str(path))
    client.api_client.jobs_api.partial_update_annotations.assert_not_called()


@pytest.mark.parametrize(
    "changes", [{"id": 8, "group": 2}, {"attributess": []}, {"id": 7}, []]
)
def test_invalid_json_update_does_not_write(client, changes, tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(changes))
    with pytest.raises(CriticalError):
        run(client, "annotations", "update", "42", "--type", "tag", "--id", "7",
            "--file", str(path))
    client.api_client.jobs_api.partial_update_annotations.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        ["--type", "tag", "--id", "999", "--attribute", "3=Yes"],
        ["--type", "tag", "--id", "7"],
        ["--type", "tag", "--id", "7", "--points", "1", "2"],
        ["--type", "tag", "--id", "7", "--attribute", "3=x", "--attribute", "3=y"],
        ["--type", "tag", "--id", "7", "--attribute", "3=x", "--file", "-"],
    ],
)
def test_invalid_update_does_not_write(client, options):
    with pytest.raises(CriticalError):
        run(client, "annotations", "update", "42", *options)
    client.api_client.jobs_api.partial_update_annotations.assert_not_called()


@pytest.mark.parametrize("kind, key, annotation_id", [
    ("tag", "tags", 7), ("shape", "shapes", 7), ("track", "tracks", 9),
    ("interval", "intervals", 10),
])
def test_delete_is_scoped_by_type_and_id(client, annotations, kind, key, annotation_id):
    run(client, "annotations", "delete", "42", "--type", kind, "--id", str(annotation_id))
    expected = next(item for item in annotations[key] if item["id"] == annotation_id)
    client.api_client.jobs_api.partial_update_annotations.assert_called_once_with(
        id=42, action="delete", patched_labeled_data_request={key: [expected]}
    )
    client.api_client.jobs_api.destroy_annotations.assert_not_called()


def test_delete_missing_id_does_not_write(client):
    with pytest.raises(CriticalError, match="not found"):
        run(client, "annotations", "delete", "42", "--type", "tag", "--id", "999")
    client.api_client.jobs_api.partial_update_annotations.assert_not_called()


def test_create_uses_patch_and_returns_created_ids(client, tmp_path, capsys):
    data = {"tags": [{"frame": 100, "label_id": 2, "attributes": []}]}
    path = tmp_path / "create.json"
    path.write_text(json.dumps(data))
    result = {"tags": [dict(data["tags"][0], id=99)]}
    client.api_client.jobs_api.partial_update_annotations.return_value = response(result)
    run(client, "annotations", "create", "42", "--file", str(path))
    client.api_client.jobs_api.partial_update_annotations.assert_called_once_with(
        id=42, action="create", patched_labeled_data_request=data
    )
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize("data", [
    {}, {"tags": []}, {"tags": {}}, {"unknown": [{}]},
    {"tags": [{"id": 1}]}, {"tracks": [{"shapes": [{"id": 2}]}]},
])
def test_invalid_create_does_not_write(client, data, tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    with pytest.raises(CriticalError):
        run(client, "annotations", "create", "42", "--file", str(path))
    client.api_client.jobs_api.partial_update_annotations.assert_not_called()
