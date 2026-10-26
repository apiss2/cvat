# SPDX-License-Identifier: MIT
"""Reference-compatible naming and pixel conversion, independent of Django."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import DicomImportError

BINARY_VRS = {"SQ", "OB", "OD", "OF", "OL", "OV", "OW", "UN"}
MAX_VALUE_LENGTH = 4096
MAX_DECODED_BYTES = 1024**3


def component(value: object, name: str) -> str:
    if value is None or not str(value).strip():
        raise DicomImportError(f"Missing required field: {name}")
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value).strip()).rstrip(" .")
    if not text:
        raise DicomImportError(f"Invalid field: {name}")
    return text


def checked_name(text: str, *, task: bool = False) -> str:
    # Do not silently truncate or hash names: this would change deduplication identity.
    if len(text.encode("utf-8")) > (240 if task else 255):
        raise DicomImportError("Generated name is too long; choose a shorter data-source prefix")
    return text


def names(ds, prefix: str, video: bool) -> tuple[str, str]:
    prefix = component(prefix, "data-source prefix")
    fields = {
        key: component(getattr(ds, key, None), key)
        for key in (
            "PatientID",
            "SeriesNumber",
            "StudyDate",
            "StudyTime",
            "InstanceNumber",
            "SOPInstanceUID",
            "StudyInstanceUID",
        )
    }
    stem = "_".join(
        (
            prefix + "_" + fields["PatientID"],
            fields["SeriesNumber"],
            fields["StudyDate"],
            fields["StudyTime"],
            fields["InstanceNumber"].zfill(4),
            fields["SOPInstanceUID"].replace(".", "")[-8:],
        )
    )
    study = "_".join((prefix, fields["PatientID"], fields["StudyDate"], fields["StudyInstanceUID"]))
    return checked_name(stem if video else study, task=True), checked_name(
        stem + (".mp4" if video else ".png")
    )


def _text(value) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise DicomImportError("Binary value in a textual element")
    if value is None:
        return ""
    if isinstance(value, Sequence) and not isinstance(value, str):
        return "\\".join(_text(item) for item in value)
    original = getattr(value, "original_string", None)
    return original if isinstance(original, str) else str(value)


def metadata(ds) -> dict[str, str]:
    result = {}
    for source in (getattr(ds, "file_meta", None), ds):
        if source is None:
            continue
        # keys/get_item avoids decoding sequence and large binary values simply to exclude them.
        for key in source.keys():
            raw = source.get_item(key, keep_deferred=True)
            vr = raw.VR
            if vr is None:
                from pydicom.datadict import dictionary_VR

                try:
                    vr = dictionary_VR(key)
                except KeyError:
                    continue
            if any(part in BINARY_VRS for part in vr.split(" or ")):
                continue
            element = source[key]
            if isinstance(element.value, (bytes, bytearray, memoryview)):
                continue
            text = _text(element.value)
            if len(text) > MAX_VALUE_LENGTH or "\x00" in text:
                raise DicomImportError(f"Unrepresentable text attribute {int(key):08X}")
            try:
                text.encode("utf-8")
            except UnicodeError as exc:
                raise DicomImportError(f"Invalid Unicode in attribute {int(key):08X}") from exc
            result[f"{int(key):08X}"] = text
    return result


def decode_pixels(ds) -> tuple[np.ndarray, bool]:
    if getattr(ds, "PhotometricInterpretation", None) == "PALETTE COLOR":
        # pixel_array contains palette indices. Encoding those directly would
        # silently turn a color image into an unrelated greyscale image.
        raise DicomImportError("PALETTE COLOR images require a color LUT and are unsupported")
    frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    rows, columns = int(ds.Rows), int(ds.Columns)
    samples = int(getattr(ds, "SamplesPerPixel", 1))
    bits = int(ds.BitsAllocated)
    if frames < 1 or rows < 1 or columns < 1 or samples not in (1, 3) or bits not in (1, 8, 16):
        raise DicomImportError("Unsupported pixel dimensions or bit depth")
    if frames * rows * columns * samples * max(1, bits // 8) > MAX_DECODED_BYTES:
        raise DicomImportError("Decoded DICOM exceeds the 1 GiB per-instance limit")
    stored = getattr(ds, "BitsStored", None)
    high = getattr(ds, "HighBit", None)
    mismatch = stored is not None and high is not None and int(high) != int(stored) - 1
    corrected = None
    if mismatch:
        stored = int(stored)
        if not 1 <= stored <= bits or int(getattr(ds, "PixelRepresentation", -1)) not in (0, 1):
            raise DicomImportError("Invalid pixel bit fields")
        corrected = copy.deepcopy(ds)
        corrected.HighBit = stored - 1
        # A dataset may already carry a cached array. Never compare the same cache twice.
        corrected._pixel_array = None
        corrected._pixel_id = {}
    pixels = ds.pixel_array
    expected = (
        ((frames,) if frames > 1 else ()) + (rows, columns) + ((samples,) if samples > 1 else ())
    )
    if pixels.shape != expected or pixels.dtype.kind not in "iu" or pixels.size == 0:
        raise DicomImportError("Unexpected decoded pixel array")
    if mismatch:
        other = corrected.pixel_array
        if (
            pixels.shape != other.shape
            or pixels.dtype != other.dtype
            or not np.array_equal(pixels, other)
        ):
            raise DicomImportError("HighBit correction changes decoded pixels")
        signed = int(ds.PixelRepresentation) == 1
        minimum = -(1 << (stored - 1)) if signed else 0
        maximum = (1 << (stored - int(signed))) - 1
        if int(pixels.min()) < minimum or int(pixels.max()) > maximum:
            raise DicomImportError("HighBit mismatch produced out-of-range pixels")
    return pixels, bool(mismatch)


def png_ready(image: np.ndarray) -> np.ndarray:
    if image.dtype.kind not in "iu" or image.size == 0:
        raise DicomImportError("PNG requires a nonempty integer array")
    low, high = int(image.min()), int(image.max())
    if image.dtype == np.uint8 or 0 <= low <= high <= 255:
        return image.astype(np.uint8, copy=False)
    if high - min(low, 0) > 65535:
        raise DicomImportError("Pixel range cannot be represented by a 16-bit PNG")
    if low < 0:
        return (image.astype(np.int64) - low).astype(np.uint16)
    return image.astype(np.uint16, copy=False)


def encode_png(image: np.ndarray) -> bytes:
    image = png_ready(image)
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise DicomImportError("PNG encoding failed")
    return buffer.tobytes()


def frame_rate(ds) -> float:
    def positive(value):
        try:
            value = float(value)
            return value if math.isfinite(value) and value > 0 else None
        except (ValueError, TypeError):
            return None

    interval = positive(getattr(ds, "FrameTime", None))
    if interval:
        rate = 1000.0 / interval
    else:
        intervals = [
            x for x in (positive(x) for x in getattr(ds, "FrameTimeVector", ()) or ()) if x
        ]
        rate = (
            1000.0 / float(np.median(intervals))
            if intervals
            else next(
                (
                    x
                    for x in (
                        positive(getattr(ds, key, None))
                        for key in ("CineRate", "RecommendedDisplayFrameRate")
                    )
                    if x
                ),
                30.0,
            )
        )
    if not math.isfinite(rate) or not 0 < rate <= 1000:
        raise DicomImportError("Unsupported frame rate")
    return rate


def write_mp4(path: Path, pixels: np.ndarray, ds) -> None:
    color = pixels.ndim == 4
    height, width = pixels.shape[1:3]
    # MPEG-4/yuv420 encoders otherwise silently crop odd dimensions.
    if width % 2 or height % 2:
        raise DicomImportError(
            "MP4 requires even image dimensions; no implicit resizing is performed"
        )
    low, high = int(pixels.min()), int(pixels.max())
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), frame_rate(ds), (width, height), color
    )
    try:
        if not writer.isOpened():
            raise DicomImportError("MP4 encoder is unavailable")
        for frame in pixels:
            if pixels.dtype == np.uint8 or 0 <= low <= high <= 255:
                output = frame.astype(np.uint8, copy=False)
            elif high <= low:
                output = np.zeros(frame.shape, dtype=np.uint8)
            else:
                output = np.clip(
                    np.rint((frame.astype(np.float32) - low) * (255.0 / (high - low))), 0, 255
                ).astype(np.uint8)
            if color:
                output = cv2.cvtColor(output, cv2.COLOR_RGB2BGR)
            writer.write(output)
    finally:
        writer.release()
    capture, count = cv2.VideoCapture(str(path)), 0
    try:
        if not capture.isOpened():
            raise DicomImportError("Encoded MP4 cannot be read")
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame.shape[:2] != (height, width):
                raise DicomImportError("MP4 dimensions changed")
            count += 1
    finally:
        capture.release()
    if count != len(pixels):
        raise DicomImportError("MP4 frame count changed")


@dataclass
class PreparedTask:
    name: str
    video: bool
    media: list[Path] = field(default_factory=list)
    tags: dict[str, dict[str, str]] = field(default_factory=dict)
    frames: int = 0


@dataclass
class Plan:
    tasks: list[PreparedTask] = field(default_factory=list)
    duplicates: int = 0
    skipped_tasks: set[str] = field(default_factory=set)
    high_bit_checked: int = 0
    ignored_files: int = 0


def prepare(
    files: list[Path],
    output: Path,
    prefix: str,
    existing_names=(),
    *,
    reader=None,
    progress: Callable[[int, int], None] | None = None,
) -> Plan:
    if reader is None:
        from pydicom import dcmread

        reader = lambda path: dcmread(path, force=True, defer_size="1 KB")
    output.mkdir(parents=True, exist_ok=False)
    plan, tasks, hashes, folders = Plan(), {}, {}, {}
    candidates = []
    existing = set(existing_names)
    # Read headers first and sort by the final name, independent of archive order.
    for path in sorted(files):
        with path.open("rb") as stream:
            header = stream.read(132)
        if path.suffix.lower() not in (".dcm", ".dicom") and header[128:132] != b"DICM":
            plan.ignored_files += 1
            continue
        ds = reader(path)
        if not hasattr(ds, "Rows") or not hasattr(ds, "Columns"):
            plan.ignored_files += 1
            continue
        frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
        task_name, filename = names(ds, prefix, frames > 1)
        if task_name in existing:
            plan.skipped_tasks.add(task_name)
            continue
        # Retaining Dataset objects here also retains their decoded pixel cache
        # later. Keep only paths so memory does not grow with every converted image.
        candidates.append((task_name, filename, frames, path))
    for index, (task_name, filename, frames, path) in enumerate(
        sorted(candidates, key=lambda x: x[:2])
    ):
        ds = reader(path)
        task = tasks.setdefault(task_name, PreparedTask(task_name, frames > 1))
        if task.video != (frames > 1) or task.video and task.media:
            raise DicomImportError("Image/video task name collision")
        # Group directories are numbered; output basenames retain the reference naming.
        folder = folders.setdefault(task_name, output / str(len(folders)))
        folder.mkdir(exist_ok=True)
        pixels, checked = decode_pixels(ds)
        plan.high_bit_checked += int(checked)
        tags = metadata(ds)
        target = folder / filename
        if frames == 1:
            encoded = encode_png(pixels)
            key = (task_name, hashlib.sha256(encoded).digest())
            bucket = hashes.setdefault(key, [])
            if any(previous.read_bytes() == encoded for previous in bucket):
                plan.duplicates += 1
                if progress:
                    progress(index + 1, len(candidates))
                continue
            if target.exists():
                raise DicomImportError("Different images produce the same output name")
            target.write_bytes(encoded)
            bucket.append(target)
        else:
            write_mp4(target, pixels, ds)
        task.media.append(target)
        task.tags[filename] = tags
        task.frames += frames
        if progress:
            progress(index + 1, len(candidates))
    plan.tasks = [task for task in tasks.values() if task.media]
    if not plan.tasks and not plan.skipped_tasks:
        raise DicomImportError("No supported DICOM images were found")
    return plan
