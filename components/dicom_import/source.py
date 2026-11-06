# SPDX-License-Identifier: MIT
"""File-backed DICOM readers, including incrementally inflated datasets."""

from __future__ import annotations

import copy
import os
import tempfile
import zlib
from contextlib import contextmanager
from pathlib import Path

from . import DicomImportError
from .settings import limits

READ_BLOCK = 1024 * 1024


class _InflatedFile:
    """Seekable on-disk cache of a raw-deflate stream.

    pydicom's ordinary Deflated reader reads and expands the complete dataset
    in RAM, even for stop_before_pixels. Replacing only the transfer syntax in
    a temporary file lets its ordinary parser use bounded reads and seeks.
    Header inspection expands only the prefix that the parser actually needs.
    """

    def __init__(self, source, target, max_bytes: int):
        self._source = source
        self._target = target
        self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        self._pending = b""
        self._size = target.tell()
        self._expanded = 0
        self._max_bytes = max_bytes
        self._eof = False
        self.name = target.name

    def _expand(self) -> None:
        data = self._pending or self._source.read(READ_BLOCK)
        try:
            value = self._decoder.decompress(data, READ_BLOCK)
        except zlib.error as exc:
            raise DicomImportError("Invalid deflated DICOM dataset") from exc
        # zlib can retain decoded output after consuming the final compressed
        # input byte. Drain it with an empty input before treating EOF as a
        # truncated stream (notably when output crosses a READ_BLOCK boundary).
        if not data and not value and not self._decoder.eof:
            raise DicomImportError("Truncated deflated DICOM dataset")
        self._pending = self._decoder.unconsumed_tail
        self._expanded += len(value)
        if self._expanded > self._max_bytes:
            raise DicomImportError("Deflated DICOM exceeds CVAT_DICOM_MAX_EXPANDED_BYTES")
        offset = self._target.tell()
        self._target.seek(self._size)
        self._target.write(value)
        self._target.flush()
        self._size += len(value)
        self._target.seek(offset)
        if self._decoder.eof:
            # PS3.5 A.5 allows a single zero padding byte after the deflate stream.
            trailing = self._decoder.unused_data + self._source.read(2)
            if trailing not in (b"", b"\0"):
                raise DicomImportError("Unexpected data after deflated DICOM dataset")
            self._eof = True
            self._pending = b""

    def _ensure(self, end: int | None) -> None:
        while not self._eof and (end is None or self._size < end):
            self._expand()

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            self._ensure(None)
        else:
            self._ensure(self.tell() + size)
        return self._target.read(size)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_CUR:
            offset += self.tell()
        elif whence == os.SEEK_END:
            self._ensure(None)
            offset += self._size
        elif whence != os.SEEK_SET:
            raise ValueError("Invalid seek origin")
        if offset < 0:
            raise ValueError("Negative seek offset")
        self._ensure(offset)
        return self._target.seek(offset)

    def tell(self) -> int:
        return self._target.tell()


@contextmanager
def open_dicom(path: Path, *, headers: bool = False, temp_dir: Path | None = None):
    """Yield (decoding path, Dataset), preserving the original file metadata.

    A deflated source is normalized into one temporary file. It lives only
    while the caller reads its deferred tags or frames, including on errors.
    """
    from pydicom import dcmread
    from pydicom.filereader import _read_file_meta_info, read_preamble
    from pydicom.filewriter import write_file_meta_info
    from pydicom.uid import DeflatedExplicitVRLittleEndian, ExplicitVRLittleEndian

    with path.open("rb") as source:
        preamble = read_preamble(source, force=True)
        file_meta = _read_file_meta_info(source)
        if file_meta.get("TransferSyntaxUID") != DeflatedExplicitVRLittleEndian:
            # Keep the original path for pydicom's later deferred reads.
            yield path, dcmread(path, force=True, defer_size="1 KB", stop_before_pixels=headers)
            return

        with tempfile.NamedTemporaryFile(
            mode="w+b", prefix="dicom-inflated-", suffix=".dcm", dir=temp_dir
        ) as target:
            target.write(preamble if preamble is not None else b"\0" * 128)
            target.write(b"DICM")
            normalized_meta = copy.deepcopy(file_meta)
            normalized_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            write_file_meta_info(target, normalized_meta, enforce_standard=False)
            inflated = _InflatedFile(source, target, limits()["max_expanded_bytes"])
            inflated.seek(0)
            ds = dcmread(inflated, force=True, defer_size="1 KB", stop_before_pixels=headers)
            # Every deferred value the parser skipped has already been written
            # to disk. Reopen that file for deferred reads instead of retaining
            # the inflater (or trying to deepcopy its live file handles).
            ds.buffer = None
            ds.fileobj_type = open
            # Tags describe the uploaded instance; decoding uses the normalized
            # syntax separately and never changes its saved TransferSyntaxUID.
            ds._dicom_decoding_transfer_syntax = ExplicitVRLittleEndian
            ds.file_meta = file_meta
            yield Path(target.name), ds


class FrameReader:
    """Bound individual decoder reads and accumulated compressed frame data."""

    def __init__(self, source, max_frame_bytes: int):
        self._source = source
        # Encapsulation and lossless compression can be larger than the decoded
        # array. The allowance also covers one-fragment lookahead in pydicom.
        self._budget = max(READ_BLOCK, 2 * max_frame_bytes)
        self._remaining = self._budget

    def next_frame(self) -> None:
        self._remaining = self._budget

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self._remaining:
            raise DicomImportError("Encoded DICOM frame exceeds the configured frame budget")
        # Four/eight-byte item headers are scanned with seek before frame
        # decoding and must not impose a limit on the length of the movie.
        if size > 8:
            self._remaining -= size
        if size <= READ_BLOCK:
            return self._source.read(size)
        parts = []
        while size:
            part = self._source.read(min(size, READ_BLOCK))
            if not part:
                break
            parts.append(part)
            size -= len(part)
        return b"".join(parts)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._source.seek(offset, whence)

    def tell(self) -> int:
        return self._source.tell()
