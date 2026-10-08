# SPDX-License-Identifier: MIT
"""Forward-only, fixed-object SAM 3.1 multiplex adapter, isolated from CVAT.

No SAM2 weights or pairwise re-initialization. One encoder/track_step invocation
per frame for the whole object set. Only bounded tensor memory survives in Redis.
The private model contract is pinned and must pass gpu_smoke.py before deployment.
"""
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from protocol import MAX_OBJECTS, ProtocolError

SAM3_REVISION = "0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc"
ADAPTER_VERSION = 1
MEMORY_FIELDS = ("obj_ptr", "maskmem_features", "maskmem_pos_enc", "image_features", "image_pos_enc")


@dataclass
class Snapshot:
    index: int
    width: int
    height: int
    count: int
    outputs: dict


def move_tensors(value, device):
    if isinstance(value, torch.Tensor):
        return value.detach().to(device).contiguous()
    if isinstance(value, dict):
        return {key: move_tensors(item, device) for key, item in value.items()}
    if isinstance(value, list):
        return [move_tensors(item, device) for item in value]
    raise TypeError("Unexpected multiplex memory value")


@dataclass(frozen=True)
class MemoryPolicy:
    masks: int
    pointers: int

    @classmethod
    def from_model(cls, model):
        # Pruning below is valid only for fixed, forward, stride-one memory.
        if (model.use_memory_selection or model.memory_temporal_stride_for_eval != 1
                or not model.use_obj_ptrs_in_encoder or not model.save_image_features
                or model.trim_past_non_cond_mem_for_eval or model.offload_output_to_cpu_for_eval):
            raise RuntimeError("Unsupported SAM3.1 memory policy")
        policy = cls(int(model.num_maskmem) - 1, int(model.max_obj_ptrs_in_encoder) - 1)
        if not (1 <= policy.masks <= 32 and 1 <= policy.pointers <= 64):
            raise RuntimeError("Unsupported SAM3.1 memory horizon")
        return policy

    def prune(self, outputs, index):
        recent = {}
        for frame, output in outputs["non_cond_frame_outputs"].items():
            if frame <= index - max(self.masks, self.pointers):
                continue
            # Older frames are used only for their multiplex object pointers.
            recent[frame] = output if frame > index - self.masks else {"obj_ptr": output["obj_ptr"]}
        return {"cond_frame_outputs": outputs["cond_frame_outputs"], "non_cond_frame_outputs": recent}


def tracking_weights(expected_keys, checkpoint):
    """Select the tracker and shared visual backbone from the official merged checkpoint.

    This mirrors model_builder.py's detector/tracker naming, but loads STRICTLY:
    missing tracker parameters must never leave a randomly initialized predictor.
    Detector/text heads are deliberately not instantiated for polygon-seeded VOS.
    """
    if "model" in checkpoint and isinstance(checkpoint["model"], dict):
        checkpoint = checkpoint["model"]
    renamed = {}
    for key, value in checkpoint.items():
        if key.startswith("sam3_model."):
            key = "detector." + key[len("sam3_model."):]
        elif key.startswith("sam2_predictor."):
            key = "tracker." + key[len("sam2_predictor."):]
        if key in renamed:
            raise ValueError("Duplicate checkpoint key after SAM3.1 remapping")
        renamed[key] = value
    selected, missing = {}, []
    for key in expected_keys:
        source = "detector." + key if key.startswith("backbone.") else "tracker.model." + key
        if source not in renamed:
            missing.append(source)
        else:
            selected[key] = renamed[source]
    if missing:
        raise ValueError(f"Not a compatible merged SAM3.1 checkpoint: missing {len(missing)} tracking parameters ({missing[0]})")
    return selected


def load_model(checkpoint, expected_sha256):
    if not torch.cuda.is_available():
        raise RuntimeError("SAM3.1 tracking requires a CUDA GPU")
    from sam3.model_builder import build_sam3_multiplex_video_model

    checkpoint = Path(checkpoint)
    if not checkpoint.is_file():
        raise ValueError("Mount the approved SAM3.1 checkpoint as a read-only file")
    with checkpoint.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != expected_sha256:
        raise ValueError("SAM3.1 checkpoint SHA256 does not match SAM31_CHECKPOINT_SHA256")
    model = build_sam3_multiplex_video_model(
        checkpoint_path=None, load_from_HF=False, multiplex_count=MAX_OBJECTS,
        use_fa3=False, use_rope_real=True, device="cpu", compile=False,
    )
    weights = torch.load(checkpoint, map_location="cpu", weights_only=True)
    model.load_state_dict(tracking_weights(model.state_dict().keys(), weights), strict=True)
    del weights
    model = model.eval().cuda()
    return model, digest


