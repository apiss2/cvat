# SPDX-License-Identifier: MIT
"""Pure preflight for DICOM-identified tag annotations, without database writes.

UIDs are used only as complete pairs. Repeated job annotations at the same task
frame are one image; different static frames or tasks are different images.
"""

from __future__ import annotations

import io
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import product

from cvat.apps.dataset_manager.formats.task_guide_core import (
    _UID,
    MAX_RECORDS,
    GuideImportError,
    _date,
    _identifier,
    _read_json,
    read_json_zip,
)

from . import PROTECTED_LABEL, ProjectAnnotationImportError

MAX_GENERATED_TAGS = 250_000
MAX_GENERATED_ATTRIBUTE_VALUES = 1_000_000
MAX_ATTRIBUTE_CHARS = 4096

# Keep the report importer's spelling and date rules; add DICOM Keyword aliases.
IDENTITY_ALIASES = {
    "StudyInstanceUID": ("study_instance_uid", "study_uid", "uid", "StudyInstanceUID"),
    "SOPInstanceUID": ("sop_instance_uid", "sop_uid", "SOPInstanceUID"),
    "PatientID": ("patient_id", "pid", "PatientID"),
    "StudyDate": ("study_date", "date", "dt", "StudyDate"),
    "SeriesInstanceUID": ("series_instance_uid", "series_uid", "SeriesInstanceUID"),
}
IDENTITY_NAMES = tuple(IDENTITY_ALIASES)
_RECORD_KEYS = {key for keys in IDENTITY_ALIASES.values() for key in keys} | {"tags"}


class DicomAnnotationError(ProjectAnnotationImportError):
    """Safe error messages never include input identifiers or attribute values."""


@dataclass(frozen=True)
class FrameIdentity:
    frame: int
    values: Mapping[str, Iterable[str]]


@dataclass(frozen=True)
class TaskSnapshot:
    id: int
    frames: tuple[int, ...]
    is_video: bool
    identities: tuple[FrameIdentity, ...]
    existing_tags: tuple[dict, ...] = ()


@dataclass
class DicomPlan:
    updates: dict[int, dict] = field(default_factory=dict)
    # Original 1-based row numbers allow a locked task to revalidate just its
    # records, while still checking all other current candidates for these SOPs.
    task_rows: dict[int, list[int]] = field(default_factory=dict)
    failed_tasks: dict[int, list[str]] = field(default_factory=dict)
    unmatched: list[int] = field(default_factory=list)
    # Invalid records with no identifiable task are reported separately. Errors
    # with identifiable tasks live in failed_tasks, avoiding double task counts.
    invalid_records: dict[int, str] = field(default_factory=dict)
    ambiguous: list[int] = field(default_factory=list)


def read_records(source) -> list:
    """Read one bounded UTF-8 JSON file from a safe ZIP, retaining row errors."""
    try:
        data = _read_json(io.BytesIO(read_json_zip(source)))
    except GuideImportError as exc:
        raise DicomAnnotationError("Invalid or oversized DICOM annotation ZIP or JSON") from exc
    if not isinstance(data, dict) or set(data) != {"records"}:
        raise DicomAnnotationError("Expected a JSON object containing a records array")
    records = data["records"]
    if not isinstance(records, list) or not 0 < len(records) <= MAX_RECORDS:
        raise DicomAnnotationError("Expected 1 to 50000 records")
    return records


def _safe_text(value: str) -> str:
    if "\x00" in value:
        raise DicomAnnotationError("NUL is not allowed")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise DicomAnnotationError("Invalid Unicode text") from exc
    return value


def _identity(record: dict) -> dict[str, str]:
    try:
        result = {
            name: _identifier(record, aliases, date=name == "StudyDate")
            for name, aliases in IDENTITY_ALIASES.items()
        }
    except GuideImportError as exc:
        raise DicomAnnotationError("Invalid or conflicting identity fields") from exc
    for name, value in result.items():
        _safe_text(value)
        if name in ("StudyInstanceUID", "SOPInstanceUID") or (
            name == "SeriesInstanceUID" and value
        ):
            if not _UID.fullmatch(value) or len(value) > 64:
                raise DicomAnnotationError(
                    "A complete numeric StudyInstanceUID and SOPInstanceUID are required"
                )
    return result


def _valid_uids(record: dict, name: str) -> set[str]:
    """Locate all possible exact targets even when identity aliases conflict."""
    values = (record.get(key) for key in IDENTITY_ALIASES[name])
    return {
        value.strip()
        for value in values
        if isinstance(value, str) and len(value.strip()) <= 64 and _UID.fullmatch(value.strip())
    }


def record_sop_uids(records: Iterable) -> set[str]:
    """Collect normalized, complete SOP aliases for current candidate lookup."""
    return {
        uid
        for record in records
        if isinstance(record, dict)
        for uid in _valid_uids(record, "SOPInstanceUID")
    }


