# SPDX-License-Identifier: MIT
"""Dependency-free tests: python -m unittest discover -s tests/filtered_export -v."""
import importlib.util
from pathlib import Path
import sys
import unittest

PATH = Path(__file__).resolve().parents[2] / "cvat/apps/dataset_manager/filtered_export/selection.py"
SPEC = importlib.util.spec_from_file_location("filtered_selection_tested", PATH)
selection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = selection
SPEC.loader.exec_module(selection)
FrameFilter = selection.FrameFilter


class SelectionTests(unittest.TestCase):
    tags = [{"frame": 0, "label_id": 1}, {"frame": 1, "label_id": 1},
            {"frame": 1, "label_id": 2}, {"frame": 2, "label_id": 2},
            {"frame": 3, "label_id": 99}, {"frame": 5, "label_id": 1}]

    def test_any(self):
        self.assertEqual(FrameFilter.parse({"mode": "any", "tag_ids": [1, 2]}).select(range(6), self.tags), {0, 1, 2, 5})

    def test_all(self):
        self.assertEqual(FrameFilter.parse({"mode": "all", "tag_ids": [1, 2]}).select(range(6), self.tags), {1})

    def test_none_is_not_untagged(self):
        self.assertEqual(FrameFilter.parse({"mode": "none", "tag_ids": [1, 2]}).select(range(6), self.tags), {3, 4})

    def test_untagged_checks_all_labels(self):
        self.assertEqual(FrameFilter.parse({"mode": "untagged"}).select(range(6), self.tags), {4})

    def test_all_frames(self):
        self.assertEqual(FrameFilter.parse({"mode": "all_frames"}).select(range(6), self.tags), set(range(6)))

    def test_native_exclusions_are_preserved(self):
        self.assertEqual(FrameFilter.parse({"mode": "any", "tag_ids": [1]}).select([0, 2, 3, 4], self.tags), {0})

    def test_range_inclusive_and_stride_anchored_to_start(self):
        self.assertEqual(FrameFilter.parse({"mode": "all_frames", "start": 1, "stop": 5, "step": 2}).select(range(10), []), {1, 3, 5})

    def test_stride_is_not_every_nth_match(self):
        self.assertEqual(FrameFilter.parse({"mode": "any", "tag_ids": [1], "step": 2}).select(range(6), self.tags), {0})

    def test_empty_and_out_of_range(self):
        for available in ([], range(5)):
            self.assertEqual(FrameFilter.parse({"mode": "all_frames", "start": 10}).select(available, self.tags), set())

    def test_deduplication_and_round_trip(self):
        parsed = FrameFilter.parse({"mode": "all", "tag_ids": [2, 1, 2]})
        self.assertEqual(parsed.tag_ids, (1, 2))
        self.assertEqual(FrameFilter.parse(parsed.to_dict()), parsed)

    def test_rejects_malformed_conditions(self):
        invalid = [None, [], {}, {"mode": "bad"}, {"mode": "any"},
                   {"mode": "all_frames", "extra": 1},
                   {"mode": "untagged", "tag_ids": [1]},
                   {"mode": "any", "tag_ids": "1"},
                   {"mode": "any", "tag_ids": [True]},
                   {"mode": "any", "tag_ids": [0]},
                   {"mode": "any", "tag_ids": [1.0]},
                   {"mode": "any", "tag_ids": [1] * 1001},
                   {"mode": "all_frames", "start": -1},
                   {"mode": "all_frames", "stop": "5"},
                   {"mode": "all_frames", "step": False},
                   {"mode": "all_frames", "step": 0},
                   {"mode": "all_frames", "start": 5, "stop": 4},
                   {"mode": "all_frames", "step": 2**31}]
        for value in invalid:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                FrameFilter.parse(value)

    def test_inputs_are_not_modified(self):
        import copy
        original = copy.deepcopy(self.tags)
        FrameFilter.parse({"mode": "any", "tag_ids": [1]}).select(range(6), self.tags)
        self.assertEqual(self.tags, original)

    def test_fingerprint_is_canonical_and_sensitive(self):
        self.assertEqual(selection.fingerprint({"b": 2, "a": 1}), selection.fingerprint({"a": 1, "b": 2}))
        self.assertNotEqual(selection.fingerprint({"a": 1}), selection.fingerprint({"a": 2}))

    def test_video_format_is_not_offered(self):
        self.assertNotIn("CVAT for video 1.1", selection.SUPPORTED_FORMATS)


if __name__ == "__main__":
    unittest.main()
