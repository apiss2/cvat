# SPDX-License-Identifier: MIT
"""Run without CVAT services: python -m unittest discover -s tests/python/unit -p test_task_guide_import.py"""

import importlib.util
import io
import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
FORMATS = ROOT / "cvat/apps/dataset_manager/formats"
CORE_NAME = "cvat.apps.dataset_manager.formats.task_guide_core"
spec = importlib.util.spec_from_file_location(CORE_NAME, FORMATS / "task_guide_core.py")
core = importlib.util.module_from_spec(spec)
sys.modules[CORE_NAME] = core
spec.loader.exec_module(core)


def record(uid="1.2.3", patient="P1", date="20250101"):
    return {
        "input": {"study_instance_uid": uid, "patient_id": patient, "study_date": date,
                  "parsed_patient_age": "25Y", "patient_sex": "M", "modality": "CT"},
        "result": {"findings_ja": "所見です。", "impression_ja": "診断です。", "extracted": {
            "part": ["brain"], "history": ["History one", "History two"],
            "objective": "Screening", "findings": "English finding", "impression": "English diagnosis",
        }},
    }


def stream(records):
    return io.BytesIO(json.dumps({"records": records}, ensure_ascii=False).encode("utf-8"))


def reports(*rows):
    return core.read_reports(stream(list(rows)))[0]


def image(task_id=1, uid="1.2.3", patient="P1", date="20250101", prefix="site"):
    return core.TaskIdentity(task_id, f"{prefix}_{core.component(patient)}_{date}_{uid}")


def video(task_id=2, uid="1.2.3"):
    return core.TaskIdentity(task_id, "site_P1_1_20250101_120000_0001_12345678", {
        core.STUDY_UID: {uid}, core.PATIENT_ID: {"P1"}, core.STUDY_DATE: {"20250101"},
    })


class ReportTests(unittest.TestCase):
    def test_minimal_fields_and_no_extras(self):
        r = record()
        r["input"]["institution"] = "DO NOT INCLUDE"
        md = reports(r)[0].markdown
        for value in ("# 概要", "年齢：25Y", "性別：M", "検査方法：CT", "## 検査概要",
                      "検査部位：brain", "検査履歴：History one", "History two", "検査目的：Screening",
                      "## 所見", "所見です。", "## 診断", "診断です。"):
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
                        r["result"][key if preferred == "plain" else key + "_" + preferred] = "chosen"
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
        r["result"]["findings_ja"] = "<script>x</script>\r\n![x](https://example.invalid)\n" + core.END
        md = reports(r)[0].markdown
        self.assertNotIn("<script>", md)
        self.assertNotIn("![x]", md)
        self.assertNotIn(core.END, md)
        self.assertIn("  \n", md)

    def test_aliases_and_iso_date(self):
        r = record()
        r["input"] = {"study_uid": "1.2.3", "uid": "P1", "date": "2025-01-01"}
        report = reports(r)[0]
        self.assertEqual((report.study_uid, report.patient_id, report.date), ("1.2.3", "P1", "20250101"))

    def test_conflicting_aliases_rejected(self):
        r = record()
        r["input"]["study_uid"] = "1.2.4"
        with self.assertRaisesRegex(core.GuideImportError, "Conflicting aliases"):
            reports(r)

    def test_full_uid_required(self):
        for uid in (None, "", "12345678", "1.2.x", "1.2.3x", "1." + "2" * 64):
            with self.subTest(uid=uid), self.assertRaises(core.GuideImportError):
                reports(record(uid=uid))

    def test_invalid_date(self):
        for date in ("20250230", "2025/01/01", "yesterday", 20250101):
            with self.subTest(date=date), self.assertRaises(core.GuideImportError):
                reports(record(date=date))

    def test_bad_json_and_structure(self):
        for raw in (b"", b"\xff", b"{}", b"[]", b'{"records": []}',
                    b'{"records": [], "records": []}', b'{"x": NaN}', b'{"x": 1e999}', b'[[[]]]'):
            with self.subTest(raw=raw), self.assertRaises(core.GuideImportError):
                core.read_reports(io.BytesIO(raw))
        for r in (None, {}, {"input": {}, "result": []}):
            with self.assertRaises(core.GuideImportError):
                reports(r)

    def test_bom_and_top_level_array(self):
        self.assertEqual(len(core.read_reports(io.BytesIO(b"\xef\xbb\xbf" + stream([record()]).read()))[0]), 1)
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


