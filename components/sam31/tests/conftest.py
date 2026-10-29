# SPDX-License-Identifier: MIT
"""Load the real SAM3.1 modules without copying or rewriting their source paths."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import torch


ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "serverless/pytorch/facebookresearch/sam2/nuclio"
RUNTIME = ROOT / "components/sam31/nuclio"


@pytest.fixture
def sam31_runtime(monkeypatch):
    modules = {}
    for name in ("protocol", "geometry", "temporal_video", "state_codec", "redis_store", "tracker"):
        directory = SHARED if name in ("protocol", "geometry", "redis_store") else RUNTIME
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return SimpleNamespace(**modules)


@pytest.fixture
def sam31_snapshot(sam31_runtime):
    def make(*, index=0, count=2, size=(24, 20)):
        def memory():
            return {
                "obj_ptr": torch.ones(1, 16, 2),
                "maskmem_features": torch.ones(1, 2, 2, 2),
                "maskmem_pos_enc": [torch.ones(1, 2, 2, 2)],
                "image_features": torch.ones(4, 1, 2),
                "image_pos_enc": torch.ones(4, 1, 2),
            }
        return sam31_runtime.temporal_video.Snapshot(index, *size, count, {
            "cond_frame_outputs": {0: memory()},
            "non_cond_frame_outputs": {index: memory()} if index else {},
        })
    return make
