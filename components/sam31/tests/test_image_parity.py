# SPDX-License-Identifier: MIT
"""CPU protocol/packaging regression tests. No GPU, real model or Docker required.

Image inference uses a deterministic torch model double at the private SAM3.1
track_step boundary. These tests do not establish real-checkpoint compatibility.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
from PIL import Image
import pytest
import torch

ROOT = Path(__file__).resolve().parents[3]
SHARED = ROOT / "serverless/pytorch/facebookresearch/sam2/nuclio"
RUNTIME = ROOT / "components/sam31/nuclio"


def load_module(name, path, monkeypatch, *, replace=False):
    module = ModuleType(name)
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, name, module)
    text = path.read_text()
    if replace:
        text = text.replace("SAM2", "SAM31").replace("sam2", "sam31")
    exec(compile(text, str(path), "exec"), module.__dict__)
    return module


@pytest.fixture
def modules(monkeypatch):
    protocol = load_module("protocol", SHARED / "protocol.py", monkeypatch, replace=True)
    geometry = load_module("geometry", SHARED / "geometry.py", monkeypatch, replace=True)
    temporal = load_module("temporal_video", RUNTIME / "temporal_video.py", monkeypatch)
    image = load_module("image_model", RUNTIME / "image_model.py", monkeypatch)
    codec = load_module("state_codec", RUNTIME / "state_codec.py", monkeypatch)
    return SimpleNamespace(protocol=protocol, geometry=geometry, temporal=temporal, image=image, codec=codec)


class ModelDouble(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.zeros(1))
        self.image_size = 32
        self.use_memory_selection = False
        self.memory_temporal_stride_for_eval = 1
        self.use_obj_ptrs_in_encoder = True
        self.save_image_features = True
        self.trim_past_non_cond_mem_for_eval = False
        self.offload_output_to_cpu_for_eval = False
        self.num_maskmem = 7
        self.max_obj_ptrs_in_encoder = 16
        self.calls = []
        self.multiplex_calls = []
        self.multiplex_controller = SimpleNamespace(get_state=self.get_state)
        self.logits = torch.ones(1, 1, 4, 8)
        self.eval()

    def get_state(self, **kwargs):
        self.multiplex_calls.append(kwargs)
        return object()

    def track_step(self, **kwargs):
        self.calls.append(kwargs)
        return {"pred_masks": self.logits}


@pytest.fixture
def interactor(modules):
    model = ModelDouble()
    op = modules.image.ImageInteractor(model)
    feature_calls = []

    def features(image):
        feature_calls.append(image.size)
        return torch.zeros(1, 3, 32, 32), {"interactive": object(), "sam2_backbone_out": object()}

    op.video._features = features
    return op, model, feature_calls


def request(size=(8, 4), **prompts):
    stream = io.BytesIO()
    Image.new("RGB", size).save(stream, format="PNG")
    return {"image": base64.b64encode(stream.getvalue()).decode(), **prompts}


def decode_mask(shape, width=8, height=4):
    values = shape["points"]
    x0, y0, x1, y1 = values[-4:]
    flat = np.concatenate([np.full(n, i % 2, dtype=bool) for i, n in enumerate(values[:-4])])
    assert len(flat) == (x1 - x0 + 1) * (y1 - y0 + 1)
    result = np.zeros((height, width), dtype=bool)
    result[y0:y1 + 1, x0:x1 + 1] = flat.reshape(y1 - y0 + 1, x1 - x0 + 1)
    return result


def test_image_point_box_scaling_and_fresh_memory(interactor):
    op, model, features = interactor
    result = op(request(pos_points=[[2, 1]], neg_points=[[7, 3]], obj_bbox=[[0, 0], [8, 4]]))
    call = model.calls[-1]
    torch.testing.assert_close(call["point_inputs"]["point_coords"],
                               torch.tensor([[[0., 0.], [32., 32.], [8., 8.], [28., 24.]]]))
    assert call["point_inputs"]["point_labels"].tolist() == [[2, 3, 1, 0]]
    assert call["is_init_cond_frame"] is True
    assert call["run_mem_encoder"] is False
    assert call["num_frames"] == 1 and call["frame_idx"] == 0
    assert call["output_dict"] == {"cond_frame_outputs": {}, "non_cond_frame_outputs": {}}
    assert call["prev_sam_mask_logits"] is None
    assert call["objects_to_interact"] == [0]
    assert model.multiplex_calls[-1]["object_ids"] == [0]
    assert features == [(8, 4)]
    assert decode_mask(result["shapes"][0]).all()


@pytest.mark.parametrize("prompts", [
    {"pos_points": [[1, 1]]}, {"obj_bbox": [[0, 0], [8, 4]]},
    {"pos_points": [[1, 1]], "neg_points": [[5, 2]]},
    {"obj_bbox": [[0, 0], [8, 4]], "neg_points": [[5, 2]]},
])
def test_supported_prompts(interactor, prompts):
    op, model, _ = interactor
    assert len(op(request(**prompts))["shapes"]) == 1
    assert len(model.calls) == 1


@pytest.mark.parametrize("prompts", [
    {}, {"neg_points": [[1, 1]]}, {"pos_points": [[8, 0]]},
    {"pos_points": [[0, 4]]}, {"pos_points": [[-1, 0]]},
    {"pos_points": [[float("nan"), 1]]}, {"pos_points": [[float("inf"), 1]]},
    {"pos_points": [1, 2]}, {"pos_points": [[1, 1]], "obj_bbox": [[1, 1]]},
    {"obj_bbox": [[3, 1], [3, 2]]}, {"obj_bbox": [[7, 1], [4, 2]]},
    {"obj_bbox": [[0, 0], [9, 4]]}, {"pos_points": [[0, 0]] * 10001},
])
def test_invalid_prompts_do_not_run_model(interactor, modules, prompts):
    op, model, features = interactor
    with pytest.raises(modules.protocol.ProtocolError):
        op(request(**prompts))
    assert model.calls == [] and features == []


def test_only_image_embedding_is_reused(interactor):
    op, model, features = interactor
    op(request(pos_points=[[1, 1]]))
    model.calls[0]["output_dict"]["cond_frame_outputs"][9] = "must not leak"
    op(request(pos_points=[[3, 2]], neg_points=[[1, 1]]))
    assert features == [(8, 4)]
    assert model.calls[1]["point_inputs"]["point_labels"].tolist() == [[1, 0]]
    assert model.calls[1]["output_dict"] == {"cond_frame_outputs": {}, "non_cond_frame_outputs": {}}
    assert model.calls[0]["multiplex_state"] is not model.calls[1]["multiplex_state"]


def test_identical_pixels_different_dimensions_do_not_share_embedding(interactor):
    op, _, features = interactor
    op(request((8, 4), pos_points=[[1, 1]]))
    op(request((4, 8), pos_points=[[1, 1]]))
    assert features == [(8, 4), (4, 8)]


def test_failed_embedding_is_not_cached(interactor):
    op, _, _ = interactor
    op(request(pos_points=[[1, 1]]))
    original = op.video._features
    op.video._features = lambda image: (_ for _ in ()).throw(RuntimeError("encoder"))
    with pytest.raises(RuntimeError, match="encoder"):
        op(request((4, 8), pos_points=[[1, 1]]))
    assert op.features is None and op.image_key is None
    op.video._features = original
    assert op(request((4, 8), pos_points=[[1, 1]]))["shapes"]


@pytest.mark.parametrize("logits", [torch.full((1, 1, 4, 8), float("nan")),
    torch.full((1, 1, 4, 8), float("inf")), torch.ones(2, 1, 4, 8),
    torch.ones(1, 2, 4, 8), torch.ones(1, 4, 8), torch.ones(1, 1, 0, 8), None])
def test_invalid_logits_are_rejected(interactor, logits):
    op, model, _ = interactor
    model.logits = logits
    with pytest.raises(RuntimeError, match="invalid image mask"):
        op(request(pos_points=[[1, 1]]))


def test_empty_result(interactor):
    op, model, _ = interactor
    model.logits.fill_(-1)
    assert op(request(pos_points=[[1, 1]])) == {"shapes": []}


def test_full_mask_preserves_holes_and_disconnected_regions(interactor):
    op, model, _ = interactor
    mask = np.zeros((4, 8), dtype=bool)
    mask[0:3, 0:3] = True
    mask[1, 1] = False
    mask[3, 7] = True
    model.logits = torch.tensor(np.where(mask, 1., -1.), dtype=torch.float32)[None, None]
    result = op(request(pos_points=[[0, 0]]))
    np.testing.assert_array_equal(decode_mask(result["shapes"][0]), mask)


def snapshot(modules, count=1, dtype=torch.float32, index=0):
    fields = {"obj_ptr": torch.ones(1, 16, 2, dtype=dtype),
              "maskmem_features": torch.ones(1, 2, 2, 2, dtype=dtype),
              "maskmem_pos_enc": [torch.ones(1, 2, 2, 2, dtype=dtype)],
              "image_features": torch.ones(4, 1, 2, dtype=dtype),
              "image_pos_enc": torch.ones(4, 1, 2, dtype=dtype)}
    return modules.temporal.Snapshot(index, 8, 4, count,
        {"cond_frame_outputs": {0: fields}, "non_cond_frame_outputs": {index: fields} if index else {}})


@pytest.mark.parametrize("count", [1, 4, 16])
@pytest.mark.parametrize("dtype", [torch.float32, torch.float16, torch.bfloat16])
def test_state_round_trip(modules, count, dtype):
    codec = modules.codec.StateCodec("a" * 64)
    original = snapshot(modules, count, dtype, 1)
    restored = codec.decode(codec.encode(original))
    assert restored.count == count and restored.index == 1
    torch.testing.assert_close(restored.outputs["cond_frame_outputs"][0]["obj_ptr"],
                               original.outputs["cond_frame_outputs"][0]["obj_ptr"])


def test_state_oversize_rejected_before_any_tensor_copy(modules, monkeypatch):
    codec = modules.codec.StateCodec("a" * 64, max_bytes=1024)
    state = snapshot(modules)
    state.outputs["cond_frame_outputs"][0]["image_pos_enc"] = torch.ones(100, 1, 10)
    def fail_copy(*args, **kwargs):
        pytest.fail("CPU transfer or clone happened before the size check")
    monkeypatch.setattr(torch.Tensor, "cpu", fail_copy)
    monkeypatch.setattr(torch.Tensor, "clone", fail_copy)
    with pytest.raises(modules.protocol.ProtocolError) as error:
        codec.encode(state)
    assert error.value.status == 413


def test_state_identity_mismatch_rejected(modules):
    data = modules.codec.StateCodec("a" * 64).encode(snapshot(modules))
    with pytest.raises(modules.protocol.ProtocolError) as error:
        modules.codec.StateCodec("b" * 64).decode(data)
    assert error.value.status == 409


@pytest.fixture
def deploy_module(monkeypatch):
    return load_module("sam31_parity_deploy", ROOT / "components/sam31/deploy.py", monkeypatch)


@pytest.fixture
def deployment(deploy_module, tmp_path):
    # Minimal explicit source/build fixtures: these are not a real CVAT checkout.
    root = tmp_path / "checkout"
    nuclio = root / "components/sam31/nuclio"
    nuclio.mkdir(parents=True)
    for name in ("main.py", "image_main.py", "image_model.py"):
        (nuclio / name).write_text("# reviewed test source\n")
    shared = root / deploy_module.SHARED
    shared.mkdir(parents=True)
    for name in ("protocol.py", "geometry.py", "redis_store.py"):
        (shared / name).write_text("# SAM2 sam2 fixture\n")
    template = {"metadata": {"name": deploy_module.FUNCTION, "annotations": {"type": "tracker"}},
        "spec": {"build": {"image": "placeholder"}, "handler": "main:handler", "env": [],
                 "volumes": [{"legacy": True}]}}
    (root / "components/sam31/function-gpu.json").write_text(json.dumps(template))
    (root / "components/sam31/deploy.py").write_text("# fixture\n")
    (root / "components/extensions").mkdir()
    (root / "components/extensions/manage.py").write_text("# fixture\n")
    checkpoint = tmp_path / "approved.pt"
    checkpoint.write_bytes(b"approved checkpoint fixture; not real weights")
    values = {"SAM31_CHECKPOINT_HOST": str(checkpoint),
        "SAM31_REDIS_PASSWORD": "secret_" * 8,
        "NUCLIO_NAMESPACE": "nuclio", "CVAT_NETWORK_NAME": "test_cvat"}
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    calls, definitions = [], []
    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ["nuctl", "get", "projects"]:
            return '{"project": {"meta": {"name": "cvat"}}}'
        if command[:2] == ["nuctl", "deploy"]:
            source = Path(command[command.index("--path") + 1])
            config = Path(command[command.index("--file") + 1])
            assert config.stat().st_mode & 0o077 == 0
            assert source not in config.parents
            definition = json.loads(config.read_text())
            assert "volumes" not in definition["spec"]
            assert all(values["SAM31_REDIS_PASSWORD"].encode() not in p.read_bytes() for p in source.iterdir())
            definitions.append(definition)
            weight = source / Path(deploy_module.CHECKPOINT).name
            if "--run-image" in command:
                assert "--no-pull" in command and not weight.exists()
            else:
                assert weight.read_bytes() == checkpoint.read_bytes()
                assert weight.stat().st_mode & 0o222 == 0
        return "sha256:fixture" if command[0] == "docker" else ""
    manager = SimpleNamespace(root=root, state=state, values=values, run=run,
        nuctl_check=lambda model: "1.16.3", functions=lambda: [],
        guard_function_ownership=lambda names: calls.append(["owner", *names]),
        function_environment=lambda: {name: deploy_module.runtime_environment(values, name) for name in deploy_module.FUNCTIONS},
        verify_functions=lambda names, **kwargs: calls.append(["verify", *names]))
    return manager, calls, definitions, checkpoint


@pytest.mark.parametrize("mode,count", [("all", 2), ("image", 1), ("tracker", 1)])
@pytest.mark.parametrize("no_build", [False, True])
def test_deployment_modes_and_private_staging(deploy_module, deployment, mode, count, no_build):
    manager, calls, definitions, checkpoint = deployment
    if no_build:
        checkpoint.unlink()
    deploy_module.deploy(manager, {}, mode=mode, no_build=no_build)
    assert len(definitions) == count
    assert not list(manager.state.iterdir())
    assert calls[-1][0] == "verify"
    for definition in definitions:
        name = definition["metadata"]["name"]
        env = {e["name"]: e["value"] for e in definition["spec"]["env"]}
        assert env["SAM31_CHECKPOINT"] == deploy_module.CHECKPOINT
        assert env["HF_HUB_OFFLINE"] == "1"
        if name == deploy_module.IMAGE_FUNCTION:
            assert definition["metadata"]["annotations"]["type"] == "interactor"
            assert definition["spec"]["handler"] == "image_main:handler"
            assert not any(key.startswith("SAM31_REDIS_") for key in env)
        else:
            assert definition["metadata"]["annotations"]["type"] == "tracker"
            assert env["SAM31_REDIS_PASSWORD"] == manager.values["SAM31_REDIS_PASSWORD"]


def test_missing_no_build_image_prevents_any_nuctl_deploy(deploy_module, deployment):
    manager, calls, definitions, _ = deployment
    def run(command, **kwargs):
        calls.append(command)
        raise RuntimeError("missing image")
    manager.run = run
    with pytest.raises(RuntimeError, match="missing image"):
        deploy_module.deploy(manager, {}, no_build=True)
    assert definitions == [] and not any(c[:2] == ["nuctl", "deploy"] for c in calls)


def test_missing_source_fails_before_external_commands(deploy_module, deployment):
    manager, calls, _, checkpoint = deployment
    checkpoint.unlink()
    with pytest.raises(ValueError, match="existing checkpoint"):
        deploy_module.deploy(manager, {})
    assert calls == []


def test_staging_copies_replacement_without_expected_digest(deploy_module, deployment, tmp_path):
    manager, _, _, checkpoint = deployment
    checkpoint.write_bytes(b"replacement checkpoint")
    destination = tmp_path / "staging"
    deploy_module.stage_sources(destination, manager.root, checkpoint=checkpoint)
    assert (destination / Path(deploy_module.CHECKPOINT).name).read_bytes() == b"replacement checkpoint"


def test_fingerprint_includes_model_selection_source_and_nuclio_version(deploy_module, deployment):
    manager, _, _, _ = deployment
    def fingerprint(source=None, version="1.16.3", function=None):
        return deploy_module.function_image(version, manager.root,
            checkpoint_source=source or manager.values["SAM31_CHECKPOINT_HOST"],
            function=function or deploy_module.FUNCTION)
    initial = fingerprint()
    assert initial != fingerprint(source="/models/another.pt")
    assert initial != fingerprint(version="1.17.0")
    assert initial != fingerprint(function=deploy_module.IMAGE_FUNCTION)
    (manager.root / deploy_module.SHARED / "geometry.py").write_text("# changed\n")
    assert initial != fingerprint()


@pytest.mark.parametrize("destination", ["/", "/opt", "/opt/nuclio", "/opt/nuclio/sam3.1_multiplex.pt", "/models/sam3.1_multiplex.pt"])
def test_checkpoint_shadow_mount_is_rejected(deploy_module, destination):
    assert not deploy_module.checkpoint_matches({"Mounts": [{"Destination": destination, "RW": False}]})


@pytest.mark.parametrize("mounts", [[], [{"Destination": "/etc/nuclio/config/processor"}], [{"Destination": "/tmp/cache"}]])
def test_unrelated_mounts_allowed(deploy_module, mounts):
    assert deploy_module.checkpoint_matches({"Mounts": mounts})


def test_existing_image_reused_with_exact_env_and_identity(deploy_module, deployment):
    manager, calls, definitions, checkpoint = deployment
    name = deploy_module.FUNCTION
    image = deploy_module.function_image("1.16.3", manager.root,
        checkpoint_source=manager.values["SAM31_CHECKPOINT_HOST"], function=name)
    manager.functions = lambda: [{"Id": "running", "State": {"Running": True}, "Mounts": [],
        "Config": {"Image": image, "Labels": {"nuclio.io/function-name": name},
                   "Env": [f"{k}={v}" for k, v in deploy_module.runtime_environment(manager.values).items()]}}]
    checkpoint.unlink()
    deploy_module.deploy(manager, {}, mode="tracker", no_build=True)
    assert definitions == []
    assert not any(c[:2] == ["nuctl", "deploy"] for c in calls)


def test_load_model_hashes_and_loads_same_file_descriptor(modules, monkeypatch, tmp_path):
    checkpoint = tmp_path / "weights.pt"
    checkpoint.write_bytes(b"approved fixture")
    expected = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    builder = ModuleType("sam3.model_builder")
    fake = SimpleNamespace(state_dict=lambda: {}, load_state_dict=lambda weights, strict: None)
    fake.eval = lambda: fake
    fake.cuda = lambda: fake
    builder.build_sam3_multiplex_video_model = lambda **kwargs: fake
    monkeypatch.setitem(sys.modules, "sam3", ModuleType("sam3"))
    monkeypatch.setitem(sys.modules, "sam3.model_builder", builder)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    reads = []
    def load(stream, **kwargs):
        assert hasattr(stream, "read") and stream.tell() == 0
        assert kwargs == {"map_location": "cpu", "weights_only": True}
        reads.append(stream.read())
        return {}
    monkeypatch.setattr(torch, "load", load)
    model, digest = modules.temporal.load_model(checkpoint)
    assert model is fake and digest == expected and reads == [b"approved fixture"]
    checkpoint.write_bytes(b"replacement fixture")
    _, replacement = modules.temporal.load_model(checkpoint)
    assert replacement != digest and len(reads) == 2
    policy = modules.temporal.MemoryPolicy(6, 15)
    assert modules.temporal.model_identity(digest, policy, False) != modules.temporal.model_identity(replacement, policy, False)


@pytest.mark.parametrize("count", [1, 4, 16])
@pytest.mark.parametrize("index", [0, 39])
def test_gpu_smoke_fixture_has_distinct_in_bounds_seeds(monkeypatch, count, index):
    smoke = load_module("sam31_parity_smoke", ROOT / "components/sam31/tests/gpu_smoke.py", monkeypatch)
    image, masks = smoke.frame(index, count)
    assert image.size == (640, 480) and len(masks) == count
    assert all(mask.shape == (480, 640) and mask.any() for mask in masks)
    assert len({hashlib.sha256(mask.tobytes()).digest() for mask in masks}) == count
    assert np.stack(masks).sum(axis=0).max() == 1


@pytest.mark.parametrize("mode", ["image", "tracker"])
def test_compatibility_entrypoint_preserves_mode_order(monkeypatch, mode):
    import os
    import runpy
    calls = []
    monkeypatch.setattr(sys, "argv", ["deploy.py", "--env-file", "/tmp/deployment.env", mode])
    def execv(executable, arguments):
        calls.append(arguments)
        raise RuntimeError("delegated")
    monkeypatch.setattr(os, "execv", execv)
    with pytest.raises(RuntimeError, match="delegated"):
        runpy.run_path(str(ROOT / "components/sam31/deploy.py"), run_name="__main__")
    assert calls[0][2:] == ["deploy-sam31", "--env-file", "/tmp/deployment.env", mode]


def test_normal_deploy_rebuilds_even_with_matching_running_image(deploy_module, deployment):
    manager, calls, definitions, checkpoint = deployment
    name = deploy_module.FUNCTION
    image = deploy_module.function_image("1.16.3", manager.root,
        checkpoint_source=manager.values["SAM31_CHECKPOINT_HOST"], function=name)
    manager.functions = lambda: [{"Id": "running", "State": {"Running": True}, "Mounts": [],
        "Config": {"Image": image, "Labels": {"nuclio.io/function-name": name},
                   "Env": [f"{k}={v}" for k, v in deploy_module.runtime_environment(manager.values).items()]}}]
    checkpoint.write_bytes(b"new weights at the same path")
    deploy_module.deploy(manager, {}, mode="tracker")
    assert len(definitions) == 1
    assert any(c[:2] == ["nuctl", "deploy"] and "--run-image" not in c for c in calls)
