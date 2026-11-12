# SPDX-License-Identifier: MIT
"""Pure ZIP, JSON, identity and Markdown logic for the project task-guide importer."""

from __future__ import annotations

import codecs
import html
import math
import re
import stat
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import SEEK_END
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Iterator
from zipfile import BadZipFile, ZipFile

from components.task_guide.streaming_json import (
    JSONStreamError,
    iter_named_entries,
    iter_report_records,
)

if TYPE_CHECKING:
    from components.task_guide.staging import TextReference

_READ_SIZE = 64 * 1024
_SURROGATE = re.compile(r"[\ud800-\udfff]")
START = "<!-- cvat:study-report:start -->"
END = "<!-- cvat:study-report:end -->"
PATIENT_ID = "PatientID"
STUDY_DATE = "StudyDate"
MODALITY = "Modality"
STUDY_UID = "StudyInstanceUID"
IDENTITY_TAGS = (PATIENT_ID, STUDY_DATE, MODALITY, STUDY_UID)
# Full StudyInstanceUID syntax, shared with the project annotation importer.
_UID = re.compile(r"[0-9]+(?:\.[0-9]+)+\Z")


class GuideImportError(ValueError):
    pass


@dataclass(frozen=True)
class Report:
    row: int
    patient_id: str
    date: str
    modality: str
    study_uid: str
    markdown: str | TextReference

    @property
    def key(self) -> tuple[str, str]:
        return self.patient_id, self.date

    @property
    def dedup_key(self) -> tuple[str, str, str, str]:
        return self.patient_id, self.date, self.modality, self.study_uid


@dataclass(frozen=True)
class InvalidRecord:
    """A report record skipped by validation. It holds no patient data."""

    row: int
    reason: str


@dataclass
class ReportPlan:
    updates: dict[int, str | TextReference]
    unmatched: list[int]
    skipped_tasks: list[int]


@dataclass(frozen=True)
class NamedGuide:
    row: int
    task_name: str
    markdown: str | TextReference


@dataclass
class TaskIdentity:
    id: int
    name: str
    tags: dict[str, set[str]] = field(default_factory=dict)


def _date(value: str) -> str:
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]{8}", value):
            parsed = datetime.strptime(value, "%Y%m%d")
        elif match := re.fullmatch(r"([0-9]{4})([-/.])([0-9]{1,2})\2([0-9]{1,2})([ T].+)?", value):
            year, _, month, day, time = match.groups()
            parsed = datetime.fromisoformat(f"{year}-{int(month):02d}-{int(day):02d}{time or ''}")
        elif match := re.fullmatch(
            r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*)?"
            r"[0-9]{1,2}\s+[A-Za-z]{3}\s+([0-9]{4})\s+"
            r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?\s+"
            r"(?:UT|UTC|GMT|[ECMP][SD]T|[+-][0-9]{4})",
            value,
            flags=re.IGNORECASE,
        ):
            # email.utils expands years below 100; keep the explicit four-digit year.
            parsed = parsedate_to_datetime(value).replace(year=int(match[1]))
        else:
            raise ValueError("Unsupported date format")
    except (ValueError, TypeError, OverflowError) as exc:
        raise GuideImportError("Invalid study date") from exc
    # StudyDate is a calendar date: preserve the written date, without a timezone shift.
    return f"{parsed.year:04d}{parsed.month:02d}{parsed.day:02d}"


def _identifier(data: dict, keys: tuple[str, ...], *, date: bool = False) -> str:
    values = []
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise GuideImportError(f"{key} must be a string")
        value = value.strip()
        if value:
            values.append(_date(value) if date else value)
    if len(set(values)) > 1:
        raise GuideImportError(f"Conflicting aliases for {keys[0]}")
    return values[0] if values else ""


