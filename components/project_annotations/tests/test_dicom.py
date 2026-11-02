# SPDX-License-Identifier: MIT
"""Pure planner regressions; no database or CVAT service is mocked here."""

import copy
import io
import json
import stat
from dataclasses import replace
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from components.project_annotations import dicom

LABELS = [
    {
        "id": 10,
        "name": "ImageQuality",
        "type": "tag",
        "attributes": [
            {"id": 20, "name": "quality", "input_type": "select", "values": ["good", "poor"]},
            {"id": 21, "name": "contact", "input_type": "checkbox", "values": []},
        ],
    },
    {
        "id": 11,
        "name": "Finding",
        "type": "any",
        "attributes": [{"id": 22, "name": "note", "input_type": "text", "values": []}],
    },
    {
        "id": 12,
        "name": "Score",
        "type": "tag",
        "attributes": [
            {"id": 23, "name": "value", "input_type": "number", "values": ["0", "10", "1"]}
        ],
    },
    {"id": 99, "name": "DicomTag", "type": "tag", "attributes": []},
]


def identity(frame=0, study="1.2.3", sop="1.2.3.4", **changes):
    values = {
        "StudyInstanceUID": {study},
        "SOPInstanceUID": {sop},
        "PatientID": {"P001"},
        "StudyDate": {"20250101"},
        "SeriesInstanceUID": {"1.2.3.9"},
    }
    values.update(changes)
    return dicom.FrameIdentity(frame, values)


def task(task_id=7, *, video=False, frames=(0, 1), study="1.2.3", sops=None, existing=()):
    if sops is None:
        sops = ["1.2.3.4"] * len(frames) if video else [f"1.2.3.{frame + 4}" for frame in frames]
    return dicom.TaskSnapshot(
        id=task_id,
        frames=frames,
        is_video=video,
        identities=tuple(identity(frame, study, sop) for frame, sop in zip(frames, sops)),
        existing_tags=tuple(existing),
    )


def record(sop="1.2.3.4", *, study="1.2.3", label="ImageQuality", attributes=None, **changes):
    result = {
        "study_instance_uid": study,
        "sop_instance_uid": sop,
        "tags": [
            {
                "label": label,
                "attributes": (
                    attributes if attributes is not None else {"quality": "good", "contact": "true"}
                ),
            }
        ],
    }
    result.update(changes)
    return result


def plan(*records, tasks=None, labels=None, mode="replace"):
    return dicom.plan_records(records, tasks or [task()], labels or LABELS, mode)


def archive(content, name="sample.json"):
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as zipped:
        zipped.writestr(name, content)
    stream.seek(0)
    return stream


