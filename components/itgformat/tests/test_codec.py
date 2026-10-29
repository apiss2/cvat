import struct

import numpy as np
import pytest
from itgformat_core.codec import (
    SUPPORTED_BYTE_COUNTS,
    FormatError,
    checked_bits,
    checked_byte_count,
    checked_size,
    choose_byte_count,
    decode_re4,
    encode_re4,
    format_boxes,
    format_header,
    mask_dtype,
    pack_segments,
    parse_boxes,
    parse_header,
    read_mask,
    write_mask,
)

BITS = {0: "a", 15: "z"}


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
@pytest.mark.parametrize("shape", [(1, 1), (1, 13), (17, 1), (3, 7), (31, 53)])
@pytest.mark.parametrize("kind", ["zero", "full", "mixed"])
def test_roundtrip(shape, kind, byte_count):
    dtype = mask_dtype(byte_count)
    capacity = byte_count * 8
    maximum = (1 << capacity) - 1
    rng = np.random.default_rng(7)
    if kind == "zero":
        array = np.zeros(shape, dtype=dtype)
    elif kind == "full":
        array = np.full(shape, maximum, dtype=dtype)
    else:
        array = rng.integers(0, maximum, shape, dtype=dtype, endpoint=True)
    bits = {i: str(i) for i in range(capacity)}
    got = decode_re4(encode_re4(array), shape[1], shape[0], bits, byte_count=byte_count)
    assert got.dtype == dtype
    np.testing.assert_array_equal(got, array)


def test_wire_layout_is_u32_pairs_row_major():
    a = np.array([[1, 1, 32769], [32769, 32769, 0]], dtype=np.uint16)
    assert encode_re4(a) == struct.pack("<6I", 1, 2, 32769, 3, 0, 1)


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
def test_noncontiguous_and_big_endian_input(byte_count):
    a = np.arange(60, dtype=mask_dtype(byte_count)).reshape(6, 10)[:, ::2]
    bits = {i: str(i) for i in range(byte_count * 8)}
    for sample in (a, a.astype(f">u{byte_count}")):
        np.testing.assert_array_equal(
            decode_re4(encode_re4(sample), 5, 6, bits, byte_count=byte_count), a
        )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "5 3 1 2 1 1 1 mask\n",
        "5 3 1 2 1 1 1 mask\n16 a\n",
        "5 3 1 2 1 1 1 mask\n-1 a\n",
        "5 3 2 2 1 1 1 mask\n0 a\n",
        "5 3 1 3 1 1 1 mask\n0 a\n",
        "5 3 1 2 1 1 1 label\n0 a\n",
        "5 3 1 2 nan 1 1 mask\n0 a\n",
        "5 3 1 2 0 1 1 mask\n0 a\n",
        "5 3 1 2 1 1 1 mask\n0 a\n0 b\n",
        "5 3 1 2 1 1 1 mask\n0 a\n1 a\n",
        "5 3 1 2 1 1 1 mask\n0\n",
        "5 3 1 2 1 1 1 mask\nx a\n",
        "0 3 1 2 1 1 1 mask\n0 a\n",
        "5.0 3 1 2 1 1 1 mask\n0 a\n",
    ],
)
def test_bad_headers(bad):
    with pytest.raises(FormatError):
        parse_header(bad)


def test_header_spacing_and_names():
    h = parse_header("\ufeff5\t3  1 2 0.5 1.25 2 mask\n0 左 肺\n15 肝臓\n")
    assert (h.width, h.height, h.spacing_xyz) == (5, 3, (0.5, 1.25, 2.0))
    assert h.bit_names == {0: "左 肺", 15: "肝臓"}
    assert parse_header(format_header(h)) == h


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"abc",
        struct.pack("<I", 1),
        struct.pack("<2I", 65536, 6),
        struct.pack("<2I", 2, 6),
        struct.pack("<2I", 1, 5),
        struct.pack("<2I", 1, 7),
        struct.pack("<4I", 0, 0, 1, 6),
        struct.pack("<2I", 1, 0xFFFFFFFF),
    ],
)
def test_bad_wire(data):
    with pytest.raises(FormatError):
        decode_re4(data, 3, 2, BITS, byte_count=2)


