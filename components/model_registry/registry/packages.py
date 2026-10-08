# SPDX-License-Identifier: MIT
from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path

from .codec import decode_image_bytes
from .schema import Manifest

MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 3 * 1024**3
MAX_FILES = 40


def extract_package(archive: Path, destination: Path) -> tuple[Manifest, str]:
    """Never import uploaded Python in the management process."""
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("package exceeds 2 GiB")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            items = bundle.infolist()
            if not items or len(items) > MAX_FILES:
                raise ValueError("package must contain between 1 and 40 files")
            names = [x.filename for x in items]
            if len({n.casefold() for n in names}) != len(names):
                raise ValueError("duplicate filenames, including case-insensitive duplicates")
            if sum(x.file_size for x in items) > MAX_EXPANDED:
                raise ValueError("expanded package exceeds 3 GiB")
            total = 0
            for item in items:
                name = item.filename
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
                    raise ValueError("package files must be root-level ASCII filenames; directories are not accepted")
                mode = (item.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or item.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError("links and special files are not allowed")
                if item.flag_bits & 1:
                    raise ValueError("encrypted archives are not allowed")
                if name not in ("manifest.json", "sample.png", "sample.jpg") and not name.endswith((".onnx", ".py")):
                    raise ValueError(f"unsupported package file: {name}")
                if name.endswith(".py") and (item.file_size > 1024**2 or name in {"sitecustomize.py", "usercustomize.py", "registry.py"}):
                    raise ValueError("Python file is oversized or uses a reserved name")
                if name == "manifest.json" and item.file_size > 256 * 1024:
                    raise ValueError("manifest exceeds 256 KiB")
                if name.startswith("sample.") and item.file_size > 32 * 1024**2:
                    raise ValueError("sample image exceeds 32 MiB")
                with bundle.open(item) as src, (destination / name).open("xb") as dst:
                    written = 0
                    while chunk := src.read(1024**2):
                        written += len(chunk)
                        total += len(chunk)
                        if written > item.file_size or total > MAX_EXPANDED:
                            raise ValueError("archive expansion limit exceeded")
                        dst.write(chunk)
                (destination / name).chmod(0o444)
        required = {"manifest.json", "model.py"}
        if not required.issubset(names):
            raise ValueError("manifest.json and model.py are required")
        if len(set(names) & {"sample.png", "sample.jpg"}) != 1:
            raise ValueError("exactly one sample.png or sample.jpg is required")
        manifest = Manifest.model_validate_json((destination / "manifest.json").read_bytes())
        if set(manifest.weights) != {x for x in names if x.endswith(".onnx")}:
            raise ValueError("manifest.weights must list all and only the uploaded ONNX files")
        for filename in names:
            if filename.endswith(".py"):
                tree = ast.parse((destination / filename).read_text(encoding="utf-8"), filename=filename)
                if filename == "model.py" and not any(isinstance(v, ast.ClassDef) and v.name == "Model" for v in tree.body):
                    raise ValueError("model.py must declare class Model(ModelBase)")
        sample = next(destination.glob("sample.*"))
        decode_image_bytes(sample.read_bytes())
        digest = hashlib.sha256()
        for file in sorted(destination.iterdir()):
            digest.update(file.name.encode() + b"\0")
            with file.open("rb") as src:
                while block := src.read(1024**2):
                    digest.update(block)
        destination.chmod(0o755)
        return manifest, digest.hexdigest()
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
