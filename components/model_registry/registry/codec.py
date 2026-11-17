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
from .sdk import Box, Mask, Polyline, Tag

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_OBJECTS = 2000
MAX_MASK_VALUES = 24_000_000
MAX_POLYGON_VERTICES = 4096
MAX_TOTAL_POLYGON_VERTICES = 100_000
GEOMETRY_EPSILON = 1e-9
# Search range for rerouting an edge shorter than min_distance_px: contour points
# on either side of each replaced vertex, and replaced vertices on either side.
REROUTE_WINDOW = 12
REROUTE_DEPTH = 3


class PolygonLimitError(ValueError):
    """Polygon conversion needs more vertices or objects than the limits allow."""


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


def _segment_distance(points: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    """Distance from each point to the finite segment between two distinct points."""
    direction = end - start
    fraction = np.clip(((points - start) @ direction) / (direction @ direction), 0., 1.)
    return np.linalg.norm(points - start - fraction[:, None] * direction, axis=1)


def _simple_loops(points: np.ndarray):
    """Split a closed walk through pixel centers wherever it revisits a pixel.

    A contour revisits a pixel where the region is one pixel wide, and such a
    walk can never be a simple polygon. Cutting it at every revisit yields each
    part that encloses area as its own loop. The one-pixel-wide bridges and
    whiskers between them enclose no area and are left out.
    """
    path: list[tuple] = []
    position: dict[tuple, int] = {}
    for point in map(tuple, points.tolist()):
        start = position.get(point)
        if start is None:
            position[point] = len(path)
            path.append(point)
            continue
        if len(path) - start >= 3:
            yield np.asarray(path[start:], dtype=np.float64)
        for removed in path[start + 1:]:
            del position[removed]
        del path[start + 1:]
    if len(path) >= 3:
        yield np.asarray(path, dtype=np.float64)


def _arc_error(doubled: np.ndarray, a: int, b: int) -> float:
    """Largest distance from the contour points a..b, walking forward, to the edge a-b.

    The contour is passed twice in a row so that an arc may wrap past its end.
    """
    n = len(doubled) // 2
    arc = doubled[a:(b if b > a else b + n) + 1]
    return float(_segment_distance(arc, doubled[a], doubled[b]).max())


def _split_contour(doubled: np.ndarray, tolerance: float) -> list[int]:
    """Douglas-Peucker on a closed contour, measured against finite edges."""
    n = len(doubled) // 2
    original = doubled[:n]
    far = int(np.argmax(np.linalg.norm(original - original[0], axis=1)))
    keep = {0, far}
    pending = [(0, far), (far, n)]
    while pending:
        a, b = pending.pop()
        if b - a < 2:
            continue
        distance = _segment_distance(original[a + 1:b], original[a], original[b % n])
        worst = int(np.argmax(distance))
        if distance[worst] > tolerance:
            middle = a + 1 + worst
            keep.add(middle)
            if len(keep) > MAX_POLYGON_VERTICES:
                raise PolygonLimitError("polygon exceeds 4096 vertices; increase polygon approximation error")
            pending += [(a, middle), (middle, b)]
    indices = sorted(keep)
    # The two starting points were kept unconditionally; drop one that lies on a straight run.
    for anchor in (0, far):
        at = indices.index(anchor)
        if len(indices) > 3 and _arc_error(doubled, indices[at - 1], indices[(at + 1) % len(indices)]) <= tolerance:
            del indices[at]
    return indices


def _reroute(doubled: np.ndarray, settings: PolygonSettings, first: int, last: int,
             replaced: list[int]) -> list[int] | None:
    """Fewest contour points that join two retained vertices by valid edges.

    Candidates are the contour points near the replaced vertices. Every edge of
    the returned route satisfies both the error and the distance limit.
    """
    n = len(doubled) // 2
    tolerance = settings.max_error_px + GEOMETRY_EPSILON
    span = (last - first) % n
    offsets = {0, span}
    for vertex in replaced:
        center = (vertex - first) % n
        offsets.update(range(max(1, center - REROUTE_WINDOW), min(span, center + REROUTE_WINDOW + 1)))
    ordered = sorted(offsets)
    # offset from first -> (vertices used, largest error, previous offset)
    best = {0: (0, 0., 0)}
    for at, target in enumerate(ordered[1:], 1):
        b = (first + target) % n
        options = []
        for source in ordered[:at]:
            if source not in best:
                continue
            a = (first + source) % n
            if np.linalg.norm(doubled[b] - doubled[a]) + GEOMETRY_EPSILON < settings.min_distance_px:
                continue
            error = _arc_error(doubled, a, b)
            if error <= tolerance:
                options.append((best[source][0] + 1, max(best[source][1], error), source))
        if options:
            best[target] = min(options)
    if span not in best:
        return None
    path = []
    offset = best[span][2]
    while offset:
        path.append((first + offset) % n)
        offset = best[offset][2]
    return path


def _sample_contour(contour: np.ndarray, settings: PolygonSettings) -> np.ndarray | None:
    """Approximate a closed contour by a subset of its own points.

    Douglas-Peucker splitting keeps every contour point within max_error_px of
    the finite edge that replaces its arc, so straight runs get no extra
    vertices. Edges shorter than min_distance_px, including the closing edge,
    are then rerouted locally through other contour points under both limits.
    The contour is omitted when no such route exists nearby.
    """
    original = contour.reshape(-1, 2).astype(np.float64)
    n = len(original)
    if n < 3 or len(np.unique(original, axis=0)) != n:
        return None
    doubled = np.concatenate((original, original))
    indices = _split_contour(doubled, settings.max_error_px + GEOMETRY_EPSILON)
    while len(indices) >= 3:
        count = len(indices)
        ring = original[indices]
        lengths = np.linalg.norm(np.roll(ring, -1, axis=0) - ring, axis=1)
        short = np.flatnonzero(lengths + GEOMETRY_EPSILON < settings.min_distance_px)
        if not len(short):
            break
        edge = int(short[0])
        # Replace the vertices around the short edge, keeping one vertex fixed on
        # each side. Every edge of the new route is valid, so each pass removes
        # this short edge and creates none.
        for depth in range(1, REROUTE_DEPTH + 1):
            if count < 2 * depth + 2:
                return None
            replaced = [indices[(edge - depth + step) % count] for step in range(1, 2 * depth + 1)]
            path = _reroute(doubled, settings, indices[(edge - depth) % count],
                            indices[(edge + 1 + depth) % count], replaced)
            if path is not None and count - len(replaced) + len(path) >= 3:
                indices = sorted(set(indices).difference(replaced).union(path))
                break
        else:
            return None
    if len(indices) < 3:
        return None
    if len(indices) > MAX_POLYGON_VERTICES:
        raise PolygonLimitError("polygon exceeds 4096 vertices; increase polygon approximation error")

    points = original[indices]
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


def encode_interactor(result, image_shape: tuple, settings: PolygonSettings) -> dict:
    h, w = image_shape[:2]
    if h * w > MAX_MASK_VALUES:
        raise ValueError("interactor mask exceeds pixel budget")
    bitmap = _check_bitmap(result, h, w)
    # Use CVAT's existing dense-mask adapter, retaining holes and disconnected regions.
    if not bitmap.any():
        return {"shapes": []}
    output = {"mask": bitmap.astype(np.uint8).tolist()}
    polygons = []
    total_vertices = 0
    try:
        for points in _polygons(bitmap, settings):
            total_vertices += len(points)
            if len(polygons) >= MAX_OBJECTS or total_vertices > MAX_TOTAL_POLYGON_VERTICES:
                raise PolygonLimitError("interactor polygons exceed object or vertex budget")
            polygons.append(points.ravel().tolist())
    except PolygonLimitError:
        # The mask is the result of an interactor and stays usable. Without
        # polygons CVAT converts it itself, as it does for any other interactor.
        return output
    output["polygons"] = polygons
    return output


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
        # Every boundary pixel is needed to find the pixels a contour visits twice.
        contours, _ = cv2.findContours(
            component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE, offset=(left, top),
        )
        for contour in contours:
            for loop in _simple_loops(contour.reshape(-1, 2)):
                points = _sample_contour(loop, settings)
                if points is not None:
                    yield points


def polyline_points(points, w: int, h: int) -> np.ndarray:
    """Validate one open polyline in original image coordinates."""
    if isinstance(points, np.ndarray):
        if points.ndim != 2 or points.shape[1] != 2 or points.dtype.kind not in ("i", "u", "f"):
            raise ValueError("Polyline.points must be a numeric (K, 2) array")
        if not 2 <= len(points) <= MAX_POLYGON_VERTICES:
            raise ValueError("polyline requires 2 to 4096 vertices")
        array = points.astype(np.float64)
    else:
        if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= MAX_POLYGON_VERTICES:
            raise ValueError("polyline requires 2 to 4096 vertices")
        if any(not isinstance(point, (list, tuple)) or len(point) != 2 for point in points):
            raise ValueError("Polyline.points must contain (x, y) pairs")
        if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, numbers.Real) for point in points for v in point):
            raise ValueError("Polyline.points contains a nonnumeric coordinate")
        array = np.asarray(points, dtype=np.float64)
    if not np.isfinite(array).all():
        raise ValueError("polyline contains NaN or infinity")
    if (array < 0).any() or (array[:, 0] > w).any() or (array[:, 1] > h).any():
        raise ValueError("polyline must be in original image coordinates")
    if (np.linalg.norm(np.diff(array, axis=0), axis=1) <= GEOMETRY_EPSILON).any():
        raise ValueError("polyline has consecutive duplicate vertices")
    return array


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
        raise ValueError("predict must return a binary (N, H, W) numpy array or at most 2000 Box, Polyline, Tag or legacy Mask values")
    mask_values = 0
    tag_ids: set[int] = set()
    for item in results:
        if not isinstance(item, (Box, Mask, Polyline, Tag)):
            raise TypeError("results must be registry.sdk.Box, Polyline, Tag or legacy Mask")
        label, score = _class_and_score(item, labels)
        if isinstance(item, Tag):
            if label.type != "tag":
                raise ValueError("Tag class_id must refer to a tag label")
            if item.class_id in tag_ids:
                raise ValueError("duplicate Tag class_id for the same image")
            tag_ids.add(item.class_id)
            if len(output) >= MAX_OBJECTS:
                raise ValueError("predict output exceeds 2000 objects")
            output.append({"label": label.name, "confidence": score, "type": "tag"})
            continue
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
        if isinstance(item, Polyline):
            if label.type != "polyline":
                raise ValueError("Polyline class_id must refer to a polyline label")
            points = polyline_points(item.points, w, h)
            total_vertices += len(points)
            if total_vertices > MAX_TOTAL_POLYGON_VERTICES:
                raise ValueError("combined polygons and polylines exceed 100000 vertices")
            if len(output) >= MAX_OBJECTS:
                raise ValueError("predict output exceeds 2000 objects")
            output.append({"label": label.name, "confidence": score, "type": "polyline", "points": points.ravel().tolist()})
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
        if len(output) >= MAX_OBJECTS:
            raise ValueError("predict output exceeds 2000 objects")
        output.append({"label": label.name, "confidence": score, "type": "rectangle", "points": [x1, y1, x2, y2]})
    return output
