# SPDX-License-Identifier: MIT
"""Nuclio entry point. SAM3.1 code and approved weights never enter CVAT itself."""
import os
import re

from protocol import serve
from redis_store import RedisStore
from tracker import Tracker
from temporal_video import TemporalVideo, load_model, model_identity


def init_context(context):
    expected = os.environ.get("SAM31_CHECKPOINT_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Set SAM31_CHECKPOINT_SHA256 to the approved checkpoint digest")
    model, digest = load_model(os.environ.get("SAM31_CHECKPOINT", "/models/sam3.1_multiplex.pt"), expected)
    video = TemporalVideo(model)
    identity = model_identity(digest, video.policy, video.bf16)
    store = RedisStore.from_env()
    context.user_data.tracker = Tracker(video, store, identity)
    context.logger.info("SAM3.1 polygon tracker initialized")


def handler(context, event):
    return serve(context, event, context.user_data.tracker)
