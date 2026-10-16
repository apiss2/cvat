# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .codec import decode_image
from .cleanup import purge_deleted_files
from .config import Settings
from .packages import extract_package
from .runtime import Busy
from .schema import Manifest, function_metadata, parse_function_id
from .store import Conflict, Store
from .wire import validate_wire


class Gone(RuntimeError):
    pass


class Service:
    def __init__(self, settings: Settings, runtime):
        self.settings = settings
        self.runtime = runtime
        self.store = Store(settings.data_dir / "registry.sqlite3")
        if hasattr(runtime, "log_sink"):
            runtime.log_sink = lambda model, rev, rid, text: self.store.event(model, rev, rid, "worker", "stdout", "info", text)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="model-registration")
        self.pending = threading.BoundedSemaphore(settings.max_pending)
        self.mutations = threading.RLock()
        self.model_idle = threading.Condition(self.mutations)
        self.model_users: dict[str, int] = {}
        self.model_uploads: dict[str, set[Path]] = {}
        self.futures = set()
        self.futures_lock = threading.Lock()
        (settings.data_dir / "uploads").mkdir(parents=True, exist_ok=True)

    @contextmanager
    def using_model(self, model_id: str):
        with self.model_idle:
            if self.store.model(model_id)["deleted"]:
                raise Gone("Model deleted")
            self.model_users[model_id] = self.model_users.get(model_id, 0) + 1
        try:
            yield
        finally:
            self._release_model(model_id)

    def _release_model(self, model_id: str) -> None:
        with self.model_idle:
            self.model_users[model_id] -= 1
            if not self.model_users[model_id]:
                del self.model_users[model_id]
            self.model_idle.notify_all()

    def recover(self) -> None:
        """Called once under the manager lock after old worker containers are stopped."""
        import re
        self.store.recover()
        deleted = {model["id"] for model in self.store.models() if model["deleted"]}
        for model_id in deleted:
            try:
                self._cleanup_deleted(model_id)
            except Busy:
                # Keep the tombstone and error log; other models remain usable.
                pass
        for path in (self.settings.data_dir / "uploads").glob("package-*.zip"):
            path.unlink(missing_ok=True)
        for model_dir in (self.settings.data_dir / "packages").glob("*"):
            if model_dir.is_symlink() or model_dir.name in deleted or not re.fullmatch(r"[0-9a-f]{20}", model_dir.name):
                continue
            known = {r["revision"] for r in self.store.revisions(model_dir.name)}
            for revision_dir in model_dir.glob("*"):
                if not revision_dir.is_symlink() and re.fullmatch(r"[0-9a-f]{16}", revision_dir.name) and revision_dir.name not in known:
                    shutil.rmtree(revision_dir)

    def submit(self, archive: Path, owner: str, model_id: str | None = None, expected: str | None = None) -> dict:
        if not self.pending.acquire(blocking=False):
            raise Conflict("Registration queue is full; retry later")
        try:
            if shutil.disk_usage(self.settings.data_dir).free < 4 * 1024**3:
                raise Conflict("Less than 4 GiB free on the registry data volume")
            total = 0
            for path in self.settings.data_dir.glob("packages/*/*/*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except FileNotFoundError:
                    pass  # A concurrent deletion may remove a scanned file.
            if total > self.settings.max_storage_bytes - 3 * 1024**3:
                raise Conflict("Model storage quota reached; delete unused models or retry failed file cleanup")
            with self.mutations:
                if model_id is None:
                    model_id = uuid.uuid4().hex[:20]
                    self.store.create_model(model_id, owner)
                else:
                    record = self.store.model(model_id)
                    if record["deleted"]:
                        raise Gone("This model has been deleted")
                    if record["active_revision"] != expected:
                        raise Conflict("Model changed; refresh before uploading an update")
                revision = uuid.uuid4().hex[:16]
                operation = uuid.uuid4().hex
                self.store.new_operation(operation, model_id, revision, owner)
                # Reserve before scheduling: deletion also waits for queued uploads.
                self.model_users[model_id] = self.model_users.get(model_id, 0) + 1
                self.model_uploads.setdefault(model_id, set()).add(archive)
                try:
                    future = self.executor.submit(self._register, archive, owner, model_id, revision, expected, operation)
                except BaseException:
                    self._release_model(model_id)
                    raise
                with self.futures_lock:
                    self.futures.add(future)
                future.add_done_callback(self._finished)
                return self.store.operation(operation)
        except BaseException:
            self.pending.release()
            raise

    def _finished(self, future):
        with self.futures_lock:
            self.futures.discard(future)

    def _register(self, archive: Path, actor: str, model_id: str, revision: str, expected: str | None, operation: str) -> None:
        destination = self.settings.data_dir / "packages" / model_id / revision
        committed = False
        try:
            if self.store.model(model_id)["deleted"]:
                raise Gone("Model deleted before registration started")
            self.store.update_operation(operation, "validating")
            self.store.event(model_id, revision, operation, actor, "package", "info", "Validating archive and manifest")
            manifest, digest = extract_package(archive, destination)
            self.store.event(model_id, revision, operation, actor, "load", "info", "Loading ONNX models in isolated worker")
            sample = next(destination.glob("sample.*")).read_bytes()
            payload = {"image": base64.b64encode(sample).decode()}
            result = self.runtime.call(model_id, revision, destination, payload, operation)
            image = decode_image(payload["image"])
            validate_wire(result, manifest, image.shape[1], image.shape[0])
            with self.mutations:
                self.store.commit_revision(model_id, revision, expected, manifest.model_dump(), digest, str(destination.relative_to(self.settings.data_dir)))
                committed = True
            self.store.event(model_id, revision, operation, actor, "publish", "info", f"Published revision; sample objects={len(result)}, sha256={digest}")
            self.store.update_operation(operation, "succeeded", f"Sample inference succeeded with {len(result)} objects")
        except Exception:
            detail = traceback.format_exc()[-20000:]
            self.store.event(model_id, revision, operation, actor, "registration", "error", detail)
            self.store.update_operation(operation, "succeeded" if committed else "failed", "Revision was committed; inspect logs" if committed else detail)
        finally:
            try:
                if not committed:
                    try:
                        self.runtime.discard(model_id, revision)
                    except Exception as exc:
                        self.store.event(model_id, revision, operation, actor, "cleanup", "error", str(exc))
                    shutil.rmtree(destination, ignore_errors=True)
                archive.unlink(missing_ok=True)
                with self.mutations:
                    self.model_uploads[model_id].discard(archive)
                    if not self.model_uploads[model_id]:
                        del self.model_uploads[model_id]
            finally:
                self.pending.release()
                self._release_model(model_id)

    def describe(self, model_id: str) -> dict:
        model = self.store.model(model_id)
        revisions = [
            {**record, "manifest": Manifest.model_validate(record["manifest"]).model_dump()}
            for record in self.store.revisions(model_id)
        ]
        active = next((r for r in revisions if r["revision"] == model["active_revision"]), None)
        return {
            **model,
            "manifest": active["manifest"] if active else None,
            "revisions": [{k: r[k] for k in ("revision", "manifest", "digest", "created_at")} for r in revisions],
            "operations": self.store.operations(model_id),
        }

    def functions(self) -> dict:
        result = {}
        for model in self.store.models():
            if model["deleted"] or not model["active_revision"]:
                continue
            try:
                rev = self.store.revision(model["id"], model["active_revision"])
            except KeyError:
                continue
            meta = function_metadata(model["id"], rev["revision"], rev["manifest"], self.settings.namespace)
            result[meta["metadata"]["name"]] = meta
        return result

    def function(self, identifier: str) -> dict:
        model_id, revision = parse_function_id(identifier)
        model = self.store.model(model_id)
        if model["deleted"]:
            raise Gone("Model deleted; all its revisions are disabled")
        record = self.store.revision(model_id, revision)
        # Old revisions stay addressable for already queued multi-frame CVAT tasks.
        return function_metadata(model_id, revision, record["manifest"], self.settings.namespace)

    def infer(self, identifier: str, payload: dict, request_id: str, actor: str = "cvat") -> list:
        model_id, revision = parse_function_id(identifier)
        with self.using_model(model_id):
            self.function(identifier)
            record = self.store.revision(model_id, revision)
            manifest = Manifest.model_validate(record["manifest"])
            self.store.event(model_id, revision, request_id, actor, "predict", "info", "Inference accepted")
            try:
                image = decode_image(payload["image"])
                result = self.runtime.call(model_id, revision, self.settings.data_dir / record["path"], {"image": payload["image"]}, request_id)
                output = validate_wire(result, manifest, image.shape[1], image.shape[0])
                self.store.event(model_id, revision, request_id, actor, "predict", "info", f"Completed; objects={len(output)}")
                return output
            except Exception:
                self.store.event(model_id, revision, request_id, actor, "predict", "error", traceback.format_exc())
                raise

    def delete(self, model_id: str, expected: str | None, actor: str) -> None:
        with self.model_idle:
            self.store.delete(model_id, expected, actor)
        self._cleanup_deleted(model_id)

    def _cleanup_deleted(self, model_id: str) -> None:
        with self.model_idle:
            self.model_idle.wait_for(lambda: model_id not in self.model_users)
            try:
                # No registered reader/writer can create or use files after this point.
                self.runtime.revoke(model_id)
                uploads = self.settings.data_dir.resolve() / "uploads"
                for archive in self.model_uploads.get(model_id, ()):
                    if uploads.is_symlink() or archive.parent.resolve() != uploads:
                        raise ValueError("Invalid model upload cleanup path")
                    archive.unlink(missing_ok=True)
                self.model_uploads.pop(model_id, None)
                purge_deleted_files(self.settings.data_dir, model_id)
            except Exception as exc:
                self.store.event(model_id, "", "", "manager", "cleanup", "error", traceback.format_exc())
                raise Busy("Model is disabled, but file cleanup failed. Check model logs and retry deletion.") from exc

    def rollback(self, model_id: str, revision: str, expected: str, actor: str) -> None:
        with self.using_model(model_id):
            record = self.store.revision(model_id, revision)
            directory = self.settings.data_dir / record["path"]
            sample = next(directory.glob("sample.*")).read_bytes()
            identifier = function_metadata(model_id, revision, record["manifest"], self.settings.namespace)["metadata"]["name"]
            self.infer(identifier, {"image": base64.b64encode(sample).decode()}, uuid.uuid4().hex, actor)
            with self.mutations:
                self.store.activate_existing(model_id, revision, expected)
            self.store.event(model_id, revision, "", actor, "rollback", "info", "Activated previously tested revision")

    def close(self):
        self.executor.shutdown(wait=True)
        self.runtime.close()
