# SPDX-License-Identifier: MIT
"""Test doubles, not Redis or SAM2. See redis_integration.py for real Redis tests."""
import copy
import threading
import numpy as np
import torch
from redis_store import CREATE, CAS


class FakeRedis:
    """Models the store's two atomic operations; does NOT execute the Lua source."""
    def __init__(self):
        self.data, self.expires = {}, {}
        self.lock = threading.RLock()
        self.now = 1000.
        self.fail = False
        self.lose_next_commit_reply = False

    def _purge(self, key):
        if key in self.expires and self.now >= self.expires[key]:
            self.data.pop(key, None)
            self.expires.pop(key, None)

    def hgetall(self, key):
        with self.lock:
            if self.fail:
                raise OSError("simulated Redis outage")
            self._purge(key)
            return copy.deepcopy(self.data.get(key, {}))

    def eval(self, script, numkeys, key, *args):
        assert numkeys == 1
        with self.lock:
            if self.fail:
                raise OSError("simulated Redis outage")
            self._purge(key)
            if script == CREATE:
                if key in self.data:
                    return 0
                payload, meta, shapes, ttl = args
                self.data[key] = {b"revision": b"0", b"payload": payload, b"meta": meta,
                                  b"request": b"", b"shapes": shapes}
                self.expires[key] = self.now + int(ttl)
                return 1
            assert script == CAS
            expected, request, payload, shapes, ttl = args
            fields = self.data.get(key)
            if fields is None:
                return [-1, b""]
            current = int(fields[b"revision"])
            if current == expected + 1 and fields[b"request"] == request.encode():
                return [2, fields[b"shapes"]]
            if current != expected:
                return [0, b""]
            fields.update({b"revision": str(expected + 1).encode(), b"payload": payload,
                           b"request": request.encode(), b"shapes": shapes})
            self.expires[key] = self.now + int(ttl)
            if self.lose_next_commit_reply:
                self.lose_next_commit_reply = False
                raise OSError("simulated lost Redis acknowledgement")
            return [1, shapes]


class FakePredictor:
    """Checks memory access sets using the pinned SAM2 forward-reference rules."""
    image_size = 8
    num_feature_levels = 1
    num_maskmem = 7
    memory_temporal_stride_for_eval = 1
    max_obj_ptrs_in_encoder = 16
    use_obj_ptrs_in_encoder = True
    training = False
    device = torch.device("cpu")

    def __init__(self):
        self.forward_calls = 0
        self.step_calls = 0
        self.accesses = []

    def forward_image(self, image):
        self.forward_calls += 1
        f = torch.nn.functional.adaptive_avg_pool2d(image.mean(1, keepdim=True), (2, 2))
        return {"backbone_fpn": [f], "vision_pos_enc": [torch.zeros_like(f)]}

    def track_step(self, **args):
        self.step_calls += 1
        index = args["frame_idx"]
        assert args["num_frames"] == index + 1
        assert args["run_mem_encoder"] and not args["track_in_reverse"]
        assert args["point_inputs"] is None and args["prev_sam_mask_logits"] is None
        outputs = args["output_dict"]
        mask_refs, ptr_refs = [], []
        if index:
            assert set(outputs["cond_frame_outputs"]) == {0}
            for tpos in range(1, self.num_maskmem):
                distance = self.num_maskmem - tpos
                frame = index - 1 if distance == 1 else (
                    ((index - 2) // self.memory_temporal_stride_for_eval) * self.memory_temporal_stride_for_eval -
                    (distance - 2) * self.memory_temporal_stride_for_eval)
                if frame > 0:
                    item = outputs["non_cond_frame_outputs"][frame]
                    assert "maskmem_features" in item and "maskmem_pos_enc" in item
                    mask_refs.append(frame)
            if self.use_obj_ptrs_in_encoder:
                for distance in range(1, min(index + 1, self.max_obj_ptrs_in_encoder)):
                    frame = index - distance
                    if frame > 0:
                        assert "obj_ptr" in outputs["non_cond_frame_outputs"][frame]
                        ptr_refs.append(frame)
        self.accesses.append((mask_refs, ptr_refs))
        # Synthetic logits. This tests contracts, not the real SAM2 neural network.
        logits = torch.full((1, 1, 8, 8), -1.)
        logits[:, :, 1:7, 1:7] = 1.
        return {"pred_masks": logits, "pred_masks_high_res": logits,
                "maskmem_features": torch.full((1, 2, 2, 2), float(index)),
                "maskmem_pos_enc": [torch.zeros((1, 2, 2, 2))],
                "obj_ptr": torch.full((1, 4), float(index)),
                "object_score_logits": torch.ones((1, 1))}
