# SPDX-License-Identifier: MIT
"""Synthetic conversion/registration/archive tests; no patient data or live CVAT."""

import io
import json
import stat
import tarfile
import weakref
import zipfile
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from components.dicom_import import DicomImportError
from components.dicom_import import conversion as c
from components.dicom_import.archive import MANIFEST, Budget, read_upload, relative_path, unpack
from components.dicom_import.registration import image_frames, missing_attributes, tag_batches


class Dataset(SimpleNamespace):
    def keys(self):
        return self.elements.keys()

    def get_item(self, key, keep_deferred=False):
        assert keep_deferred
        return self.elements[key]

    def __getitem__(self, key):
        element = self.elements[key]
        assert element.VR not in c.BINARY_VRS, "Excluded values must not be decoded"
        return element

    @property
    def pixel_array(self):
        if getattr(self, "fail_decode", False):
            raise AssertionError("Pixel decoding was not expected")
        if getattr(self, "change_on_correction", False) and self.HighBit == self.BitsStored - 1:
            return self.pixels + 1
        return self.pixels


def dataset(**overrides):
    values = dict(
        PatientID="SYNTHETIC",
        SeriesNumber="2",
        StudyDate="20260102",
        StudyTime="030405.12",
        InstanceNumber="3",
        SOPInstanceUID="1.2.345678901",
        StudyInstanceUID="1.2.300",
        Rows=4,
        Columns=6,
        BitsAllocated=16,
        BitsStored=12,
        HighBit=11,
        PixelRepresentation=0,
        SamplesPerPixel=1,
        pixels=np.arange(24, dtype=np.uint16).reshape(4, 6),
        elements={},
    )
    values.update(overrides)
    return Dataset(**values)


def files(tmp_path, count):
    result = []
    for n in range(count):
        path = tmp_path / f"{n}.dcm"
        path.write_bytes(b"synthetic header supplied by test double")
        result.append(path)
    return result


def zip_bytes(entries):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    result.seek(0)
    return result


def envelope(entries):
    return zip_bytes([(MANIFEST, json.dumps({"version": 1, "prefix": "GRD"})), *entries])


def test_reference_names():
    ds = dataset()
    assert c.names(ds, "GRD", False) == (
        "GRD_SYNTHETIC_20260102_1.2.300",
        "GRD_SYNTHETIC_2_20260102_030405.12_0003_45678901.png",
    )
    assert c.names(ds, "GRD", True)[0] == c.names(ds, "GRD", True)[1][:-4]
    assert c.component(" a b:/ ", "test") == "a b__"


@pytest.mark.parametrize(
    "field",
    [
        "PatientID",
        "SeriesNumber",
        "StudyDate",
        "StudyTime",
        "InstanceNumber",
        "SOPInstanceUID",
        "StudyInstanceUID",
    ],
)
def test_missing_naming_field(field):
    with pytest.raises(DicomImportError, match="Missing"):
        c.names(dataset(**{field: ""}), "GRD", False)


def test_long_names_rejected_not_truncated():
    with pytest.raises(DicomImportError, match="too long"):
        c.names(dataset(PatientID="x" * 250), "GRD", False)


@pytest.mark.parametrize(
    "values,dtype,expected",
    [
        ([0, 255], "uint16", [0, 255]),
        ([0, 4095], "uint16", [0, 4095]),
        ([-100, 50], "int16", [0, 150]),
        ([-32768, 32767], "int16", [0, 65535]),
    ],
)
def test_png_reference_conversion(values, dtype, expected):
    image = np.array([values], dtype=dtype)
    result = cv2.imdecode(np.frombuffer(c.encode_png(image), dtype="uint8"), cv2.IMREAD_UNCHANGED)
    assert result.tolist() == [expected]
    assert result.dtype == (np.uint8 if min(values) >= 0 and max(values) <= 255 else np.uint16)


def test_png_rejects_overflow_and_float():
    for arr in [np.array([0, 70000]), np.array([0.5, 1.5])]:
        with pytest.raises(DicomImportError):
            c.png_ready(arr)


