# SPDX-License-Identifier: MIT
import json
from copy import deepcopy

import pytest

from components.physical_measurements.validation import (
    MAX_DOCUMENT_BYTES,
    empty_document,
    parse_document_bytes,
    restrict_to_segment,
    validate_document,
    validate_update,
)


def calibration():
    return {
        "version": 1,
        "default": {"x": 0.7, "y": 1.2, "unit": "mm"},
        "frames": {"1": {"x": 0.5, "y": 0.25, "unit": "mm"}, "3": None},
    }


def test_preserves_anisotropic_spacing_and_null_frame_override():
    original = calibration()
    result = validate_document(original, frame_count=5)
    assert result == original
    result["default"]["x"] = 9
    result["frames"]["1"]["y"] = 9
    assert original["default"]["x"] == 0.7
    assert original["frames"]["1"]["y"] == 0.25


def test_no_spacing_is_distinct_from_unit_spacing():
    assert validate_document(empty_document(), frame_count=5)["default"] is None
    value = {**empty_document(), "default": {"x": 1, "y": 1, "unit": "mm"}}
    assert validate_document(value, frame_count=5)["default"]["x"] == 1


@pytest.mark.parametrize(
    "number", [True, False, 0, -1, "0.5", None, float("nan"), float("inf"), 10**400]
)
@pytest.mark.parametrize("axis", ["x", "y"])
def test_rejects_invalid_spacing_numbers(number, axis):
    value = calibration()
    value["default"][axis] = number
    with pytest.raises(ValueError, match="finite, positive"):
        validate_document(value, frame_count=5)


@pytest.mark.parametrize(
    "spacing",
    [
        [],
        1,
        "0.5",
        {},
        {"x": 1, "y": 1},
        {"x": 1, "y": 1, "unit": "cm"},
        {"x": 1, "y": 1, "unit": "mm", "z": 1},
    ],
)
def test_rejects_malformed_spacing(spacing):
    value = calibration()
    value["frames"]["1"] = spacing
    with pytest.raises(ValueError):
        validate_document(value, frame_count=5)


@pytest.mark.parametrize("key", ["-1", "5", "01", "1.0", " 1", "+1", "", "x", 1, "9" * 100])
def test_rejects_unknown_or_noncanonical_frame_numbers(key):
    value = {**empty_document(), "frames": {key: None}}
    with pytest.raises(ValueError, match="frame number"):
        validate_document(value, frame_count=5)


@pytest.mark.parametrize("value", [True, 0, 2, "1", 1.0])
def test_rejects_unknown_version(value):
    with pytest.raises(ValueError, match="version"):
        validate_document({**empty_document(), "version": value}, frame_count=5)


@pytest.mark.parametrize("value", [None, [], {}, {**empty_document(), "extra": True}])
def test_rejects_malformed_document(value):
    with pytest.raises(ValueError):
        validate_document(value, frame_count=5)


def test_put_requires_revision_and_complete_replacement():
    value = calibration()
    with pytest.raises(ValueError, match="expected_revision"):
        validate_update(value, frame_count=5)
    update = {**value, "expected_revision": 2}
    assert validate_update(update, frame_count=5) == (value, 2)
    del update["frames"]
    with pytest.raises(ValueError):
        validate_update(update, frame_count=5)


@pytest.mark.parametrize("revision", [True, -1, 1.0, "0", None, 2**53])
def test_rejects_invalid_revisions(revision):
    with pytest.raises(ValueError, match="safe integer"):
        validate_update({**empty_document(), "expected_revision": revision}, frame_count=5)


def test_segment_selection_uses_original_start_and_step_but_retains_task_indices():
    value = calibration()
    original = deepcopy(value)
    result = restrict_to_segment(value, absolute_frames={103, 109}, start_frame=100, frame_step=3)
    assert result == value  # relative frames 1 and 3
    result = restrict_to_segment(value, absolute_frames={103}, start_frame=100, frame_step=3)
    assert result["frames"] == {"1": value["frames"]["1"]}
    assert result["default"] == value["default"]
    assert value == original


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"[]",
        b"null",
        b"{",
        b"\xff",
        b'{"frames":{"1":null,"1":null}}',
    ],
)
def test_strict_json_parser(raw):
    with pytest.raises(ValueError):
        parse_document_bytes(raw)


def test_parser_limits_raw_body_size_before_decoding():
    with pytest.raises(ValueError, match="1 MiB"):
        parse_document_bytes(b" " * (MAX_DOCUMENT_BYTES + 1))
    assert parse_document_bytes(json.dumps(calibration()).encode()) == calibration()


def test_document_size_limit_also_applies_to_non_http_callers():
    value = {**empty_document(), "frames": {str(index): None for index in range(100000)}}
    with pytest.raises(ValueError, match="1 MiB"):
        validate_document(value, frame_count=100000)
