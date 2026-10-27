# SPDX-License-Identifier: MIT
"""Pure ZIP, JSON, identity and Markdown logic for the project task-guide importer."""

import html
import io
import json
import math
import re
import stat
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_GUIDE_CHARS = 1_000_000
START = "<!-- cvat:study-report:start -->"
END = "<!-- cvat:study-report:end -->"
STUDY_UID = "StudyInstanceUID"
PATIENT_ID = "PatientID"
STUDY_DATE = "StudyDate"
IDENTITY_TAGS = (STUDY_UID, PATIENT_ID, STUDY_DATE)
_UID = re.compile(r"[0-9]+(?:\.[0-9]+)+\Z")


class GuideImportError(ValueError):
    pass


@dataclass(frozen=True)
class Report:
    row: int
    study_uid: str
    patient_id: str
    date: str
    markdown: str


@dataclass(frozen=True)
class NamedGuide:
    row: int
    task_name: str
    markdown: str


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
    if len(markdown) > MAX_GUIDE_CHARS:
        raise GuideImportError("Generated guide exceeds the size limit")
    return markdown


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GuideImportError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_number(value):
    raise GuideImportError("Nonfinite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        _invalid_number(value)
    return number


def _read_json(source):
    raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise GuideImportError("JSON exceeds the 32 MiB limit")
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_number,
            parse_float=_finite_float,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise GuideImportError(
            "Invalid UTF-8 JSON (including duplicate keys or nonfinite numbers)"
        ) from exc


def read_reports(source) -> tuple[list[Report], int]:
    data = _read_json(source)
    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list) or not 0 < len(records) <= MAX_RECORDS:
        raise GuideImportError("Expected a nonempty records array, at most 50000 records")
    reports = {}
    duplicates = 0
    for row, record in enumerate(records, 1):
        try:
            if not isinstance(record, dict):
                raise GuideImportError("Record must be an object")
            source, result = record.get("input"), record.get("result")
            if not isinstance(source, dict) or not isinstance(result, dict):
                raise GuideImportError("Record must contain input and result objects")
            uid = _identifier(source, ("study_instance_uid", "study_uid", "uid"))
            if not _UID.fullmatch(uid) or len(uid) > 64:
                raise GuideImportError("A full numeric StudyInstanceUID is required")
            report = Report(
                row,
                uid,
                _identifier(source, ("patient_id", "pid")),
                _identifier(source, ("study_date", "date", "dt"), date=True),
                render_markdown(source, result, input_text=record.get("input_text")),
            )
            previous = reports.get(uid)
            if previous:
                if (previous.patient_id, previous.date, previous.markdown) != (
                    report.patient_id,
                    report.date,
                    report.markdown,
                ):
                    raise GuideImportError(
                        f"Conflicting reports for the study in record {previous.row}"
                    )
                duplicates += 1
            else:
                reports[uid] = report
        except GuideImportError as exc:
            raise GuideImportError(f"Record {row}: {exc}") from exc
    return list(reports.values()), duplicates


def _named_guide(row: int, name, markdown) -> NamedGuide:
    if not isinstance(name, str) or not name.strip():
        raise GuideImportError(f"Record {row}: task name must be a nonempty string")
    if not isinstance(markdown, str):
        raise GuideImportError(f"Record {row}: Markdown must be a string")
    if len(markdown) > MAX_GUIDE_CHARS:
        raise GuideImportError(f"Record {row}: guide exceeds the size limit")
    if "\x00" in name or "\x00" in markdown:
        raise GuideImportError(f"Record {row}: NUL is not allowed")
    try:
        name.encode("utf-8")
        markdown.encode("utf-8")
    except UnicodeError as exc:
        raise GuideImportError(f"Record {row}: invalid Unicode") from exc
    # Names and Markdown are intentionally not stripped, escaped or reformatted.
    return NamedGuide(row, name, markdown)


