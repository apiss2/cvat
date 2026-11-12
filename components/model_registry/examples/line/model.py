# SPDX-License-Identifier: MIT
"""Protocol demonstration, not a trained line detector."""
import cv2
import numpy as np
from registry.sdk import ModelBase, ModelContext, PredictParams, Polyline


class Model(ModelBase):
    def load(self, context: ModelContext) -> None:
        self.session = context.create_session("model.onnx")

    def predict(self, image: np.ndarray, params: PredictParams):
        tensor = image.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        prediction = self.session.run(None, {"image": tensor})[0]
        foreground = (prediction[0].mean(axis=0) > 0.5).astype(np.uint8)
        count, _, stats, _ = cv2.connectedComponentsWithStats(foreground, connectivity=4)
        lines = []
        # Any number of lines per image: two for each bright region.
        for left, top, width, height, _ in stats[1:count].tolist():
            right, bottom = left + width, top + height
            middle = top + height / 2
            lines.append(Polyline(class_id=0, score=0.9, points=[(left, middle), (right, middle)]))
            # An open outline, left side omitted, shows a multi-vertex polyline.
            lines.append(Polyline(class_id=0, score=0.8, points=[
                (left, top), (right, top), (right, bottom), (left, bottom),
            ]))
        return lines
