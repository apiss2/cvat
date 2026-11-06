# SPDX-License-Identifier: MIT
"""Shared, dependency-free limits for uploads, extraction and deployment checks."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from . import DicomImportError

DEFAULTS = {
    "CVAT_DICOM_MAX_UPLOAD_BYTES": str(1024**4),
    "CVAT_DICOM_MAX_UPLOAD_FILES": "100000",
    "CVAT_DICOM_MAX_EXPANDED_BYTES": str(1024**4),
    "CVAT_DICOM_MAX_FRAME_BYTES": str(1024**3),
    "CVAT_DICOM_IMPORT_TIMEOUT": "86400",
}


def limits(values: Mapping[str, str] | None = None) -> dict[str, int]:
    """Use integers that both Python and browser byte counters represent exactly."""
    configured = os.environ if values is None else values
    result = {}
    for key, default in DEFAULTS.items():
        value = configured.get(key, default)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
            raise DicomImportError(f"{key} must be a positive integer")
        number = int(value)
        if not 1 <= number <= 2**53 - 1:
            raise DicomImportError(f"{key} must be between 1 and 9007199254740991")
        result[key.removeprefix("CVAT_DICOM_").lower()] = number
    return result
