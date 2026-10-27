# SPDX-License-Identifier: MIT
"""Run real DICOM decoding when pydicom is installed. Synthetic data only."""

import numpy as np
import pytest

pydicom = pytest.importorskip("pydicom")
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

from components.dicom_import import DicomImportError
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
