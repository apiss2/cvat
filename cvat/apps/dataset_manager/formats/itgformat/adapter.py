"""CVAT integration through its existing format registry and Datumaro bridge."""

from __future__ import annotations

import math
import zipfile
from pathlib import Path, PurePosixPath

import datumaro as dm
import numpy as np
from pycocotools import mask as mask_utils

from cvat.apps.dataset_manager.bindings import (
    CvatExportError,
    CvatImportError,
    GetCVATDataExtractor,
    import_dm_annotations,
)
from cvat.apps.dataset_manager.formats.registry import dm_env, exporter, importer
from cvat.apps.dataset_manager.formats.transformations import EllipsesToMasks

from .archive import (
    IMAGE_EXTS,
    MANIFEST,
    Record,
    extract_zip,
    make_zip,
    read_records,
    resolve_frame,
    safe_relative,
    write_manifest,
)
from .codec import (
    Box,
    FormatError,
    checked_name,
    checked_size,
    choose_byte_count,
    format_boxes,
    pack_segments,
    read_mask,
    write_mask,
)

FORMAT_NAME = "ITGformat"
SEGMENT_TYPES = {
    dm.AnnotationType.mask,
    dm.AnnotationType.polygon,
    dm.AnnotationType.ellipse,
}
SUPPORTED_TYPES = SEGMENT_TYPES | {dm.AnnotationType.bbox}


def freeze_items(extractor) -> list[dm.DatasetItem]:
    # CVAT can stream annotations. Evaluate each frame's annotation getter while
    # iterating, instead of walking the original stream twice.
    return [item.wrap(annotations=list(item.annotations)) for item in extractor]


def _visible(ann) -> bool:
    return ann.attributes.get("outside", False) not in (True, "true", "True", 1)


def _name(ann, labels: list[str]) -> str:
    if ann.label is None or not 0 <= ann.label < len(labels):
        raise FormatError("Annotation has no valid class")
    return labels[ann.label]


def rasterize(ann, width: int, height: int) -> np.ndarray:
    if ann.type == dm.AnnotationType.mask:
        return np.asarray(ann.image, dtype=bool)
    if ann.type == dm.AnnotationType.polygon:
        points = list(ann.points)
        if (
            len(points) < 6
            or len(points) % 2
            or not all(math.isfinite(x) for x in points)
        ):
            raise FormatError("Invalid polygon coordinates")
        rles = mask_utils.frPyObjects([points], height, width)
        return mask_utils.decode(mask_utils.merge(rles)).astype(bool)
    if ann.type == dm.AnnotationType.ellipse:
        return np.asarray(
            EllipsesToMasks.convert_ellipse(ann, height, width).image, dtype=bool
        )
    raise FormatError(f"Not a segmentation shape: {ann.type}")


def export_items(
    items: list[dm.DatasetItem],
    labels: list[str],
    root: Path,
    *,
    save_images: bool = False,
) -> None:
    """Export a complete scope: absent annotations still get required empty files."""
    if not items:
        raise FormatError("The export scope contains no images")
    labels = [checked_name(name) for name in labels]
    if len(set(labels)) != len(labels):
        raise FormatError("This format requires unique, non-hierarchical class names")
    used_segments: set[str] = set()
    has_boxes = False
    identities = set()
    for item in items:
        safe_relative(item.id)
        safe_relative(item.subset or "default")
        identity = (item.subset, item.id)
        if identity in identities:
            raise FormatError(f"Duplicate item id within a subset: {identity}")
        identities.add(identity)
        if not isinstance(item.media, dm.Image) or item.media.size is None:
            raise FormatError(f"Image dimensions unavailable: {item.id}")
        height, width = item.media.size
        checked_size(width, height)
        for ann in item.annotations:
            if not _visible(ann):
                continue
            if ann.type not in SUPPORTED_TYPES:
                raise FormatError(
                    f"{item.id}: unsupported annotation {ann.type.name}; export cancelled"
                )
            label = _name(ann, labels)
            if ann.type in SEGMENT_TYPES:
                used_segments.add(label)
            else:
                rotation = float(ann.attributes.get("rotation", 0) or 0)
                if not math.isfinite(rotation) or rotation % 360 != 0:
                    raise FormatError(
                        f"{item.id}: rotated rectangles cannot be represented by .bb"
                    )
                checked_name(label, bbox=True)
                has_boxes = True
    # Bounding-box-only classes do not consume mask bits. Pick one width for
    # the complete scope so empty images carry the same HDR contract.
    byte_count = choose_byte_count(len(used_segments)) if used_segments else None
    # Export bit numbers are canonical for this scope, not CVAT database label IDs.
    bit_names = dict(enumerate(sorted(used_segments)))
    multiple_subsets = len({item.subset or "default" for item in items}) > 1
    stems = set()
    plan = []
    for item in items:
        subset = item.subset or "default"
        stem = safe_relative(f"{subset}/{item.id}" if multiple_subsets else item.id)
        ext = item.media.ext or ".png"
        ext = ext if ext.startswith(".") else "." + ext
        if ext.lower() not in IMAGE_EXTS:
            raise FormatError(f"Unsupported image extension for export: {ext}")
        if stem in stems:
            raise FormatError(f"File stem collision: {stem}")
        stems.add(stem)
        plan.append((item, stem, stem + ext, subset))
    # Also reject file/directory collisions (e.g. a.png and a.png/b.png).
    paths = [MANIFEST]
    for _, stem, image_name, _ in plan:
        paths.append(image_name)  # reserved even for annotation-only exports
        if bit_names:
            paths.extend([stem + ".mask.hdr", stem + ".mask.re4"])
        if has_boxes:
            paths.append(stem + ".bb")
    path_set = set(paths)
    if len(path_set) != len(paths) or any(
        str(p) in path_set
        for n in paths
        for p in PurePosixPath(n).parents
        if str(p) != "."
    ):
        raise FormatError("Output contains conflicting file and directory names")
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for item, stem, image_name, subset in plan:
        height, width = item.media.size
        annotations = [ann for ann in item.annotations if _visible(ann)]
        if bit_names:
            segments = (
                (_name(ann, labels), rasterize(ann, width, height))
                for ann in annotations
                if ann.type in SEGMENT_TYPES
            )
            packed = pack_segments(
                segments, width, height, bit_names, byte_count=byte_count
            )
            write_mask(root / (stem + ".mask.hdr"), packed, bit_names)
        if has_boxes:
            boxes = [
                Box(*ann.points, _name(ann, labels))
                for ann in annotations
                if ann.type == dm.AnnotationType.bbox
            ]
            path = root / (stem + ".bb")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(format_boxes(boxes, width, height), encoding="utf-8")
        if save_images:
            destination = root / image_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            item.media.save(str(destination))
        entries.append(
            {
                "id": item.id,
                "subset": subset,
                "image": image_name,
                "width": int(width),
                "height": int(height),
            }
        )
    write_manifest(root, entries, labels)


