# SPDX-License-Identifier: MIT
"""Small, testable contracts for native CVAT labels and frame metadata."""

from . import DicomImportError

LABEL_NAME = "DicomTag"


def missing_attributes(existing, names):
    if existing is not None and existing.get("type") not in ("tag", "any"):
        raise DicomImportError("Existing DicomTag label is not a tag label")
    current = {attribute["name"]: attribute for attribute in (existing or {}).get("attributes", [])}
    # Every existing attribute is emitted, including empty values for elements
    # absent from this input. Non-text fields cannot safely represent that absence.
    for name in current:
        if current[name]["input_type"] != "text":
            raise DicomImportError(f"Existing DicomTag attribute {name} is not text")
    return [
        {"name": name, "input_type": "text", "mutable": False, "default_value": "", "values": []}
        for name in sorted(set(names) - current.keys())
    ]


def image_frames(tags, images):
    seen = set()
    for path, frame in images:
        # initialize_task may prepend a source directory. Each task has unique basenames.
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        if name not in tags or name in seen:
            raise DicomImportError("CVAT image order does not match the converted files")
        seen.add(name)
        yield int(frame), tags[name]
    if seen != set(tags):
        raise DicomImportError("CVAT did not register all converted images")


def tag_batches(frames, label_id, attribute_ids, limit=100):
    tags = []
    count = 0
    for frame, values in frames:
        # CVAT fills omitted attributes from the label's defaults on read/export.
        # A missing DICOM element must stay empty, even when an existing project
        # label has a nonempty default for PixelSpacing or a patient identifier.
        tag = {
            "frame": frame,
            "label_id": label_id,
            "group": 0,
            "source": "auto",
            "attributes": [
                {"spec_id": spec_id, "value": values.get(key, "")}
                for key, spec_id in attribute_ids.items()
            ],
        }
        if tags and (len(tags) >= limit or count + len(attribute_ids) > 10000):
            yield {"tags": tags, "shapes": [], "tracks": []}
            tags, count = [], 0
        tags.append(tag)
        count += len(attribute_ids)
    if tags:
        yield {"tags": tags, "shapes": [], "tracks": []}
