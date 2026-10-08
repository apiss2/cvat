"""ITGformat HDR-driven unsigned bit masks and axis-aligned bounding boxes.

This module intentionally has no CVAT, Datumaro or native-extension dependency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAX_PIXELS = 64_000_000
MAX_HEADER_BYTES = 1024**2
# RE4 stores each value and run length as one little-endian uint32 word.
# HDR describes the decoded pixel width, not the wire word width.
SUPPORTED_BYTE_COUNTS = (1, 2, 4)
RE4_WORD_DTYPE = np.dtype("<u4")
RE4_PAIR_BYTES = RE4_WORD_DTYPE.itemsize * 2
RE4_MAX_VALUE = int(np.iinfo(RE4_WORD_DTYPE).max)
MAX_MASK_BITS = max(SUPPORTED_BYTE_COUNTS) * 8


class FormatError(ValueError):
    """Input cannot be represented without ambiguity or silent truncation."""


def checked_size(width: int, height: int) -> tuple[int, int]:
    if isinstance(width, bool) or isinstance(height, bool):
        raise FormatError("Image dimensions must be integers")
    if not isinstance(width, (int, np.integer)) or not isinstance(
        height, (int, np.integer)
    ):
        raise FormatError("Image dimensions must be integers")
    if width <= 0 or height <= 0 or int(width) * int(height) > MAX_PIXELS:
        raise FormatError(
            f"Invalid/oversized image: {width} x {height} (limit {MAX_PIXELS} pixels)"
        )
    return int(width), int(height)


def checked_name(name: str, *, bbox: bool = False) -> str:
    if not isinstance(name, str) or not name or name != name.strip():
        raise FormatError("Class names must be nonempty without surrounding whitespace")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise FormatError(
            f"Control characters are not allowed in a class name: {name!r}"
        )
    if bbox and any(c.isspace() for c in name):
        raise FormatError(
            f"A .bb class must be a single whitespace-free token: {name!r}"
        )
    return name


def checked_byte_count(byte_count: int) -> int:
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, (int, np.integer))
        or byte_count not in SUPPORTED_BYTE_COUNTS
    ):
        raise FormatError(
            f"Unsupported HDR byte count {byte_count!r}; expected 1, 2 or 4. "
            "RE4 value words are uint32, so wider pixels cannot be represented."
        )
    return int(byte_count)


def mask_dtype(byte_count: int) -> np.dtype:
    return np.dtype(f"u{checked_byte_count(byte_count)}")


def choose_byte_count(required_bits: int) -> int:
    """Choose the smallest supported pixel width containing required_bits bits.

    For a sparse mapping, pass max(bit_indices) + 1, not len(bit_indices).
    A scope with no segmentation shapes does not need a mask or a width.
    """
    if (
        isinstance(required_bits, bool)
        or not isinstance(required_bits, (int, np.integer))
        or not 1 <= required_bits <= MAX_MASK_BITS
    ):
        raise FormatError(
            f"Mask requires {required_bits!r} bits; this RE4 implementation supports "
            f"1..{MAX_MASK_BITS} bits (at most {MAX_MASK_BITS} segmentation classes "
            "in one export scope)."
        )
    return next(size for size in SUPPORTED_BYTE_COUNTS if required_bits <= size * 8)


def checked_bits(bit_names: Mapping[int, str], byte_count: int) -> dict[int, str]:
    capacity = checked_byte_count(byte_count) * 8
    if not bit_names:
        raise FormatError(
            "HDR must contain a bit-to-class mapping, including for an empty mask"
        )
    result: dict[int, str] = {}
    for bit, name in bit_names.items():
        if (
            isinstance(bit, bool)
            or not isinstance(bit, (int, np.integer))
            or not 0 <= bit < capacity
        ):
            raise FormatError(
                f"Bit index {bit!r} is outside 0..{capacity - 1} for "
                f"HDR byte count {byte_count}"
            )
        result[int(bit)] = checked_name(name)
    if len(set(result.values())) != len(result):
        raise FormatError("A class must not be assigned to multiple bits in one HDR")
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class Header:
    width: int
    height: int
    bit_names: dict[int, str]
    byte_count: int
    spacing_xyz: tuple[float, float, float] = (1.0, 1.0, 1.0)

    def __post_init__(self) -> None:
        width, height = checked_size(self.width, self.height)
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "byte_count", checked_byte_count(self.byte_count))
        object.__setattr__(
            self, "bit_names", checked_bits(self.bit_names, self.byte_count)
        )
        if len(self.spacing_xyz) != 3 or not all(
            np.isfinite(x) and x > 0 for x in self.spacing_xyz
        ):
            raise FormatError("HDR spacing must contain three finite, positive values")

    @property
    def bit_capacity(self) -> int:
        return self.byte_count * 8

    @property
    def dtype(self) -> np.dtype:
        return mask_dtype(self.byte_count)


def parse_header(text: str) -> Header:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines:
        raise FormatError("Empty HDR")
    fields = lines[0].split()
    if len(fields) != 8 or fields[7] != "mask":
        raise FormatError("HDR first line must be: x y 1 byte_count sx sy sz mask")
    try:
        width, height, depth, byte_count = map(int, fields[:4])
        spacing = tuple(map(float, fields[4:7]))
    except ValueError as exc:
        raise FormatError("Invalid numeric value in HDR first line") from exc
    if depth != 1:
        raise FormatError("Only 2-D masks (HDR z=1) are supported")
    checked_byte_count(byte_count)
    bits: dict[int, str] = {}
    for number, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            raise FormatError(f"HDR line {number}: expected bit_index class_name")
        try:
            bit = int(parts[0])
        except ValueError as exc:
            raise FormatError(f"HDR line {number}: invalid bit index") from exc
        if bit in bits:
            raise FormatError(f"HDR line {number}: duplicate bit {bit}")
        bits[bit] = parts[1]
    return Header(width, height, bits, byte_count, spacing)


def format_header(header: Header) -> str:
    sx, sy, sz = header.spacing_xyz
    first = (
        f"{header.width} {header.height} 1 {header.byte_count} "
        f"{sx:.9g} {sy:.9g} {sz:.9g} mask\n"
    )
    return first + "".join(
        f"{bit} {name}\n" for bit, name in sorted(header.bit_names.items())
    )


def decode_re4(
    data: bytes,
    width: int,
    height: int,
    bit_names: Mapping[int, str],
    *,
    byte_count: int,
) -> np.ndarray:
    """Validate uint32 wire values before narrowing to the dtype declared by HDR."""
    checked_size(width, height)
    bits = checked_bits(bit_names, byte_count)
    dtype = mask_dtype(byte_count)
    pixels = int(width) * int(height)
    if not data or len(data) % RE4_PAIR_BYTES or len(data) > pixels * RE4_PAIR_BYTES:
        raise FormatError("RE4 must contain nonempty uint32 value/length pairs")
    pairs = np.frombuffer(data, dtype=RE4_WORD_DTYPE).reshape(-1, 2)
    values, lengths = pairs[:, 0], pairs[:, 1]
    if np.any(values > int(np.iinfo(dtype).max)):
        raise FormatError(
            f"RE4 contains a value outside the HDR {byte_count}-byte pixel range"
        )
    if np.any(lengths == 0) or int(lengths.sum(dtype=np.uint64)) != pixels:
        raise FormatError(
            "RE4 run lengths do not match width*height or contain a zero run"
        )
    defined = sum(1 << bit for bit in bits)
    if np.any(values & np.uint32(RE4_MAX_VALUE ^ defined)):
        raise FormatError("RE4 uses a bit without a class definition in HDR")
    return np.repeat(values.astype(dtype, copy=False), lengths.astype(np.intp)).reshape(
        height, width
    )


def _checked_mask_dtype(mask: np.ndarray) -> np.dtype:
    if (
        not isinstance(mask, np.ndarray)
        or mask.ndim != 2
        or mask.dtype.kind != "u"
        or mask.dtype.itemsize not in SUPPORTED_BYTE_COUNTS
    ):
        raise FormatError("Mask must be a 2-D uint8, uint16 or uint32 array")
    checked_size(mask.shape[1], mask.shape[0])
    return mask_dtype(mask.dtype.itemsize)


def encode_re4(mask: np.ndarray) -> bytes:
    dtype = _checked_mask_dtype(mask)
    flat = np.ascontiguousarray(mask, dtype=dtype).reshape(-1)
    starts = np.r_[0, np.flatnonzero(flat[1:] != flat[:-1]) + 1]
    lengths = np.diff(np.r_[starts, flat.size])
    encoded = np.empty((len(starts), 2), dtype=RE4_WORD_DTYPE)
    encoded[:, 0] = flat[starts]
    encoded[:, 1] = lengths
    return encoded.tobytes()


def read_mask(hdr_path: Path) -> tuple[Header, np.ndarray]:
    if hdr_path.stat().st_size > MAX_HEADER_BYTES:
        raise FormatError("Oversized HDR")
    header = parse_header(hdr_path.read_text(encoding="utf-8-sig"))
    re4_path = hdr_path.with_suffix(".re4")
    if not re4_path.is_file():
        raise FormatError(f"Missing paired file: {re4_path.name}")
    if re4_path.stat().st_size > header.width * header.height * RE4_PAIR_BYTES:
        raise FormatError(f"Oversized RE4: {re4_path.name}")
    return header, decode_re4(
        re4_path.read_bytes(),
        header.width,
        header.height,
        header.bit_names,
        byte_count=header.byte_count,
    )


def write_mask(hdr_path: Path, mask: np.ndarray, bit_names: Mapping[int, str]) -> None:
    """Write HDR pixel width from the unsigned array dtype, including for all-zero masks."""
    dtype = _checked_mask_dtype(mask)
    header = Header(mask.shape[1], mask.shape[0], dict(bit_names), dtype.itemsize)
    defined = sum(1 << bit for bit in header.bit_names)
    if np.any(mask & dtype.type(int(np.iinfo(dtype).max) ^ defined)):
        raise FormatError("Mask uses an undefined bit")
    encoded = encode_re4(mask)
    hdr_path.parent.mkdir(parents=True, exist_ok=True)
    hdr_path.with_suffix(".re4").write_bytes(encoded)
    hdr_path.write_text(format_header(header), encoding="utf-8")


@dataclass(frozen=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float
    label: str


def check_box(box: Box, width: int | None = None, height: int | None = None) -> Box:
    checked_name(box.label, bbox=True)
    if not all(np.isfinite(v) for v in (box.x1, box.y1, box.x2, box.y2)):
        raise FormatError("Bbox coordinates must be finite")
    if not (0 <= box.x1 < box.x2 and 0 <= box.y1 < box.y2):
        raise FormatError(
            "Bbox must have nonnegative coordinates and positive width/height"
        )
    if width is not None and box.x2 > width or height is not None and box.y2 > height:
        raise FormatError("Bbox extends outside the image")
    return box


def parse_boxes(
    text: str, width: int | None = None, height: int | None = None
) -> list[Box]:
    boxes = []
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 7:
            raise FormatError(f"BB line {number}: expected x1 y1 z1 x2 y2 z2 class")
        try:
            x1, y1, z1, x2, y2, z2 = map(float, fields[:6])
        except ValueError as exc:
            raise FormatError(f"BB line {number}: invalid numeric coordinate") from exc
        # ITGformat accepts a zero-depth plane or the half-open unit slice.
        # Export uses the half-open slice [0, 1).
        if (z1, z2) not in {(0.0, 0.0), (0.0, 1.0)}:
            raise FormatError(f"BB line {number}: expected z=(0,0) or z=(0,1)")
        boxes.append(check_box(Box(x1, y1, x2, y2, fields[6]), width, height))
    return boxes


def format_boxes(boxes: Iterable[Box], width: int, height: int) -> str:
    lines = []
    for box in boxes:
        check_box(box, width, height)
        lines.append(
            f"{box.x1:.17g} {box.y1:.17g} 0 {box.x2:.17g} {box.y2:.17g} 1 {box.label}\n"
        )
    return "".join(lines)


def pack_segments(
    segments: Iterable[tuple[str, np.ndarray]],
    width: int,
    height: int,
    bit_names: Mapping[int, str],
    *,
    byte_count: int,
) -> np.ndarray:
    """OR instances of one class and preserve overlaps within the specified pixel width."""
    checked_size(width, height)
    reverse = {name: bit for bit, name in checked_bits(bit_names, byte_count).items()}
    dtype = mask_dtype(byte_count)
    packed = np.zeros((height, width), dtype=dtype)
    for label, binary in segments:
        binary = np.asarray(binary)
        if label not in reverse:
            raise FormatError(f"No export bit assigned for {label!r}")
        if binary.shape != packed.shape or not np.all((binary == 0) | (binary == 1)):
            raise FormatError(
                "Segmentation masks must be binary and match the image shape"
            )
        packed[binary.astype(bool)] |= dtype.type(1 << reverse[label])
    return packed
