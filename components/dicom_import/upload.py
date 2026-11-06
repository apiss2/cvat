# SPDX-License-Identifier: MIT
"""DICOM folder transfer through the native project permission and import queue."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import time
from pathlib import Path
from uuid import UUID

import rq
from django.conf import settings
from django.dispatch import receiver
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import BaseParser
from rest_framework.response import Response
from rq.job import JobStatus

from cvat.apps.engine.background import DatasetImporter
from cvat.apps.engine.models import RequestAction, RequestSubresource, RequestTarget
from cvat.apps.engine.rq import ImportRequestId, ImportRQMeta
from cvat.apps.redis_handler.signals import request_failed, request_succeeded

from . import DicomImportError
from .settings import limits
from .storage import (
    SESSION_PATTERN,
    SOURCE_MARKER,
    SOURCE_NAME,
    MissingUpload,
    UploadConflict,
    UploadError,
    UploadSession,
    session_lock,
    write_json,
)

logger = logging.getLogger(__name__)
ACTIVE_STATES = {JobStatus.QUEUED, JobStatus.STARTED, JobStatus.DEFERRED, JobStatus.SCHEDULED}


class DicomChunkParser(BaseParser):
    """Leave the bounded raw body for streaming after project authorization.

    ProjectPermission inspects request.data before dispatch. Returning an empty
    mapping here allows that existing check without materializing the file body.
    Other upload requests continue to use CVAT's ordinary parsers.
    """

    media_type = "application/octet-stream"

    def parse(self, stream, media_type=None, parser_context=None):
        return {}


def _job_matches(job, source):
    return bool(
        job
        and job.func_name == f"{__name__}.import_staged_dataset"
        and job.args
        and Path(job.args[0]) == source
    )


class DicomFolderImporter(DatasetImporter):
    def __init__(self, *, request, db_instance, session):
        super().__init__(request=request, db_instance=db_instance)
        self.session = session

    @property
    def job_retry(self):
        # Keep one acceptance per session. A terminal receipt never starts again.
        return None

    def init_callback_with_params(self):
        self.callback = import_staged_dataset
        self.callback_args = (
            str(self.session.directory / SOURCE_NAME),
            self.db_instance.pk,
            self.import_args.format,
            self.import_args.conv_mask_to_poly,
        )

    def handle_existing_job(self, job, queue):
        if _job_matches(job, self.session.directory / SOURCE_NAME):
            return self.get_response(job.id)
        return super().handle_existing_job(job, queue)

    def setup_new_job(self, queue, request_id, /, **kwargs):
        super().setup_new_job(queue, request_id, timeout=limits()["import_timeout"], **kwargs)

    def build_request_id(self):
        # A project ID is reused by ordinary dataset imports. An immutable
        # operation ID keeps an old upload receipt tied to its own Requests row.
        return ImportRequestId(
            action=RequestAction.IMPORT,
            target=RequestTarget.PROJECT,
            subresource=RequestSubresource.DATASET,
            id=UUID(self.session.read()["operation_id"]),
            user_id=self.user_id,
        ).render()


def _terminal_cleanup(session, state):
    try:
        with session.locked() as data:
            session.remove_payload()
            data["state"] = state
            session.save(data)
    except Exception:
        # The native temporary cleanup retries this. A cleanup failure must not
        # turn a committed project import into a failed operation.
        logger.exception("Could not clean up staged DICOM import data")


def import_staged_dataset(source, project_id, format_name, conv_mask_to_poly):
    from cvat.apps.dataset_manager.project import import_dataset_as_project

    job = rq.get_current_job()
    if job is None:
        raise DicomImportError("DICOM folder import requires the native import queue")
    meta = ImportRQMeta.for_job(job)
    session = UploadSession(
        Path(source).parent,
        user_id=meta.user.id if meta.user else None,
        project_id=project_id,
        organization_id=meta.org_id,
    )
    try:
        with session.locked() as data:
            if data["state"] not in ("submitting", "queued") or data.get("rq_id") != job.id:
                raise DicomImportError("This DICOM upload was not queued for this request")
        import_dataset_as_project(source, project_id, format_name, conv_mask_to_poly)
    except BaseException:
        _terminal_cleanup(session, "failed")
        raise
    else:
        _terminal_cleanup(session, "finished")


@receiver(request_succeeded, sender=DicomFolderImporter)
@receiver(request_failed, sender=DicomFolderImporter)
def remove_staged_import_files(sender, rq_job, status, **kwargs):
    """Also runs when RQ reports a worker failure outside the import callback."""
    if not rq_job.args:
        return
    source = Path(rq_job.args[0])
    if source.name != SOURCE_NAME or not SESSION_PATTERN.fullmatch(source.parent.name):
        return
    meta = ImportRQMeta.for_job(rq_job)
    session = UploadSession(
        source.parent,
        user_id=meta.user.id if meta.user else None,
        project_id=rq_job.args[1],
        organization_id=meta.org_id,
    )
    _terminal_cleanup(session, "finished" if status == JobStatus.FINISHED else "failed")


def _resolve_submission(session, data):
    """Recover a lost enqueue response before accepting another finish request."""
    if data["state"] != "submitting":
        return
    job = DicomFolderImporter.get_queue().fetch_job(data["rq_id"])
    if _job_matches(job, session.directory / SOURCE_NAME):
        data["state"] = "queued"
    else:
        data["state"] = "uploading"
        data.pop("rq_id", None)
    session.save(data)


def _finish(request, project, session):
    with session.locked() as data:
        _resolve_submission(session, data)
        if data["state"] in ("queued", "finished", "failed"):
            return Response({"rq_id": data["rq_id"]}, status=202)
        if data["state"] != "uploading":
            raise UploadConflict("This DICOM upload cannot be submitted")
        session.verify_files(data)
        importer = DicomFolderImporter(request=request, db_instance=project, session=session)
        request_id = importer.build_request_id()
        write_json(session.directory / SOURCE_NAME, SOURCE_MARKER)
        data.update(state="submitting", rq_id=request_id)
        session.save(data)
        response = importer.enqueue_job()
        if response.status_code == 202:
            data["state"] = "queued"
        else:
            data["state"] = "uploading"
            data.pop("rq_id", None)
        session.save(data)
        return response


def _status(session, index):
    with session.locked() as data:
        _resolve_submission(session, data)
    return Response(session.status(index=index))


def _cancel(session):
    with session.locked() as data:
        _resolve_submission(session, data)
        if data["state"] not in ("uploading", "cancelled"):
            raise UploadConflict("The import was accepted; use Requests to inspect the operation")
        session.remove_payload()
        data["state"] = "cancelled"
        session.save(data)
    return Response(status=204)


def _query_int(request, name):
    value = request.query_params.get(name, "")
    if not value.isascii() or not value.isdecimal():
        raise UploadError(f"Invalid {name}")
    return int(value)


def _dispatch(request, project):
    if os.environ.get("CVAT_DICOM_IMPORT_ENABLED") != "1":
        raise NotFound("DICOM import is disabled")
    if (
        request.method != "POST"
        or request.query_params.get("format") != "DICOM 1.0"
        or request.query_params.get("location", "local") != "local"
        or request.query_params.get("filename")
        or "cloud_storage_id" in request.query_params
    ):
        raise UploadError("Folder transfer requires a local DICOM project import")
    operation = request.query_params["dicom_upload"]
    if operation == "chunk":
        if request.content_type != "application/octet-stream":
            raise UploadError("DICOM file chunks must use application/octet-stream")
        upload_id = request.query_params.get("upload_id")
        body = {}
    else:
        if request.content_type != "application/json" or not isinstance(request.data, dict):
            raise UploadError("DICOM upload metadata must be a JSON object")
        body = request.data
        upload_id = body.get("upload_id")
    session = UploadSession.for_project(
        Path(project.get_tmp_dirname()),
        upload_id,
        user_id=request.user.id,
        project_id=project.id,
        organization_id=project.organization_id,
    )
    if operation == "start":
        if getattr(settings, "DEFAULT_BACKING_CS_ID", None):
            raise UploadError("DICOM folder import requires local media storage")
        return Response(
            session.start(
                prefix=body.get("prefix"),
                file_count=body.get("file_count"),
                total_bytes=body.get("total_bytes"),
            ),
            status=201,
        )
    if operation == "files":
        return Response(
            session.register(start_index=body.get("start_index"), files=body.get("files"))
        )
    if operation == "chunk":
        length = request.META.get("CONTENT_LENGTH", "")
        if not isinstance(length, str) or not length.isascii() or not length.isdecimal():
            raise UploadError("A valid Content-Length header is required")
        return Response(
            session.write_chunk(
                index=_query_int(request, "file_index"),
                offset=_query_int(request, "offset"),
                length=int(length),
                stream=request.stream,
            )
        )
    if operation == "finish":
        return _finish(request, project, session)
    if operation == "status":
        return _status(session, body.get("file_index"))
    if operation == "cancel":
        return _cancel(session)
    raise UploadError("Unknown DICOM upload operation")


def handle_upload(request, project):
    try:
        response = _dispatch(request, project)
    except UploadError as exc:
        response = Response({"detail": str(exc)}, status=exc.status_code)
    except DicomImportError as exc:
        raise ValidationError(str(exc)) from None
    except OSError as exc:
        if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
            raise
        response = Response({"detail": "Insufficient disk space for DICOM upload"}, status=507)
    response["Cache-Control"] = "no-store"
    return response


def cleanup_stale_upload(path: Path, *, retention_seconds: float):
    """Participate in native temporary cleanup without deleting live imports."""
    if not SESSION_PATTERN.fullmatch(path.name):
        return None
    try:
        with session_lock(path, blocking=False):
            if path.stat().st_atime + retention_seconds >= time.time():
                return False
            from .storage import _read_json

            try:
                data = _read_json(path / "session.json")
            except UploadError:
                # Missing or unreadable metadata cannot describe a queued job.
                data = {}
            if not isinstance(data, dict):
                data = {}
            if data.get("state") in ("submitting", "queued") and data.get("rq_id"):
                job = DicomFolderImporter.get_queue().fetch_job(data["rq_id"])
                if (
                    _job_matches(job, path / SOURCE_NAME)
                    and job.get_status(refresh=False) in ACTIVE_STATES
                ):
                    return False
            shutil.rmtree(path)
            return True
    except BlockingIOError:
        return False
    except MissingUpload:
        # No lock file: start() was interrupted before creating the session.
        # Nothing can hold it, so the native access-time rule applies.
        return None
