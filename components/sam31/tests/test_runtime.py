# SPDX-License-Identifier: MIT
"""CPU tracking/serialization contracts. Neural network and Redis are doubled."""
import base64
import copy
import importlib
import io
import json
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
from PIL import Image
import pytest
import torch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from components.extensions import functions


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    functions.stage_sources(tmp_path, "sam31", ROOT)
    names = ("protocol", "geometry", "temporal_video", "state_codec", "redis_store", "tracker")
    monkeypatch.syspath_prepend(str(tmp_path))
    modules = {}
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
        modules[name] = importlib.import_module(name)
    yield SimpleNamespace(**modules)
    for name in names:
        sys.modules.pop(name, None)


class FakeMultiplex:
    def __init__(self, count): self.count = count


class FakeModel(torch.nn.Module):
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
        self.multiplex_controller = SimpleNamespace(get_state=self.get_state)
        self.eval()
    def get_state(self, *, num_valid_entries, device, dtype, random, object_ids):
        assert random is False and object_ids == list(range(num_valid_entries))
        return FakeMultiplex(num_valid_entries)
    def track_step(self, **kwargs):
        self.calls.append(kwargs["frame_idx"])
        assert kwargs["point_inputs"] is None and kwargs["track_in_reverse"] is False
        assert kwargs["run_mem_encoder"] is True and kwargs["num_frames"] == kwargs["frame_idx"]+1
        count, index = kwargs["multiplex_state"].count, kwargs["frame_idx"]
        if index == 0: assert kwargs["mask_inputs"].shape == (count, 1, 8, 8)
        else:
            assert kwargs["mask_inputs"] is None and 0 in kwargs["output_dict"]["cond_frame_outputs"]
        logits = torch.full((count, 1, 8, 8), -1.); logits[:, :, 2:6, 2:6] = 1
        return {"pred_masks": logits, "obj_ptr": torch.ones(1, 16, 8)*index,
                "maskmem_features": torch.ones(1, 8, 2, 2)*index,
                "maskmem_pos_enc": [torch.ones(1, 8, 2, 2)],
                "image_features": torch.ones(4, 1, 8), "image_pos_enc": torch.ones(4, 1, 8)}


@pytest.fixture
def video(runtime, monkeypatch):
    module = ModuleType("sam3.model.multiplex_utils"); module.MultiplexState = FakeMultiplex
    monkeypatch.setitem(sys.modules, module.__name__, module)
    video = runtime.temporal_video.TemporalVideo(FakeModel())
    video._features = lambda image: (torch.zeros(1, 3, 8, 8), {"interactive": {}, "sam2_backbone_out": {}})
    return video


def initial(video, count=2):
    image = Image.new("RGB", (24, 20))
    masks = [np.ones((20, 24), dtype=bool) for _ in range(count)]
    return image, video.initialize(image, masks)[0]


@pytest.mark.parametrize("count", [1, 4, 16])
def test_one_multiplex_step_per_frame(video, count):
    image, state = initial(video, count)
    state, result = video.advance(image, state)
    assert video.model.calls == [0, 1] and state.count == len(result) == count
    assert all(mask.shape == (20, 24) for mask in result)


def test_pruning_keeps_seed_six_masks_fifteen_pointers(video):
    image, state = initial(video)
    for _ in range(80): state, _ = video.advance(image, state)
    recent = state.outputs["non_cond_frame_outputs"]
    assert set(state.outputs["cond_frame_outputs"]) == {0}
    assert set(recent) == set(range(66, 81))
    assert {frame for frame,out in recent.items() if "maskmem_features" in out} == set(range(75, 81))
    assert all(set(recent[frame]) == {"obj_ptr"} for frame in range(66, 75))


def test_codec_restore_each_frame_and_no_mutation(runtime, video):
    image, state = initial(video)
    original = copy.deepcopy(state.outputs)
    codec = runtime.state_codec.StateCodec("a"*64)
    updated, _ = video.advance(image, state)
    assert not state.outputs["non_cond_frame_outputs"]
    assert torch.equal(original["cond_frame_outputs"][0]["obj_ptr"], state.outputs["cond_frame_outputs"][0]["obj_ptr"])
    for _ in range(34): updated, _ = video.advance(image, codec.decode(codec.encode(updated)))
    restored = codec.decode(codec.encode(updated))
    assert restored.index == 35 and restored.count == 2
    assert restored.outputs["cond_frame_outputs"][0]["obj_ptr"].device.type == "cpu"


@pytest.mark.parametrize("change", ["use_memory_selection", "save_image_features", "memory_temporal_stride_for_eval",
                                   "trim_past_non_cond_mem_for_eval", "offload_output_to_cpu_for_eval"])
def test_changed_memory_policy_rejected(runtime, change):
    model = FakeModel()
    setattr(model, change, 2 if change == "memory_temporal_stride_for_eval" else not getattr(model, change))
    with pytest.raises(RuntimeError, match="memory policy"): runtime.temporal_video.TemporalVideo(model)


