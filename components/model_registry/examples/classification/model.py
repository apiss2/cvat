# SPDX-License-Identifier: MIT
"""Interface demo only: an ONNX Identity graph plus mean-brightness classification.

This is not a trained classifier. Its score is a brightness-based test value,
not a calibrated probability. Replace this preprocessing/postprocessing for your
own ONNX classification model.
"""
import numpy as np
from registry.sdk import ModelBase, ModelContext, PredictParams, Tag


class Model(ModelBase):
    def load(self, context: ModelContext) -> None:
        if len(context.manifest.labels) != 2 or any(label.type != "tag" for label in context.manifest.labels):
            raise ValueError("This demo expects two tag labels in dark/bright order")
        self.class_ids = [label.id for label in context.manifest.labels]
        self.session = context.create_session("model.onnx")
        self.input_name = self.session.get_inputs()[0].name

    def predict(self, image: np.ndarray, params: PredictParams) -> list[Tag]:
        tensor = image.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        output = self.session.run(None, {self.input_name: tensor})[0]
        brightness = float(np.mean(output))
        if not np.isfinite(brightness) or not 0 <= brightness <= 1:
            raise ValueError("Expected normalized brightness in [0, 1]")
        index = int(brightness >= 0.5)
        return [Tag(self.class_ids[index], brightness if index else 1.0 - brightness)]