def test_png_rgb_order():
    rgb = np.zeros((4, 6, 3), dtype=np.uint8)
    rgb[:, :, 0] = 201
    bgr = cv2.imdecode(np.frombuffer(c.encode_png(rgb), dtype="uint8"), cv2.IMREAD_UNCHANGED)
    assert bgr[0, 0].tolist() == [0, 0, 201]


def test_high_bit_identical_only():
    ds = dataset(HighBit=15)
    pixels, checked = c.decode_pixels(ds)
    assert checked and np.array_equal(pixels, ds.pixels) and ds.HighBit == 15
    with pytest.raises(DicomImportError, match="changes"):
        c.decode_pixels(dataset(HighBit=15, change_on_correction=True))
    with pytest.raises(DicomImportError, match="out-of-range"):
        c.decode_pixels(dataset(HighBit=15, pixels=np.full((4, 6), 5000, dtype="uint16")))


def test_decode_shape_and_size():
    with pytest.raises(DicomImportError, match="array"):
        c.decode_pixels(dataset(pixels=np.zeros((2, 2), dtype="uint8")))
    with pytest.raises(DicomImportError, match="limit"):
        c.decode_pixels(dataset(Rows=100000, Columns=100000, fail_decode=True))


@pytest.mark.parametrize(
    "options,expected",
    [
        ({"FrameTime": 40, "CineRate": 50}, 25),
        ({"FrameTimeVector": [0, 20, 40]}, 1000 / 30),
        ({"CineRate": 60, "RecommendedDisplayFrameRate": 24}, 60),
        ({"RecommendedDisplayFrameRate": 24}, 24),
        ({}, 30),
    ],
)
def test_frame_rate(options, expected):
    assert c.frame_rate(dataset(**options)) == pytest.approx(expected)


def test_metadata_top_level_text_and_multivalue():
    element = lambda vr, value: SimpleNamespace(VR=vr, value=value)
    ds = dataset(
        elements={
            0x100020: element("LO", " SYNTHETIC "),
            0x280030: element("DS", ["0.5000", "0.75"]),
            0x280008: element("IS", 3),
            0x80008: element("CS", ["ORIGINAL", "PRIMARY"]),
            0x7FE00010: element("OW", b"pixels"),
            0x81111: element("SQ", ["nested"]),
            0x111010: element("UN", b"private"),
            0x111011: element("LO", b"binary"),
        }
    )
    assert c.metadata(ds) == {
        "00100020": " SYNTHETIC ",
        "00280030": "0.5000\\0.75",
        "00280008": "3",
        "00080008": "ORIGINAL\\PRIMARY",
    }


@pytest.mark.parametrize("text", ["x" * 4097, "nul\x00value"])
def test_unrepresentable_metadata_not_silently_truncated(text):
    with pytest.raises(DicomImportError):
        c.metadata(dataset(elements={0x100020: SimpleNamespace(VR="LO", value=text)}))


def test_prepare_dedup_is_task_scoped_and_no_saved_hash(tmp_path):
    inputs = files(tmp_path, 3)
    data = {
        inputs[0]: dataset(InstanceNumber="2"),
        inputs[1]: dataset(InstanceNumber="1"),
        inputs[2]: dataset(StudyInstanceUID="1.2.301"),
    }
    plan = c.prepare(inputs, tmp_path / "output", "GRD", reader=data.__getitem__)
    assert len(plan.tasks) == 2 and plan.duplicates == 1
    assert plan.tasks[0].frames == 1 and "_0001_" in plan.tasks[0].media[0].name
    assert all(path.suffix == ".png" for path in (tmp_path / "output").rglob("*") if path.is_file())


def test_existing_task_is_skipped_before_decode(tmp_path):
    inputs = files(tmp_path, 1)
    ds = dataset(fail_decode=True)
    plan = c.prepare(
        inputs, tmp_path / "output", "GRD", [c.names(ds, "GRD", False)[0]], reader=lambda _: ds
    )
    assert not plan.tasks and len(plan.skipped_tasks) == 1


