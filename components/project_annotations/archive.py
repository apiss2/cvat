# SPDX-License-Identifier: MIT
"""Validate uploaded paths before passing a bounded archive to native importers."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, is_zipfile

from . import ProjectAnnotationImportError

MAX_UPLOAD_BYTES = 512 * 1024**2
MAX_EXPANDED_BYTES = 2 * 1024**3
MAX_ENTRIES = 100_000


@dataclass
class Budget:
    bytes_left: int = MAX_EXPANDED_BYTES
    entries_left: int = MAX_ENTRIES


@dataclass
class FolderPlan:
    # The prefixes stay private: task names may contain patient identifiers.
    targets: dict[int, tuple[str, ...]] = field(default_factory=dict)
    failed_tasks: set[int] = field(default_factory=set)
    unmatched: int = 0


def _path(name):
    if (
        not name
        or "\\" in name
        or re.match(r"^[A-Za-z]:", name)
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
        or name.startswith("/")
        or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
    ):
        raise ProjectAnnotationImportError("ZIP contains an unsafe member path")
    try:
        name.encode("utf-8")
    except UnicodeError:
        raise ProjectAnnotationImportError("ZIP contains an invalid Unicode path") from None
    return PurePosixPath(name).parts


def inspect_zip(source, budget=None):
    """Check names, types, advertised sizes, actual expansion and CRCs without extracting."""
    budget = budget or Budget()
    source.seek(0, 2)
    if source.tell() > MAX_UPLOAD_BYTES:
        raise ProjectAnnotationImportError("ZIP exceeds the 512 MiB upload limit")
    source.seek(0)
    members = []
    try:
        with ZipFile(source) as archive:
            entries = archive.infolist()
            budget.entries_left -= len(entries)
            if budget.entries_left < 0:
                raise ProjectAnnotationImportError("ZIP exceeds the entry limit")
            seen, files = set(), set()
            for info in entries:
                parts = _path(info.orig_filename)
                if parts in seen:
                    raise ProjectAnnotationImportError("ZIP has duplicate member paths")
                seen.add(parts)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if (
                    info.flag_bits & 1
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (kind == stat.S_IFDIR and not info.is_dir())
                    or (kind == stat.S_IFREG and info.is_dir())
                ):
                    raise ProjectAnnotationImportError("ZIP contains encrypted or special entries")
                if info.is_dir():
                    if info.file_size:
                        raise ProjectAnnotationImportError("ZIP directory contains data")
                else:
                    files.add(parts)
                    budget.bytes_left -= info.file_size
                    if budget.bytes_left < 0:
                        raise ProjectAnnotationImportError("ZIP exceeds the expanded size limit")
                members.append((parts, info.is_dir()))
            if any(parts[:i] in files for parts in seen for i in range(1, len(parts))):
                raise ProjectAnnotationImportError("ZIP has conflicting files and directories")
            # Consume a bounded stream, so corrupt headers and CRCs fail before any task changes.
            for info in entries:
                if info.is_dir():
                    continue
                count = 0
                with archive.open(info) as stream:
                    while chunk := stream.read(min(1024**2, info.file_size - count + 1)):
                        count += len(chunk)
                        if count > info.file_size:
                            raise ProjectAnnotationImportError("ZIP has an oversized member")
                if count != info.file_size:
                    raise ProjectAnnotationImportError("ZIP has a truncated member")
    except ProjectAnnotationImportError:
        raise
    except (BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError, EOFError):
        raise ProjectAnnotationImportError("Invalid ZIP archive") from None
    finally:
        source.seek(0)
    if not any(not directory for _, directory in members):
        raise ProjectAnnotationImportError("ZIP does not contain annotation files")
    return members


def plan_folders(members, tasks):
    """Exact names take priority over removing one optional Windows ZIP parent."""
    names = {}
    for task_id, name in tasks:
        names.setdefault(name, []).append(task_id)
    if any(len(parts) == 1 and not directory for parts, directory in members):
        raise ProjectAnnotationImportError("ZIP annotation files must be inside task folders")
    roots = {parts[0] for parts, _ in members}
    offset = 0
    if len(roots) == 1 and not (roots & names.keys()):
        children = {parts[1] for parts, _ in members if len(parts) > 1}
        if children & names.keys():
            if any(len(parts) == 2 and not directory for parts, directory in members):
                raise ProjectAnnotationImportError("ZIP common parent contains loose files")
            offset = 1
    folders = {parts[: offset + 1] for parts, _ in members if len(parts) > offset}
    result = FolderPlan()
    for prefix in sorted(folders):
        candidates = names.get(prefix[-1], [])
        if not candidates:
            result.unmatched += 1
        elif len(candidates) > 1:
            result.failed_tasks.update(candidates)
        else:
            result.targets[candidates[0]] = prefix
    return result


def task_source(source, members, prefix, destination, extensions, budget):
    """Preserve native paths; a single native file may also be uploaded in its task folder."""
    destination = Path(destination)
    paths = [
        parts[len(prefix) :]
        for parts, directory in members
        if not directory and parts[: len(prefix)] == prefix
    ]
    if not paths:
        raise ProjectAnnotationImportError("Task folder contains no annotation files")
    extensions = {value.strip().lower() for value in extensions.split(",")}
    single = (
        len(paths) == 1
        and len(paths[0]) == 1
        and PurePosixPath(paths[0][0]).suffix.lstrip(".").lower() in extensions
    )
    target = destination / ("input" + PurePosixPath(paths[0][0]).suffix if single else "input.zip")
    with ZipFile(source) as archive:
        if single:
            with archive.open("/".join((*prefix, *paths[0]))) as member, target.open("xb") as out:
                while chunk := member.read(1024**2):
                    out.write(chunk)
            if is_zipfile(target):
                # A rejected task must not spend the expansion budget of others.
                nested_budget = Budget(budget.bytes_left, budget.entries_left)
                with target.open("rb") as nested:
                    inner = inspect_zip(nested, nested_budget)
                if any(PurePosixPath(*p).suffix.lower() == ".zip" for p, d in inner if not d):
                    raise ProjectAnnotationImportError(
                        "Nested annotation archives are not supported"
                    )
                budget.bytes_left = nested_budget.bytes_left
                budget.entries_left = nested_budget.entries_left
            elif target.suffix.lower() == ".zip":
                raise ProjectAnnotationImportError("Invalid annotation ZIP")
        else:
            if "zip" not in extensions:
                raise ProjectAnnotationImportError(
                    "Task folder must contain one native annotation file"
                )
            if any(PurePosixPath(*parts).suffix.lower() == ".zip" for parts in paths):
                raise ProjectAnnotationImportError("Task folder has ambiguous nested archives")
            # Repacking changes only the outer task prefix, never the format's internal structure.
            with ZipFile(target, "w") as output:
                for parts in paths:
                    with archive.open("/".join((*prefix, *parts))) as member:
                        with output.open("/".join(parts), "w", force_zip64=True) as out:
                            while chunk := member.read(1024**2):
                                out.write(chunk)
    return target
