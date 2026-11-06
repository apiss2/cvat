# SPDX-License-Identifier: MIT
"""Upload sessions with real files and locks; no server or large allocation."""

import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from components.dicom_import.storage import (
    CHUNK_SIZE,
    SOURCE_MARKER,
    SOURCE_NAME,
    MissingUpload,
    UploadConflict,
    UploadError,
    UploadSession,
    UploadStorageFull,
    UploadTooLarge,
    read_staged_upload,
    write_json,
)
from components.dicom_import import DicomImportError


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "components.dicom_import.storage.shutil.disk_usage",
        lambda path: SimpleNamespace(free=2 * 1024**4),
    )
    return UploadSession.for_project(
        tmp_path, str(uuid4()), user_id=7, project_id=11, organization_id=None
    )


def prepare(session, files):
    session.start(
        prefix="synthetic", file_count=len(files), total_bytes=sum(file[1] for file in files)
    )
    for start in range(0, len(files), 500):
        session.register(
            start_index=start,
            files=[{"path": path, "size": size} for path, size in files[start : start + 500]],
        )
    return session


def send(session, content, *, index=0, offset=0, length=None):
    return session.write_chunk(
        index=index,
        offset=offset,
        length=len(content) if length is None else length,
        stream=io.BytesIO(content),
    )


def payload_path(session, index=0):
    data = session.read()
    return session.file_path(index, session.file_entry(index, data))


def test_200_gib_folder_is_registered_without_allocating_or_reading_its_contents(session):
    size = 200 * 1024**3
    prepare(session, [("patient/video.dcm", size)])
    assert session.read()["total_bytes"] == size
    assert list((session.directory / "files").iterdir()) == []
    assert (
        sum(path.stat().st_size for path in session.directory.rglob("*") if path.is_file()) < 4096
    )
    with pytest.raises(UploadConflict, match="not all been uploaded"):
        session.verify_files(session.read())


def test_file_larger_than_4_gib_has_exact_offset_arithmetic(session):
    size = 5 * 1024**3
    prepare(session, [("large.dcm", size + 4)])
    path = payload_path(session)
    with path.open("wb") as stream:
        stream.truncate(size)  # Sparse fixture exercises offsets, not transfer performance.
    assert send(session, b"last", offset=size) == {"offset": size + 4}
    with path.open("rb") as stream:
        stream.seek(size)
        assert stream.read() == b"last"
    session.verify_files(session.read())


def test_start_and_metadata_registration_replays_are_idempotent(session):
    first = session.start(prefix="source", file_count=1, total_bytes=4)
    second = session.start(prefix="source", file_count=1, total_bytes=4)
    assert first == second
    body = {"start_index": 0, "files": [{"path": "folder/test.dcm", "size": 4}]}
    assert session.register(**body) == session.register(**body) == {"registered_files": 1}
    with pytest.raises(UploadConflict, match="another selection"):
        session.start(prefix="other", file_count=1, total_bytes=4)
    with pytest.raises(UploadConflict, match="does not match"):
        session.register(start_index=0, files=[{"path": "other.dcm", "size": 4}])


def test_multiple_metadata_pages_are_validated_before_any_bytes(session):
    files = [(f"folder/{index}.dcm", 1) for index in range(501)]
    session.start(prefix="source", file_count=501, total_bytes=501)
    first = [{"path": name, "size": size} for name, size in files[:500]]
    session.register(start_index=0, files=first)
    with pytest.raises(UploadConflict, match="Register every file"):
        send(session, b"x")
    with pytest.raises(UploadError, match="Duplicate file paths"):
        session.register(start_index=500, files=[{"path": "FOLDER/0.DCM", "size": 1}])
    assert session.read()["registered_files"] == 500
    assert session.register(start_index=500, files=[{"path": "folder/500.dcm", "size": 1}]) == {
        "registered_files": 501
    }
    assert session.register(start_index=0, files=first) == {"registered_files": 501}


@pytest.mark.parametrize(
    "name",
    [
        "../x.dcm",
        "folder/../x.dcm",
        "/x.dcm",
        "C:/x.dcm",
        "x\\x.dcm",
        "a//x.dcm",
        "./x.dcm",
        "x\x00.dcm",
        "x/",
        "x" * 1025,
    ],
)
def test_unsafe_file_paths_are_rejected_before_registration(session, name):
    session.start(prefix="source", file_count=1, total_bytes=1)
    with pytest.raises(DicomImportError):
        session.register(start_index=0, files=[{"path": name, "size": 1}])
    assert session.read()["registered_files"] == 0


def test_upload_limit_and_free_space_are_checked_before_creating_data(session, monkeypatch):
    monkeypatch.setenv("CVAT_DICOM_MAX_UPLOAD_BYTES", "3")
    with pytest.raises(UploadTooLarge):
        session.start(prefix="source", file_count=1, total_bytes=4)
    assert not session.directory.exists()
    monkeypatch.setenv("CVAT_DICOM_MAX_UPLOAD_BYTES", "100")
    monkeypatch.setattr(
        "components.dicom_import.storage.shutil.disk_usage", lambda path: SimpleNamespace(free=3)
    )
    with pytest.raises(UploadStorageFull):
        session.start(prefix="source", file_count=1, total_bytes=4)
    assert not (session.directory / "files").exists()