def test_converted_datasets_are_released_before_next_decode(tmp_path, monkeypatch):
    inputs = files(tmp_path, 3)
    references = []

    def reader(path):
        index = inputs.index(path)
        ds = dataset(InstanceNumber=index, pixels=np.full((4, 6), index, dtype="uint16"))
        references.append(weakref.ref(ds))
        return ds

    decode = c.decode_pixels

    def decode_one(ds):
        assert sum(reference() is not None for reference in references) == 1
        return decode(ds)

    monkeypatch.setattr(c, "decode_pixels", decode_one)
    plan = c.prepare(inputs, tmp_path / "output", "GRD", reader=reader)
    assert plan.tasks[0].frames == 3
    assert all(reference() is None for reference in references)


def test_output_name_collision_is_not_suffixed(tmp_path):
    inputs = files(tmp_path, 2)
    data = {inputs[0]: dataset(), inputs[1]: dataset(pixels=np.ones((4, 6), dtype="uint16"))}
    with pytest.raises(DicomImportError, match="same output name"):
        c.prepare(inputs, tmp_path / "output", "GRD", reader=data.__getitem__)


def test_identical_input_with_same_name_is_deduplicated(tmp_path):
    plan = c.prepare(files(tmp_path, 2), tmp_path / "output", "GRD", reader=lambda _: dataset())
    assert len(plan.tasks) == 1 and plan.tasks[0].frames == 1 and plan.duplicates == 1


def test_video_repeated_frames_retained(tmp_path):
    inputs = files(tmp_path, 1)
    ds = dataset(NumberOfFrames=3, pixels=np.zeros((3, 4, 6), dtype="uint8"))
    plan = c.prepare(inputs, tmp_path / "output", "GRD", reader=lambda _: ds)
    assert plan.duplicates == 0 and plan.tasks[0].frames == 3 and plan.tasks[0].video
    cap = cv2.VideoCapture(str(plan.tasks[0].media[0]))
    try:
        assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 3
    finally:
        cap.release()


def test_odd_video_not_cropped(tmp_path):
    with pytest.raises(DicomImportError, match="even"):
        c.write_mp4(tmp_path / "test.mp4", np.zeros((3, 5, 7), dtype="uint8"), dataset())


@pytest.mark.parametrize(
    "name", ["../outside", "/outside", "C:/outside", "a\\b", "a/../b", "a//b", "a\x00b"]
)
def test_archive_paths(name):
    with pytest.raises(DicomImportError):
        relative_path(name)


def test_nested_archive_and_prefix(tmp_path):
    nested = zip_bytes([("folder/a.dcm", b"synthetic")]).getvalue()
    prefix, items = read_upload(envelope([("data.zip", nested)]), tmp_path)
    assert prefix == "GRD" and len(items) == 1 and items[0].read_bytes() == b"synthetic"


def test_archive_case_duplicates_rejected(tmp_path):
    with pytest.raises(DicomImportError, match="Duplicate"):
        unpack(zip_bytes([("a", b"1"), ("A", b"2")]), tmp_path / "unpack", Budget())


def test_archive_byte_and_entry_limits(tmp_path):
    for index, budget in enumerate([Budget(bytes_left=1), Budget(files_left=0)]):
        with pytest.raises(DicomImportError):
            unpack(zip_bytes([("a", b"123")]), tmp_path / str(index), budget)


def test_zip_symlink_rejected(tmp_path):
    item = zipfile.ZipInfo("link")
    item.create_system = 3
    item.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(DicomImportError, match="special"):
        unpack(zip_bytes([(item, b"../outside")]), tmp_path / "unpack", Budget())


