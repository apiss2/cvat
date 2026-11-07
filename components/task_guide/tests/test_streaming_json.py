# SPDX-License-Identifier: MIT
"""Exercise strict incremental JSON parsing without CVAT or external services."""

import io
import itertools
import json
import tracemalloc

import pytest

from components.task_guide.streaming_json import (
    JSONStreamError,
    iter_named_entries,
    iter_report_records,
    read_json,
)


class ShortReads(io.BytesIO):
    def __init__(self, content, width):
        super().__init__(content)
        self.width = width

    def read(self, size=-1):
        assert 0 < size <= 64 * 1024, "The JSON parser must use bounded reads"
        return super().read(min(size, self.width))


class GeneratedSource:
    """Generate a large document while retaining at most one source segment."""

    def __init__(self, segments):
        self.segments = iter(segments)
        self.pending = b""
        self.bytes_read = 0

    def read(self, size=-1):
        assert 0 < size <= 64 * 1024, "The JSON parser must use bounded reads"
        chunks = []
        remaining = size
        while remaining:
            if not self.pending:
                self.pending = next(self.segments, b"")
                if not self.pending:
                    break
            chunks.append(self.pending[:remaining])
            self.pending = self.pending[len(chunks[-1]) :]
            remaining -= len(chunks[-1])
        result = b"".join(chunks)
        self.bytes_read += len(result)
        return result


@pytest.mark.parametrize("width", [1, 2, 3, 7, 64 * 1024])
def test_complete_json_values_cross_read_boundaries(width, subtests):
    record = {
        'quote"slash\\tab\t': [
            None,
            True,
            False,
            0,
            -17,
            1.25,
            -0.0625,
            3.5e37,
            '日本語 é 😀\r\n\t\b\f/\\"',
            {"nested": {"array": [[], {}, ""]}},
        ]
    }
    for escaped in (False, True):
        for wrapped in (False, True):
            with subtests.test(escaped=escaped, wrapped=wrapped):
                document = (
                    {"before": [{"ignored": [1, False, None]}], "records": [record], "after": {}}
                    if wrapped
                    else [record]
                )
                raw = b"\xef\xbb\xbf" + json.dumps(document, ensure_ascii=escaped).encode("utf-8")
                source = ShortReads(raw, width)
                assert list(iter_report_records(source)) == [record]
                assert source.tell() == len(raw)


@pytest.mark.parametrize("width", [1, 2, 7, 64 * 1024])
def test_numbers_split_between_digits_and_exponents(width):
    raw = b"[0, -0, 12345678901234567890, 0.0, -1.25e+8, 3E-15, 1.7976931348623157e308]"
    assert list(iter_report_records(ShortReads(raw, width))) == json.loads(raw)


def test_named_entries_preserve_order_names_and_markdown():
    text = '{" task 名 ":"# Header\\r\\n\\n**bold** 😀", "empty":"", "Task":"other"}'
    expected = list(json.loads(text).items())
    assert list(iter_named_entries(ShortReads(text.encode(), 1))) == expected
    assert list(iter_named_entries(io.StringIO(text))) == expected


def test_report_iterator_yields_before_reading_entire_input():
    raw = b'[1, "' + b"x" * (128 * 1024) + b'"]'
    source = ShortReads(raw, 7)
    records = iter_report_records(source)
    assert next(records) == 1
    assert source.tell() < len(raw)
    assert next(records) == "x" * (128 * 1024)
    with pytest.raises(StopIteration):
        next(records)
    assert source.tell() == len(raw)


@pytest.mark.parametrize("width", [1, 3, 64 * 1024])
def test_rejects_malformed_json_with_arbitrary_read_boundaries(width, subtests):
    malformed = [
        b"",
        b" ",
        b"[]",
        b"{}",
        b"null",
        b'"text"',
        b'{"records": null}',
        b'{"records": {}}',
        b'{"records": []}',
        b"[",
        b"[1",
        b"[1,]",
        b"[,1]",
        b"[1 2]",
        b"[truefalse]",
        b"[nul]",
        b"[False]",
        b"[+1]",
        b"[01]",
        b"[--1]",
        b"[1.]",
        b"[.1]",
        b"[1e]",
        b"[1e+]",
        b"[1e+-2]",
        b"[1e2.5]",
        b"[NaN]",
        b"[Infinity]",
        b"[-Infinity]",
        b"[1e999]",
        b"[-1e999]",
        b'["unclosed]',
        b'["\\x"]',
        b'["\\u123"]',
        b'["\\u12xz"]',
        b'["line\nbreak"]',
        b'["nul\x00byte"]',
        b'["bad\xffbyte"]',
        b'["incomplete\xe3\x81',
        b'[{"x" 1}]',
        b'[{"x":}]',
        b'[{"x":1,}]',
        b"[{1: 2}]",
        b'[{"x":1,"x":2}]',
        b'[{"x":1,"\\u0078":2}]',
        b"[1] trailing",
        b"[1][2]",
        b"[1]\xef\xbb\xbf",
        b'{"records":[1],"records":[2]}',
        b'{"records":[1],"rec\\u006frds":[2]}',
        b'{"records":[1] "extra":0}',
        b'{"records":[1],}',
    ]
    for raw in malformed:
        with subtests.test(raw=raw):
            with pytest.raises(JSONStreamError):
                list(iter_report_records(ShortReads(raw, width)))


