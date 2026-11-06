# SPDX-License-Identifier: MIT
"""Reference-compatible naming and pixel conversion, independent of Django."""

from __future__ import annotations

import copy
import hashlib
import math
import re
import shutil
from collections.abc import Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import DicomImportError
from .settings import limits
from .source import FrameReader, open_dicom
from .tag_names import attribute_name

BINARY_VRS = {"SQ", "OB", "OD", "OF", "OL", "OV", "OW", "UN"}
MAX_VALUE_LENGTH = 4096


def _read_dicom(path: Path, *, headers: bool = False):
    """Read ordinary file-backed metadata; Deflated sources use open_dicom."""
    from pydicom import dcmread
    from pydicom.filereader import _read_file_meta_info, read_preamble
    from pydicom.uid import DeflatedExplicitVRLittleEndian

    with path.open("rb") as stream:
        read_preamble(stream, force=True)
        if _read_file_meta_info(stream).get("TransferSyntaxUID") == DeflatedExplicitVRLittleEndian:
            raise DicomImportError("Deflated DICOM requires the file-backed source reader")
    return dcmread(path, force=True, defer_size="1 KB", stop_before_pixels=headers)


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
    has_prefix = str(getattr(ds, "PatientID", "")).strip().startswith(str(prefix).strip() + "_")
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
    patient_id = fields["PatientID"] if has_prefix else prefix + "_" + fields["PatientID"]
    stem = "_".join(
        (
            patient_id,
            fields["SeriesNumber"],
            fields["StudyDate"],
            fields["StudyTime"],
            fields["InstanceNumber"].zfill(4),
            fields["SOPInstanceUID"].replace(".", "")[-8:],
        )
    )
    study = "_".join(
        (
            patient_id,
            fields["StudyDate"],
            fields["StudyInstanceUID"].replace(".", "")[-8:],
        )
    )
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
            # A deferred textual value can be arbitrarily large even when the
            # image dimensions are small. Reject it before pydicom reads it.
            # Four bytes per Unicode character plus padding covers UTF-8 text;
            # the usual decoded-character check below remains authoritative.
            if getattr(raw, "length", 0) > 4 * MAX_VALUE_LENGTH + 16:
                raise DicomImportError(f"Unrepresentable text attribute {int(key):08X}")
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
            result[attribute_name(int(key))] = text
    return result


