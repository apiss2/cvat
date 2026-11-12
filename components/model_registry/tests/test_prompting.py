# SPDX-License-Identifier: MIT
import json
import sys
import time

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, headers, upload
from registry.codec import MAX_MASK_VALUES, encode_interactor
from registry.gateway import create_gateway
from registry.partial_updates import merge_manifest
from registry.schema import InvokeRequest, Manifest, function_id, function_metadata, sample_payload
from registry.wire import validate_wire


@pytest.fixture
def manifest():
    return Manifest.model_validate_json((ROOT / "examples/prompting/manifest.json").read_text())


def test_metadata_and_old_models(manifest):
    annotations = function_metadata("a" * 20, "b" * 16, manifest.model_dump(), "nuclio")["metadata"]["annotations"]
    assert annotations["type"] == "interactor"
    assert annotations["startswith_box"] is False
    assert annotations["startswith_box_optional"] is True
    assert annotations["box_satisfies_min_pos_points"] is True
    assert annotations["min_pos_points"] == "1" and annotations["min_neg_points"] == "0"
    legacy = Manifest(name="legacy", weights=["model.onnx"], labels=[{"id": 0, "name": "object", "type": "polygon"}])
    assert legacy.kind == "detector" and legacy.interactor is None
    assert function_metadata("a" * 20, "b" * 16, legacy.model_dump(), "nuclio")["metadata"]["annotations"]["type"] == "detector"


@pytest.mark.parametrize("patch", [
    {"interactor": None}, {"labels": []},
    {"labels": [{"id": 0, "name": "tag", "type": "tag"}]},
    {"interactor": {"sample": {"pos_points": [[float("nan"), 1]], "neg_points": []}}},
    {"interactor": {"startswith_box": True, "startswith_box_optional": True}},
    {"interactor": {"min_pos_points": 0}},
    {"kind": "tracker"}, {"kind": "detector"},
])
def test_invalid_manifests(manifest, patch):
    with pytest.raises(ValueError):
        merge_manifest(manifest.model_dump(), patch)


def test_partial_settings_preserve_sample_and_weights(manifest):
    updated = merge_manifest(manifest.model_dump(), {"interactor": {"min_neg_points": -1}})
    assert updated.interactor.sample == manifest.interactor.sample
    assert updated.weights == manifest.weights
    detector = merge_manifest(manifest.model_dump(), {"kind": "detector", "interactor": None})
    assert detector.kind == "detector"


@pytest.mark.parametrize("prompts", [
    {"pos_points": []}, {"neg_points": []},
    {"pos_points": [], "neg_points": []},
    {"pos_points": [[-1, 0]], "neg_points": []},
    {"pos_points": [[64, 0]], "neg_points": []},
    {"pos_points": [[0, 48]], "neg_points": []},
    {"pos_points": [[True, 1]], "neg_points": []},
    {"pos_points": [[1, 2, 3]], "neg_points": []},
    {"pos_points": [[float("inf"), 1]], "neg_points": []},
    {"pos_points": [[1, 1]] * 1001, "neg_points": []},
    {"pos_points": [], "neg_points": [], "obj_bbox": [[20, 10], [10, 20]]},
    {"pos_points": [], "neg_points": [], "obj_bbox": [[0, 0], [65, 48]]},
    {"pos_points": [[1, 1]], "neg_points": [], "text_prompts": ["object"]},
])
def test_invalid_requests(manifest, prompts):
    with pytest.raises(ValueError):
        InvokeRequest.model_validate({"image": "fixture", **prompts}).prompts_for(manifest, 64, 48)


def test_point_box_requirements_and_image_edges(manifest):
    body = InvokeRequest(image="fixture", pos_points=[], neg_points=[], obj_bbox=[[0, 0], [64, 48]])
    assert body.prompts_for(manifest, 64, 48).obj_bbox == [[0, 0], [64, 48]]
    required = merge_manifest(manifest.model_dump(), {"interactor": {"startswith_box": True, "startswith_box_optional": False}})
    with pytest.raises(ValueError, match="requires a box"):
        InvokeRequest.model_validate(sample_payload(manifest, "fixture")).prompts_for(required, 64, 48)
    disabled = merge_manifest(manifest.model_dump(), {"interactor": {"startswith_box_optional": False}})
    with pytest.raises(ValueError, match="does not accept a box"):
        body.prompts_for(disabled, 64, 48)
    no_negative = merge_manifest(manifest.model_dump(), {"interactor": {"min_neg_points": -1}})
    with pytest.raises(ValueError, match="negative"):
        InvokeRequest(image="fixture", pos_points=[[1, 1]], neg_points=[[2, 2]]).prompts_for(no_negative, 64, 48)
    minimum = merge_manifest(manifest.model_dump(), {"interactor": {"min_neg_points": 1}})
    with pytest.raises(ValueError, match="not enough negative"):
        body.prompts_for(minimum, 64, 48)


