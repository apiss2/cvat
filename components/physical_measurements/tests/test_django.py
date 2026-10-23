# SPDX-License-Identifier: MIT
import os
import subprocess
import sys
from pathlib import Path


def test_persistence_and_api_in_isolated_django_process():
    """Use real Django ORM and DRF without changing the caller's Django settings."""
    script = Path(__file__).with_name("django_harness.py")
    root = script.parents[3]
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
