# SPDX-License-Identifier: MIT
"""Copy-on-write model updates; unchanged files never leave the registry volume."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import tempfile
import zipfile
from pathlib import Path

from fastapi import File, Form, HTTPException, Request, UploadFile

from .auth import owner
from .packages import MAX_ARCHIVE, MAX_EXPANDED, MAX_FILES
from .schema import Manifest
from .service import Gone
from .store import Conflict


def merge_manifest(previous: dict, patch: dict) -> Manifest:
    """Omission retains a value. Empty strings/lists are explicit replacements."""
    if not isinstance(patch, dict):
        raise ValueError("Manifest update must be a JSON object")
    if "weights" in patch and patch["weights"] != previous["weights"]:
        raise ValueError("Weight filenames cannot change during a partial update")
    merged = {**previous, **patch}
    if isinstance(patch.get("polygon"), dict):
        merged["polygon"] = {**previous.get("polygon", {}), **patch["polygon"]}
    return Manifest.model_validate(merged)


def assemble_update(destination: Path, source: Path, manifest: Manifest,
                    code: UploadFile | None, weights: list[UploadFile],
                    sample: UploadFile | None) -> None:
    """Build a complete candidate without writing to, linking or importing old files."""
    names = [item.filename for item in weights]
    if len(names) != len(set(names)) or any(name not in manifest.weights for name in names):
        raise ValueError("Replacement weight filenames must be distinct names in manifest.weights")
    replacements = dict(zip(names, weights))
    if code is not None:
        if code.filename != "model.py":
            raise ValueError("Replacement code filename must be model.py")
        replacements["model.py"] = code
    sample_name = None
    if sample is not None:
        suffix = Path(sample.filename or "").suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg"):
            raise ValueError("Sample must be PNG or JPEG")
        sample_name = "sample.png" if suffix == ".png" else "sample.jpg"
        replacements[sample_name] = sample

    # Packages can include Python helper modules in addition to model.py.
    existing = {}
    for entry in source.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise ValueError("Stored package contains a link or special file")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", entry.name):
            raise ValueError("Stored package contains an invalid filename")
        if entry.name.endswith(".py") or (entry.name in ("sample.png", "sample.jpg") and sample is None):
            existing[entry.name] = entry
    for name in manifest.weights:
        if name not in replacements:
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Upload the new weight file: {name}")
            existing[name] = path
    files = {**existing, **replacements}
    if "model.py" not in files or len(set(files) & {"sample.png", "sample.jpg"}) != 1:
        raise ValueError("Update must retain or supply model.py and exactly one sample image")
    if len(files) + 1 > MAX_FILES:
        raise ValueError("Too many files in updated package")
    serialized = manifest.model_dump_json(indent=2).encode()
    if len(serialized) > 256 * 1024:
        raise ValueError("Manifest exceeds 256 KiB")
    total = len(serialized)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", serialized)
        for name, item in sorted(files.items()):
            limit = 1024**2 if name.endswith(".py") else 32 * 1024**2 if name.startswith("sample.") else MAX_EXPANDED
            stream = item.open("rb") if isinstance(item, Path) else item.file
            try:
                count = 0
                with archive.open(name, "w", force_zip64=True) as output:
                    while chunk := stream.read(1024**2):
                        count += len(chunk)
                        total += len(chunk)
                        if count > limit or total > MAX_EXPANDED:
                            raise HTTPException(413, "Updated package exceeds its byte limit")
                        output.write(chunk)
                        # Include ZIP headers and avoid filling disk with inherited weights.
                        if archive.fp.tell() > MAX_ARCHIVE:
                            raise HTTPException(413, "Updated package exceeds 2 GiB")
            finally:
                if isinstance(item, Path):
                    stream.close()
    if destination.stat().st_size > MAX_ARCHIVE:
        raise HTTPException(413, "Updated package exceeds 2 GiB")


def install_partial_updates(app, service, settings) -> None:
    assembly_slots = threading.BoundedSemaphore(2)

    @app.post("/api/models/{model_id}/update", status_code=202)
    def update_model(
        model_id: str, request: Request, expected_revision: str = Form(...),
        manifest: str | None = Form(None), code: UploadFile | None = File(None),
        weights: list[UploadFile] | None = File(None), sample: UploadFile | None = File(None),
    ):
        record = service.store.model(model_id)
        owner(request.state.user, record)
        if record["deleted"]:
            raise Gone("Model deleted")
        if not expected_revision or record["active_revision"] != expected_revision:
            raise Conflict("Model changed; refresh before updating")
        previous = service.store.revision(model_id, expected_revision)
        if manifest is not None and len(manifest.encode()) > 256 * 1024:
            raise HTTPException(413, "Manifest too large")
        parsed = merge_manifest(previous["manifest"], json.loads(manifest) if manifest else {})
        if not code and not weights and not sample and parsed.model_dump() == Manifest.model_validate(previous["manifest"]).model_dump():
            raise ValueError("No files or model settings were changed")
        # Only server-owned revision paths are used; a request can never select a path.
        root = settings.data_dir.resolve()
        source = root / previous["path"]
        if source.is_symlink() or not source.resolve().is_relative_to(root / "packages"):
            raise ValueError("Invalid stored package path")
        estimated = sum(p.stat().st_size for p in source.iterdir() if p.is_file())
        if shutil.disk_usage(root).free < estimated + 4 * 1024**3:
            raise Conflict("Insufficient free space for a separate candidate revision")
        if not assembly_slots.acquire(blocking=False):
            raise Conflict("Other model updates are being assembled; retry later")
        try:
            fd, name = tempfile.mkstemp(prefix="package-", suffix=".zip", dir=root / "uploads")
        except BaseException:
            assembly_slots.release()
            raise
        os.close(fd)
        path = Path(name)
        try:
            assemble_update(path, source, parsed, code, weights or [], sample)
            # submit and commit_revision both check expected_revision. A concurrent
            # update/delete during copying or sample inference cannot be overwritten.
            return service.submit(path, request.state.user.name, model_id, expected_revision)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            assembly_slots.release()
