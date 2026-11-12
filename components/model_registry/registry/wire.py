# SPDX-License-Identifier: MIT
"""Validate the worker response again outside uploaded Python's process."""
from __future__ import annotations
import math
import numpy as np
from .codec import (
    GEOMETRY_EPSILON, MAX_MASK_VALUES, MAX_OBJECTS, MAX_POLYGON_VERTICES,
    MAX_TOTAL_POLYGON_VERTICES, is_simple_polygon, polygon_area,
)
from .schema import Manifest


def validate_wire(result: list | dict, manifest: Manifest, width: int, height: int) -> list | dict:
    if manifest.kind == "interactor":
        return validate_interactor_wire(result, width, height)
    if not isinstance(result, list) or len(result) > MAX_OBJECTS:
        raise ValueError("invalid worker object count")
    labels = {label.name: label.type for label in manifest.labels}
    total_vertices = 0
    tag_labels: set[str] = set()
    for obj in result:
        if not isinstance(obj, dict):
            raise ValueError("invalid worker output fields")
        kind = obj.get("type")
        fields = {"label", "type", "confidence"}
        if kind != "tag":
            fields.add("points")
        if set(obj) != fields:
            raise ValueError("invalid worker output fields")
        if not isinstance(obj["label"], str) or labels.get(obj["label"]) != kind:
            raise ValueError("worker label/type does not match manifest")
        score = obj["confidence"]
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("invalid worker score")
        if kind == "tag":
            if obj["label"] in tag_labels:
                raise ValueError("duplicate worker tag for the same image")
            tag_labels.add(obj["label"])
            continue
        points = obj["points"]
        if not isinstance(points, list) or any(type(x) not in (int, float) or not math.isfinite(x) for x in points):
            raise ValueError("invalid worker coordinates")
        if kind == "rectangle":
            if len(points) != 4:
                raise ValueError("invalid worker rectangle")
            x1, y1, x2, y2 = points
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise ValueError("worker rectangle outside image")
        elif kind == "polygon":
            if len(points) < 6 or len(points) % 2 or len(points) > 2 * MAX_POLYGON_VERTICES:
                raise ValueError("invalid worker polygon vertex count")
            ring = np.asarray(points, dtype=np.float64).reshape(-1, 2)
            total_vertices += len(ring)
            if total_vertices > MAX_TOTAL_POLYGON_VERTICES:
                raise ValueError("combined worker polygons exceed vertex budget")
            if (ring < 0).any() or (ring[:, 0] > width - 1).any() or (ring[:, 1] > height - 1).any():
                raise ValueError("worker polygon outside original image pixel centers")
            distances = np.linalg.norm(np.roll(ring, -1, axis=0) - ring, axis=1)
            if (distances + GEOMETRY_EPSILON < manifest.polygon.min_distance_px).any():
                raise ValueError("worker polygon violates minimum adjacent vertex distance")
            area = polygon_area(ring)
            if area <= GEOMETRY_EPSILON or area + GEOMETRY_EPSILON < manifest.polygon.min_area_px:
                raise ValueError("worker polygon is degenerate or below minimum area")
            if not is_simple_polygon(ring):
                raise ValueError("worker polygon intersects or touches itself")
        elif kind == "polyline":
            if len(points) < 4 or len(points) % 2 or len(points) > 2 * MAX_POLYGON_VERTICES:
                raise ValueError("invalid worker polyline vertex count")
            line = np.asarray(points, dtype=np.float64).reshape(-1, 2)
            total_vertices += len(line)
            if total_vertices > MAX_TOTAL_POLYGON_VERTICES:
                raise ValueError("combined worker polygons and polylines exceed vertex budget")
            if (line < 0).any() or (line[:, 0] > width).any() or (line[:, 1] > height).any():
                raise ValueError("worker polyline outside image")
            if (np.linalg.norm(np.diff(line, axis=0), axis=1) <= GEOMETRY_EPSILON).any():
                raise ValueError("worker polyline has consecutive duplicate vertices")
        else:
            raise ValueError("unsupported worker shape")
    return result


def validate_interactor_wire(result, width: int, height: int) -> dict:
    if not isinstance(result, dict):
        raise ValueError("interactor output must be a mask response")
    if result == {"shapes": []}:
        return result
    if set(result) != {"mask"} or width * height > MAX_MASK_VALUES:
        raise ValueError("invalid interactor output fields or pixel budget")
    mask = result["mask"]
    if (not isinstance(mask, list) or len(mask) != height
            or any(not isinstance(row, list) or len(row) != width for row in mask)):
        raise ValueError("interactor mask must match input image H x W")
    if any(type(pixel) not in (int, bool) or pixel not in (0, 1) for row in mask for pixel in row):
        raise ValueError("interactor mask pixels must be bool or integer 0/1")
    return result