def test_chunks_use_bounded_reads_and_replays_do_not_append_twice(session):
    content = b"abcd" * (1024**2 + 2)
    prepare(session, [("nested/image.dcm", len(content))])

    class Reader(io.BytesIO):
        def read(self, count=-1):
            assert 0 < count <= 1024**2
            return super().read(count)

    assert session.write_chunk(index=0, offset=0, length=len(content), stream=Reader(content)) == {
        "offset": len(content)
    }
    assert send(session, content) == {"offset": len(content)}
    assert payload_path(session).read_bytes() == content
    assert session.status(index=0)["offset"] == len(content)
    with pytest.raises(UploadConflict, match="differs"):
        send(session, b"wrong")
    assert payload_path(session).read_bytes() == content


def test_interrupted_partial_chunk_can_be_retried_without_corruption(session):
    prepare(session, [("x.dcm", 8)])
    with pytest.raises(UploadError, match="interrupted"):
        send(session, b"abcd", length=8)
    assert session.status(index=0)["offset"] == 4
    assert send(session, b"abcdefgh") == {"offset": 8}
    assert payload_path(session).read_bytes() == b"abcdefgh"


def test_wrong_offsets_and_oversize_chunks_do_not_write(session):
    prepare(session, [("x.dcm", CHUNK_SIZE + 1)])
    with pytest.raises(UploadConflict, match="starts after"):
        send(session, b"x", offset=1)
    with pytest.raises(UploadTooLarge):
        send(session, b"x", length=CHUNK_SIZE + 1)
    with pytest.raises(UploadError, match="declared file size"):
        send(session, b"x", offset=CHUNK_SIZE + 1)
    assert payload_path(session).stat().st_size == 0


def test_different_files_stream_concurrently_and_replayed_file_writes_serialize(session):
    prepare(session, [("a.dcm", 4), ("b.dcm", 4)])
    barrier = Barrier(2, timeout=5)

    class Reader(io.BytesIO):
        def read(self, count=-1):
            barrier.wait()
            return super().read(count)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(
                session.write_chunk, index=index, offset=0, length=4, stream=Reader(b"data")
            )
            for index in range(2)
        ]
        assert [result.result(timeout=10) for result in results] == [{"offset": 4}, {"offset": 4}]
        repeats = [pool.submit(send, session, b"data") for _ in range(2)]
        assert all(result.result(timeout=10) == {"offset": 4} for result in repeats)
    assert payload_path(session, 0).read_bytes() == payload_path(session, 1).read_bytes() == b"data"


def test_staged_source_uses_existing_files_and_preserves_dicom_archive_suffixes(session):
    prepare(session, [("nested/no-preamble.dcm", 4), ("nested/archive.tar.gz", 0)])
    send(session, b"data")
    with session.locked() as data:
        session.verify_files(data)
        data.update(state="queued", rq_id="request")
        session.save(data)
        write_json(session.directory / SOURCE_NAME, SOURCE_MARKER)
    prefix, paths = read_staged_upload(session.directory / SOURCE_NAME, **session.owner)
    assert prefix == "synthetic"
    assert [path.name for path in paths] == ["00000000.dcm", "00000001.tar.gz"]
    assert paths[0].read_bytes() == b"data" and paths[1].stat().st_size == 0
    assert paths[0].is_relative_to(session.directory)
    with pytest.raises(MissingUpload):
        read_staged_upload(
            session.directory / SOURCE_NAME, user_id=8, project_id=11, organization_id=None
        )
    with pytest.raises(MissingUpload):
        read_staged_upload(
            session.directory / SOURCE_NAME, user_id=7, project_id=12, organization_id=None
        )
    with pytest.raises(MissingUpload):
        read_staged_upload(
            session.directory / SOURCE_NAME, user_id=7, project_id=11, organization_id=3
        )


def test_submitted_session_rejects_new_bytes_and_registration(session):
    prepare(session, [("x.dcm", 4)])
    send(session, b"data")
    with session.locked() as data:
        data.update(state="queued", rq_id="request")
        session.save(data)
    with pytest.raises(UploadConflict, match="already been submitted"):
        send(session, b"data")
    with pytest.raises(UploadConflict, match="already been submitted"):
        session.register(start_index=0, files=[{"path": "x.dcm", "size": 4}])
    assert payload_path(session).read_bytes() == b"data"


def test_manifest_cannot_name_an_arbitrary_local_file(tmp_path):
    source = tmp_path / SOURCE_NAME
    source.write_text(json.dumps(SOURCE_MARKER))
    with pytest.raises(UploadError, match="Invalid staged"):
        read_staged_upload(source, user_id=7, project_id=11, organization_id=None)


def test_foreign_user_session_id_cannot_resolve_its_directory(session):
    prepare(session, [("x.dcm", 0)])
    foreign = UploadSession.for_project(
        session.directory.parent,
        session.directory.name.removeprefix("dicom-upload-7-"),
        user_id=8,
        project_id=11,
        organization_id=None,
    )
    with pytest.raises(MissingUpload):
        foreign.status()


def test_active_session_explicitly_refreshes_access_time(session):
    prepare(session, [("x.dcm", 4)])
    os.utime(session.directory, (1, 1))
    send(session, b"data")
    assert session.directory.stat().st_atime > 1
