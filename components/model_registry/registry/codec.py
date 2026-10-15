# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import binascii
import io
import math
import numbers
import warnings

import numpy as np
from PIL import Image

from .schema import Manifest, PolygonSettings
from .sdk import Box, Mask

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_OBJECTS = 2000
MAX_MASK_VALUES = 24_000_000
MAX_POLYGON_VERTICES = 4096
MAX_TOTAL_POLYGON_VERTICES = 100_000
GEOMETRY_EPSILON = 1e-9


def decode_image(text: str) -> np.ndarray:
    if len(text) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ValueError("encoded image exceeds 32 MiB")
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("image must be standard base64 without a data URL prefix") from exc
    return decode_image_bytes(raw)


def decode_image_bytes(raw: bytes) -> np.ndarray:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image is empty or exceeds 32 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ("PNG", "JPEG"):
                    raise ValueError("only PNG and JPEG are accepted")
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("image exceeds 16 million pixels")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("animated input is not supported")
                # Deliberately do not EXIF-transpose: CVAT coordinates refer to decoded pixels.
                return np.array(image.convert("RGB"), dtype=np.uint8, copy=True)
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Image could not be decoded safely as PNG or JPEG") from exc


def polygon_area(points: np.ndarray) -> float:
    following = np.roll(points, -1, axis=0)
    return abs(float(np.sum(points[:, 0] * following[:, 1] - following[:, 0] * points[:, 1]))) / 2.0


def is_simple_polygon(points: np.ndarray) -> bool:
    """Reject nonadjacent crossings/touches, including overlapping collinear edges.

    Candidate segments are pruned by bounding box before vectorized orientation
    tests. Inputs are bounded by MAX_POLYGON_VERTICES in both worker and manager.
    """
    if len(points) < 3 or len(np.unique(points, axis=0)) != len(points):
        return False
    ends = np.roll(points, -1, axis=0)
    lower = np.minimum(points, ends)
    upper = np.maximum(points, ends)
    for index in range(len(points) - 2):
        first = index + 2
        last = len(points) - (1 if index == 0 else 0)
        if first >= last:
            continue
        candidates = np.all(lower[first:last] <= upper[index] + GEOMETRY_EPSILON, axis=1)
        candidates &= np.all(upper[first:last] >= lower[index] - GEOMETRY_EPSILON, axis=1)
        if not candidates.any():
            continue
        a, b = points[index], ends[index]
        c, d = points[first:last][candidates], ends[first:last][candidates]
        ab = b - a
        cd = d - c
        cross_c = ab[0] * (c[:, 1] - a[1]) - ab[1] * (c[:, 0] - a[0])
        cross_d = ab[0] * (d[:, 1] - a[1]) - ab[1] * (d[:, 0] - a[0])
        cross_a = cd[:, 0] * (a[1] - c[:, 1]) - cd[:, 1] * (a[0] - c[:, 0])
        cross_b = cd[:, 0] * (b[1] - c[:, 1]) - cd[:, 1] * (b[0] - c[:, 0])
        crossing = (np.minimum(cross_c, cross_d) <= GEOMETRY_EPSILON)
        crossing &= np.maximum(cross_c, cross_d) >= -GEOMETRY_EPSILON
        crossing &= np.minimum(cross_a, cross_b) <= GEOMETRY_EPSILON
        crossing &= np.maximum(cross_a, cross_b) >= -GEOMETRY_EPSILON
        if crossing.any():
            return False
    return True


def _sample_contour(contour: np.ndarray, settings: PolygonSettings) -> np.ndarray | None:
    points = contour.reshape(-1, 2).astype(np.float64)
    if len(points) < 3:
        return None
    following = np.roll(points, -1, axis=0)
    lengths = np.linalg.norm(following - points, axis=1)
    if (lengths <= GEOMETRY_EPSILON).any():
        return None
    perimeter = float(lengths.sum())
    step = max(settings.min_distance_px, perimeter * settings.spacing_percent / 100.0)
    if step > 0:
        count = int(math.floor(perimeter / step))
        if count < 3:
            return None
        if count > MAX_POLYGON_VERTICES:
            raise ValueError("polygon exceeds 4096 vertices; increase polygon spacing settings")
        # floor count keeps the actual equal arc interval at least the requested step.
        positions = np.arange(count, dtype=np.float64) * (perimeter / count)
        cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
        segments = np.searchsorted(cumulative, positions, side="right") - 1
        fractions = (positions - cumulative[segments]) / lengths[segments]
        points = points[segments] + (following[segments] - points[segments]) * fractions[:, None]
    elif len(points) > MAX_POLYGON_VERTICES:
        raise ValueError("polygon exceeds 4096 vertices; increase polygon spacing settings")

    # Arc distance does not guarantee a chord distance near a corner. Delete short
    # adjacent chords until the closed ring (including last -> first) satisfies it.
    while len(points) >= 3:
        lengths = np.linalg.norm(np.roll(points, -1, axis=0) - points, axis=1)
        short = np.flatnonzero(lengths + GEOMETRY_EPSILON < settings.min_distance_px)
        if not len(short):
            break
        edge = int(short[0])
        points = np.delete(points, edge + 1 if edge < len(points) - 1 else edge, axis=0)
    if len(points) < 3:
        return None
    area = polygon_area(points)
    if area <= GEOMETRY_EPSILON or area + GEOMETRY_EPSILON < settings.min_area_px:
        return None
    if not is_simple_polygon(points):
        return None
    return points


