# SPDX-License-Identifier: MIT
"""Classification contracts, package validation and worker HTTP tests.

The ordinary worker test substitutes ONLY the ONNX session/checker. The optional
real-ONNX test uses the generated Identity fixture, not a trained classifier.
"""
import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from registry.codec import MAX_OBJECTS, encode_results
from registry.packages import extract_package
from registry.schema import Manifest, function_metadata
from registry.sdk import Box, Mask, ModelContext, Tag
from registry.wire import validate_wire


@pytest.fixture
def classification():
    # Sparse IDs distinguish the class ID from the probability-vector index.
    return Manifest(name="classification", weights=["model.onnx"], labels=[
        {"id": 7, "name": "usable", "type": "tag"},
        {"id": 42, "name": "low_quality", "type": "tag"},
    ])


def encode(items, manifest):
    result = encode_results(items, manifest, (20, 24, 3))
    # Exercise independent validation outside the model worker as well.
    assert validate_wire(result, manifest, 24, 20) is result
    return result


@pytest.mark.parametrize("count", [0, 1, 2])
def test_zero_single_and_multi_label_without_coordinates(classification, count):
    expected = [Tag(7, 0.9), Tag(42, 0.7)][:count]
    result = encode(expected, classification)
    assert len(result) == count
    assert all(set(item) == {"type", "label", "confidence"} and item["type"] == "tag" for item in result)
    assert [item["label"] for item in result] == ["usable", "low_quality"][:count]


def test_numpy_scalar_values_and_tuple_results(classification):
    result = encode((Tag(np.int64(42), np.float32(0.5)),), classification)
    assert result == [{"label": "low_quality", "confidence": 0.5, "type": "tag"}]
    assert type(result[0]["confidence"]) is float


@pytest.mark.parametrize("score", [0.0, 1.0, 0.0001])
def test_scores_not_implicitly_threshold_filtered(classification, score):
    assert encode([Tag(7, score)], classification)[0]["confidence"] == score


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.01, 1.01, True, np.bool_(False), "0.8", None])
def test_invalid_scores_rejected(classification, score):
    with pytest.raises(ValueError, match="score"):
        encode_results([Tag(7, score)], classification, (20, 24, 3))


@pytest.mark.parametrize("class_id", [True, np.bool_(True), 7.0, "7", None, -1, 0, 8])
def test_invalid_or_undeclared_ids_rejected(classification, class_id):
    with pytest.raises(ValueError, match="class_id"):
        encode_results([Tag(class_id, .9)], classification, (20, 24, 3))


def test_duplicate_class_rejected_in_worker_and_control_plane(classification):
    with pytest.raises(ValueError, match="duplicate"):
        encode_results([Tag(7, .5), Tag(7, .8)], classification, (20, 24, 3))
    result = encode([Tag(7, .5)], classification)
    with pytest.raises(ValueError, match="duplicate"):
        validate_wire(result * 2, classification, 24, 20)


@pytest.mark.parametrize("kind", ["rectangle", "polygon"])
def test_tag_cannot_refer_to_spatial_label(kind):
    manifest = Manifest(name="shape", weights=["model.onnx"], labels=[{"id": 7, "name": "shape", "type": kind}])
    with pytest.raises(ValueError, match="tag label"):
        encode_results([Tag(7, .9)], manifest, (20, 24, 3))


def test_spatial_objects_cannot_refer_to_tag(classification):
    for item in [Box(7, .8, (0, 0, 5, 5)), Mask(7, .9, np.ones((20, 24), bool))]:
        with pytest.raises(ValueError, match="label"):
            encode_results([item], classification, (20, 24, 3))


def test_classification_requires_explicit_tag_objects(classification):
    for result in [[.9, .1], [{"class_id": 7, "score": .9}], np.array([.9, .1])]:
        with pytest.raises((ValueError, TypeError)):
            encode_results(result, classification, (20, 24, 3))


@pytest.mark.parametrize("change", [
    {"points": []}, {"points": [0, 0, 24, 20]}, {"frame": 8}, {"attributes": []},
    {"label": "unknown"}, {"label": []}, {"type": "rectangle"}, {"type": "classification"},
    {"confidence": True}, {"confidence": float("nan")}, {"confidence": "0.9"},
])
def test_control_plane_rejects_forged_tag_output(classification, change):
    tag = {"label": "usable", "type": "tag", "confidence": .9}
    with pytest.raises(ValueError):
        validate_wire([{**tag, **change}], classification, 24, 20)