def build_dataset(
    records: list[Record], labels: list[str], *, frame_info=None
) -> dm.Dataset:
    label_ids = {name: index for index, name in enumerate(labels)}
    items = []
    used_frames = set()
    for record in records:
        item_id = record.item_id
        image_name = record.image_name
        if frame_info is not None:
            number, info = resolve_frame(record, frame_info)
            if number in used_frames:
                raise FormatError(
                    f"Several records map to the same CVAT frame: {record.item_id}"
                )
            used_frames.add(number)
            image_name = str(info["path"]).replace("\\", "/")
            item_id = str(PurePosixPath(image_name).with_suffix(""))
        if record.width is None or record.height is None:
            raise FormatError(f"{record.stem}: image dimensions cannot be determined")
        if image_name is None:
            image_name = (
                item_id + ".png"
            )  # metadata only; never pretends to contain image bytes
        annotations = []
        if record.hdr_path is not None:
            header, packed = read_mask(record.hdr_path)
            for bit, label in header.bit_names.items():
                binary = ((packed & header.dtype.type(1 << bit)) != 0).astype(np.uint8)
                if np.any(binary):
                    annotations.append(
                        dm.RleMask(
                            rle=mask_utils.encode(np.asfortranarray(binary)),
                            label=label_ids[label],
                        )
                    )
        for box in record.boxes:
            annotations.append(
                dm.Bbox(
                    box.x1,
                    box.y1,
                    box.x2 - box.x1,
                    box.y2 - box.y1,
                    label=label_ids[box.label],
                )
            )
        media_path = str(record.image_path) if record.image_path else image_name
        media = dm.Image.from_file(
            path=media_path,
            size=(record.height, record.width),
            ext=PurePosixPath(image_name).suffix,
        )
        items.append(
            dm.DatasetItem(
                id=item_id, subset=record.subset, media=media, annotations=annotations
            )
        )
    return dm.Dataset.from_iterable(items, categories=labels, env=dm_env)


@exporter(name=FORMAT_NAME, ext="ZIP", version="1.0")
def _export(dst_file, temp_dir, instance_data, save_images=False):
    try:
        with GetCVATDataExtractor(
            instance_data, include_images=save_images
        ) as extractor:
            labels = [
                entry.name
                for entry in extractor.categories()[dm.AnnotationType.label].items
            ]
            items = freeze_items(extractor)
            root = Path(temp_dir) / "itgformat_output"
            export_items(items, labels, root, save_images=save_images)
            make_zip(root, dst_file)
    except (FormatError, OSError, ValueError) as exc:
        raise CvatExportError(f"ITGformat: {exc}") from exc


@importer(name=FORMAT_NAME, ext="ZIP", version="1.0")
def _import(src_file, temp_dir, instance_data, load_data_callback=None, **kwargs):
    try:
        # Unlike some existing importers, intentionally do not honor
        # conv_mask_to_poly: native masks preserve holes and overlapping classes.
        root = extract_zip(src_file, Path(temp_dir) / "itgformat_input")
        records, labels = read_records(root)
        if load_data_callback is not None:
            if any(record.image_path is None for record in records):
                raise FormatError(
                    "Project dataset import requires an image file for every record"
                )
            dataset = build_dataset(records, labels)
        else:
            meta = instance_data.meta[instance_data.META_FIELD]
            known = {
                label["name"] for _, label in meta["labels"] if not label.get("parent")
            }
            unknown = set(labels) - known
            if unknown:
                raise FormatError(
                    f"Classes are not registered in the task: {sorted(unknown)}"
                )
            dataset = build_dataset(
                records, labels, frame_info=instance_data.frame_info
            )
        if load_data_callback is not None:
            load_data_callback(dataset, instance_data)
        import_dm_annotations(dataset, instance_data)
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as exc:
        raise CvatImportError(f"ITGformat: {exc}") from exc
