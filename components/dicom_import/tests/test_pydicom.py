# SPDX-License-Identifier: MIT
"""Run real DICOM decoding when pydicom is installed. Synthetic data only."""

import builtins
import io
import itertools
import os
import struct
from contextlib import closing
from pathlib import Path

import cv2
import numpy as np
import pytest

pydicom = pytest.importorskip("pydicom")
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    RLELossless,
    SecondaryCaptureImageStorage,
)

from components.dicom_import import DicomImportError, conversion
from components.dicom_import.conversion import decode_pixels, prepare


def save(path, instance, pixels, *, study="1.2.300", high_bit=11):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = f"1.2.301.{instance}"
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID = "SYNTHETIC"
    ds.StudyDate = "20260102"
    ds.StudyTime = "030405"
    ds.SeriesNumber = 2
    ds.InstanceNumber = instance
    ds.StudyInstanceUID = study
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = pixels.shape[-2:]
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = high_bit
    ds.PixelRepresentation = 0
    if pixels.ndim == 3:
        ds.NumberOfFrames = len(pixels)
    ds.PixelData = pixels.astype("<u2").tobytes()
    nested = Dataset()
    nested.PatientID = "NOT_FLATTENED"
    ds.ReferencedStudySequence = [nested]
    ds.save_as(path, enforce_file_format=True)
    return path


def save_movie(path, pixels, *, instance=1, bits_stored=None, high_bit=None):
    height, width = pixels.shape[1:3]
    save(path, instance, np.zeros((height, width), dtype=np.uint16))
    ds = pydicom.dcmread(path)
    ds.NumberOfFrames = len(pixels)
    ds.SamplesPerPixel = pixels.shape[-1] if pixels.ndim == 4 else 1
    ds.PhotometricInterpretation = "RGB" if pixels.ndim == 4 else "MONOCHROME2"
    if ds.SamplesPerPixel > 1:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = pixels.dtype.itemsize * 8
    ds.BitsStored = bits_stored or ds.BitsAllocated
    ds.HighBit = ds.BitsStored - 1 if high_bit is None else high_bit
    ds.PixelRepresentation = int(pixels.dtype.kind == "i")
    ds.FrameTime = 40
    ds.PixelData = pixels.astype(pixels.dtype.newbyteorder("<")).tobytes()
    ds.save_as(path, enforce_file_format=True)
    return path


def guard_source_reads(monkeypatch, sources, *, maximum=1024**2):
    """Reject whole-value reads while allowing real seeks and frame decoding."""
    sources = {Path(path).resolve() for path in sources}
    reads = []

    class BoundedReads(io.BufferedReader):
        def __init__(self, stream):
            super().__init__(stream)

        def read(self, size=-1):
            assert 0 <= size <= maximum, f"Unbounded DICOM read: {size}"
            reads.append(size)
            return super().read(size)

        def readinto(self, buffer):
            assert len(buffer) <= maximum
            reads.append(len(buffer))
            return super().readinto(buffer)

    def guard(open_file):
        def open_bounded(file, mode="r", *args, **kwargs):
            stream = open_file(file, mode, *args, **kwargs)
            if (
                "r" in mode
                and "b" in mode
                and isinstance(file, (str, os.PathLike))
                and Path(file).resolve() in sources
            ):
                return BoundedReads(stream)
            return stream

        return open_bounded

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))

    def reject_full_array(_ds):
        pytest.fail("Production conversion must not decode the complete pixel_array")

    monkeypatch.setattr(Dataset, "pixel_array", property(reject_full_array))
    return reads


def video_frames(path):
    capture = cv2.VideoCapture(str(path))
    frames = []
    try:
        assert capture.isOpened()
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        capture.release()
    return frames


def test_real_decode_dedup_metadata_and_high_bit(tmp_path):
    pixels = np.arange(24, dtype=np.uint16).reshape(4, 6) * 100
    inputs = [save(tmp_path / "a.dcm", 1, pixels, high_bit=15), save(tmp_path / "b.dcm", 2, pixels)]
    plan = prepare(inputs, tmp_path / "output", "GRD")
    assert plan.duplicates == 1 and plan.high_bit_checked == 1
    assert plan.tasks[0].frames == 1
    values = next(iter(plan.tasks[0].tags.values()))
    assert values["PatientID"] == "SYNTHETIC"
    assert values["HighBit"] == "15"
    assert "PixelData" not in values and "ReferencedStudySequence" not in values


