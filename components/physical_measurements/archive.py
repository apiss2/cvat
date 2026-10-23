# SPDX-License-Identifier: MIT
"""Read the optional calibration member without extracting untrusted ZIP paths."""

from pathlib import PurePosixPath
from zipfile import ZipFile

from .validation import MAX_DOCUMENT_BYTES, parse_document_bytes

BACKUP_FILENAME = "physical_measurements.json"


def backup_member(prefix: str | None = None) -> str:
    return str(PurePosixPath(prefix or "") / BACKUP_FILENAME)


def read_backup_document(archive: ZipFile, prefix: str | None = None) -> dict | None:
    name = backup_member(prefix)
    members = [entry for entry in archive.infolist() if entry.filename == name]
    if not members:
        return None
    if len(members) != 1:
        raise ValueError("Duplicate physical measurement sidecar in backup")
    if members[0].file_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Calibration JSON must not exceed 1 MiB")
    with archive.open(members[0]) as stream:
        return parse_document_bytes(stream.read(MAX_DOCUMENT_BYTES + 1))
