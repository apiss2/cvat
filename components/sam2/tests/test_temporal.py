# SPDX-License-Identifier: MIT
import json
import pickle
import struct
import numpy as np
import pytest
import torch
from PIL import Image
from safetensors.torch import save
from fakes import FakePredictor
from temporal_video import TemporalVideo, MemoryPolicy, normalize_frame, model_identity
from state_codec import StateCodec
from protocol import ProtocolError

IDENTITY = "a" * 64


def initial(count=1):
    predictor = FakePredictor()
    video = TemporalVideo(predictor)
    image = Image.new("RGB", (24, 20), 90)
    snapshot, masks = video.initialize(image, [np.ones((20, 24), dtype=bool)] * count)
    return predictor, video, image, snapshot


def test_single_frame_step_shared_encoder_and_no_full_video_state():
    predictor, video, image, snapshot = initial(4)
    assert predictor.forward_calls == 1 and predictor.step_calls == 4
    for i in range(1, 21):
        snapshot, masks = video.advance(image, snapshot)
        assert snapshot.index == i and all(mask.shape == (20, 24) for mask in masks)
    assert predictor.forward_calls == 21 and predictor.step_calls == 84
    assert set(vars(video)) == {"predictor", "device", "policy", "prune"}
    assert "images" not in str(snapshot.objects)


@pytest.mark.parametrize("stride", [1, 2, 3, 5])
@pytest.mark.parametrize("slots,pointers", [(7, 16), (3, 2), (1, 16), (2, 1)])
def test_pruning_preserves_every_mask_and_pointer_reference(stride, slots, pointers):
    pred = FakePredictor()
    pred.num_maskmem, pred.max_obj_ptrs_in_encoder = slots, pointers
    pred.memory_temporal_stride_for_eval = stride
    video = TemporalVideo(pred)
    image = Image.new("RGB", (24, 20))
    snapshot, _ = video.initialize(image, [np.ones((20, 24), dtype=bool)])
    codec = StateCodec(IDENTITY)
    for i in range(1, 100):
        # A process boundary, CPU serialization, and reconstruction at EVERY frame.
        snapshot = codec.decode(codec.encode(snapshot))
        snapshot, _ = video.advance(image, snapshot)
        noncond = snapshot.objects[0]["non_cond_frame_outputs"]
        policy = video.policy
        assert len(noncond) <= max(policy.mask_horizon, policy.pointer_horizon)
        assert sum("maskmem_features" in x for x in noncond.values()) <= policy.mask_horizon
    if (stride, slots, pointers) == (1, 7, 16):
        assert len(noncond) == 15  # Do not truncate the 15 object pointers to num_maskmem=7.
        assert sum("maskmem_features" in x for x in noncond.values()) == 6


def test_cpu_serialization_and_dtype_round_trip():
    _, _, _, snapshot = initial()
    memory = snapshot.objects[0]["cond_frame_outputs"][0]
    memory["maskmem_features"] = memory["maskmem_features"].bfloat16()
    codec = StateCodec(IDENTITY)
    result = codec.decode(codec.encode(snapshot))
    got = result.objects[0]["cond_frame_outputs"][0]
    assert got["maskmem_features"].dtype == torch.bfloat16
    for key in ("obj_ptr", "maskmem_features"):
        torch.testing.assert_close(got[key], memory[key], rtol=0, atol=0)
        assert got[key].device.type == "cpu"


def test_state_size_stabilizes_with_long_video():
    _, video, image, snapshot = initial()
    codec = StateCodec(IDENTITY)
    for _ in range(100):
        snapshot, _ = video.advance(image, snapshot)
    size100 = len(codec.encode(snapshot))
    for _ in range(900):
        snapshot, _ = video.advance(image, snapshot)
    assert snapshot.index == 1000
    assert abs(len(codec.encode(snapshot)) - size100) < 2048
    assert len(snapshot.objects[0]["non_cond_frame_outputs"]) == 15