def test_real_multiframe_is_one_video_task(tmp_path):
    pixels = np.zeros((3, 4, 6), dtype=np.uint16)
    plan = prepare([save(tmp_path / "video.dcm", 1, pixels)], tmp_path / "output", "GRD")
    assert len(plan.tasks) == 1 and plan.tasks[0].video and plan.tasks[0].frames == 3


def test_palette_indices_are_never_saved_as_greyscale(tmp_path):
    path = save(tmp_path / "palette.dcm", 1, np.zeros((4, 6), dtype=np.uint16))
    ds = pydicom.dcmread(path)
    ds.PhotometricInterpretation = "PALETTE COLOR"
    ds.RedPaletteColorLookupTableDescriptor = [1, 0, 16]
    ds.GreenPaletteColorLookupTableDescriptor = [1, 0, 16]
    ds.BluePaletteColorLookupTableDescriptor = [1, 0, 16]
    ds.RedPaletteColorLookupTableData = b"\xff\xff"
    ds.GreenPaletteColorLookupTableData = b"\x00\x00"
    ds.BluePaletteColorLookupTableData = b"\x00\x00"
    # A zero-valued pixel means red here; encoding the index would make it black.
    assert not ds.pixel_array.any()
    with pytest.raises(DicomImportError, match="PALETTE COLOR"):
        decode_pixels(ds)


def test_source_larger_than_one_gib_is_streamed_through_real_video_conversion(
    tmp_path, monkeypatch
):
    pixels = np.arange(3 * 4 * 6, dtype=np.uint16).reshape(3, 4, 6)
    source = save_movie(tmp_path / "large.dcm", pixels)
    ds = pydicom.dcmread(source)
    del ds.PixelData
    ds.save_as(source, enforce_file_format=True)
    with source.open("r+b") as stream:
        stream.seek(0, os.SEEK_END)
        # A valid, deferred binary value makes the source larger than the old
        # limit without writing or allocating a gigabyte of test data.
        size = 1024**3 + 4096
        stream.write(struct.pack("<HH2sHI", 0x1111, 0x1010, b"OB", 0, size))
        stream.seek(size - 1, os.SEEK_CUR)
        stream.write(b"\0")
        stream.write(struct.pack("<HH2sHI", 0x7FE0, 0x0010, b"OW", 0, pixels.nbytes))
        stream.write(pixels.tobytes())
    assert source.stat().st_size > 1024**3
    reads = guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.failed_tasks
    assert len(plan.tasks) == 1 and plan.tasks[0].frames == 3
    assert len(video_frames(plan.tasks[0].media[0])) == 3
    assert sum(reads) < 1024**2


def test_movie_decoded_total_larger_than_one_gib_is_not_materialized(tmp_path, monkeypatch):
    source = save_movie(tmp_path / "long.dcm", np.zeros((2, 256, 256), dtype=np.uint16))
    ds = pydicom.dcmread(source)
    del ds.PixelData
    ds.NumberOfFrames = 8193
    ds.save_as(source, enforce_file_format=True)
    size = int(ds.NumberOfFrames) * int(ds.Rows) * int(ds.Columns) * 2
    assert size > 1024**3
    with source.open("r+b") as stream:
        stream.seek(0, os.SEEK_END)
        stream.write(struct.pack("<HH2sHI", 0x7FE0, 0x0010, b"OW", 0, size))
        stream.seek(size - 1, os.SEEK_CUR)
        stream.write(b"\0")
    reads = guard_source_reads(monkeypatch, [source])
    header = conversion._read_dicom(source)
    with closing(conversion.iter_dicom_frames(source, header)) as frames:
        observed = list(itertools.islice(frames, 3))
    assert len(observed) == 3
    assert all(frame.shape == (256, 256) and frame.dtype == np.uint16 for frame in observed)
    assert all(not frame.any() for frame in observed)
    assert sum(reads) < 1024**2


