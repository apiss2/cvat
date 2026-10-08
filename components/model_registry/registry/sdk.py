# SPDX-License-Identifier: MIT
"""The only interface model authors need to implement. Executed in workers only."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .schema import Manifest


@dataclass(frozen=True)
class Box:
    class_id: int
    score: float
    # Original input image pixel coordinates. Right/bottom are exclusive edges.
    xyxy: tuple[float, float, float, float]


@dataclass(frozen=True)
class Mask:
    """Compatibility adapter for stored packages; new models return (N, H, W)."""
    class_id: int
    score: float
    # Full input image H x W; bool or integer 0/1. One object per Mask.
    bitmap: np.ndarray


@dataclass(frozen=True)
class PredictParams:
    request_id: str


@dataclass(frozen=True)
class ModelContext:
    package_dir: Path
    manifest: Manifest
    providers: tuple[str, ...]

    def create_session(self, weight: str) -> Any:
        """No custom operators or model-specific dependency installation."""
        if weight not in self.manifest.weights:
            raise ValueError(f"Undeclared weight: {weight}")
        import onnxruntime as ort
        missing = set(self.providers) - set(ort.get_available_providers())
        if missing:
            raise RuntimeError(f"ONNX Runtime providers unavailable: {sorted(missing)}")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(self.package_dir / weight), sess_options=options, providers=list(self.providers)
        )
        if not set(self.providers).issubset(session.get_providers()):
            raise RuntimeError("Requested provider failed to initialize; refusing silent CPU fallback")
        return session


class ModelBase(abc.ABC):
    @abc.abstractmethod
    def load(self, context: ModelContext) -> None:
        """Initialize sessions once for each loaded revision."""
        raise NotImplementedError

    @abc.abstractmethod
    def predict(self, image: np.ndarray, params: PredictParams) -> np.ndarray | Sequence[Box]:
        """Input: RGB uint8 H x W x 3, including CVAT's optional cropped ROI.

        Segmentation: return bool/integer 0-or-1 ndarray (N, H, W), in manifest
        label order and original input dimensions. Perform binarization here.
        Detection: return Box values after your own confidence/NMS decisions.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Optional cleanup. This is not guaranteed after a timeout or process crash."""
