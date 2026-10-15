#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regenerate the tiny real ONNX Identity fixtures and upload packages.

Uses the documented ONNX protobuf wire fields so fixture generation itself does
not require ONNX Runtime. tests/test_sdk_worker.py validates them using ONNX and
ONNX Runtime when those optional test dependencies are installed.
"""
import json
import zipfile
from pathlib import Path
from PIL import Image, ImageDraw


def varint(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def integer(field, value):
    return varint(field << 3) + varint(value)


def message(field, value):
    if isinstance(value, str):
        value = value.encode()
    return varint(field << 3 | 2) + varint(len(value)) + value


def value_info(name):
    dims = b"".join(message(1, integer(1, d) if isinstance(d, int) else message(2, d)) for d in [1, 3, "height", "width"])
    tensor_type = integer(1, 1) + message(2, dims)  # TensorProto.FLOAT
    return message(1, name) + message(2, message(1, tensor_type))


def identity_model():
    node = message(1, "image") + message(2, "output") + message(3, "identity") + message(4, "Identity")
    graph = message(1, node) + message(2, "fixture") + message(11, value_info("image")) + message(12, value_info("output"))
    return integer(1, 8) + message(2, "cvat-model-registry-example") + message(7, graph) + message(8, integer(2, 13))


def generate(root: Path):
    for kind, label_type in [("segmentation", "polygon"), ("detection", "rectangle"), ("classification", "tag")]:
        folder = root / kind
        folder.mkdir(exist_ok=True)
        (folder / "model.onnx").write_bytes(identity_model())
        labels = ([{"id": 0, "name": "dark_image", "type": "tag"}, {"id": 1, "name": "bright_image", "type": "tag"}]
                  if label_type == "tag" else [{"id": 0, "name": "bright_region", "type": label_type}])
        (folder / "manifest.json").write_text(json.dumps({"schema_version": 1, "name": "ONNX demo " + kind, "description": "Identity network plus brightness postprocessing; interface test only", "weights": ["model.onnx"], "labels": labels, "author_contact": "", "polygon": {"min_distance_px": 2.0, "spacing_percent": 1.0, "min_area_px": 10.0}}, indent=2) + "\n")
        image = Image.new("RGB", (64, 48))
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 8, 43, 31), fill="white")
        draw.rectangle((20, 16, 27, 23), fill="black")  # Polygon output deliberately keeps the external boundary only.
        image.save(folder / "sample.png")
        with zipfile.ZipFile(root / f"{kind}-demo.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(folder.iterdir()):
                if file.is_file() and file.suffix in (".json", ".py", ".onnx", ".png"):
                    archive.write(file, file.name)


if __name__ == "__main__":
    generate(Path(__file__).resolve().parent)
