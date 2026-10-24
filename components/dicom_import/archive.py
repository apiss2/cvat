# SPDX-License-Identifier: MIT
"""Bounded ZIP/TAR ingestion. Never use extractall on uploaded archives."""
from __future__ import annotations

import json
import re
import stat
import tarfile
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import DicomImportError
from .conversion import component

MANIFEST = '_cvat_dicom_import.json'


@dataclass
class Budget:
    bytes_left: int = 20 * 1024 ** 3
    files_left: int = 100000


def relative_path(name: str) -> Path:
    if not name or '\\' in name or '\x00' in name or re.match(r'^[A-Za-z]:', name):
        raise DicomImportError('Unsafe archive path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '..') for part in name.rstrip('/').split('/')):
        raise DicomImportError('Unsafe archive path')
    if any(any(ord(char) < 32 for char in part) for part in path.parts):
        raise DicomImportError('Control character in archive path')
    return Path(*path.parts)


def _copy(stream, target: Path, size: int, budget: Budget) -> None:
    if size < 0 or size > budget.bytes_left:
        raise DicomImportError('Expanded archive exceeds the byte limit')
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open('xb') as output:
        while chunk := stream.read(min(1024 ** 2, budget.bytes_left + 1)):
            count += len(chunk)
            budget.bytes_left -= len(chunk)
            if budget.bytes_left < 0 or count > size:
                raise DicomImportError('Expanded archive exceeds the byte limit')
            output.write(chunk)
    if count != size:
        raise DicomImportError('Truncated archive member')


def unpack(source, destination: Path, budget: Budget) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=False)
    paths, seen = [], set()

    def member(name: str) -> Path:
        relative = relative_path(name)
        canonical = relative.as_posix().casefold()
        if canonical in seen:
            raise DicomImportError('Duplicate archive path')
        seen.add(canonical)
        budget.files_left -= 1
        if budget.files_left < 0:
            raise DicomImportError('Archive has too many entries')
        return destination / relative

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                target = member(info.filename)
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if info.flag_bits & 1 or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise DicomImportError('Encrypted files and special archive entries are unsupported')
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                with archive.open(info) as stream:
                    _copy(stream, target, info.file_size, budget)
                paths.append(target)
    else:
        if hasattr(source, 'seek'):
            source.seek(0)
        args = {'fileobj': source} if hasattr(source, 'read') else {'name': str(source)}
        with tarfile.open(mode='r|*', **args) as archive:
            for info in archive:
                target = member(info.name)
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif info.isfile() and not info.sparse:
                    with archive.extractfile(info) as stream:
                        _copy(stream, target, info.size, budget)
                    paths.append(target)
                else:
                    raise DicomImportError('Links, sparse files and special archive entries are unsupported')
    return paths


def read_upload(source, destination: Path) -> tuple[str, list[Path]]:
    """A native project upload is one ZIP envelope with prefix and source files."""
    budget = Budget()
    outer = destination / 'upload'
    paths = unpack(source, outer, budget)
    manifest = outer / MANIFEST
    if manifest not in paths or manifest.stat().st_size > 4096:
        raise DicomImportError('Use Project Actions > DICOM import to specify the data-source prefix')
    try:
        options = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(options, dict) or set(options) != {'version', 'prefix'} or options['version'] != 1:
            raise ValueError
        if not isinstance(options['prefix'], str):
            raise ValueError
        prefix = component(options['prefix'], 'data-source prefix')
    except (ValueError, KeyError, UnicodeError) as exc:
        raise DicomImportError('Invalid DICOM import manifest') from exc
    files, queue, counter = [], deque((path, 0) for path in paths if path != manifest), 0
    while queue:
        path, depth = queue.popleft()
        if path.name.lower().endswith(('.zip', '.tar', '.tar.gz', '.tgz')):
            if depth >= 3:
                raise DicomImportError('Archive nesting exceeds three levels')
            counter += 1
            children = unpack(path, destination / f'archive-{counter}', budget)
            queue.extend((child, depth + 1) for child in children)
        else:
            files.append(path)
    return prefix, files
