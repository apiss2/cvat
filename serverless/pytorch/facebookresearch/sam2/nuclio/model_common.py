# SPDX-License-Identifier: MIT
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
import torch

MODEL_CONFIG = os.getenv("SAM2_CONFIG", "configs/sam2.1/sam2.1_hiera_s.yaml")
CHECKPOINT = os.getenv("SAM2_CHECKPOINT", "/opt/nuclio/checkpoints/sam2.1_hiera_small.pt")

def require_gpu():
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required. Configure NVIDIA Container Toolkit and a GPU for this function.")
    if not Path(CHECKPOINT).is_file():
        raise RuntimeError("SAM2 checkpoint is missing: " + CHECKPOINT)

@contextmanager
def inference_context():
    # Do not silently fall back to CPU or fp16 on older GPUs.
    autocast = (torch.autocast("cuda", dtype=torch.bfloat16)
                if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else nullcontext())
    with torch.inference_mode(), autocast:
        yield
