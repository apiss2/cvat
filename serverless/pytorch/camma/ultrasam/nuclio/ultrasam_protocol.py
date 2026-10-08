# SPDX-License-Identifier: MIT
"""Nuclio JSON transport. This module never imports the model runtime."""

import base64
import binascii
import io
import json
import warnings

from PIL import Image, UnidentifiedImageError
from ultrasam_constants import MAX_BODY_BYTES, MAX_PIXELS


class ProtocolError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def body_dict(body):
    if isinstance(body, (str, bytes, bytearray)):
        if len(body) > MAX_BODY_BYTES:
            raise ProtocolError("Request body is too large", 413)
        try:
            body = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("Request must contain valid JSON") from exc
    if not isinstance(body, dict):
        raise ProtocolError("Request body must be a JSON object")
    return body


def decode_image(data):
    encoded = data.get("image")
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_BODY_BYTES:
        raise ProtocolError("image must be a bounded base64-encoded image")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > MAX_PIXELS:
                    raise ProtocolError("Image exceeds the 16777216 pixel limit", 413)
                if min(width, height) * 1024 / max(width, height) < 0.5:
                    raise ProtocolError(
                        "Image aspect ratio cannot be resized to 1024 pixels"
                    )
                if getattr(image, "n_frames", 1) != 1:
                    raise ProtocolError("Send a single frame, not an animation")
                # CVAT frame coordinates refer to the encoded raster. Do not rotate by EXIF.
                return image.convert("RGB")
    except (
        binascii.Error,
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("image is not a supported base64-encoded image") from exc


def serve(context, event, operation):
    try:
        result = operation(body_dict(event.body))
        code = 200
    except ProtocolError as exc:
        result, code = {"error": str(exc)}, exc.status
    except Exception as exc:
        # Do not log image payloads, coordinates or tensor contents.
        context.logger.error("UltraSAM inference failed: " + type(exc).__name__)
        result = {"error": "UltraSAM inference failed; inspect the function logs."}
        code = 500
    return context.Response(
        body=json.dumps(result, allow_nan=False),
        headers={},
        content_type="application/json",
        status_code=code,
    )
