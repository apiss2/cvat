# SPDX-License-Identifier: MIT
"""Cross-language comparisons using native JavaScript operators as an oracle.

json-logic-js 2.0.2 implements these operators with the same JS primitives.
This is not a test of the query-builder UI or a replacement for browser tests.
"""
import json
import shutil
import subprocess
import pytest

from test_filtered_export_selection import selection

SCRIPT = r"""
const fs = require('node:fs');
const operations = {
  '==': (a,b) => a == b, '===': (a,b) => a === b,
  '!=': (a,b) => a != b, '!==': (a,b) => a !== b,
  '<': (a,b) => a < b, '<=': (a,b) => a <= b,
  '>': (a,b) => a > b, '>=': (a,b) => a >= b,
  'in': (a,b) => !b || typeof b.indexOf === 'undefined' ? false : b.indexOf(a) !== -1,
};
console.log(JSON.stringify(JSON.parse(fs.readFileSync(0, 'utf8')).map(([op,a,b]) => operations[op](a,b))));
"""


@pytest.mark.skipif(
    not (shutil.which("node")), reason="Node.js is needed for the JavaScript comparison oracle"
)
class TestJavaScriptParity:
    def compare(self, cases, subtests):
        # Compute the oracle in one Node process, then report each runtime result separately.
        response = subprocess.run(
            ["node", "-e", SCRIPT],
            input=json.dumps(cases),
            text=True,
            capture_output=True,
            check=True,
            timeout=10,
        )
        expected = json.loads(response.stdout)
        for (op, a, b), result in zip(cases, expected):
            with subtests.test(op=op, left=a, right=b):
                assert selection.evaluate({op: [a, b]}, {}) == result

    def test_scalar_comparisons(self, subtests):
        values = [
            None,
            False,
            True,
            0,
            1,
            -2,
            1.5,
            "",
            " ",
            "0",
            "1",
            "1.0",
            "01",
            "-2",
            "horse",
            "false",
            "NaN",
            "Infinity",
            "0x10",
            "1_0",
            "\ufeff",
            "\x1c",
            "\U00010000",
            "\ue000",
        ]
        self.compare(
            [
                [op, a, b]
                for op in ("==", "!=", "===", "!==", "<", "<=", ">", ">=")
                for a in values
                for b in values
            ],
            subtests,
        )

    def test_membership(self, subtests):
        self.compare(
            [
                ["in", a, b]
                for a in (None, False, True, 0, 1, 1.0, "", "ready")
                for b in (None, "", "ready", "null", "1", [], [1, "ready"], [True])
            ],
            subtests,
        )
