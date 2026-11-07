# SPDX-License-Identifier: MIT
"""Read task-guide JSON one record at a time without loading the document.

Only the current report or named guide is materialized. Other fields of a report
wrapper are validated and discarded incrementally, including nested arrays and
strings. Consumers must exhaust the iterator before committing any changes:
invalid trailing content or a duplicate wrapper key can follow valid records.
"""

import codecs
import json
import math
import re

_READ_SIZE = 64 * 1024
_SPACE = re.compile(r"[ \t\r\n]+")
# Coalesce complete escapes as well as literal text. Keeping one Python string
# per escape would multiply memory use for a single large escaped value.
_STRING_BODY = re.compile(r'(?:[^"\\\x00-\x1f]+|\\["\\/bfnrt]|\\u[0-9A-Fa-f]{4})+')
_NUMBER_TEXT = re.compile(r"[-+0-9.eE]+")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_HEX = frozenset("0123456789abcdefABCDEF")
_INVALID_JSON = "Invalid UTF-8 JSON (including duplicate keys or nonfinite numbers)"


class JSONStreamError(ValueError):
    """The input is not a complete, strict JSON task-guide document."""


class _Reader:
    def __init__(self, source):
        self.source = source
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.binary = None
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")()

    def peek(self):
        while self.position == len(self.buffer) and not self.eof:
            raw = self.source.read(_READ_SIZE)
            if not isinstance(raw, (bytes, str)):
                raise JSONStreamError("JSON source must contain bytes or text")
            binary = isinstance(raw, bytes)
            if self.binary is not None and self.binary != binary:
                raise JSONStreamError("JSON source changed between bytes and text")
            self.binary = binary
            self.eof = not raw
            self.buffer = self.decoder.decode(raw, final=self.eof) if binary else raw
            self.position = 0
        return self.buffer[self.position : self.position + 1]

    def take(self):
        character = self.peek()
        if not character:
            raise JSONStreamError("Incomplete JSON value")
        self.position += 1
        return character

    def space(self):
        while self.peek():
            match = _SPACE.match(self.buffer, self.position)
            if match is None:
                break
            self.position = match.end()

    def expect(self, character):
        self.space()
        if self.take() != character:
            raise JSONStreamError("Unexpected JSON separator")

    def string(self, *, keep=True):
        self.expect('"')
        parts = ['"'] if keep else None
        while self.peek():
            match = _STRING_BODY.match(self.buffer, self.position)
            if match is not None:
                if keep:
                    parts.append(match.group())
                self.position = match.end()
                continue
            character = self.take()
            if character == '"':
                if not keep:
                    return None
                parts.append('"')
                return json.loads("".join(parts))
            if character != "\\":
                raise JSONStreamError("Unescaped control character in JSON string")
            escape = self.take()
            if escape == "u":
                digits = "".join(self.take() for _ in range(4))
                if any(digit not in _HEX for digit in digits):
                    raise JSONStreamError("Invalid JSON Unicode escape")
                escape += digits
            elif escape not in '"\\/bfnrt':
                raise JSONStreamError("Invalid JSON string escape")
            if keep:
                parts.append("\\" + escape)
        raise JSONStreamError("Incomplete JSON string")

    def number(self):
        parts = []
        while self.peek():
            match = _NUMBER_TEXT.match(self.buffer, self.position)
            if match is None:
                break
            parts.append(match.group())
            self.position = match.end()
        token = "".join(parts)
        if not _NUMBER.fullmatch(token):
            raise JSONStreamError("Invalid JSON number")
        if any(character in token for character in ".eE"):
            value = float(token)
            if not math.isfinite(value):
                raise JSONStreamError("Nonfinite JSON number")
            return value
        return int(token)

    def object_keys(self):
        self.expect("{")
        self.space()
        if self.peek() == "}":
            self.take()
            return
        seen = set()
        while True:
            key = self.string()
            if key in seen:
                raise JSONStreamError("Duplicate JSON object key")
            seen.add(key)
            self.expect(":")
            yield key
            self.space()
            separator = self.take()
            if separator == "}":
                return
            if separator != ",":
                raise JSONStreamError("Expected a comma or the end of a JSON object")

    def array_values(self, *, keep=True):
        self.expect("[")
        self.space()
        if self.peek() == "]":
            self.take()
            return
        while True:
            yield self.value(keep=keep)
            self.space()
            separator = self.take()
            if separator == "]":
                return
            if separator != ",":
                raise JSONStreamError("Expected a comma or the end of a JSON array")

    def value(self, *, keep=True):
        self.space()
        character = self.peek()
        if character == '"':
            return self.string(keep=keep)
        if character == "{":
            result = {} if keep else None
            for key in self.object_keys():
                value = self.value(keep=keep)
                if keep:
                    result[key] = value
            return result
        if character == "[":
            values = self.array_values(keep=keep)
            if keep:
                return list(values)
            for _ in values:
                pass
            return None
        if character and character in "-0123456789":
            return self.number()
        for literal, value in (("true", True), ("false", False), ("null", None)):
            if character == literal[0]:
                if "".join(self.take() for _ in literal) != literal:
                    raise JSONStreamError("Invalid JSON literal")
                return value
        raise JSONStreamError("Invalid JSON value")

    def finish(self):
        self.space()
        if self.peek():
            raise JSONStreamError("Unexpected data after the JSON document")


def read_json(source):
    """Read one complete strict JSON value for consumers that need the whole value."""
    try:
        reader = _Reader(source)
        value = reader.value()
        reader.finish()
        return value
    except JSONStreamError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise JSONStreamError(_INVALID_JSON) from exc


def _records(reader):
    reader.space()
    if reader.peek() != "[":
        raise JSONStreamError("Expected a nonempty records array")
    count = 0
    for count, value in enumerate(reader.array_values(), 1):
        yield value
        del value
    if not count:
        raise JSONStreamError("Expected a nonempty records array")


def iter_report_records(source):
    """Yield the records from an array or a ``{\"records\": [...]}`` wrapper."""
    try:
        reader = _Reader(source)
        reader.space()
        if reader.peek() == "[":
            yield from _records(reader)
        elif reader.peek() == "{":
            found = False
            for key in reader.object_keys():
                if key == "records":
                    found = True
                    yield from _records(reader)
                else:
                    reader.value(keep=False)
            if not found:
                raise JSONStreamError("Expected a nonempty records array")
        else:
            raise JSONStreamError("Expected a nonempty records array")
        reader.finish()
    except JSONStreamError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise JSONStreamError(_INVALID_JSON) from exc


def iter_named_entries(source):
    """Yield task names and their values from a top-level JSON object in order."""
    try:
        reader = _Reader(source)
        reader.space()
        if reader.peek() != "{":
            raise JSONStreamError("Expected a nonempty task-name-to-Markdown object")
        count = 0
        for count, key in enumerate(reader.object_keys(), 1):
            yield key, reader.value()
        if not count:
            raise JSONStreamError("Expected a nonempty task-name-to-Markdown object")
        reader.finish()
    except JSONStreamError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise JSONStreamError(_INVALID_JSON) from exc
