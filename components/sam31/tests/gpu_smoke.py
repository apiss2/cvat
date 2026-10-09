#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real-checkpoint GPU contract test. Not an accuracy benchmark or Redis integration test.

Run inside the built function container, where /opt/nuclio contains staged modules.
At least 40 frames cross both memory horizons. Fail on checkpoint/API incompatibility,
state corruption, unbounded memory, or divergence from unpruned forward inference.
"""
import argparse
import base64
import io
import os
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw


def frame(index, count):
    if not 1 <= count <= 16:
        raise ValueError("count must be in 1..16")
    image = Image.new("RGB", (640, 480), (35, 35, 35))
    draw = ImageDraw.Draw(image)
    masks = []
    for obj in range(count):
        x = 25 + (obj % 4) * 155 + index % 12
        y = 25 + (obj // 4) * 105
        box = (x, y, x + 42, y + 36)
        draw.rectangle(box, fill=[(230, 100, 50), (60, 210, 90), (60, 90, 230), (225, 210, 65)][obj % 4])
        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).rectangle(box, fill=1)
        masks.append(np.asarray(mask, dtype=bool))
    return image, masks


def check_image_prompts(model):
    """Exercise real single-image prompts, full mask output and embedding reuse."""
    from image_model import ImageInteractor

    image, _ = frame(0, 1)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    encoded = base64.b64encode(stream.getvalue()).decode()
    op = ImageInteractor(model)
    cases = (
        {"pos_points": [[45, 40]]},
        {"pos_points": [[45, 40]], "neg_points": [[5, 5]]},
        {"obj_bbox": [[25, 25], [67, 61]]},
        {"obj_bbox": [[25, 25], [67, 61]], "pos_points": [[45, 40]]},
    )
    for prompts in cases:
        data = {"image": encoded, **prompts}
        result = op(data)
        if result != ImageInteractor(model)(data):
            raise AssertionError("Cached and fresh single-image inference differ")
        for shape in result["shapes"]:
            if shape["type"] != "mask":
                raise AssertionError("Single-image inference must return full masks")
            *runs, x0, y0, x1, y1 = shape["points"]
            if not (0 <= x0 <= x1 < image.width and 0 <= y0 <= y1 < image.height):
                raise AssertionError("Image mask bounds are invalid")
            if sum(runs) != (x1 - x0 + 1) * (y1 - y0 + 1):
                raise AssertionError("Image mask RLE is invalid")
    print("PASS: real single-image point/box prompts and embedding reuse; not an accuracy test")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=Path("/opt/nuclio"))
    parser.add_argument("--frames", type=int, default=40)
    parser.add_argument("--objects", type=int, choices=range(1, 17), default=4)
    args = parser.parse_args()
    if args.frames < 40:
        parser.error("Use at least 40 frames to cross both memory horizons")
    sys.path.insert(0, str(args.runtime_dir))
    from temporal_video import CHECKPOINT, load_model, TemporalVideo, model_identity
    from state_codec import StateCodec

    checkpoint = os.environ.get("SAM31_CHECKPOINT", CHECKPOINT)
    model, digest = load_model(checkpoint)
    check_image_prompts(model)
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
