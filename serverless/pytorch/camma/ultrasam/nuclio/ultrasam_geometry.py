# SPDX-License-Identifier: MIT
"""CVAT pixel coordinates and zero-first, row-major RLE with inclusive bounds."""

import numpy as np
from ultrasam_constants import MAX_POINTS
from ultrasam_protocol import ProtocolError


def point_array(value, name, size):
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > MAX_POINTS:
        raise ProtocolError(f"{name} must contain at most {MAX_POINTS} [x, y] points")
    if not value:
        return np.empty((0, 2), dtype=np.float32)
    # Reject booleans and numeric strings instead of silently coercing them.
    for pair in value:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(
                isinstance(number, bool) or not isinstance(number, (int, float))
                for number in pair
            )
        ):
            raise ProtocolError(f"{name} must contain numeric [x, y] coordinates")
    try:
        points = np.asarray(value, dtype=np.float64)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ProtocolError(f"Invalid {name}") from exc
    if not np.isfinite(points).all():
        raise ProtocolError(f"{name} must contain finite coordinates")
    width, height = size
    if (points < 0).any() or (points >= np.array([width, height])).any():
        raise ProtocolError(f"{name} lies outside the image")
    return points.astype(np.float32)


def mask_shape(mask):
    mask = np.asarray(mask)
    if mask.ndim != 2 or mask.dtype != np.bool_:
        raise ValueError("Expected a two-dimensional boolean mask")
    rows, cols = np.nonzero(mask)
    if not len(rows):
        return None
    x0, x1 = int(cols.min()), int(cols.max())
    y0, y1 = int(rows.min()), int(rows.max())
    flat = mask[y0 : y1 + 1, x0 : x1 + 1].ravel(order="C")
    boundaries = np.r_[0, np.flatnonzero(flat[1:] != flat[:-1]) + 1, flat.size]
    runs = np.diff(boundaries).astype(int).tolist()
    if flat[0]:
        runs.insert(0, 0)
    return {"type": "mask", "points": runs + [x0, y0, x1, y1], "attributes": []}
