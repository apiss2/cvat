# SPDX-License-Identifier: MIT
"""Select frames containing an annotation that matches the display-filter rules.

This module knows neither the output format nor how annotations are exported.
Field names and attribute conversion follow cvat-core/annotations-filter.ts.
"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from .logic import evaluate, number, truthy, validate_rule

OBJECT_FIELDS = {"label", "type", "shape", "occluded", "source", "width", "height",
                 "rotation", "serverID", "score", "zOrder"}
ELEMENT_FIELDS = {"label", "occluded"}


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
    ).encode()).hexdigest()


def adjusted(name):
    return name.replace(".", "\u2219")


def fields_for(labels, *, elements=False):
    fields = set(ELEMENT_FIELDS if elements else OBJECT_FIELDS)
    for label in labels:
        entries = [(label["name"], label)] if not elements else [
            (f"{label['name']} / {child['name']}", child) for child in label.get("sublabels", [])
        ]
        for name, spec in entries:
            for attribute in spec.get("attributes", []):
                fields.add(f"attr.{adjusted(name)}.{adjusted(attribute['name'])}")
    return fields


def dimensions(points, shape):
    if not points:
        return None, None
    if shape == "mask":
        x1, y1, x2, y2 = points[-4:]
        return x2 - x1 + 1, y2 - y1 + 1
    if shape == "ellipse":
        cx, cy, rx, ty = points
        return abs(rx - cx) * 2, abs(cy - ty) * 2
    xs, ys = points[::2], points[1::2]
    return max(xs) - min(xs), max(ys) - min(ys)


class AnnotationFields:
    def __init__(self, labels):
        self.labels = {label["id"]: label for label in labels}
        self.elements = {child["id"]: (f"{label['name']} / {child['name']}", child)
                         for label in labels for child in label.get("sublabels", [])}

    @staticmethod
    def attributes(annotation, spec):
        values = {a["spec_id"]: a["value"] for a in annotation.get("attributes", [])}
        result = {}
        for attribute in spec.get("attributes", []):
            value = values.get(attribute["id"], attribute["default_value"])
            if attribute["input_type"] == "number":
                value = number(value)
            elif attribute["input_type"] == "checkbox":
                value = value == "true"
            result[adjusted(attribute["name"])] = value
        return result

    def convert(self, annotation, *, tag=False):
        label = self.labels[annotation["label_id"]]
        shape = None if tag else annotation["type"]
        points = annotation.get("points", [])
        elements = []
        if shape == "skeleton":
            points = [p for child in annotation.get("elements", []) for p in child.get("points", [])]
            for child in annotation.get("elements", []):
                name, spec = self.elements[child["label_id"]]
                elements.append({"label": name, "occluded": child.get("occluded", False),
                                 "attr": {adjusted(name): self.attributes(child, spec)}})
        width, height = (None, None) if tag else dimensions(points, shape)
        return {
            "label": label["name"], "type": "tag" if tag else (
                "track" if "track_id" in annotation else "shape"),
            "shape": shape, "width": width, "height": height,
            "rotation": annotation.get("rotation") if shape in ("rectangle", "ellipse") else None,
            "serverID": annotation.get("track_id", annotation.get("id")),
            "source": annotation.get("source"), "occluded": False if tag else annotation.get("occluded"),
            "score": None if tag else annotation.get("score"),
            "zOrder": 0 if tag else annotation.get("z_order", 0),
            "attr": {adjusted(label["name"]): self.attributes(annotation, label)}, "elements": elements,
        }


@dataclass(frozen=True)
class AnnotationFilter:
    objects: dict
    elements: dict

    @classmethod
    def parse(cls, value):
        if not isinstance(value, list) or len(value) > 2 or any(not isinstance(v, dict) for v in value):
            raise ValueError("filters must be an array of at most two JsonLogic objects")
        try:
            if len(json.dumps(value, allow_nan=False)) > 65536:
                raise ValueError("Filter is too large")
        except (TypeError, RecursionError) as exc:
            raise ValueError("Invalid filter") from exc
        rules = deepcopy(value) + [{}, {}]
        return cls(rules[0], rules[1])

    def to_list(self):
        if self.elements:
            return deepcopy([self.objects, self.elements])
        return deepcopy([self.objects]) if self.objects else []

    def validate(self, labels):
        validate_rule(self.objects, fields_for(labels))
        validate_rule(self.elements, fields_for(labels, elements=True))

    def select(self, available, tags, shapes, labels):
        self.validate(labels)
        available = set(available)
        if not self.objects and not self.elements:
            return available
        fields = AnnotationFields(labels)
        selected = set()

        def matches(annotation, tag=False):
            frame = annotation["frame"]
            if frame not in available or frame in selected or (not tag and annotation.get("outside", False)):
                return
            value = fields.convert(annotation, tag=tag)
            if truthy(evaluate(self.objects, value)) and (
                not self.elements or any(truthy(evaluate(self.elements, child)) for child in value["elements"])
            ):
                selected.add(frame)

        for tag in tags:
            matches(tag, tag=True)
        for shape in shapes:
            matches(shape)
        return selected
