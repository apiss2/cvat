# SPDX-License-Identifier: MIT
"""Disk-backed, bounded DICOM upload sessions, without a database schema.

The project upload permission is checked by the HTTP entry point. Session and
file locks coordinate CVAT server processes and the existing import worker.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from itertools import chain
from pathlib import Path
from uuid import UUID, uuid4

from . import DicomImportError
from .settings import limits

CHUNK_SIZE = 16 * 1024**2
FILES_PER_BATCH = 500
PARALLEL_UPLOADS = 4
SESSION_PREFIX = "dicom-upload-"
SESSION_PATTERN = re.compile(r"dicom-upload-[0-9]+-[0-9a-f-]{36}")
SOURCE_NAME = "import.json"
SOURCE_MARKER = {"dicom_staged_upload": 1}


class UploadError(DicomImportError):
    status_code = 400


class MissingUpload(UploadError):
    status_code = 404


class UploadConflict(UploadError):
    status_code = 409


class UploadTooLarge(UploadError):
    status_code = 413


class UploadStorageFull(UploadError):
    status_code = 507


def integer(value, name: str, *, minimum=0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise UploadError(f"Invalid {name}")
    return value


def identifier(value) -> str:
    if not isinstance(value, str):
        raise UploadError("Invalid upload ID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise UploadError("Invalid upload ID") from None
    return value


def _read_json(path: Path):
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        raise MissingUpload("The upload session is no longer available") from None
    except (ValueError, UnicodeError):
        raise UploadError("The upload session metadata is invalid") from None


def write_json(path: Path, value) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def session_lock(path: Path, *, shared=False, blocking=True, create=False):
    """A shared session lock permits parallel files and excludes finish/cleanup."""
    try:
        descriptor = os.open(path / ".lock", os.O_RDWR | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError:
        raise MissingUpload("The upload session is no longer available") from None
    with os.fdopen(descriptor, "r+b") as lock:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(lock.fileno(), operation | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _safe_path(value) -> str:
    from .archive import relative_path

    if not _valid_text(value, 1024):
        raise UploadError("Invalid file path")
    if value.endswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise UploadError("Invalid file path")
    relative_path(value)
    return value


def _valid_text(value, max_bytes):
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeError:
        return False


def _suffix(name: str) -> str:
    return next(
        (
            suffix
            for suffix in (".tar.gz", ".dicom", ".dcm", ".zip", ".tar", ".tgz")
            if name.lower().endswith(suffix)
        ),
        ".bin",
    )


class UploadSession:
    def __init__(self, directory: Path, *, user_id: int, project_id: int, organization_id):
        self.directory = Path(directory)
        self.owner = {
            "user_id": user_id,
            "project_id": project_id,
            "organization_id": organization_id,
        }

    @classmethod
    def for_project(cls, upload_dir: Path, upload_id, **owner):
        name = f"{SESSION_PREFIX}{owner['user_id']}-{identifier(upload_id)}"
        return cls(Path(upload_dir) / name, **owner)

    @property
    def state_path(self):
        return self.directory / "session.json"

    def read(self):
        data = _read_json(self.state_path)
        if not isinstance(data, dict) or any(
            data.get(key) != value for key, value in self.owner.items()
        ):
            raise MissingUpload("The upload session is not available in this workspace")
        return data

    def save(self, data):
        write_json(self.state_path, data)
        self.touch()

    def touch(self):
        # Native temporary-directory expiry is based on access time, not mtime.
        os.utime(self.directory, None)

    @contextmanager
    def locked(self, *, shared=False):
        with session_lock(self.directory, shared=shared):
            data = self.read()
            self.touch()
            yield data

    def start(self, *, prefix, file_count, total_bytes):
        from .conversion import component

        if not _valid_text(prefix, 256):
            raise UploadError("Invalid data-source prefix")
        prefix = prefix.strip()
        component(prefix, "data-source prefix")
        file_count = integer(file_count, "file count", minimum=1)
        total_bytes = integer(total_bytes, "total byte count")
        configured = limits()
        if (
            file_count > configured["max_upload_files"]
            or total_bytes > configured["max_upload_bytes"]
        ):
            raise UploadTooLarge("The folder exceeds the configured DICOM upload limits")
        initial = {
            "version": 1,
            **self.owner,
            "prefix": prefix,
            "file_count": file_count,
            "total_bytes": total_bytes,
        }
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(exist_ok=True)
        with session_lock(self.directory, create=True):
            if self.state_path.exists():
                data = self.read()
                if any(data.get(key) != value for key, value in initial.items()):
                    raise UploadConflict("This upload ID belongs to another selection")
            else:
                if shutil.disk_usage(self.directory).free < total_bytes:
                    raise UploadStorageFull(
                        "Insufficient free disk space for the selected source files"
                    )
                (self.directory / "batches").mkdir(exist_ok=True)
                (self.directory / "files").mkdir(exist_ok=True)
                data = {
                    **initial,
                    "operation_id": str(uuid4()),
                    "state": "uploading",
                    "registered_files": 0,
                    "registered_bytes": 0,
                }
                self.save(data)
            self.touch()
        return {
            "upload_id": self.directory.name.removeprefix(
                f"{SESSION_PREFIX}{self.owner['user_id']}-"
            ),
            "chunk_size": CHUNK_SIZE,
            "parallel_uploads": PARALLEL_UPLOADS,
            "max_files_per_batch": FILES_PER_BATCH,
            **configured,
        }

    def _batch_path(self, start_index):
        return self.directory / "batches" / f"{start_index:08d}.json"

    def entries(self, count):
        for start in range(0, count, FILES_PER_BATCH):
            yield from _read_json(self._batch_path(start))

    def file_entry(self, index, data):
        index = integer(index, "file index")
        if index >= data["registered_files"]:
            raise UploadError("The file has not been registered")
        start = index // FILES_PER_BATCH * FILES_PER_BATCH
        return _read_json(self._batch_path(start))[index - start]

    def file_path(self, index, entry):
        return self.directory / "files" / f"{index:08d}{_suffix(entry['path'])}"

    def register(self, *, start_index, files):
        start = integer(start_index, "batch start index")
        if (
            start % FILES_PER_BATCH
            or not isinstance(files, list)
            or not 1 <= len(files) <= FILES_PER_BATCH
        ):
            raise UploadError("Invalid file registration batch")
        entries = []
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"path", "size"}:
                raise UploadError("Invalid file metadata")
            entries.append(
                {"path": _safe_path(entry["path"]), "size": integer(entry["size"], "file size")}
            )
        with self.locked() as data:
            if data["state"] != "uploading":
                raise UploadConflict("This upload has already been submitted")
            if start < data["registered_files"]:
                if _read_json(self._batch_path(start)) != entries:
                    raise UploadConflict("File registration does not match the accepted batch")
                return {"registered_files": data["registered_files"]}
            if start != data["registered_files"] or start + len(entries) > data["file_count"]:
                raise UploadConflict("File batches must be registered in order")
            count = start + len(entries)
            size = data["registered_bytes"] + sum(entry["size"] for entry in entries)
            if size > data["total_bytes"] or (
                count < data["file_count"] and len(entries) != FILES_PER_BATCH
            ):
                raise UploadError("The registered files do not match the selected folder")
            if count == data["file_count"]:
                if size != data["total_bytes"]:
                    raise UploadError(
                        "The registered byte count does not match the selected folder"
                    )
                seen = set()
                for entry in chain(self.entries(start), entries):
                    path = entry["path"].casefold()
                    if path in seen:
                        raise UploadError("Duplicate file paths in the selected folder")
                    seen.add(path)
            write_json(self._batch_path(start), entries)
            data.update(registered_files=count, registered_bytes=size)
            self.save(data)
        return {"registered_files": count}

    def write_chunk(self, *, index, offset, length, stream):
        offset = integer(offset, "upload offset")
        length = integer(length, "chunk length", minimum=1)
        if length > CHUNK_SIZE:
            raise UploadTooLarge("A DICOM upload chunk exceeds the permitted size")
        with self.locked(shared=True) as data:
            if data["state"] != "uploading":
                raise UploadConflict("This upload has already been submitted")
            if data["registered_files"] != data["file_count"]:
                raise UploadConflict("Register every file before sending file contents")
            entry = self.file_entry(index, data)
            if offset + length > entry["size"]:
                raise UploadError("The chunk exceeds the declared file size")
            path = self.file_path(index, entry)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "r+b") as output:
                fcntl.flock(output.fileno(), fcntl.LOCK_EX)
                current = output.seek(0, os.SEEK_END)
                if offset > current:
                    raise UploadConflict("The chunk starts after the acknowledged file contents")
                output.seek(offset)
                remaining = length
                while remaining:
                    block = stream.read(min(1024**2, remaining))
                    if not block:
                        raise UploadError("The upload chunk was interrupted; retry the same chunk")
                    if len(block) > remaining:
                        raise UploadError("The upload chunk exceeds its declared size")
                    overlap = min(len(block), max(current - output.tell(), 0))
                    if overlap and output.read(overlap) != block[:overlap]:
                        raise UploadConflict(
                            "A retried chunk differs from the accepted file contents"
                        )
                    if overlap < len(block):
                        output.write(block[overlap:])
                    remaining -= len(block)
            self.touch()
        return {"offset": offset + length}

    def verify_files(self, data):
        if data["registered_files"] != data["file_count"]:
            raise UploadConflict("The selected files have not all been registered")
        for index, entry in enumerate(self.entries(data["file_count"])):
            path = self.file_path(index, entry)
            if entry["size"] == 0:
                path.touch(exist_ok=True)
            if not path.is_file() or path.is_symlink() or path.stat().st_size != entry["size"]:
                raise UploadConflict("The selected files have not all been uploaded")

    def status(self, *, index=None):
        with self.locked(shared=True) as data:
            result = {key: data[key] for key in ("state", "registered_files")}
            if "rq_id" in data:
                result["rq_id"] = data["rq_id"]
            if index is not None and data["state"] == "uploading":
                path = self.file_path(index, self.file_entry(index, data))
                result["offset"] = path.stat().st_size if path.exists() else 0
            return result

    def remove_payload(self):
        for name in ("files", "batches"):
            path = self.directory / name
            if path.exists():
                shutil.rmtree(path)
        (self.directory / SOURCE_NAME).unlink(missing_ok=True)


def read_staged_upload(source_path, *, user_id, project_id, organization_id):
    """Only a server-created descriptor inside the owning session is accepted."""
    source = Path(source_path)
    if source.name != SOURCE_NAME or not SESSION_PATTERN.fullmatch(source.parent.name):
        raise UploadError("Invalid staged DICOM import source")
    if _read_json(source) != SOURCE_MARKER:
        raise UploadError("Invalid staged DICOM import descriptor")
    session = UploadSession(
        source.parent, user_id=user_id, project_id=project_id, organization_id=organization_id
    )
    with session.locked(shared=True) as data:
        if data["state"] not in ("submitting", "queued"):
            raise UploadConflict("This DICOM upload is not queued for import")
        session.verify_files(data)
        return data["prefix"], [
            session.file_path(index, entry)
            for index, entry in enumerate(session.entries(data["file_count"]))
        ]