def test_identity_and_corrupt_state_rejected():
    _, _, _, snapshot = initial()
    value = StateCodec(IDENTITY).encode(snapshot)
    with pytest.raises(ProtocolError, match="model or state schema"):
        StateCodec("b" * 64).decode(value)
    for corrupt in (b"x", pickle.dumps(snapshot), value[:20], b"\xff" * 20):
        with pytest.raises(ProtocolError):
            StateCodec(IDENTITY).decode(corrupt)


def test_missing_conditioning_memory_rejected():
    metadata = dict(schema=2, identity=IDENTITY, index=2, width=24, height=20, count=1)
    value = save({"o0.n.2.ptr": torch.zeros(1, 4), "o0.n.2.mem": torch.zeros(1, 2, 2, 2),
                  "o0.n.2.pos": torch.zeros(1, 2, 2, 2)}, metadata={"state": json.dumps(metadata)})
    with pytest.raises(ProtocolError):
        StateCodec(IDENTITY).decode(value)


def test_allocation_limit_checked_before_load():
    _, _, _, snapshot = initial()
    value = StateCodec(IDENTITY).encode(snapshot)
    size = struct.unpack("<Q", value[:8])[0]
    header = json.loads(value[8:8 + size])
    header["o0.c.0.ptr"]["shape"] = [1, 999999999]
    serialized = json.dumps(header).encode()
    bad = struct.pack("<Q", len(serialized)) + serialized + value[8 + size:]
    with pytest.raises(ProtocolError):
        StateCodec(IDENTITY).decode(bad)


def test_byte_limit_encode():
    _, _, _, snapshot = initial(4)
    with pytest.raises(ProtocolError, match="exceeds"):
        StateCodec(IDENTITY, max_bytes=1024).encode(snapshot)


def test_pixel_size_change_rejected():
    _, video, _, snapshot = initial()
    with pytest.raises(ProtocolError, match="dimensions"):
        video.advance(Image.new("RGB", (30, 20)), snapshot)


def test_policy_contract_fails_closed():
    pred = FakePredictor()
    pred.memory_temporal_stride_for_eval = 0
    with pytest.raises(RuntimeError):
        TemporalVideo(pred)
    pred = FakePredictor()
    pred.training = True
    with pytest.raises(RuntimeError):
        TemporalVideo(pred)


def test_nonfinite_masks_rejected():
    pred = FakePredictor()
    original = pred.track_step
    def bad(**args):
        result = original(**args)
        result["pred_masks"] = torch.full((1, 1, 8, 8), float("nan"))
        return result
    pred.track_step = bad
    with pytest.raises(RuntimeError, match="invalid mask"):
        TemporalVideo(pred).initialize(Image.new("RGB", (24, 20)), [np.ones((20, 24), dtype=bool)])


def test_model_identity_tracks_contents(tmp_path):
    ckpt, cfg = tmp_path / "weights", tmp_path / "config"
    ckpt.write_bytes(b"weights-a")
    cfg.write_text("config-a")
    policy = MemoryPolicy(7, 1, 16)
    first = model_identity(ckpt, cfg, policy)
    assert first == model_identity(ckpt, cfg, policy)
    ckpt.write_bytes(b"weights-b")
    assert first != model_identity(ckpt, cfg, policy)
    ckpt.write_bytes(b"weights-a")
    assert first != model_identity(ckpt, cfg, MemoryPolicy(7, 2, 16))


def test_normalization_reference_formula():
    image = Image.fromarray(np.random.default_rng(9).integers(0, 256, (20, 24, 3), dtype=np.uint8))
    value = torch.from_numpy(np.array(image.resize((8, 8))) / 255.0).permute(2, 0, 1).float()
    expected = (value - torch.tensor([.485, .456, .406])[:, None, None]) / torch.tensor([.229, .224, .225])[:, None, None]
    torch.testing.assert_close(normalize_frame(image, 8), expected, rtol=0, atol=0)
