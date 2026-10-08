# SPDX-License-Identifier: MIT
"""CPU boundary/serialization regressions. No GPU, network Redis or CVAT server."""
import base64
from dataclasses import dataclass
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace, ModuleType

import numpy as np
from PIL import Image
import pytest
import torch

ROOT = Path(__file__).resolve().parents[3]
SAM2 = ROOT / "serverless/pytorch/facebookresearch/sam2/nuclio"


@pytest.fixture(params=["sam2", "sam31"])
def runtime(request, monkeypatch, tmp_path):
    feature = request.param
    names = ("protocol", "geometry", "temporal_video", "state_codec", "redis_store", "redis_tracker", "tracker")
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    if feature == "sam31":
        spec = importlib.util.spec_from_file_location("review_sam31_deploy", ROOT / "components/sam31/deploy.py")
        deploy = importlib.util.module_from_spec(spec); spec.loader.exec_module(deploy)
        deploy.stage_sources(tmp_path, ROOT)
        monkeypatch.syspath_prepend(str(tmp_path))
    else:
        # SAM2's numerical predictor is not exercised: only the persisted Snapshot contract.
        @dataclass
        class Snapshot:
            index: int
            width: int
            height: int
            objects: list
        temporal = ModuleType("temporal_video"); temporal.Snapshot = Snapshot
        monkeypatch.setitem(sys.modules, "temporal_video", temporal)
        monkeypatch.syspath_prepend(str(SAM2))
    modules = {name: importlib.import_module(name) for name in names[:5]}
    tracker = importlib.import_module("tracker" if feature == "sam31" else "redis_tracker")
    yield SimpleNamespace(**modules, tracker=tracker, feature=feature)
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)


def snapshot(runtime, count, index=0):
    if runtime.feature == "sam2":
        def memory():
            return {"obj_ptr": torch.ones(1, 8), "maskmem_features": torch.ones(1, 8, 2, 2),
                    "maskmem_pos_enc": [torch.ones(1, 8, 2, 2)]}
        objects = [{"cond_frame_outputs": {0: memory()},
                    "non_cond_frame_outputs": {i: memory() for i in range(1, index+1)}} for _ in range(count)]
        return runtime.temporal_video.Snapshot(index, 24, 20, objects)
    memory = {"obj_ptr": torch.ones(1, 16, 8), "maskmem_features": torch.ones(1, 8, 2, 2),
              "maskmem_pos_enc": [torch.ones(1, 8, 2, 2)],
              "image_features": torch.ones(4, 1, 8), "image_pos_enc": torch.ones(4, 1, 8)}
    return runtime.temporal_video.Snapshot(index, 24, 20, count,
        {"cond_frame_outputs": {0: memory}, "non_cond_frame_outputs": {i: memory for i in range(1, index+1)}})


@pytest.mark.parametrize("count", [1, 4, 5, 16])
def test_codec_roundtrip_supports_old_and_extended_object_counts(runtime, count):
    codec = runtime.state_codec.StateCodec("a"*64)
    state = codec.decode(codec.encode(snapshot(runtime, count)))
    assert (len(state.objects) if runtime.feature == "sam2" else state.count) == count
    assert runtime.protocol.MAX_OBJECTS == 16
    assert codec.max_bytes == 256 * 1024**2


def test_codec_rejects_seventeen_objects(runtime):
    with pytest.raises((ValueError, runtime.protocol.ProtocolError)):
        codec = runtime.state_codec.StateCodec("a"*64)
        codec.decode(codec.encode(snapshot(runtime, 17)))


def test_extended_sam2_header_with_all_object_indices(runtime):
    # Exercise many memory entries with all 16 object indices.
    codec = runtime.state_codec.StateCodec("a"*64)
    encoded = codec.encode(snapshot(runtime, 16, 31))
    if runtime.feature == "sam2":
        assert int.from_bytes(encoded[:8], "little") > 65536
    restored = codec.decode(encoded)
    assert restored.index == 31
    assert (len(restored.objects) if runtime.feature == "sam2" else restored.count) == 16


@pytest.mark.parametrize("count,valid", [(4, True), (5, True), (16, True), (17, False)])
def test_redis_metadata_count_validation(runtime, count, valid):
    meta = {"identity": "a"*64, "count": count, "width": 24, "height": 20}
    fields = {b"revision": b"0", b"payload": b"state", b"meta": json.dumps(meta).encode(),
              b"request": b"", b"shapes": json.dumps([None]*count).encode()}
    store = runtime.redis_store.RedisStore(SimpleNamespace(hgetall=lambda key: fields))
    if valid:
        assert store.load("a"*43).meta["count"] == count
    else:
        with pytest.raises(runtime.protocol.ProtocolError):
            store.load("a"*43)


@pytest.mark.parametrize("count", [5, 16])
def test_tracker_initialization_continuation_and_replay(runtime, count):
    class VideoDouble:
        calls = 0
        def initialize(self, image, masks):
            self.calls += 1
            return snapshot(runtime, len(masks)), masks
        def advance(self, image, previous):
            self.calls += 1
            return snapshot(runtime, count, previous.index+1), [np.ones((20,24), dtype=bool)]*count
    class StoreDouble:
        max_bytes = 256*1024**2
        def _key(self, sid): return sid
        def create(self, sid, payload, meta, shapes):
            self.record = SimpleNamespace(revision=0, payload=payload, meta=meta, request="", shapes=shapes)
            return True
        def load(self, sid): return self.record
        def commit(self, sid, expected, request, payload, shapes):
            assert self.record.revision == expected
            self.record.revision += 1; self.record.request = request
            self.record.payload = payload; self.record.shapes = shapes
            return shapes
    video, store = VideoDouble(), StoreDouble()
    cls = runtime.tracker.Tracker if runtime.feature == "sam31" else runtime.tracker.RedisTracker
    tracker = cls(video, store, "a"*64)
    data = io.BytesIO(); Image.new("RGB", (24,20)).save(data, format="PNG")
    image = base64.b64encode(data.getvalue()).decode()
    seed = {"type":"polygon", "points":[2,2,15,2,15,15,2,15]}
    initialized = tracker({"image":image, "shapes":[seed]*count})
    continued = tracker({"image":image, "states":initialized["states"]})
    assert len(continued["states"]) == count
    assert cls(video, store, "a"*64)({"image":image, "states":initialized["states"]}) == continued
    assert video.calls == 2
    with pytest.raises(runtime.protocol.ProtocolError):
        tracker({"image":image, "shapes":[seed]*17})
    assert video.calls == 2


def test_sam31_adapter_accepts_sixteen_masks_and_rejects_seventeen(runtime):
    if runtime.feature == "sam2":
        with pytest.raises(ValueError, match="max_objects"):
            runtime.tracker.RedisTracker(None, None, "a"*64, max_objects=17)
        return
    # Exercise the real adapter's count/shape checks without a neural model.
    video = object.__new__(runtime.temporal_video.TemporalVideo)
    video.bf16 = False
    memory = snapshot(runtime, 16).outputs["cond_frame_outputs"][0]
    video._step = lambda image, index, count, outputs, masks: (memory, masks)
    image = Image.new("RGB", (24,20)); masks = [np.ones((20,24), bool)]*16
    state, result = video.initialize(image, masks)
    assert state.count == len(result) == 16
    with pytest.raises(runtime.protocol.ProtocolError):
        video.initialize(image, masks+[masks[0]])
