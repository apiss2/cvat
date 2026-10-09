# SPDX-License-Identifier: MIT
"""SAM3.1 CVAT image interactor; no Redis or tracking session initialization."""
import os

from protocol import serve


def init_context(context):
    from image_model import ImageInteractor
    from temporal_video import CHECKPOINT, load_model
    model, _ = load_model(os.environ.get("SAM31_CHECKPOINT", CHECKPOINT))
    context.user_data.operation = ImageInteractor(model)
    context.logger.info("SAM3.1 image interactor initialized")


def handler(context, event):
    return serve(context, event, context.user_data.operation)