class TemporalVideo:
    def __init__(self, model, *, prune=True):
        if model.training or not callable(getattr(model, "track_step", None)):
            raise RuntimeError("SAM3.1 must expose the pinned evaluation track_step contract")
        self.model = model
        self.device = next(model.parameters()).device
        self.policy = MemoryPolicy.from_model(model)
        self.prune = prune  # False is only for the GPU unbounded-reference comparison.
        self.bf16 = self.device.type == "cuda" and torch.cuda.is_bf16_supported()

    def context(self):
        return torch.autocast("cuda", dtype=torch.bfloat16) if self.bf16 else nullcontext()

    def _features(self, image):
        from sam3.model.data_misc import NestedTensor
        # The official SAM3.1 video stack uses mean/std=(0.5, 0.5, 0.5).
        array = np.asarray(image.convert("RGB"), dtype=np.float32).copy() / 255.0
        tensor = torch.from_numpy(array).permute(2, 0, 1)[None].to(self.device)
        tensor = F.interpolate(tensor, (self.model.image_size, self.model.image_size),
                               mode="bilinear", align_corners=False, antialias=True)
        tensor = (tensor - 0.5) / 0.5
        backbone = self.model.forward_image(
            NestedTensor(tensors=tensor, mask=None), need_sam3_out=False,
            need_interactive_out=True, need_propagation_out=True,
        )
        return tensor, self.model._prepare_backbone_features(backbone)

    def _step(self, image, index, count, outputs, masks=None):
        from sam3.model.multiplex_utils import MultiplexState
        tensor, features = self._features(image)
        multiplex = self.model.multiplex_controller.get_state(
            num_valid_entries=count, device=self.device, dtype=torch.float32,
            random=False, object_ids=list(range(count)),
        )
        if not isinstance(multiplex, MultiplexState):
            raise RuntimeError("Unsupported SAM3.1 multiplex controller")
        mask_inputs = None
        if masks is not None:
            mask_inputs = torch.from_numpy(np.stack(masks).astype(np.float32))[:, None].to(self.device)
            mask_inputs = F.interpolate(mask_inputs, (self.model.image_size, self.model.image_size),
                                        mode="bilinear", align_corners=False, antialias=True)
            mask_inputs = (mask_inputs >= .5).float()
        current = self.model.track_step(
            frame_idx=index, is_init_cond_frame=masks is not None,
            backbone_features_interactive=features["interactive"],
            backbone_features_propagation=features["sam2_backbone_out"],
            image=tensor, point_inputs=None, mask_inputs=mask_inputs,
            gt_masks=None, frames_to_add_correction_pt=[], output_dict=outputs,
            num_frames=index + 1, track_in_reverse=False, run_mem_encoder=True,
            prev_sam_mask_logits=None, multiplex_state=multiplex, objects_to_interact=None,
        )
        logits = current["pred_masks"]
        if (logits.ndim != 4 or tuple(logits.shape[:2]) != (count, 1)
                or not torch.isfinite(logits).all().item()):
            raise RuntimeError("SAM3.1 returned invalid mask logits")
        video_logits = F.interpolate(logits.float(), (image.height, image.width), mode="bilinear", align_corners=False)
        result = (video_logits[:, 0] > 0).detach().cpu().numpy()
        memory = {field: current[field] for field in MEMORY_FIELDS}
        if any(value is None for value in memory.values()) or not memory["maskmem_pos_enc"]:
            raise RuntimeError("SAM3.1 did not produce temporal memory")
        # Only the final position encoding is read by the pinned forward contract.
        memory["maskmem_pos_enc"] = memory["maskmem_pos_enc"][-1:]
        return memory, list(result)

    def initialize(self, image, masks):
        if not 1 <= len(masks) <= MAX_OBJECTS or any(np.asarray(mask).shape != (image.height, image.width) for mask in masks):
            raise ProtocolError(f"Provide one to {MAX_OBJECTS} masks matching the seed image")
        with torch.inference_mode(), self.context():
            output, result = self._step(image, 0, len(masks), {"cond_frame_outputs": {}, "non_cond_frame_outputs": {}}, masks)
        outputs = {"cond_frame_outputs": {0: output}, "non_cond_frame_outputs": {}}
        return Snapshot(0, image.width, image.height, len(masks), move_tensors(outputs, "cpu")), result

    def advance(self, image, snapshot):
        if image.size != (snapshot.width, snapshot.height):
            raise ProtocolError("Video frame dimensions changed")
        index = snapshot.index + 1
        with torch.inference_mode(), self.context():
            outputs = move_tensors(snapshot.outputs, self.device)
            output, result = self._step(image, index, snapshot.count, outputs)
            outputs["non_cond_frame_outputs"][index] = output
            if self.prune:
                outputs = self.policy.prune(outputs, index)
            outputs = move_tensors(outputs, "cpu")
        return Snapshot(index, image.width, image.height, snapshot.count, outputs), result


def model_identity(digest, policy, bf16):
    details = dict(adapter=ADAPTER_VERSION, sam3=SAM3_REVISION, checkpoint=digest,
                   policy=vars(policy), torch=torch.__version__, precision="bf16" if bf16 else "fp32",
                   normalization="rgb-bilinear-antialias-mean0.5-std0.5", objects="fixed-ordered-multiplex16")
    return hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()
