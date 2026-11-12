# SPDX-License-Identifier: MIT
"""Run without CVAT services: python -m pytest -q components/task_guide/tests"""

import ast
import errno
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tracemalloc
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

ROOT = Path(__file__).resolve().parents[3]
FORMATS = ROOT / "cvat/apps/dataset_manager/formats"
CORE_NAME = "cvat.apps.dataset_manager.formats.task_guide_core"
spec = importlib.util.spec_from_file_location(CORE_NAME, FORMATS / "task_guide_core.py")
core = importlib.util.module_from_spec(spec)
sys.modules[CORE_NAME] = core
spec.loader.exec_module(core)


def native_asset_ids_reader():
    """Use CVAT's actual asset-reference parser without importing Django models."""
    source = ROOT / "cvat/apps/engine/models.py"
    guide = next(
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.ClassDef) and node.name == "AnnotationGuide"
    )
    method = next(
        node
        for node in guide.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_asset_ids_from_markdown"
    )
    method.decorator_list = []
    namespace = {"re": re}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["get_asset_ids_from_markdown"]


ASSET_IDS_FROM_MARKDOWN = native_asset_ids_reader()


def record(patient="P1", date="20250101", modality="CT", uid="1.2.3"):
    return {
        "input": {
            "study_instance_uid": uid,
            "patient_id": patient,
            "study_date": date,
            "parsed_patient_age": "25Y",
            "patient_sex": "M",
            "modality": modality,
        },
        "result": {
            "findings_ja": "所見です。",
            "impression_ja": "診断です。",
            "extracted": {
                "part": ["brain"],
                "history": ["History one", "History two"],
                "objective": "Screening",
                "findings": "English finding",
                "impression": "English diagnosis",
            },
        },
    }


def stream(records):
    return io.BytesIO(json.dumps({"records": records}, ensure_ascii=False).encode("utf-8"))


def reports(*rows):
    return core.read_reports(stream(list(rows)))[0]


def invalid_reasons(*rows):
    parsed, _, invalid = core.read_reports(stream(list(rows)))
    assert parsed == []
    return [(item.row, item.reason) for item in invalid]


def plan(rows, tasks):
    return core.plan_updates(reports(*rows), tasks)


def named_stream(mapping):
    return io.BytesIO(json.dumps(mapping, ensure_ascii=False).encode("utf-8"))


def zip_stream(members):
    source = io.BytesIO()
    with ZipFile(source, "w", compression=ZIP_DEFLATED) as archive:
        for name, markdown in members:
            archive.writestr(name, markdown)
    source.seek(0)
    return source


def named_zip(mapping):
    return zip_stream([("guides.json", named_stream(mapping).read())])


def report_zip(records):
    return zip_stream([("reports.json", stream(records).read())])


def write_json_zip(path, chunks, *, compression=ZIP_DEFLATED):
    """Generate actual large archives without assembling the JSON document in memory."""
    with ZipFile(path, "w", compression=compression) as archive:
        with archive.open("guides.json", "w") as member:
            for chunk in chunks:
                member.write(chunk)
    return path


def named_json_chunks(entries):
    yield b"{"
    for row, (name, markdown) in enumerate(entries):
        if row:
            yield b","
        yield json.dumps(name, ensure_ascii=False).encode("utf-8")
        yield b":"
        yield json.dumps(markdown, ensure_ascii=False).encode("utf-8")
    yield b"}"


def report_json_chunks(rows):
    yield b'{"records":['
    for row, value in enumerate(rows):
        if row:
            yield b","
        yield json.dumps(value, ensure_ascii=False).encode("utf-8")
    yield b"]}"


class BoundedReader:
    """Reject whole-payload reads while allowing zipfile's bounded end-of-file lookup."""

    def __init__(self, source, *, allow_tail=False):
        self.source = source
        self.allow_tail = allow_tail
        self.bytes_read = 0

    def read(self, size=-1):
        limit = 128 * 1024
        if size is None or size < 0:
            assert self.allow_tail, "ZIP members must be read in bounded chunks"
            position = self.source.tell()
            self.source.seek(0, io.SEEK_END)
            size = self.source.tell() - position
            self.source.seek(position)
        assert 0 <= size <= limit, f"Read requested {size} bytes at once"
        data = self.source.read(size)
        self.bytes_read += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self.source, name)

    def __enter__(self):
        self.source.__enter__()
        return self

    def __exit__(self, *args):
        return self.source.__exit__(*args)


def tags(patient="P1", date="20250101", modality="CT"):
    values = {core.PATIENT_ID: {patient}, core.STUDY_DATE: {date}}
    if modality is not None:
        values[core.MODALITY] = {modality}
    return values


def image(task_id=1, patient="P1", date="20250101", modality="CT", prefix="site"):
    return core.TaskIdentity(
        task_id,
        f"{prefix}_{patient.replace('/', '_')}_{date}_12345678",
        tags(patient, date, modality),
    )


def video(task_id=2, patient="P1", date="20250101", modality="CT"):
    return core.TaskIdentity(
        task_id, "site_P1_1_20250101_120000_0001_12345678", tags(patient, date, modality)
    )