def _stored_values(values: Iterable[str]) -> set[str]:
    # The loader supplies sets, but accepting one scalar avoids treating a string
    # as a collection of characters if a caller constructs a snapshot directly.
    if isinstance(values, str):
        values = (values,)
    return {value.strip() for value in values if isinstance(value, str) and value.strip()}


def _frames(task: TaskSnapshot) -> dict[int, dict[str, set[str]]]:
    result: dict[int, dict[str, set[str]]] = {}
    for identity in task.identities:
        frame = result.setdefault(identity.frame, {})
        for name, values in identity.values.items():
            frame.setdefault(name, set()).update(_stored_values(values))
    return result


def _attribute_value(value, spec: dict) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise DicomAnnotationError("Invalid attribute value")
        value = str(value)
    if not isinstance(value, str) or len(value) > MAX_ATTRIBUTE_CHARS:
        raise DicomAnnotationError("Attribute values must be scalar and at most 4096 characters")
    _safe_text(value)
    kind, choices = spec.get("input_type"), spec.get("values", [])
    if kind == "text":
        return value
    if kind == "checkbox" and value.lower() in ("true", "false"):
        return value.lower()
    if kind in ("select", "radio") and value in choices:
        return value
    if kind == "number":
        try:
            minimum, maximum, number = float(choices[0]), float(choices[1]), float(value)
            if (
                all(math.isfinite(n) for n in (minimum, maximum, number))
                and minimum <= number <= maximum
            ):
                return value
        except (ValueError, TypeError, IndexError, OverflowError):
            pass
    raise DicomAnnotationError("Attribute value does not match its project specification")


def _tags(record: dict, labels: Mapping[str, list[dict]]) -> list[dict]:
    if set(record) - _RECORD_KEYS:
        raise DicomAnnotationError("Unsupported record fields")
    tags = record.get("tags")
    if not isinstance(tags, list) or len(tags) > MAX_RECORDS:
        raise DicomAnnotationError("A tags array is required")
    output = []
    for tag in tags:
        if not isinstance(tag, dict) or set(tag) - {"label", "attributes"}:
            raise DicomAnnotationError("Only tag labels and attributes may be supplied")
        name = tag.get("label")
        if not isinstance(name, str) or not name:
            raise DicomAnnotationError("A tag label name is required")
        _safe_text(name)
        if name == PROTECTED_LABEL:
            raise DicomAnnotationError("DicomTag is reserved for matching and cannot be imported")
        candidates = labels.get(name, [])
        if len(candidates) != 1:
            raise DicomAnnotationError("Tag label is missing or ambiguous in this project")
        label = candidates[0]
        if label.get("type") not in ("tag", "any") or label.get("parent_id") is not None:
            raise DicomAnnotationError("The project label is not available for tag annotations")
        attributes = tag.get("attributes", {})
        if not isinstance(attributes, dict):
            raise DicomAnnotationError("Tag attributes must be an object")
        specs = defaultdict(list)
        for spec in label.get("attributes", []):
            specs[spec["name"]].append(spec)
        prepared = []
        for name, value in attributes.items():
            if len(specs.get(name, [])) != 1:
                raise DicomAnnotationError(
                    "Attribute is missing or ambiguous in this project label"
                )
            spec = specs[name][0]
            prepared.append({"spec_id": spec["id"], "value": _attribute_value(value, spec)})
        output.append(
            {"label_id": label["id"], "group": 0, "source": "file", "attributes": prepared}
        )
    return output


def _validate_target(
    task: TaskSnapshot,
    frames: dict[int, dict[str, set[str]]],
    selected_frame: int | None,
    identity: Mapping[str, str],
) -> tuple[int, ...]:
    selected = task.frames if task.is_video else (selected_frame,)
    available = set(task.frames)
    if not selected or len(set(selected)) != len(selected):
        raise DicomAnnotationError("Target media has invalid frame metadata")
    for frame in selected:
        if frame not in available or frame not in frames:
            raise DicomAnnotationError("Target media is missing DicomTag frame identities")
        saved = frames[frame]
        for name, expected in identity.items():
            values = saved.get(name, set())
            if name == "StudyDate":
                try:
                    values = {_date(value) for value in values}
                except GuideImportError as exc:
                    raise DicomAnnotationError("Target media has an invalid study date") from exc
            # Conflicting stored scalar identities are unsafe even if the
            # optional field was omitted from this record.
            if len(values) > 1:
                raise DicomAnnotationError("Target media has conflicting DicomTag identities")
            if expected and values != {expected}:
                raise DicomAnnotationError("Input identity differs from the stored DicomTag")
    if task.is_video:
        for name in IDENTITY_NAMES:
            values = set().union(*(frames[frame].get(name, set()) for frame in selected))
            if name == "StudyDate":
                values = {_date(value) for value in values}
            if len(values) > 1:
                raise DicomAnnotationError("Video frames have conflicting DicomTag identities")
    return selected


