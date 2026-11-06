# SPDX-License-Identifier: MIT
"""Decode encapsulated DICOM video without copying or buffering its bitstream."""

from __future__ import annotations

import io
import struct
from bisect import bisect_right
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import numpy as np

from . import DicomImportError

_UID_PREFIX = "1.2.840.10008.1.2.4."
_SYNTAXES = {
    **{
        f"{_UID_PREFIX}{number}{suffix}": "mpeg2video"
        for number in (100, 101)
        for suffix in ("", ".1")
    },
    **{
        f"{_UID_PREFIX}{number}{suffix}": "h264"
        for number in range(102, 107)
        for suffix in ("", ".1")
    },
    f"{_UID_PREFIX}107": "hevc",
    f"{_UID_PREFIX}108": "hevc",
}
_STEREO_SYNTAXES = {
    f"{_UID_PREFIX}{number}{suffix}" for number in (105, 106) for suffix in ("", ".1")
}
_ITEM = (0xFFFE, 0xE000)
_DELIMITER = (0xFFFE, 0xE0DD)
_READ_BYTES = 1024**2


def is_video_transfer_syntax(uid: object) -> bool:
    return str(uid) in _SYNTAXES


class FragmentedVideoReader(io.RawIOBase):
    """Expose fragment payloads as one seekable stream, retaining only offsets.

    Pixel Data items can be several GiB long, unlike a single decoded frame.
    The index skips payloads using seek; subsequent reads go directly to the
    source DICOM and remain bounded even across item boundaries.
    """

    def __init__(self, source: BinaryIO, pixel_offset: int, *, fragmentable: bool):
        super().__init__()
        self._source = source
        self._position = 0
        self._size = 0
        self._starts: list[int] = []
        self._offsets: list[int] = []
        end = source.seek(0, io.SEEK_END)
        if pixel_offset < 0 or pixel_offset > end - 8:
            raise DicomImportError("Invalid DICOM video Pixel Data offset")
        source.seek(pixel_offset)
        tag, length = self._item_header()
        if tag != _ITEM or length % 4 or length > end - source.tell():
            raise DicomImportError("Invalid DICOM video Basic Offset Table")
        # Video navigation belongs to the codec; an offset table must not be
        # mistaken for bitstream bytes or loaded as one large Python value.
        if length:
            if source.read(4) != b"\0" * 4:
                raise DicomImportError("Invalid DICOM video Basic Offset Table")
            source.seek(length - 4, io.SEEK_CUR)
        while True:
            tag, length = self._item_header()
            if tag == _DELIMITER:
                if length or not self._starts:
                    raise DicomImportError("Invalid DICOM video sequence delimiter")
                break
            if tag != _ITEM or length < 2 or length % 2 or length > end - source.tell():
                raise DicomImportError("Invalid or truncated DICOM video fragment")
            if self._starts and not fragmentable:
                raise DicomImportError("This DICOM video transfer syntax requires one fragment")
            self._starts.append(self._size)
            self._offsets.append(source.tell())
            self._size += length
            source.seek(length, io.SEEK_CUR)

    def _item_header(self) -> tuple[tuple[int, int], int]:
        value = self._source.read(8)
        if len(value) != 8:
            raise DicomImportError("Truncated DICOM video fragment header")
        group, element, length = struct.unpack("<HHI", value)
        return (group, element), length

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        origins = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}
        if whence not in origins or offset + origins[whence] < 0:
            raise ValueError("Invalid video seek")
        self._position = offset + origins[whence]
        return self._position

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        if size is None or size < 0:
            raise DicomImportError("Unbounded DICOM video reads are not supported")
        remaining = min(size, _READ_BYTES, max(0, self._size - self._position))
        result = []
        while remaining:
            index = bisect_right(self._starts, self._position) - 1
            fragment_end = self._starts[index + 1] if index + 1 < len(self._starts) else self._size
            self._source.seek(self._offsets[index] + self._position - self._starts[index])
            count = min(remaining, fragment_end - self._position)
            chunk = self._source.read(count)
            if len(chunk) != count:
                raise DicomImportError("Truncated DICOM video fragment")
            result.append(chunk)
            remaining -= count
            self._position += count
        return b"".join(result)

    def readinto(self, buffer) -> int:
        value = self.read(len(buffer))
        buffer[: len(value)] = value
        return len(value)


