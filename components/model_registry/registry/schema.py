# SPDX-License-Identifier: MIT
from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class OutputLabel(StrictModel):
    id: Annotated[int, Field(ge=0, le=2**31 - 1)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    type: Literal["rectangle", "polygon", "polyline", "tag"]

    @model_validator(mode="before")
    @classmethod
    def legacy_shape_name(cls, value):
        # Stored packages use schema version 1. Normalize their CVAT shape name.
        if isinstance(value, dict) and value.get("type") == "mask":
            return {**value, "type": "polygon"}
        return value

    @model_validator(mode="after")
    def clean_name(self):
        if self.name != self.name.strip() or any(ord(c) < 32 for c in self.name):
            raise ValueError("label names must be trimmed and contain no control characters")
        return self


class PolygonSettings(StrictModel):
    min_distance_px: Annotated[float, Field(ge=0)] = 2.0
    spacing_percent: Annotated[float, Field(ge=0, le=100)] = 1.0
    min_area_px: Annotated[float, Field(ge=0)] = 10.0


Point = Annotated[list[float], Field(min_length=2, max_length=2)]
# CVAT's AI tools send [] when no box is drawn; treat it the same as null.
Box = Annotated[
    Annotated[list[Point], Field(min_length=2, max_length=2)] | None,
    BeforeValidator(lambda value: None if value == [] else value),
]


class Prompts(StrictModel):
    pos_points: Annotated[list[Point], Field(max_length=1000)] = Field(default_factory=list)
    neg_points: Annotated[list[Point], Field(max_length=1000)] = Field(default_factory=list)
    obj_bbox: Box = None

    def validate_image(self, settings: InteractorSettings, width: int, height: int) -> None:
        for points in (self.pos_points, self.neg_points):
            if any(not (0 <= x < width and 0 <= y < height) for x, y in points):
                raise ValueError("prompt points must lie inside the input image")
        if self.obj_bbox is not None:
            if not (settings.startswith_box or settings.startswith_box_optional):
                raise ValueError("this interactor does not accept a box prompt")
            (x1, y1), (x2, y2) = self.obj_bbox
            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise ValueError("obj_bbox must be ordered image-edge coordinates inside the image")
        elif settings.startswith_box:
            raise ValueError("this interactor requires a box prompt")
        if self.obj_bbox is None and len(self.pos_points) < settings.min_pos_points:
            raise ValueError("not enough positive prompt points")
        if settings.min_neg_points == -1 and self.neg_points:
            raise ValueError("this interactor does not accept negative prompt points")
        if len(self.neg_points) < max(0, settings.min_neg_points):
            raise ValueError("not enough negative prompt points")


class InteractorSettings(StrictModel):
    min_pos_points: Annotated[int, Field(ge=0, le=1000)] = 1
    min_neg_points: Annotated[int, Field(ge=-1, le=1000)] = 0
    startswith_box: bool = False
    startswith_box_optional: bool = True
    sample: Prompts

    @model_validator(mode="after")
    def usable_prompts(self):
        if self.startswith_box and self.startswith_box_optional:
            raise ValueError("box prompts cannot be both required and optional")
        if not self.startswith_box and self.min_pos_points < 1:
            raise ValueError("point-based interaction requires at least one positive point")
        return self


class Manifest(StrictModel):
    schema_version: Literal[1] = 1
    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: Annotated[str, Field(max_length=4000)] = ""
    weights: Annotated[list[str], Field(min_length=1, max_length=16)]
    labels: Annotated[list[OutputLabel], Field(min_length=1, max_length=1024)]
    author_contact: Annotated[str, Field(max_length=512)] = ""
    polygon: PolygonSettings = Field(default_factory=PolygonSettings)
    kind: Literal["detector", "interactor"] = "detector"
    interactor: InteractorSettings | None = None

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_threshold(cls, value):
        # Keep old manifest files readable without introducing a threshold setting.
        # Binarization / detection filtering belong to model.py.
        if isinstance(value, dict) and "threshold" in value:
            value = dict(value)
            old = value.pop("threshold")
            if type(old) not in (int, float) or not math.isfinite(old) or not 0 <= old <= 1:
                raise ValueError("legacy threshold must be a finite number in [0, 1]")
        return value

    @model_validator(mode="after")
    def unique_and_safe(self):
        if self.kind == "interactor":
            if self.interactor is None:
                raise ValueError("interactor settings and sample prompts are required")
            if len(self.labels) != 1 or self.labels[0].type != "polygon":
                raise ValueError("interactors must declare one polygon label; CVAT assigns the task label")
        elif self.interactor is not None:
            raise ValueError("detector models cannot declare interactor settings")
        if self.name != self.name.strip() or any(ord(c) < 32 for c in self.name):
            raise ValueError("model name must be trimmed and contain no control characters")
        if self.author_contact != self.author_contact.strip() or any(ord(c) < 32 for c in self.author_contact):
            raise ValueError("author_contact must be trimmed and contain no control characters")
        for field in ("id", "name"):
            if len({getattr(v, field) for v in self.labels}) != len(self.labels):
                raise ValueError(f"duplicate label {field}")
        if len(set(self.weights)) != len(self.weights):
            raise ValueError("duplicate weight file")
        for name in self.weights:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.onnx", name):
                raise ValueError("weights must be root-level ASCII .onnx filenames")
        return self


class InvokeRequest(StrictModel):
    image: Annotated[str, Field(min_length=1, max_length=48 * 1024 * 1024)]
    # CVAT's upstream detector request includes this field. Accepted but ignored.
    threshold: Annotated[float | None, Field(ge=0, le=1)] = None
    pos_points: Annotated[list[Point], Field(max_length=1000)] | None = None
    neg_points: Annotated[list[Point], Field(max_length=1000)] | None = None
    obj_bbox: Box = None

    def prompts_for(self, manifest: Manifest, width: int, height: int) -> Prompts | None:
        if manifest.kind == "detector":
            if self.pos_points is not None or self.neg_points is not None or self.obj_bbox is not None:
                raise ValueError("detector models do not accept prompts")
            return None
        if self.pos_points is None or self.neg_points is None:
            raise ValueError("interactors require pos_points and neg_points arrays")
        prompts = Prompts(pos_points=self.pos_points, neg_points=self.neg_points, obj_bbox=self.obj_bbox)
        if manifest.interactor is None:
            raise ValueError("interactor settings are missing")
        prompts.validate_image(manifest.interactor, width, height)
        return prompts


def sample_payload(manifest: Manifest, image: str) -> dict:
    return {"image": image, **(manifest.interactor.sample.model_dump() if manifest.interactor else {})}


def function_metadata(model_id: str, revision: str, manifest: dict, namespace: str) -> dict:
    m = Manifest.model_validate(manifest)
    import json
    return {
        "metadata": {
            "name": function_id(model_id, revision),
            "namespace": namespace,
            "labels": {"nuclio.io/project-name": "cvat"},
            "annotations": {
                "name": m.name,
                # CVAT's detector protocol accepts both shapes and image tags.
                # Classification is a label/output type, not a new function kind.
                "type": m.kind,
                "spec": json.dumps([v.model_dump() for v in m.labels], ensure_ascii=False),
                "version": "1",
                **({
                    "min_pos_points": str(m.interactor.min_pos_points),
                    "min_neg_points": str(m.interactor.min_neg_points),
                    # CVAT reads these fields with bool(), not a string parser.
                    "startswith_box": m.interactor.startswith_box,
                    "startswith_box_optional": m.interactor.startswith_box_optional,
                    "box_satisfies_min_pos_points": True,
                    "help_message": "Use positive points, negative points and a box as supported by this model.",
                } if m.interactor else {}),
            },
        },
        "spec": {"description": f"{m.description}\nONNX revision: {revision}".strip()},
        "status": {"state": "ready", "httpPort": 0},
    }


def function_id(model_id: str, revision: str) -> str:
    return f"mr-{model_id}-{revision}"


def parse_function_id(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"mr-([0-9a-f]{20})-([0-9a-f]{16})", value)
    if not match:
        raise ValueError("invalid model registry function id")
    return match.group(1), match.group(2)
