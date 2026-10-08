#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real-checkpoint GPU contract test. Not an accuracy benchmark or Redis integration test.

Run inside the built function container, where /opt/nuclio contains staged modules.
At least 40 frames cross both memory horizons. Fail on checkpoint/API incompatibility,
state corruption, unbounded memory, or divergence from unpruned forward inference.
"""
import argparse
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw


def frame(index, count):
    image = Image.new("RGB", (320, 240), (35, 35, 35))
    draw = ImageDraw.Draw(image)
    masks = []
    for obj in range(count):
        x = 25 + (obj % 2) * 155 + index % 12
        y = 25 + (obj // 2) * 105
        box = (x, y, x + 42, y + 36)
        draw.rectangle(box, fill=[(230, 100, 50), (60, 210, 90), (60, 90, 230), (225, 210, 65)][obj])
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).rectangle(box, fill=1)
        masks.append(np.asarray(mask, dtype=bool))
    return image, masks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/opt/nuclio"))
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--objects", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args()
    if args.frames < 40:
        parser.error("Use at least 40 frames to cross both memory horizons")
    sys.path.insert(0, str(args.runtime_dir))
    from temporal_video import load_model, TemporalVideo, model_identity
    from state_codec import StateCodec

    checkpoint = os.environ.get("SAM31_CHECKPOINT", "/models/sam3.1_multiplex.pt")
    model, digest = load_model(checkpoint, os.environ["SAM31_CHECKPOINT_SHA256"])
    bounded, reference = TemporalVideo(model), TemporalVideo(model, prune=False)
    codec = StateCodec(model_identity(digest, bounded.policy, bounded.bf16), max_bytes=int(os.getenv("SAM31_STATE_MAX_BYTES", str(256 * 1024**2))))
    image, masks = frame(0, args.objects)
    state, predictions = bounded.initialize(image, masks)
    reference_state, expected = reference.initialize(image, masks)
    maximum = 0
    for index in range(args.frames):
        if index:
            image, _ = frame(index, args.objects)
            # Reconstruct the adapter and decode all persistent state every frame.
            bounded = TemporalVideo(model)
            state, predictions = bounded.advance(image, codec.decode(encoded))
            reference_state, expected = reference.advance(image, reference_state)
        for actual, target in zip(predictions, expected, strict=True):
            if not np.array_equal(actual, target):
                raise AssertionError(f"Pruned/restored and unbounded masks differ at frame {index}")
        encoded = codec.encode(state)
        maximum = max(maximum, len(encoded))
        recent = state.outputs["non_cond_frame_outputs"]
        if len(recent) > max(bounded.policy.masks, bounded.policy.pointers):
            raise AssertionError("Temporal memory did not remain bounded")
        print(f"frame={index} objects={args.objects} state_bytes={len(encoded)}", flush=True)
    print(f"PASS: {args.frames} real-GPU frames; restored/pruned masks match unbounded inference; peak state={maximum} bytes")


if __name__ == "__main__":
    main()
