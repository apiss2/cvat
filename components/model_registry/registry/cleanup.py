# SPDX-License-Identifier: MIT
"""File cleanup shared by the manager and the stopped-manager recovery command."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .store import Store


def purge_deleted_files(data_dir: Path, model_id: str, actor: str = "manager") -> None:
    if not re.fullmatch(r"[0-9a-f]{20}", model_id):
        raise ValueError("Invalid model ID")
    root = data_dir.resolve()
    if not (root / "registry.sqlite3").is_file():
        raise ValueError("Registry database does not exist")
    store = Store(root / "registry.sqlite3")
    if not store.model(model_id)["deleted"]:
        raise ValueError("Only deleted models may be purged")
    for name in ("packages", "run", "uploads"):
        parent = root / name
        if parent.is_symlink():
            raise ValueError("Model storage must not be a symbolic link")
        if name == "uploads":
            for archive in parent.glob(f"package-{model_id}-*.zip"):
                archive.unlink()
            continue
        directories = [parent / model_id] if name == "packages" else list(parent.glob(model_id + "-*"))
        for directory in directories:
            if directory.is_symlink() or not directory.resolve().is_relative_to(parent):
                raise ValueError("Invalid model cleanup path")
            if directory.exists():
                shutil.rmtree(directory)
    store.prune_deleted(model_id)
    store.event(model_id, "", "", actor, "cleanup", "info", "All model files and revision/operation metadata removed")
