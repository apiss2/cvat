# SPDX-License-Identifier: MIT
"""Run without CVAT services: python -m unittest discover -s tests/python/unit -p test_task_guide_import.py"""

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

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


def record(uid="1.2.3", patient="P1", date="20250101"):
    return {
        "input": {
            "study_instance_uid": uid,
            "patient_id": patient,
            "study_date": date,
            "parsed_patient_age": "25Y",
            "patient_sex": "M",
            "modality": "CT",
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


def image(task_id=1, uid="1.2.3", patient="P1", date="20250101", prefix="site"):
    return core.TaskIdentity(
        task_id,
        f"{prefix}_{patient.replace('/', '_')}_{date}_{uid.replace('.', '')[-8:]}",
        {core.STUDY_UID: {uid}, core.PATIENT_ID: {patient}, core.STUDY_DATE: {date}},
    )


def video(task_id=2, uid="1.2.3"):
    return core.TaskIdentity(
        task_id,
        "site_P1_1_20250101_120000_0001_12345678",
        {
            core.STUDY_UID: {uid},
            core.PATIENT_ID: {"P1"},
            core.STUDY_DATE: {"20250101"},
        },
    )


class ReportTests(unittest.TestCase):
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
            self.assertIn(value, md)
        for value in ("DO NOT INCLUDE", "P1", "1.2.3", "20250101", "English finding"):
            self.assertNotIn(value, md)

    def test_language_fallbacks_independently(self):
        for key, heading in (("findings", "所見"), ("impression", "診断")):
            for preferred in ("ja", "jp", "extracted", "en", "plain"):
                with self.subTest(key=key, preferred=preferred):
                    r = record()
                    r["result"][key + "_ja"] = " "
                    r["result"]["extracted"].pop(key)
                    if preferred == "extracted":
                        r["result"]["extracted"][key] = "chosen"
                    else:
                        r["result"][
                            key if preferred == "plain" else key + "_" + preferred
                        ] = "chosen"
                    self.assertIn(f"## {heading}\nchosen", reports(r)[0].markdown)

    def test_empty_values_and_zero_age(self):
        r = record()
        r["input"]["parsed_patient_age"] = 0
        r["input"]["patient_sex"] = None
        r["result"] = {"extracted": {"history": [None, " ", []]}}
        md = reports(r)[0].markdown
        self.assertIn("年齢：0", md)
        self.assertNotIn("性別", md)
        self.assertNotIn("## 検査概要", md)
        self.assertIn("## 所見\n記載なし", md)
        self.assertIn("## 診断\n記載なし", md)

    def test_no_invented_translation_or_age_conversion(self):
        r = record()
        r["input"]["parsed_patient_age"] = "005M"
        md = reports(r)[0].markdown
        self.assertIn("005M", md)
        self.assertIn("brain", md)
        self.assertNotIn("脳", md)

    def test_plain_text_escaping_and_line_breaks(self):
        r = record()
        r["result"]["findings_ja"] = (
            "<script>x</script>\r\n![x](https://example.invalid)\n" + core.END
        )
        md = reports(r)[0].markdown
        self.assertNotIn("<script>", md)
        self.assertNotIn("![x]", md)
        self.assertNotIn(core.END, md)
        self.assertIn("  \n", md)

    def test_aliases_and_iso_date(self):
        r = record()
        r["input"] = {"study_uid": "1.2.3", "pid": "P1", "date": "2025-01-01"}
        report = reports(r)[0]
        self.assertEqual(
            (report.study_uid, report.patient_id, report.date), ("1.2.3", "P1", "20250101")
        )

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
        self.assertEqual(report.study_uid, r["input"]["uid"])
        self.assertEqual((report.patient_id, report.date), ("ABC_DEFGH", "20250101"))
        for value in ("年齢：21Y", "性別：M", "検査方法：CT"):
            self.assertIn(value, report.markdown)

    def test_overview_prefers_nonempty_canonical_fields(self):
        r = record()
        r["input"].update(age="21Y", sex="F", mod="MR")
        md = reports(r)[0].markdown
        for value in ("年齢：25Y", "性別：M", "検査方法：CT"):
            self.assertIn(value, md)
        r["input"].update(parsed_patient_age=" ", patient_sex=None, modality="")
        md = reports(r)[0].markdown
        for value in ("年齢：21Y", "性別：F", "検査方法：MR"):
            self.assertIn(value, md)

    def test_uid_is_a_study_alias_only(self):
        r = record()
        r["input"] = {"uid": "1.2.3"}
        report = reports(r)[0]
        self.assertEqual((report.study_uid, report.patient_id), ("1.2.3", ""))

    def test_equivalent_identifier_aliases_are_accepted(self):
        r = record()
        r["input"].update(
            study_uid="1.2.3",
            uid="1.2.3",
            pid=" P1 ",
            date="2025-01-01",
            dt="Wed, 01 Jan 2025 00:00:00 GMT",
        )
        report = reports(r)[0]
        self.assertEqual(
            (report.study_uid, report.patient_id, report.date), ("1.2.3", "P1", "20250101")
        )

    def test_conflicting_aliases_rejected(self):
        for alias, value in (
            ("study_uid", "1.2.4"),
            ("uid", "1.2.4"),
            ("uid", "P1"),
            ("pid", "OTHER"),
            ("date", "20250102"),
            ("dt", "2025-01-02 00:00:00"),
        ):
            r = record()
            r["input"][alias] = value
            with (
                self.subTest(alias=alias, value=value),
                self.assertRaisesRegex(core.GuideImportError, "Conflicting aliases"),
            ):
                reports(r)

    def test_supported_dates_keep_the_written_calendar_date(self):
        for value, expected in (
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
        ):
            with self.subTest(value=value):
                self.assertEqual(reports(record(date=value))[0].date, expected)

    def test_full_uid_required(self):
        for uid in (None, "", "12345678", "1.2.x", "1.2.3x", "1." + "2" * 64):
            with self.subTest(uid=uid), self.assertRaises(core.GuideImportError):
                reports(record(uid=uid))

    def test_invalid_date(self):
        for date in (
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
        ):
            with self.subTest(date=date), self.assertRaises(core.GuideImportError):
                reports(record(date=date))

    def test_reference_text_is_the_last_section_and_escaped(self):
        r = record()
        r["input_text"] = (
            "History: original\r\nObjective: unchanged\rFindings: original\n"
            "![image](https://example.invalid)\n<script>text</script>\n" + core.END
        )
        md = reports(r)[0].markdown
        reference = md.split("## 参考生データ\n", 1)[1]
        self.assertGreater(md.index("## 参考生データ"), md.index("## 診断"))
        self.assertIn("History: original  \nObjective: unchanged  \nFindings: original", reference)
        for raw in ("![image]", "<script>", core.END, "\r"):
            self.assertNotIn(raw, reference)
        self.assertIn("&lt;script&gt;text&lt;/script&gt;", reference)
        self.assertTrue(md.endswith(reference))

    def test_optional_reference_and_reference_only_report(self):
        for value in (None, "", " \r\n "):
            r = record()
            r["input_text"] = value
            with self.subTest(value=value):
                self.assertNotIn("参考生データ", reports(r)[0].markdown)
        r = {"input": {"uid": "1.2.3"}, "result": {}, "input_text": "Original report"}
        self.assertIn("## 参考生データ\nOriginal report", reports(r)[0].markdown)

    def test_reference_validation_and_guide_size_limit(self):
        for value in (123, True, ["text"], {"text": "report"}, "bad\x00text"):
            r = record()
            r["input_text"] = value
            with self.subTest(value=value), self.assertRaises(core.GuideImportError):
                reports(r)
        r = record()
        base_size = len(reports(r)[0].markdown)
        r["input_text"] = "reference"
        with (
            patch.object(core, "MAX_GUIDE_CHARS", base_size),
            self.assertRaisesRegex(core.GuideImportError, "size limit"),
        ):
            reports(r)

    def test_two_sample_formats_from_json_zip(self):
        fixtures = ROOT / "tests/python/unit/fixtures/task_guide"
        for filename, dates, suffixes in (
            ("structured_report.json", ["20250102", "20250107"], ["05522970", "02065974"]),
            ("ptc_report.json", ["20250101", "20250102"], ["30673432", "30694803"]),
        ):
            with self.subTest(filename=filename):
                raw = (fixtures / filename).read_bytes()
                rows = json.loads(raw)["records"]
                parsed, duplicates = core.read_reports(
                    io.BytesIO(core.read_json_zip(zip_stream([(filename, raw)])))
                )
                self.assertEqual(duplicates, 0)
                self.assertEqual([r.date for r in parsed], dates)
                self.assertEqual([r.study_uid[-8:] for r in parsed], suffixes)
                tasks = []
                for task_id, (row, report, date) in enumerate(zip(rows, parsed, dates), 1):
                    source = row["input"]
                    uid = source.get("study_instance_uid", source.get("uid"))
                    patient = source.get("patient_id", source.get("pid"))
                    self.assertEqual((report.study_uid, report.patient_id), (uid, patient))
                    for label in ("年齢", "性別", "検査方法", "既往歴", "検査部位", "検査目的"):
                        self.assertIn(label + "：", report.markdown)
                    self.assertIn("## 参考生データ\nHistory: Synthetic", report.markdown)
                    tasks.append(image(task_id, uid=uid, patient=patient, date=date))
                updates, unmatched = core.plan_updates(parsed, tasks)
                self.assertEqual(set(updates), {1, 2})
                self.assertEqual(unmatched, [])

    def test_bad_json_and_structure(self):
        for raw in (
            b"",
            b"\xff",
            b"{}",
            b"[]",
            b'{"records": []}',
            b'{"records": [], "records": []}',
            b'{"x": NaN}',
            b'{"x": 1e999}',
            b"[[[]]]",
        ):
            with self.subTest(raw=raw), self.assertRaises(core.GuideImportError):
                core.read_reports(io.BytesIO(raw))
        for r in (None, {}, {"input": {}, "result": []}):
            with self.assertRaises(core.GuideImportError):
                reports(r)

    def test_bom_and_top_level_array(self):
        self.assertEqual(
            len(core.read_reports(io.BytesIO(b"\xef\xbb\xbf" + stream([record()]).read()))[0]), 1
        )
        self.assertEqual(len(core.read_reports(io.BytesIO(json.dumps([record()]).encode()))[0]), 1)

    def test_limits(self):
        with patch.object(core, "MAX_FILE_BYTES", 5), self.assertRaises(core.GuideImportError):
            reports(record())
        with patch.object(core, "MAX_RECORDS", 1), self.assertRaises(core.GuideImportError):
            reports(record(), record())
        with patch.object(core, "MAX_GUIDE_CHARS", 5), self.assertRaises(core.GuideImportError):
            reports(record())

    def test_unsupported_selected_value_is_not_silently_dropped(self):
        for value in ({"nested": "value"}, True, "\x00"):
            r = record()
            r["result"]["extracted"]["history"] = value
            with self.assertRaises(core.GuideImportError):
                reports(r)

    def test_duplicate_reports(self):
        unique, duplicates = core.read_reports(stream([record(), record()]))
        self.assertEqual((len(unique), duplicates), (1, 1))
        changed = record()
        changed["result"]["findings_ja"] = "Different"
        with self.assertRaisesRegex(core.GuideImportError, "Conflicting reports"):
            reports(record(), changed)

    def test_different_reference_text_is_not_deduplicated(self):
        first, second = record(), record()
        first["input_text"] = "Original report"
        second["input_text"] = "Different original report"
        with self.assertRaisesRegex(core.GuideImportError, "Conflicting reports"):
            reports(first, second)


class MatchingTests(unittest.TestCase):
    def test_all_images_and_videos_with_same_full_uid(self):
        tasks = [image(), image(3, prefix="other_site"), video()]
        updates, unmatched = core.plan_updates(reports(record()), tasks)
        self.assertEqual(set(updates), {1, 2, 3})
        self.assertEqual(unmatched, [])

    def test_sanitized_patient_and_arbitrary_prefix(self):
        r = record(patient="A/B_C")
        updates, _ = core.plan_updates(
            reports(r), [image(patient="A/B_C", prefix="prefix_with_underlines")]
        )
        self.assertEqual(set(updates), {1})

    def test_prefixed_patient_matches_without_name_inference(self):
        r = record(patient="ABC_DEFGH", date="Tue, 07 Jan 2025 00:00:00 GMT")
        task = image(patient="ABC_DEFGH", date="20250107", prefix="ABC")
        task.name = "ABC_DEFGH_20250107_123"
        self.assertEqual(set(core.plan_updates(reports(r), [task])[0]), {1})
        task.name = "renamed task"
        self.assertEqual(set(core.plan_updates(reports(r), [task])[0]), {1})
        for different_patient in ("DEFGH", "OTHER_DEFGH", "ABCDEFGH"):
            with (
                self.subTest(patient=different_patient),
                self.assertRaisesRegex(core.GuideImportError, "patient ID mismatch"),
            ):
                core.plan_updates(
                    reports(record(patient=different_patient, date="20250107")), [task]
                )

    def test_uid_not_substring_or_short_sop(self):
        for task in (image(uid="1.2.30"), core.TaskIdentity(1, "video_12345678")):
            with self.assertRaisesRegex(core.GuideImportError, "No matching tasks"):
                core.plan_updates(reports(record()), [task])

    def test_unmatched_record_positions(self):
        updates, unmatched = core.plan_updates(reports(record(), record(uid="1.2.4")), [image()])
        self.assertEqual(set(updates), {1})
        self.assertEqual(unmatched, [2])

    def test_name_is_not_used_as_a_source_of_study_identity(self):
        task = image()
        task.name = "site_OTHER_19991231_9.9.9"
        self.assertEqual(set(core.plan_updates(reports(record()), [task])[0]), {1})
        task.tags.clear()
        task.name = "site_P1_20250101_1.2.3"
        with self.assertRaisesRegex(core.GuideImportError, "No matching tasks"):
            core.plan_updates(reports(record()), [task])

    def test_mixed_study_task_rejected(self):
        task = video()
        task.tags[core.STUDY_UID].add("1.2.4")
        with self.assertRaises(core.GuideImportError):
            core.plan_updates(reports(record()), [task])

    def test_patient_date_and_name_checks(self):
        for task in (image(patient="WRONG"), image(date="20250102")):
            with self.assertRaises(core.GuideImportError):
                core.plan_updates(reports(record()), [task])
        for key, value in ((core.PATIENT_ID, "WRONG"), (core.STUDY_DATE, "20250102")):
            task = video()
            task.tags[key] = {value}
            with self.assertRaises(core.GuideImportError):
                core.plan_updates(reports(record()), [task])

    def test_full_uid_only_and_renamed_task(self):
        r = record()
        r["input"].pop("patient_id")
        r["input"].pop("study_date")
        task = core.TaskIdentity(9, "renamed", {core.STUDY_UID: {"1.2.3"}})
        self.assertEqual(set(core.plan_updates(reports(r), [task])[0]), {9})

    def test_shortened_study_names_use_full_metadata_uid_for_images_and_videos(self):
        tags = {
            "StudyInstanceUID": {"1.2.12345678"},
            "PatientID": {"P1"},
            "StudyDate": {"20250101"},
        }
        tasks = [
            core.TaskIdentity(1, "site_P1_20250101_12345678", tags),
            core.TaskIdentity(2, video().name, tags),
        ]
        updates, unmatched = core.plan_updates(reports(record(uid="1.2.12345678")), tasks)
        self.assertEqual(set(updates), {1, 2})
        self.assertEqual(unmatched, [])

    def test_identical_short_uid_suffix_does_not_match_another_study(self):
        tasks = [
            core.TaskIdentity(
                1, "site_P1_20250101_12345678", {"StudyInstanceUID": {"1.2.12345678"}}
            ),
            core.TaskIdentity(
                2, "site_P1_20250101_12345678", {"StudyInstanceUID": {"1.3.12345678"}}
            ),
        ]
        updates, _ = core.plan_updates(reports(record(uid="1.2.12345678")), tasks)
        self.assertEqual(set(updates), {1})
        with self.assertRaisesRegex(core.GuideImportError, "No matching tasks"):
            core.plan_updates(
                reports(record(uid="1.2.12345678")), [core.TaskIdentity(3, tasks[0].name)]
            )

    def test_only_keyword_metadata_is_used_for_identity(self):
        task = core.TaskIdentity(1, "site_P1_20250101_1.2.3", {"0020000D": {"1.2.3"}})
        with self.assertRaisesRegex(core.GuideImportError, "No matching tasks"):
            core.plan_updates(reports(record()), [task])


class JsonZipTests(unittest.TestCase):
    def test_one_json_file_in_a_directory_is_read_without_changing_content(self):
        raw = named_stream({"task": "# Markdown\n"}).read()
        source = zip_stream([("guides/", ""), ("guides/data.JSON", raw)])
        self.assertEqual(core.read_json_zip(source), raw)

    def test_json_archive_requires_exactly_one_json_file(self):
        for members in (
            [],
            [("directory/", "")],
            [("a.json", "{}"), ("b.json", "{}")],
            [("one/report.json", "{}"), ("two/report.json", "{}")],
        ):
            with (
                self.subTest(members=members),
                self.assertRaisesRegex(core.GuideImportError, "exactly one JSON"),
            ):
                core.read_json_zip(zip_stream(members))

    def test_json_archive_rejects_extra_files_and_unsafe_paths(self):
        for members in (
            [("data.json", "{}"), ("README.txt", "text")],
            [("data.md", "{}")],
            [("../data.json", "{}")],
        ):
            with self.subTest(members=members), self.assertRaises(core.GuideImportError):
                core.read_json_zip(zip_stream(members))

    def test_raw_json_is_not_an_accepted_upload(self):
        with self.assertRaisesRegex(core.GuideImportError, "ZIP"):
            core.read_json_zip(named_stream({"task": "text"}))


class NamedGuideTests(unittest.TestCase):
    def test_json_preserves_task_name_and_complete_markdown(self):
        markdown = "# 見出し\r\n\n![image](/api/assets/a)\n  text  \n"
        guides = core.read_named_json(named_stream({" task 名 ": markdown, "empty": ""}))
        self.assertEqual(
            guides, [core.NamedGuide(1, " task 名 ", markdown), core.NamedGuide(2, "empty", "")]
        )

    def test_json_rejects_bad_structure_names_and_text(self):
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
            with self.subTest(raw=raw), self.assertRaises(core.GuideImportError):
                core.read_named_json(io.BytesIO(raw))

    def test_json_size_and_entry_limits(self):
        for constant, limit, mapping in (
            ("MAX_FILE_BYTES", 5, {"task": "text"}),
            ("MAX_RECORDS", 1, {"a": "text", "b": "text"}),
            ("MAX_GUIDE_CHARS", 3, {"task": "text"}),
        ):
            with patch.object(core, constant, limit), self.assertRaises(core.GuideImportError):
                core.read_named_json(named_stream(mapping))

    def test_name_matching_is_exact_and_reports_unmatched_positions(self):
        guides = core.read_named_json(named_stream({"task A": "a", "task B": "b", "TASK A": "c"}))
        updates, unmatched = core.plan_named_updates(guides, [core.TaskIdentity(1, "task A")])
        self.assertEqual(updates, {1: "a"})
        self.assertEqual(unmatched, [2, 3])
        with self.assertRaisesRegex(core.GuideImportError, "No matching tasks"):
            core.plan_named_updates(guides, [core.TaskIdentity(2, "task A ")])

    def test_ambiguous_target_name_rejected(self):
        guides = core.read_named_json(named_stream({"task": "text"}))
        with self.assertRaisesRegex(core.GuideImportError, "multiple tasks"):
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
        self.assertEqual(
            guides, [core.NamedGuide(1, "日本語", "# 日本語\r\n"), core.NamedGuide(2, "empty", "")]
        )

    def test_zip_rejects_unsafe_paths_non_markdown_and_duplicate_task_names(self):
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
            with self.subTest(members=members), self.assertRaises(core.GuideImportError):
                core.read_markdown_zip(zip_stream(members))
        info = ZipInfo("task.md")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(core.GuideImportError, "regular files"):
            core.read_markdown_zip(zip_stream([(info, "elsewhere")]))
        with self.assertRaisesRegex(core.GuideImportError, "Invalid"):
            core.read_markdown_zip(io.BytesIO(b"not a zip"))

    def test_zip_size_entry_and_total_uncompressed_limits(self):
        with patch.object(core, "MAX_FILE_BYTES", 5), self.assertRaises(core.GuideImportError):
            core.read_markdown_zip(zip_stream([("task.md", "text")]))
        with patch.object(core, "MAX_RECORDS", 1), self.assertRaises(core.GuideImportError):
            core.read_markdown_zip(zip_stream([("a.md", "a"), ("b.md", "b")]))
        with patch.object(core, "MAX_GUIDE_CHARS", 3), self.assertRaises(core.GuideImportError):
            core.read_markdown_zip(zip_stream([("task.md", "text")]))
        with (
            patch.object(core, "MAX_FILE_BYTES", 1024),
            self.assertRaisesRegex(core.GuideImportError, "Uncompressed"),
        ):
            core.read_markdown_zip(zip_stream([("a.md", "a" * 600), ("b.md", "b" * 600)]))


class MergeTests(unittest.TestCase):
    def test_append_preserves_manual_text_and_assets(self):
        manual = "# Manual\n![asset](/api/assets/1234)\n"
        md = core.merge_markdown(manual, "# 概要\nFirst\n")
        self.assertTrue(md.startswith(manual))
        self.assertEqual(md.count(core.START), 1)

    def test_idempotence_and_replace_only_block(self):
        md = core.merge_markdown("manual", "old") + "manual suffix\n"
        updated = core.merge_markdown(md, "new")
        self.assertTrue(updated.startswith("manual\n\n"))
        self.assertTrue(updated.endswith("manual suffix\n"))
        self.assertNotIn("old", updated)
        self.assertEqual(core.merge_markdown(updated, "new"), updated)

    def test_broken_markers(self):
        for md in (
            core.START,
            core.END,
            core.END + core.START,
            core.START * 2 + core.END,
            core.START + core.END * 2,
        ):
            with self.subTest(md=md), self.assertRaises(core.GuideImportError):
                core.merge_markdown(md, "new")

    def test_size_and_invalid_unicode(self):
        with patch.object(core, "MAX_GUIDE_CHARS", 1), self.assertRaises(core.GuideImportError):
            core.merge_markdown("", "new")
        with self.assertRaises(core.GuideImportError):
            core.merge_markdown("", "\ud800")


def module(name, **attributes):
    obj = ModuleType(name)
    obj.__dict__.update(attributes)
    return obj


class AdapterTests(unittest.TestCase):
    """Contract tests with mocked ORM/OPA/RQ, not CVAT/PostgreSQL integration tests."""

    def setUp(self):
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
        self.guide_manager.select_for_update.return_value.filter.return_value.order_by.side_effect = (
            lambda *a: self.existing
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
        self.addCleanup(self.modules_patch.stop)
        spec = importlib.util.spec_from_file_location(
            "cvat.apps.dataset_manager.formats.task_guide_adapter_test", FORMATS / "task_guide.py"
        )
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.context_patch = patch.object(
            self.adapter, "_import_context", return_value=("request", {"user_id": 3}, self.meta)
        )
        self.context_patch.start()
        self.addCleanup(self.context_patch.stop)

    def timestamped(self, **attributes):
        obj = SimpleNamespace(updated_date=datetime(2026, 1, 1, tzinfo=timezone.utc), **attributes)

        def touch():
            self.assertTrue(
                self.in_transaction, "Timestamp writes must share the guide transaction"
            )
            obj.updated_date += timedelta(seconds=1)

        obj.touch = MagicMock(side_effect=touch)
        return obj

    def assert_no_timestamps_changed(self):
        for obj in [self.project, *self.tasks]:
            obj.touch.assert_not_called()

    def run_import(self, rows=None):
        self.adapter.import_project_guides(report_zip(rows or [record()]), self.project)

    def test_registration_and_project_only(self):
        self.assertEqual(
            self.registration,
            {
                name: {"name": name, "version": "1.0", "ext": ext}
                for name, ext in (
                    ("Task Guide JSON", "ZIP"),
                    ("Task Guide Markdown Zip", "ZIP"),
                    ("Task Guide DICOM Report Import", "ZIP"),
                )
            },
        )
        for importer in (
            self.adapter.import_task_guides,
            self.adapter.import_task_guides_markdown_zip,
            self.adapter.import_task_guides_dicom_report,
        ):
            with self.assertRaisesRegex(self.error, "project imports only"):
                importer(io.BytesIO(), "unused", object())
        self.guide_manager.create.assert_not_called()

    def test_registered_formats_route_their_actual_payloads(self):
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
            with self.subTest(importer=importer.__name__):
                self.guide_manager.create.reset_mock()
                importer(payload, "unused", instance)
                self.assertTrue(self.guide_manager.create.called)
        self.assertEqual(self.guide_manager.create.call_count, 2)

    def test_registered_formats_report_invalid_input_without_writes(self):
        instance = self.project_data()
        instance.db_project = self.project
        for importer, raw in (
            (self.adapter.import_task_guides, b"[]"),
            (self.adapter.import_task_guides_markdown_zip, b"not a zip"),
            (self.adapter.import_task_guides_dicom_report, b"{}"),
        ):
            with self.subTest(importer=importer.__name__), self.assertRaises(self.error):
                importer(io.BytesIO(raw), "unused", instance)
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_json_formats_validate_the_contents_of_the_uploaded_archive(self):
        instance = self.project_data()
        instance.db_project = self.project
        for importer, raw in (
            (self.adapter.import_task_guides, b"[]"),
            (self.adapter.import_task_guides_dicom_report, b"{}"),
            (self.adapter.import_task_guides, b"\xff"),
        ):
            with self.subTest(importer=importer.__name__), self.assertRaises(self.error):
                importer(zip_stream([("data.json", raw)]), "unused", instance)
        self.guide_manager.create.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_generic_import_replaces_entire_markdown_and_preserves_exact_text(self):
        guide = SimpleNamespace(task_id=1, markdown="old manual text", save=MagicMock())
        self.existing[:] = [guide]
        text = "# 新しい説明\r\n\n  paragraph  \n"
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: text}), self.project, input_format="json"
        )
        self.assertEqual(guide.markdown, text)
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
        self.assertIn("1 unchanged", self.meta.status)

    def test_generic_import_accepts_empty_markdown_and_tasks_without_media(self):
        self.adapter.import_project_guides(
            named_zip({self.tasks[0].name: ""}), self.project, input_format="json"
        )
        self.guide_manager.create.assert_called_once_with(task_id=1, markdown="")
        self.task_manager.select_for_update.return_value.filter.return_value.exclude.assert_not_called()

    def test_generic_new_guide_asset_reference_prevents_all_writes(self):
        markdown = "![image](/api/assets/12345678-1234-5678-9abc-123456789abc)"
        for input_format in ("json", "zip"):
            mapping = {self.tasks[0].name: "valid first guide", self.tasks[1].name: markdown}
            payload = (
                named_zip(mapping)
                if input_format == "json"
                else zip_stream([(name + ".md", text) for name, text in mapping.items()])
            )
            with (
                self.subTest(input_format=input_format),
                self.assertRaisesRegex(core.GuideImportError, "Task 2.*assets already owned"),
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
        self.assertEqual(guide.markdown, markdown)
        assets.filter.assert_called_once_with(uuid__in={asset_id})
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])
        self.guide_manager.create.assert_not_called()

    def test_generic_guide_rejects_foreign_or_unknown_asset_references(self):
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
                self.subTest(available=available),
                self.assertRaisesRegex(core.GuideImportError, "assets already owned"),
            ):
                self.adapter.import_project_guides(
                    named_zip({self.tasks[0].name: markdown}), self.project, input_format="json"
                )
            self.assertEqual(guide.markdown, "original")
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
        self.assertTrue(guide.markdown.startswith(markdown))
        self.asset_ids_from_markdown.assert_not_called()
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])

    def test_generic_permission_denial_before_any_write(self):
        allowed = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=True))
        denied = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=False))
        self.permission.create_base_perm.side_effect = [allowed, denied]
        with self.assertRaisesRegex(core.GuideImportError, "Task 2.*permission denied"):
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
        self.assertIn("1 unmatched records", self.meta.status)
        self.assertNotIn("SECRET_UNMATCHED", self.meta.status)
        self.guide_manager.create.reset_mock()
        self.tasks[1].name = self.tasks[0].name
        with self.assertRaisesRegex(core.GuideImportError, "multiple tasks"):
            self.adapter.import_project_guides(
                named_zip({self.tasks[0].name: "text"}), self.project, input_format="json"
            )
        self.guide_manager.create.assert_not_called()

    def test_create_and_scoped_metadata_read(self):
        self.run_import()
        self.assertEqual(self.guide_manager.create.call_count, 2)
        filters = self.values_manager.filter.call_args.kwargs
        self.assertEqual(filters["image__job__segment__task__project_id"], 7)
        self.assertEqual(filters["image__label__name"], "DicomTag")
        self.assertEqual(set(filters["spec__name__in"]), set(core.IDENTITY_TAGS))
        self.assertIn("2 created", self.meta.status)
        self.transaction.atomic.assert_called_once()
        for task in self.tasks:
            task.touch.assert_called_once_with()
        self.project.touch.assert_called_once_with()

    def test_permission_denial_before_any_write(self):
        allowed = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=True))
        denied = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=False))
        self.permission.create_base_perm.side_effect = [allowed, denied]
        with self.assertRaisesRegex(core.GuideImportError, "Task 2.*permission denied"):
            self.run_import()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_project_import_permission_is_rechecked_before_reading_tasks(self):
        self.project_permission.create_base_perm.return_value.check_access.return_value.allow = (
            False
        )
        with self.assertRaisesRegex(
            core.GuideImportError, "Project dataset import permission denied"
        ):
            self.run_import()
        self.task_manager.select_for_update.assert_not_called()
        self.guide_manager.create.assert_not_called()
        self.permission.create_base_perm.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_workspace_change_after_enqueue_prevents_import(self):
        self.project.organization_id = 12
        with self.assertRaisesRegex(core.GuideImportError, "Project workspace changed"):
            self.run_import()
        self.task_manager.select_for_update.assert_not_called()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_same_organization_is_allowed(self):
        self.project.organization_id = self.meta.org_id = 12
        self.run_import()
        self.assertEqual(self.guide_manager.create.call_count, 2)
        self.project_permission.create_base_perm.assert_called_once_with(
            "request", None, "import:dataset", {"user_id": 3}, obj=self.project
        )

    def test_malformed_second_guide_prevents_first_write(self):
        self.existing[:] = [SimpleNamespace(task_id=2, markdown=core.START)]
        with self.assertRaises(core.GuideImportError):
            self.run_import()
        self.guide_manager.create.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_unchanged_and_manual_content_preserved(self):
        markdown = core.merge_markdown("manual", reports(record())[0].markdown)
        guide = SimpleNamespace(task_id=1, markdown=markdown, save=MagicMock())
        self.existing[:] = [guide]
        self.run_import()
        guide.save.assert_not_called()
        self.assertEqual(self.guide_manager.create.call_count, 1)
        self.assertIn("1 unchanged", self.meta.status)
        self.assertEqual(self.permission.create_base_perm.call_args_list[0].args[2], "update")
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
        self.assertIn("2 unchanged", self.meta.status)

    def test_unmatched_task_timestamp_is_preserved(self):
        self.tasks.append(self.timestamped(id=3, name=image(3, uid="1.2.4").name))
        self.run_import()
        self.tasks[2].touch.assert_not_called()
        self.assertEqual(self.guide_manager.create.call_count, 2)
        self.project.touch.assert_called_once_with()

    def test_existing_guide_updated_without_replacement(self):
        guide = SimpleNamespace(task_id=1, markdown="manual", save=MagicMock())
        self.existing[:] = [guide]
        self.run_import()
        self.assertTrue(guide.markdown.startswith("manual\n\n"))
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])
        self.assertIn("1 updated", self.meta.status)
        self.tasks[0].touch.assert_called_once_with()
        self.project.touch.assert_called_once_with()

    def test_unmatched_summary_contains_positions_not_patient_data(self):
        self.run_import([record(), record(uid="1.2.4", patient="SECRET_PATIENT")])
        self.assertIn("1 unmatched records", self.meta.status)
        self.assertIn("unmatched positions: 2", self.meta.status)
        self.assertNotIn("SECRET_PATIENT", self.meta.status)

    def test_write_failure_does_not_report_success(self):
        self.guide_manager.create.side_effect = RuntimeError("write failed")
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            self.run_import()
        self.meta.save.assert_not_called()
        self.assert_no_timestamps_changed()

    def test_timestamp_failure_aborts_before_reporting_success(self):
        self.tasks[0].touch.side_effect = RuntimeError("timestamp write failed")
        with self.assertRaisesRegex(RuntimeError, "timestamp write failed"):
            self.run_import()
        self.project.touch.assert_not_called()
        self.meta.save.assert_not_called()
        self.assertFalse(self.in_transaction)

    def test_standard_backup_cache_contains_created_and_updated_guides(self):
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
            self.assertTrue(self.in_transaction)
            guide = SimpleNamespace(**attributes, save=MagicMock())
            self.existing.append(guide)
            return guide

        self.guide_manager.create.side_effect = create_guide
        for existing in (False, True):
            for target in (task, self.project):
                with (
                    self.subTest(existing=existing, target_id=target.id),
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
                    self.assertEqual(backup(), original)
                    exports.assert_called_once()
                    with ZipFile(original) as archive:
                        if existing:
                            self.assertEqual(archive.read(member), b"original")
                        else:
                            self.assertNotIn(member, archive.namelist())

                    self.run_import()
                    refreshed = backup()
                    self.assertNotEqual(refreshed, original)
                    self.assertEqual(exports.call_count, 2)
                    with ZipFile(refreshed) as archive:
                        markdown = archive.read(member).decode("utf-8")
                        self.assertEqual(markdown, self.existing[0].markdown)
                        self.assertIn("所見です。", markdown)

                    self.run_import()
                    self.assertEqual(backup(), refreshed)
                    self.assertEqual(exports.call_count, 2)


if __name__ == "__main__":
    unittest.main()
