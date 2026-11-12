# SPDX-License-Identifier: MIT
import base64
import json
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, headers, upload
from registry.codec import MAX_OBJECTS, MAX_POLYGON_VERTICES, MAX_TOTAL_POLYGON_VERTICES, encode_results
from registry.partial_updates import merge_manifest
from registry.schema import Manifest, function_id, function_metadata
from registry.sdk import Box, Polyline, Tag
from registry.wire import validate_wire


@pytest.fixture
def manifest():
    return Manifest.model_validate_json((ROOT / "examples/line/manifest.json").read_text())


@pytest.fixture
def mixed():
    return Manifest(name="mixed", weights=["model.onnx"], labels=[
        {"id": 0, "name": "lane", "type": "polyline"},
        {"id": 1, "name": "car", "type": "rectangle"},
        {"id": 2, "name": "night", "type": "tag"},
    ])


def test_metadata_exposes_polyline_labels_to_cvat_detector(manifest):
    metadata = function_metadata("a" * 20, "b" * 16, manifest.model_dump(), "nuclio")["metadata"]["annotations"]
    assert metadata["type"] == "detector"
    assert json.loads(metadata["spec"]) == [{"id": 0, "name": "bright_region", "type": "polyline"}]


def test_polylines_are_not_interactor_or_segmentation_labels(manifest):
    with pytest.raises(ValueError):
        merge_manifest(manifest.model_dump(), {"kind": "interactor", "interactor": {
            "sample": {"pos_points": [[1, 1]], "neg_points": []}}})
    with pytest.raises(ValueError, match="polygon labels"):
        encode_results(np.zeros((1, 48, 64), dtype=np.uint8), manifest, (48, 64, 3))


@pytest.mark.parametrize("count", [0, 1, 3, 250])
def test_any_number_of_polylines_round_trip(manifest, count):
    lines = [Polyline(0, .5, [(i % 64, 0), (i % 64, 48)]) for i in range(count)]
    output = encode_results(lines, manifest, (48, 64, 3))
    assert len(output) == count
    assert all(obj["type"] == "polyline" and obj["label"] == "bright_region" for obj in output)
    assert validate_wire(output, manifest, 64, 48) == output


def test_polyline_formats_edges_and_mixed_outputs(mixed):
    lines = [
        Polyline(0, 1, [(0, 0), (64, 48)]),
        Polyline(0, .25, np.array([[1, 1], [10, 1], [10, 10], [1, 10], [9, 2]], dtype=np.int32)),
        Polyline(0, .5, ((0.5, 47.5), (63.5, 0.5))),
        Box(1, .9, (1, 2, 3, 4)), Tag(2, .7),
    ]
    output = encode_results(lines, mixed, (48, 64, 3))
    assert [obj["type"] for obj in output] == ["polyline", "polyline", "polyline", "rectangle", "tag"]
    assert output[0]["points"] == [0.0, 0.0, 64.0, 48.0]
    # Self-crossing open lines are valid polylines.
    assert output[1]["points"] == [1.0, 1.0, 10.0, 1.0, 10.0, 10.0, 1.0, 10.0, 9.0, 2.0]
    assert validate_wire(output, mixed, 64, 48) == output


@pytest.mark.parametrize("item", [
    Polyline(0, .5, [(1, 1)]),
    Polyline(0, .5, []),
    Polyline(0, .5, [(1, 1), (1, 1)]),
    Polyline(0, .5, [(1, 1), (2, 2), (2, 2)]),
    Polyline(0, .5, [(-0.1, 1), (2, 2)]),
    Polyline(0, .5, [(1, 1), (64.1, 2)]),
    Polyline(0, .5, [(1, 1), (2, 48.1)]),
    Polyline(0, .5, [(1, float("nan")), (2, 2)]),
    Polyline(0, .5, [(1, float("inf")), (2, 2)]),
    Polyline(0, .5, [(True, 1), (2, 2)]),
    Polyline(0, .5, [("1", 1), (2, 2)]),
    Polyline(0, .5, [(1, 1, 1), (2, 2, 2)]),
    Polyline(0, .5, np.zeros((3, 3))),
    Polyline(0, .5, np.array([[1, 1], [2, 2]], dtype=bool)),
    Polyline(0, .5, [(i % 2, i % 3) for i in range(MAX_POLYGON_VERTICES + 1)]),
    Polyline(0, 1.5, [(1, 1), (2, 2)]),
    Polyline(1, .5, [(1, 1), (2, 2)]),
    Polyline(2, .5, [(1, 1), (2, 2)]),
    Box(0, .5, (1, 1, 2, 2)),
])
def test_worker_rejects_invalid_polylines(mixed, item):
    with pytest.raises((TypeError, ValueError)):
        encode_results([item], mixed, (48, 64, 3))


