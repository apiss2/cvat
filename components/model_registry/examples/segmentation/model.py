# SPDX-License-Identifier: MIT
"""Protocol demonstration, not a trained segmentation model."""
import numpy as np
from registry.sdk import ModelBase, ModelContext, PredictParams


class Model(ModelBase):
    def load(self, context: ModelContext) -> None:
        self.session = context.create_session("model.onnx")

    def predict(self, image: np.ndarray, params: PredictParams):
        tensor = image.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        prediction = self.session.run(None, {"image": tensor})[0]
        bitmap = prediction[0].mean(axis=0) > 0.5
        # Binarization is model-specific and is decided here. The first axis
        # follows manifest.labels order; the spatial axes match the input image.
        return bitmap[None]
