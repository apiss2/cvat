# SPDX-License-Identifier: MIT
"""Pure matching tests; the fixtures use CVAT's annotation-filter field names."""
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest

DIRECTORY = Path(__file__).resolve().parents[2] / "cvat/apps/dataset_manager/filtered_export"
PACKAGE = "filtered_export_tested"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(DIRECTORY)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.selection", DIRECTORY / "selection.py")
selection = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = selection
spec.loader.exec_module(selection)
AnnotationFilter = selection.AnnotationFilter

LABELS = [
    {"id": 1, "name": "ready", "type": "tag", "sublabels": [], "attributes": [
        {"id": 10, "name": "quality", "input_type": "number", "default_value": "2", "values": []},
        {"id": 11, "name": "checked", "input_type": "checkbox", "default_value": "false", "values": []},
        {"id": 12, "name": "note", "input_type": "text", "default_value": "", "values": []},
    ]},
    {"id": 2, "name": "horse", "type": "any", "attributes": [], "sublabels": []},
    {"id": 3, "name": "person", "type": "skeleton", "attributes": [], "sublabels": [
        {"id": 4, "name": "nose", "type": "points", "attributes": [], "sublabels": []},
    ]},
]
TAGS = [{"id": 100, "frame": 0, "label_id": 1, "source": "manual", "attributes": []},
        {"id": 101, "frame": 2, "label_id": 1, "source": "auto", "attributes": [
            {"spec_id": 10, "value": "9"}, {"spec_id": 11, "value": "true"},
            {"spec_id": 12, "value": "good image"},
        ]}]
SHAPES = [{"id": 200, "frame": 0, "label_id": 2, "type": "mask", "points": [5, 5, 14, 24],
           "occluded": False, "source": "manual", "attributes": []},
          {"id": 201, "frame": 1, "label_id": 2, "type": "rectangle", "points": [0, 0, 20, 10],
           "rotation": 30, "occluded": True, "source": "auto", "attributes": []},
          {"id": 202, "track_id": 300, "frame": 2, "label_id": 2, "type": "rectangle",
           "points": [0, 0, 30, 10], "occluded": False, "source": "manual", "attributes": []},
          {"id": 203, "track_id": 300, "frame": 4, "label_id": 2, "type": "rectangle",
           "points": [0, 0, 50, 10], "outside": True, "attributes": []}]


def rule(field, value, op="=="):
    return {op: [{"var": field}, value]}


