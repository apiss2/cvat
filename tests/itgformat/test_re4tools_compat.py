"""Compatibility with the unmodified reference C++ codec and HDR functions.

Install reference/re4tools-main.zip or put its built src directory on PYTHONPATH.
The reference save_re4 wrapper accepts uint8/uint16 masks; for uint32, use the
reference save_hdr and encode_run_length functions to create the reference file.
"""

import sys

import numpy as np
import pytest

reference = pytest.importorskip(
    "re4tools", reason="Build/install re4tools for compatibility tests"
)
from itgformat_core.codec import (
    SUPPORTED_BYTE_COUNTS,
    encode_re4,
    mask_dtype,
    read_mask,
    write_mask,
)
from re4tools.io import read_hdr, save_hdr
from re4tools.run_length import encode_run_length

pytestmark = pytest.mark.skipif(
    sys.byteorder != "little", reason="The wire platform is little-endian"
)


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
@pytest.mark.parametrize("seed", list(range(12)))
def test_original_cpp_wire_is_byte_identical(seed, byte_count, tmp_path):
    dtype = mask_dtype(byte_count)
    capacity = byte_count * 8
    maximum = (1 << capacity) - 1
    rng = np.random.default_rng(seed)
    a = rng.integers(0, maximum, (3 + seed, 19 + seed), dtype=dtype, endpoint=True)
    a[:2, :3] = 0
    a[-1, -1] = (1 << (capacity - 1)) | 1
    if seed % 2:
        a = a[:, ::2]
    bits = {i: f"class_{i}" for i in range(capacity)}
    encoded = encode_run_length(a[np.newaxis])
    assert encode_re4(a) == encoded
    original_hdr = tmp_path / "original.mask.hdr"
    if byte_count in (1, 2):
        reference.save_re4(
            a[np.newaxis], (1.0, 1.0, 1.0), "mask", original_hdr, bit_dict=bits
        )
    else:
        save_hdr(original_hdr, (1, *a.shape), dtype, (1.0, 1.0, 1.0), "mask", bits)
        original_hdr.with_suffix(".re4").write_bytes(encoded)
    header, from_original = read_mask(original_hdr)
    assert header.byte_count == byte_count and from_original.dtype == dtype
    np.testing.assert_array_equal(from_original, a)
    our_hdr = tmp_path / "ours.mask.hdr"
    write_mask(our_hdr, a, bits)
    assert (
        our_hdr.with_suffix(".re4").read_bytes()
        == original_hdr.with_suffix(".re4").read_bytes()
    )
    np.testing.assert_array_equal(reference.read_re4(our_hdr), a[np.newaxis])
    original_size, original_dtype, original_spacing, original_bits = read_hdr(
        our_hdr, return_bit_dict=True
    )
    assert original_dtype == dtype and original_bits == bits


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
@pytest.mark.parametrize("kind", ["zero", "low", "high", "high_and_low", "full"])
def test_original_cpp_constant_runs(kind, byte_count, tmp_path):
    capacity = byte_count * 8
    value = {
        "zero": 0,
        "low": 1,
        "high": 1 << (capacity - 1),
        "high_and_low": (1 << (capacity - 1)) | 1,
        "full": (1 << capacity) - 1,
    }[kind]
    a = np.full((7, 11), value, dtype=mask_dtype(byte_count))
    bits = {i: f"c{i}" for i in range(capacity)}
    assert encode_re4(a) == encode_run_length(a[np.newaxis])
    hdr = tmp_path / "constant.mask.hdr"
    write_mask(hdr, a, bits)
    np.testing.assert_array_equal(reference.read_re4(hdr), a[np.newaxis])