def _pixel_parameters(ds) -> tuple[int, tuple[int, ...], bool]:
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
    if rows * columns * samples * max(1, bits // 8) > limits()["max_frame_bytes"]:
        raise DicomImportError("Decoded DICOM frame exceeds the CVAT_DICOM_MAX_FRAME_BYTES limit")
    stored = getattr(ds, "BitsStored", None)
    high = getattr(ds, "HighBit", None)
    mismatch = stored is not None and high is not None and int(high) != int(stored) - 1
    if mismatch:
        stored = int(stored)
        if not 1 <= stored <= bits or int(getattr(ds, "PixelRepresentation", -1)) not in (0, 1):
            raise DicomImportError("Invalid pixel bit fields")
    shape = (rows, columns) + ((samples,) if samples > 1 else ())
    return frames, shape, bool(mismatch)


def _check_frame(pixels: np.ndarray, shape: tuple[int, ...], ds, mismatch: bool) -> None:
    if pixels.shape != shape or pixels.dtype.kind not in "iu" or pixels.size == 0:
        raise DicomImportError("Unexpected decoded pixel array")
    if pixels.nbytes > limits()["max_frame_bytes"]:
        raise DicomImportError("Decoded DICOM frame exceeds the CVAT_DICOM_MAX_FRAME_BYTES limit")
    if mismatch:
        stored = int(ds.BitsStored)
        signed = int(ds.PixelRepresentation) == 1
        minimum = -(1 << (stored - 1)) if signed else 0
        maximum = (1 << (stored - int(signed))) - 1
        if int(pixels.min()) < minimum or int(pixels.max()) > maximum:
            raise DicomImportError("HighBit mismatch produced out-of-range pixels")


def decode_pixels(ds) -> tuple[np.ndarray, bool]:
    """Decode an in-memory dataset supplied by an explicit reader.

    File imports use iter_dicom_frames instead so neither encoded pixel data
    nor a complete movie array is materialized by Dataset.pixel_array.
    """
    frames, shape, mismatch = _pixel_parameters(ds)
    corrected = None
    if mismatch:
        corrected = copy.deepcopy(ds)
        stored = int(ds.BitsStored)
        corrected.HighBit = stored - 1
        # A dataset may already carry a cached array. Never compare the same cache twice.
        corrected._pixel_array = None
        corrected._pixel_id = {}
    pixels = ds.pixel_array
    expected = ((frames,) if frames > 1 else ()) + shape
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
    for frame in pixels if frames > 1 else (pixels,):
        _check_frame(frame, shape, ds, mismatch)
    return pixels, bool(mismatch)


def _transfer_syntax(ds):
    from pydicom.uid import ExplicitVRBigEndian, ExplicitVRLittleEndian, ImplicitVRLittleEndian, UID

    syntax = getattr(ds, "_dicom_decoding_transfer_syntax", None)
    if syntax is None:
        syntax = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    if syntax:
        return UID(syntax)
    implicit, little = ds.original_encoding[:2]
    if implicit:
        return ImplicitVRLittleEndian
    return ExplicitVRLittleEndian if little else ExplicitVRBigEndian


def _pixel_element(path: Path, ds, syntax):
    from pydicom.pixels.utils import get_expected_length

    element = ds.get_item(0x7FE00010, keep_deferred=True)
    if element is None or not hasattr(element, "value_tell"):
        raise DicomImportError("DICOM has no file-backed PixelData")
    if syntax.is_encapsulated:
        if element.length != 0xFFFFFFFF:
            raise DicomImportError("Encapsulated DICOM PixelData must have undefined length")
    else:
        expected = get_expected_length(ds, unit="bytes")
        # A file-backed decoder reads by dimensions, not the element length.
        # Validate the declaration first so it cannot read following elements
        # as pixels or accept missing/truncated frames.
        if element.length not in (expected, expected + expected % 2):
            raise DicomImportError("DICOM PixelData length does not match its declared dimensions")
        if element.value_tell + element.length > path.stat().st_size:
            raise DicomImportError("DICOM PixelData is truncated")
    return element


def _decoded_frames(path: Path, ds, *, temp_dir: Path | None = None):
    from pydicom.pixels import get_decoder
    from pydicom.pixels.utils import as_pixel_options

    from .video import is_video_transfer_syntax, iter_video_frames

    syntax = _transfer_syntax(ds)
    element = _pixel_element(path, ds, syntax)
    max_frame_bytes = limits()["max_frame_bytes"]
    if is_video_transfer_syntax(syntax):
        yield from iter_video_frames(
            path, ds, element.value_tell, max_frame_bytes=max_frame_bytes, temp_dir=temp_dir
        )
        return
    try:
        decoder = get_decoder(syntax)
    except NotImplementedError as exc:
        raise DicomImportError("Unsupported DICOM transfer syntax") from exc
    options = as_pixel_options(ds)
    options.update(pixel_keyword="PixelData", pixel_vr=element.VR or "OW")
    with path.open("rb") as source:
        source.seek(element.value_tell)
        reader = FrameReader(source, max_frame_bytes)
        # pydicom 3.0's native YBR_FULL_422 decoder changes the runner's
        # photometric interpretation while expanding its first frame. Reuse
        # the file but initialize that decoder per frame so later offsets
        # still use the original two bytes per pixel, not three.
        indices = (
            ((index,) for index in range(int(getattr(ds, "NumberOfFrames", 1) or 1)))
            if not syntax.is_encapsulated and ds.PhotometricInterpretation == "YBR_FULL_422"
            else (None,)
        )
        for selected in indices:
            with closing(decoder.iter_array(reader, indices=selected, **options)) as frames:
                for pixels, _properties in frames:
                    reader.next_frame()
                    yield pixels


def iter_dicom_frames(path: Path, ds, *, temp_dir: Path | None = None):
    """Yield validated frames without materializing a whole DICOM instance."""
    expected_count, shape, mismatch = _pixel_parameters(ds)
    with ExitStack() as stack:
        frames = stack.enter_context(closing(_decoded_frames(path, ds, temp_dir=temp_dir)))
        other = None
        if mismatch:
            corrected = copy.deepcopy(ds)
            corrected.HighBit = int(ds.BitsStored) - 1
            other = stack.enter_context(
                closing(_decoded_frames(path, corrected, temp_dir=temp_dir))
            )
        count = 0
        for frame in frames:
            _check_frame(frame, shape, ds, mismatch)
            if other is not None:
                corrected_frame = next(other, None)
                if (
                    corrected_frame is None
                    or frame.shape != corrected_frame.shape
                    or frame.dtype != corrected_frame.dtype
                    or not np.array_equal(frame, corrected_frame)
                ):
                    raise DicomImportError("HighBit correction changes decoded pixels")
            count += 1
            if count > expected_count:
                raise DicomImportError("Decoded DICOM frame count changed")
            yield frame
        if count != expected_count or other is not None and next(other, None) is not None:
            raise DicomImportError("Decoded DICOM frame count changed")


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


def write_mp4(path: Path, pixels, ds) -> None:
    # A factory can be reopened for the range pass without retaining frames.
    if isinstance(pixels, np.ndarray):
        factory = lambda: (frame for frame in pixels)
        height, width = pixels.shape[1:3]
        color = pixels.ndim == 4
        expected_count = len(pixels)
        byte_pixels = pixels.dtype == np.uint8
    else:
        factory = pixels
        height, width = int(ds.Rows), int(ds.Columns)
        color = int(getattr(ds, "SamplesPerPixel", 1)) > 1
        expected_count = int(ds.NumberOfFrames)
        byte_pixels = int(ds.BitsAllocated) <= 8 and int(ds.PixelRepresentation) == 0
    # MPEG-4/yuv420 encoders otherwise silently crop odd dimensions.
    if width % 2 or height % 2:
        raise DicomImportError(
            "MP4 requires even image dimensions; no implicit resizing is performed"
        )
    low, high = 0, 255
    if not byte_pixels:
        # Preserve one range across the whole video. Per-frame normalization
        # would change relative brightness and introduce flickering.
        low, high, count = None, None, 0
        with closing(factory()) as frames:
            for frame in frames:
                minimum, maximum = int(frame.min()), int(frame.max())
                low = minimum if low is None else min(low, minimum)
                high = maximum if high is None else max(high, maximum)
                count += 1
        if count != expected_count or low is None:
            raise DicomImportError("Decoded DICOM frame count changed")
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), frame_rate(ds), (width, height), color
    )
    try:
        if not writer.isOpened():
            raise DicomImportError("MP4 encoder is unavailable")
        count = 0
        with closing(factory()) as frames:
            for frame in frames:
                if frame.dtype == np.uint8 or 0 <= low <= high <= 255:
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
                count += 1
        if count != expected_count:
            raise DicomImportError("Decoded DICOM frame count changed")
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
    if count != expected_count:
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
    failed_tasks: dict[str, str] = field(default_factory=dict)
    high_bit_checked: int = 0
    ignored_files: int = 0
    study_uids: dict[str, str] = field(default_factory=dict)