class MatchingTests(unittest.TestCase):
    def test_all_images_and_videos_with_same_full_uid(self):
        tasks = [image(), image(3, prefix="other_site"), video()]
        updates, unmatched = core.plan_updates(reports(record()), tasks)
        self.assertEqual(set(updates), {1, 2, 3})
        self.assertEqual(unmatched, [])

    def test_sanitized_patient_and_arbitrary_prefix(self):
        r = record(patient="A/B_C")
        updates, _ = core.plan_updates(reports(r), [image(patient="A/B_C", prefix="prefix_with_underlines")])
        self.assertEqual(set(updates), {1})

    def test_uid_not_substring_or_short_sop(self):
        for task in (image(uid="1.2.30"), core.TaskIdentity(1, "video_12345678")):
            with self.assertRaisesRegex(core.GuideImportError, "No matching tasks"):
                core.plan_updates(reports(record()), [task])

    def test_unmatched_record_positions(self):
        updates, unmatched = core.plan_updates(reports(record(), record(uid="1.2.4")), [image()])
        self.assertEqual(set(updates), {1})
        self.assertEqual(unmatched, [2])

    def test_name_and_metadata_uid_conflict(self):
        task = image()
        task.tags[core.STUDY_UID] = {"1.2.4"}
        with self.assertRaisesRegex(core.GuideImportError, "conflicting study identities"):
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
        for md in (core.START, core.END, core.END + core.START,
                   core.START * 2 + core.END, core.START + core.END * 2):
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
        self.project = SimpleNamespace(pk=7)
        self.tasks = [SimpleNamespace(id=1, name=image().name), SimpleNamespace(id=2, name=video().name)]
        self.project_manager = MagicMock()
        self.project_manager.select_for_update.return_value.get.return_value = self.project
        self.task_manager = MagicMock()
        self.task_manager.select_for_update.return_value.filter.return_value.exclude.return_value.order_by.return_value = self.tasks
        self.values_manager = MagicMock()
        self.values = [(2, tag, next(iter(value))) for tag, value in video().tags.items()]
        self.values_manager.filter.return_value.order_by.return_value.values_list.return_value.distinct.return_value.iterator.side_effect = lambda: iter(self.values)
        self.guide_manager = MagicMock()
        self.existing = []
        self.guide_manager.select_for_update.return_value.filter.return_value.order_by.side_effect = lambda *a: self.existing
        self.permission = MagicMock()
        self.permission.Scopes = SimpleNamespace(CREATE="create", UPDATE="update")
        self.permission.create_base_perm.return_value.check_access.return_value.allow = True
        self.transaction = MagicMock()
        self.transaction.atomic.side_effect = nullcontext
        self.meta = SimpleNamespace(status="", progress=0, save=MagicMock())
        self.project_data = type("ProjectData", (), {})
        self.error = type("CvatImportError", (Exception,), {})
        self.registration = {}

        def importer(**kwargs):
            self.registration.update(kwargs)
            return lambda func: func

        models = SimpleNamespace(Project=SimpleNamespace(objects=self.project_manager),
                                 Task=SimpleNamespace(objects=self.task_manager),
                                 LabeledImageAttributeVal=SimpleNamespace(objects=self.values_manager),
                                 AnnotationGuide=SimpleNamespace(objects=self.guide_manager))
        dependencies = {
            "cvat.apps.dataset_manager.bindings": module("bindings", CvatImportError=self.error, ProjectData=self.project_data),
            "cvat.apps.dataset_manager.formats.registry": module("registry", importer=importer),
            "django": module("django"),
            "django.db": module("db", transaction=self.transaction, IntegrityError=type("IntegrityError", (Exception,), {})),
            "cvat.apps.engine": module("engine", models=models),
            "cvat.apps.engine.permissions": module("permissions", AnnotationGuidePermission=self.permission),
        }
        self.modules_patch = patch.dict(sys.modules, dependencies)
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        spec = importlib.util.spec_from_file_location(
            "cvat.apps.dataset_manager.formats.task_guide_adapter_test", FORMATS / "task_guide.py")
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.context_patch = patch.object(self.adapter, "_import_context", return_value=("request", {"user_id": 3}, self.meta))
        self.context_patch.start()
        self.addCleanup(self.context_patch.stop)

    def run_import(self, rows=None):
        self.adapter.import_project_guides(stream(rows or [record()]), self.project)

    def test_registration_and_project_only(self):
        self.assertEqual(self.registration, {"name": "Task guide JSON", "version": "1.0", "ext": "JSON"})
        with self.assertRaisesRegex(self.error, "project imports only"):
            self.adapter.import_task_guides(stream([record()]), "unused", object())
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

    def test_permission_denial_before_any_write(self):
        allowed = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=True))
        denied = SimpleNamespace(check_access=lambda: SimpleNamespace(allow=False))
        self.permission.create_base_perm.side_effect = [allowed, denied]
        with self.assertRaisesRegex(core.GuideImportError, "Task 2.*permission denied"):
            self.run_import()
        self.guide_manager.create.assert_not_called()
        self.meta.save.assert_not_called()

    def test_malformed_second_guide_prevents_first_write(self):
        self.existing[:] = [SimpleNamespace(task_id=2, markdown=core.START)]
        with self.assertRaises(core.GuideImportError):
            self.run_import()
        self.guide_manager.create.assert_not_called()

    def test_unchanged_and_manual_content_preserved(self):
        markdown = core.merge_markdown("manual", reports(record())[0].markdown)
        guide = SimpleNamespace(task_id=1, markdown=markdown, save=MagicMock())
        self.existing[:] = [guide]
        self.run_import()
        guide.save.assert_not_called()
        self.assertEqual(self.guide_manager.create.call_count, 1)
        self.assertIn("1 unchanged", self.meta.status)
        self.assertEqual(self.permission.create_base_perm.call_args_list[0].args[2], "update")

    def test_existing_guide_updated_without_replacement(self):
        guide = SimpleNamespace(task_id=1, markdown="manual", save=MagicMock())
        self.existing[:] = [guide]
        self.run_import()
        self.assertTrue(guide.markdown.startswith("manual\n\n"))
        guide.save.assert_called_once_with(update_fields=["markdown", "updated_date"])
        self.assertIn("1 updated", self.meta.status)

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


if __name__ == "__main__":
    unittest.main()
