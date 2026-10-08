"""Control-flow tests with explicit substitutes for unavailable CVAT/Datumaro.

These test our adapter's file policy, not the real CVAT bridge or rasterizer.
Run scripts/check_in_cvat.py in the deployed image to test those real modules.
"""

import importlib.util
import io
import sys
import types
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import numpy as np
import pytest
from itgformat_core.archive import MANIFEST, make_zip, read_records
from itgformat_core.codec import FormatError, read_mask
from PIL import Image as PillowImage


class AnnType(Enum):
    mask = auto()
    polygon = auto()
    ellipse = auto()
    bbox = auto()
    label = auto()


@dataclass
class Media:
    size: tuple = (5, 7)
    ext: str = ".png"
    path: str = "unused.png"

    @classmethod
    def from_file(cls, path, size, ext):
        return cls(size, ext, path)

    def save(self, path):
        PillowImage.fromarray(np.zeros((*self.size, 3), np.uint8)).save(path)


@dataclass
class Ann:
    type: AnnType
    label: int = 0
    image: object = None
    points: list = field(default_factory=list)
    attributes: dict = field(default_factory=dict)


@dataclass
class Item:
    id: str
    annotations: list = field(default_factory=list)
    media: Media = field(default_factory=Media)
    subset: str = "default"

    def wrap(self, **kwargs):
        from dataclasses import replace

        return replace(self, **kwargs)