class TestReport:
    def test_minimal_fields_and_no_extras(self):
        r = record()
        r["input"]["institution"] = "DO NOT INCLUDE"
        md = reports(r)[0].markdown
        for value in (
            "# 概要",
            "年齢：25Y",
            "性別：M",
            "検査方法：CT",
            "## 検査概要",
            "検査部位：brain",
            "既往歴：History one",
            "History two",
            "検査目的：Screening",
            "## 所見",
            "所見です。",
            "## 診断",
            "診断です。",
        ):
            assert value in md
        for value in ("DO NOT INCLUDE", "P1", "1.2.3", "20250101", "English finding"):
            assert value not in md

    @pytest.mark.parametrize("key, heading", [("findings", "所見"), ("impression", "診断")])
    @pytest.mark.parametrize("preferred", ["ja", "jp", "extracted", "en", "plain"])
    def test_language_fallbacks_independently(self, key, heading, preferred):
        r = record()
        r["result"][key + "_ja"] = " "
        r["result"]["extracted"].pop(key)
        if preferred == "extracted":
            r["result"]["extracted"][key] = "chosen"
        else:
            r["result"][key if preferred == "plain" else key + "_" + preferred] = "chosen"
        assert f"## {heading}\nchosen" in reports(r)[0].markdown

    def test_empty_values_and_zero_age(self):
        r = record()
        r["input"]["parsed_patient_age"] = 0
        r["input"]["patient_sex"] = None
        r["result"] = {"extracted": {"history": [None, " ", []]}}
        md = reports(r)[0].markdown
        assert "年齢：0" in md
        assert "性別" not in md
        assert "## 検査概要" not in md
        assert "## 所見\n記載なし" in md
        assert "## 診断\n記載なし" in md

    def test_no_invented_translation_or_age_conversion(self):
        r = record()
        r["input"]["parsed_patient_age"] = "005M"
        md = reports(r)[0].markdown
        assert "005M" in md
        assert "brain" in md
        assert "脳" not in md

    def test_plain_text_escaping_and_line_breaks(self):
        r = record()
        r["result"]["findings_ja"] = (
            "<script>x</script>\r\n![x](https://example.invalid)\n" + core.END
        )
        md = reports(r)[0].markdown
        assert "<script>" not in md
        assert "![x]" not in md
        assert core.END not in md
        assert "  \n" in md

    def test_aliases_and_iso_date(self):
        r = record()
        r["input"] = {"study_uid": "1.2.3", "pid": "P1", "date": "2025-01-01"}
        report = reports(r)[0]
        assert (report.patient_id, report.date, report.modality) == ("P1", "20250101", "")

    def test_ptc_aliases_and_overview(self):
        r = record()
        r["input"] = {
            "uid": "2.25.324479513911158040129943219792030673432",
            "pid": "ABC_DEFGH",
            "dt": "2025-01-01 00:00:00",
            "age": "21Y",
            "sex": "M",
            "mod": "CT",
        }
        report = reports(r)[0]
        assert (report.patient_id, report.date, report.modality) == ("ABC_DEFGH", "20250101", "CT")
        for value in ("年齢：21Y", "性別：M", "検査方法：CT"):
            assert value in report.markdown

    def test_overview_prefers_nonempty_canonical_fields(self):
        r = record()
        r["input"].update(age="21Y", sex="F", mod="MR")
        md = reports(r)[0].markdown
        for value in ("年齢：25Y", "性別：M", "検査方法：CT"):
            assert value in md
        r["input"].update(parsed_patient_age=" ", patient_sex=None, modality="")
        md = reports(r)[0].markdown
        for value in ("年齢：21Y", "性別：F", "検査方法：MR"):
            assert value in md

    def test_uid_is_not_a_patient_alias(self):
        r = record()
        r["input"] = {"uid": "P1", "study_date": "20250101"}
        assert invalid_reasons(r) == [(1, "PatientID is required")]

    @pytest.mark.parametrize(
        "uid", [None, "", "12345678", "1.2.x", "conflict", 123, "1." + "2" * 64]
    )
    def test_study_uid_is_neither_required_nor_used(self, uid):
        r = record(uid=uid)
        r["input"].update(study_uid="1.2.4", uid="other")
        report = reports(r)[0]
        assert (report.patient_id, report.date, report.modality) == ("P1", "20250101", "CT")

    @pytest.mark.parametrize(
        "keys, reason",
        [(("patient_id",), "PatientID is required"), (("study_date",), "StudyDate is required")],
    )
    def test_patient_and_date_are_required(self, keys, reason):
        r = record()
        for key in keys:
            r["input"][key] = " "
        assert invalid_reasons(r) == [(1, reason)]

    def test_modality_alias_case_and_absence(self):
        r = record(modality=" mr ")
        assert reports(r)[0].modality == "MR"
        r["input"].update(modality="", mod="ct")
        assert reports(r)[0].modality == "CT"
        r["input"].pop("modality")
        r["input"].pop("mod")
        assert reports(r)[0].modality == ""

    def test_equivalent_identifier_aliases_are_accepted(self):
        r = record()
        r["input"].update(
            pid=" P1 ",
            date="2025-01-01",
            dt="Wed, 01 Jan 2025 00:00:00 GMT",
        )
        report = reports(r)[0]
        assert (report.patient_id, report.date) == ("P1", "20250101")

    @pytest.mark.parametrize(
        "alias, value",
        [
            ("pid", "OTHER"),
            ("date", "20250102"),
            ("dt", "2025-01-02 00:00:00"),
        ],
    )
    def test_conflicting_aliases_skip_only_that_record(self, alias, value):
        r = record()
        r["input"][alias] = value
        parsed, _, invalid = core.read_reports(stream([r, record(patient="P2")]))
        assert [report.row for report in parsed] == [2]
        assert [item.row for item in invalid] == [1]
        assert "Conflicting aliases" in invalid[0].reason

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("20250107", "20250107"),
            ("2025-01-07", "20250107"),
            ("2025/01/07", "20250107"),
            ("2025.01.07", "20250107"),
            ("2025/1/7", "20250107"),
            ("2025-01-07 00:00:00", "20250107"),
            ("2025-01-07T23:59:59.123456", "20250107"),
            ("2025-01-07T00:00:00Z", "20250107"),
            ("2025-01-07T00:30:00+09:00", "20250107"),
            ("2025-01-07T23:30:00-05:00", "20250107"),
            ("Tue, 07 Jan 2025 00:00:00 GMT", "20250107"),
            ("Tue, 07 Jan 2025 00:30:00 +0900", "20250107"),
            ("7 Jan 2025 00:00:00 GMT", "20250107"),
            (" 2024-02-29 ", "20240229"),
            ("00010101", "00010101"),
            ("1 Jan 0001 00:00:00 GMT", "00010101"),
        ],
    )
    def test_supported_dates_keep_the_written_calendar_date(self, value, expected):
        assert reports(record(date=value))[0].date == expected

    @pytest.mark.parametrize(
        "date",
        [
            "20250230",
            "2025-02-29",
            "2025/13/01",
            "2025-01/01",
            "01/02/2025",
            "2025-01",
            "2025-01-01 garbage",
            "2025-01-01T25:00:00",
            "Tue, 30 Feb 2025 00:00:00 GMT",
            "Tue, 07 Jan 2025 00:00:00 GMT garbage",
            "Tue, 07 Jan 25 00:00:00 GMT",
            "1 Jan 0000 00:00:00 GMT",
            "yesterday",
            20250101,
        ],
    )
    def test_invalid_date(self, date):
        [(row, reason)] = invalid_reasons(record(date=date))
        assert row == 1
        assert reason in ("Invalid study date", "study_date must be a string")

    def test_reference_text_is_the_last_section_and_escaped(self):
        r = record()
        r["input_text"] = (
            "History: original\r\nObjective: unchanged\rFindings: original\n"
            "![image](https://example.invalid)\n<script>text</script>\n" + core.END
        )
        md = reports(r)[0].markdown
        reference = md.split("## 参考生データ\n", 1)[1]
        assert md.index("## 参考生データ") > md.index("## 診断")
        assert "History: original  \nObjective: unchanged  \nFindings: original" in reference
        for raw in ("![image]", "<script>", core.END, "\r"):
            assert raw not in reference
        assert "&lt;script&gt;text&lt;/script&gt;" in reference
        assert md.endswith(reference)

    def test_optional_reference_and_reference_only_report(self, subtests):
        for value in (None, "", " \r\n "):
            r = record()
            r["input_text"] = value
            with subtests.test(value=value):
                assert "参考生データ" not in reports(r)[0].markdown
        r = {"input": {"pid": "P1", "dt": "20250101"}, "result": {}, "input_text": "Original"}
        assert "## 参考生データ\nOriginal" in reports(r)[0].markdown

    def test_reference_validation(self, subtests):
        for value in (123, True, ["text"], {"text": "report"}, "bad\x00text"):
            r = record()
            r["input_text"] = value
            with subtests.test(value=value):
                assert [row for row, _ in invalid_reasons(r)] == [1]

    def test_two_sample_formats_from_json_zip(self, subtests):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        for filename, dates, modalities in (
            ("structured_report.json", ["20250102", "20250107"], ["MR", "CT"]),
            ("ptc_report.json", ["20250101", "20250102"], ["CT", "CT"]),
        ):
            with subtests.test(filename=filename):
                raw = (fixtures / filename).read_bytes()
                rows = json.loads(raw)["records"]
                with core.open_json_zip(zip_stream([(filename, raw)])) as member:
                    parsed, duplicates, invalid = core.read_reports(member)
                assert (duplicates, invalid) == (0, [])
                assert [r.date for r in parsed] == dates
                assert [r.modality for r in parsed] == modalities
                tasks = []
                for task_id, (row, report) in enumerate(zip(rows, parsed), 1):
                    patient = row["input"].get("patient_id", row["input"].get("pid"))
                    assert report.patient_id == patient
                    for label in ("年齢", "性別", "検査方法", "既往歴", "検査部位", "検査目的"):
                        assert label + "：" in report.markdown
                    assert "## 参考生データ\nHistory: Synthetic" in report.markdown
                    tasks.append(image(task_id, patient, report.date, report.modality))
                result = core.plan_updates(parsed, tasks)
                assert set(result.updates) == {1, 2}
                assert (result.unmatched, result.skipped_tasks) == ([], [])

    def test_bad_json_and_structure(self, subtests):
        for raw in (
            b"",
            b"\xff",
            b"{}",
            b"[]",
            b'{"records": []}',
            b'{"records": [], "records": []}',
            b'{"x": NaN}',
            b'{"x": 1e999}',
        ):
            with subtests.test(raw=raw), pytest.raises(core.GuideImportError):
                core.read_reports(io.BytesIO(raw))
        # A non-object record is a skipped record, not a broken JSON document.
        assert [item.row for item in core.read_reports(io.BytesIO(b"[[[]]]"))[2]] == [1]
        for r in (None, {}, {"input": {}, "result": []}):
            assert [row for row, _ in invalid_reasons(r)] == [1]

    def test_bom_and_top_level_array(self):
        assert (
            len(core.read_reports(io.BytesIO(b"\xef\xbb\xbf" + stream([record()]).read()))[0]) == 1
        )
        assert len(core.read_reports(io.BytesIO(json.dumps([record()]).encode()))[0]) == 1

    def test_generated_report_can_exceed_one_million_characters(self):
        r = record()
        r["result"]["findings_ja"] = "x" * 1_000_001
        report = reports(r)[0]
        assert len(report.markdown) > 1_000_000
        assert "## 所見\n" + r["result"]["findings_ja"] + "\n\n## 診断" in report.markdown

    def test_unsupported_selected_value_is_not_silently_dropped(self):
        for value in ({"nested": "value"}, True, "\x00"):
            r = record()
            r["result"]["extracted"]["history"] = value
            assert [row for row, _ in invalid_reasons(r)] == [1]

    def test_duplicate_reports(self):
        unique, duplicates, _ = core.read_reports(stream([record(), record(uid="9.9")]))
        assert (len(unique), duplicates) == (1, 1)
        changed = record()
        changed["result"]["findings_ja"] = "Different"
        assert [r.row for r in reports(record(), changed)] == [1, 2]

    def test_different_reference_text_is_not_deduplicated(self):
        first, second = record(), record()
        first["input_text"] = "Original report"
        second["input_text"] = "Different original report"
        assert [r.row for r in reports(first, second)] == [1, 2]


