"""Run through manage.py shell in the modified CVAT image. No DB data is created.

This script uses real Datumaro, pycocotools, CVAT registration and MaskConverter.
It is not an HTTP/UI/task-database end-to-end test.
"""

import tempfile
import types
import unittest
from pathlib import Path

import datumaro as dm
import numpy as np

from cvat.apps.dataset_manager.formats.itgformat.adapter import (
    build_dataset,
    export_items,
)
from cvat.apps.dataset_manager.formats.itgformat.archive import read_records
from cvat.apps.dataset_manager.formats.itgformat.codec import (
    FormatError,
    read_mask,
    write_mask,
)
from cvat.apps.dataset_manager.formats.registry import EXPORT_FORMATS, IMPORT_FORMATS
from cvat.apps.dataset_manager.formats.transformations import MaskConverter


class NativeChecks(unittest.TestCase):
    def make_image(self):
        return dm.Image.from_numpy(
            data=np.zeros((7, 11, 3), dtype=np.uint8), ext=".png"
        )

    def test_registry(self):
        self.assertTrue(IMPORT_FORMATS["ITGformat 1.0"].ENABLED)
        self.assertTrue(EXPORT_FORMATS["ITGformat 1.0"].ENABLED)
        self.assertEqual(str(IMPORT_FORMATS["ITGformat 1.0"].DIMENSION), "2d")

    def test_mixed_and_empty_frames(self):
        a = np.ones((7, 11), dtype=bool)
        a[2:5, 3:8] = False
        b = np.zeros((7, 11), dtype=bool)
        b[:3, :4] = True
        items = [
            dm.DatasetItem(
                id="a",
                media=self.make_image(),
                annotations=[
                    dm.Mask(image=a, label=0),
                    dm.Mask(image=b, label=1),
                    dm.Bbox(1, 2, 3, 2, label=2),
                ],
            ),
            dm.DatasetItem(id="b", media=self.make_image()),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export_items(items, ["outer", "overlap", "box"], root)
            self.assertEqual(len(list(root.glob("*.mask.hdr"))), 2)
            self.assertEqual(len(list(root.glob("*.bb"))), 2)
            self.assertEqual((root / "b.bb").read_bytes(), b"")
            _, empty = read_mask(root / "b.mask.hdr")
            self.assertFalse(empty.any())
            records, labels = read_records(root)
            recovered = build_dataset(records, labels)
            first = next(item for item in recovered if item.id == "a")
            masks = {
                labels[ann.label]: ann.image
                for ann in first.annotations
                if ann.type == dm.AnnotationType.mask
            }
            np.testing.assert_array_equal(masks["outer"], a)
            np.testing.assert_array_equal(masks["overlap"], b)

    def test_polygon_and_image(self):
        polygon = dm.Polygon([1, 1, 4, 1, 4, 4, 1, 4], label=0)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            export_items(
                [
                    dm.DatasetItem(
                        id="a", media=self.make_image(), annotations=[polygon]
                    )
                ],
                ["region"],
                root,
                save_images=True,
            )
            self.assertTrue((root / "a.png").is_file())
            _, packed = read_mask(root / "a.mask.hdr")
            self.assertEqual(int(np.count_nonzero(packed)), 9)
            self.assertFalse((root / "a.bb").exists())

    def test_bb_only_import_matches_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.bb").write_text("1 2 0 4 4 1 box\n")
            records, labels = read_records(root)
            ds = build_dataset(
                records,
                labels,
                frame_info={0: {"path": "folder/a.png", "width": 11, "height": 7}},
            )
            item = next(iter(ds))
            self.assertEqual(item.id, "folder/a")
            self.assertEqual(list(item.annotations[0].points), [1, 2, 4, 4])

    def test_mask_cvat_bridge_preserves_pixels(self):
        a = np.ones((7, 11), dtype=bool)
        a[2:5, 3:8] = False
        a[0, :] = False
        original = dm.Mask(image=a, label=0)
        points = MaskConverter.dm_mask_to_cvat_rle(original)
        shape = types.SimpleNamespace(
            points=points, label=0, z_order=0, attributes={}, group=0
        )
        reconstructed = MaskConverter.cvat_rle_to_dm_rle(shape, 7, 11)
        np.testing.assert_array_equal(reconstructed.image.astype(bool), a)

    def test_rotated_box_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            ann = dm.Bbox(1, 1, 3, 2, label=0, attributes={"rotation": 30})
            with self.assertRaises(FormatError):
                export_items(
                    [
                        dm.DatasetItem(
                            id="a", media=self.make_image(), annotations=[ann]
                        )
                    ],
                    ["box"],
                    Path(temp),
                )

    def test_scope_width_boundaries(self):
        for count, expected_bytes in ((8, 1), (9, 2), (16, 2), (17, 4), (32, 4)):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temp:
                labels = [f"c{i:02}" for i in range(count)]
                annotations = [
                    dm.Mask(image=np.ones((7, 11), bool), label=i) for i in range(count)
                ]
                items = [
                    dm.DatasetItem(
                        id="full", media=self.make_image(), annotations=annotations
                    ),
                    dm.DatasetItem(id="empty", media=self.make_image()),
                ]
                root = Path(temp)
                export_items(items, labels, root)
                for item in items:
                    header, packed = read_mask(root / f"{item.id}.mask.hdr")
                    self.assertEqual(header.byte_count, expected_bytes)
                    self.assertEqual(packed.dtype.itemsize, expected_bytes)
                    self.assertEqual(
                        int(packed[0, 0]), (1 << count) - 1 if item.id == "full" else 0
                    )
                records, imported_labels = read_records(root)
                ds = build_dataset(records, imported_labels)
                full = next(item for item in ds if item.id == "full")
                self.assertEqual(len(full.annotations), count)
                for ann in full.annotations:
                    self.assertTrue(ann.image.all())

    def test_hdr_widths_and_high_bits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for byte_count in (1, 2, 4):
                bit = byte_count * 8 - 1
                array = np.zeros((7, 11), dtype=f"u{byte_count}")
                array[2:5, 3:8] = 1 << bit
                write_mask(
                    root / f"width{byte_count}.mask.hdr", array, {bit: f"c{bit}"}
                )
            records, labels = read_records(root)
            ds = build_dataset(records, labels)
            self.assertEqual(len(ds), 3)
            for item in ds:
                self.assertEqual(len(item.annotations), 1)
                self.assertEqual(int(item.annotations[0].image.sum()), 15)

    def test_scope_overflow_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            labels = [f"c{i:02}" for i in range(33)]
            annotations = [
                dm.Mask(image=np.ones((7, 11), bool), label=i) for i in range(33)
            ]
            with self.assertRaises(FormatError):
                export_items(
                    [
                        dm.DatasetItem(
                            id="a", media=self.make_image(), annotations=annotations
                        )
                    ],
                    labels,
                    Path(temp),
                )

    def test_many_bbox_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            labels = [f"box{i:02}" for i in range(40)]
            annotations = [dm.Bbox(1, 1, 3, 2, label=i) for i in range(40)]
            root = Path(temp)
            export_items(
                [
                    dm.DatasetItem(
                        id="a", media=self.make_image(), annotations=annotations
                    )
                ],
                labels,
                root,
            )
            self.assertFalse(list(root.glob("*.mask.*")))
            self.assertEqual(len((root / "a.bb").read_text().splitlines()), 40)


suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeChecks)
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    raise SystemExit(1)
