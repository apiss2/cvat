# SPDX-License-Identifier: MIT
"""Validate the worker response again outside uploaded Python's process."""
from __future__ import annotations
import math
import numpy as np
from .codec import (
    GEOMETRY_EPSILON, MAX_OBJECTS, MAX_POLYGON_VERTICES,
    MAX_TOTAL_POLYGON_VERTICES, is_simple_polygon, polygon_area,
)
from .schema import Manifest


def validate_wire(result: list, manifest: Manifest, width: int, height: int) -> list:
    if not isinstance(result, list) or len(result) > MAX_OBJECTS:
        raise ValueError("invalid worker object count")
    labels = {label.name: label.type for label in manifest.labels}
    total_vertices = 0
    for obj in result:
        if not isinstance(obj, dict) or set(obj) != {"label", "type", "confidence", "points"}:
            raise ValueError("invalid worker output fields")
        kind = obj["type"]
        if not isinstance(obj["label"], str) or labels.get(obj["label"]) != kind:
            raise ValueError("worker label/type does not match manifest")
        score = obj["confidence"]
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("invalid worker score")
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
        else:
            raise ValueError("unsupported worker shape")
    return result
