# SPDX-License-Identifier: MIT
"""Dictionary drift and readable, unambiguous DICOM attribute names."""

import json
from pathlib import Path

import pytest

from components.dicom_import.conversion import metadata
from components.dicom_import.tag_names import attribute_name


def test_pydicom_dictionary_matches_recorded_tag_keyword_correspondence():
    pytest.importorskip("pydicom")
    from pydicom.datadict import DicomDictionary, RepeatersDictionary, tag_for_keyword

    snapshot = json.loads(
        (Path(__file__).parents[1] / "dicom_keywords.json").read_text(encoding="utf-8")
    )
    current = {f"{tag:08X}": value[-1] for tag, value in DicomDictionary.items()}
    repeaters = {tag: value[-1] for tag, value in RepeatersDictionary.items()}
    assert current == snapshot["tags"], "Review tag/Keyword changes before updating the snapshot"
    assert repeaters == snapshot["repeaters"], "Review repeating tag/Keyword changes"
    for number, keyword in snapshot["tags"].items():
        if keyword:
            assert tag_for_keyword(keyword) == int(number, 16)
        assert len(attribute_name(int(number, 16))) <= 64  # AttributeSpec.name max_length


@pytest.mark.parametrize(
    "number,name",
    [
        (0x00100020, "PatientID"),
        (0x0020000D, "StudyInstanceUID"),
        (0x00080020, "StudyDate"),
        (0x00280030, "PixelSpacing"),
        (0x00111010, "00111010"),
        (0x77760001, "77760001"),
        (0x60000010, "OverlayRows_60000010"),
        (0x60020010, "OverlayRows_60020010"),
        (0x60010010, "60010010"),
    ],
)
def test_attribute_names_are_stable_and_unique(number, name):
    assert attribute_name(number) == name


def test_runtime_names_do_not_change_with_the_installed_pydicom_dictionary(monkeypatch):
    pytest.importorskip("pydicom")
    import pydicom.datadict

    monkeypatch.setitem(
        pydicom.datadict.DicomDictionary,
        0x00100020,
        ("LO", "1", "Unexpected name", "", "ChangedPatientID"),
    )
    assert attribute_name(0x00100020) == "PatientID"


def test_metadata_keeps_private_and_repeating_values_separate():
    pytest.importorskip("pydicom")
    from pydicom.dataset import Dataset

    dataset = Dataset()
    dataset.PatientID = "SYNTHETIC"
    dataset.add_new(0x00111010, "LO", "private")
    dataset.add_new(0x60000010, "US", 10)
    dataset.add_new(0x60020010, "US", 20)
    assert metadata(dataset) == {
        "PatientID": "SYNTHETIC",
        "00111010": "private",
        "OverlayRows_60000010": "10",
        "OverlayRows_60020010": "20",
    }
