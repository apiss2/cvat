# SPDX-License-Identifier: MIT
"""Protocol demonstration, not a trained object detector."""
import numpy as np
from registry.sdk import Box, ModelBase, ModelContext, PredictParams


class Model(ModelBase):
    def load(self, context: ModelContext) -> None:
        self.session = context.create_session("model.onnx")

    def predict(self, image: np.ndarray, params: PredictParams):
        tensor = image.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        prediction = self.session.run(None, {"image": tensor})[0]
        # Perform all confidence thresholds / NMS in this method.
        ys, xs = np.nonzero(prediction[0].mean(axis=0) > 0.5)
        if not len(xs):
            return []
        return [Box(class_id=0, score=0.9, xyxy=(
            float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)
        ))]
