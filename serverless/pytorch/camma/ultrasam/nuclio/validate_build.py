# SPDX-License-Identifier: MIT
"""Build-time CPU check: import the compiled operators and strictly load weights."""

import importlib
import json
import sys
from pathlib import Path

import torch
from ultrasam_constants import CHECKPOINT, CHECKPOINT_SHA256, UPSTREAM_COMMIT
from ultrasam_model import build_model
from ultrasam_runtime import run_probe


def main():
    runtime = run_probe("build")
    expected = {
        "torch": "2.0.0",
        "torchvision": "0.15.1",
        "mmcv": "2.1.0",
        "mmengine": "0.10.7",
        "mmdet": "3.2.0",
        "mmpretrain": "1.2.0",
    }
    versions = {name: importlib.import_module(name).__version__ for name in expected}
    if sys.version_info[:2] != (3, 10) or torch.version.cuda != "11.8":
        raise RuntimeError("UltraSAM build requires Python 3.10 and PyTorch CUDA 11.8")
    for name, expected_version in expected.items():
        if versions[name].split("+")[0] != expected_version:
            raise RuntimeError(f"Unexpected {name} version: {versions[name]}")
    importlib.import_module("mmcv._ext")
    model = build_model(device="cpu")
    metadata = {
        "upstream_commit": UPSTREAM_COMMIT,
        "checkpoint_sha256": CHECKPOINT_SHA256,
        "python": sys.version.split()[0],
        "versions": versions,
        "parameters": sum(parameter.numel() for parameter in model.parameters()),
        "checkpoint_tensors": len(model.state_dict()),
        "strict_load": True,
        "runtime": runtime,
    }
    Path("/opt/nuclio/build-validation.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    CHECKPOINT.with_name("SHA256SUMS").write_text(
        f"{CHECKPOINT_SHA256}  {CHECKPOINT.name}\n"
    )
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
