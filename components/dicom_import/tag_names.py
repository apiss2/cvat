# SPDX-License-Identifier: MIT
"""Stable attribute names, independent of the installed pydicom dictionary."""

import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _dictionary():
    snapshot = json.loads(
        Path(__file__).with_name("dicom_keywords.json").read_text(encoding="utf-8")
    )
    tags = snapshot["tags"]
    repeaters = [
        (re.compile(pattern.upper().replace("X", "[0-9A-F]")), keyword)
        for pattern, keyword in snapshot["repeaters"].items()
        if keyword
    ]
    return tags, repeaters


def attribute_name(tag: int) -> str:
    number = f"{tag:08X}"
    # Odd groups are private, including ones that happen to fit a repeating mask.
    if (tag >> 16) & 1:
        return number
    tags, repeaters = _dictionary()
    if number in tags:
        return tags[number] or number
    for pattern, keyword in repeaters:
        if pattern.fullmatch(number):
            # One repeating Keyword identifies several concrete tags. Keep each
            # value distinct instead of overwriting one overlay with another.
            return f"{keyword}_{number}"
    return number
