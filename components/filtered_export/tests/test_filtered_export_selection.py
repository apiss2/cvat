# SPDX-License-Identifier: MIT
"""Pure matching tests; the fixtures use CVAT's annotation-filter field names."""
import copy
import importlib.util
from pathlib import Path
import sys
import types
import pytest

DIRECTORY = Path(__file__).resolve().parents[3] / "cvat/apps/dataset_manager/filtered_export"
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
    {
        "id": 1,
        "name": "ready",
        "type": "tag",
        "sublabels": [],
        "attributes": [
            {
                "id": 10,
                "name": "quality",
                "input_type": "number",
                "default_value": "2",
                "values": [],
            },
            {
                "id": 11,
                "name": "checked",
                "input_type": "checkbox",
                "default_value": "false",
                "values": [],
            },
            {"id": 12, "name": "note", "input_type": "text", "default_value": "", "values": []},
        ],
    },
    {"id": 2, "name": "horse", "type": "any", "attributes": [], "sublabels": []},
    {
        "id": 3,
        "name": "person",
        "type": "skeleton",
        "attributes": [],
        "sublabels": [
            {"id": 4, "name": "nose", "type": "points", "attributes": [], "sublabels": []},
        ],
    },
]
TAGS = [
    {"id": 100, "frame": 0, "label_id": 1, "source": "manual", "attributes": []},
    {
        "id": 101,
        "frame": 2,
        "label_id": 1,
        "source": "auto",
        "attributes": [
            {"spec_id": 10, "value": "9"},
            {"spec_id": 11, "value": "true"},
            {"spec_id": 12, "value": "good image"},
        ],
    },
]
SHAPES = [
    {
        "id": 200,
        "frame": 0,
        "label_id": 2,
        "type": "mask",
        "points": [5, 5, 14, 24],
        "occluded": False,
        "source": "manual",
        "attributes": [],
    },
    {
        "id": 201,
        "frame": 1,
        "label_id": 2,
        "type": "rectangle",
        "points": [0, 0, 20, 10],
        "rotation": 30,
        "occluded": True,
        "source": "auto",
        "attributes": [],
    },
    {
        "id": 202,
        "track_id": 300,
        "frame": 2,
        "label_id": 2,
        "type": "rectangle",
        "points": [0, 0, 30, 10],
        "occluded": False,
        "source": "manual",
        "attributes": [],
    },
    {
        "id": 203,
        "track_id": 300,
        "frame": 4,
        "label_id": 2,
        "type": "rectangle",
        "points": [0, 0, 50, 10],
        "outside": True,
        "attributes": [],
    },
]


def rule(field, value, op="=="):
    return {op: [{"var": field}, value]}