class TestPlanning:
    def test_replace_produces_only_new_tags_and_does_not_mutate_snapshots(self):
        snapshot = task(
            existing=[{"id": 1, "frame": 0, "label_id": 99}, {"id": 2, "frame": 1, "label_id": 11}]
        )
        before = copy.deepcopy(snapshot)
        result = plan(record(), tasks=[snapshot])
        assert not result.failed_tasks
        assert set(result.updates) == {7}
        assert result.updates[7] == {
            "tags": [
                {
                    "frame": 0,
                    "label_id": 10,
                    "source": "file",
                    "group": 0,
                    "attributes": [
                        {"spec_id": 20, "value": "good"},
                        {"spec_id": 21, "value": "true"},
                    ],
                }
            ],
            "shapes": [],
            "tracks": [],
        }
        assert snapshot == before

    def test_append_preserves_existing_data_and_checks_existing_tags(self):
        snapshot = task(existing=[{"frame": 0, "label_id": 99}, {"frame": 0, "label_id": 11}])
        assert 7 in plan(record(), tasks=[snapshot], mode="append").updates
        duplicate = replace(
            snapshot, existing_tags=(*snapshot.existing_tags, {"frame": 0, "label_id": 10})
        )
        result = plan(record(), tasks=[duplicate], mode="append")
        assert 7 not in result.updates
        assert 7 in result.failed_tasks

    def test_replace_does_not_check_tags_that_will_be_removed(self):
        snapshot = task(existing=[{"frame": 0, "label_id": 10}, {"frame": 0, "label_id": 10}])
        assert 7 in plan(record(), tasks=[snapshot]).updates

    def test_append_rejects_existing_non_dicom_duplicates_in_final_task(self):
        snapshot = task(existing=[{"frame": 1, "label_id": 11}, {"frame": 1, "label_id": 11}])
        assert 7 in plan(record(), tasks=[snapshot], mode="append").failed_tasks

    def test_protected_duplicate_tags_remain_duplicates_in_both_modes(self):
        snapshot = task(existing=[{"frame": 0, "label_id": 99}, {"frame": 0, "label_id": 99}])
        for mode in ("replace", "append"):
            assert 7 in plan(record(), tasks=[snapshot], mode=mode).failed_tasks

    def test_reimport_replace_is_identical_but_append_detects_duplicates(self):
        first = plan(record())
        snapshot = task(existing=first.updates[7]["tags"])
        assert first.updates == plan(record(), tasks=[snapshot]).updates
        assert 7 in plan(record(), tasks=[snapshot], mode="append").failed_tasks

    def test_same_frame_in_multiple_records_accepts_different_labels(self):
        result = plan(record(), record(label="Finding", attributes={"note": "seen"}))
        assert [tag["label_id"] for tag in result.updates[7]["tags"]] == [10, 11]

    @pytest.mark.parametrize("mode", ("replace", "append"))
    @pytest.mark.parametrize("same_record", (True, False), ids=("same-record", "different-records"))
    def test_repeated_tag_in_same_record_or_different_records_fails_whole_task(
        self, mode, same_record
    ):
        if same_record:
            duplicated = record()
            duplicated["tags"] *= 2
            records = (duplicated,)
        else:
            records = (record(), record())
        result = plan(*records, mode=mode)
        assert not result.updates
        assert 7 in result.failed_tasks

    def test_same_label_on_different_frames_is_allowed(self):
        result = plan(record(), record("1.2.3.5"))
        assert [tag["frame"] for tag in result.updates[7]["tags"]] == [0, 1]

    def test_video_repeated_sop_is_one_image_and_applies_every_frame(self):
        snapshot = task(video=True, frames=(0, 1, 2))
        result = plan(record(), tasks=[snapshot])
        assert not result.ambiguous
        assert [tag["frame"] for tag in result.updates[7]["tags"]] == [0, 1, 2]

    def test_video_frame_indices_are_supplied_by_the_task_not_the_json(self):
        snapshot = task(video=True, frames=(0, 3, 5))
        assert [tag["frame"] for tag in plan(record(), tasks=[snapshot]).updates[7]["tags"]] == [
            0,
            3,
            5,
        ]
        assert 7 in plan(record(frame=1), tasks=[snapshot]).failed_tasks

    def test_multiple_jobs_at_same_task_frame_are_one_identity(self):
        snapshot = task()
        repeated = replace(snapshot, identities=(*snapshot.identities, snapshot.identities[0]))
        assert plan(record()).updates == plan(record(), tasks=[repeated]).updates

    def test_duplicate_uid_on_different_static_frames_is_ambiguous(self):
        result = plan(record(), tasks=[task(sops=["1.2.3.4", "1.2.3.4"])])
        assert result.ambiguous == [1]
        assert not result.updates
        assert 7 in result.failed_tasks

    def test_duplicate_uid_across_tasks_poison_both_tasks(self):
        result = plan(record(), tasks=[task(), task(8)])
        assert result.ambiguous == [1]
        assert set(result.failed_tasks) == {7, 8}
        assert not result.updates

    @pytest.mark.parametrize(
        "snapshot",
        [
            task(video=True, sops=["1.2.3.4", "1.2.3.5"]),
            replace(task(video=True), identities=(identity(),)),
        ],
        ids=("mixed-uids", "missing-frame-metadata"),
    )
    def test_video_mixed_uids_or_missing_frame_metadata_fails(self, snapshot):
        assert 7 in plan(record(), tasks=[snapshot]).failed_tasks

    def test_video_conflicting_optional_identity_is_rejected_without_input_optional_fields(self):
        snapshot = replace(
            task(video=True), identities=(identity(), identity(1, PatientID={"another"}))
        )
        assert 7 in plan(record(), tasks=[snapshot]).failed_tasks

    def test_study_and_sop_require_full_exact_uid_pair(self):
        result = plan(record("1.2.3"), record(study="1.2"), record("1.2.3.40"))
        assert result.unmatched == [1, 2, 3]
        assert not result.updates
        assert not result.failed_tasks

    def test_optional_identity_and_date_aliases_share_existing_normalization(self):
        row = record(
            patient_id="P001",
            pid="P001",
            study_date="2025/1/1",
            dt="Wed, 01 Jan 2025 12:00:00 GMT",
            SeriesInstanceUID="1.2.3.9",
        )
        assert 7 in plan(row).updates
        keywords = {"StudyInstanceUID": "1.2.3", "SOPInstanceUID": "1.2.3.4", "tags": row["tags"]}
        assert 7 in plan(keywords).updates

    @pytest.mark.parametrize(
        "changes",
        (
            {"patient_id": "wrong"},
            {"study_date": "20250102"},
            {"study_date": "not a date"},
            {"series_instance_uid": "9.8.7"},
        ),
    )
    def test_invalid_optional_identity_fails_only_its_task(self, changes):
        unrelated = task(8, study="2.3.4", sops=["2.3.4.5", "2.3.4.6"])
        result = plan(
            record(),
            record("1.2.3.5", **changes),
            record("2.3.4.5", study="2.3.4"),
            tasks=[task(), unrelated],
        )
        assert set(result.updates) == {8}
        assert set(result.failed_tasks) == {7}

    def test_conflicting_uid_aliases_poison_all_possible_exact_targets(self):
        second = task(8, study="2.3.4", sops=["2.3.4.5", "2.3.4.6"])
        row = record(uid="2.3.4", SOPInstanceUID="2.3.4.5")
        result = plan(row, tasks=[task(), second])
        assert set(result.failed_tasks) == {7, 8}
        assert not result.updates

    def test_conflicting_stored_identity_is_not_silently_chosen(self):
        snapshot = replace(task(), identities=(identity(StudyInstanceUID={"1.2.3", "1.2.999"}),))
        assert 7 in plan(record(), tasks=[snapshot]).failed_tasks

    @pytest.mark.parametrize(
        "row",
        [
            record(label="missing"),
            record(attributes={"missing": "value"}),
            record(shapes=[]),
            record(tags=[{"label": "Finding", "type": "rectangle"}]),
            record(tags=[{"label": "Finding", "points": [1, 2, 3, 4]}]),
        ],
        ids=("unknown-label", "unknown-attribute", "shapes", "tag-type", "tag-points"),
    )
    def test_unknown_label_attribute_or_unsupported_annotation_fails_task(self, row):
        result = plan(record("1.2.3.5"), row)
        assert not result.updates
        assert 7 in result.failed_tasks

    def test_dicom_tag_is_forbidden_in_both_modes(self):
        for mode in ("replace", "append"):
            result = plan(record(label="DicomTag", attributes={}), mode=mode)
            assert 7 in result.failed_tasks
            assert not result.updates

    def test_non_tag_labels_and_sublabels_are_rejected(self):
        for overrides in ({"type": "polygon"}, {"type": "rectangle"}, {"parent_id": 6}):
            labels = copy.deepcopy(LABELS)
            labels[0].update(overrides)
            assert 7 in plan(record(), labels=labels).failed_tasks

    def test_ambiguous_label_or_attribute_definition_is_rejected(self):
        assert 7 in plan(record(), labels=LABELS + [LABELS[0]]).failed_tasks
        labels = copy.deepcopy(LABELS)
        labels[0]["attributes"].append(labels[0]["attributes"][0])
        assert 7 in plan(record(), labels=labels).failed_tasks

    def test_empty_tags_explicitly_target_task_for_replace(self):
        result = plan(record(tags=[]))
        assert result.updates[7] == {"tags": [], "shapes": [], "tracks": []}

    def test_tag_attributes_can_be_omitted_for_native_defaults(self):
        result = plan(record(tags=[{"label": "ImageQuality"}]))
        assert result.updates[7]["tags"][0]["attributes"] == []

    def test_scalar_attribute_values_and_standard_value_checks(self):
        result = plan(record(attributes={"quality": "poor", "contact": True}))
        assert result.updates[7]["tags"][0]["attributes"][1]["value"] == "true"
        assert 7 in plan(record(label="Score", attributes={"value": 2.5})).updates

    @pytest.mark.parametrize(
        ("label", "attrs"),
        (
            ("ImageQuality", {"contact": "yes"}),
            ("ImageQuality", {"quality": "excellent"}),
            ("Score", {"value": "nan"}),
            ("Score", {"value": "11"}),
            ("Finding", {"note": []}),
            ("Finding", {"note": None}),
            ("Finding", {"note": "x" * 4097}),
            ("Finding", {"note": "\x00"}),
            ("Finding", {"note": "\ud800"}),
        ),
    )
    def test_invalid_attribute_values_fail_task(self, label, attrs):
        assert 7 in plan(record(label=label, attributes=attrs)).failed_tasks

    def test_malformed_records_without_identifiable_tasks_are_reported(self):
        rows = [
            None,
            {},
            {"study_instance_uid": "secret", "sop_instance_uid": "1.2.3.4", "tags": []},
        ]
        result = plan(*rows)
        assert set(result.invalid_records) == {1, 2, 3}
        assert not result.updates
        assert "secret" not in str(result)

    def test_missing_target_with_invalid_tag_definition_is_unmatched_and_invalid(self):
        result = plan(record("1.2.3.999", label="missing"))
        assert set(result.invalid_records) == {1}
        assert result.unmatched == [1]
        assert not result.failed_tasks
        assert not result.updates

    def test_task_rows_keep_original_positions_including_failed_candidate_records(self):
        second = task(8, study="2.3.4", sops=["2.3.4.5", "2.3.4.6"])
        result = plan(
            record(),
            record("1.2.3.999"),
            record("2.3.4.5", study="2.3.4"),
            record("1.2.3.5", label="missing"),
            tasks=[task(), second],
        )
        assert result.task_rows == {7: [1, 4], 8: [3]}
        assert set(result.updates) == {8}

    def test_ambiguous_records_are_associated_with_every_candidate_task(self):
        result = plan(record(), tasks=[task(), task(8)])
        assert result.task_rows == {7: [1], 8: [1]}

    def test_sop_lookup_uses_complete_aliases_without_guessing(self):
        rows = [
            record(sop_instance_uid=" 1.2.3.4 ", SOPInstanceUID="1.2.3.5"),
            {"sop_uid": "1.2.0005"},
            {"sop_instance_uid": 1234},
            {"sop_instance_uid": "partial"},
            None,
        ]
        assert dicom.record_sop_uids(rows) == {"1.2.3.4", "1.2.3.5", "1.2.0005"}

    def test_missing_tags_fails_identified_task(self):
        row = record()
        del row["tags"]
        assert 7 in plan(row).failed_tasks

    def test_expansion_limit_failure_releases_budget_for_other_tasks(self):
        first = task(video=True, frames=(0, 1, 2))
        second = task(8, study="2.3.4", frames=(0,), sops=["2.3.4.5"])
        with patch.object(dicom, "MAX_GENERATED_TAGS", 2):
            result = plan(record(), record("2.3.4.5", study="2.3.4"), tasks=[first, second])
        assert set(result.failed_tasks) == {7}
        assert set(result.updates) == {8}

    def test_later_duplicate_releases_previous_task_output_budget(self):
        second = task(8, study="2.3.4", frames=(0,), sops=["2.3.4.5"])
        with patch.object(dicom, "MAX_GENERATED_TAGS", 2):
            result = plan(
                record(), record(), record("2.3.4.5", study="2.3.4"), tasks=[task(), second]
            )
        assert set(result.failed_tasks) == {7}
        assert set(result.updates) == {8}

    def test_failed_video_task_does_not_expand_subsequent_records(self):
        first = task(video=True, frames=tuple(range(100)))
        second = task(8, study="2.3.4", frames=(0,), sops=["2.3.4.5"])
        labels = LABELS + [
            {"id": 1000 + index, "name": f"Extra{index}", "type": "tag", "attributes": []}
            for index in range(20)
        ]
        rows = [record(label="missing")]
        rows.extend(record(label=f"Extra{index}", attributes={}) for index in range(20))
        rows.append(record("2.3.4.5", study="2.3.4"))
        native_product = dicom.product
        expanded = 0

        def counted_product(*args):
            nonlocal expanded
            for values in native_product(*args):
                if isinstance(values[1], dict) and "label_id" in values[1]:
                    expanded += 1
                yield values

        with (
            patch.object(dicom, "MAX_GENERATED_TAGS", 100),
            patch.object(dicom, "product", side_effect=counted_product),
        ):
            result = plan(*rows, tasks=[first, second], labels=labels)
        assert set(result.failed_tasks) == {7}
        assert set(result.updates) == {8}
        # Only the unrelated task's tag is expanded. The failed video's later
        # records must not build thousands of keys outside the output budget.
        assert expanded == 1

    def test_later_ambiguous_row_still_invalidates_other_tasks_after_failure(self):
        second = task(8, sops=["1.2.3.5", "1.2.3.6"])
        result = plan(
            record(label="missing"),
            record("1.2.3.6"),
            record("1.2.3.5"),
            tasks=[task(), second],
        )
        assert set(result.failed_tasks) == {7, 8}
        assert result.ambiguous == [3]
        assert not result.updates

    def test_diagnostic_messages_do_not_echo_identifiers_labels_or_values(self):
        row = record(
            patient_id="sensitive-patient",
            label="sensitive-label",
            attributes={"sensitive-attr": "sensitive-value"},
        )
        text = str(plan(row))
        assert "sensitive" not in text
        assert "1.2.3" not in text