@pytest.mark.parametrize("declared_length", [2, 3 * 1024**3])
def test_invalid_pixel_length_fails_only_its_task_without_large_read(
    tmp_path, monkeypatch, declared_length
):
    bad = save(tmp_path / "bad.dcm", 1, np.zeros((2, 2), dtype=np.uint16))
    good = save(tmp_path / "good.dcm", 2, np.zeros((2, 2), dtype=np.uint16), study="1.2.999")
    ds = pydicom.dcmread(bad)
    del ds.PixelData
    ds.save_as(bad, enforce_file_format=True)
    with bad.open("ab") as stream:
        stream.write(struct.pack("<HH2sHI", 0x7FE0, 0x0010, b"OW", 0, declared_length))
        stream.write(b"\0" * 8)
    guard_source_reads(monkeypatch, [bad, good])
    plan = prepare([bad, good], tmp_path / "output", "GRD")
    assert [task.name for task in plan.tasks] == ["GRD_SYNTHETIC_20260102_12999"]
    assert set(plan.failed_tasks) == {"GRD_SYNTHETIC_20260102_12300"}
    assert len(list((tmp_path / "output").rglob("*.png"))) == 1


def test_normal_undefined_length_compressed_pixels_remain_supported(tmp_path):
    pixels = np.arange(24, dtype=np.uint16).reshape(4, 6)
    path = save(tmp_path / "compressed.dcm", 1, pixels)
    ds = pydicom.dcmread(path)
    ds.compress(RLELossless)
    ds.save_as(path, enforce_file_format=True)
    deferred = pydicom.dcmread(path, defer_size=1)
    assert deferred.get_item(0x7FE00010, keep_deferred=True).length == 0xFFFFFFFF
    decoded, checked = decode_pixels(deferred)
    assert not checked and np.array_equal(decoded, pixels)
    assert "PixelData" not in conversion._read_dicom(path, headers=True)
    plan = prepare([path], tmp_path / "output", "GRD")
    assert len(plan.tasks) == 1 and plan.tasks[0].frames == 1


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("dtype", ["uint8", "uint16", "int16"])
def test_native_and_rle_movies_decode_one_frame_at_a_time(tmp_path, monkeypatch, compressed, dtype):
    pixels = np.arange(3 * 4 * 6).reshape(3, 4, 6).astype(dtype)
    if dtype == "int16":
        pixels -= 35
    source = save_movie(tmp_path / "frames.dcm", pixels)
    if compressed:
        ds = pydicom.dcmread(source)
        ds.compress(RLELossless)
        ds.save_as(source, enforce_file_format=True)
    guard_source_reads(monkeypatch, [source])
    ds = conversion._read_dicom(source)
    actual = list(conversion.iter_dicom_frames(source, ds))
    assert len(actual) == len(pixels)
    for decoded, expected in zip(actual, pixels, strict=True):
        assert np.array_equal(decoded, expected)
        assert decoded.dtype == expected.dtype


def test_frame_limit_applies_to_one_frame_and_can_be_configured(tmp_path, monkeypatch):
    source = save_movie(tmp_path / "frames.dcm", np.zeros((3, 4, 6), dtype=np.uint16))
    guard_source_reads(monkeypatch, [source])
    monkeypatch.setenv("CVAT_DICOM_MAX_FRAME_BYTES", "47")
    rejected = prepare([source], tmp_path / "rejected", "GRD")
    assert not rejected.tasks and len(rejected.failed_tasks) == 1
    assert "frame" in next(iter(rejected.failed_tasks.values())).lower()
    monkeypatch.setenv("CVAT_DICOM_MAX_FRAME_BYTES", "48")
    accepted = prepare([source], tmp_path / "accepted", "GRD")
    assert not accepted.failed_tasks
    assert len(accepted.tasks) == 1 and accepted.tasks[0].frames == 3


@pytest.mark.parametrize(
    "dtype,levels", [("uint16", [1000, 2000, 3000]), ("int16", [-1000, 0, 1000])]
)
def test_streamed_video_keeps_global_intensity_scale(tmp_path, monkeypatch, dtype, levels):
    pixels = np.array(levels, dtype=dtype)[:, None, None] * np.ones((3, 16, 16), dtype=dtype)
    source = save_movie(tmp_path / "levels.dcm", pixels)
    guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.failed_tasks and len(plan.tasks) == 1
    frames = video_frames(plan.tasks[0].media[0])
    assert len(frames) == 3
    # Check the actual lossy video rather than just the encoder inputs. A
    # per-frame normalization would turn every constant frame into black.
    assert [float(frame.mean()) for frame in frames] == pytest.approx([0, 128, 255], abs=4)


