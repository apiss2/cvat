# SPDX-License-Identifier: MIT
"""Load the original OpenMMLab UltraSAM model without converting its weights."""

import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from ultrasam_constants import (
    CHECKPOINT,
    CHECKPOINT_SHA256,
    UPSTREAM_COMMIT,
    UPSTREAM_ROOT,
)
from ultrasam_prompt_adapter import encode_points, restore_mask


def checkpoint_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_checkpoint_load(model, checkpoint):
    """Reject renamed, missing, unexpected, non-tensor and mismatched weights."""
    if not isinstance(checkpoint, dict) or not isinstance(
        checkpoint.get("state_dict"), dict
    ):
        raise RuntimeError(
            "Expected the official OpenMMLab UltraSAM state_dict checkpoint"
        )
    state = checkpoint["state_dict"]
    expected = model.state_dict()
    missing = sorted(expected.keys() - state.keys())
    unexpected = sorted(state.keys() - expected.keys())
    if missing or unexpected:
        raise RuntimeError(
            f"Checkpoint keys differ: missing={missing}, unexpected={unexpected}"
        )
    for name, tensor in state.items():
        if not isinstance(tensor, torch.Tensor) or tensor.shape != expected[name].shape:
            raise RuntimeError(f"Checkpoint tensor shape differs: {name}")
        if tensor.dtype != expected[name].dtype or not torch.isfinite(tensor).all():
            raise RuntimeError(f"Checkpoint tensor type or values differ: {name}")
    model.load_state_dict(state, strict=True)


def build_model(device="cuda", checkpoint_path=CHECKPOINT, upstream_root=UPSTREAM_ROOT):
    root = Path(upstream_root).resolve()
    actual_revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual_revision != UPSTREAM_COMMIT:
        raise RuntimeError(
            "UltraSAM source revision does not match the pinned revision"
        )
    if checkpoint_digest(checkpoint_path) != CHECKPOINT_SHA256:
        raise RuntimeError(
            "UltraSAM checkpoint SHA256 does not match the pinned checkpoint"
        )
    if device != "cpu" and not torch.cuda.is_available():
        raise RuntimeError(
            "UltraSAM requires an NVIDIA GPU available to the Nuclio worker"
        )
    sys.path.insert(0, str(root))
    from mmdet.registry import MODELS
    from mmdet.utils import register_all_modules
    from mmengine.config import Config

    register_all_modules(init_default_scope=True)
    for name in (
        "mmpretrain.models",
        "endosam.models.detectors.SAM",
        "endosam.models.dense_heads.sam_mask_decoder",
        "endosam.models.task_modules.prior_generators.label_encoder",
        "endosam.models.task_modules.assigners.SAMassigner",
    ):
        importlib.import_module(name)
    # This is the same hook required by upstream inference. Ordinary PyTorch
    # attention rejects the custom downsampled Q/K/V projection dimensions.
    from endosam.models.utils.custom_functional import multi_head_attention_forward

    F.multi_head_attention_forward = multi_head_attention_forward
    config = Config.fromfile(
        str(root / "configs/_base_/models/sam_mask_refinement.py"),
        import_custom_modules=False,
    )
    model = MODELS.build(config.model)
    # The official checkpoint includes MMEngine HistoryBuffer and NumPy metadata.
    # It cannot be read with weights_only=True in the pinned PyTorch release.
    # The hard-coded SHA256 is checked BEFORE unpickling; arbitrary checkpoints
    # and a fallback from a restricted unpickler are deliberately not accepted.
    checkpoint = torch.load(
        str(checkpoint_path), map_location="cpu", weights_only=False
    )
    strict_checkpoint_load(model, checkpoint)
    del checkpoint
    model.eval().to(device)
    return model


