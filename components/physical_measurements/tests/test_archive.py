# SPDX-License-Identifier: MIT
import io
import json
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from components.physical_measurements.archive import backup_member, read_backup_document
from components.physical_measurements.validation import MAX_DOCUMENT_BYTES, empty_document


def test_missing_sidecar_preserves_old_backups():
    with ZipFile(io.BytesIO(), "w") as archive:
        archive.writestr("task.json", "{}")
        assert read_backup_document(archive) is None


@pytest.mark.parametrize("prefix", [None, "", "task_0", "task_1"])
def test_reads_only_exact_task_or_project_member(prefix):
    value = {**empty_document(), "frames": {"0": None}}
    with ZipFile(io.BytesIO(), "w") as archive:
        archive.writestr(backup_member(prefix), json.dumps(value))
        archive.writestr("other_task/physical_measurements.json", "invalid")
        archive.writestr("../physical_measurements.json", "invalid")
        assert read_backup_document(archive, prefix) == value


def test_rejects_ambiguous_duplicate_sidecar():
    with ZipFile(io.BytesIO(), "w") as archive:
        archive.writestr(backup_member(), json.dumps(empty_document()))
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr(backup_member(), "{}")
        with pytest.raises(ValueError, match="Duplicate"):
            read_backup_document(archive)


def test_rejects_large_compressed_sidecar_before_decompressing():
    with ZipFile(io.BytesIO(), "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(backup_member(), b" " * (MAX_DOCUMENT_BYTES + 1))
        with pytest.raises(ValueError, match="1 MiB"):
            read_backup_document(archive)


def test_rejects_invalid_json_in_sidecar():
    with ZipFile(io.BytesIO(), "w") as archive:
        archive.writestr(backup_member(), b'{"version": 1, "frames": {"0": NaN}}')
        with pytest.raises(ValueError):
            read_backup_document(archive)
