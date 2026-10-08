"""Validate the distributed example archives against their documented contract."""

from pathlib import Path

import numpy as np
import pytest
from itgformat_core.archive import extract_zip, read_records
from itgformat_core.codec import read_mask

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize(
    "name, expected_labels",
    [
        ("sample_images.zip", []),
        ("sample_dataset.zip", ["lesion", "region", "vessel"]),
        ("sample_annotations.zip", ["lesion", "region", "vessel"]),
        ("sample_widths_dataset.zip", ["bit_15", "bit_31", "bit_7"]),
    ],
)
def test_archive_inventory(name, expected_labels, tmp_path):
    if not EXAMPLES.is_dir():
        pytest.skip(
            "Example archives are in the distribution bundle, not the source overlay"
        )
    records, labels = read_records(extract_zip(EXAMPLES / name, tmp_path / "data"))
    assert len(records) == 3 and labels == expected_labels
    if name != "sample_annotations.zip":
        assert all(record.image_path is not None for record in records)


def test_sample_regions_and_boxes(tmp_path):
    if not EXAMPLES.is_dir():
        pytest.skip("Example archives are in the distribution bundle")
    root = extract_zip(EXAMPLES / "sample_dataset.zip", tmp_path / "data")
    header, packed = read_mask(root / "a.mask.hdr")
    assert header.byte_count == 2 and header.bit_names == {0: "region", 15: "vessel"}
    region = (packed & 1) != 0
    vessel = (packed & 32768) != 0
    assert (
        region.sum() == 744 and vessel.sum() == 560 and (region & vessel).sum() == 224
    )
    records, labels = read_records(root)
    box = next(record for record in records if record.item_id == "b").boxes[0]
    assert (box.x1, box.y1, box.x2, box.y2, box.label) == (10, 12, 35, 32, "lesion")


def test_width_samples_use_their_highest_bit(tmp_path):
    if not EXAMPLES.is_dir():
        pytest.skip("Example archives are in the distribution bundle")
    root = extract_zip(EXAMPLES / "sample_widths_dataset.zip", tmp_path / "data")
    for byte_count in (1, 2, 4):
        header, packed = read_mask(root / f"width{byte_count}.mask.hdr")
        bit = byte_count * 8 - 1
        assert header.byte_count == byte_count and header.bit_names == {
            bit: f"bit_{bit}"
        }
        assert packed.dtype.itemsize == byte_count
        assert np.count_nonzero(packed) == 48 and int(packed.max()) == 1 << bit
