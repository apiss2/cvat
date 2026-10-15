# SPDX-License-Identifier: MIT
"""CPU-only mask seed contracts; real checkpoint validation is separate."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]


def load(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    shared = ROOT / 'serverless/pytorch/facebookresearch/sam2/nuclio'
    protocol = load('protocol', shared / 'protocol.py', monkeypatch)
    geometry = load('geometry', shared / 'geometry.py', monkeypatch)
    return SimpleNamespace(protocol=protocol, geometry=geometry)


def test_mask_initialization_preserves_holes_and_disconnected_regions(modules):
    mask = np.zeros((6, 8), dtype=bool)
    mask[0:4, 0:4] = True
    mask[1, 1] = False
    mask[5, 7] = True
    shape = modules.geometry.mask_shape(mask)
    np.testing.assert_array_equal(modules.geometry.seed_mask(shape, (8, 6)), mask)


@pytest.mark.parametrize('points', [[0, 8, 1, 1, 3, 3], [9, 1, 1, 3, 3],
    [0, 9, 1, 1, 20, 20], [-1, 10, 1, 1, 3, 3], [False, 9, 1, 1, 3, 3],
    [0, 9.5, 1, 1, 3, 3], [0, 9, 3, 3, 1, 1], [0, 10**400, 1, 1, 3, 3],
    [0, float("nan"), 1, 1, 3, 3], [0, float("inf"), 1, 1, 3, 3]])
def test_invalid_mask_seed_rejected(modules, points):
    with pytest.raises(modules.protocol.ProtocolError):
        modules.geometry.seed_mask({'type': 'mask', 'points': points}, (8, 6))


def test_cvat_integral_float_rle_is_accepted(modules):
    result = modules.geometry.seed_mask({'type': 'mask', 'points': [0., 9., 1., 1., 3., 3.]}, (8, 6))
    assert result.sum() == 9