def warmup_predictor(predictor):
    """Exercise image encoding and positive/negative decoding before readiness."""
    image = np.full((96, 128, 3), 32, dtype=np.uint8)
    image[24:72, 32:96] = 160
    try:
        predictor.set_image(image)
        mask, score = predictor.predict(
            np.array([[64, 48], [8, 8]], dtype=np.float32),
            np.array([1, 0], dtype=np.int64),
        )
        if mask.shape != image.shape[:2] or not np.isfinite(score):
            raise RuntimeError("UltraSAM warmup returned an invalid mask or score")
        if predictor.device.type == "cuda":
            torch.cuda.synchronize(predictor.device)
    finally:
        # A synthetic warmup must not become the first user's cached image.
        predictor.features = None
        predictor.metadata = None


class UltraSamPredictor:
    def __init__(self, model):
        from endosam.models.task_modules.prior_generators.prompt_encoder import (
            EmbeddingIndex,
        )
        from mmdet.datasets.transforms import FixScaleResize

        self.model = model
        self.embedding_index = EmbeddingIndex
        self.resize = FixScaleResize(scale=(1024, 1024), keep_ratio=True)
        self.features = None
        self.metadata = None
        self.device = next(model.parameters()).device

    @torch.inference_mode()
    def set_image(self, image):
        from mmdet.structures import DetDataSample

        self.features = None
        self.metadata = None
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise ValueError("Expected an RGB uint8 image")
        original_size = tuple(image.shape[:2])
        # Upstream decodes BGR, then DetDataPreprocessor converts BGR to RGB.
        resized = self.resize(
            dict(img=image[..., ::-1].copy(), ori_shape=original_size)
        )
        sample = DetDataSample(
            metainfo={
                "ori_shape": original_size,
                "img_shape": tuple(resized["img_shape"]),
                "scale_factor": tuple(resized["scale_factor"]),
            }
        )
        tensor = torch.from_numpy(np.ascontiguousarray(resized["img"])).permute(2, 0, 1)
        batch = self.model.data_preprocessor(
            dict(inputs=[tensor], data_samples=[sample]), training=False
        )
        features = self.model.extract_feat(batch["inputs"])
        if (
            tuple(features[-1].shape) != (1, 256, 64, 64)
            or not torch.isfinite(features[-1]).all()
        ):
            raise RuntimeError("UltraSAM produced an invalid image embedding")
        self.features = features
        self.metadata = batch["data_samples"][0].metainfo

    def transform_points(self, points):
        if self.metadata is None:
            raise RuntimeError("set_image must succeed before point inference")
        point_tensor = torch.as_tensor(points, dtype=torch.float32, device=self.device)
        scale = point_tensor.new_tensor(self.metadata["scale_factor"])
        # GetPointFromMask(test=True, normalize=False) applies +0.5 AFTER resizing.
        return point_tensor * scale + 0.5

    @torch.inference_mode()
    def decode_points(self, points, labels):
        if self.features is None:
            raise RuntimeError("set_image must succeed before point inference")
        encoded = encode_points(
            self.model.prompt_encoder,
            self.transform_points(points),
            torch.as_tensor(labels, dtype=torch.long, device=self.device),
            self.embedding_index,
        )
        feat = self.features[-1]
        inputs = self.model.forward_decoder(
            img_feats=feat,
            img_pos=self.model.prompt_encoder.get_dense_pe(),
            padding_mask=torch.zeros(
                (1, *feat.shape[-2:]), dtype=torch.bool, device=self.device
            ),
            **encoded,
        )
        # Keep all point constraints at each click. Automatic box refinement would
        # replace the user's points. The mask-downscaling weights are still loaded.
        logits, scores = self.model.bbox_head(**inputs, multimask_output=True)
        if (
            tuple(logits.shape) != (1, 3, 256, 256)
            or tuple(scores.shape) != (1, 3)
            or not torch.isfinite(logits).all()
            or not torch.isfinite(scores).all()
        ):
            raise RuntimeError("UltraSAM produced invalid mask logits or IoU scores")
        return logits, scores

    @torch.inference_mode()
    def predict(self, points, labels):
        logits, scores = self.decode_points(points, labels)
        best = int(scores[0].argmax().item())
        mask = restore_mask(
            logits[0, best],
            tuple(self.metadata["pad_shape"]),
            tuple(self.metadata["img_shape"]),
            tuple(self.metadata["ori_shape"]),
        )
        return mask.cpu().numpy(), float(scores[0, best].item())