class TestMatching:
    def test_all_images_and_videos_with_same_patient_and_date(self):
        tasks = [image(), image(3, prefix="other_site"), video()]
        result = plan([record()], tasks)
        assert set(result.updates) == {1, 2, 3}
        assert (result.unmatched, result.skipped_tasks) == ([], [])

    def test_study_uid_is_not_used_for_matching(self):
        task = image()
        task.tags["StudyInstanceUID"] = {"9.9.9"}
        assert set(plan([record(uid="1.2.3")], [task]).updates) == {1}

    def test_sanitized_patient_and_arbitrary_prefix(self):
        result = plan(
            [record(patient="A/B_C")], [image(patient="A/B_C", prefix="prefix_with_underlines")]
        )
        assert set(result.updates) == {1}

    def test_prefixed_patient_matches_without_name_inference(self, subtests):
        r = record(patient="ABC_DEFGH", date="Tue, 07 Jan 2025 00:00:00 GMT")
        task = image(patient="ABC_DEFGH", date="20250107", prefix="ABC")
        task.name = "ABC_DEFGH_20250107_123"
        assert set(plan([r], [task]).updates) == {1}
        task.name = "renamed task"
        assert set(plan([r], [task]).updates) == {1}
        for different_patient in ("DEFGH", "OTHER_DEFGH", "ABCDEFGH"):
            with (
                subtests.test(patient=different_patient),
                pytest.raises(core.GuideImportError, match="No matching tasks"),
            ):
                plan([record(patient=different_patient, date="20250107")], [task])

    def test_date_formats_are_compared_as_calendar_dates(self):
        task = image(date="2025-01-01")
        assert set(plan([record(date="Wed, 01 Jan 2025 00:00:00 GMT")], [task]).updates) == {1}

    def test_patient_or_date_mismatch_does_not_match(self):
        for task in (image(patient="WRONG"), image(date="20250102")):
            with pytest.raises(core.GuideImportError, match="No matching tasks"):
                plan([record()], [task])

    def test_unmatched_record_positions(self):
        result = plan([record(), record(patient="P2")], [image()])
        assert set(result.updates) == {1}
        assert result.unmatched == [2]

    def test_name_is_not_used_as_a_source_of_identity(self):
        task = image()
        task.name = "site_OTHER_19991231_9.9.9"
        assert set(plan([record()], [task]).updates) == {1}
        task.tags.clear()
        task.name = "site_P1_20250101_1.2.3"
        with pytest.raises(core.GuideImportError, match="No matching tasks"):
            plan([record()], [task])

    def test_only_keyword_metadata_is_used_for_identity(self):
        task = core.TaskIdentity(
            1, "site_P1_20250101", {"00100020": {"P1"}, "00080020": {"20250101"}}
        )
        with pytest.raises(core.GuideImportError, match="No matching tasks"):
            plan([record()], [task])

    @pytest.mark.parametrize(
        "report_modality, task_modality",
        [("CT", "CT"), ("ct", " CT "), ("", "CT"), ("CT", None), ("CT", "")],
    )
    def test_modality_is_checked_only_when_both_have_it(self, report_modality, task_modality):
        task = image(modality=task_modality)
        assert set(plan([record(modality=report_modality)], [task]).updates) == {1}

    def test_modality_mismatch_skips_the_task_only(self):
        result = plan([record(modality="MR")], [image(modality="CT"), image(3, modality="MR")])
        assert set(result.updates) == {3}
        assert result.skipped_tasks == [1]
        assert result.unmatched == []

    def test_same_day_reports_are_selected_by_modality(self):
        ct, mr = record(modality="CT"), record(modality="MR")
        mr["result"]["findings_ja"] = "MR所見"
        result = plan([ct, mr], [image(1, modality="CT"), video(2, modality="MR")])
        assert "所見です。" in result.updates[1]
        assert "MR所見" in result.updates[2]
        assert (result.unmatched, result.skipped_tasks) == ([], [])

    def test_ambiguous_reports_skip_the_task(self, subtests):
        first, second = record(), record()
        second["result"]["findings_ja"] = "Different"
        for task_modality in ("CT", None):
            with subtests.test(task_modality=task_modality):
                result = plan(
                    [first, second, record(patient="P2")],
                    [image(1, modality=task_modality), image(3, patient="P2")],
                )
                assert set(result.updates) == {3}
                assert result.skipped_tasks == [1]
                assert result.unmatched == [1, 2]

    def test_task_with_mixed_identities_is_skipped(self, subtests):
        for key, value in ((core.PATIENT_ID, "P9"), (core.STUDY_DATE, "20250102")):
            with subtests.test(key=key):
                mixed = video()
                mixed.tags[key].add(value)
                result = plan([record()], [image(), mixed])
                assert set(result.updates) == {1}
                assert result.skipped_tasks == [2]

    def test_unreadable_task_date_is_skipped_only_for_reported_patients(self):
        broken = image(2, date="not a date")
        other = image(3, patient="P9", date="not a date")
        result = plan([record()], [image(), broken, other])
        assert set(result.updates) == {1}
        assert result.skipped_tasks == [2]

    def test_no_exact_match_is_an_error(self):
        with pytest.raises(core.GuideImportError, match="No matching tasks"):
            plan([record(modality="MR")], [image(modality="CT")])


class TestJsonZip:
    def test_one_json_file_in_a_directory_is_read_without_changing_content(self):
        raw = named_stream({"task": "# Markdown\n"}).read()
        source = zip_stream([("guides/", ""), ("guides/data.JSON", raw)])
        with core.open_json_zip(source) as member:
            assert member.read() == raw

    @pytest.mark.parametrize(
        "members",
        [
            [],
            [("directory/", "")],
            [("a.json", "{}"), ("b.json", "{}")],
            [("one/report.json", "{}"), ("two/report.json", "{}")],
        ],
    )
    def test_json_archive_requires_exactly_one_json_file(self, members):
        with pytest.raises(core.GuideImportError, match="exactly one JSON"):
            with core.open_json_zip(zip_stream(members)):
                pass

    @pytest.mark.parametrize(
        "members",
        [
            [("data.json", "{}"), ("README.txt", "text")],
            [("data.md", "{}")],
            [("../data.json", "{}")],
        ],
    )
    def test_json_archive_rejects_extra_files_and_unsafe_paths(self, members):
        with pytest.raises(core.GuideImportError):
            with core.open_json_zip(zip_stream(members)):
                pass

    def test_raw_json_is_not_an_accepted_upload(self):
        with pytest.raises(core.GuideImportError, match="ZIP"):
            with core.open_json_zip(named_stream({"task": "text"})):
                pass

    def test_json_archive_can_contain_more_than_fifty_thousand_directory_entries(self, tmp_path):
        path = tmp_path / "many-entries.zip"
        with ZipFile(path, "w") as archive:
            for index in range(50_000):
                archive.writestr(f"directory-{index}/", b"")
            archive.writestr("guides.json", b'{"task":"Markdown"}')
        with path.open("rb") as source, core.open_json_zip(source) as member:
            assert list(core.iter_named_json(member)) == [core.NamedGuide(1, "task", "Markdown")]


