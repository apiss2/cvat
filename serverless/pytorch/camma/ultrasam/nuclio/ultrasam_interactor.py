# SPDX-License-Identifier: MIT
"""One image embedding per worker, with no cross-request prompt history."""

import hashlib
from threading import Lock

import numpy as np
from ultrasam_constants import MAX_POINTS
from ultrasam_geometry import mask_shape, point_array
from ultrasam_protocol import ProtocolError, decode_image


class ImageInteractor:
    def __init__(self, predictor):
        self.predictor = predictor
        self.image_key = None
        self.lock = Lock()

    def __call__(self, data):
        image = decode_image(data)
        positive = point_array(data.get("pos_points", []), "pos_points", image.size)
        negative = point_array(data.get("neg_points", []), "neg_points", image.size)
        if not len(positive):
            raise ProtocolError("At least one positive point is required")
        if len(positive) + len(negative) > MAX_POINTS:
            raise ProtocolError(f"At most {MAX_POINTS} total points are supported")
        if data.get("obj_bbox") not in (None, []):
            raise ProtocolError("UltraSAM accepts point prompts only")
        points = np.concatenate((positive, negative))
        labels = np.r_[
            np.ones(len(positive), dtype=np.int64),
            np.zeros(len(negative), dtype=np.int64),
        ]
        key = (image.size, hashlib.sha256(image.tobytes()).digest())
        with self.lock:
            if self.image_key != key:
                self.image_key = None
                self.predictor.set_image(np.asarray(image))
                self.image_key = key
            mask, score = self.predictor.predict(points, labels)
            if (
                np.shape(mask) != (image.height, image.width)
                or np.asarray(mask).dtype != np.bool_
                or not np.isfinite(score)
            ):
                raise RuntimeError(
                    "Predictor returned invalid mask dimensions, type or score"
                )
            shape = mask_shape(mask)
        return {"shapes": [] if shape is None else [shape]}
