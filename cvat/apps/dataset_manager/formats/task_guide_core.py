# SPDX-License-Identifier: MIT
"""Pure JSON, identity and Markdown logic for the project task-guide importer."""

import html
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 50_000
MAX_GUIDE_CHARS = 1_000_000
START = "<!-- cvat:study-report:start -->"
END = "<!-- cvat:study-report:end -->"
STUDY_UID = "0020000D"
PATIENT_ID = "00100020"
STUDY_DATE = "00080020"
IDENTITY_TAGS = (STUDY_UID, PATIENT_ID, STUDY_DATE)
_UID = re.compile(r"[0-9]+(?:\.[0-9]+)+\Z")
_STUDY_NAME = re.compile(r"(?P<head>.+_.+)_(?P<date>[0-9]{8})_(?P<uid>[0-9]+(?:\.[0-9]+)+)\Z")


class GuideImportError(ValueError):
    pass


@dataclass(frozen=True)
class Report:
    row: int
    study_uid: str
    patient_id: str
    date: str
    markdown: str


@dataclass
class TaskIdentity:
    id: int
    name: str
    tags: dict[str, set[str]] = field(default_factory=dict)


def component(value: str) -> str:
    """Same filename component normalization as PR11; never abbreviate a UID."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value.strip()).rstrip(" .")


def _date(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        value = value.replace("-", "")
    if not re.fullmatch(r"[0-9]{8}", value):
        raise GuideImportError("Invalid study date")
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise GuideImportError("Invalid study date") from exc
    return value


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


def render_markdown(source: dict, result: dict) -> str:
    extracted = result.get("extracted")
    if extracted is None:
        extracted = {}
    if not isinstance(extracted, dict):
        raise GuideImportError("result.extracted must be an object or null")
    overview = [
        ("年齢", _text(source.get("parsed_patient_age"))),
        ("性別", _text(source.get("patient_sex"))),
        ("検査方法", _text(source.get("modality"))),
    ]
    exam = [
        ("検査部位", _text(extracted.get("part"))),
        ("検査履歴", _text(extracted.get("history"))),
        ("検査目的", _text(extracted.get("objective"))),
    ]
    findings = _first(result.get("findings_ja"), result.get("findings_jp"),
                      extracted.get("findings"), result.get("findings_en"), result.get("findings"))
    impression = _first(result.get("impression_ja"), result.get("impression_jp"),
                        extracted.get("impression"), result.get("impression_en"), result.get("impression"))
    if not any(value for _, value in overview + exam) and not findings and not impression:
        raise GuideImportError("No report content to import")

    def bullets(items):
        return [f"- {label}：" + _escape(value).replace("\n", "  \n  ")
                for label, value in items if value]

    sections = ["# 概要\n" + "\n".join(bullets(overview))]
    if any(value for _, value in exam):
        sections.append("## 検査概要\n" + "\n".join(bullets(exam)))
    for heading, text in (("所見", findings), ("診断", impression)):
        sections.append(f"## {heading}\n" + _escape(text or "記載なし").replace("\n", "  \n"))
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


def read_reports(source) -> tuple[list[Report], int]:
    raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise GuideImportError("JSON exceeds the 32 MiB limit")
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig")
        data = json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=_invalid_number, parse_float=_finite_float)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise GuideImportError("Invalid UTF-8 JSON (including duplicate keys or nonfinite numbers)") from exc
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
            uid = _identifier(source, ("study_instance_uid", "study_uid"))
            if not _UID.fullmatch(uid) or len(uid) > 64:
                raise GuideImportError("A full numeric StudyInstanceUID is required")
            report = Report(row, uid, _identifier(source, ("patient_id", "uid")),
                            _identifier(source, ("study_date", "date"), date=True),
                            render_markdown(source, result))
            previous = reports.get(uid)
            if previous:
                if (previous.patient_id, previous.date, previous.markdown) != (
                    report.patient_id, report.date, report.markdown
                ):
                    raise GuideImportError(f"Conflicting reports for the study in record {previous.row}")
                duplicates += 1
            else:
                reports[uid] = report
        except GuideImportError as exc:
            raise GuideImportError(f"Record {row}: {exc}") from exc
    return list(reports.values()), duplicates


def plan_updates(reports: list[Report], tasks: list[TaskIdentity]) -> tuple[dict[int, str], list[int]]:
    """Apply one study report to all matching image/video tasks, never by short SOP suffix."""
    by_uid = {report.study_uid: report for report in reports}
    updates, matched = {}, set()
    for task in tasks:
        name = _STUDY_NAME.fullmatch(task.name)
        tags = {key: {value.strip() for value in values if value.strip()}
                for key, values in task.tags.items()}
        uids = tags.get(STUDY_UID, set())
        candidates = uids | ({name["uid"]} if name else set())
        if not candidates.intersection(by_uid):
            continue
        if len(candidates) != 1:
            raise GuideImportError(f"Task {task.id}: conflicting study identities")
        report = by_uid[next(iter(candidates))]
        dates = {_date(value) for value in tags.get(STUDY_DATE, set())}
        if name:
            dates.add(_date(name["date"]))
        patients = tags.get(PATIENT_ID, set())
        if len(dates) > 1 or (report.date and dates and report.date not in dates):
            raise GuideImportError(f"Task {task.id}: study date mismatch")
        if len(patients) > 1 or (report.patient_id and patients and report.patient_id not in patients):
            raise GuideImportError(f"Task {task.id}: patient ID mismatch")
        # A full UID identifies a study. The other fields are consistency checks when present.
        if name and report.patient_id and not name["head"].endswith("_" + component(report.patient_id)):
            raise GuideImportError(f"Task {task.id}: patient ID in task name does not match")
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
        merged = existing[:start] + block + existing[end + len(END):]
    if len(merged) > MAX_GUIDE_CHARS:
        raise GuideImportError("Combined guide exceeds the size limit")
    try:
        merged.encode("utf-8")
    except UnicodeError as exc:
        raise GuideImportError("Guide contains invalid Unicode") from exc
    return merged
