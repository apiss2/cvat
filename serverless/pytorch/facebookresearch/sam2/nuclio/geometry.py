# SPDX-License-Identifier: MIT
"""CVAT pixel coordinates and row-major, zero-first RLE (NOT COCO RLE)."""
import math
import numpy as np
from PIL import Image, ImageDraw
from protocol import ProtocolError

MAX_POINTS = 10000

def point_array(value, name, size, *, allow_empty=True, edge=False):
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > MAX_POINTS:
        raise ProtocolError(f"{name} must be a bounded array of [x, y] points")
    if not value:
        if allow_empty:
            return np.empty((0, 2), dtype=np.float32)
        raise ProtocolError(f"{name} cannot be empty")
    try:
        points = np.asarray(value, dtype=np.float32)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"Invalid {name}") from exc
    if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
        raise ProtocolError(f"{name} must contain finite [x, y] coordinates")
    width, height = size
    limits = np.array([width, height])
    outside = (points > limits).any() if edge else (points >= limits).any()
    if (points < 0).any() or outside:
        raise ProtocolError(f"{name} lies outside the image")
    return points

def mask_shape(mask):
    mask = np.asarray(mask, dtype=np.bool_)
    if mask.ndim != 2:
        raise ValueError("Expected a two-dimensional mask")
    rows, cols = np.nonzero(mask)
    if not len(rows):
        return None
    x0, x1, y0, y1 = int(cols.min()), int(cols.max()), int(rows.min()), int(rows.max())
    flat = mask[y0:y1 + 1, x0:x1 + 1].ravel(order="C").astype(np.uint8)
    boundaries = np.r_[0, np.flatnonzero(flat[1:] != flat[:-1]) + 1, flat.size]
    runs = np.diff(boundaries).astype(int).tolist()
    if flat[0]:
        runs.insert(0, 0)
    return {"type": "mask", "points": runs + [x0, y0, x1, y1], "attributes": []}

def seed_mask(shape, size):
    if not isinstance(shape, dict) or shape.get("type") not in ("polygon", "mask"):
        raise ProtocolError("Tracker seeds must be typed polygons or masks")
    if shape["type"] == "polygon":
        return polygon_mask(shape, size)
    points = shape.get("points")
    if (not isinstance(points, list) or not 5 <= len(points) <= 2_000_004
            or any(type(value) not in (int, float) or value < 0 or value > 2**53 - 1
                   or not math.isfinite(value) or value != int(value) for value in points)):
        raise ProtocolError("Mask points must be bounded nonnegative integer CVAT RLE")
    # CVAT serializes point arrays as floats; accept integral values such as 9.0.
    points = [int(value) for value in points]
    left, top, right, bottom = points[-4:]
    width, height = size
    if right < left or bottom < top or right >= width or bottom >= height:
        raise ProtocolError("Mask bounds lie outside the image")
    runs = points[:-4]
    area = (right - left + 1) * (bottom - top + 1)
    if sum(runs) != area:
        raise ProtocolError("Mask RLE length does not match its bounds")
    cropped = np.zeros(area, dtype=np.bool_)
    offset = 0
    for index, run in enumerate(runs):
        if index % 2:
            cropped[offset:offset + run] = True
        offset += run
    if not cropped.any():
        raise ProtocolError("Mask does not cover any pixels")
    result = np.zeros((height, width), dtype=np.bool_)
    result[top:bottom + 1, left:right + 1] = cropped.reshape(bottom - top + 1, right - left + 1)
    return result


def polygon_mask(shape, size):
    if not isinstance(shape, dict) or shape.get("type") != "polygon":
        raise ProtocolError("Tracker seeds must be typed polygons")
    raw = shape.get("points")
    if not isinstance(raw, list) or len(raw) < 6 or len(raw) % 2 or len(raw) > MAX_POINTS * 2:
        raise ProtocolError("A polygon needs at least three [x, y] pairs")
    points = point_array([raw[i:i + 2] for i in range(0, len(raw), 2)], "polygon", size, edge=True)
    # Reject degenerate contours before allocating the seed image.
    area2 = abs(np.dot(points[:, 0], np.roll(points[:, 1], 1)) -
                np.dot(points[:, 1], np.roll(points[:, 0], 1)))
    if area2 < 1:
        raise ProtocolError("Polygon has zero or sub-pixel area")
    seed = Image.new("L", size, 0)
    ImageDraw.Draw(seed).polygon([tuple(map(float, p)) for p in points], fill=1)
    result = np.asarray(seed, dtype=np.bool_)
    if not result.any():
        raise ProtocolError("Polygon does not cover any pixels")
    return result

def polygon_shape(mask, epsilon=1.0):
    # CVAT polygon tracks cannot represent holes or disconnected components.
    # Keep the largest external contour. Full masks are preserved by mask_shape.
    import cv2
    contours, _ = cv2.findContours(np.asarray(mask, dtype=np.uint8), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < 1:
        return None
    simplified = cv2.approxPolyDP(contour, epsilon, True)
    if len(simplified) >= 3:
        contour = simplified
    # Bound response size for pathological, jagged contours.
    while len(contour) > MAX_POINTS:
        epsilon = max(1, epsilon * 2)
        contour = cv2.approxPolyDP(contour, epsilon, True)
    if len(contour) < 3:
        return None
    return {"type": "polygon", "points": contour.reshape(-1).astype(float).tolist()}