@pytest.mark.parametrize("field", ["type", "label", "confidence"])
def test_missing_wire_fields_rejected(classification, field):
    tag = {"label": "usable", "type": "tag", "confidence": .9}
    del tag[field]
    with pytest.raises(ValueError):
        validate_wire([tag], classification, 24, 20)


def test_output_count_limit(classification):
    with pytest.raises(ValueError, match="2000"):
        encode_results([Tag(7, .8)] * (MAX_OBJECTS + 1), classification, (20, 24, 3))
    with pytest.raises(ValueError, match="count"):
        validate_wire([{}] * (MAX_OBJECTS + 1), classification, 24, 20)


def test_metadata_uses_existing_detector_protocol_with_tag_labels(classification):
    meta = function_metadata("a" * 20, "b" * 16, classification.model_dump(), "nuclio")
    assert meta["metadata"]["annotations"]["type"] == "detector"
    assert json.loads(meta["metadata"]["annotations"]["spec"]) == [label.model_dump() for label in classification.labels]
    assert classification.schema_version == 1


def test_existing_detection_segmentation_and_legacy_mixed_outputs():
    manifest = Manifest(name="mixed", weights=["model.onnx"], labels=[
        {"id": 0, "name": "region", "type": "mask"},
        {"id": 8, "name": "object", "type": "rectangle"},
        {"id": 42, "name": "usable", "type": "tag"},
    ])
    result = encode([Mask(0, .9, np.ones((20, 24), np.uint8)), Box(8, .8, (0, 0, 24, 20)), Tag(42, .7)], manifest)
    assert [item["type"] for item in result] == ["polygon", "rectangle", "tag"]
    assert result[1]["points"] == [0., 0., 24., 20.]
    polygon_manifest = Manifest(name="segmentation", weights=["model.onnx"], labels=[{"id": 0, "name": "region", "type": "polygon"}])
    assert encode(np.ones((1, 20, 24), np.uint8), polygon_manifest)[0]["type"] == "polygon"


def test_classification_example_is_a_valid_upload_package(tmp_path):
    manifest, digest = extract_package(ROOT / "examples/classification-demo.zip", tmp_path / "candidate")
    assert [label.type for label in manifest.labels] == ["tag", "tag"]
    assert len(digest) == 64
    assert (tmp_path / "candidate/model.onnx").read_bytes() == (ROOT / "examples/classification/model.onnx").read_bytes()


def image_payload(value):
    buffer = io.BytesIO()
    Image.new("RGB", (24, 20), (value, value, value)).save(buffer, format="PNG")
    return {"image": base64.b64encode(buffer.getvalue()).decode()}


def check_worker(monkeypatch):
    from registry.worker import create_worker
    # The worker intentionally loads a module named uploaded_model. Restore the
    # test process after its lifespan to avoid leaking state into other tests.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "uploaded_model", None)
    monkeypatch.setenv("MR_PROVIDERS", "CPUExecutionProvider")
    with TestClient(create_worker(ROOT / "examples/classification")) as client:
        for value, label in [(0, "dark_image"), (255, "bright_image")]:
            response = client.post("/predict", json=image_payload(value), headers={"x-request-id": "classification-test"})
            assert response.status_code == 200, response.text
            assert response.json() == [{"type": "tag", "label": label, "confidence": 1.0}]
        assert client.get("/health").json() == {"ready": True}


def test_worker_http_with_identity_session_double(monkeypatch):
    import registry.worker as worker
    class IdentitySession:
        def get_inputs(self):
            return [SimpleNamespace(name="image")]
        def run(self, outputs, inputs):
            return [inputs["image"]]
    monkeypatch.setattr(worker, "check_onnx_files", lambda *args: None)
    monkeypatch.setattr(ModelContext, "create_session", lambda *args: IdentitySession())
    check_worker(monkeypatch)


def test_real_onnx_classification_worker(monkeypatch):
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    check_worker(monkeypatch)
