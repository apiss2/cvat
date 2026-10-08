# SPDX-License-Identifier: MIT
"""Transport helpers. No CVAT, SAM2 or torch dependency in this module."""
import base64
import binascii
import io
import json
import os
from PIL import Image, UnidentifiedImageError

MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_PIXELS = int(os.getenv("SAM2_MAX_PIXELS", "16777216"))

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
        raise ProtocolError("Request body must be an object")
    return body

def decode_image(data):
    encoded = data.get("image")
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_BODY_BYTES:
        raise ProtocolError("image must be a bounded base64-encoded image")
    try:
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if width < 1 or height < 1 or width * height > MAX_PIXELS:
                raise ProtocolError("Image exceeds SAM2_MAX_PIXELS", 413)
            if getattr(image, "n_frames", 1) != 1:
                raise ProtocolError("Send a single frame, not an animation")
            return image.convert("RGB")
    except (binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ProtocolError("image is not a supported base64-encoded image") from exc

def serve(context, event, operation):
    try:
        result = operation(body_dict(event.body))
        code = 200
    except ProtocolError as exc:
        result, code = {"error": str(exc)}, exc.status
    except Exception as exc:
        # Do not log images, points, session tokens, or tensor contents.
        context.logger.error("SAM2 inference failed: " + type(exc).__name__)
        result, code = {"error": "Inference failed. Restart tracking from the seed frame; inspect function logs."}, 500
    return context.Response(body=json.dumps(result, allow_nan=False), headers={},
                            content_type="application/json", status_code=code)