@pytest.mark.parametrize("compressed", [False, True])
def test_streamed_rgb_video_keeps_channel_order(tmp_path, monkeypatch, compressed):
    pixels = np.zeros((3, 16, 16, 3), dtype=np.uint8)
    for index in range(3):
        pixels[index, :, :, index] = 240
    source = save_movie(tmp_path / "rgb.dcm", pixels)
    if compressed:
        ds = pydicom.dcmread(source)
        ds.compress(RLELossless)
        ds.save_as(source, enforce_file_format=True)
    guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.failed_tasks and len(plan.tasks) == 1
    frames = video_frames(plan.tasks[0].media[0])
    assert len(frames) == 3
    for index, frame in enumerate(frames):
        expected = [0, 0, 0]
        expected[2 - index] = 240  # OpenCV reads BGR.
        assert frame.mean(axis=(0, 1)) == pytest.approx(expected, abs=5)


def test_multiframe_high_bit_is_checked_and_original_metadata_is_kept(tmp_path, monkeypatch):
    pixels = np.arange(3 * 4 * 6, dtype=np.uint16).reshape(3, 4, 6) * 50
    source = save_movie(tmp_path / "high-bit.dcm", pixels, bits_stored=12, high_bit=15)
    guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.failed_tasks and len(plan.tasks) == 1
    assert plan.high_bit_checked == 1
    assert next(iter(plan.tasks[0].tags.values()))["HighBit"] == "15"


@pytest.mark.parametrize("implicit_vr", [False, True])
def test_movie_without_preamble_or_file_meta_uses_original_encoding(
    tmp_path, monkeypatch, implicit_vr
):
    pixels = np.arange(3 * 4 * 6, dtype=np.uint16).reshape(3, 4, 6)
    source = save_movie(tmp_path / "bare.dcm", pixels)
    ds = pydicom.dcmread(source)
    ds.file_meta = FileMetaDataset()
    ds.preamble = None
    pydicom.dcmwrite(source, ds, implicit_vr=implicit_vr, little_endian=True)
    guard_source_reads(monkeypatch, [source])
    header = conversion._read_dicom(source)
    assert header.original_encoding == (implicit_vr, True)
    decoded = list(conversion.iter_dicom_frames(source, header))
    assert np.array_equal(np.array(decoded), pixels)


def test_metadata_after_pixel_data_is_preserved(tmp_path, monkeypatch):
    from components.dicom_import.tag_names import attribute_name

    source = save_movie(tmp_path / "metadata.dcm", np.zeros((3, 4, 6), dtype=np.uint8))
    ds = pydicom.dcmread(source)
    ds.add_new(0x80011010, "LO", "SYNTHETIC TRAILING TEXT")
    ds.save_as(source, enforce_file_format=True)
    guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.failed_tasks and len(plan.tasks) == 1
    assert next(iter(plan.tasks[0].tags.values()))[attribute_name(0x80011010)] == (
        "SYNTHETIC TRAILING TEXT"
    )


def test_oversized_deferred_text_is_rejected_before_materialization(tmp_path, monkeypatch):
    source = save(tmp_path / "huge-text.dcm", 1, np.zeros((2, 2), dtype=np.uint16))
    ds = pydicom.dcmread(source)
    del ds.PixelData
    ds.save_as(source, enforce_file_format=True)
    with source.open("r+b") as stream:
        stream.seek(0, os.SEEK_END)
        size = 1024**3 + 4096
        stream.write(struct.pack("<HH2sHI", 0x1111, 0x1010, b"UT", 0, size))
        stream.seek(size - 1, os.SEEK_CUR)
        stream.write(b"\0")
        stream.write(struct.pack("<HH2sHI", 0x7FE0, 0x0010, b"OW", 0, 8))
        stream.write(b"\0" * 8)
    reads = guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.tasks
    assert list(plan.failed_tasks.values()) == ["Unrepresentable text attribute 11111010"]
    assert sum(reads) < 1024**2