class SelectionTests(unittest.TestCase):
    def select(self, objects=None, elements=None, **kwargs):
        filters = AnnotationFilter.parse([objects or {}, elements or {}])
        return filters.select(kwargs.get("available", range(5)), kwargs.get("tags", TAGS),
                              kwargs.get("shapes", SHAPES), kwargs.get("labels", LABELS))

    def test_tag_and_label_select_images_not_only_tags(self):
        self.assertEqual(self.select({"and": [rule("type", "tag"), rule("label", "ready")]}), {0, 2})

    def test_shape_and_occlusion(self):
        self.assertEqual(self.select({"and": [rule("shape", "rectangle"), rule("occluded", True)]}), {1})

    def test_or_groups(self):
        self.assertEqual(self.select({"or": [rule("type", "tag"), rule("shape", "rectangle")]}), {0, 1, 2})

    def test_not_is_per_annotation_not_absence_on_frame(self):
        self.assertEqual(self.select({"!": rule("label", "ready")}), {0, 1, 2})
        # Frame 0 has a matching horse AND the excluded tag, just as the display filter does.
        self.assertIn(0, self.select(rule("label", "ready", "!=")))

    def test_and_does_not_join_two_different_objects(self):
        self.assertEqual(self.select({"and": [rule("label", "ready"), rule("label", "horse")]}), set())

    def test_empty_filter_includes_unannotated_frames(self):
        self.assertEqual(self.select(), set(range(5)))

    def test_exclusions_and_task_local_indices(self):
        self.assertEqual(self.select(rule("type", "tag"), available=[1, 2, 4]), {2})
        self.assertEqual(self.select(rule("type", "tag"), available=[]), set())

    def test_number_and_checkbox_attributes(self):
        self.assertEqual(self.select({"and": [rule("attr.ready.quality", 8, ">="),
                                              rule("attr.ready.checked", True)]}), {2})

    def test_attribute_defaults_and_false_are_not_missing(self):
        self.assertEqual(self.select(rule("attr.ready.quality", 2)), {0})
        self.assertEqual(self.select(rule("attr.ready.checked", False, "===")), {0})

    def test_like_and_list_membership(self):
        self.assertEqual(self.select({"in": ["good", {"var": "attr.ready.note"}]}), {2})
        self.assertEqual(self.select({"in": [{"var": "label"}, ["horse"]]}), {0, 1, 2})

    def test_between_and_field_comparison(self):
        self.assertEqual(self.select({"<=": [15, {"var": "width"}, 25]}), {1})
        self.assertEqual(self.select({">": [{"var": "width"}, {"var": "height"}]}), {1, 2})

    def test_missing_attribute_is_null_not_empty_text(self):
        self.assertEqual(self.select(rule("attr.ready.note", "", "===")), {0})
        self.assertEqual(self.select(rule("attr.ready.note", None, "===")), {0, 1, 2})

    def test_mask_inclusive_bounds_and_ellipse(self):
        self.assertEqual(selection.dimensions([5, 5, 14, 24], "mask"), (10, 20))
        self.assertEqual(selection.dimensions([10, 10, 20, 5], "ellipse"), (20, 10))
        self.assertEqual(selection.dimensions([], "points"), (None, None))

    def test_native_track_frames_and_track_server_id(self):
        self.assertEqual(self.select({"and": [rule("type", "track"), rule("serverID", 300)]}), {2})
        self.assertEqual(self.select(rule("serverID", 202)), set())

    def test_tag_null_dimensions(self):
        self.assertEqual(self.select({"and": [rule("type", "tag"), rule("width", None)]}), {0, 2})

    def test_keypoint_filter_is_correlated_with_parent(self):
        skeleton = {"id": 301, "frame": 3, "type": "skeleton", "label_id": 3, "attributes": [],
                    "elements": [{"label_id": 4, "points": [7, 8], "occluded": True, "attributes": []}]}
        self.assertEqual(self.select(rule("label", "person"), rule("label", "person / nose"),
                                     shapes=[*SHAPES, skeleton]), {3})
        self.assertEqual(self.select(rule("label", "horse"), rule("label", "person / nose"),
                                     shapes=[*SHAPES, skeleton]), set())

    def test_dot_escaping_and_input_nonmutation(self):
        labels = copy.deepcopy(LABELS)
        labels[0]["name"] = "ready.v1"
        labels[0]["attributes"][0]["name"] = "quality.score"
        original = copy.deepcopy((labels, TAGS, SHAPES))
        self.assertEqual(self.select(rule("attr.ready\u2219v1.quality\u2219score", 9), labels=labels), {2})
        self.assertEqual((labels, TAGS, SHAPES), original)

    def test_payload_copy_and_canonical_empty_filters(self):
        payload = [rule("type", "tag")]
        filters = AnnotationFilter.parse(payload)
        payload[0].clear()
        self.assertEqual(filters.to_list(), [rule("type", "tag")])
        self.assertEqual(AnnotationFilter.parse([{}, {}]).to_list(), [])

    def test_rejects_old_modes_ranges_and_malformed_payloads(self):
        for payload in (None, {}, {"mode": "any", "tag_ids": [1]}, [1], [{}, {}, {}]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                AnnotationFilter.parse(payload)
        for field in ("start", "stop", "step", "frame", "objectID", "votes", "attr.ready.unknown", "__proto__"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.select(rule(field, 1))

    def test_rejects_unsupported_operators_arity_and_unbounded_logic(self):
        for expression in ({"eval": ["1"]}, {"log": ["secret"]}, {"and": []}, {"==": [1]},
                           {"var": ["label", "fallback"]}, rule("width", float("nan")),
                           rule("width", float("inf")), {"in": [{"var": "label"}, ["horse"] * 101]},
                           {"==": [1, 2], "!": True}):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                self.select(expression)
        nested = rule("type", "tag")
        for _ in range(20):
            nested = {"!": nested}
        with self.assertRaises(ValueError):
            self.select(nested)

    def test_js_equality_and_numeric_coercion(self):
        evaluate = selection.evaluate
        for op, left, right, expected in [("==", "9", 9, True), ("===", "9", 9, False),
                                          ("==", None, False, False), ("==", "", 0, True),
                                          ("!==", True, 1, True), ("in", 1, [True], False),
                                          (">", "10", "2", False), ("<=", None, 0, True)]:
            with self.subTest(op=op, left=left, right=right):
                self.assertEqual(evaluate({op: [left, right]}, {}), expected)

    def test_selection_has_no_format_or_output_rewriting(self):
        source = (DIRECTORY / "selection.py").read_text()
        self.assertNotIn("ITGformat", source)
        self.assertNotIn("omit_tags", source)
        self.assertNotIn("SUPPORTED_FORMATS", source)


if __name__ == "__main__":
    unittest.main()
