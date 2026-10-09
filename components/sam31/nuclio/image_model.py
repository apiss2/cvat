# SPDX-License-Identifier: MIT
"""Single-image SAM3.1 prompts using the same strictly loaded multiplex model.

The image embedding is bounded to one image; prompt history and video memory
never cross requests. Positive/negative points and optional boxes match SAM2's
CVAT protocol, including full masks rather than largest-contour polygons.
"""
import hashlib
from threading import Lock

import numpy as np
import torch
import torch.nn.functional as F

from geometry import mask_shape, point_array
from protocol import ProtocolError, decode_image
from temporal_video import TemporalVideo


class ImageInteractor:
    def __init__(self, model):
        self.video = TemporalVideo(model)
        self.image_key = None
        self.features = None
        self.lock = Lock()

    def __call__(self, data):
        image = decode_image(data)
        positive = point_array(data.get("pos_points", []), "pos_points", image.size)
        negative = point_array(data.get("neg_points", []), "neg_points", image.size)
        bbox = point_array(data.get("obj_bbox", []), "obj_bbox", image.size, edge=True)
        if len(bbox) not in (0, 2):
            raise ProtocolError("obj_bbox must be empty or contain two corners")
        if len(bbox) and (bbox[1] <= bbox[0]).any():
            raise ProtocolError("obj_bbox must be [top-left, bottom-right] with positive area")
        if not len(positive) and not len(bbox):
            raise ProtocolError("At least one positive point or a box is required")
        # SAM's two box corners are point labels 2 and 3, preceding normal clicks.
        points = np.concatenate((bbox, positive, negative))
        labels = np.concatenate((np.array([2, 3] if len(bbox) else [], dtype=np.int32),
                                 np.ones(len(positive), dtype=np.int32),
                                 np.zeros(len(negative), dtype=np.int32)))
        points = points * np.array([self.video.model.image_size / image.width,
                                   self.video.model.image_size / image.height], dtype=np.float32)
        key = (image.size, hashlib.sha256(image.tobytes()).digest())
        with self.lock, torch.inference_mode(), self.video.context():
            if self.image_key != key:
                # Release the previous image before computing a replacement embedding.
                self.image_key, self.features = None, None
                self.features = self.video._features(image)
                self.image_key = key
            tensor, features = self.features
            multiplex = self.video.model.multiplex_controller.get_state(
                num_valid_entries=1, device=self.video.device, dtype=torch.float32,
                random=False, object_ids=[0],
            )
            current = self.video.model.track_step(
                frame_idx=0, is_init_cond_frame=True,
                backbone_features_interactive=features["interactive"],
                # The pinned model saves image_features even with its memory encoder off.
                backbone_features_propagation=features["sam2_backbone_out"],
                image=tensor,
                point_inputs={"point_coords": torch.from_numpy(points)[None].to(self.video.device),
                              "point_labels": torch.from_numpy(labels)[None].to(self.video.device)},
                mask_inputs=None, gt_masks=None, frames_to_add_correction_pt=[],
                output_dict={"cond_frame_outputs": {}, "non_cond_frame_outputs": {}},
                num_frames=1, track_in_reverse=False, run_mem_encoder=False,
                prev_sam_mask_logits=None, multiplex_state=multiplex, objects_to_interact=[0],
            )
            logits = current["pred_masks"]  # The model already selects its best mask.
            if (not isinstance(logits, torch.Tensor) or logits.ndim != 4
                    or tuple(logits.shape[:2]) != (1, 1) or min(logits.shape[2:]) < 1
                    or not torch.isfinite(logits).all().item()):
                raise RuntimeError("SAM3.1 returned invalid image mask logits")
            mask = F.interpolate(logits.float(), (image.height, image.width),
                                 mode="bilinear", align_corners=False)[0, 0] > 0
            shape = mask_shape(mask.cpu().numpy())
        return {"shapes": [shape] if shape is not None else []}
