# SPDX-License-Identifier: MIT
"""Host-side model storage shared by cvatctl and standalone registryctl."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

DIRECTORY = "cvat-model-registry"
DOCKER_EXCLUDE = f"/{DIRECTORY}/"


def atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def default_home(repo_root: Path) -> Path:
    return repo_root.resolve() / DIRECTORY


def storage_paths(
    repo_root: Path, home: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Resolve paths without creating directories or changing configuration."""
    root = repo_root.resolve()
    requested = (home or default_home(root)).expanduser()
    resolved = requested.resolve()
    if root == resolved or root in resolved.parents:
        if resolved != default_home(root):
            raise ValueError(
                f"Storage inside the repository must use {default_home(root)}; "
                "an explicit path outside the repository is also supported"
            )
    # A Git ignore file in a symlink target cannot hide the symlink itself.
    if requested.is_symlink() and requested.parent.resolve() == root:
        raise ValueError("Use a real cvat-model-registry directory or an external storage path")
    return resolved, resolved / "config", resolved / "data"


def verify_build_exclusion(repo_root: Path, home: Path) -> None:
    """Check the effective ignore files before placing data in a build context."""
    root = repo_root.resolve()
    if home != default_home(root):
        return
    # Dockerfile-specific ignore files take precedence over the root file.
    # Requiring the exclusion last also prevents a later ! rule exposing secrets.
    for dockerfile in ("Dockerfile", "Dockerfile.ui"):
        specific = root / f"{dockerfile}.dockerignore"
        ignore = specific if specific.is_file() else root / ".dockerignore"
        rules = [
            line.strip() for line in ignore.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ] if ignore.is_file() else []
        if not rules or rules[-1] != DOCKER_EXCLUDE:
            raise ValueError(
                f"Add {DOCKER_EXCLUDE} as the last rule in {ignore} before using "
                "repository-local model storage. Keep the existing ignore rules."
            )


def prepare_storage(
    repo_root: Path, home: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Create missing state, keeping existing data and the service token intact."""
    home, config, data = storage_paths(repo_root, home)
    verify_build_exclusion(repo_root, home)
    for path, mode in (
        (home, 0o700), (config, 0o700), (config / "secrets", 0o755),
        (data, 0o700), (data / "uploads", 0o700),
    ):
        path.mkdir(parents=True, exist_ok=True, mode=mode)
    ignore = home / ".gitignore"
    if ignore.is_symlink():
        raise ValueError(f"{ignore} must not be a symbolic link")
    if not ignore.is_file() or ignore.read_text() != "*\n":
        atomic(ignore, "*\n", mode=0o600)
    token = config / "secrets/service_token"
    if token.is_symlink() or (token.exists() and not token.is_file()):
        raise ValueError(f"{token} must be a regular file")
    # O_EXCL prevents concurrent initializers from replacing each other's token.
    # Outer directories are private; the bind-mounted token must be readable by
    # the gateway's non-root UID inside its isolated mount.
    try:
        descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        pass
    else:
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return home, config, data
