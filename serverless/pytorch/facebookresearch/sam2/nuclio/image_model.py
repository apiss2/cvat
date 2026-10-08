# SPDX-License-Identifier: MIT
import hashlib
from threading import Lock
import numpy as np
from geometry import mask_shape, point_array
from model_common import inference_context
from protocol import ProtocolError, decode_image

class ImageInteractor:
    def __init__(self, predictor):
        self.predictor = predictor
        self.image_key = None
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
        points = np.concatenate((positive, negative))
        labels = np.r_[np.ones(len(positive), dtype=np.int32), np.zeros(len(negative), dtype=np.int32)]
        key = (image.size, hashlib.sha256(image.tobytes()).digest())
        with self.lock, inference_context():
            # One image embedding is cached; no cross-request prompt/mask history is reused.
            if self.image_key != key:
                self.image_key = None
                self.predictor.set_image(np.asarray(image))
                self.image_key = key
            masks, scores, _ = self.predictor.predict(
                point_coords=points if len(points) else None,
                point_labels=labels if len(points) else None,
                box=bbox.reshape(4) if len(bbox) else None,
                multimask_output=True,
            )
            if (np.ndim(masks) != 3 or np.shape(masks)[1:] != (image.height, image.width) or
                len(masks) == 0 or np.shape(scores) != (len(masks),) or not np.isfinite(scores).all()):
                raise RuntimeError("Predictor returned invalid mask dimensions or scores")
            shape = mask_shape(masks[int(np.argmax(scores))])
        return {"shapes": [shape] if shape is not None else []}