class TestNamedGuide:
    def test_json_preserves_task_name_and_complete_markdown(self):
        markdown = "# 見出し\r\n\n![image](/api/assets/a)\n  text  \n"
        guides = core.read_named_json(named_stream({" task 名 ": markdown, "empty": ""}))
        assert guides == [
            core.NamedGuide(1, " task 名 ", markdown),
            core.NamedGuide(2, "empty", ""),
        ]

    def test_json_rejects_bad_structure_names_and_text(self, subtests):
        for raw in (
            b"[]",
            b"{}",
            b'{"task":null}',
            b'{"task":1}',
            b'{"task":{}}',
            b'{"": "text"}',
            b'{" ": "text"}',
            b'{"task":"one","task":"two"}',
            b'{"task":"\\ud800"}',
            b'{"\\ud800":"text"}',
            b'{"task":"\\u0000"}',
            b'{"ta\\u0000sk":"text"}',
            b"\xff",
            b'{"task":NaN}',
        ):
            with subtests.test(raw=raw), pytest.raises(core.GuideImportError):
                core.read_named_json(io.BytesIO(raw))

    def test_json_preserves_markdown_over_one_million_characters(self):
        markdown = "# Large guide\r\n" + "x" * 1_000_001 + "\n最後\n"
        assert core.read_named_json(named_stream({"task": markdown})) == [
            core.NamedGuide(1, "task", markdown)
        ]

    def test_name_matching_is_exact_and_reports_unmatched_positions(self):
        guides = core.read_named_json(named_stream({"task A": "a", "task B": "b", "TASK A": "c"}))
        updates, unmatched = core.plan_named_updates(guides, [core.TaskIdentity(1, "task A")])
        assert updates == {1: "a"}
        assert unmatched == [2, 3]
        with pytest.raises(core.GuideImportError, match="No matching tasks"):
            core.plan_named_updates(guides, [core.TaskIdentity(2, "task A ")])

    def test_ambiguous_target_name_rejected(self):
        guides = core.read_named_json(named_stream({"task": "text"}))
        with pytest.raises(core.GuideImportError, match="multiple tasks"):
            core.plan_named_updates(
                guides, [core.TaskIdentity(1, "task"), core.TaskIdentity(2, "task")]
            )

    def test_zip_supports_utf8_bom_nested_files_and_empty_text(self):
        guides = core.read_markdown_zip(
            zip_stream(
                [
                    ("guides/", ""),
                    ("guides/日本語.md", "\ufeff# 日本語\r\n"),
                    ("empty.MD", ""),
                ]
            )
        )
        assert guides == [
            core.NamedGuide(1, "日本語", "# 日本語\r\n"),
            core.NamedGuide(2, "empty", ""),
        ]

    def test_zip_rejects_unsafe_paths_non_markdown_and_duplicate_task_names(self, subtests):
        for members in (
            [("../task.md", "text")],
            [("/task.md", "text")],
            [("a/../task.md", "text")],
            [("./task.md", "text")],
            [("C:/task.md", "text")],
            [("a\\task.md", "text")],
            [("a//task.md", "text")],
            [("task.txt", "text")],
            [(".md", "text")],
            [("first/task.md", "one"), ("second/task.md", "two")],
            [("task.md", b"\xff")],
            [("task.md", "\x00")],
            [],
            [("directory/", "")],
        ):
            with subtests.test(members=members), pytest.raises(core.GuideImportError):
                core.read_markdown_zip(zip_stream(members))
        info = ZipInfo("task.md")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with pytest.raises(core.GuideImportError, match="regular files"):
            core.read_markdown_zip(zip_stream([(info, "elsewhere")]))
        with pytest.raises(core.GuideImportError, match="Invalid"):
            core.read_markdown_zip(io.BytesIO(b"not a zip"))

    def test_zip_preserves_markdown_over_one_million_characters(self):
        markdown = "# Large guide\r\n" + "x" * 1_000_001 + "\n最後\n"
        assert core.read_markdown_zip(zip_stream([("task.md", markdown)])) == [
            core.NamedGuide(1, "task", markdown)
        ]


class TestMerge:
    def test_append_preserves_manual_text_and_assets(self):
        manual = "# Manual\n![asset](/api/assets/1234)\n"
        md = core.merge_markdown(manual, "# 概要\nFirst\n")
        assert md.startswith(manual)
        assert md.count(core.START) == 1

    def test_idempotence_and_replace_only_block(self):
        md = core.merge_markdown("manual", "old") + "manual suffix\n"
        updated = core.merge_markdown(md, "new")
        assert updated.startswith("manual\n\n")
        assert updated.endswith("manual suffix\n")
        assert "old" not in updated
        assert core.merge_markdown(updated, "new") == updated

    @pytest.mark.parametrize(
        "md",
        [
            core.START,
            core.END,
            core.END + core.START,
            core.START * 2 + core.END,
            core.START + core.END * 2,
        ],
    )
    def test_broken_markers(self, md):
        with pytest.raises(core.GuideImportError):
            core.merge_markdown(md, "new")

    def test_merged_guide_can_exceed_one_million_characters(self):
        manual = "manual\n" + "x" * 1_000_001
        merged = core.merge_markdown(manual, "new")
        assert merged == manual + f"\n\n{core.START}\nnew\n{core.END}\n"

    def test_invalid_unicode(self):
        with pytest.raises(core.GuideImportError):
            core.merge_markdown("", "\ud800")


def module(name, **attributes):
    obj = ModuleType(name)
    obj.__dict__.update(attributes)
    return obj