def prepare(
    files: list[Path],
    output: Path,
    prefix: str,
    existing_names=(),
    *,
    reader=None,
    progress: Callable[[int, int], None] | None = None,
    inspection_progress: Callable[[int, int], None] | None = None,
) -> Plan:
    @contextmanager
    def source(path, *, headers=False):
        if reader is None:
            with open_dicom(path, headers=headers, temp_dir=output) as value:
                yield value
        else:
            yield path, reader(path)

    output.mkdir(parents=True, exist_ok=False)
    plan, tasks, hashes, folders = Plan(), {}, {}, {}
    candidates = []
    existing = set(existing_names)
    # Read headers first and sort by the final name, independent of archive order.
    if inspection_progress:
        inspection_progress(0, len(files))
    for index, path in enumerate(sorted(files)):
        try:
            with path.open("rb") as stream:
                header = stream.read(132)
            if path.suffix.lower() not in (".dcm", ".dicom") and header[128:132] != b"DICM":
                plan.ignored_files += 1
                continue
            with source(path, headers=True) as (_decoding_path, ds):
                if not hasattr(ds, "Rows") or not hasattr(ds, "Columns"):
                    plan.ignored_files += 1
                    continue
                frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
                task_name, filename = names(ds, prefix, frames > 1)
                study_uid = str(ds.StudyInstanceUID).strip()
                if plan.study_uids.setdefault(task_name, study_uid) != study_uid:
                    plan.failed_tasks[task_name] = "Different studies produce the same task name"
                if task_name in existing:
                    plan.skipped_tasks.add(task_name)
                    continue
                # Retaining Dataset objects here also retains their decoded pixel cache
                # later. Keep only paths so memory does not grow with every converted image.
                candidates.append((task_name, filename, frames, path))
        finally:
            if inspection_progress:
                inspection_progress(index + 1, len(files))
    if progress:
        progress(0, len(candidates))
    for index, (task_name, filename, frames, path) in enumerate(
        sorted(candidates, key=lambda x: x[:2])
    ):
        # One bad image invalidates its complete task, never just a subset of its frames.
        if task_name not in plan.failed_tasks:
            try:
                with source(path) as (decoding_path, ds):
                    task = tasks.setdefault(task_name, PreparedTask(task_name, frames > 1))
                    if task.video != (frames > 1) or task.video and task.media:
                        raise DicomImportError("Image/video task name collision")
                    # Group directories are numbered; basenames retain the reference naming.
                    folder = folders.setdefault(task_name, output / str(len(folders)))
                    folder.mkdir(exist_ok=True)
                    if reader is None:
                        _count, _shape, checked = _pixel_parameters(ds)
                        factory = lambda: iter_dicom_frames(decoding_path, ds, temp_dir=folder)
                    else:
                        pixels, checked = decode_pixels(ds)
                        factory = lambda: (frame for frame in (pixels if frames > 1 else (pixels,)))
                    tags = metadata(ds)
                    target = folder / filename
                    duplicate = False
                    if frames == 1:
                        with closing(factory()) as decoded:
                            encoded = encode_png(next(decoded))
                            if next(decoded, None) is not None:
                                raise DicomImportError("Decoded DICOM frame count changed")
                        key = (task_name, hashlib.sha256(encoded).digest())
                        bucket = hashes.setdefault(key, [])
                        duplicate = any(previous.read_bytes() == encoded for previous in bucket)
                        if duplicate:
                            plan.duplicates += 1
                        else:
                            if target.exists():
                                raise DicomImportError(
                                    "Different images produce the same output name"
                                )
                            target.write_bytes(encoded)
                            bucket.append(target)
                    else:
                        write_mp4(target, factory, ds)
                    plan.high_bit_checked += int(checked)
                    if not duplicate:
                        task.media.append(target)
                        task.tags[filename] = tags
                        task.frames += frames
            except DicomImportError as exc:
                plan.failed_tasks[task_name] = str(exc)
            except (ValueError, TypeError, AttributeError, RuntimeError, cv2.error) as exc:
                # Decoder exceptions can contain filenames or patient values. Keep only the type.
                plan.failed_tasks[task_name] = f"DICOM conversion failed ({type(exc).__name__})"
            if task_name in plan.failed_tasks:
                tasks.pop(task_name, None)
                if task_name in folders:
                    shutil.rmtree(folders[task_name])
        if progress:
            progress(index + 1, len(candidates))
    plan.tasks = [task for task in tasks.values() if task.media]
    if not plan.tasks and not plan.skipped_tasks and not plan.failed_tasks:
        raise DicomImportError("No supported DICOM images were found")
    return plan