def test_ignored_wrapper_values_are_fully_validated(subtests):
    invalid_values = [
        b'{"nested":{"key":1,"key":2}}',
        b'[1, {"nested":1e999}]',
        b'"bad\\xescape"',
        b'"unescaped\x00control"',
        b"[true, false,]",
        b'{"incomplete":',
    ]
    for value in invalid_values:
        for before in (True, False):
            with subtests.test(value=value, before=before):
                if before:
                    raw = b'{"metadata":' + value + b',"records":[1]}'
                else:
                    raw = b'{"records":[1],"metadata":' + value + b"}"
                records = iter_report_records(ShortReads(raw, 1))
                if not before:
                    assert next(records) == 1
                with pytest.raises(JSONStreamError):
                    list(records)


def test_named_entries_validate_nested_values_and_trailing_content(subtests):
    for raw in [
        b"{}",
        b"[]",
        b'{"task":"text", "task":"other"}',
        b'{"task":"text", "t\\u0061sk":"other"}',
        b'{"task":"text", "other":{"key":1,"key":2}}',
        b'{"task":"text", "other":1e999}',
        b'{"task":"text"} false',
        b'{"task":"text"',
    ]:
        with subtests.test(raw=raw):
            with pytest.raises(JSONStreamError):
                list(iter_named_entries(ShortReads(raw, 1)))


def test_deep_input_reports_a_validation_error():
    raw = b'{"ignored":' + b"[" * 2000 + b"0" + b"]" * 2000 + b',"records":[1]}'
    with pytest.raises(JSONStreamError):
        list(iter_report_records(ShortReads(raw, 64 * 1024)))


def test_large_nested_wrapper_metadata_is_discarded_incrementally():
    source = GeneratedSource(
        itertools.chain(
            [b'{"metadata":{"nested":[{"ignored":"'],
            itertools.repeat(b"x" * (32 * 1024), 1024),
            [b'"}]},"records":[{"value":"report"}],"trailer":['],
            itertools.repeat(b'{"ignored":false},', 1000),
            [b"null]}"],
        )
    )
    tracemalloc.start()
    try:
        assert list(iter_report_records(source)) == [{"value": "report"}]
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert source.bytes_read > 32 * 1024 * 1024
    assert peak < 2 * 1024 * 1024


def test_large_records_array_retains_only_the_current_record():
    record = b'{"text":"' + b"x" * (64 * 1024) + b'"}'
    source = GeneratedSource(
        itertools.chain([b"["], itertools.repeat(record + b",", 511), [record, b"]"])
    )
    tracemalloc.start()
    try:
        count = 0
        for value in iter_report_records(source):
            assert len(value["text"]) == 64 * 1024
            count += 1
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert count == 512
    assert source.bytes_read > 32 * 1024 * 1024
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize(
    "escaped,decoded",
    [(b"\\n", "\n"), (b"\\u3042", "あ"), (b'a\\n\\"\\\\\\u3042', 'a\n"\\あ')],
)
def test_large_escaped_strings_do_not_retain_one_object_per_escape(escaped, decoded):
    repetitions = 32 * 1024 // len(escaped)
    source = GeneratedSource(
        itertools.chain(
            [b'{"task":"'],
            itertools.repeat(escaped * repetitions, 64),
            [b'"}'],
        )
    )
    tracemalloc.start()
    try:
        entries = list(iter_named_entries(source))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert entries == [("task", decoded * repetitions * 64)]
    assert peak < 10 * 1024 * 1024


@pytest.mark.parametrize("width", [1, 64 * 1024])
def test_read_json_validates_a_complete_value_without_guide_shape_rules(width, subtests):
    for value in (None, 1.5, "日本語", [], {}, {"records": []}):
        with subtests.test(value=value):
            source = ShortReads(json.dumps(value).encode(), width)
            assert read_json(source) == value
    for raw in (
        b'{"records":[]} false',
        b'{"records":[],"records":[]}',
        b'{"records":[1e999]}',
        b'{"records":["\xff"]}',
    ):
        with subtests.test(raw=raw):
            with pytest.raises(JSONStreamError):
                read_json(ShortReads(raw, width))