@pytest.mark.parametrize(
    "a",
    [
        np.zeros((2, 3), np.uint64),
        np.zeros((2, 3), np.float32),
        np.zeros((2, 3), bool),
        np.zeros((2, 3), np.int16),
        np.zeros((1, 2, 3), np.uint16),
        np.zeros((0, 3), np.uint16),
    ],
)
def test_bad_encode_input(a):
    with pytest.raises(FormatError):
        encode_re4(a)


def test_empty_mapping_always_fails(tmp_path):
    with pytest.raises(FormatError):
        write_mask(tmp_path / "a.mask.hdr", np.zeros((2, 3), np.uint16), {})


def test_undefined_export_bit_fails(tmp_path):
    with pytest.raises(FormatError):
        write_mask(tmp_path / "a.mask.hdr", np.full((2, 3), 2, np.uint16), {0: "a"})


def test_pack_overlap_hole_and_same_class_union():
    outer = np.ones((5, 7), bool)
    outer[1:4, 1:6] = False
    overlap = np.zeros((5, 7), bool)
    overlap[0:2, :2] = True
    extra = np.zeros((5, 7), bool)
    extra[2, 3] = True
    packed = pack_segments(
        [("a", outer), ("z", overlap), ("a", extra)], 7, 5, BITS, byte_count=2
    )
    assert packed[0, 0] == 32769
    assert packed[2, 2] == 0
    assert packed[2, 3] == 1
    np.testing.assert_array_equal((packed & 1) != 0, outer | extra)
    np.testing.assert_array_equal((packed & 32768) != 0, overlap)


def test_pack_rejects_nonbinary():
    with pytest.raises(FormatError):
        pack_segments([("a", np.full((2, 3), 255))], 3, 2, {0: "a"}, byte_count=1)


@pytest.mark.parametrize(
    "text", ["0 0 0 3 2 1 a\n", "0 0 0 3 2 0 a\n", "\n0.25 0.5 0 2.75 1.5 1 クラス\n"]
)
def test_bb_roundtrip(text):
    boxes = parse_boxes(text, 3, 2)
    assert parse_boxes(format_boxes(boxes, 3, 2), 3, 2) == boxes
    assert all(
        line.split()[5] == "1" for line in format_boxes(boxes, 3, 2).splitlines()
    )


@pytest.mark.parametrize(
    "text",
    [
        "0 0 0 1 1 1 a b",
        "0 0 2 1 1 3 a",
        "0 0 0 nan 1 1 a",
        "-1 0 0 1 1 1 a",
        "0 0 0 0 1 1 a",
        "2 0 0 1 1 1 a",
        "0 0 0 4 2 1 a",
        "0 0 0 1 1 inf a",
        "0 0 0 1 x 1 a",
    ],
)
def test_bad_boxes(text):
    with pytest.raises(FormatError):
        parse_boxes(text, 3, 2)


def test_bbox_numeric_class_is_literal_name_and_empty_is_valid():
    assert parse_boxes("0 0 0 1 1 1 7")[0].label == "7"
    assert parse_boxes("\n \n") == []
    assert format_boxes([], 5, 3) == ""


def test_size_limit_before_allocation():
    with pytest.raises(FormatError):
        decode_re4(struct.pack("<2I", 0, 1), 10**8, 10**8, {0: "a"}, byte_count=1)
    for size in ((True, 1), (1.5, 1), (-1, 1)):
        with pytest.raises(FormatError):
            checked_size(*size)


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
def test_hdr_and_file_dtype_follow_declared_width(tmp_path, byte_count):
    highest = byte_count * 8 - 1
    bits = {0: "low", highest: "high"}
    a = np.array(
        [[0, 1, 1 << highest], [(1 << highest) | 1, 0, 0]], dtype=mask_dtype(byte_count)
    )
    path = tmp_path / "a.mask.hdr"
    write_mask(path, a, bits)
    header, decoded = read_mask(path)
    assert header.byte_count == byte_count
    assert header.bit_capacity == byte_count * 8
    assert decoded.dtype == mask_dtype(byte_count)
    assert int(path.read_text().split()[3]) == byte_count
    assert parse_header(format_header(header)) == header
    np.testing.assert_array_equal(a, decoded)


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
def test_empty_mask_keeps_width_and_mapping(tmp_path, byte_count):
    path = tmp_path / "empty.mask.hdr"
    bits = {byte_count * 8 - 1: "unused_high_bit"}
    write_mask(path, np.zeros((3, 5), dtype=mask_dtype(byte_count)), bits)
    header, decoded = read_mask(path)
    assert header.byte_count == byte_count and header.bit_names == bits
    assert decoded.dtype.itemsize == byte_count and not decoded.any()
    assert path.with_suffix(".re4").read_bytes() == struct.pack("<2I", 0, 15)


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
def test_mapping_limit_depends_on_highest_bit_not_mapping_length(byte_count):
    highest = byte_count * 8 - 1
    assert checked_bits({highest: "valid"}, byte_count) == {highest: "valid"}
    for bit in (-1, highest + 1, True, 1.5):
        with pytest.raises(FormatError, match="Bit index"):
            checked_bits({bit: "invalid"}, byte_count)
    with pytest.raises(FormatError, match="Bit index"):
        parse_header(f"5 3 1 {byte_count} 1 1 1 mask\n{highest + 1} invalid\n")


