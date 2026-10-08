import io
import json
import stat
import warnings
import zipfile

import numpy as np
import pytest
from itgformat_core.archive import (
    MANIFEST,
    Record,
    extract_zip,
    make_zip,
    read_records,
    resolve_frame,
    safe_relative,
    write_manifest,
)
from itgformat_core.codec import FormatError, write_mask
from PIL import Image


def image(path, width=7, height=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.zeros((height, width, 3), np.uint8)).save(path)


def test_optional_annotations_and_images(tmp_path):
    image(tmp_path / "image_only.png")
    write_mask(tmp_path / "mask_only.mask.hdr", np.zeros((5, 7), np.uint16), {0: "a"})
    (tmp_path / "bb_only.bb").write_text("0 0 0 2 2 1 box\n")
    (tmp_path / "empty_bb.bb").write_text("")
    records, labels = read_records(tmp_path)
    assert len(records) == 4
    assert labels == ["a", "box"]
    assert next(r for r in records if r.stem == "bb_only").width is None


@pytest.mark.parametrize("suffix", [".mask.hdr", ".mask.re4"])
def test_missing_half_of_pair_is_error(tmp_path, suffix):
    (tmp_path / ("x" + suffix)).write_bytes(b"test")
    with pytest.raises(FormatError, match="must both exist"):
        read_records(tmp_path)


def test_mask_image_size_mismatch(tmp_path):
    image(tmp_path / "a.png", 6, 4)
    write_mask(tmp_path / "a.mask.hdr", np.zeros((5, 7), np.uint16), {0: "a"})
    with pytest.raises(FormatError, match="size mismatch"):
        read_records(tmp_path)


def test_duplicate_image_stems(tmp_path):
    image(tmp_path / "a.png")
    image(tmp_path / "a.jpg")
    with pytest.raises(FormatError, match="share one"):
        read_records(tmp_path)


def test_per_image_bit_maps_are_interpreted_independently(tmp_path):
    write_mask(tmp_path / "a.mask.hdr", np.ones((5, 7), np.uint16), {0: "a"})
    write_mask(tmp_path / "b.mask.hdr", np.ones((5, 7), np.uint16), {0: "b"})
    _, labels = read_records(tmp_path)
    assert labels == ["a", "b"]


def test_manifest_retains_fully_empty_items(tmp_path):
    entries = [
        {"id": "a", "subset": "train", "image": "a.png", "width": 7, "height": 5}
    ]
    write_manifest(tmp_path, entries, ["unused"])
    records, labels = read_records(tmp_path)
    assert len(records) == 1 and records[0].image_path is None
    assert records[0].subset == "train" and labels == ["unused"]


def test_manifest_extra_file_rejected(tmp_path):
    write_manifest(
        tmp_path, [{"id": "a", "image": "a.png", "width": 7, "height": 5}], []
    )
    (tmp_path / "b.bb").write_text("")
    with pytest.raises(FormatError, match="not listed"):
        read_records(tmp_path)


@pytest.mark.parametrize(
    "value", ["../a", "/a", "a/../b", "a\\b", "C:/a", "a//b", "./a", ""]
)
def test_unsafe_paths(value):
    with pytest.raises(FormatError):
        safe_relative(value)


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "a/../../outside", "a\\..\\outside"]
)
def test_zip_traversal_rejected(tmp_path, name):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr(name, "x")
    stream.seek(0)
    with pytest.raises(FormatError):
        extract_zip(stream, tmp_path / "out")
    assert not (tmp_path / "outside").exists()


def test_zip_symlink_rejected(tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "../../outside")
    stream.seek(0)
    with pytest.raises(FormatError, match="link/special"):
        extract_zip(stream, tmp_path / "out")


def test_zip_duplicate_rejected(tmp_path):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w") as z:
            z.writestr("a.bb", "")
            z.writestr("a.bb", "")
    stream.seek(0)
    with pytest.raises(FormatError, match="Duplicate"):
        extract_zip(stream, tmp_path / "out")


def test_zip_wrapper_and_roundtrip(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    image(source / "folder/a.png")
    stream = io.BytesIO()
    make_zip(source, stream)
    stream.seek(0)
    root = extract_zip(stream, tmp_path / "out")
    records, _ = read_records(root)
    assert len(records) == 1 and records[0].stem == "a"


def test_task_matching_is_exact_or_unique_suffix():
    frames = {
        1: {"path": "patient1/a.png", "width": 7, "height": 5},
        2: {"path": "patient2/a.png", "width": 7, "height": 5},
    }
    assert resolve_frame(Record("patient1/a", "patient1/a"), frames)[0] == 1
    with pytest.raises(FormatError, match="found 2"):
        resolve_frame(Record("a", "a"), frames)
    assert resolve_frame(Record("a", "a"), {1: frames[1]})[0] == 1


def test_task_matching_does_not_use_numeric_indices():
    with pytest.raises(FormatError, match="found 0"):
        resolve_frame(
            Record("1", "1"), {1: {"path": "different.png", "width": 7, "height": 5}}
        )


def test_bb_only_dimension_check_uses_task(tmp_path):
    (tmp_path / "a.bb").write_text("0 0 0 10 10 1 a")
    records, _ = read_records(tmp_path)
    with pytest.raises(FormatError, match="outside"):
        resolve_frame(records[0], {0: {"path": "a.png", "width": 7, "height": 5}})


def test_task_size_mismatch_rejected():
    r = Record("a", "a", width=8, height=5)
    with pytest.raises(FormatError, match="mismatch"):
        resolve_frame(r, {0: {"path": "a.png", "width": 7, "height": 5}})


def test_archive_limits(tmp_path, monkeypatch):
    import itgformat_core.archive as archive_module

    monkeypatch.setattr(archive_module, "MAX_ARCHIVE_BYTES", 1)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("a.bb", "large")
    stream.seek(0)
    with pytest.raises(FormatError, match="limit"):
        extract_zip(stream, tmp_path / "out")


def test_different_hdr_pixel_widths_in_one_archive(tmp_path):
    widths = (1, 2, 4)
    for byte_count in widths:
        highest = byte_count * 8 - 1
        write_mask(
            tmp_path / f"a{byte_count}.mask.hdr",
            np.full((5, 7), 1 << highest, dtype=f"u{byte_count}"),
            {highest: f"class_{highest}"},
        )
    records, labels = read_records(tmp_path)
    assert [r.header.byte_count for r in records] == [1, 2, 4]
    assert set(labels) == {"class_7", "class_15", "class_31"}


def test_import_capacity_is_per_hdr_not_global_class_count(tmp_path):
    for index in range(5):
        bits = {bit: f"image_{index}_class_{bit}" for bit in range(8)}
        write_mask(
            tmp_path / f"a{index}.mask.hdr", np.full((2, 3), 255, np.uint8), bits
        )
    records, labels = read_records(tmp_path)
    assert len(records) == 5 and len(labels) == 40


def test_manifest_uses_itgformat_identity(tmp_path):
    write_manifest(
        tmp_path, [{"id": "a", "image": "a.png", "width": 7, "height": 5}], []
    )
    assert MANIFEST == "itgformat.json"
    assert json.loads((tmp_path / MANIFEST).read_text())["format"] == "ITGformat"
