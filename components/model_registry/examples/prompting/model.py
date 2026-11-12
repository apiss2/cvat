# SPDX-License-Identifier: MIT
"""Identity ONNX plus prompted bright regions; not a trained SAM model."""
import cv2
import numpy as np

from registry.sdk import ModelBase, ModelContext, PredictParams


class Model(ModelBase):
    def load(self, context: ModelContext) -> None:
        self.session = context.create_session("model.onnx")

    def predict(self, image: np.ndarray, params: PredictParams) -> np.ndarray:
        tensor = image.transpose(2, 0, 1)[None].astype(np.float32) / 255
        output = self.session.run(None, {self.session.get_inputs()[0].name: tensor})[0]
        foreground = output[0].mean(axis=0) > 0.5
        _, components = cv2.connectedComponents(foreground.astype(np.uint8), connectivity=4)
        selected = np.zeros(image.shape[:2], dtype=np.bool_)
        if params.obj_bbox is not None:
            (x1, y1), (x2, y2) = params.obj_bbox
            selected[int(y1):int(np.ceil(y2)), int(x1):int(np.ceil(x2))] = True
            selected &= foreground
        for x, y in params.pos_points:
            component = components[int(y), int(x)]
            if component:
                selected |= components == component
        for x, y in params.neg_points:
            component = components[int(y), int(x)]
            if component:
                selected &= components != component
        return selected
