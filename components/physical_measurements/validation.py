# SPDX-License-Identifier: MIT
"""The versioned calibration contract, independent of Django and image formats."""

import json
import math
import re
from collections.abc import Collection
from copy import deepcopy

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_REVISION = 2**53 - 1


def empty_document() -> dict:
    return {"version": 1, "default": None, "frames": {}}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON number: {value}")


def parse_document_bytes(value: bytes) -> dict:
    if len(value) > MAX_DOCUMENT_BYTES:
        raise ValueError("Calibration JSON must not exceed 1 MiB")
    try:
        parsed = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("Calibration must be a UTF-8 JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Calibration must be a JSON object")
    return parsed


def _validate_spacing(value, field: str):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"x", "y", "unit"}:
        raise ValueError(f"{field} must be null or an object containing x, y and unit")
    if value["unit"] != "mm":
        raise ValueError(f"{field}.unit must be mm")
    for axis in ("x", "y"):
        number = value[axis]
        try:
            valid = type(number) in (int, float) and math.isfinite(number) and number > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{field}.{axis} must be a finite, positive number")
    return dict(value)


def validate_document(value, *, frame_count: int) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "default", "frames"}:
        raise ValueError("Calibration must contain exactly version, default and frames")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Calibration version must be 1")
    frames = value["frames"]
    if not isinstance(frames, dict):
        raise ValueError("frames must be an object keyed by task-relative frame number")
    result = {
        "version": 1,
        "default": _validate_spacing(value["default"], "default"),
        "frames": {},
    }
    for key, spacing in frames.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"0|[1-9][0-9]*", key)
            or len(key) > 16
            or int(key) >= frame_count
        ):
            raise ValueError(f"Invalid task-relative frame number: {key}")
        result["frames"][key] = _validate_spacing(spacing, f"frames.{key}")
    # Bound direct service callers and backups as well as HTTP requests.
    if (
        len(json.dumps(result, separators=(",", ":"), allow_nan=False).encode())
        > MAX_DOCUMENT_BYTES
    ):
        raise ValueError("Calibration JSON must not exceed 1 MiB")
    return result


def validate_update(value, *, frame_count: int) -> tuple[dict, int]:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "default",
        "frames",
        "expected_revision",
    }:
        raise ValueError("PUT requires version, default, frames and expected_revision")
    revision = value["expected_revision"]
    if type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise ValueError("expected_revision must be a nonnegative safe integer")
    document = {key: value[key] for key in ("version", "default", "frames")}
    return validate_document(document, frame_count=frame_count), revision


def restrict_to_segment(
    document: dict,
    *,
    absolute_frames: Collection[int],
    start_frame: int,
    frame_step: int,
) -> dict:
    """Return only overrides for frames belonging to this job, retaining task indices."""
    result = deepcopy(document)
    result["frames"] = {
        key: value
        for key, value in result["frames"].items()
        if start_frame + int(key) * frame_step in absolute_frames
    }
    return result
