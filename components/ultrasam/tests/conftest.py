# SPDX-License-Identifier: MIT
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "serverless/pytorch/camma/ultrasam/nuclio"))