def test_tar_symlink_rejected(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../outside"
        archive.addfile(info)
    buffer.seek(0)
    with pytest.raises(DicomImportError, match="Links"):
        unpack(buffer, tmp_path / "unpack", Budget())


def test_manifest_required(tmp_path):
    with pytest.raises(DicomImportError, match="prefix"):
        read_upload(zip_bytes([("a.dcm", b"file")]), tmp_path)


def test_archive_nesting_limited(tmp_path):
    content = zip_bytes([("a.dcm", b"data")]).getvalue()
    for index in range(3):
        content = zip_bytes([(f"{index}.zip", content)]).getvalue()
    with pytest.raises(DicomImportError, match="nesting"):
        read_upload(envelope([("root.zip", content)]), tmp_path)


def test_attributes_are_added_without_overwriting_existing():
    original = {"type": "tag", "attributes": [{"name": "00100020", "input_type": "text", "id": 5}]}
    additions = missing_attributes(original, {"00100020", "00280030"})
    assert [x["name"] for x in additions] == ["00280030"]
    assert original["attributes"][0]["id"] == 5 and additions[0]["mutable"] is False
    for conflict in [
        {"type": "rectangle"},
        {"type": "tag", "attributes": [{"name": "00100020", "input_type": "number"}]},
    ]:
        with pytest.raises(DicomImportError):
            missing_attributes(conflict, {"00100020"})


@pytest.mark.parametrize("input_type", ["number", "select"])
def test_existing_nontext_attributes_are_rejected_even_when_absent_from_input(input_type):
    existing = {
        "type": "tag",
        "attributes": [
            {"name": "00100020", "input_type": "text"},
            {"name": "00280030", "input_type": input_type},
        ],
    }
    with pytest.raises(DicomImportError, match="00280030 is not text"):
        missing_attributes(existing, {"00100020"})


def test_frame_mapping_uses_actual_cvat_paths():
    tags = {"a.png": {"key": "a"}, "b.png": {"key": "b"}}
    assert list(image_frames(tags, [("dir/b.png", 0), ("dir/a.png", 1)])) == [
        (0, tags["b.png"]),
        (1, tags["a.png"]),
    ]
    with pytest.raises(DicomImportError):
        list(image_frames(tags, [("a.png", 0)]))


def test_tag_batches_are_append_only_and_bounded():
    batches = list(tag_batches(((n, {"a": "value"}) for n in range(205)), 3, {"a": 7}))
    assert [len(b["tags"]) for b in batches] == [100, 100, 5]
    assert all(b["shapes"] == [] and b["tracks"] == [] for b in batches)
    assert batches[-1]["tags"][-1]["frame"] == 204


def test_missing_elements_do_not_inherit_project_attribute_defaults():
    ids = {"00100020": 7, "0020000D": 8, "00280030": 9}
    (batch,) = tag_batches([(0, {"00100020": "SYNTHETIC"})], 3, ids)
    assert batch["tags"][0]["attributes"] == [
        {"spec_id": 7, "value": "SYNTHETIC"},
        {"spec_id": 8, "value": ""},
        {"spec_id": 9, "value": ""},
    ]


def test_batch_bound_counts_missing_attributes_as_well():
    ids = {str(index): index for index in range(101)}
    batches = list(tag_batches(((index, {}) for index in range(100)), 3, ids))
    assert [len(batch["tags"]) for batch in batches] == [99, 1]


def test_person_name_is_text_not_an_iterable_or_encoded_bytes():
    class PersonName:
        original_string = b"encoded representation"

        def __str__(self):
            return "Synthetic^Name"

        def __iter__(self):
            return iter(str(self))

    assert c._text(PersonName()) == "Synthetic^Name"
    assert c._text(SimpleNamespace(original_string="0.5000")) == "0.5000"


def test_tar_style_dot_prefix_is_normalized_safely():
    assert relative_path("./folder/a.dcm") == Path("folder/a.dcm")


def test_ambiguous_numeric_vr_is_not_discarded():
    ds = dataset(elements={0x280120: SimpleNamespace(VR="US or SS", value=-1)})
    assert c.metadata(ds) == {"00280120": "-1"}