def read_named_json(source) -> list[NamedGuide]:
    data = _read_json(source)
    if not isinstance(data, dict) or not 0 < len(data) <= MAX_RECORDS:
        raise GuideImportError(
            "Expected a nonempty task-name-to-Markdown object, at most 50000 entries"
        )
    return [_named_guide(row, name, text) for row, (name, text) in enumerate(data.items(), 1)]


def _read_zip_files(source, extension: str) -> list[tuple[str, bytes]]:
    raw = source.read(MAX_FILE_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_FILE_BYTES:
        raise GuideImportError("ZIP exceeds the 32 MiB limit or is not binary")
    files = []
    total = 0
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > MAX_RECORDS:
                raise GuideImportError("ZIP has more than 50000 entries")
            for info in archive.infolist():
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
                if total > MAX_FILE_BYTES:
                    raise GuideImportError("Uncompressed ZIP exceeds the 32 MiB limit")
                with archive.open(info) as member:
                    data = member.read(MAX_FILE_BYTES + 1)
                if len(data) != info.file_size or len(data) > MAX_FILE_BYTES:
                    raise GuideImportError("Invalid or oversized ZIP member")
                files.append((path.name, data))
    except GuideImportError:
        raise
    except (
        BadZipFile,
        EOFError,
        UnicodeError,
        OSError,
        RuntimeError,
        ValueError,
        NotImplementedError,
        zlib.error,
    ) as exc:
        raise GuideImportError("Invalid ZIP archive") from exc
    return files


def read_json_zip(source) -> bytes:
    files = _read_zip_files(source, ".json")
    if len(files) != 1:
        raise GuideImportError("ZIP must contain exactly one JSON file")
    return files[0][1]


def read_markdown_zip(source) -> list[NamedGuide]:
    guides, names = [], set()
    for filename, data in _read_zip_files(source, ".md"):
        name = filename[:-3]
        if name in names:
            raise GuideImportError("ZIP contains multiple Markdown files for the same task name")
        try:
            markdown = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise GuideImportError("Invalid UTF-8 Markdown") from exc
        guides.append(_named_guide(len(guides) + 1, name, markdown))
        names.add(name)
    if not guides:
        raise GuideImportError("ZIP contains no Markdown files")
    return guides


def plan_named_updates(
    guides: list[NamedGuide], tasks: list[TaskIdentity]
) -> tuple[dict[int, str], list[int]]:
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


def plan_updates(
    reports: list[Report], tasks: list[TaskIdentity]
) -> tuple[dict[int, str], list[int]]:
    """Apply one study report to all matching image/video tasks, never by short SOP suffix."""
    by_uid = {report.study_uid: report for report in reports}
    updates, matched = {}, set()
    for task in tasks:
        tags = {
            key: {value.strip() for value in task.tags.get(key, set()) if value.strip()}
            for key in IDENTITY_TAGS
        }
        uids = tags.get(STUDY_UID, set())
        if not uids.intersection(by_uid):
            continue
        if len(uids) != 1:
            raise GuideImportError(f"Task {task.id}: conflicting study identities")
        report = by_uid[next(iter(uids))]
        dates = {_date(value) for value in tags.get(STUDY_DATE, set())}
        patients = tags.get(PATIENT_ID, set())
        if len(dates) > 1 or (report.date and dates and report.date not in dates):
            raise GuideImportError(f"Task {task.id}: study date mismatch")
        if len(patients) > 1 or (
            report.patient_id and patients and report.patient_id not in patients
        ):
            raise GuideImportError(f"Task {task.id}: patient ID mismatch")
        updates[task.id] = report.markdown
        matched.add(report.study_uid)
    if not updates:
        raise GuideImportError("No matching tasks in this project; no guides were changed")
    return updates, [report.row for report in reports if report.study_uid not in matched]


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
    if len(merged) > MAX_GUIDE_CHARS:
        raise GuideImportError("Combined guide exceeds the size limit")
    try:
        merged.encode("utf-8")
    except UnicodeError as exc:
        raise GuideImportError("Guide contains invalid Unicode") from exc
    return merged