def test_native_bitpacked_frames_can_cross_byte_boundaries(tmp_path, monkeypatch):
    from pydicom.pixels import pack_bits

    pixels = np.array([[[0, 1], [1, 0]], [[1, 1], [0, 0]], [[0, 0], [1, 1]]], dtype=np.uint8)
    source = save_movie(tmp_path / "bits.dcm", pixels)
    ds = pydicom.dcmread(source)
    ds.BitsAllocated = ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelData = pack_bits(pixels.reshape(-1))
    ds.save_as(source, enforce_file_format=True)
    guard_source_reads(monkeypatch, [source])
    ds = conversion._read_dicom(source)
    decoded = list(conversion.iter_dicom_frames(source, ds))
    assert np.array_equal(np.array(decoded), pixels)


def test_native_subsampled_ybr_frames_keep_rgb_conversion(tmp_path, monkeypatch):
    source = save_movie(tmp_path / "ybr.dcm", np.zeros((3, 2, 2, 3), dtype=np.uint8))
    ds = pydicom.dcmread(source)
    ds.PhotometricInterpretation = "YBR_FULL_422"
    ds.PixelData = bytes([50, 60, 128, 128, 70, 80, 128, 128]) * 3
    ds.save_as(source, enforce_file_format=True)
    expected = ds.pixel_array.copy()
    guard_source_reads(monkeypatch, [source])
    decoded = list(conversion.iter_dicom_frames(source, conversion._read_dicom(source)))
    assert np.array_equal(np.array(decoded), expected)


def test_bad_late_compressed_frame_discards_partial_video_but_keeps_other_study(
    tmp_path, monkeypatch
):
    from pydicom.encaps import encapsulate, generate_frames

    bad = save_movie(tmp_path / "broken.dcm", np.zeros((3, 16, 16), dtype=np.uint8))
    ds = pydicom.dcmread(bad)
    ds.compress(RLELossless)
    encoded = list(generate_frames(ds.PixelData, number_of_frames=3))
    ds.PixelData = encapsulate([encoded[0], b"\0" * 8, encoded[2]])
    ds.save_as(bad, enforce_file_format=True)
    good = save(tmp_path / "good.dcm", 2, np.zeros((2, 2), dtype=np.uint16), study="1.2.999")
    guard_source_reads(monkeypatch, [bad, good])
    plan = prepare([bad, good], tmp_path / "output", "GRD")
    assert [task.name for task in plan.tasks] == ["GRD_SYNTHETIC_20260102_12999"]
    assert len(plan.failed_tasks) == 1
    assert not list((tmp_path / "output").rglob("*.mp4"))


@pytest.mark.parametrize("declared_frames", [2, 4])
def test_compressed_frame_count_mismatch_does_not_create_a_partial_task(
    tmp_path, monkeypatch, declared_frames
):
    source = save_movie(tmp_path / "count.dcm", np.zeros((3, 16, 16), dtype=np.uint8))
    ds = pydicom.dcmread(source)
    ds.compress(RLELossless)
    ds.NumberOfFrames = declared_frames
    ds.save_as(source, enforce_file_format=True)
    guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.tasks and len(plan.failed_tasks) == 1
    assert not list((tmp_path / "output").rglob("*.mp4"))


@pytest.mark.parametrize("high_bit", [11, 15])
def test_deflated_movie_uses_streaming_inflate_and_cleans_temporary_data(
    tmp_path, monkeypatch, high_bit
):
    import zlib

    source = save_movie(
        tmp_path / "deflated.dcm",
        np.zeros((3, 4, 6), dtype=np.uint16),
        bits_stored=12,
        high_bit=high_bit,
    )
    ds = pydicom.dcmread(source)
    ds.file_meta.TransferSyntaxUID = DeflatedExplicitVRLittleEndian
    ds.save_as(source, enforce_file_format=True)
    guard_source_reads(monkeypatch, [source])
    monkeypatch.setattr(
        zlib,
        "decompress",
        lambda *_args, **_kwargs: pytest.fail("Deflated datasets must not be inflated in memory"),
    )
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.failed_tasks and len(plan.tasks) == 1
    assert plan.high_bit_checked == int(high_bit == 15)
    assert len(video_frames(plan.tasks[0].media[0])) == 3
    values = next(iter(plan.tasks[0].tags.values()))
    assert values["TransferSyntaxUID"] == str(DeflatedExplicitVRLittleEndian)
    assert [path for path in (tmp_path / "output").rglob("*") if path.is_file()] == plan.tasks[
        0
    ].media