@pytest.mark.parametrize("byte_count", [1, 2])
def test_wire_range_overflow_rejected_before_cast(byte_count):
    # Low bits would look valid if the value were silently narrowed first.
    data = struct.pack("<2I", (1 << (byte_count * 8)) | 1, 6)
    with pytest.raises(FormatError, match="pixel range"):
        decode_re4(data, 3, 2, {0: "low"}, byte_count=byte_count)


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
def test_undefined_highest_bit_rejected_on_input_and_output(tmp_path, byte_count):
    value = 1 << (byte_count * 8 - 1)
    with pytest.raises(FormatError, match="without a class definition"):
        decode_re4(
            struct.pack("<2I", value, 6), 3, 2, {0: "low"}, byte_count=byte_count
        )
    with pytest.raises(FormatError, match="undefined bit"):
        write_mask(
            tmp_path / "bad.mask.hdr",
            np.full((2, 3), value, mask_dtype(byte_count)),
            {0: "low"},
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("byte_count", [0, -1, 3, 5, 8, 16, True, 2.0, "2", None])
def test_unsupported_pixel_width_fails(byte_count):
    with pytest.raises(FormatError, match="Unsupported HDR byte count"):
        checked_byte_count(byte_count)


@pytest.mark.parametrize("byte_count", [0, -1, 3, 8])
def test_unsupported_width_in_hdr_fails_even_for_low_bits(byte_count):
    with pytest.raises(FormatError, match="Unsupported HDR byte count"):
        parse_header(f"5 3 1 {byte_count} 1 1 1 mask\n0 a\n")


@pytest.mark.parametrize(
    "required_bits, expected",
    [(1, 1), (8, 1), (9, 2), (16, 2), (17, 4), (31, 4), (32, 4)],
)
def test_smallest_sufficient_width(required_bits, expected):
    assert choose_byte_count(required_bits) == expected


@pytest.mark.parametrize("required_bits", [0, -1, 33, 64, True, 2.0, "2", None])
def test_unrepresentable_bit_requirement_fails(required_bits):
    with pytest.raises(FormatError):
        choose_byte_count(required_bits)


@pytest.mark.parametrize("byte_count", SUPPORTED_BYTE_COUNTS)
def test_pack_uses_full_unsigned_width_and_overlap(byte_count):
    highest = byte_count * 8 - 1
    low = np.ones((2, 3), bool)
    high = np.array([[True, False, False], [False, True, False]])
    packed = pack_segments(
        [("low", low), ("high", high)],
        3,
        2,
        {0: "low", highest: "high"},
        byte_count=byte_count,
    )
    assert packed.dtype == mask_dtype(byte_count)
    assert int(packed[0, 0]) == (1 << highest) | 1
    assert int(packed[0, 1]) == 1


def test_sparse_mapping_cannot_be_packed_into_a_narrow_dtype():
    with pytest.raises(FormatError, match="Bit index"):
        pack_segments([], 3, 2, {31: "one_class"}, byte_count=2)


def test_same_wire_word_size_for_each_hdr_width():
    expected = struct.pack("<6I", 0, 2, 1, 3, 0, 1)
    for byte_count in SUPPORTED_BYTE_COUNTS:
        a = np.array([[0, 0, 1], [1, 1, 0]], dtype=mask_dtype(byte_count))
        assert encode_re4(a) == expected