def test_checkpoint_mapping_requires_all_tracking_weights(runtime):
    keys = ["backbone.vision_backbone.trunk.weight", "transformer.weight"]
    checkpoint = {"detector.backbone.vision_backbone.trunk.weight": torch.ones(1),
                  "tracker.model.transformer.weight": torch.ones(2), "detector.text.weight": torch.ones(3)}
    assert set(runtime.temporal_video.tracking_weights(keys, checkpoint)) == set(keys)
    official_aliases = {key.replace("detector.", "sam3_model.").replace("tracker.", "sam2_predictor."): value for key,value in checkpoint.items()}
    assert set(runtime.temporal_video.tracking_weights(keys, {"model": official_aliases})) == set(keys)
    with pytest.raises(ValueError, match="missing"): runtime.temporal_video.tracking_weights(keys, {"sam2_weight": torch.ones(1)})


def test_identity_changes_for_weights_precision_policy(runtime):
    identity = runtime.temporal_video.model_identity
    policy = runtime.temporal_video.MemoryPolicy(6, 15)
    assert len({identity("a"*64, policy, False), identity("b"*64, policy, False), identity("a"*64, policy, True),
                identity("a"*64, runtime.temporal_video.MemoryPolicy(5, 15), False)}) == 4


def mutate_header(data, mutation):
    size = struct.unpack("<Q", data[:8])[0]
    header = json.loads(data[8:8+size]); mutation(header)
    encoded = json.dumps(header, separators=(",", ":")).encode(); encoded += b" "*(-len(encoded)%8)
    return struct.pack("<Q", len(encoded))+encoded+data[8+size:]


@pytest.mark.parametrize("case", ["truncated", "huge-header", "identity", "huge-shape", "wrong-dtype", "missing-seed", "bad-index"])
def test_codec_rejects_corruption(runtime, video, case):
    _, state = initial(video); codec = runtime.state_codec.StateCodec("a"*64); data = codec.encode(state)
    if case == "truncated": data = data[:17]
    if case == "huge-header": data = struct.pack("<Q", 2**63)+data[8:]
    if case == "identity": data = data.replace(b"a"*64, b"b"*64)
    if case == "huge-shape": data = mutate_header(data, lambda h: h["c.0.obj_ptr"].update(shape=[100000, 100000, 256]))
    if case == "wrong-dtype": data = mutate_header(data, lambda h: h["c.0.obj_ptr"].update(dtype="I64"))
    if case == "missing-seed": data = mutate_header(data, lambda h: h.pop("c.0.obj_ptr"))
    if case == "bad-index":
        def change(h):
            meta = json.loads(h["__metadata__"]["state"]); meta["index"] = -1
            h["__metadata__"]["state"] = json.dumps(meta)
        data = mutate_header(data, change)
    with pytest.raises(runtime.protocol.ProtocolError): codec.decode(data)


def test_codec_byte_limit(runtime, video):
    _, state = initial(video)
    with pytest.raises(runtime.protocol.ProtocolError): runtime.state_codec.StateCodec("a"*64, max_bytes=1024).encode(state)


class StoreDouble:
    max_bytes = 256*1024**2
    def __init__(self): self.records = {}
    def _key(self, sid): return sid
    def create(self, sid, payload, meta, shapes):
        self.records[sid] = SimpleNamespace(revision=0, payload=payload, meta=meta, request="", shapes=shapes)
        return True
    def load(self, sid): return self.records[sid]
    def commit(self, sid, expected, request, payload, shapes):
        record = self.records[sid]; assert record.revision == expected
        record.revision += 1; record.request = request; record.payload = payload; record.shapes = shapes
        return shapes


def request():
    buffer = io.BytesIO(); Image.new("RGB", (24,20)).save(buffer, format="PNG")
    return {"image": base64.b64encode(buffer.getvalue()).decode()}


def test_tracker_restart_retry_wrong_model_and_order(runtime, video):
    store = StoreDouble(); tracker = runtime.tracker.Tracker(video, store, "a"*64)
    data = request(); seed = {"type": "polygon", "points": [2,2,15,2,15,15,2,15]}
    initialized = tracker({**data, "shapes": [seed, seed]})
    continued = tracker({**data, "states": initialized["states"]})
    recreated = runtime.tracker.Tracker(video, store, "a"*64)
    assert recreated({**data, "states": initialized["states"]}) == continued
    assert video.model.calls == [0, 1]
    with pytest.raises(runtime.protocol.ProtocolError, match="order"):
        tracker({**data, "states": continued["states"][::-1]})
    with pytest.raises(runtime.protocol.ProtocolError, match="model changed"):
        runtime.tracker.Tracker(video, store, "b"*64)({**data, "states": continued["states"]})
    assert recreated({**data, "states": continued["states"]})["states"][0]["seq"] == 2