def _video_properties(ds, uid: str, max_frame_bytes: int) -> tuple[int, int, int, int, int]:
    if uid in _STEREO_SYNTAXES:
        raise DicomImportError("Stereoscopic DICOM video is not supported by the 2D importer")
    rows, columns = int(ds.Rows), int(ds.Columns)
    frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
    samples = int(getattr(ds, "SamplesPerPixel", 1))
    bits = 10 if uid == f"{_UID_PREFIX}108" else 8
    mono = _SYNTAXES[uid] == "mpeg2video" and samples == 1
    if (
        rows < 1
        or columns < 1
        or frames < 1
        or samples != (1 if mono else 3)
        or str(getattr(ds, "PhotometricInterpretation", ""))
        != ("MONOCHROME2" if mono else "YBR_PARTIAL_420")
        or int(ds.BitsAllocated) != (16 if bits == 10 else 8)
        or int(ds.BitsStored) != bits
        or int(ds.PixelRepresentation) != 0
    ):
        raise DicomImportError("Unsupported DICOM video pixel description")
    if rows * columns * samples * (2 if bits == 10 else 1) > max_frame_bytes:
        raise DicomImportError("Decoded DICOM frame exceeds CVAT_DICOM_MAX_FRAME_BYTES")
    return rows, columns, frames, samples, bits


def _deny_external_io(*_args, **_kwargs):
    raise DicomImportError("External references are not supported in DICOM video")


def iter_video_frames(
    path: Path,
    ds,
    pixel_offset: int,
    *,
    max_frame_bytes: int,
    temp_dir: Path | None = None,
) -> Iterator[np.ndarray]:
    """Yield RGB or monochrome frames from a video DICOM using CVAT's PyAV.

    `pixel_offset` points at the first item tag in the Pixel Data value. The
    source remains on disk, including for fragments larger than the frame
    budget. `temp_dir` is accepted alongside the other frame readers; this
    reader does not need a temporary copy.
    """
    import av

    uid = str(ds.file_meta.TransferSyntaxUID)
    if uid not in _SYNTAXES:
        raise DicomImportError("Unsupported DICOM video transfer syntax")
    rows, columns, count, samples, bits = _video_properties(ds, uid, max_frame_bytes)
    fragmentable = uid.endswith(".1") or _SYNTAXES[uid] == "hevc"
    # MPEG-2 additionally permits elementary and program streams. Restrict
    # automatic probing so a payload cannot act as a playlist or open a URL.
    formats = "mov,mpegts,mpeg,mpegvideo" if _SYNTAXES[uid] == "mpeg2video" else "mov,mpegts"
    try:
        with (
            path.open("rb") as source,
            FragmentedVideoReader(source, pixel_offset, fragmentable=fragmentable) as video,
        ):
            with av.open(
                video,
                mode="r",
                options={"format_whitelist": formats, "protocol_whitelist": ""},
                io_open=_deny_external_io,
            ) as container:
                if len(container.streams.video) != 1:
                    raise DicomImportError("DICOM video must contain exactly one video stream")
                stream = container.streams.video[0]
                codec = stream.codec_context
                if codec.name != _SYNTAXES[uid]:
                    raise DicomImportError("DICOM transfer syntax does not match the video codec")
                if (codec.height, codec.width) != (rows, columns):
                    raise DicomImportError("Encoded video dimensions differ from DICOM metadata")
                # Bound codec reference buffers to one decoding thread. Coded
                # dimensions may be padded to a 64-pixel coding block, so use
                # the aligned declared size as FFmpeg's additional pixel guard.
                codec.thread_count = 1
                codec.thread_type = "NONE"
                codec.options = {
                    **codec.options,
                    "max_pixels": str(((rows + 63) // 64 * 64) * ((columns + 63) // 64 * 64)),
                }
                seen = 0
                for frame in container.decode(stream):
                    if seen >= count:
                        raise DicomImportError("Encoded video has more frames than DICOM metadata")
                    if (frame.height, frame.width) != (rows, columns):
                        raise DicomImportError("Encoded video dimensions change between frames")
                    if frame.is_corrupt:
                        raise DicomImportError("Corrupt frame in encoded DICOM video")
                    if max(component.bits for component in frame.format.components) != bits:
                        raise DicomImportError(
                            "Encoded video bit depth differs from DICOM metadata"
                        )
                    if frame.rotation:
                        raise DicomImportError("Rotated encoded DICOM video is unsupported")
                    if samples == 1:
                        pixels = frame.to_ndarray(format="gray")
                    elif bits == 10:
                        # Preserve Main 10 precision for the shared, whole-video
                        # intensity scaling instead of silently reducing to 8 bit.
                        pixels = frame.to_ndarray(format="rgb48le")
                        np.right_shift(pixels, 6, out=pixels)
                    else:
                        pixels = frame.to_ndarray(format="rgb24")
                    if pixels.nbytes > max_frame_bytes:
                        raise DicomImportError(
                            "Decoded DICOM frame exceeds CVAT_DICOM_MAX_FRAME_BYTES"
                        )
                    seen += 1
                    yield pixels
                if seen != count:
                    raise DicomImportError("Encoded video frame count differs from DICOM metadata")
    except av.error.FFmpegError as exc:
        # FFmpeg messages may contain file paths or patient values.
        raise DicomImportError(f"DICOM video decoding failed ({type(exc).__name__})") from None
