# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import fcntl
import json
import re
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .auth import Auth, owner
from .config import Settings
from .http_limits import GuardMiddleware
from .packages import MAX_ARCHIVE
from .partial_updates import install_partial_updates
from .runtime import Busy, DockerRuntime, RuntimeFailure
from .schema import InvokeRequest, Manifest, function_id
from .service import Gone, Service
from .store import Conflict


class RevisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: str | None
    revision: str | None = None


def create_app(settings: Settings | None = None, runtime=None, service: Service | None = None, *, reconcile: bool = True) -> FastAPI:
    settings = settings or Settings()
    settings.validate()
    service = service or Service(settings, runtime or DockerRuntime(settings))
    auth = Auth(settings)

    @asynccontextmanager
    async def lifespan(app):
        lock_file = (settings.data_dir / "manager.lock").open("a+")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            raise RuntimeError("Only one manager process may use this data directory") from exc
        try:
            if reconcile:
                service.runtime.reconcile()
            service.recover()
            yield
        finally:
            service.close()
            lock_file.close()

    app = FastAPI(title="CVAT model registry", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.auth = auth
    app.add_middleware(GuardMiddleware, auth=auth)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "Model, revision or operation not found"}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(Gone)
    async def gone(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=410)

    @app.exception_handler(Busy)
    async def busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "2"})

    @app.exception_handler(RuntimeFailure)
    async def runtime_error(request, exc):
        # Full tracebacks are in owner-protected logs, not the CVAT-facing service API.
        return JSONResponse({"detail": "Inference failed. Check model logs in the registry."}, status_code=502)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)[:4000]}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default errors may echo the entire base64 image. Never do that.
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    static_dir = Path(__file__).resolve().parent.parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html", headers={"Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/me")
    def me(request: Request):
        user = request.state.user
        return {"id": user.cvat_id, "name": user.name, "admin": user.admin, "auth_mode": settings.auth_mode}

    def describe_visible(model: dict, user):
        can_manage = user.admin or model["owner"] == user.name
        if can_manage:
            return {**service.describe(model["id"]), "can_manage": True}
        if model["deleted"] or not model["active_revision"]:
            raise HTTPException(404, "Published model not found")
        revision = service.store.revision(model["id"], model["active_revision"])
        # Build public responses from an allowlist. Operation failure details,
        # logs, package paths and historical revisions stay owner/admin only.
        return {**{key: model[key] for key in ("id", "owner", "active_revision", "deleted", "created_at")},
                "manifest": Manifest.model_validate(revision["manifest"]).model_dump(), "can_manage": False}

    @app.get("/api/models")
    def list_models(request: Request):
        user = request.state.user
        return [describe_visible(model, user) for model in service.store.models()
                if user.admin or model["owner"] == user.name or (not model["deleted"] and model["active_revision"])]

    @app.get("/api/models/{model_id}")
    def get_model(model_id: str, request: Request):
        return describe_visible(service.store.model(model_id), request.state.user)

    def check_update(request, model_id, expected):
        if model_id:
            record = service.store.model(model_id)
            owner(request.state.user, record)
            if record["deleted"]:
                raise Gone("Model deleted")
            if record["active_revision"] != (expected or None):
                raise Conflict("Model changed; refresh before updating")

    def save_upload(src: UploadFile, dst, limit: int) -> int:
        count = 0
        while chunk := src.file.read(1024**2):
            count += len(chunk)
            if count > limit:
                raise HTTPException(413, "Uploaded file exceeds byte limit")
            dst.write(chunk)
        return count

    def temporary_archive() -> Path:
        fd, name = tempfile.mkstemp(prefix="package-", suffix=".zip", dir=settings.data_dir / "uploads")
        import os
        os.close(fd)
        return Path(name)

    @app.post("/api/upload", status_code=202)
    def upload_files(request: Request, manifest: str = Form(...), code: UploadFile = File(...), weights: list[UploadFile] = File(...), sample: UploadFile = File(...), model_id: str = Form(""), expected_revision: str = Form("")):
        check_update(request, model_id, expected_revision)
        if len(manifest) > 256 * 1024:
            raise HTTPException(413, "Manifest too large")
        parsed = Manifest.model_validate_json(manifest)
        names = [item.filename for item in weights]
        if len(names) != len(set(names)) or set(names) != set(parsed.weights):
            raise HTTPException(422, "Weight filenames must match manifest.weights exactly")
        # Avoid all client-supplied pathnames, including paths in multipart filenames.
        if sample.filename is None or Path(sample.filename).suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise HTTPException(422, "Sample must be PNG or JPEG")
        sample_name = "sample.png" if sample.filename.lower().endswith(".png") else "sample.jpg"
        path = temporary_archive()
        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("manifest.json", parsed.model_dump_json(indent=2))
                with archive.open("model.py", "w") as dst:
                    total = save_upload(code, dst, 1024**2)
                with archive.open(sample_name, "w") as dst:
                    total += save_upload(sample, dst, 32 * 1024**2)
                for weight in weights:
                    with archive.open(weight.filename, "w", force_zip64=True) as dst:
                        total += save_upload(weight, dst, MAX_ARCHIVE - total)
            return service.submit(path, request.state.user.name, model_id or None, expected_revision or None)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @app.get("/api/operations/{operation_id}")
    def operation(operation_id: str, request: Request):
        op = service.store.operation(operation_id)
        owner(request.state.user, op)
        return op

    @app.get("/api/models/{model_id}/logs")
    def logs(model_id: str, request: Request, after: int = 0):
        owner(request.state.user, service.store.model(model_id))
        return service.store.events(model_id, max(0, after))

    @app.get("/api/models/{model_id}/logs.txt")
    def logs_text(model_id: str, request: Request):
        owner(request.state.user, service.store.model(model_id))
        text = "\n".join(json.dumps(x, ensure_ascii=False) for x in service.store.events(model_id))
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="model-{model_id}-logs.jsonl"'})

    @app.post("/api/models/{model_id}/test")
    def test(model_id: str, body: InvokeRequest, request: Request, revision: str | None = None):
        model = service.store.model(model_id)
        owner(request.state.user, model)
        rev = revision or model["active_revision"]
        if not rev:
            raise HTTPException(409, "Model has no published revision")
        rid = uuid.uuid4().hex
        result = service.infer(function_id(model_id, rev), body.model_dump(exclude_none=True), rid, request.state.user.name)
        return {"request_id": rid, "revision": rev, "results": result}

    @app.delete("/api/models/{model_id}")
    def delete(model_id: str, body: RevisionAction, request: Request):
        owner(request.state.user, service.store.model(model_id))
        service.delete(model_id, body.expected_revision, request.state.user.name)
        return {"deleted": True}

    @app.post("/api/models/{model_id}/rollback")
    def rollback(model_id: str, body: RevisionAction, request: Request):
        owner(request.state.user, service.store.model(model_id))
        if not body.revision or not body.expected_revision:
            raise HTTPException(422, "revision and expected_revision are required")
        service.rollback(model_id, body.revision, body.expected_revision, request.state.user.name)
        return describe_visible(service.store.model(model_id), request.state.user)

    @app.get("/internal/functions")
    def functions():
        return service.functions()

    @app.get("/internal/functions/{identifier}")
    def function(identifier: str):
        return service.function(identifier)

    @app.post("/internal/functions/{identifier}/invoke")
    def invoke(identifier: str, body: InvokeRequest, request: Request):
        supplied = request.headers.get("x-request-id", "")
        rid = supplied if re.fullmatch(r"[0-9a-f]{32}", supplied) else uuid.uuid4().hex
        result = service.infer(identifier, body.model_dump(exclude_none=True), rid)
        return JSONResponse(result, headers={"X-Request-ID": rid})

    install_partial_updates(app, service, settings)
    return app