class TestSelection:
    def select(self, objects=None, elements=None, **kwargs):
        filters = AnnotationFilter.parse([objects or {}, elements or {}])
        return filters.select(
            kwargs.get("available", range(5)),
            kwargs.get("tags", TAGS),
            kwargs.get("shapes", SHAPES),
            kwargs.get("labels", LABELS),
        )

    def test_tag_and_label_select_images_not_only_tags(self):
        assert self.select({"and": [rule("type", "tag"), rule("label", "ready")]}) == {0, 2}

    def test_shape_and_occlusion(self):
        assert self.select({"and": [rule("shape", "rectangle"), rule("occluded", True)]}) == {1}

    def test_or_groups(self):
        assert self.select({"or": [rule("type", "tag"), rule("shape", "rectangle")]}) == {0, 1, 2}

    def test_not_is_per_annotation_not_absence_on_frame(self):
        assert self.select({"!": rule("label", "ready")}) == {0, 1, 2}
        # Frame 0 has a matching horse AND the excluded tag, just as the display filter does.
        assert 0 in self.select(rule("label", "ready", "!="))

    def test_and_does_not_join_two_different_objects(self):
        assert self.select({"and": [rule("label", "ready"), rule("label", "horse")]}) == set()

    def test_empty_filter_includes_unannotated_frames(self):
        assert self.select() == set(range(5))

    def test_exclusions_and_task_local_indices(self):
        assert self.select(rule("type", "tag"), available=[1, 2, 4]) == {2}
        assert self.select(rule("type", "tag"), available=[]) == set()

    def test_number_and_checkbox_attributes(self):
        assert self.select(
            {"and": [rule("attr.ready.quality", 8, ">="), rule("attr.ready.checked", True)]}
        ) == {2}

    def test_attribute_defaults_and_false_are_not_missing(self):
        assert self.select(rule("attr.ready.quality", 2)) == {0}
        assert self.select(rule("attr.ready.checked", False, "===")) == {0}

    def test_like_and_list_membership(self):
        assert self.select({"in": ["good", {"var": "attr.ready.note"}]}) == {2}
        assert self.select({"in": [{"var": "label"}, ["horse"]]}) == {0, 1, 2}

    def test_between_and_field_comparison(self):
        assert self.select({"<=": [15, {"var": "width"}, 25]}) == {1}
        assert self.select({">": [{"var": "width"}, {"var": "height"}]}) == {1, 2}

    def test_missing_attribute_is_null_not_empty_text(self):
        assert self.select(rule("attr.ready.note", "", "===")) == {0}
        assert self.select(rule("attr.ready.note", None, "===")) == {0, 1, 2}

    def test_mask_inclusive_bounds_and_ellipse(self):
        assert selection.dimensions([5, 5, 14, 24], "mask") == (10, 20)
        assert selection.dimensions([10, 10, 20, 5], "ellipse") == (20, 10)
        assert selection.dimensions([], "points") == (None, None)

    def test_native_track_frames_and_track_server_id(self):
        assert self.select({"and": [rule("type", "track"), rule("serverID", 300)]}) == {2}
        assert self.select(rule("serverID", 202)) == set()

    def test_tag_null_dimensions(self):
        assert self.select({"and": [rule("type", "tag"), rule("width", None)]}) == {0, 2}

    def test_keypoint_filter_is_correlated_with_parent(self):
        skeleton = {
            "id": 301,
            "frame": 3,
            "type": "skeleton",
            "label_id": 3,
            "attributes": [],
            "elements": [{"label_id": 4, "points": [7, 8], "occluded": True, "attributes": []}],
        }
        assert self.select(
            rule("label", "person"), rule("label", "person / nose"), shapes=[*SHAPES, skeleton]
        ) == {3}
        assert (
            self.select(
                rule("label", "horse"), rule("label", "person / nose"), shapes=[*SHAPES, skeleton]
            )
            == set()
        )

    def test_dot_escaping_and_input_nonmutation(self):
        labels = copy.deepcopy(LABELS)
        labels[0]["name"] = "ready.v1"
        labels[0]["attributes"][0]["name"] = "quality.score"
        original = copy.deepcopy((labels, TAGS, SHAPES))
        assert self.select(rule("attr.ready∙v1.quality∙score", 9), labels=labels) == {2}
        assert (labels, TAGS, SHAPES) == original

    def test_payload_copy_and_canonical_empty_filters(self):
        payload = [rule("type", "tag")]
        filters = AnnotationFilter.parse(payload)
        payload[0].clear()
        assert filters.to_list() == [rule("type", "tag")]
        assert AnnotationFilter.parse([{}, {}]).to_list() == []

    @pytest.mark.parametrize(
        "payload", [None, {}, {"mode": "any", "tag_ids": [1]}, [1], [{}, {}, {}]]
    )
    def test_rejects_old_modes_and_malformed_payloads(self, payload):
        with pytest.raises(ValueError):
            AnnotationFilter.parse(payload)

    @pytest.mark.parametrize(
        "field",
        ["start", "stop", "step", "frame", "objectID", "votes", "attr.ready.unknown", "__proto__"],
    )
    def test_rejects_unknown_fields(self, field):
        with pytest.raises(ValueError):
            self.select(rule(field, 1))

    @pytest.mark.parametrize(
        "expression",
        [
            {"eval": ["1"]},
            {"log": ["secret"]},
            {"and": []},
            {"==": [1]},
            {"var": ["label", "fallback"]},
            rule("width", float("nan")),
            rule("width", float("inf")),
            {"in": [{"var": "label"}, ["horse"] * 101]},
            {"==": [1, 2], "!": True},
        ],
    )
    def test_rejects_unsupported_operators_and_arity(self, expression):
        with pytest.raises(ValueError):
            self.select(expression)

    def test_rejects_unbounded_logic(self):
        nested = rule("type", "tag")
        for _ in range(20):
            nested = {"!": nested}
        with pytest.raises(ValueError):
            self.select(nested)

    @pytest.mark.parametrize(
        "op, left, right, expected",
        [
            ("==", "9", 9, True),
            ("===", "9", 9, False),
            ("==", None, False, False),
            ("==", "", 0, True),
            ("!==", True, 1, True),
            ("in", 1, [True], False),
            (">", "10", "2", False),
            ("<=", None, 0, True),
        ],
    )
    def test_js_equality_and_numeric_coercion(self, op, left, right, expected):
        assert selection.evaluate({op: [left, right]}, {}) == expected

    def test_selection_has_no_format_or_output_rewriting(self):
        source = (DIRECTORY / "selection.py").read_text()
        assert "ITGformat" not in source
        assert "omit_tags" not in source
        assert "SUPPORTED_FORMATS" not in source
