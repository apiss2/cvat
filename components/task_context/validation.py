# SPDX-License-Identifier: MIT
"""The same bounded, plain-text contract is used by the API and backup import."""

import json

MAX_DOCUMENT_BYTES = 64 * 1024
MAX_FIELDS = 64
MAX_NAME_LENGTH = 128
MAX_VALUE_LENGTH = 4096
MAX_NOTES_LENGTH = 16384
MAX_VERSION = 2**53 - 1  # Versions must round-trip through a browser without rounding.


def _text(value, *, name, maximum):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    if len(value) > maximum:
        raise ValueError(f"{name} must contain at most {maximum} characters.")
    if "\x00" in value:
        raise ValueError(f"{name} contains an unsupported null character.")
    return value


def validate_context(payload, *, with_version=False):
    expected = {"fields", "notes", "version"} if with_version else {"fields", "notes"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError(f"Expected fields: {', '.join(sorted(expected))}.")

    fields = payload["fields"]
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise ValueError(f"fields must be a list of at most {MAX_FIELDS} items.")

    result = {
        "fields": [],
        "notes": _text(
            payload["notes"],
            name="notes",
            maximum=MAX_NOTES_LENGTH,
        ),
    }
    names = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) != {"name", "value"}:
            raise ValueError("Each field must contain only name and value.")
        name = _text(field["name"], name="Field name", maximum=MAX_NAME_LENGTH).strip()
        if not name:
            raise ValueError("Field names must not be empty.")
        if name in names:
            raise ValueError("Field names must be unique.")
        names.add(name)
        result["fields"].append(
            {
                "name": name,
                "value": _text(field["value"], name="Field value", maximum=MAX_VALUE_LENGTH),
            }
        )

    if with_version:
        version = payload["version"]
        if type(version) is not int or not 0 <= version <= MAX_VERSION:
            raise ValueError("version must be a non-negative safe integer.")
        result["version"] = version

    try:
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("Task details must contain valid Unicode text.") from exc
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError("Task details must fit within 64 KiB.")
    return result
