# SPDX-License-Identifier: MIT
import base64
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from ultrasam_geometry import mask_shape, point_array
from ultrasam_protocol import ProtocolError, body_dict, decode_image, serve


def encoded_image(image):
    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode()


def decode_shape(shape, size):
    width, height = size
    mask = np.zeros((height, width), dtype=bool)
    if shape is not None:
        *runs, x0, y0, x1, y1 = shape["points"]
        flat = np.repeat(np.arange(len(runs)) % 2, runs).astype(bool)
        assert flat.size == (x1 - x0 + 1) * (y1 - y0 + 1)
        mask[y0 : y1 + 1, x0 : x1 + 1] = flat.reshape(y1 - y0 + 1, x1 - x0 + 1)
    return mask


def test_rle_preserves_holes_components_and_inclusive_bounds():
    mask = np.zeros((9, 13), dtype=bool)
    mask[1:6, 2:7] = True
    mask[2:4, 3:5] = False
    mask[8, 12] = True
    shape = mask_shape(mask)
    assert shape["points"][-4:] == [2, 1, 12, 8]
    assert shape["points"][0] == 0
    np.testing.assert_array_equal(decode_shape(shape, (13, 9)), mask)


@pytest.mark.parametrize(
    "mask",
    [
        np.zeros((2, 3), bool),
        np.ones((1, 1), bool),
        np.eye(6, dtype=bool),
        np.ones((3, 7), bool),
    ],
)
def test_rle_empty_and_edge_masks(mask):
    shape = mask_shape(mask)
    assert (shape is None) == (not mask.any())
    np.testing.assert_array_equal(
        decode_shape(shape, (mask.shape[1], mask.shape[0])), mask
    )


@pytest.mark.parametrize(
    "points",
    [
        [[True, 2]],
        [["1", 2]],
        [[float("nan"), 2]],
        [[float("inf"), 2]],
        [[-1, 0]],
        [[8, 0]],
        [[1, 9]],
        [[1]],
        [1, 2],
        [[1, 2]] * 129,
    ],
)
def test_points_reject_invalid_values(points):
    with pytest.raises(ProtocolError):
        point_array(points, "pos_points", (8, 9))


def test_decode_grayscale_image_without_rotating_coordinates():
    decoded = decode_image({"image": encoded_image(Image.new("L", (7, 5), 100))})
    assert decoded.mode == "RGB"
    assert decoded.size == (7, 5)
    assert decoded.getpixel((2, 3)) == (100, 100, 100)


@pytest.mark.parametrize(
    "value", [None, "", "!!!!", base64.b64encode(b"invalid").decode()]
)
def test_invalid_images(value):
    with pytest.raises(ProtocolError):
        decode_image({"image": value})


def test_pixel_limit_is_checked_before_decompression(monkeypatch):
    import ultrasam_protocol

    monkeypatch.setattr(ultrasam_protocol, "MAX_PIXELS", 10)
    with pytest.raises(ProtocolError) as error:
        decode_image({"image": encoded_image(Image.new("RGB", (4, 3)))})
    assert error.value.status == 413


def test_animated_image_rejected():
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (3, 2), "blue")],
    )
    with pytest.raises(ProtocolError, match="single frame"):
        decode_image({"image": base64.b64encode(buffer.getvalue()).decode()})


@pytest.mark.parametrize("value", ["{", [], 1, b"\xff"])
def test_body_validation(value):
    with pytest.raises(ProtocolError):
        body_dict(value)


def test_handler_returns_400_and_hides_internal_error_details():
    logged = []
    context = SimpleNamespace(
        Response=lambda **values: values, logger=SimpleNamespace(error=logged.append)
    )
    bad = serve(context, SimpleNamespace(body="[1]"), lambda data: data)
    assert bad["status_code"] == 400

    def fail(data):
        raise RuntimeError("secret image payload")

    failed = serve(context, SimpleNamespace(body={}), fail)
    assert failed["status_code"] == 500
    assert "secret" not in json.dumps(failed)
    assert "secret" not in " ".join(logged)