def _text(value, depth: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if "\x00" in value:
            raise GuideImportError("NUL is not allowed in report text")
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise GuideImportError("Nonfinite report number")
        return str(value)
    if isinstance(value, list) and depth < 4:
        return "\n".join(text for item in value if (text := _text(item, depth + 1)))
    raise GuideImportError("Report fields must be text, numbers, null, or lists of these")


def _escape(text: str) -> str:
    # Input is data, not Markdown. Do not render links, HTML or remote images.
    text = html.escape(text, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", text)


def _first(*values) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def render_markdown(source: dict, result: dict, *, input_text=None) -> str:
    extracted = result.get("extracted")
    if extracted is None:
        extracted = {}
    if not isinstance(extracted, dict):
        raise GuideImportError("result.extracted must be an object or null")
    overview = [
        ("年齢", _first(source.get("parsed_patient_age"), source.get("age"))),
        ("性別", _first(source.get("patient_sex"), source.get("sex"))),
        ("検査方法", _first(source.get("modality"), source.get("mod"))),
    ]
    exam = [
        ("検査部位", _text(extracted.get("part"))),
        ("既往歴", _text(extracted.get("history"))),
        ("検査目的", _text(extracted.get("objective"))),
    ]
    findings = _first(
        result.get("findings_ja"),
        result.get("findings_jp"),
        extracted.get("findings"),
        result.get("findings_en"),
        result.get("findings"),
    )
    impression = _first(
        result.get("impression_ja"),
        result.get("impression_jp"),
        extracted.get("impression"),
        result.get("impression_en"),
        result.get("impression"),
    )
    if input_text is not None and not isinstance(input_text, str):
        raise GuideImportError("input_text must be a string or null")
    reference = _text(input_text)
    if (
        not any(value for _, value in overview + exam)
        and not findings
        and not impression
        and not reference
    ):
        raise GuideImportError("No report content to import")

    def bullets(items):
        return [
            f"- {label}：" + _escape(value).replace("\n", "  \n  ")
            for label, value in items
            if value
        ]

    sections = ["# 概要\n" + "\n".join(bullets(overview))]
    if any(value for _, value in exam):
        sections.append("## 検査概要\n" + "\n".join(bullets(exam)))
    for heading, text in (("所見", findings), ("診断", impression)):
        sections.append(f"## {heading}\n" + _escape(text or "記載なし").replace("\n", "  \n"))
    if reference:
        sections.append("## 参考生データ\n" + _escape(reference).replace("\n", "  \n"))
    markdown = "\n\n".join(sections).rstrip() + "\n"
    return markdown


def _modality(value) -> str:
    # DICOM Modality is a code string; compare codes without case or padding.
    return _text(value).upper()


def _valid_uid(value: str) -> bool:
    return len(value) <= 64 and _UID.fullmatch(value) is not None


def _report_uid(data: dict) -> str:
    try:
        uid = _identifier(data, ("study_instance_uid", "study_uid", "uid"))
    except GuideImportError:
        return ""
    return uid if _valid_uid(uid) else ""


def _report(row: int, record) -> Report:
    if not isinstance(record, dict):
        raise GuideImportError("Record must be an object")
    data, result = record.get("input"), record.get("result")
    if not isinstance(data, dict) or not isinstance(result, dict):
        raise GuideImportError("Record must contain input and result objects")
    patient_id = _identifier(data, ("patient_id", "pid"))
    if not patient_id:
        raise GuideImportError("PatientID is required")
    date = _identifier(data, ("study_date", "date", "dt"), date=True)
    if not date:
        raise GuideImportError("StudyDate is required")
    markdown = render_markdown(data, result, input_text=record.get("input_text"))
    if _SURROGATE.search(markdown):
        raise GuideImportError("Guide contains invalid Unicode")
    # Check the same value that the overview shows: the first nonempty alias.
    modality = _modality(_first(data.get("modality"), data.get("mod")))
    return Report(row, patient_id, date, modality, _report_uid(data), markdown)


def iter_reports(source) -> Iterator[Report | InvalidRecord]:
    """Yield reports and skipped records; callers must exhaust the iterator before writing.

    A malformed record is skipped so that the other records can still be matched.
    A broken JSON or ZIP stream still rejects the complete input.
    """
    try:
        for row, record in enumerate(iter_report_records(source), 1):
            try:
                yield _report(row, record)
            except GuideImportError as exc:
                yield InvalidRecord(row, str(exc))
            del record
    except JSONStreamError as exc:
        raise GuideImportError(str(exc)) from exc


def read_reports(source) -> tuple[list[Report], int, list[InvalidRecord]]:
    """Collect and deduplicate reports for callers that need an in-memory result."""
    unique = {}
    duplicates = 0
    invalid = []
    for report in iter_reports(source):
        if isinstance(report, InvalidRecord):
            invalid.append(report)
            continue
        key = (*report.dedup_key, report.markdown)
        if key in unique:
            duplicates += 1
        else:
            unique[key] = report
    return list(unique.values()), duplicates, invalid


def _named_guide(row: int, name, markdown) -> NamedGuide:
    if not isinstance(name, str) or not name.strip():
        raise GuideImportError(f"Record {row}: task name must be a nonempty string")
    if not isinstance(markdown, str):
        raise GuideImportError(f"Record {row}: Markdown must be a string")
    if "\x00" in name or "\x00" in markdown:
        raise GuideImportError(f"Record {row}: NUL is not allowed")
    if _SURROGATE.search(name) or _SURROGATE.search(markdown):
        raise GuideImportError(f"Record {row}: invalid Unicode")
    # Names and Markdown are intentionally not stripped, escaped or reformatted.
    return NamedGuide(row, name, markdown)


def iter_named_json(source) -> Iterator[NamedGuide]:
    """Yield exact task names and Markdown, validating the entire JSON on exhaustion."""
    try:
        for row, (name, text) in enumerate(iter_named_entries(source), 1):
            yield _named_guide(row, name, text)
            del text
    except JSONStreamError as exc:
        raise GuideImportError(str(exc)) from exc


def read_named_json(source) -> list[NamedGuide]:
    return list(iter_named_json(source))


@contextmanager
def _open_zip(
    source, extension: str, *, max_bytes: int | None = None, max_members: int | None = None
):
    """Open the seekable upload directly and validate all member metadata."""
    try:
        if max_bytes is not None:
            position = source.tell()
            source.seek(0, SEEK_END)
            size = source.tell()
            source.seek(position)
            if size > max_bytes:
                raise GuideImportError("ZIP exceeds the size limit")
        with ZipFile(source) as archive:
            entries = archive.infolist()
            if max_members is not None and len(entries) > max_members:
                raise GuideImportError("ZIP exceeds the entry limit")
            files = []
            total = 0
            for info in entries:
                path = PurePosixPath(info.filename)
                # Read members directly; never extract archive paths to the filesystem.
                if (
                    not info.filename
                    or "\\" in info.filename
                    or "\x00" in info.orig_filename
                    or path.is_absolute()
                    or re.match(r"^[A-Za-z]:", info.filename)
                    or any(part in ("", ".", "..") for part in info.filename.rstrip("/").split("/"))
                ):
                    raise GuideImportError("ZIP contains an unsafe member path")
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise GuideImportError("ZIP may contain only regular files and directories")
                if stat.S_ISDIR(mode) and not info.is_dir():
                    raise GuideImportError("ZIP directory has an invalid member name")
                if info.flag_bits & 1:
                    raise GuideImportError("Encrypted ZIP members are not supported")
                if info.is_dir():
                    continue
                if path.suffix.lower() != extension:
                    raise GuideImportError(f"ZIP files must have the {extension} extension")
                total += info.file_size
                if max_bytes is not None and total > max_bytes:
                    raise GuideImportError("Uncompressed ZIP exceeds the size limit")
                files.append((path.name, info))
            yield archive, files
    except GuideImportError:
        raise
    except (
        BadZipFile,
        EOFError,
        UnicodeError,
        RuntimeError,
        ValueError,
        TypeError,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise GuideImportError("Invalid ZIP archive") from exc


@contextmanager
def _open_zip_member(archive, info):
    with archive.open(info) as member:
        yield member
        # Reaching EOF checks the CRC even when a caller has read only a prefix.
        while member.read(_READ_SIZE):
            pass
        if member.tell() != info.file_size:
            raise GuideImportError("Invalid or truncated ZIP member")


@contextmanager
def open_json_zip(source, *, max_bytes: int | None = None, max_members: int | None = None):
    """Yield the sole JSON member, optionally bounding archive and expanded sizes.

    Task guides impose no format-specific limits. Other importers can supply
    their own byte and member limits while sharing the same ZIP validation.
    """
    with _open_zip(source, ".json", max_bytes=max_bytes, max_members=max_members) as (
        archive,
        files,
    ):
        if len(files) != 1:
            raise GuideImportError("ZIP must contain exactly one JSON file")
        with _open_zip_member(archive, files[0][1]) as member:
            yield member


def iter_markdown_zip(source) -> Iterator[NamedGuide]:
    """Decode one Markdown member at a time; exhaust before saving any guide."""
    with _open_zip(source, ".md") as (archive, files):
        if not files:
            raise GuideImportError("ZIP contains no Markdown files")
        names = set()
        for row, (filename, info) in enumerate(files, 1):
            name = filename[:-3]
            if name in names:
                raise GuideImportError(
                    "ZIP contains multiple Markdown files for the same task name"
                )
            names.add(name)
            try:
                with _open_zip_member(archive, info) as member:
                    decoder = codecs.getincrementaldecoder("utf-8-sig")()
                    chunks = []
                    while data := member.read(_READ_SIZE):
                        chunks.append(decoder.decode(data))
                    chunks.append(decoder.decode(b"", final=True))
                markdown = "".join(chunks)
                del chunks
            except UnicodeError as exc:
                raise GuideImportError("Invalid UTF-8 Markdown") from exc
            yield _named_guide(row, name, markdown)
            del markdown


def read_markdown_zip(source) -> list[NamedGuide]:
    return list(iter_markdown_zip(source))


def plan_named_updates(
    guides: list[NamedGuide], tasks: list[TaskIdentity]
) -> tuple[dict[int, str | TextReference], list[int]]:
    by_name = {}
    for task in tasks:
        by_name.setdefault(task.name, []).append(task.id)
    updates, unmatched = {}, []
    for guide in guides:
        matches = by_name.get(guide.task_name, [])
        if len(matches) > 1:
            raise GuideImportError(f"Record {guide.row}: multiple tasks have the same name")
        if matches:
            updates[matches[0]] = guide.markdown
        else:
            unmatched.append(guide.row)
    if not updates:
        raise GuideImportError("No matching tasks in this project; no guides were changed")
    return updates, unmatched


def _task_dates(values: set[str]) -> set[str] | None:
    try:
        return {_date(value) for value in values}
    except GuideImportError:
        return None


@dataclass(frozen=True)
class _ReportTarget:
    id: int
    modalities: set[str]
    study_uids: set[str]


def _index_report_targets(tasks, report_keys):
    patients_with_reports = {patient for patient, _ in report_keys}
    by_key = {}
    considered = set()
    for task in tasks:
        tags = {
            key: {value.strip() for value in task.tags.get(key, set()) if value.strip()}
            for key in IDENTITY_TAGS
        }
        patients = tags[PATIENT_ID]
        dates = _task_dates(tags[STUDY_DATE])
        if dates is None:
            # An unreadable StudyDate cannot be checked against this patient's reports.
            if patients & patients_with_reports:
                considered.add(task.id)
            continue
        keys = report_keys.intersection((patient, date) for patient in patients for date in dates)
        if not keys:
            continue
        if len(patients) != 1 or len(dates) != 1:
            considered.add(task.id)
            continue
        modalities = {_modality(value) for value in tags[MODALITY]}
        by_key.setdefault(keys.pop(), []).append(
            _ReportTarget(task.id, modalities, tags[STUDY_UID])
        )
    return by_key, considered


def _choose_report_targets(report, tasks):
    candidates = [
        task
        for task in tasks
        if not report.modality or not task.modalities or report.modality in task.modalities
    ]
    exact_uid = {report.study_uid} if report.study_uid else set()
    exact = [task.id for task in candidates if exact_uid and task.study_uids == exact_uid]
    if exact:
        return exact, True
    if len(candidates) == 1 and len(candidates[0].study_uids) <= 1:
        return [candidates[0].id], False
    if candidates:
        uids = candidates[0].study_uids
        if (
            len(uids) == 1
            and _valid_uid(next(iter(uids)))
            and all(task.study_uids == uids for task in candidates)
        ):
            return [task.id for task in candidates], False
    return [], False


def plan_updates(reports: list[Report], tasks: list[TaskIdentity]) -> ReportPlan:
    """Use UID matches to disambiguate compatible reports and tasks."""
    by_key, considered = _index_report_targets(tasks, {report.key for report in reports})
    exact_reports, fallback_reports = {}, {}
    for report in reports:
        tasks_for_key = by_key.get(report.key, ())
        targets, exact = _choose_report_targets(report, tasks_for_key)
        considered.update(
            task.id for task in tasks_for_key if not exact or report.study_uid in task.study_uids
        )
        selected = exact_reports if exact else fallback_reports
        for task_id in targets:
            selected[task_id] = None if task_id in selected else report

    # An exact match takes priority, including a conflict between exact reports.
    chosen = {
        task_id: report
        for task_id, report in (fallback_reports | exact_reports).items()
        if report is not None
    }
    updates = {task_id: report.markdown for task_id, report in chosen.items()}
    if not updates:
        raise GuideImportError("No matching tasks in this project; no guides were changed")
    matched = {report.row for report in chosen.values()}
    unmatched = [report.row for report in reports if report.row not in matched]
    return ReportPlan(updates, unmatched, sorted(considered - updates.keys()))


def merge_markdown(existing: str, generated: str) -> str:
    """Preserve manual content and replace only our single delimited report block."""
    block = f"{START}\n{generated.rstrip()}\n{END}"
    if START not in existing and END not in existing:
        merged = existing + ("\n\n" if existing else "") + block + "\n"
    else:
        if existing.count(START) != 1 or existing.count(END) != 1:
            raise GuideImportError("Guide has invalid or multiple generated report markers")
        start, end = existing.index(START), existing.index(END)
        if start >= end:
            raise GuideImportError("Guide report markers are out of order")
        merged = existing[:start] + block + existing[end + len(END) :]
    if _SURROGATE.search(merged):
        raise GuideImportError("Guide contains invalid Unicode")
    return merged
