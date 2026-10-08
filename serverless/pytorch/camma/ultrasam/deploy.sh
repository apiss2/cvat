#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
exec "$ROOT/components/extensions/cvatctl" deploy-ultrasam "$@"
