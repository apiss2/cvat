# SPDX-License-Identifier: MIT
"""Small project-only adapter around the native TaskData and annotation writers."""

from copy import deepcopy

from cvat.apps.dataset_manager.annotation import AnnotationIR
from cvat.apps.dataset_manager.bindings import TaskData
from cvat.apps.dataset_manager.task import JobAnnotation, PatchAction
from cvat.apps.engine.serializers import LabeledDataSerializer, LabelSerializer

from . import ANNOTATION_KEYS, PROTECTED_LABEL, ProjectAnnotationImportError

MAX_ANNOTATIONS = 500_000


def _without_protected_elements(annotation):
    if hasattr(annotation, "elements"):
        return annotation._replace(
            elements=[
                _without_protected_elements(element)
                for element in annotation.elements
                if element.label != PROTECTED_LABEL
            ]
        )
    return annotation


class ProjectTaskData(TaskData):
    """Drop protected input by name before native label/attribute resolution."""

    def add_tag(self, tag):
        if tag.label != PROTECTED_LABEL:
            super().add_tag(tag)

    def add_shape(self, shape):
        if shape.label != PROTECTED_LABEL:
            super().add_shape(_without_protected_elements(shape))

    def add_track(self, track):
        if track.label != PROTECTED_LABEL:
            super().add_track(_without_protected_elements(track))

    def add_interval(self, interval):
        if interval.label != PROTECTED_LABEL:
            super().add_interval(interval)


def stage_native(source, importer, task, temp_dir, *, conv_mask_to_poly):
    """Use the native format reader and frame matching, with a non-writing callback."""
    staged = {key: [] for key in ANNOTATION_KEYS}
    count = 0

    def collect(data):
        nonlocal count

        def size(item):
            return 1 + sum(
                size(child) for key in ("elements", "shapes") for child in item.get(key, [])
            )

        count += sum(size(item) for key in ANNOTATION_KEYS for item in data.get(key, []))
        if count > MAX_ANNOTATIONS:
            raise ProjectAnnotationImportError("Task exceeds the annotation count limit")
        for key in ANNOTATION_KEYS:
            staged[key].extend(data.get(key, []))

    data = ProjectTaskData(
        annotation_ir=AnnotationIR(task.dimension), db_task=task, create_callback=collect
    )
    with source.open("rb") as stream:
        importer(stream, str(temp_dir), data, conv_mask_to_poly=conv_mask_to_poly)
    collect(data.data.serialize())
    return staged


def validate_payload(annotation, payload, labels):
    """Validate output before any delete, preserving exact text attribute values."""
    serializer = LabeledDataSerializer(
        data=deepcopy(payload), context={"annotation_action": PatchAction.CREATE}
    )
    serializer.is_valid(raise_exception=True)
    result = serializer.validated_data
    by_id = {}

    def add_labels(items):
        for label in items:
            by_id[label["id"]] = label
            add_labels(label.get("sublabels", []))

    add_labels(labels)

    def validate(original, output, parent_label=None):
        label_id = output.get("label_id", parent_label)
        if label_id not in by_id or by_id[label_id]["name"] == PROTECTED_LABEL:
            raise ProjectAnnotationImportError("Invalid annotation label")
        frame = output.get("frame")
        if (
            frame is not None
            and frame >= annotation.db_task.data.size
            and not output.get("outside")
        ):
            raise ProjectAnnotationImportError("Annotation frame is outside the task")
        specs = {spec["id"]: spec for spec in by_id[label_id].get("attributes", [])}
        for old, new in zip(original.get("attributes", []), output.get("attributes", [])):
            spec = specs.get(new["spec_id"])
            if spec is None:
                raise ProjectAnnotationImportError("Invalid annotation attribute")
            value = str(old["value"])
            LabelSerializer._validate_attribute_value(
                spec["input_type"], value, "\n".join(spec["values"])
            )
            new["value"] = value
        for key in ("elements", "shapes"):
            for old, new in zip(original.get(key, []), output.get(key, [])):
                validate(old, new, label_id)

    for key in ANNOTATION_KEYS:
        for old, new in zip(payload.get(key, []), result[key]):
            validate(old, new)
    annotation._validate_input_annotations(result)
    return result


def apply_task(annotation, payload, labels, import_mode):
    """Preserve original DicomTag rows and attribute rows using native partial delete."""
    protected = {label["id"] for label in labels if label["name"] == PROTECTED_LABEL}
    if import_mode == "replace":
        # Work with actual job IDs, not task-merged overlap annotations. A merged
        # task view can hide replicas, leaving stale rows behind after a delete.
        for db_job in annotation.db_jobs:
            job = JobAnnotation(db_job.id, db_job=db_job)
            job.init_from_db()
            removal = {
                key: [item for item in job.data[key] if item["label_id"] not in protected]
                for key in ANNOTATION_KEYS
            }
            if any(removal.values()):
                job.delete(removal)
    elif import_mode != "append":
        raise ProjectAnnotationImportError("Unsupported annotation import mode")
    # All records for a task are written together, inside the caller's transaction.
    if any(payload.get(key) for key in ANNOTATION_KEYS):
        annotation.create(payload)