@pytest.fixture
def adapter(monkeypatch):
    import itgformat_core

    dm = types.ModuleType("datumaro")
    dm.AnnotationType, dm.Image, dm.DatasetItem = AnnType, Media, Item
    dm.RleMask = lambda rle, label: Ann(AnnType.mask, label, image=rle["binary"])
    dm.Bbox = lambda x, y, w, h, label: Ann(
        AnnType.bbox, label, points=[x, y, x + w, y + h]
    )
    dm.Dataset = types.SimpleNamespace(
        from_iterable=lambda items, categories, env: items
    )
    monkeypatch.setitem(sys.modules, "datumaro", dm)
    coco = types.ModuleType("pycocotools")
    coco.mask = types.SimpleNamespace(encode=lambda a: {"binary": a.copy()})
    monkeypatch.setitem(sys.modules, "pycocotools", coco)
    for path in [
        "cvat",
        "cvat.apps",
        "cvat.apps.dataset_manager",
        "cvat.apps.dataset_manager.formats",
    ]:
        mod = types.ModuleType(path)
        mod.__path__ = []
        monkeypatch.setitem(sys.modules, path, mod)
    bindings = types.ModuleType("cvat.apps.dataset_manager.bindings")

    class ImportErrorForTest(Exception):
        pass

    class ExportErrorForTest(Exception):
        pass

    bindings.CvatImportError = ImportErrorForTest
    bindings.CvatExportError = ExportErrorForTest
    bindings.GetCVATDataExtractor = None
    bindings.import_dm_annotations = lambda dataset, instance: setattr(
        instance, "imported", dataset
    )
    monkeypatch.setitem(sys.modules, bindings.__name__, bindings)
    reg = types.ModuleType("cvat.apps.dataset_manager.formats.registry")
    reg.dm_env = object()
    reg.exporter = reg.importer = lambda **kwargs: lambda function: function
    monkeypatch.setitem(sys.modules, reg.__name__, reg)
    transforms = types.ModuleType("cvat.apps.dataset_manager.formats.transformations")
    transforms.EllipsesToMasks = object()
    monkeypatch.setitem(sys.modules, transforms.__name__, transforms)
    name = "itgformat_core.adapter_test"
    spec = importlib.util.spec_from_file_location(
        name, Path(itgformat_core.__path__[0]) / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("mode", ["none", "mask", "bbox", "both", "polygon"])
@pytest.mark.parametrize("save_images", [False, True])
def test_scope_wide_file_policy(adapter, tmp_path, monkeypatch, mode, save_images):
    anns = []
    if mode in {"mask", "both"}:
        anns.append(Ann(AnnType.mask, image=np.ones((5, 7), bool)))
    if mode in {"bbox", "both"}:
        anns.append(Ann(AnnType.bbox, points=[0, 0, 2, 2]))
    if mode == "polygon":
        anns.append(Ann(AnnType.polygon, points=[0, 0, 2, 0, 2, 2]))
        monkeypatch.setattr(adapter, "rasterize", lambda *a: np.ones((5, 7), bool))
    items = [Item("a", anns), Item("b"), Item("c")]
    adapter.export_items(items, ["organ"], tmp_path, save_images=save_images)
    assert len(list(tmp_path.glob("*.mask.hdr"))) == (
        3 if mode in {"mask", "both", "polygon"} else 0
    )
    assert len(list(tmp_path.glob("*.mask.re4"))) == (
        3 if mode in {"mask", "both", "polygon"} else 0
    )
    assert len(list(tmp_path.glob("*.bb"))) == (3 if mode in {"bbox", "both"} else 0)
    assert len(list(tmp_path.glob("*.png"))) == (3 if save_images else 0)
    assert (tmp_path / MANIFEST).is_file()
    if (tmp_path / "b.mask.hdr").exists():
        header, packed = read_mask(tmp_path / "b.mask.hdr")
        assert not packed.any() and header.bit_names == {0: "organ"}
    if (tmp_path / "b.bb").exists():
        assert (tmp_path / "b.bb").read_bytes() == b""
    records, labels = read_records(tmp_path)
    assert len(records) == 3 and labels == ["organ"]


def test_semantic_mask_roundtrip_with_overlap_and_hole(adapter, tmp_path):
    a = np.ones((5, 7), bool)
    a[1:4, 1:6] = False
    b = np.zeros((5, 7), bool)
    b[:2, :2] = True
    adapter.export_items(
        [Item("a", [Ann(AnnType.mask, 0, a), Ann(AnnType.mask, 1, b)])],
        ["z", "a"],
        tmp_path,
    )
    header, packed = read_mask(tmp_path / "a.mask.hdr")
    assert header.bit_names == {0: "a", 1: "z"}
    assert packed[0, 0] == 3 and packed[2, 2] == 0
    records, labels = read_records(tmp_path)
    dataset = adapter.build_dataset(records, labels)
    recovered = {labels[ann.label]: ann.image for ann in dataset[0].annotations}
    np.testing.assert_array_equal(recovered["z"], a)
    np.testing.assert_array_equal(recovered["a"], b)


def test_full_width_masks_and_many_boxes(adapter, tmp_path):
    labels = [f"c{i:02}" for i in range(40)]
    annotations = [Ann(AnnType.mask, i, np.ones((5, 7), bool)) for i in range(32)]
    annotations += [Ann(AnnType.bbox, i, points=[0, 0, 1, 1]) for i in range(40)]
    adapter.export_items([Item("a", annotations)], labels, tmp_path)
    assert len((tmp_path / "a.bb").read_text().splitlines()) == 40
    _, packed = read_mask(tmp_path / "a.mask.hdr")
    assert np.all(packed == 0xFFFFFFFF)


def test_more_than_wire_capacity_segments_fails(adapter, tmp_path):
    anns = [Ann(AnnType.mask, i, np.ones((5, 7), bool)) for i in range(33)]
    with pytest.raises(FormatError, match="32"):
        adapter.export_items([Item("a", anns)], [f"c{i}" for i in range(33)], tmp_path)


@pytest.mark.parametrize(
    "ann",
    [
        Ann(AnnType.bbox, points=[0, 0, 1, 1], attributes={"rotation": 30}),
        Ann(AnnType.label),
    ],
)
def test_unrepresentable_shapes_fail(adapter, tmp_path, ann):
    with pytest.raises(FormatError):
        adapter.export_items([Item("a", [ann])], ["a"], tmp_path)


def test_bbox_label_with_space_fails(adapter, tmp_path):
    with pytest.raises(FormatError):
        adapter.export_items(
            [Item("a", [Ann(AnnType.bbox, points=[0, 0, 1, 1])])],
            ["left lung"],
            tmp_path,
        )


def test_multi_subset_keeps_identity(adapter, tmp_path):
    adapter.export_items(
        [Item("a", subset="train"), Item("a", subset="val")],
        [],
        tmp_path,
        save_images=True,
    )
    records, _ = read_records(tmp_path)
    assert [(r.item_id, r.subset) for r in records] == [("a", "train"), ("a", "val")]
    assert (tmp_path / "train/a.png").exists() and (tmp_path / "val/a.png").exists()


@pytest.mark.parametrize(
    "items",
    [[Item("a"), Item("a")], [Item("a"), Item("a.png/b")], [Item("itgformat.json/a")]],
)
def test_collisions_fail(adapter, tmp_path, items):
    with pytest.raises(FormatError):
        adapter.export_items(items, [], tmp_path)


def test_task_bb_without_images(adapter, tmp_path):
    (tmp_path / "a.bb").write_text("0 0 0 2 2 1 x\n")
    records, labels = read_records(tmp_path)
    items = adapter.build_dataset(
        records,
        labels,
        frame_info={0: {"path": "folder/a.png", "width": 7, "height": 5}},
    )
    assert items[0].id == "folder/a" and items[0].annotations[0].points == [0, 0, 2, 2]


def test_project_import_requires_all_images_before_callback(adapter, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bb").write_text("")
    stream = io.BytesIO()
    make_zip(source, stream)
    stream.seek(0)
    called = []
    with pytest.raises(adapter.CvatImportError, match="requires an image"):
        adapter._import(
            stream,
            tmp_path / "extract",
            object(),
            load_data_callback=lambda *args: called.append(True),
        )
    assert not called


def test_import_ignores_polygon_conversion_flag(adapter, tmp_path):
    source = tmp_path / "source"
    adapter.export_items(
        [Item("a", [Ann(AnnType.mask, image=np.ones((5, 7), bool))])], ["a"], source
    )
    stream = io.BytesIO()
    make_zip(source, stream)
    stream.seek(0)
    instance = types.SimpleNamespace(
        META_FIELD="task",
        meta={"task": {"labels": [("label", {"name": "a"})]}},
        frame_info={0: {"path": "a.png", "width": 7, "height": 5}},
    )
    adapter._import(stream, tmp_path / "out", instance, conv_mask_to_poly=True)
    assert instance.imported[0].annotations[0].type == AnnType.mask


@pytest.mark.parametrize(
    "count, expected_bytes",
    [(1, 1), (8, 1), (9, 2), (16, 2), (17, 4), (31, 4), (32, 4)],
)
def test_export_width_and_empty_files_follow_scope(
    adapter, tmp_path, count, expected_bytes
):
    labels = [f"c{i:02}" for i in range(count)]
    # Each class appears in a different image: capacity is for the whole scope.
    items = [
        Item(f"item_{i:02}", [Ann(AnnType.mask, i, np.ones((5, 7), bool))])
        for i in range(count)
    ] + [Item("empty")]
    adapter.export_items(items, labels, tmp_path)
    for index, item in enumerate(items):
        header, packed = read_mask(tmp_path / f"{item.id}.mask.hdr")
        assert header.byte_count == expected_bytes
        assert len(header.bit_names) == count
        assert packed.dtype.itemsize == expected_bytes
        assert int(packed[0, 0]) == (1 << index if index < count else 0)
    records, recovered_labels = read_records(tmp_path)
    recovered = adapter.build_dataset(records, recovered_labels)
    for item in recovered:
        if item.id == "empty":
            assert not item.annotations
        else:
            assert len(item.annotations) == 1
            expected = "c" + item.id.split("_")[-1]
            assert recovered_labels[item.annotations[0].label] == expected
            assert item.annotations[0].image.all()


def test_boxes_and_unused_labels_do_not_reserve_mask_bits(adapter, tmp_path):
    labels = [f"c{i:03}" for i in range(100)]
    annotations = [Ann(AnnType.mask, 0, np.ones((5, 7), bool))]
    annotations += [Ann(AnnType.bbox, i, points=[0, 0, 1, 1]) for i in range(1, 70)]
    adapter.export_items([Item("a", annotations)], labels, tmp_path)
    header, packed = read_mask(tmp_path / "a.mask.hdr")
    assert header.byte_count == 1 and header.bit_names == {0: labels[0]}
    assert len((tmp_path / "a.bb").read_text().splitlines()) == 69


def test_only_boxes_have_no_mask_capacity_limit(adapter, tmp_path):
    labels = [f"box{i:03}" for i in range(100)]
    annotations = [Ann(AnnType.bbox, i, points=[0, 0, 1, 1]) for i in range(100)]
    adapter.export_items([Item("a", annotations)], labels, tmp_path)
    assert len((tmp_path / "a.bb").read_text().splitlines()) == 100
    assert not list(tmp_path.glob("*.mask.*"))


def test_outside_segments_do_not_consume_capacity(adapter, tmp_path):
    labels = [f"c{i:02}" for i in range(33)]
    annotations = [
        Ann(AnnType.mask, i, np.ones((5, 7), bool), attributes={"outside": True})
        for i in range(33)
    ]
    adapter.export_items([Item("a", annotations)], labels, tmp_path)
    assert not list(tmp_path.glob("*.mask.*"))


def test_sparse_high_bit_import_preserves_label_and_pixels(adapter, tmp_path):
    from itgformat_core.codec import write_mask

    source = np.array([[0, 1 << 31, 0], [1 << 31, 0, 1 << 31]], dtype=np.uint32)
    write_mask(tmp_path / "a.mask.hdr", source, {31: "high"})
    records, labels = read_records(tmp_path)
    ds = adapter.build_dataset(records, labels)
    assert labels == ["high"] and len(ds[0].annotations) == 1
    np.testing.assert_array_equal(ds[0].annotations[0].image, source != 0)


def test_oversized_scope_rejected_before_creating_output(adapter, tmp_path):
    labels = [f"c{i:02}" for i in range(33)]
    items = [
        Item(f"a{i}", [Ann(AnnType.mask, i, np.ones((5, 7), bool))]) for i in range(33)
    ]
    root = tmp_path / "output"
    with pytest.raises(FormatError, match="32"):
        adapter.export_items(items, labels, root)
    assert not root.exists()
