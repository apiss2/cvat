# SPDX-License-Identifier: MIT
from __future__ import annotations

import math
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class OutputLabel(StrictModel):
    id: Annotated[int, Field(ge=0, le=2**31 - 1)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    type: Literal["rectangle", "polygon", "tag"]

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


class Manifest(StrictModel):
    schema_version: Literal[1] = 1
    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: Annotated[str, Field(max_length=4000)] = ""
    weights: Annotated[list[str], Field(min_length=1, max_length=16)]
    labels: Annotated[list[OutputLabel], Field(min_length=1, max_length=1024)]
    author_contact: Annotated[str, Field(max_length=512)] = ""
    polygon: PolygonSettings = Field(default_factory=PolygonSettings)

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
                "type": "detector",
                "spec": json.dumps([v.model_dump() for v in m.labels], ensure_ascii=False),
                "version": "1",
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