def test_object_and_vertex_budgets_are_shared(manifest):
    with pytest.raises(ValueError, match="2000"):
        encode_results([Polyline(0, .5, [(0, 0), (1, 1)])] * (MAX_OBJECTS + 1), manifest, (48, 64, 3))
    long_line = [(i % 64, (i // 64) % 48) for i in range(MAX_POLYGON_VERTICES)]
    count = MAX_TOTAL_POLYGON_VERTICES // MAX_POLYGON_VERTICES + 1
    with pytest.raises(ValueError, match="vertices"):
        encode_results([Polyline(0, .5, long_line)] * count, manifest, (48, 64, 3))
    wire = [{"label": "bright_region", "type": "polyline", "confidence": .5,
             "points": [float(v) for point in long_line for v in point]}] * count
    with pytest.raises(ValueError, match="vertex budget"):
        validate_wire(wire, manifest, 64, 48)


@pytest.mark.parametrize("points", [
    [1, 1], [1, 1, 2], [1, 1, 1, 1], [1, 1, 2, 2, 2, 2], [-1, 0, 2, 2], [0, 0, 65, 2],
    [0, 0, 2, 49], [0, 0, True, 2], [0, 0, "2", 2], "0,0,2,2",
])
def test_control_plane_rejects_untrusted_polylines(manifest, points):
    with pytest.raises(ValueError):
        validate_wire([{"label": "bright_region", "type": "polyline", "confidence": .5, "points": points}], manifest, 64, 48)
    with pytest.raises(ValueError):
        validate_wire([{"label": "bright_region", "type": "rectangle", "confidence": .5, "points": [0, 0, 2, 2]}], manifest, 64, 48)


def test_real_onnx_line_demo(monkeypatch, manifest):
    pytest.importorskip("onnxruntime", reason="ONNX Runtime is not installed in this environment")
    from registry.worker import create_worker
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "uploaded_model", None)
    monkeypatch.setenv("MR_PROVIDERS", "CPUExecutionProvider")
    payload = {"image": base64.b64encode((ROOT / "examples/line/sample.png").read_bytes()).decode()}
    with TestClient(create_worker(ROOT / "examples/line")) as client:
        response = client.post("/predict", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert [obj["type"] for obj in result] == ["polyline", "polyline"]
        assert result[0]["points"] == [12.0, 20.0, 44.0, 20.0]
        assert result[1]["points"] == [12.0, 8.0, 44.0, 8.0, 44.0, 32.0, 12.0, 32.0]
        assert validate_wire(result, manifest, 64, 48) == result


def test_registration_and_cvat_invoke(ctx, sample):
    operation = upload(ctx, "line")
    assert operation["status"] == "succeeded", operation
    fid = function_id(operation["model_id"], operation["revision"])
    client = ctx["client"]
    meta = client.get("/internal/functions/" + fid, headers=headers("service")).json()["metadata"]["annotations"]
    assert json.loads(meta["spec"])[0]["type"] == "polyline"
    response = client.post("/internal/functions/" + fid + "/invoke", headers=headers("service"), json={"image": sample})
    assert response.status_code == 200, response.text
    assert response.json() == [{"label": "bright_region", "confidence": .9, "type": "polyline", "points": [12, 20, 44, 20]}]
