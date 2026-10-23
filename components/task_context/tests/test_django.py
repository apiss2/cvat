# SPDX-License-Identifier: MIT
"""Run the isolated Django/real-OPA suite in its own process."""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


class DjangoContextTests(unittest.TestCase):
    def test_django_and_native_permissions(self):
        opa = os.environ.get("CVAT_TASK_CONTEXT_OPA") or shutil.which("opa")
        if not opa:
            self.skipTest(
                "Set CVAT_TASK_CONTEXT_OPA to an OPA 1.x binary to run the API/database suite"
            )
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("django_cases.py")), opa],
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