def plan_records(
    records: Sequence,
    tasks: Sequence[TaskSnapshot],
    labels: Sequence[dict],
    import_mode: str,
) -> DicomPlan:
    """Validate all records and return independent, all-or-nothing task updates.

    A bad record invalidates every identifiable candidate task, including tasks
    targeted by other valid records. Unmatched records never nominate a task.
    No database operation occurs here; callers must revalidate a locked snapshot
    before applying each returned task update inside its own transaction.
    """
    if import_mode not in ("replace", "append"):
        raise DicomAnnotationError("Unsupported annotation import mode")
    plan = DicomPlan()
    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise DicomAnnotationError("Duplicate task snapshots")
    saved = {task.id: _frames(task) for task in tasks}
    index = defaultdict(set)
    for task in tasks:
        for frame, values in saved[task.id].items():
            for study, sop in product(
                values.get("StudyInstanceUID", ()), values.get("SOPInstanceUID", ())
            ):
                index[(study, sop)].add((task.id, None if task.is_video else frame))
    by_label = defaultdict(list)
    for label in labels:
        by_label[label["name"]].append(label)
    protected = {label["id"] for label in labels if label["name"] == PROTECTED_LABEL}
    occupied: dict[int, set[tuple[int, int]]] = {}
    generated = attribute_values = 0
    totals: dict[int, tuple[int, int]] = {}
    validated_targets: dict[tuple, tuple[int, ...]] = {}

    def fail(row: int, candidates: set[tuple[int, int | None]], reason: str):
        nonlocal generated, attribute_values
        message = f"Record {row}: {reason}"
        if candidates:
            for task_id in {task_id for task_id, _ in candidates}:
                plan.failed_tasks.setdefault(task_id, []).append(message)
                plan.updates.pop(task_id, None)
                occupied.pop(task_id, None)
                removed_tags, removed_values = totals.pop(task_id, (0, 0))
                generated -= removed_tags
                attribute_values -= removed_values
        else:
            plan.invalid_records[row] = message

    for row, record in enumerate(records, 1):
        candidates = set()
        if isinstance(record, dict):
            for pair in product(
                _valid_uids(record, "StudyInstanceUID"), _valid_uids(record, "SOPInstanceUID")
            ):
                candidates.update(index.get(pair, ()))
        for task_id in {task_id for task_id, _ in candidates}:
            plan.task_rows.setdefault(task_id, []).append(row)
        try:
            if not isinstance(record, dict):
                raise DicomAnnotationError("Record must be an object")
            identity = _identity(record)
            if len(candidates) > 1:
                plan.ambiguous.append(row)
                raise DicomAnnotationError("DICOM image matches multiple tasks or different images")
            if not candidates:
                plan.unmatched.append(row)
            # A valid UID pair with no target is always reported as unmatched.
            # Still reject missing tags and invalid label/attribute definitions;
            # that row can also be counted as invalid without nominating a task.
            tags = _tags(record, by_label)
            if not candidates:
                continue
            task_id, selected_frame = next(iter(candidates))
            # A failed task can never produce an update. Keep checking identity
            # ambiguity above so later rows can still invalidate other tasks,
            # but do not expand video tags or retain duplicate keys for it.
            if task_id in plan.failed_tasks:
                continue
            task = by_id[task_id]
            target_key = (task_id, selected_frame, tuple(identity.items()))
            if target_key not in validated_targets:
                validated_targets[target_key] = _validate_target(
                    task, saved[task_id], selected_frame, identity
                )
            frames = validated_targets[target_key]
            if task_id not in occupied:
                occupied[task_id] = set()
                for tag in task.existing_tags:
                    if import_mode == "replace" and tag["label_id"] not in protected:
                        continue
                    key = (tag["frame"], tag["label_id"])
                    if key in occupied[task_id]:
                        raise DicomAnnotationError(
                            "Retained tags contain a duplicate frame and label"
                        )
                    occupied[task_id].add(key)
            new_tags = len(frames) * len(tags)
            new_values = len(frames) * sum(len(tag["attributes"]) for tag in tags)
            if (
                generated + new_tags > MAX_GENERATED_TAGS
                or attribute_values + new_values > MAX_GENERATED_ATTRIBUTE_VALUES
            ):
                raise DicomAnnotationError(
                    "Expanded DICOM annotations exceed the import size limit"
                )
            update = {"tags": [], "shapes": [], "tracks": []}
            for frame, tag in product(frames, tags):
                key = (frame, tag["label_id"])
                if key in occupied[task_id]:
                    raise DicomAnnotationError("Duplicate tag label on the same frame")
                occupied[task_id].add(key)
                update["tags"].append({**tag, "frame": frame})
            if task_id not in plan.failed_tasks:
                plan.updates.setdefault(task_id, {"tags": [], "shapes": [], "tracks": []})[
                    "tags"
                ].extend(update["tags"])
                generated += new_tags
                attribute_values += new_values
                prior_tags, prior_values = totals.get(task_id, (0, 0))
                totals[task_id] = (prior_tags + new_tags, prior_values + new_values)
        except (DicomAnnotationError, GuideImportError) as exc:
            # Shared date parsing also uses safe, field-only errors.
            fail(row, candidates, str(exc))
    return plan
