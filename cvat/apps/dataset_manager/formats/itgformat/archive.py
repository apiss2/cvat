"""ZIP layout, optional image inventory, validation, and deterministic frame matching."""

from __future__ import annotations

import json
import shutil
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from PIL import Image

from .codec import (
    Box,
    FormatError,
    Header,
    checked_name,
    checked_size,
    parse_boxes,
    read_mask,
)

MANIFEST = "itgformat.json"
IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".ppm",
    ".pgm",
    ".pbm",
}
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_ARCHIVE_FILES = 100_000
MAX_BB_BYTES = 64 * 1024**2


def safe_relative(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise FormatError(f"Unsafe relative path: {value!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FormatError(f"Control character in path: {value!r}")
    parts = value.split("/")
    if any(p in {"", ".", ".."} for p in parts) or value.startswith("/"):
        raise FormatError(f"Unsafe relative path: {value!r}")
    return value


def extract_zip(source, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if (
            len(infos) > MAX_ARCHIVE_FILES
            or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES
        ):
            raise FormatError(
                "ZIP exceeds the configured file count or uncompressed size limit"
            )
        seen = set()
        for info in infos:
            name = safe_relative(
                info.filename.rstrip("/") if info.is_dir() else info.filename
            )
            if name in seen:
                raise FormatError(f"Duplicate ZIP entry: {name}")
            seen.add(name)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in {
                0,
                stat.S_IFREG,
                stat.S_IFDIR,
            }:
                raise FormatError(f"ZIP contains a link/special file: {name}")
            if info.flag_bits & 1:
                raise FormatError("Encrypted ZIP archives are not supported")
        # Extraction occurs only after all entry names/types have been checked.
        for info in infos:
            target = destination / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    root = destination
    while not (root / MANIFEST).exists():
        entries = [p for p in root.iterdir() if p.name not in {"__MACOSX", ".DS_Store"}]
        if len(entries) != 1 or not entries[0].is_dir():
            break
        root = entries[0]
    return root


def make_zip(source: Path, destination) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


@dataclass
class Record:
    stem: str
    item_id: str
    subset: str = "default"
    image_name: str | None = None
    image_path: Path | None = None
    hdr_path: Path | None = None
    bb_path: Path | None = None
    width: int | None = None
    height: int | None = None
    header: Header | None = None
    boxes: list[Box] = field(default_factory=list)

    def set_size(self, width: int, height: int, source: str) -> None:
        checked_size(width, height)
        if self.width is not None and (self.width, self.height) != (width, height):
            raise FormatError(f"{self.stem}: size mismatch at {source}")
        self.width, self.height = width, height


def _classify(relative: str) -> tuple[str, str] | None:
    for suffix, kind in ((".mask.hdr", "hdr"), (".mask.re4", "re4"), (".bb", "bb")):
        if relative.endswith(suffix):
            return relative[: -len(suffix)], kind
    if PurePosixPath(relative).suffix.lower() in IMAGE_EXTS:
        return str(PurePosixPath(relative).with_suffix("")), "image"
    return None


def read_records(root: Path) -> tuple[list[Record], list[str]]:
    manifest_path = root / MANIFEST
    records: dict[str, Record] = {}
    declared_labels: list[str] = []
    has_manifest = manifest_path.is_file()
    if has_manifest:
        if manifest_path.stat().st_size > 32 * 1024**2:
            raise FormatError("Oversized image inventory")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("format") != "ITGformat"
            or data.get("version") != 1
        ):
            raise FormatError("Unsupported itgformat.json format/version")
        entries = data.get("items")
        if (
            not isinstance(entries, list)
            or not entries
            or len(entries) > MAX_ARCHIVE_FILES
        ):
            raise FormatError("Manifest must contain a nonempty items list")
        identities = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise FormatError("Manifest item must be an object")
            image_name = safe_relative(entry["image"])
            if PurePosixPath(image_name).suffix.lower() not in IMAGE_EXTS:
                raise FormatError(f"Unsupported image extension: {image_name}")
            stem = str(PurePosixPath(image_name).with_suffix(""))
            item_id = safe_relative(entry["id"])
            subset = entry.get("subset", "default") or "default"
            safe_relative(subset)
            identity = (subset, item_id)
            if stem in records or identity in identities:
                raise FormatError("Duplicate manifest filename stem or item identity")
            identities.add(identity)
            record = Record(stem, item_id, subset, image_name=image_name)
            record.set_size(entry["width"], entry["height"], MANIFEST)
            records[stem] = record
        labels = data.get("labels", [])
        if not isinstance(labels, list):
            raise FormatError("Manifest labels must be a list")
        declared_labels = [checked_name(label) for label in labels]
        if len(set(declared_labels)) != len(declared_labels):
            raise FormatError("Duplicate manifest class name")

    found: dict[str, dict[str, Path]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if "__MACOSX" in path.relative_to(root).parts or path.name.startswith("._"):
            continue
        kind_info = _classify(relative)
        if kind_info is None:
            continue
        stem, kind = kind_info
        safe_relative(stem)
        if kind in found.setdefault(stem, {}):
            raise FormatError(
                f"Multiple images/files share one annotation stem: {stem}"
            )
        found[stem][kind] = path
        if stem not in records:
            if has_manifest:
                raise FormatError(f"File not listed in {MANIFEST}: {relative}")
            records[stem] = Record(stem, stem)

    if not records:
        raise FormatError("No supported image or annotation files in ZIP")
    names = set(declared_labels)
    for stem, record in records.items():
        paths = found.get(stem, {})
        if ("hdr" in paths) != ("re4" in paths):
            raise FormatError(
                f"{stem}: .mask.hdr and .mask.re4 must both exist or both be absent"
            )
        if "image" in paths:
            record.image_path = paths["image"]
            image_name = record.image_path.relative_to(root).as_posix()
            if record.image_name is not None and image_name != record.image_name:
                raise FormatError(f"Image name differs from manifest: {image_name}")
            record.image_name = image_name
            with Image.open(record.image_path) as image:
                record.set_size(*image.size, source="image")
                image.verify()
        if "hdr" in paths:
            record.hdr_path = paths["hdr"]
            # Validate run lengths and undefined bits before any database callback.
            record.header, packed = read_mask(record.hdr_path)
            del packed
            record.set_size(record.header.width, record.header.height, "HDR")
            names.update(record.header.bit_names.values())
        if "bb" in paths:
            record.bb_path = paths["bb"]
            if record.bb_path.stat().st_size > MAX_BB_BYTES:
                raise FormatError(f"Oversized BB: {record.bb_path.name}")
            record.boxes = parse_boxes(
                record.bb_path.read_text(encoding="utf-8-sig"),
                record.width,
                record.height,
            )
            names.update(box.label for box in record.boxes)
    return sorted(records.values(), key=lambda r: (r.subset, r.item_id)), sorted(names)


def resolve_frame(record: Record, frame_info: Mapping) -> tuple[object, dict]:
    """Exact relative path first; allow only a unique missing directory prefix.

    Never fall back to a numeric frame index or silently choose one basename.
    """
    candidates = []
    for number, info in frame_info.items():
        path = str(info["path"]).replace("\\", "/")
        stem = str(PurePosixPath(path).with_suffix(""))
        candidates.append((number, info, path, stem))
    matches = [entry for entry in candidates if entry[3] == record.item_id]
    if not matches:
        matches = [
            entry for entry in candidates if entry[3].endswith("/" + record.item_id)
        ]
    if record.image_name:
        ext = PurePosixPath(record.image_name).suffix.lower()
        matches = [
            entry for entry in matches if PurePosixPath(entry[2]).suffix.lower() == ext
        ]
    if len(matches) != 1:
        raise FormatError(
            f"{record.item_id}: expected one matching task image, found {len(matches)}"
        )
    number, info, _, _ = matches[0]
    record.set_size(info["width"], info["height"], "CVAT task image")
    # Dimensions may only now be known for a BB-only, image-free import.
    for box in record.boxes:
        from .codec import check_box

        check_box(box, record.width, record.height)
    return number, info


def write_manifest(root: Path, entries: list[dict], labels: list[str]) -> None:
    (root / MANIFEST).write_text(
        json.dumps(
            {"format": "ITGformat", "version": 1, "labels": labels, "items": entries},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
