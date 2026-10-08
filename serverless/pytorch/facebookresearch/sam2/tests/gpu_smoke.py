#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real-GPU comparison: pruned + serialized vs unbounded SAME track_step adapter.

This is NOT a comparison against the full-video predictor (which has different
initial-mask consolidation and early object-pointer normalization semantics).
Run only in the Nuclio GPU environment. No checkpoints are bundled in this ZIP.
"""
import argparse
import json
from pathlib import Path
import sys
import numpy as np
from PIL import Image, ImageDraw
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nuclio"))
from model_common import CHECKPOINT, MODEL_CONFIG, require_gpu
from temporal_video import TemporalVideo, model_identity
from state_codec import StateCodec
from geometry import polygon_mask


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=32)
    parser.add_argument("--min-iou", type=float, default=.999)
    args = parser.parse_args()
    if not 17 <= args.frames <= 1000:
        parser.error("--frames must be 17..1000 to exercise history beyond num_maskmem")
    require_gpu()
    import torch
    import sam2
    from sam2.build_sam import build_sam2_video_predictor
    predictor = build_sam2_video_predictor(MODEL_CONFIG, CHECKPOINT, device="cuda", apply_postprocessing=False)
    bounded = TemporalVideo(predictor)
    reference = TemporalVideo(predictor, prune=False)
    identity = model_identity(CHECKPOINT, Path(sam2.__file__).parent / MODEL_CONFIG, bounded.policy)
    codec = StateCodec(identity)
    def frame(i):
        image = Image.new("RGB", (256, 192), (60, 80, 100))
        offset = i % 20
        ImageDraw.Draw(image).rectangle((64 + offset, 45, 145 + offset, 130), fill=(205, 180, 150))
        return image
    seed_image = frame(0)
    mask = polygon_mask({"type": "polygon", "points": [64, 45, 145, 45, 145, 130, 64, 130]}, seed_image.size)
    bounded_state, actual = bounded.initialize(seed_image, [mask])
    reference_state, expected = reference.initialize(seed_image, [mask])
    ious, sizes = [], []
    for index in range(args.frames):
        payload = codec.encode(bounded_state)
        sizes.append(len(payload))
        # Reconstruct the adapter and deserialize CPU state between every frame.
        bounded_state = codec.decode(payload)
        bounded = TemporalVideo(predictor)
        if index:
            bounded_state, actual = bounded.advance(frame(index), bounded_state)
            reference_state, expected = reference.advance(frame(index), reference_state)
        union = np.logical_or(actual[0], expected[0]).sum()
        iou = float(np.logical_and(actual[0], expected[0]).sum() / union) if union else 1.
        ious.append(iou)
        kind = "non_cond_frame_outputs" if index else "cond_frame_outputs"
        torch.testing.assert_close(
            bounded_state.objects[0][kind][index]["obj_ptr"],
            reference_state.objects[0][kind][index]["obj_ptr"], rtol=1e-3, atol=1e-3,
        )
    print(json.dumps(dict(frames=args.frames, torch=torch.__version__, identity=identity,
                         minimum_iou=min(ious), ious=ious, serialized_bytes=sizes,
                         noncond_frames=len(bounded_state.objects[0]["non_cond_frame_outputs"])), indent=2))
    if min(ious) < args.min_iou:
        raise RuntimeError("Serialized/pruned temporal state changed output beyond tolerance")


if __name__ == "__main__":
    main()
