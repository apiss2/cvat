# SPDX-License-Identifier: MIT
"""Cross-extension contracts using synthetic DICOM files and real media conversion.

No Django, database, queue, or permission service is replaced or exercised here.
"""

import io
import json

import numpy as np
import pytest

pydicom = pytest.importorskip("pydicom")
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

from components.dicom_import import DicomImportError
from components.dicom_import.conversion import metadata, prepare
from components.dicom_import.registration import image_frames, missing_attributes, tag_batches
from cvat.apps.dataset_manager.formats import task_guide_core as guides

STUDY_UID = "1.2.826.0.1.3680043.10.543.1"
PATIENT_ID = "SYNTHETIC/PATIENT"
STUDY_DATE = "20260102"
PIXEL_SPACING = "00280030"


def save_dicom(path, instance, pixels, *, spacing=None, study_uid=STUDY_UID):
    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = f"1.2.826.0.1.3680043.10.543.2.{instance}"
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.PatientID = PATIENT_ID
    dataset.StudyDate = STUDY_DATE
    dataset.StudyTime = "030405"
    dataset.SeriesNumber = 2
    dataset.InstanceNumber = instance
    dataset.SOPClassUID = file_meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    if study_uid is not None:
        dataset.StudyInstanceUID = study_uid
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.Rows, dataset.Columns = pixels.shape[-2:]
    dataset.BitsAllocated = 16
    dataset.BitsStored = 12
    dataset.HighBit = 11
    dataset.PixelRepresentation = 0
    if pixels.ndim == 3:
        dataset.NumberOfFrames = len(pixels)
        dataset.FrameTime = "40"
    if spacing is not None:
        dataset.PixelSpacing = spacing
    dataset.PixelData = pixels.astype("<u2").tobytes()
    dataset.save_as(path, enforce_file_format=True)
    return path


def read_report():
    data = {
        "records": [
            {
                "input": {
                    "study_instance_uid": STUDY_UID,
                    "patient_id": PATIENT_ID,
                    "study_date": STUDY_DATE,
                    "parsed_patient_age": "025Y",
                    "patient_sex": "M",
                },
                "result": {"findings_ja": "合成画像の所見です。", "impression_ja": "検証用です。"},
            }
        ]
    }
    return guides.read_reports(io.BytesIO(json.dumps(data).encode("utf-8")))[0]


def registered_tags(prepared, attribute_ids):
    if prepared.video:
        values = next(iter(prepared.tags.values()))
        frames = ((frame, values) for frame in range(prepared.frames))
    else:
        # Exercise the same basename-to-frame mapping used after CVAT media registration.
        images = ((f"upload/{path.name}", frame) for frame, path in enumerate(prepared.media))
        frames = image_frames(prepared.tags, images)
    return [tag for batch in tag_batches(frames, 7, attribute_ids) for tag in batch["tags"]]


def attribute_values(tag, attribute_ids):
    names = {value: name for name, value in attribute_ids.items()}
    return {names[attribute["spec_id"]]: attribute["value"] for attribute in tag["attributes"]}


def task_identity(task_id, name, tags, attribute_ids):
    identity = guides.TaskIdentity(task_id, name)
    for tag in tags:
        for key, value in attribute_values(tag, attribute_ids).items():
            if key in guides.IDENTITY_TAGS:
                identity.tags.setdefault(key, set()).add(value)
    return identity