class TestInput:
    def test_reads_user_example_with_optional_fields(self):
        rows = [
            record(patient_id="P001", study_date="20250101"),
            record("1.2.3.5", attributes={"quality": "poor", "contact": "false"}),
        ]
        result = dicom.read_records(archive(json.dumps({"records": rows})))
        assert result == rows
        assert len(plan(*result).updates[7]["tags"]) == 2

    def test_one_nested_json_and_utf8_bom_are_supported(self):
        rows = [record()]
        source = archive(
            b"\xef\xbb\xbf" + json.dumps({"records": rows}).encode(), "folder/sample.json"
        )
        assert dicom.read_records(source) == rows

    @pytest.mark.parametrize(
        "content",
        (
            "[]",
            '{"records":[]}',
            '{"records":{},"extra":1}',
            '{"records":[],"records":[{}]}',
            '{"records":[NaN]}',
            '{"records":[1e999]}',
            '{"records":null}',
            '{"records":[{}],"extra":1}',
        ),
    )
    def test_bad_json_structure_duplicate_keys_and_nonfinite_numbers_are_rejected(self, content):
        with pytest.raises(dicom.DicomAnnotationError):
            dicom.read_records(archive(content))

    def test_plain_json_and_multiple_json_files_are_rejected(self):
        with pytest.raises(dicom.DicomAnnotationError):
            dicom.read_records(io.BytesIO(b'{"records":[{}]}'))
        stream = archive('{"records":[{}]}')
        with ZipFile(stream, "a") as zipped:
            zipped.writestr("other.json", '{"records":[{}]}')
        stream.seek(0)
        with pytest.raises(dicom.DicomAnnotationError):
            dicom.read_records(stream)

    @pytest.mark.parametrize(
        "name", ("../sample.json", "/sample.json", "C:/sample.json", "folder\\sample.json")
    )
    def test_unsafe_paths_are_rejected(self, name):
        with pytest.raises(dicom.DicomAnnotationError):
            dicom.read_records(archive('{"records":[{}]}', name))

    def test_symbolic_links_are_rejected(self):
        info = ZipInfo("sample.json")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with pytest.raises(dicom.DicomAnnotationError):
            dicom.read_records(archive('{"records":[{}]}', info))