def test_cvat_ui_empty_box_means_no_box(manifest):
    # CVAT's AI tools send obj_bbox=[] when only points are placed.
    body = InvokeRequest.model_validate({"image": "fixture", "pos_points": [[14, 10]], "neg_points": [], "obj_bbox": []})
    assert body.prompts_for(manifest, 64, 48).obj_bbox is None
    assert "obj_bbox" not in body.model_dump(exclude_none=True)
    required = merge_manifest(manifest.model_dump(), {"interactor": {"startswith_box": True, "startswith_box_optional": False}})
    with pytest.raises(ValueError, match="requires a box"):
        body.prompts_for(required, 64, 48)


def test_binary_mask_retains_holes_disconnected_regions_and_empty(manifest):
    bitmap = np.zeros((48, 64), dtype=np.uint8)
    bitmap[2:12, 3:15] = 1
    bitmap[5:8, 6:9] = 0
    bitmap[30:35, 40:45] = 1
    result = encode_interactor(bitmap, (48, 64, 3))
    assert validate_wire(result, manifest, 64, 48) == result
    np.testing.assert_array_equal(result["mask"], bitmap)
    assert encode_interactor(np.zeros_like(bitmap), (48, 64, 3)) == {"shapes": []}
    assert validate_wire({"shapes": []}, manifest, 64, 48) == {"shapes": []}


@pytest.mark.parametrize("result", [
    np.zeros((1, 48, 64), dtype=np.uint8), np.zeros((48, 64), dtype=np.float32),
    np.full((48, 64), 255, dtype=np.uint8), np.zeros((47, 64), dtype=np.uint8),
    [],
])
def test_worker_rejects_invalid_masks(result):
    with pytest.raises(ValueError):
        encode_interactor(result, (48, 64, 3))


@pytest.mark.parametrize("result", [
    [], {"shapes": [{}]}, {"mask": [], "custom": 1}, {"mask": [[0]]},
    {"mask": [[0.0] * 64 for _ in range(48)]},
    {"mask": [[255] * 64 for _ in range(48)]},
])
def test_control_plane_rejects_untrusted_masks(manifest, result):
    with pytest.raises(ValueError):
        validate_wire(result, manifest, 64, 48)


def test_mask_budget_is_checked_before_output_allocation(manifest):
    with pytest.raises(ValueError, match="budget"):
        encode_interactor(None, (1, MAX_MASK_VALUES + 1, 3))
    with pytest.raises(ValueError, match="budget"):
        validate_wire({"mask": []}, manifest, MAX_MASK_VALUES + 1, 1)


def test_real_onnx_worker_points_negative_box_empty_and_roi_local(monkeypatch, manifest, sample):
    from registry.worker import create_worker
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "uploaded_model", None)
    monkeypatch.setenv("MR_PROVIDERS", "CPUExecutionProvider")
    with TestClient(create_worker(ROOT / "examples/prompting")) as client:
        payload = sample_payload(manifest, sample)
        result = client.post("/predict", json=payload)
        assert result.status_code == 200, result.text
        mask = np.asarray(result.json()["mask"])
        assert mask.sum() == 32 * 24 - 8 * 8
        assert mask[10, 14] == 1 and mask[18, 22] == 0
        payload["neg_points"] = [[15, 11]]
        assert client.post("/predict", json=payload).json() == {"shapes": []}
        payload.update(pos_points=[], neg_points=[], obj_bbox=[[12, 8], [20, 16]])
        response = client.post("/predict", json=payload)
        assert response.status_code == 200, response.text
        assert np.asarray(response.json()["mask"]).sum() == 64
        from registry.codec import decode_image
        from PIL import Image
        import base64
        import io
        buffer = io.BytesIO()
        Image.fromarray(decode_image(sample)[8:32, 12:44]).save(buffer, format="PNG")
        response = client.post("/predict", json={
            "image": base64.b64encode(buffer.getvalue()).decode(),
            "pos_points": [[2, 2]], "neg_points": [],
        })
        assert response.status_code == 200, response.text
        assert np.asarray(response.json()["mask"]).shape == (24, 32)
        response = client.post("/predict", json={**sample_payload(manifest, sample), "obj_bbox": []})
        assert response.status_code == 200, response.text
        assert np.asarray(response.json()["mask"]).sum() == 32 * 24 - 8 * 8
        for bad in ({"image": sample}, {**sample_payload(manifest, sample), "pos_points": [[64, 0]]}):
            assert client.post("/predict", json=bad).status_code == 422


