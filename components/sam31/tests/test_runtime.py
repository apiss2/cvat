# SPDX-License-Identifier: MIT
"""CPU contract tests. The SAM3.1 neural network and real Redis are NOT exercised."""
import base64
import copy
import importlib
import importlib.util
import io
import json
from pathlib import Path
import re
import struct
import sys
import types

import numpy as np
from PIL import Image
import pytest
import torch


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("_sam31_deploy_test", root / "components/sam31/deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    staged = tmp_path_factory.mktemp("sam31-runtime")
    deploy.stage_sources(staged, root)
    names = ("protocol", "geometry", "temporal_video", "state_codec", "redis_store", "tracker")
    previous = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    sys.path.insert(0, str(staged))
    modules = {name: importlib.import_module(name) for name in names}
    yield types.SimpleNamespace(**modules, deploy=deploy, staged=staged)
    sys.path.remove(str(staged))
    for name in names:
        sys.modules.pop(name, None)
    sys.modules.update(previous)


class FakeMultiplex:
    def __init__(self, count): self.count = count


class FakeModel(torch.nn.Module):
    """Contract double, not an accuracy/performance stand-in for the actual network."""
    image_size = 8
    num_maskmem = 7
    max_obj_ptrs_in_encoder = 16
    memory_temporal_stride_for_eval = 1
    use_memory_selection = False
    use_obj_ptrs_in_encoder = True
    save_image_features = True
    trim_past_non_cond_mem_for_eval = False
    offload_output_to_cpu_for_eval = False
    def __init__(self):
        super().__init__()
        self.weight = torch.nn.Parameter(torch.zeros(1))
        self.calls = []
        self.multiplex_controller = types.SimpleNamespace(get_state=self.get_state)
        self.eval()
    def get_state(self, *, num_valid_entries, device, dtype, random, object_ids):
        assert random is False and object_ids == list(range(num_valid_entries))
        return FakeMultiplex(num_valid_entries)
    def track_step(self, **kwargs):
        self.calls.append(kwargs["frame_idx"])
        assert kwargs["point_inputs"] is None and kwargs["track_in_reverse"] is False
        assert kwargs["run_mem_encoder"] is True and kwargs["num_frames"] == kwargs["frame_idx"] + 1
        count, index = kwargs["multiplex_state"].count, kwargs["frame_idx"]
        if index == 0:
            assert kwargs["mask_inputs"].shape == (count, 1, 8, 8)
        else:
            assert kwargs["mask_inputs"] is None
            assert 0 in kwargs["output_dict"]["cond_frame_outputs"]
        logits = torch.full((count, 1, 8, 8), -1.)
        logits[:, :, 2:6, 2:6] = 1
        return {"pred_masks": logits, "obj_ptr": torch.ones(1, 16, 8) * index,
                "maskmem_features": torch.ones(1, 8, 2, 2) * index,
                "maskmem_pos_enc": [torch.ones(1, 8, 2, 2)],
                "image_features": torch.ones(4, 1, 8), "image_pos_enc": torch.ones(4, 1, 8)}


@pytest.fixture
def video(runtime, monkeypatch):
    module = types.ModuleType("sam3.model.multiplex_utils")
    module.MultiplexState = FakeMultiplex
    monkeypatch.setitem(sys.modules, module.__name__, module)
    model = FakeModel()
    video = runtime.temporal_video.TemporalVideo(model)
    video._features = lambda image: (torch.zeros(1, 3, 8, 8), {"interactive": {}, "sam2_backbone_out": {}})
    return video


def initial(video, count=2):
    image = Image.new("RGB", (24, 20))
    masks = [np.ones((20, 24), dtype=bool) for _ in range(count)]
    return image, video.initialize(image, masks)[0]


def test_one_multiplex_step_per_frame_for_four_objects(video):
    image, state = initial(video, 4)
    state, result = video.advance(image, state)
    assert video.model.calls == [0, 1]
    assert state.count == len(result) == 4
    assert all(mask.shape == (20, 24) for mask in result)


def test_pruning_keeps_seed_six_masks_and_fifteen_pointers(runtime, video):
    image, state = initial(video)
    for _ in range(80):
        state, _ = video.advance(image, state)
    recent = state.outputs["non_cond_frame_outputs"]
    assert set(state.outputs["cond_frame_outputs"]) == {0}
    assert set(recent) == set(range(66, 81))
    assert {frame for frame, out in recent.items() if "maskmem_features" in out} == set(range(75, 81))
    assert all(set(recent[frame]) == {"obj_ptr"} for frame in range(66, 75))


def test_codec_roundtrip_can_continue_after_adapter_recreation(runtime, video):
    image, snapshot = initial(video)
    codec = runtime.state_codec.StateCodec("a" * 64)
    for _ in range(35):
        snapshot = codec.decode(codec.encode(snapshot))
        snapshot, _ = video.advance(image, snapshot)
    restored = codec.decode(codec.encode(snapshot))
    assert restored.index == 35 and restored.count == 2
    assert restored.outputs["cond_frame_outputs"][0]["obj_ptr"].device.type == "cpu"
    assert restored.outputs["non_cond_frame_outputs"][35]["image_features"].shape == (4, 1, 8)


def test_advance_does_not_mutate_previous_snapshot(video):
    image, state = initial(video)
    snapshot = copy.deepcopy(state.outputs)
    video.advance(image, state)
    assert state.index == 0 and not state.outputs["non_cond_frame_outputs"]
    assert torch.equal(snapshot["cond_frame_outputs"][0]["obj_ptr"], state.outputs["cond_frame_outputs"][0]["obj_ptr"])


@pytest.mark.parametrize("change", ["use_memory_selection", "save_image_features", "memory_temporal_stride_for_eval",
                                   "trim_past_non_cond_mem_for_eval", "offload_output_to_cpu_for_eval"])
def test_changed_official_memory_contract_rejected(runtime, change):
    model = FakeModel()
    setattr(model, change, 2 if change == "memory_temporal_stride_for_eval" else not getattr(model, change))
    with pytest.raises(RuntimeError, match="memory policy"):
        runtime.temporal_video.TemporalVideo(model)


def test_checkpoint_mapping_loads_only_required_tracking_weights(runtime):
    expected = ["backbone.vision_backbone.trunk.weight", "transformer.weight"]
    checkpoint = {"detector.backbone.vision_backbone.trunk.weight": torch.ones(1),
                  "tracker.model.transformer.weight": torch.ones(2), "detector.text.weight": torch.ones(3)}
    chosen = runtime.temporal_video.tracking_weights(expected, checkpoint)
    assert set(chosen) == set(expected)
    old = {key.replace("detector.", "sam3_model.").replace("tracker.", "sam2_predictor."): value for key, value in checkpoint.items()}
    assert set(runtime.temporal_video.tracking_weights(expected, {"model": old})) == set(expected)
    with pytest.raises(ValueError, match="missing"):
        runtime.temporal_video.tracking_weights(expected, {"some_sam2_weight": torch.ones(1)})


def test_identity_changes_with_weights_precision_or_policy(runtime):
    function = runtime.temporal_video.model_identity
    policy = runtime.temporal_video.MemoryPolicy(6, 15)
    assert len({function("a"*64, policy, False), function("b"*64, policy, False), function("a"*64, policy, True),
                function("a"*64, runtime.temporal_video.MemoryPolicy(5, 15), False)}) == 4


def mutate_header(data, mutation):
    size = struct.unpack("<Q", data[:8])[0]
    header = json.loads(data[8:8+size])
    mutation(header)
    encoded = json.dumps(header, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 8)
    return struct.pack("<Q", len(encoded)) + encoded + data[8+size:]


@pytest.mark.parametrize("case", ["truncated", "huge-header", "identity", "huge-shape", "wrong-dtype", "missing-seed", "bad-index"])
def test_codec_rejects_corruption_before_inference(runtime, video, case):
    _, snapshot = initial(video)
    codec = runtime.state_codec.StateCodec("a"*64)
    data = codec.encode(snapshot)
    if case == "truncated": data = data[:17]
    if case == "huge-header": data = struct.pack("<Q", 2**63) + data[8:]
    if case == "identity": data = data.replace(b"a"*64, b"b"*64)
    if case == "huge-shape": data = mutate_header(data, lambda h: h["c.0.obj_ptr"].update(shape=[100000, 100000, 256]))
    if case == "wrong-dtype": data = mutate_header(data, lambda h: h["c.0.obj_ptr"].update(dtype="I64"))
    if case == "missing-seed": data = mutate_header(data, lambda h: h.pop("c.0.obj_ptr"))
    if case == "bad-index":
        def change(h):
            meta = json.loads(h["__metadata__"]["state"]); meta["index"] = -1
            h["__metadata__"]["state"] = json.dumps(meta)
        data = mutate_header(data, change)
    with pytest.raises(runtime.protocol.ProtocolError):
        codec.decode(data)


def test_codec_enforces_combined_byte_limit(runtime, video):
    _, snapshot = initial(video)
    with pytest.raises(runtime.protocol.ProtocolError):
        runtime.state_codec.StateCodec("a"*64, max_bytes=1024).encode(snapshot)


class StoreDouble:
    max_bytes = 256 * 1024**2
    def __init__(self): self.records = {}
    def _key(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid): raise ValueError("bad session id")
        return "cvat:sam31:" + sid
    def create(self, sid, payload, meta, shapes):
        self.records[sid] = types.SimpleNamespace(revision=0, payload=payload, meta=meta, request="", shapes=shapes)
        return True
    def load(self, sid): return self.records[sid]
    def commit(self, sid, expected, request, payload, shapes):
        record = self.records[sid]
        assert record.revision == expected
        record.revision += 1; record.request = request; record.payload = payload; record.shapes = shapes
        return shapes


def request():
    image = Image.new("RGB", (24, 20))
    buffer = io.BytesIO(); image.save(buffer, format="PNG")
    return {"image": base64.b64encode(buffer.getvalue()).decode()}


def test_tracker_restart_and_lost_reply_are_idempotent(runtime, video):
    store = StoreDouble()
    tracker = runtime.tracker.Tracker(video, store, "a"*64)
    data = request()
    reply = tracker({**data, "shapes": [{"type": "polygon", "points": [2,2,15,2,15,15,2,15]}]})
    previous = reply["states"]
    continued = tracker({**data, "states": previous})
    assert video.model.calls == [0, 1]
    # A new wrapper sees only the persisted payload, not the previous wrapper's state.
    recreated = runtime.tracker.Tracker(video, store, "a"*64)
    assert recreated({**data, "states": previous}) == continued
    assert video.model.calls == [0, 1]  # Retry did not run the neural-network double again.
    assert recreated({**data, "states": continued["states"]})["states"][0]["seq"] == 2


def test_tracker_rejects_wrong_model_and_mixed_object_states(runtime, video):
    store = StoreDouble()
    tracker = runtime.tracker.Tracker(video, store, "a"*64)
    data = request(); seed = {"type":"polygon", "points":[2,2,15,2,15,15,2,15]}
    reply = tracker({**data, "shapes":[seed, seed]})
    with pytest.raises(runtime.protocol.ProtocolError, match="order"):
        tracker({**data, "states": reply["states"][::-1]})
    with pytest.raises(runtime.protocol.ProtocolError, match="model changed"):
        runtime.tracker.Tracker(video, store, "b"*64)({**data, "states": reply["states"]})
    assert video.model.calls == [0]


def test_generated_helpers_do_not_share_sam2_namespace_or_environment(runtime):
    for name in ("protocol.py", "redis_store.py"):
        text = (runtime.staged/name).read_text()
        assert "SAM2_" not in text and "cvat:sam2:" not in text
    assert "cvat:sam31:" in (runtime.staged/"redis_store.py").read_text()
    assert runtime.redis_store.RedisStore(None).prefix == "cvat:sam31:"


def test_function_secrets_are_runtime_only_and_checkpoint_read_only(runtime, tmp_path):
    template = json.loads((Path(__file__).resolve().parents[1]/"function-gpu.json").read_text())
    values = {"NUCLIO_NAMESPACE":"team", "SAM31_REDIS_PASSWORD":"secret"*8, "SAM31_CHECKPOINT_SHA256":"a"*64}
    result = runtime.deploy.render_function(template, values, tmp_path/"model.pt", "image:revision")
    assert result["metadata"]["namespace"] == "team"
    assert result["spec"]["volumes"][0]["volumeMount"]["readOnly"] is True
    assert "secret" not in json.dumps(result["spec"]["build"])
    assert result["spec"]["triggers"]["http"]["attributes"]["disablePortPublishing"] is True