class TestAdapter:
    """Contract tests with mocked ORM/OPA/RQ, not CVAT/PostgreSQL integration tests."""

    @pytest.fixture(autouse=True)
    def setup_context(self, request):
        self.in_transaction = False
        self.project = self.timestamped(pk=7, id=7, organization_id=None)
        self.tasks = [
            self.timestamped(id=1, name=image().name),
            self.timestamped(id=2, name=video().name),
        ]
        self.project_manager = MagicMock()
        self.project_manager.select_for_update.return_value.get.return_value = self.project
        self.task_manager = MagicMock()
        self.task_manager.select_for_update.return_value.filter.return_value.exclude.return_value.order_by.return_value = (
            self.tasks
        )
        self.task_manager.select_for_update.return_value.filter.return_value.order_by.return_value = (
            self.tasks
        )
        self.values_manager = MagicMock()
        self.values = [
            (task.id, tag, next(iter(value)))
            for task in (image(), video())
            for tag, value in task.tags.items()
        ]
        self.values_manager.filter.return_value.order_by.return_value.values_list.return_value.distinct.return_value.iterator.side_effect = lambda: iter(
            self.values
        )
        self.guide_manager = MagicMock()
        self.existing = []

        def guide_iterator(*, chunk_size):
            for guide in sorted(self.existing, key=lambda guide: guide.task_id):
                if not hasattr(guide, "id"):
                    guide.id = 1000 + guide.task_id
                yield guide

        self.guide_manager.select_for_update.return_value.filter.return_value.order_by.return_value.iterator.side_effect = (
            guide_iterator
        )
        self.guide_manager.only.return_value.get.side_effect = lambda *, pk: next(
            guide for guide in self.existing if guide.id == pk
        )
        self.permission = MagicMock()
        self.permission.Scopes = SimpleNamespace(CREATE="create", UPDATE="update")
        self.permission.create_base_perm.return_value.check_access.return_value.allow = True
        self.project_permission = MagicMock()
        self.project_permission.Scopes = SimpleNamespace(IMPORT_DATASET="import:dataset")
        self.project_permission.create_base_perm.return_value.check_access.return_value.allow = True
        self.transaction = MagicMock()

        @contextmanager
        def atomic():
            self.in_transaction = True
            try:
                yield
            finally:
                self.in_transaction = False

        self.transaction.atomic.side_effect = atomic
        self.meta = SimpleNamespace(status="", progress=0, org_id=None, save=MagicMock())
        self.project_data = type("ProjectData", (), {})
        self.error = type("CvatImportError", (Exception,), {})
        self.registration = {}
        self.asset_ids_from_markdown = MagicMock(wraps=ASSET_IDS_FROM_MARKDOWN)

        def importer(**kwargs):
            self.registration[kwargs["name"]] = kwargs
            return lambda func: func

        models = SimpleNamespace(
            Project=SimpleNamespace(objects=self.project_manager),
            Task=SimpleNamespace(objects=self.task_manager),
            LabeledImageAttributeVal=SimpleNamespace(objects=self.values_manager),
            AnnotationGuide=SimpleNamespace(
                objects=self.guide_manager,
                get_asset_ids_from_markdown=self.asset_ids_from_markdown,
            ),
        )
        dependencies = {
            "cvat.apps.dataset_manager.bindings": module(
                "bindings", CvatImportError=self.error, ProjectData=self.project_data
            ),
            "cvat.apps.dataset_manager.formats.registry": module("registry", importer=importer),
            "django": module("django"),
            "django.db": module(
                "db",
                transaction=self.transaction,
                IntegrityError=type("IntegrityError", (Exception,), {}),
            ),
            "cvat.apps.engine": module("engine", models=models),
            "cvat.apps.engine.permissions": module(
                "permissions",
                AnnotationGuidePermission=self.permission,
                ProjectPermission=self.project_permission,
            ),
        }
        self.modules_patch = patch.dict(sys.modules, dependencies)
        self.modules_patch.start()
        request.addfinalizer(self.modules_patch.stop)
        spec = importlib.util.spec_from_file_location(
            "cvat.apps.dataset_manager.formats.task_guide_adapter_test", FORMATS / "task_guide.py"
        )
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.context_patch = patch.object(
            self.adapter, "_import_context", return_value=("request", {"user_id": 3}, self.meta)
        )
        self.context_patch.start()
        request.addfinalizer(self.context_patch.stop)

    def timestamped(self, **attributes):
        obj = SimpleNamespace(updated_date=datetime(2026, 1, 1, tzinfo=timezone.utc), **attributes)

        def touch():
            assert self.in_transaction, "Timestamp writes must share the guide transaction"
            obj.updated_date += timedelta(seconds=1)

        obj.touch = MagicMock(side_effect=touch)
        return obj

    def assert_no_timestamps_changed(self):
        for obj in [self.project, *self.tasks]:
            obj.touch.assert_not_called()

    def run_import(self, rows=None):
        self.adapter.import_project_guides(report_zip(rows or [record()]), self.project)

    def test_registration_and_project_only(self):
        assert self.registration == {
            name: {"name": name, "version": "1.0", "ext": ext}
            for name, ext in (
                ("Task Guide JSON", "ZIP"),
                ("Task Guide Markdown Zip", "ZIP"),
                ("Task Guide DICOM Report Import", "ZIP"),
            )
        }
        for importer in (
            self.adapter.import_task_guides,
            self.adapter.import_task_guides_markdown_zip,
            self.adapter.import_task_guides_dicom_report,
        ):
            with pytest.raises(self.error, match="project imports only"):
                importer(io.BytesIO(), "unused", object())
        self.guide_manager.create.assert_not_called()

    def test_registered_formats_route_their_actual_payloads(self, subtests, tmp_path):
        instance = self.project_data()
        instance.db_project = self.project
        for importer, payload in (
            (self.adapter.import_task_guides, named_zip({self.tasks[0].name: "# Markdown"})),
            (
                self.adapter.import_task_guides_markdown_zip,
                zip_stream([(self.tasks[0].name + ".md", "# Markdown")]),
            ),
            (self.adapter.import_task_guides_dicom_report, report_zip([record()])),
        ):
            with subtests.test(importer=importer.__name__):
                self.guide_manager.create.reset_mock()
                with patch(
                    "components.task_guide.staging.TemporaryFile", wraps=tempfile.TemporaryFile
                ) as temporary:
                    importer(payload, tmp_path, instance)
                assert temporary.call_args.kwargs["dir"] == tmp_path
                assert self.guide_manager.create.called
        assert self.guide_manager.create.call_count == 2

    def test_registered_formats_report_invalid_input_without_writes(self, subtests, tmp_path):
        instance = self.project_data()
        instance.db_project = self.project
        for importer, raw in (
            (self.adapter.import_task_guides, b"[]"),
            (self.adapter.import_task_guides_markdown_zip, b"not a zip"),
            (self.adapter.import_task_guides_dicom_report, b"{}"),
        ):
            with subtests.test(importer=importer.__name__), pytest.raises(self.error):
                importer(io.BytesIO(raw), tmp_path, instance)
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_json_formats_validate_the_contents_of_the_uploaded_archive(self, subtests, tmp_path):
        instance = self.project_data()
        instance.db_project = self.project
        for importer, raw in (
            (self.adapter.import_task_guides, b"[]"),
            (self.adapter.import_task_guides_dicom_report, b"{}"),
            (self.adapter.import_task_guides, b"\xff"),
        ):
            with subtests.test(importer=importer.__name__), pytest.raises(self.error):
                importer(zip_stream([("data.json", raw)]), tmp_path, instance)
        self.guide_manager.create.assert_not_called()
        self.assert_no_timestamps_changed()

    @pytest.mark.parametrize("compression", [ZIP_STORED, ZIP_DEFLATED])
    def test_large_json_zip_import_uses_bounded_memory_and_matches_late_entries(
        self, tmp_path, compression
    ):
        count = 520
        body = "x" * (64 * 1024)

        def markdown(row):
            return f"# Guide {row}\r\n{body}\n最後 {row}\n"

        def entries():
            matched_names = {1: self.tasks[0].name, count: self.tasks[1].name}
            for row in range(1, count + 1):
                yield matched_names.get(row, f"unmatched-{row}"), markdown(row)

        path = write_json_zip(
            tmp_path / "large.zip", named_json_chunks(entries()), compression=compression
        )
        with ZipFile(path) as archive:
            info = archive.getinfo("guides.json")
            assert info.file_size > 32 * 1024 * 1024
            if compression == ZIP_STORED:
                assert info.compress_size > 32 * 1024 * 1024
            else:
                assert info.compress_size < 32 * 1024 * 1024

        archive_open = ZipFile.open
        members = []

        def bounded_member(archive, name, mode="r", *args, **kwargs):
            assert mode == "r"
            member = BoundedReader(archive_open(archive, name, mode, *args, **kwargs))
            members.append(member)
            return member

        with path.open("rb") as source, patch.object(ZipFile, "open", bounded_member):
            bounded_source = BoundedReader(source, allow_tail=True)
            tracemalloc.start()
            try:
                self.adapter.import_project_guides(
                    bounded_source, self.project, input_format="json"
                )
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        # The input is over 32 MiB, but only individual guides and small indexes
        # should be live. This allows ample overhead while detecting all-guide lists.
        assert peak < 24 * 1024 * 1024
        assert len(members) == 1
        assert members[0].bytes_read == info.file_size
        assert members[0].closed
        assert self.guide_manager.create.call_count == 2
        created = {
            call.kwargs["task_id"]: call.kwargs["markdown"]
            for call in self.guide_manager.create.call_args_list
        }
        assert created == {1: markdown(1), 2: markdown(count)}
        assert f"{count - 2} unmatched records" in self.meta.status
        assert "unmatched positions: 2, 3, 4" in self.meta.status
        self.meta.save.assert_called_once_with()

    @pytest.mark.parametrize("input_format", ["json", "dicom"])
    def test_more_than_fifty_thousand_json_entries_match_first_and_last(
        self, tmp_path, input_format
    ):
        count = 50_001
        if input_format == "json":
            self.tasks[0].name, self.tasks[1].name = "task-1", f"task-{count}"
            chunks = named_json_chunks(
                (f"task-{row}", f"guide {row}") for row in range(1, count + 1)
            )
        else:
            self.values[:] = [
                (1, core.PATIENT_ID, "P1"),
                (1, core.STUDY_DATE, "20250101"),
                (2, core.PATIENT_ID, f"P{count}"),
                (2, core.STUDY_DATE, "20250101"),
            ]
            chunks = report_json_chunks(
                {
                    "input": {"pid": f"P{row}", "dt": "20250101"},
                    "result": {"findings": f"guide {row}"},
                }
                for row in range(1, count + 1)
            )
        path = write_json_zip(tmp_path / "many-records.zip", chunks)
        with path.open("rb") as source:
            self.adapter.import_project_guides(source, self.project, input_format=input_format)
        assert self.guide_manager.create.call_count == 2
        created = {
            call.kwargs["task_id"]: call.kwargs["markdown"]
            for call in self.guide_manager.create.call_args_list
        }
        if input_format == "json":
            assert created == {1: "guide 1", 2: f"guide {count}"}
        else:
            assert "## 所見\nguide 1\n" in created[1]
            assert f"## 所見\nguide {count}\n" in created[2]
        assert f"{count - 2} unmatched records, 0 duplicate records" in self.meta.status
        assert "unmatched positions: 2, 3, 4" in self.meta.status
        assert self.meta.status.endswith(", ...")

    @pytest.mark.parametrize(
        "input_format, invalid_end",
        [
            (input_format, invalid_end)
            for input_format in ("json", "dicom")
            for invalid_end in ("trailing_document", "duplicate_key", "nonfinite", "unicode")
        ]
        # A malformed named-guide value breaks the JSON format. A malformed report
        # record is skipped instead; see test_late_invalid_report_record_is_skipped.
        + [("json", "record")],
    )
    def test_late_invalid_json_prevents_all_guide_writes(self, tmp_path, input_format, invalid_end):
        guide = SimpleNamespace(task_id=1, markdown="manual", save=MagicMock())
        self.existing[:] = [guide]
        if input_format == "dicom":
            prefix = b'{"records":[' + json.dumps(record()).encode("utf-8")
            suffix = {
                "trailing_document": b"]} []",
                "duplicate_key": b'], "records":[]}',
                "nonfinite": b'], "metadata":{"number":1e999}}',
                "unicode": b"]}\xff",
            }[invalid_end]
            reader_name, expected_rows = "iter_reports", [1]
        else:
            prefix = json.dumps({task.name: "new guide" for task in self.tasks}).encode("utf-8")[
                :-1
            ]
            suffix = {
                "trailing_document": b"} []",
                "duplicate_key": b","
                + json.dumps(self.tasks[0].name).encode("utf-8")
                + b':"duplicate"}',
                "nonfinite": b', "later":1e999}',
                "unicode": b"}\xff",
                "record": b', "later":null}',
            }[invalid_end]
            reader_name, expected_rows = "iter_named_json", [1, 2]
        padding = b" " * (64 * 1024)
        path = write_json_zip(tmp_path / "late-invalid.zip", (prefix, padding, padding, suffix))
        reader = getattr(core, reader_name)
        yielded_rows = []

        def observed_entries(source):
            for entry in reader(source):
                yielded_rows.append(entry.row)
                yield entry

        with (
            path.open("rb") as source,
            patch.object(self.adapter, reader_name, observed_entries),
            pytest.raises(core.GuideImportError),
        ):
            self.adapter.import_project_guides(source, self.project, input_format=input_format)
        assert yielded_rows == expected_rows
        assert guide.markdown == "manual"
        guide.save.assert_not_called()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    @pytest.mark.parametrize("input_format", ["json", "dicom"])
    def test_late_zip_crc_failure_prevents_all_guide_writes(self, tmp_path, input_format):
        guide = SimpleNamespace(task_id=1, markdown="manual", save=MagicMock())
        self.existing[:] = [guide]
        if input_format == "dicom":
            chunks = report_json_chunks([record()])
            reader_name, expected_rows = "iter_reports", [1]
        else:
            chunks = named_json_chunks((task.name, "new guide") for task in self.tasks)
            reader_name, expected_rows = "iter_named_json", [1, 2]

        def padded_chunks():
            yield from chunks
            for _ in range(3):
                yield b" " * (64 * 1024)

        path = write_json_zip(tmp_path / "late-crc.zip", padded_chunks(), compression=ZIP_STORED)
        with ZipFile(path) as archive:
            info = archive.getinfo("guides.json")
        with path.open("r+b") as archive:
            archive.seek(info.header_offset + 26)
            name_size = int.from_bytes(archive.read(2), "little")
            extra_size = int.from_bytes(archive.read(2), "little")
            archive.seek(info.header_offset + 30 + name_size + extra_size + info.file_size - 1)
            assert archive.read(1) == b" "
            archive.seek(-1, io.SEEK_CUR)
            # JSON stays valid; only ZIP integrity changes, after earlier records were yielded.
            archive.write(b"\t")
        reader = getattr(core, reader_name)
        yielded_rows = []

        def observed_entries(source):
            for entry in reader(source):
                yielded_rows.append(entry.row)
                yield entry

        with (
            path.open("rb") as source,
            patch.object(self.adapter, reader_name, observed_entries),
            pytest.raises(core.GuideImportError, match="ZIP"),
        ):
            self.adapter.import_project_guides(source, self.project, input_format=input_format)
        assert yielded_rows == expected_rows
        assert guide.markdown == "manual"
        guide.save.assert_not_called()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    @pytest.mark.parametrize("invalid_tail", [False, True])
    def test_staged_text_file_is_closed_after_success_or_late_failure(self, invalid_tail):
        files = []

        def temporary_file(*args, **kwargs):
            file = tempfile.TemporaryFile(*args, **kwargs)
            files.append(file)
            return file

        payload = stream([record()]).getvalue() + (b" []" if invalid_tail else b"")
        with (
            patch("components.task_guide.staging.TemporaryFile", side_effect=temporary_file),
            pytest.raises(core.GuideImportError) if invalid_tail else nullcontext(),
        ):
            self.adapter.import_project_guides(
                zip_stream([("reports.json", payload)]), self.project
            )
        assert len(files) == 1
        assert files[0].closed

    @pytest.mark.parametrize("input_format", ["json", "dicom", "zip"])
    def test_staging_disk_full_preserves_io_error_and_prevents_writes(self, input_format):
        guide = SimpleNamespace(task_id=1, markdown="manual", save=MagicMock())
        self.existing[:] = [guide]
        if input_format == "json":
            source = named_zip({task.name: f"guide {task.id}" for task in self.tasks})
        elif input_format == "dicom":
            source = report_zip([record(), record(patient="P2")])
        else:
            source = zip_stream([(f"{task.name}.md", f"guide {task.id}") for task in self.tasks])
        files = []

        def temporary_file(*args, **kwargs):
            file = tempfile.TemporaryFile(*args, **kwargs)
            files.append(file)
            return file

        original_write = self.adapter.TextStore.write
        failure = OSError(errno.ENOSPC, "No space left on device")
        attempts = 0

        def write(store, text):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise failure
            return original_write(store, text)

        with (
            patch("components.task_guide.staging.TemporaryFile", side_effect=temporary_file),
            patch.object(self.adapter.TextStore, "write", write),
            pytest.raises(OSError) as error,
        ):
            self.adapter.import_project_guides(source, self.project, input_format=input_format)
        assert error.value is failure
        assert attempts == 2
        assert len(files) == 1
        assert files[0].closed
        assert guide.markdown == "manual"
        guide.save.assert_not_called()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_generic_import_replaces_entire_markdown_and_preserves_exact_text(self):
        guide = SimpleNamespace(task_id=1, markdown="old manual text", save=MagicMock())
        self.existing[:] = [guide]
        text = "# 新しい説明\r\n\n  paragraph  \n"
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: text}), self.project, input_format="json"
        )
        assert guide.markdown == text
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])
        self.guide_manager.create.assert_not_called()
        self.values_manager.filter.assert_not_called()
        self.tasks[0].touch.assert_called_once_with()
        self.tasks[1].touch.assert_not_called()
        self.project.touch.assert_called_once_with()
        guide.save.reset_mock()
        self.tasks[0].touch.reset_mock()
        self.project.touch.reset_mock()
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: text}), self.project, input_format="json"
        )
        guide.save.assert_not_called()
        self.assert_no_timestamps_changed()
        assert "1 unchanged" in self.meta.status

    def test_generic_import_accepts_empty_markdown_and_tasks_without_media(self):
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: ""}), self.project, input_format="json"
        )
        self.guide_manager.create.assert_called_once_with(task_id=1, markdown="")
        self.task_manager.select_for_update.return_value.filter.return_value.exclude.assert_not_called()

    def test_generic_new_guide_asset_reference_prevents_all_writes(self, subtests):
        markdown = "![image](/api/assets/12345678-1234-5678-9abc-123456789abc)"
        for input_format in ("json", "zip"):
            mapping = {self.tasks[0].name: "valid first guide", self.tasks[1].name: markdown}
            payload = (
                named_zip(mapping)
                if input_format == "json"
                else zip_stream([(name + ".md", text) for name, text in mapping.items()])
            )
            with (
                subtests.test(input_format=input_format),
                pytest.raises(core.GuideImportError, match="Task 2.*assets already owned"),
            ):
                self.adapter.import_project_guides(payload, self.project, input_format=input_format)
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_generic_guide_preserves_its_own_asset_reference(self):
        asset_id = UUID("12345678-1234-5678-9abc-123456789abc")
        assets = MagicMock()
        assets.filter.return_value.values_list.return_value = [asset_id]
        guide = SimpleNamespace(task_id=1, markdown="original", assets=assets, save=MagicMock())
        self.existing[:] = [guide]
        markdown = f"# Guide\n![image](/api/assets/{str(asset_id).upper()})\n"
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: markdown}), self.project, input_format="json"
        )
        assert guide.markdown == markdown
        assets.filter.assert_called_once_with(uuid__in={asset_id})
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])
        self.guide_manager.create.assert_not_called()

    def test_generic_guide_rejects_foreign_or_unknown_asset_references(self, subtests):
        own_id = UUID("12345678-1234-5678-9abc-123456789abc")
        foreign_or_missing_id = UUID("87654321-4321-8765-cba9-cba987654321")
        for available in ([], [own_id]):
            assets = MagicMock()
            assets.filter.return_value.values_list.return_value = available
            guide = SimpleNamespace(task_id=1, markdown="original", assets=assets, save=MagicMock())
            self.existing[:] = [guide]
            markdown = (
                f"![own](/api/assets/{own_id})\n![other](/api/assets/{foreign_or_missing_id})"
            )
            with (
                subtests.test(available=available),
                pytest.raises(core.GuideImportError, match="assets already owned"),
            ):
                self.adapter.import_project_guides(
                    named_zip({self.tasks[0].name: markdown}), self.project, input_format="json"
                )
            assert guide.markdown == "original"
            guide.save.assert_not_called()
            assets.filter.assert_called_once_with(uuid__in={own_id, foreign_or_missing_id})
        self.guide_manager.create.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_generic_external_image_urls_are_preserved(self):
        markdown = "![image](https://example.test/image.png)\n[link](https://example.test/page)\n"
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: markdown}), self.project, input_format="json"
        )
        self.guide_manager.create.assert_called_once_with(task_id=1, markdown=markdown)

    def test_dicom_report_keeps_existing_handwritten_asset_references(self):
        markdown = "![image](/api/assets/12345678-1234-5678-9abc-123456789abc)\n"
        guide = SimpleNamespace(task_id=1, markdown=markdown, save=MagicMock())
        self.existing[:] = [guide]
        self.run_import()
        assert guide.markdown.startswith(markdown)
        self.asset_ids_from_markdown.assert_not_called()
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])

    def test_generic_permission_denial_before_any_write(self):
        allowed = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=True))
        denied = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=False))
        self.permission.create_base_perm.side_effect = [allowed, denied]
        with pytest.raises(core.GuideImportError, match="Task 2.*permission denied"):
            self.adapter.import_project_guides(
                named_zip({task.name: "text" for task in self.tasks}),
                self.project,
                input_format="json",
            )
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_generic_partial_match_and_ambiguous_names(self):
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: "text", "SECRET_UNMATCHED": "missing"}),
            self.project,
            input_format="json",
        )
        self.guide_manager.create.assert_called_once_with(task_id=1, markdown="text")
        assert "1 unmatched records" in self.meta.status
        assert "SECRET_UNMATCHED" not in self.meta.status
        self.guide_manager.create.reset_mock()
        self.tasks[1].name = self.tasks[0].name
        with pytest.raises(core.GuideImportError, match="multiple tasks"):
            self.adapter.import_project_guides(
                named_zip({self.tasks[0].name: "text"}), self.project, input_format="json"
            )
        self.guide_manager.create.assert_not_called()

    def test_create_and_scoped_metadata_read(self):
        self.run_import()
        assert self.guide_manager.create.call_count == 2
        filters = self.values_manager.filter.call_args.kwargs
        assert filters["image__job__segment__task__project_id"] == 7
        assert filters["image__label__name"] == "DicomTag"
        assert set(filters["spec__name__in"]) == set(core.IDENTITY_TAGS)
        assert "2 created" in self.meta.status
        self.transaction.atomic.assert_called_once()
        for task in self.tasks:
            task.touch.assert_called_once_with()
        self.project.touch.assert_called_once_with()

    def test_permission_denial_before_any_write(self):
        allowed = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=True))
        denied = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=False))
        self.permission.create_base_perm.side_effect = [allowed, denied]
        with pytest.raises(core.GuideImportError, match="Task 2.*permission denied"):
            self.run_import()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_project_import_permission_is_rechecked_before_reading_tasks(self):
        self.project_permission.create_base_perm.return_value.check_access.return_value.allow = (
            False
        )
        with pytest.raises(core.GuideImportError, match="Project dataset import permission denied"):
            self.run_import()
        self.task_manager.select_for_update.assert_not_called()
        self.guide_manager.create.assert_not_called()
        self.permission.create_base_perm.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_workspace_change_after_enqueue_prevents_import(self):
        self.project.organization_id = 12
        with pytest.raises(core.GuideImportError, match="Project workspace changed"):
            self.run_import()
        self.task_manager.select_for_update.assert_not_called()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_same_organization_is_allowed(self):
        self.project.organization_id = self.meta.org_id = 12
        self.run_import()
        assert self.guide_manager.create.call_count == 2
        self.project_permission.create_base_perm.assert_called_once_with(
            "request", None, "import:dataset", {"user_id": 3}, obj=self.project
        )

    def test_malformed_second_guide_prevents_first_write(self):
        self.existing[:] = [SimpleNamespace(task_id=2, markdown=core.START)]
        with pytest.raises(core.GuideImportError):
            self.run_import()
        self.guide_manager.create.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_unchanged_and_manual_content_preserved(self):
        markdown = core.merge_markdown("manual", reports(record())[0].markdown)
        guide = SimpleNamespace(task_id=1, markdown=markdown, save=MagicMock())
        self.existing[:] = [guide]
        self.run_import()
        guide.save.assert_not_called()
        assert self.guide_manager.create.call_count == 1
        assert "1 unchanged" in self.meta.status
        assert self.permission.create_base_perm.call_args_list[0].args[2] == "update"
        self.tasks[0].touch.assert_not_called()
        self.tasks[1].touch.assert_called_once_with()
        self.project.touch.assert_called_once_with()

    def test_all_guides_unchanged_preserve_task_and_project_timestamps(self):
        markdown = core.merge_markdown("manual", reports(record())[0].markdown)
        self.existing[:] = [
            SimpleNamespace(task_id=task.id, markdown=markdown, save=MagicMock())
            for task in self.tasks
        ]
        self.run_import()
        self.guide_manager.create.assert_not_called()
        for guide in self.existing:
            guide.save.assert_not_called()
        self.assert_no_timestamps_changed()
        assert "2 unchanged" in self.meta.status

    def test_unmatched_task_timestamp_is_preserved(self):
        self.tasks.append(self.timestamped(id=3, name=image(3, patient="P3").name))
        self.run_import()
        self.tasks[2].touch.assert_not_called()
        assert self.guide_manager.create.call_count == 2
        self.project.touch.assert_called_once_with()

    def test_existing_guide_updated_without_replacement(self):
        guide = SimpleNamespace(task_id=1, markdown="manual", save=MagicMock())
        self.existing[:] = [guide]
        self.run_import()
        assert guide.markdown.startswith("manual\n\n")
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])
        assert "1 updated" in self.meta.status
        self.tasks[0].touch.assert_called_once_with()
        self.project.touch.assert_called_once_with()

    def test_unmatched_summary_contains_positions_not_patient_data(self):
        self.run_import([record(), record(patient="SECRET_PATIENT")])
        assert "1 unmatched records" in self.meta.status
        assert "unmatched positions: 2" in self.meta.status
        assert "SECRET_PATIENT" not in self.meta.status

    def test_invalid_records_are_skipped_and_reported_by_position(self):
        missing_date = record(patient="SECRET_PATIENT", date="")
        bad_date = record(patient="SECRET_PATIENT", date="SECRET_DATE")
        self.run_import([missing_date, record(), bad_date, None])
        assert self.guide_manager.create.call_count == 2
        assert "0 unmatched records, 0 duplicate records, 3 invalid records" in self.meta.status
        assert "invalid positions: 1, 3, 4" in self.meta.status
        for secret in ("SECRET_PATIENT", "SECRET_DATE"):
            assert secret not in self.meta.status

    def test_only_invalid_records_fail_without_writes(self):
        with pytest.raises(core.GuideImportError, match="No matching tasks"):
            self.run_import([record(date=""), {"input": {}, "result": {}}])
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_late_invalid_report_record_is_skipped(self, tmp_path):
        path = write_json_zip(
            tmp_path / "late-record.zip",
            (
                b'{"records":[',
                json.dumps(record()).encode("utf-8"),
                b" " * (64 * 1024),
                b', {"input":{},"result":{}}]}',
            ),
        )
        with path.open("rb") as source:
            self.adapter.import_project_guides(source, self.project)
        assert self.guide_manager.create.call_count == 2
        assert "invalid positions: 2" in self.meta.status

    def test_duplicate_streamed_reports_keep_original_unmatched_positions(self):
        unmatched = record(patient="SECRET_PATIENT")
        matched = record()
        self.run_import([unmatched, matched, unmatched, record(patient="P5"), matched])
        assert self.guide_manager.create.call_count == 2
        assert "2 unmatched records, 2 duplicate records" in self.meta.status
        assert "unmatched positions: 1, 4" in self.meta.status
        assert "SECRET_PATIENT" not in self.meta.status

    @pytest.mark.parametrize("last_character", ["あ", "い"])
    def test_duplicate_reports_compare_equal_length_text_beyond_the_first_chunk(
        self, last_character
    ):
        first, last = record(), record()
        prefix = "x" * (64 * 1024)
        first["result"]["findings_ja"] = prefix + "あ"
        last["result"]["findings_ja"] = prefix + last_character
        if last_character == "あ":
            self.run_import([first, first, last])
            assert self.guide_manager.create.call_count == 2
            assert "2 duplicate records" in self.meta.status
        else:
            # Different text is a second candidate: both tasks become ambiguous.
            with pytest.raises(core.GuideImportError, match="No matching tasks"):
                self.run_import([first, first, last])
            self.guide_manager.create.assert_not_called()
            self.meta.save.assert_not_called()
            self.assert_no_timestamps_changed()

    def test_conflicting_report_skips_only_the_affected_tasks(self):
        guide = SimpleNamespace(task_id=1, markdown="manual", save=MagicMock())
        self.existing[:] = [guide]
        self.values[:] = [
            (task.id, tag, next(iter(value)))
            for task in (image(), video(patient="P2"))
            for tag, value in task.tags.items()
        ]
        changed = record()
        changed["result"]["findings_ja"] = "Different"
        self.run_import([record(), record(patient="P2"), changed])
        assert guide.markdown == "manual"
        guide.save.assert_not_called()
        self.guide_manager.create.assert_called_once()
        assert self.guide_manager.create.call_args.kwargs["task_id"] == 2
        assert "2 unmatched records" in self.meta.status
        assert "1 skipped tasks" in self.meta.status
        assert "skipped task IDs: 1" in self.meta.status
        self.tasks[0].touch.assert_not_called()
        self.tasks[1].touch.assert_called_once_with()

    def test_modality_mismatch_skips_only_that_task(self):
        self.values[:] = [
            (task.id, tag, next(iter(value)))
            for task in (image(modality="MR"), video())
            for tag, value in task.tags.items()
        ]
        self.run_import()
        self.guide_manager.create.assert_called_once()
        assert self.guide_manager.create.call_args.kwargs["task_id"] == 2
        assert "skipped task IDs: 1" in self.meta.status

    def test_write_failure_does_not_report_success(self):
        self.guide_manager.create.side_effect = RuntimeError("write failed")
        with pytest.raises(RuntimeError, match="write failed"):
            self.run_import()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_second_write_failure_exits_the_same_transaction_without_reporting_success(self):
        failure = RuntimeError("second write failed")

        def create(**attributes):
            assert self.in_transaction
            if attributes["task_id"] == 2:
                raise failure

        self.guide_manager.create.side_effect = create
        with pytest.raises(RuntimeError, match="second write failed"):
            self.run_import()
        self.transaction.atomic.assert_called_once_with()
        assert not self.in_transaction
        assert self.guide_manager.create.call_count == 2
        self.tasks[0].touch.assert_called_once_with()
        self.tasks[1].touch.assert_not_called()
        self.project.touch.assert_not_called()
        self.meta.save.assert_not_called()

    def test_timestamp_failure_aborts_before_reporting_success(self):
        self.tasks[0].touch.side_effect = RuntimeError("timestamp write failed")
        with pytest.raises(RuntimeError, match="timestamp write failed"):
            self.run_import()
        self.project.touch.assert_not_called()
        self.meta.save.assert_not_called()
        assert not self.in_transaction

    def test_standard_backup_cache_contains_created_and_updated_guides(self, subtests):
        # Execute the standard cache logic unchanged, without importing service dependencies.
        # ORM, locks and exporters are substitutes; ZIP files and cache reuse are real.
        source = ROOT / "cvat/apps/engine/backup.py"
        tree = ast.parse(source.read_text())
        create_backup_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "create_backup"
        )
        backup_module = ast.Module(
            body=[
                ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                create_backup_node,
            ],
            type_ignores=[],
        )
        ast.fix_missing_locations(backup_module)
        self.tasks[:] = self.tasks[:1]
        task = self.tasks[0]

        def create_guide(**attributes):
            assert self.in_transaction
            guide = SimpleNamespace(**attributes, save=MagicMock())
            self.existing.append(guide)
            return guide

        self.guide_manager.create.side_effect = create_guide
        for existing in (False, True):
            for target in (task, self.project):
                with (
                    subtests.test(existing=existing, target_id=target.id),
                    tempfile.TemporaryDirectory() as cache,
                ):
                    self.existing[:] = (
                        [SimpleNamespace(task_id=task.id, markdown="original", save=MagicMock())]
                        if existing
                        else []
                    )
                    exports = MagicMock()
                    member = (
                        "annotation_guide.md"
                        if target is task
                        else f"task_{task.id}/annotation_guide.md"
                    )

                    class Exporter:
                        @staticmethod
                        def get_object(instance_id):
                            return target

                        def __init__(self, instance_id, **kwargs):
                            pass

                        def export_to(exporter, filename):
                            exports()
                            with ZipFile(filename, "w") as archive:
                                if self.existing:
                                    archive.writestr(member, self.existing[0].markdown)

                    namespace = {
                        "timezone": SimpleNamespace(localtime=lambda value: value),
                        "ExportCacheManager": SimpleNamespace(
                            make_backup_file_path=lambda **key: str(
                                Path(cache)
                                / f"{key['instance_id']}_{key['instance_timestamp']}.zip"
                            )
                        ),
                        "get_export_cache_lock": lambda *args, **kwargs: nullcontext(),
                        "EXPORT_CACHE_LOCK_ACQUISITION_TIMEOUT": 10,
                        "EXPORT_CACHE_LOCK_TTL": 30,
                        "extend_export_file_lifetime": MagicMock(),
                        "TmpDirManager": SimpleNamespace(
                            get_tmp_directory_for_export=lambda **kwargs: tempfile.TemporaryDirectory()
                        ),
                        "os": os,
                        "shutil": shutil,
                    }
                    exec(compile(backup_module, str(source), "exec"), namespace)

                    def backup():
                        return namespace["create_backup"](
                            target.id, Exporter, MagicMock(), timedelta(hours=1)
                        )

                    original = backup()
                    assert backup() == original
                    exports.assert_called_once()
                    with ZipFile(original) as archive:
                        if existing:
                            assert archive.read(member) == b"original"
                        else:
                            assert member not in archive.namelist()

                    self.run_import()
                    refreshed = backup()
                    assert refreshed != original
                    assert exports.call_count == 2
                    with ZipFile(refreshed) as archive:
                        markdown = archive.read(member).decode("utf-8")
                        assert markdown == self.existing[0].markdown
                        assert "所見です。" in markdown

                    self.run_import()
                    assert backup() == refreshed
                    assert exports.call_count == 2
