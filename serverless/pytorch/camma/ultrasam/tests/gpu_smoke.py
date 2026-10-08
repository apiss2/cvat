#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Real-checkpoint comparison against upstream's single-point decoder.

Run in the built Nuclio container, with an NVIDIA GPU (the default).
--device cpu permits an explicit CPU diagnostic in the same pinned environment.
This checks inference/protocol consistency, not ultrasound segmentation accuracy.
"""

import argparse
import base64
import io
import json
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw

for candidate in (Path(__file__).resolve().parents[1] / "nuclio", Path("/opt/nuclio")):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))

from ultrasam_constants import (
    CHECKPOINT,
    CHECKPOINT_SHA256,
    UPSTREAM_COMMIT,
    UPSTREAM_ROOT,
)
from ultrasam_interactor import ImageInteractor
from ultrasam_model import UltraSamPredictor, build_model, warmup_predictor
from ultrasam_runtime import run_probe


def coordinate(value):
    try:
        parts = [float(part) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Point must be x,y") from exc
    if len(parts) != 2 or not np.isfinite(parts).all():
        raise argparse.ArgumentTypeError("Point must be finite x,y")
    return parts


def decode_cvat_mask(result, size):
    width, height = size
    mask = np.zeros((height, width), dtype=bool)
    if not result["shapes"]:
        return mask
    if len(result["shapes"]) != 1 or result["shapes"][0]["type"] != "mask":
        raise AssertionError("Expected one full CVAT mask")
    *runs, x0, y0, x1, y1 = result["shapes"][0]["points"]
    assert 0 <= x0 <= x1 < width and 0 <= y0 <= y1 < height
    values = np.repeat(np.arange(len(runs)) % 2, runs).astype(bool)
    assert len(values) == (x1 - x0 + 1) * (y1 - y0 + 1)
    mask[y0 : y1 + 1, x0 : x1 + 1] = values.reshape(y1 - y0 + 1, x1 - x0 + 1)
    return mask


@torch.inference_mode()
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="Optional local ultrasound image")
    parser.add_argument(
        "--positive", type=coordinate, help="One positive x,y; default image center"
    )
    parser.add_argument(
        "--negative",
        type=coordinate,
        help="One negative x,y; default near the upper-left corner",
    )
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT)
    parser.add_argument("--upstream-root", type=Path, default=UPSTREAM_ROOT)
    args = parser.parse_args()
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("No NVIDIA GPU is available; the GPU smoke test was not run")
    runtime = run_probe("cuda") if args.device == "cuda" else None
    if args.image:
        with Image.open(args.image) as input_image:
            image = input_image.convert("RGB")
    else:
        image = Image.new("RGB", (317, 193), (35, 35, 35))
        ImageDraw.Draw(image).ellipse((92, 40, 235, 160), fill=(160, 160, 160))
    positive = args.positive or [image.width / 2, image.height / 2]
    negative = args.negative or [image.width / 10, image.height / 10]
    for name, point in (("positive", positive), ("negative", negative)):
        if any(value < 0 or value >= limit for value, limit in zip(point, image.size)):
            parser.error(f"--{name} lies outside the image")
    model = build_model(
        args.device, checkpoint_path=args.checkpoint, upstream_root=args.upstream_root
    )
    predictor = UltraSamPredictor(model)
    warmup_predictor(predictor)
    predictor.set_image(np.asarray(image))
    points = np.asarray([positive], dtype=np.float32)
    labels = np.array([1], dtype=np.int64)
    actual_logits, actual_scores = predictor.decode_points(points, labels)

    # The original upstream generator pads 20 instances and accepts one positive
    # point per instance. Compare its active instance with our single-instance
    # adapter before either implementation performs mask resizing/thresholding.
    from endosam.datasets.transforms.custom_pipeline import PromptType
    from mmdet.structures import DetDataSample
    from mmengine.structures import InstanceData

    sample = DetDataSample(metainfo=predictor.metadata)
    sample.gt_instances = InstanceData(
        labels=torch.zeros(1, dtype=torch.long, device=args.device),
        points=predictor.transform_points(points).unsqueeze(0),
        boxes=torch.zeros((1, 2, 2), device=args.device),
        prompt_types=torch.tensor([PromptType.POINT.value], device=args.device),
    )
    head_inputs = model.forward_transformer(
        predictor.features, [sample], use_mask_prompt=False
    )
    expected_logits, expected_scores = model.bbox_head(
        **head_inputs, multimask_output=True
    )
    torch.testing.assert_close(actual_logits, expected_logits, rtol=1e-4, atol=1e-4)
    torch.testing.assert_close(actual_scores, expected_scores, rtol=1e-4, atol=1e-4)

    original_mask, positive_score = predictor.predict(points, labels)
    combined_points = np.asarray([positive, negative], dtype=np.float32)
    negative_mask, negative_score = predictor.predict(combined_points, np.array([1, 0]))
    repeat_mask, repeat_score = predictor.predict(points, labels)
    np.testing.assert_array_equal(repeat_mask, original_mask)
    assert repeat_score == positive_score
    assert original_mask.shape == (image.height, image.width)
    assert negative_mask.shape == original_mask.shape

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    operation = ImageInteractor(predictor)
    response = operation(
        dict(
            image=base64.b64encode(buffer.getvalue()).decode(),
            pos_points=[positive],
            neg_points=[negative],
            obj_bbox=None,
        )
    )
    np.testing.assert_array_equal(decode_cvat_mask(response, image.size), negative_mask)
    report = dict(
        device=args.device,
        torch=torch.__version__,
        cuda=torch.version.cuda,
        runtime=runtime,
        model_warmup=True,
        checkpoint_sha256=CHECKPOINT_SHA256,
        upstream_commit=UPSTREAM_COMMIT,
        image_kind="supplied" if args.image else "synthetic",
        image_size=list(image.size),
        max_absolute_logit_difference=float(
            (actual_logits - expected_logits).abs().max()
        ),
        max_absolute_score_difference=float(
            (actual_scores - expected_scores).abs().max()
        ),
        positive_score=positive_score,
        negative_score=negative_score,
        positive_pixels=int(original_mask.sum()),
        negative_pixels=int(negative_mask.sum()),
        changed_pixels=int((original_mask != negative_mask).sum()),
        prompt_history_isolated=True,
        cvat_rle_roundtrip=True,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
