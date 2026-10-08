# SPDX-License-Identifier: MIT
"""Pinned upstream identity; paths are private to the Nuclio image."""

from pathlib import Path

UPSTREAM_COMMIT = "ff3157b1fca8b1d963d9138372768e1fecad71e9"
UPSTREAM_ROOT = Path("/opt/ultrasam")
CHECKPOINT = Path("/opt/nuclio/checkpoints/UltraSam.pth")
CHECKPOINT_URL = "https://s3.unistra.fr/camma_public/github/ultrasam/UltraSam.pth"
# SHA256 of the official 381,156,023-byte checkpoint; verified before unpickling.
CHECKPOINT_SHA256 = "d7c223dd03f56b0b77cd246aa3edfae651e99f0ee2dde2e8c50eda7b21fa8a0c"
MAX_POINTS = 128
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024
