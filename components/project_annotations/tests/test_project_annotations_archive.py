# SPDX-License-Identifier: MIT
"""Exercise real ZIP parsing/repacking without Django or a running CVAT server."""

import io
import stat
import tempfile
import warnings
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from components.project_annotations import ProjectAnnotationImportError
from components.project_annotations import archive as subject


def zipped(entries):
    stream = io.BytesIO()
    with warnings.catch_warnings(), ZipFile(stream, "w", ZIP_DEFLATED) as result:
        warnings.simplefilter("ignore", UserWarning)
        for name, value in entries:
            result.writestr(name, value)
    stream.seek(0)
    return stream


class TestArchive:
    def plan(self, entries, tasks):
        source = zipped(entries)
        return subject.plan_folders(subject.inspect_zip(source), tasks)

    def test_exact_task_names_and_unmatched(self):
        plan = self.plan(
            [("Task/annotations.xml", b"x"), ("task/annotations.xml", b"y")], [(1, "Task")]
        )
        assert plan.targets == {1: ("Task",)}
        assert plan.unmatched == 1

    def test_windows_parent_does_not_depend_on_zip_filename(self):
        plan = self.plan(
            [("arbitrary/Task/annotations.xml", b"x"), ("arbitrary/Other/a.xml", b"y")],
            [(1, "Task"), (2, "Other")],
        )
        assert plan.targets == {1: ("arbitrary", "Task"), 2: ("arbitrary", "Other")}

    def test_exact_task_wins_over_possible_wrapper(self):
        plan = self.plan([("A/B/a.xml", b"x")], [(1, "A"), (2, "B")])
        assert plan.targets == {1: ("A",)}

    def test_only_one_wrapper_is_removed(self):
        plan = self.plan([("outer/inner/Task/a.xml", b"x")], [(1, "Task")])
        assert not plan.targets
        assert plan.unmatched == 1

    def test_duplicate_tasks_fail_both_without_poisoning_unrelated_task(self):
        plan = self.plan([("A/a.xml", b"x"), ("B/a.xml", b"y")], [(1, "A"), (2, "A"), (3, "B")])
        assert plan.failed_tasks == {1, 2}
        assert plan.targets == {3: ("B",)}

    def test_loose_root_files_fail_before_task_processing(self):
        with pytest.raises(ProjectAnnotationImportError):
            self.plan([("A/a.xml", b"x"), ("a.xml", b"y")], [(1, "A")])

    def test_wrapper_with_loose_file_is_rejected(self):
        with pytest.raises(ProjectAnnotationImportError):
            self.plan([("outer/A/a.xml", b"x"), ("outer/readme.txt", b"x")], [(1, "A")])

    @pytest.mark.parametrize(
        "name",
        (
            "../Task/a.xml",
            "/Task/a.xml",
            "C:/Task/a.xml",
            "Task/../a.xml",
            "Task//a.xml",
            "Task/./a.xml",
            "Task\\a.xml",
            "Task/a\n.xml",
        ),
    )
    def test_unsafe_paths(self, name):
        with pytest.raises(ProjectAnnotationImportError):
            subject.inspect_zip(zipped([(name, b"x")]))

    def test_nul_in_raw_zip_filename(self):
        # Python's ZIP writer truncates NUL names, so inject it in both raw headers.
        raw = zipped([("Task/a#.xml", b"x")]).getvalue().replace(b"a#.xml", b"a\x00.xml")
        with pytest.raises(ProjectAnnotationImportError):
            subject.inspect_zip(io.BytesIO(raw))

    @pytest.mark.parametrize(
        "entries",
        (
            [("A/a.xml", b"x"), ("A/a.xml", b"y")],
            [("A/a.xml", b"x"), ("A/a.xml/b", b"y")],
            [("A/a.xml/b", b"y"), ("A/a.xml", b"x")],
            [("A/a.xml", b"x"), ("A/a.xml/", b"")],
        ),
    )
    def test_duplicate_and_file_directory_conflicts(self, entries):
        with pytest.raises(ProjectAnnotationImportError):
            subject.inspect_zip(zipped(entries))

    @pytest.mark.parametrize("mode", (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR))
    def test_symlinks_and_special_files(self, mode):
        info = ZipInfo("Task/a.xml")
        info.create_system = 3
        info.external_attr = (mode | 0o600) << 16
        with pytest.raises(ProjectAnnotationImportError):
            subject.inspect_zip(zipped([(info, b"../secret")]))

    def test_empty_or_invalid_archive(self):
        for stream in (io.BytesIO(b"JSON is not ZIP"), zipped([]), zipped([("Task/", b"")])):
            with pytest.raises(ProjectAnnotationImportError):
                subject.inspect_zip(stream)

    def test_limits_apply_before_any_extraction(self):
        stream = zipped([("A/a.xml", b"a" * 50), ("B/b.xml", b"b")])
        with pytest.raises(ProjectAnnotationImportError):
            subject.inspect_zip(stream, subject.Budget(bytes_left=20))
        with pytest.raises(ProjectAnnotationImportError):
            subject.inspect_zip(stream, subject.Budget(entries_left=1))
        with (
            patch.object(subject, "MAX_UPLOAD_BYTES", 10),
            pytest.raises(ProjectAnnotationImportError),
        ):
            subject.inspect_zip(stream)

    def test_repacking_preserves_native_internal_paths_and_contents(self):
        entries = [
            ("Task/annotations/instances_default.json", b'{"images":[]}'),
            ("Task/images/default/frame_000000.png", b"image"),
            ("Other/a.xml", b"unrelated"),
        ]
        source = zipped(entries)
        members = subject.inspect_zip(source)
        with tempfile.TemporaryDirectory() as temp:
            target = subject.task_source(
                source, members, ("Task",), temp, "JSON, ZIP", subject.Budget()
            )
            with ZipFile(target) as result:
                assert set(result.namelist()) == {
                    "annotations/instances_default.json",
                    "images/default/frame_000000.png",
                }
                assert result.read("annotations/instances_default.json") == b'{"images":[]}'

    def test_single_native_xml_is_not_given_extra_directory(self):
        source = zipped([("Task/annotations.xml", b"<annotations/>")])
        members = subject.inspect_zip(source)
        with tempfile.TemporaryDirectory() as temp:
            target = subject.task_source(
                source, members, ("Task",), temp, "XML, ZIP", subject.Budget()
            )
            assert target.read_bytes() == b"<annotations/>"
            assert target.suffix == ".xml"

    def test_single_native_zip_is_checked_then_passed_unchanged(self):
        nested = zipped([("annotations/instances.json", b"{}")]).getvalue()
        source = zipped([("Task/export.zip", nested)])
        members = subject.inspect_zip(source)
        with tempfile.TemporaryDirectory() as temp:
            target = subject.task_source(source, members, ("Task",), temp, "ZIP", subject.Budget())
            assert target.read_bytes() == nested

    def test_nested_traversal_is_rejected_even_disguised_as_xml(self):
        nested = zipped([("../a.xml", b"evil")]).getvalue()
        for name in ("export.zip", "annotations.xml"):
            source = zipped([("Task/" + name, nested)])
            members = subject.inspect_zip(source)
            with (
                tempfile.TemporaryDirectory() as temp,
                pytest.raises(ProjectAnnotationImportError),
            ):
                subject.task_source(source, members, ("Task",), temp, "XML, ZIP", subject.Budget())

    def test_second_nested_zip_or_mixed_payload_is_rejected(self):
        inner = zipped([("a.xml", b"x")]).getvalue()
        for entries in (
            [("Task/export.zip", zipped([("again.zip", inner)]).getvalue())],
            [("Task/export.zip", inner), ("Task/a.xml", b"x")],
        ):
            source = zipped(entries)
            members = subject.inspect_zip(source)
            with (
                tempfile.TemporaryDirectory() as temp,
                pytest.raises(ProjectAnnotationImportError),
            ):
                subject.task_source(source, members, ("Task",), temp, "XML, ZIP", subject.Budget())

    def test_nested_expansion_uses_shared_budget(self):
        nested = zipped([("a.xml", b"x" * 20)]).getvalue()
        source = zipped([("Task/export.zip", nested)])
        members = subject.inspect_zip(source)
        with tempfile.TemporaryDirectory() as temp, pytest.raises(ProjectAnnotationImportError):
            subject.task_source(
                source, members, ("Task",), temp, "ZIP", subject.Budget(bytes_left=10)
            )

    def test_error_does_not_include_private_member_path(self):
        source = zipped([("PrivatePatient/../a.xml", b"x")])
        with pytest.raises(ProjectAnnotationImportError) as error:
            subject.inspect_zip(source)
        assert "PrivatePatient" not in str(error.value)