def test_real_dicom_images_and_video_receive_the_same_study_report(tmp_path):
    image = np.arange(24, dtype=np.uint16).reshape(4, 6) * 10
    video = np.stack([image, image + 20, image + 40])
    inputs = [
        save_dicom(tmp_path / "image.dcm", 1, image, spacing=["0.30", "0.25"]),
        save_dicom(tmp_path / "video.dcm", 2, video, spacing=["0.30", "0.25"]),
    ]
    plan = prepare(inputs, tmp_path / "converted", "site_with_underscores")
    assert len(plan.tasks) == 2
    assert sorted((task.video, task.frames) for task in plan.tasks) == [(False, 1), (True, 3)]

    names = {name for task in plan.tasks for values in task.tags.values() for name in values}
    attribute_ids = {name: index for index, name in enumerate(sorted(names), 1)}
    identities = []
    for task_id, task in enumerate(plan.tasks, 1):
        tags = registered_tags(task, attribute_ids)
        assert len(tags) == task.frames
        assert [tag["frame"] for tag in tags] == list(range(task.frames))
        assert all(
            attribute_values(tag, attribute_ids)[PIXEL_SPACING] == "0.30\\0.25" for tag in tags
        )
        identities.append(task_identity(task_id, task.name, tags, attribute_ids))

    reports = read_report()
    updates, unmatched = guides.plan_updates(reports, identities)
    assert updates == {1: reports[0].markdown, 2: reports[0].markdown}
    assert unmatched == []
    assert PATIENT_ID not in reports[0].markdown
    assert STUDY_UID not in reports[0].markdown
    assert "合成画像の所見です。" in reports[0].markdown


def test_missing_frame_spacing_does_not_inherit_existing_label_defaults(tmp_path):
    pixels = np.arange(24, dtype=np.uint16).reshape(4, 6)
    inputs = [
        save_dicom(tmp_path / "calibrated.dcm", 1, pixels, spacing=["0.5", "0.25"]),
        save_dicom(tmp_path / "uncalibrated.dcm", 2, pixels + 1),
    ]
    plan = prepare(inputs, tmp_path / "converted", "site")
    assert len(plan.tasks) == 1 and plan.tasks[0].frames == 2
    defaults = {PIXEL_SPACING: "99\\88", guides.STUDY_UID: "9.9.9"}
    existing = {
        "type": "tag",
        "attributes": [
            {"name": name, "input_type": "text", "default_value": value}
            for name, value in defaults.items()
        ],
    }
    names = {name for values in plan.tasks[0].tags.values() for name in values}
    additions = missing_attributes(existing, names)
    assert not set(defaults).intersection(attribute["name"] for attribute in additions)
    assert {
        attribute["name"]: attribute["default_value"] for attribute in existing["attributes"]
    } == defaults

    attribute_ids = {name: index for index, name in enumerate(sorted(names), 1)}
    values = [
        attribute_values(tag, attribute_ids)
        for tag in registered_tags(plan.tasks[0], attribute_ids)
    ]
    assert [value[PIXEL_SPACING] for value in values] == ["0.5\\0.25", ""]
    # Explicit blanks prevent CVAT's ordinary default expansion from inventing a measurement.
    expanded = [{**defaults, **value} for value in values]
    assert [value[PIXEL_SPACING] for value in expanded] == ["0.5\\0.25", ""]
    assert all(value[guides.STUDY_UID] == STUDY_UID for value in expanded)


def test_missing_study_uid_is_rejected_and_cannot_be_supplied_by_a_label_default(tmp_path):
    source = save_dicom(
        tmp_path / "missing-study.dcm", 1, np.zeros((3, 4, 6), dtype=np.uint16), study_uid=None
    )
    with pytest.raises(DicomImportError, match="StudyInstanceUID"):
        prepare([source], tmp_path / "converted", "site")

    # Also exercise the registration boundary independently of naming validation.
    # An absent UID must not become an existing project's unrelated default UID.
    values = metadata(pydicom.dcmread(source))
    assert guides.STUDY_UID not in values
    attribute_ids = {name: index for index, name in enumerate(guides.IDENTITY_TAGS, 1)}
    tag = next(tag_batches([(0, values)], 7, attribute_ids))["tags"][0]
    registered = attribute_values(tag, attribute_ids)
    assert registered[guides.STUDY_UID] == ""
    expanded = {guides.STUDY_UID: STUDY_UID, **registered}
    assert expanded[guides.STUDY_UID] == ""
    identity = task_identity(1, "video-without-study-identity", [tag], attribute_ids)
    with pytest.raises(guides.GuideImportError, match="No matching tasks"):
        guides.plan_updates(read_report(), [identity])