def test_deflated_deferred_text_remains_readable_until_conversion_finishes(tmp_path, monkeypatch):
    source = save_movie(tmp_path / "deflated-text.dcm", np.zeros((3, 4, 6), dtype=np.uint16))
    ds = pydicom.dcmread(source)
    ds.TextValue = ("SYNTHETIC " * 200).rstrip()
    ds.file_meta.TransferSyntaxUID = DeflatedExplicitVRLittleEndian
    ds.save_as(source, enforce_file_format=True)
    guard_source_reads(monkeypatch, [source])
    plan = prepare([source], tmp_path / "output", "GRD")
    assert not plan.failed_tasks and len(plan.tasks) == 1
    assert next(iter(plan.tasks[0].tags.values()))["TextValue"] == ds.TextValue


def test_deflated_header_is_lazy_and_expansion_limit_cleans_temporary_file(tmp_path, monkeypatch):
    from components.dicom_import.source import open_dicom

    source = save_movie(tmp_path / "deflated-budget.dcm", np.zeros((17, 256, 256), dtype=np.uint16))
    ds = pydicom.dcmread(source)
    ds.file_meta.TransferSyntaxUID = DeflatedExplicitVRLittleEndian
    ds.save_as(source, enforce_file_format=True)
    work = tmp_path / "temporary"
    work.mkdir()
    guard_source_reads(monkeypatch, [source])
    monkeypatch.setenv("CVAT_DICOM_MAX_EXPANDED_BYTES", str(1024**2))
    with open_dicom(source, headers=True, temp_dir=work) as (normalized, header):
        assert normalized.exists()
        assert int(header.NumberOfFrames) == 17
        assert "PixelData" not in header
    assert not list(work.iterdir())
    with pytest.raises(DicomImportError, match="CVAT_DICOM_MAX_EXPANDED_BYTES"):
        with open_dicom(source, temp_dir=work):
            pytest.fail("The complete deflated movie exceeds the configured expansion budget")
    assert not list(work.iterdir())


@pytest.mark.parametrize("corruption", ["truncated", "trailing"])
def test_invalid_deflated_dataset_cleans_temporary_file(tmp_path, monkeypatch, corruption):
    from components.dicom_import.source import open_dicom

    source = save_movie(tmp_path / "deflated-bad.dcm", np.zeros((3, 16, 16), dtype=np.uint16))
    ds = pydicom.dcmread(source)
    ds.file_meta.TransferSyntaxUID = DeflatedExplicitVRLittleEndian
    ds.save_as(source, enforce_file_format=True)
    with source.open("r+b") as stream:
        if corruption == "truncated":
            stream.truncate(source.stat().st_size - 3)
        else:
            stream.seek(0, os.SEEK_END)
            stream.write(b"unexpected trailing bytes")
    work = tmp_path / "temporary"
    work.mkdir()
    guard_source_reads(monkeypatch, [source])
    with pytest.raises(DicomImportError, match="deflated DICOM dataset"):
        with open_dicom(source, temp_dir=work):
            pytest.fail("Invalid compressed data must not be accepted")
    assert not list(work.iterdir())


@pytest.mark.parametrize("extra_bytes", [1, 2])
def test_deflated_output_buffer_is_drained_after_compressed_input_ends(tmp_path, extra_bytes):
    import zlib

    from components.dicom_import.source import READ_BLOCK, _InflatedFile

    size = 2 * READ_BLOCK + extra_bytes
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(b"x" * size) + compressor.flush()
    source = tmp_path / "compressed.bin"
    source.write_bytes(compressed)
    with source.open("rb") as stream, (tmp_path / "inflated.bin").open("w+b") as target:
        target.write(b"header")
        reader = _InflatedFile(stream, target, size)
        assert reader.seek(0, os.SEEK_END) == size + len(b"header")
        reader.seek(len(b"header"))
        total = 0
        while chunk := reader.read(READ_BLOCK):
            assert chunk == b"x" * len(chunk)
            total += len(chunk)
        assert total == size
