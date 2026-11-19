# SPDX-License-Identifier: MIT

import argparse
import json
import math
import sys
from pathlib import Path

from .common import CriticalError


def _id(value: str, minimum: int) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer ID or index") from exc
    if result < minimum:
        raise argparse.ArgumentTypeError(f"expected an integer >= {minimum}")
    return result


def positive_id(value: str) -> int:
    return _id(value, 1)


def nonnegative_int(value: str) -> int:
    return _id(value, 0)


def finite_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a finite number") from exc
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("expected a finite number")
    return result


def print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))


def response_json(response):
    return json.loads(response.data)


def read_json_object(filename: str) -> dict:
    try:
        text = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8")
        data = json.loads(text, parse_constant=_reject_nonfinite)
    except (OSError, ValueError) as exc:
        raise CriticalError(f"Cannot read JSON from {filename}: {exc}") from exc
    if not isinstance(data, dict):
        raise CriticalError("Expected a JSON object")
    return data


def _reject_nonfinite(value):
    raise ValueError(f"Invalid JSON number: {value}")
