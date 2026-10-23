# SPDX-License-Identifier: MIT
import unittest

from components.task_context.validation import MAX_FIELDS, validate_context


class ContextValidationTests(unittest.TestCase):
    def test_unicode_and_multiline_text_are_preserved(self):
        payload = {
            "fields": [{"name": " 撮影場所 ", "value": "施設A\n検査室2"}],
            "notes": "<script>text only</script>\n確認事項",
            "version": 0,
        }
        result = validate_context(payload, with_version=True)
        self.assertEqual(result["fields"][0]["name"], "撮影場所")
        self.assertEqual(result["fields"][0]["value"], payload["fields"][0]["value"])
        self.assertEqual(result["notes"], payload["notes"])

    def test_empty_document_is_valid(self):
        self.assertEqual(validate_context({"fields": [], "notes": ""}), {"fields": [], "notes": ""})

    def test_unknown_or_missing_properties_are_rejected(self):
        for payload in (None, [], {}, {"fields": []}, {"fields": [], "notes": "", "patient": "x"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_context(payload)

    def test_field_types_names_and_duplicates_are_rejected(self):
        invalid_fields = [
            {},
            None,
            ["x"],
            [{"name": "a", "value": 4}],
            [{"name": " ", "value": ""}],
            [{"name": "a", "value": "", "id": 1}],
            [{"name": 1, "value": ""}],
            [{"name": "a", "value": ""}, {"name": " a ", "value": "x"}],
        ]
        for fields in invalid_fields:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_context({"fields": fields, "notes": ""})

    def test_per_field_limits(self):
        invalid = [
            {"fields": [{"name": str(i), "value": ""} for i in range(MAX_FIELDS + 1)], "notes": ""},
            {"fields": [{"name": "x" * 129, "value": ""}], "notes": ""},
            {"fields": [{"name": "x", "value": "x" * 4097}], "notes": ""},
            {"fields": [], "notes": "x" * 16385},
        ]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:50]), self.assertRaises(ValueError):
                validate_context(payload)

    def test_utf8_document_limit_is_enforced(self):
        payload = {
            "fields": [{"name": str(i), "value": "医" * 4096} for i in range(6)],
            "notes": "",
        }
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            validate_context(payload)

    def test_version_must_be_an_integer_without_coercion(self):
        for version in (None, True, False, 1.0, "1", -1, 2**53):
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate_context({"fields": [], "notes": "", "version": version}, with_version=True)

    def test_invalid_unicode_and_null_are_rejected(self):
        for value in ("a\x00b", "\ud800"):
            for payload in (
                {"fields": [], "notes": value},
                {"fields": [{"name": value, "value": ""}], "notes": ""},
                {"fields": [{"name": "a", "value": value}], "notes": ""},
            ):
                with self.subTest(payload=repr(payload)), self.assertRaises(ValueError):
                    validate_context(payload)


if __name__ == "__main__":
    unittest.main()
