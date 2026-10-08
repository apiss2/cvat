# SPDX-License-Identifier: MIT
"""Single-frame SAM2 adapter. No video image buffer or process-local sessions.

Pinned to SAM2 2b90b9f5ceec907a1c18123530e92e794ad901a4. The low-level
track_step contract is isolated here, not patched into SAM2 or CVAT.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import numpy as np
import torch
import torch.nn.functional as F
from model_common import inference_context
from protocol import ProtocolError

SAM2_REVISION = "2b90b9f5ceec907a1c18123530e92e794ad901a4"
ADAPTER_VERSION = 2


@dataclass
class Snapshot:
    index: int
    width: int
    height: int
    objects: list[dict]


def normalize_frame(image, image_size):
    # Match the pinned SAM2 JPEG loader's resize/normalization, without lossy JPEG I/O.
    array = np.asarray(image.convert("RGB").resize((image_size, image_size))) / 255.0
    tensor = torch.from_numpy(array).permute(2, 0, 1).to(torch.float32).contiguous()
    mean = torch.tensor([.485, .456, .406], dtype=torch.float32)[:, None, None]
    std = torch.tensor([.229, .224, .225], dtype=torch.float32)[:, None, None]
    return (tensor - mean) / std


def move_tensors(value, device):
    if isinstance(value, torch.Tensor):
        return value.detach().to(device).contiguous()
    if isinstance(value, dict):
        return {key: move_tensors(item, device) for key, item in value.items()}
    if isinstance(value, list):
        return [move_tensors(item, device) for item in value]
    raise TypeError("Unexpected temporal-memory value")


@dataclass(frozen=True)
class MemoryPolicy:
    mask_slots: int
    stride: int
    pointer_slots: int

    @classmethod
    def from_predictor(cls, predictor):
        policy = cls(int(predictor.num_maskmem), int(predictor.memory_temporal_stride_for_eval),
                     int(predictor.max_obj_ptrs_in_encoder) if predictor.use_obj_ptrs_in_encoder else 1)
        if not (1 <= policy.mask_slots <= 64 and 1 <= policy.stride <= 64 and
                1 <= policy.pointer_slots <= 64):
            raise RuntimeError("Unsupported SAM2 memory policy")
        return policy

    @property
    def mask_horizon(self):
        # Conservative consecutive window covering every future strided mask-memory
        # reference. For stride=1 and num_maskmem=7 this is six non-conditioning frames.
        return (self.mask_slots - 2) * self.stride + 1 if self.mask_slots > 1 else 0

    @property
    def pointer_horizon(self):
        return self.pointer_slots - 1

    def prune(self, outputs, index):
        keep = max(self.mask_horizon, self.pointer_horizon)
        recent = {}
        for frame, output in outputs["non_cond_frame_outputs"].items():
            if frame <= index - keep:
                continue
            item = {"obj_ptr": output["obj_ptr"]}
            if frame > index - self.mask_horizon:
                item.update(maskmem_features=output["maskmem_features"],
                            maskmem_pos_enc=output["maskmem_pos_enc"])
            recent[frame] = item
        return {"cond_frame_outputs": outputs["cond_frame_outputs"],
                "non_cond_frame_outputs": recent}


class TemporalVideo:
    def __init__(self, predictor, *, prune=True):
        if predictor.training:
            raise RuntimeError("SAM2 must be in evaluation mode")
        if not callable(getattr(predictor, "track_step", None)):
            raise RuntimeError("Unsupported SAM2 track_step contract")
        self.predictor = predictor
        self.device = predictor.device
        self.policy = MemoryPolicy.from_predictor(predictor)
        self.prune = prune  # False is used only by the unbounded-memory comparison test.

    def _features(self, image):
        tensor = normalize_frame(image, self.predictor.image_size)[None].to(self.device)
        backbone = self.predictor.forward_image(tensor)
        levels = self.predictor.num_feature_levels
        features = backbone["backbone_fpn"][-levels:]
        positions = backbone["vision_pos_enc"][-levels:]
        if len(features) != levels or len(positions) != levels:
            raise RuntimeError("Unsupported SAM2 backbone output")
        return dict(
            current_vision_feats=[value.flatten(2).permute(2, 0, 1) for value in features],
            current_vision_pos_embeds=[value.flatten(2).permute(2, 0, 1) for value in positions],
            feat_sizes=[tuple(value.shape[-2:]) for value in positions],
        )

    def _step(self, features, index, outputs, mask, size):
        mask_input = None
        if mask is not None:
            mask_input = torch.from_numpy(np.asarray(mask, dtype=np.float32))[None, None].to(self.device)
            if tuple(mask_input.shape[-2:]) != (self.predictor.image_size, self.predictor.image_size):
                mask_input = F.interpolate(mask_input, (self.predictor.image_size, self.predictor.image_size),
                                           mode="bilinear", align_corners=False, antialias=True)
                mask_input = (mask_input >= .5).float()
        output = self.predictor.track_step(
            **features, frame_idx=index, is_init_cond_frame=mask is not None,
            point_inputs=None, mask_inputs=mask_input, output_dict=outputs,
            num_frames=index + 1, track_in_reverse=False, run_mem_encoder=True,
            prev_sam_mask_logits=None,
        )
        logits = output["pred_masks"]
        if (logits.ndim != 4 or tuple(logits.shape[:2]) != (1, 1) or
                not torch.isfinite(logits).all().item()):
            raise RuntimeError("SAM2 returned invalid mask logits")
        video_logits = F.interpolate(logits.float(), (size[1], size[0]),
                                     mode="bilinear", align_corners=False)
        mask_result = (video_logits[0, 0] > 0).detach().cpu().numpy()
        # Past logits, current images, encoder features, and input prompts are NOT
        # consumed by forward-only track_step. Do not serialize them.
        memory = {
            "maskmem_features": output["maskmem_features"],
            "maskmem_pos_enc": output["maskmem_pos_enc"][-1:],
            "obj_ptr": output["obj_ptr"],
        }
        if memory["maskmem_features"] is None or not memory["maskmem_pos_enc"]:
            raise RuntimeError("SAM2 did not produce temporal memory")
        return memory, mask_result

    def initialize(self, image, masks):
        objects, result_masks = [], []
        with inference_context():
            features = self._features(image)  # One encoder invocation for all objects.
            for mask in masks:
                memory, result = self._step(features, 0, {}, mask, image.size)
                objects.append(move_tensors({"cond_frame_outputs": {0: memory},
                                             "non_cond_frame_outputs": {}}, "cpu"))
                result_masks.append(result)
        return Snapshot(0, image.width, image.height, objects), result_masks

    def advance(self, image, snapshot):
        if image.size != (snapshot.width, snapshot.height):
            raise ProtocolError("Video frame dimensions changed")
        index = snapshot.index + 1
        objects, masks = [], []
        with inference_context():
            features = self._features(image)
            for original in snapshot.objects:
                # Redis remains unchanged if any object fails. All GPU memory here is
                # request-local; no frame/session survives in this predictor instance.
                outputs = move_tensors(original, self.device)
                memory, mask = self._step(features, index, outputs, None, image.size)
                outputs["non_cond_frame_outputs"][index] = memory
                if self.prune:
                    outputs = self.policy.prune(outputs, index)
                objects.append(move_tensors(outputs, "cpu"))
                masks.append(mask)
        return Snapshot(index, image.width, image.height, objects), masks


def model_identity(checkpoint, config_path, policy):
    """Content identity, not just a checkpoint filename or a mutable model tag."""
    def file_digest(path):
        with Path(path).open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    details = dict(adapter=ADAPTER_VERSION, sam2=SAM2_REVISION,
                   checkpoint=file_digest(checkpoint), config=file_digest(config_path),
                   policy=vars(policy), torch=torch.__version__,
                   precision="bf16" if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else "fp32")
    return hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