def wait_operation(ctx, operation):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        operation = ctx["client"].get("/api/operations/" + operation["id"], headers=headers()).json()
        if operation["status"] in ("succeeded", "failed"):
            return operation
        time.sleep(.01)
    pytest.fail(f"operation did not finish: {operation}")


def test_registration_test_update_old_revision_and_rollback(ctx, sample, manifest):
    operation = upload(ctx, "prompting")
    assert operation["status"] == "succeeded", operation
    mid, old = operation["model_id"], operation["revision"]
    assert ctx["runtime"].calls[-1][2] == sample_payload(manifest, sample)
    c = ctx["client"]
    fid = function_id(mid, old)
    assert c.get("/internal/functions/" + fid, headers=headers("service")).json()["metadata"]["annotations"]["type"] == "interactor"
    payload = {"image": sample, "pos_points": [], "neg_points": [], "obj_bbox": [[0, 0], [64, 48]]}
    response = c.post("/internal/functions/" + fid + "/invoke", headers=headers("service"), json=payload)
    assert response.status_code == 200 and "mask" in response.json()
    assert ctx["runtime"].calls[-1][2] == payload
    assert c.post(f"/api/models/{mid}/test", headers=headers("bob"), json=payload).status_code == 403
    assert c.post(f"/api/models/{mid}/test", headers=headers(), json=payload).status_code == 200
    before = len(ctx["runtime"].calls)
    assert c.post(f"/api/models/{mid}/test", headers=headers(), json={"image": sample}).status_code == 422
    assert len(ctx["runtime"].calls) == before
    update = c.post(f"/api/models/{mid}/update", headers=headers(), data={
        "expected_revision": old, "manifest": json.dumps({"interactor": {"sample": {"pos_points": [[16, 12]], "neg_points": []}}}),
    })
    assert update.status_code == 202, update.text
    new = wait_operation(ctx, update.json())
    assert new["status"] == "succeeded", new
    assert ctx["runtime"].calls[-1][2]["pos_points"] == [[16, 12]]
    ctx["runtime"].fail_next = True
    failed = c.post(f"/api/models/{mid}/update", headers=headers(), data={
        "expected_revision": new["revision"], "manifest": '{"description":"failed candidate"}',
    })
    assert wait_operation(ctx, failed.json())["status"] == "failed"
    assert c.get(f"/api/models/{mid}", headers=headers()).json()["active_revision"] == new["revision"]
    assert c.post("/internal/functions/" + fid + "/invoke", headers=headers("service"), json=sample_payload(manifest, sample)).status_code == 200
    response = c.post(f"/api/models/{mid}/rollback", headers=headers(), json={"expected_revision": new["revision"], "revision": old})
    assert response.status_code == 200, response.text
    assert ctx["runtime"].calls[-1][2]["pos_points"] == [[14, 10]]


def test_invalid_sample_is_not_published_or_executed(ctx):
    operation = upload(ctx, "prompting")
    mid, old = operation["model_id"], operation["revision"]
    before = len(ctx["runtime"].calls)
    response = ctx["client"].post(f"/api/models/{mid}/update", headers=headers(), data={
        "expected_revision": old, "manifest": '{"interactor":{"sample":{"pos_points":[[1000,1000]],"neg_points":[]}}}',
    })
    failed = wait_operation(ctx, response.json())
    assert failed["status"] == "failed"
    assert len(ctx["runtime"].calls) == before
    assert ctx["service"].store.model(mid)["active_revision"] == old


def test_gateway_passes_prompts_and_mask_unchanged(tmp_path, manifest, sample):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"mask": [[0, 1], [1, 0]]})
    token = tmp_path / "token"
    token.write_text("s" * 48)
    gateway = create_gateway(
        token_file=token, registry_client=httpx.Client(base_url="http://registry", transport=httpx.MockTransport(handler)),
        nuclio_client=httpx.Client(base_url="http://nuclio", transport=httpx.MockTransport(handler)),
    )
    payload = sample_payload(manifest, sample)
    payload["obj_bbox"] = [[0, 0], [64, 48]]
    with TestClient(gateway) as client:
        response = client.post("/api/function_invocations", json=payload, headers={"x-nuclio-function-name": function_id("a" * 20, "b" * 16)})
        assert response.status_code == 200 and response.json() == {"mask": [[0, 1], [1, 0]]}
        assert json.loads(calls[-1].content) == payload