def _check_bitmap(bitmap, h: int, w: int) -> np.ndarray:
    if not isinstance(bitmap, np.ndarray) or bitmap.shape != (h, w):
        raise ValueError("mask must be a numpy array with the input image H x W")
    if bitmap.dtype.kind not in ("b", "u", "i") or not np.all((bitmap == 0) | (bitmap == 1)):
        raise ValueError("mask pixels must be bool or integer 0/1, not 0/255 or probabilities")
    return bitmap


def _polygons(bitmap: np.ndarray, settings: PolygonSettings):
    # Imported only for inference. The control plane requires no OpenCV and never
    # runs uploaded Python or computes model predictions.
    import cv2

    count, components, stats, _ = cv2.connectedComponentsWithStats(
        bitmap.astype(np.uint8), connectivity=4, ltype=cv2.CV_32S,
    )
    for index in range(1, count):
        left, top, width, height, pixel_area = map(int, stats[index])
        if pixel_area < settings.min_area_px:
            continue
        component = (components[top:top + height, left:left + width] == index).astype(np.uint8)
        contours, _ = cv2.findContours(
            component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE, offset=(left, top),
        )
        for contour in contours:
            points = _sample_contour(contour, settings)
            if points is not None:
                yield points


def _class_and_score(item, labels):
    if isinstance(item.class_id, (bool, np.bool_)) or not isinstance(item.class_id, numbers.Integral):
        raise ValueError("class_id must be an integer")
    if item.class_id not in labels:
        raise ValueError(f"undeclared class_id: {item.class_id}")
    if isinstance(item.score, (bool, np.bool_)) or not isinstance(item.score, numbers.Real):
        raise ValueError("score must be a finite number in [0, 1]")
    score = float(item.score)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("score must be a finite number in [0, 1]")
    return labels[item.class_id], score


def encode_results(results, manifest: Manifest, shape: tuple[int, ...]) -> list[dict]:
    labels = {x.id: x for x in manifest.labels}
    h, w = shape[:2]
    output: list[dict] = []
    total_vertices = 0

    def add_bitmap(bitmap, label, score):
        nonlocal total_vertices
        for points in _polygons(bitmap, manifest.polygon):
            total_vertices += len(points)
            if total_vertices > MAX_TOTAL_POLYGON_VERTICES:
                raise ValueError("combined polygons exceed 100000 vertices")
            if len(output) >= MAX_OBJECTS:
                raise ValueError("predict output exceeds 2000 objects")
            output.append({"label": label.name, "confidence": score, "type": "polygon", "points": points.ravel().tolist()})

    if isinstance(results, np.ndarray):
        if any(label.type != "polygon" for label in manifest.labels):
            raise ValueError("segmentation arrays require polygon labels")
        if results.shape != (len(manifest.labels), h, w):
            raise ValueError("segmentation must return (N, H, W) in manifest label order and original image dimensions")
        if results.size > MAX_MASK_VALUES:
            raise ValueError("segmentation array exceeds 24000000 elements")
        if results.dtype.kind not in ("b", "u", "i") or not np.all((results == 0) | (results == 1)):
            raise ValueError("segmentation pixels must be bool or integer 0/1, not 0/255 or probabilities")
        for label, bitmap in zip(manifest.labels, results):
            add_bitmap(bitmap, label, 1.0)
        return output

    if not isinstance(results, (list, tuple)) or len(results) > MAX_OBJECTS:
        raise ValueError("predict must return a binary (N, H, W) numpy array or at most 2000 Box values")
    mask_values = 0
    for item in results:
        if not isinstance(item, (Box, Mask)):
            raise TypeError("detection results must be registry.sdk.Box (legacy segmentation Mask is also accepted)")
        label, score = _class_and_score(item, labels)
        if isinstance(item, Mask):
            # Compatibility for stored packages. New segmentation models return
            # an ndarray. Neither legacy Mask scores nor Box scores are filtered.
            if label.type != "polygon":
                raise ValueError("Mask class_id must refer to a polygon label")
            bitmap = _check_bitmap(item.bitmap, h, w)
            mask_values += bitmap.size
            if mask_values > MAX_MASK_VALUES:
                raise ValueError("segmentation masks exceed 24000000 elements")
            add_bitmap(bitmap, label, score)
            continue
        if label.type != "rectangle":
            raise ValueError("Box class_id must refer to a rectangle label")
        if not isinstance(item.xyxy, (tuple, list)) or len(item.xyxy) != 4:
            raise ValueError("Box.xyxy requires exactly four numbers")
        if any(isinstance(x, (bool, np.bool_)) or not isinstance(x, numbers.Real) for x in item.xyxy):
            raise ValueError("Box.xyxy contains a nonnumeric coordinate")
        x1, y1, x2, y2 = map(float, item.xyxy)
        if not all(math.isfinite(x) for x in (x1, y1, x2, y2)):
            raise ValueError("box contains NaN or infinity")
        if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
            raise ValueError("box must be nonempty and in original image coordinates")
        output.append({"label": label.name, "confidence": score, "type": "rectangle", "points": [x1, y1, x2, y2]})
    return output
