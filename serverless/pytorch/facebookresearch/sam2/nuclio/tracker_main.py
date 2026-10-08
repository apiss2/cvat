# SPDX-License-Identifier: MIT
import os
from pathlib import Path
from protocol import MAX_OBJECTS, serve


def init_context(context):
    import sam2
    from sam2.build_sam import build_sam2_video_predictor
    from model_common import CHECKPOINT, MODEL_CONFIG, require_gpu
    from temporal_video import TemporalVideo, model_identity
    from redis_store import RedisStore
    from redis_tracker import RedisTracker
    require_gpu()
    store = RedisStore.from_env()  # Fail closed: no process-local fallback if Redis fails.
    predictor = build_sam2_video_predictor(MODEL_CONFIG, CHECKPOINT, device="cuda", apply_postprocessing=False)
    video = TemporalVideo(predictor)
    config_path = Path(sam2.__file__).parent / MODEL_CONFIG
    identity = model_identity(CHECKPOINT, config_path, video.policy)
    context.user_data.operation = RedisTracker(
        video, store, identity, max_objects=int(os.getenv("SAM2_MAX_OBJECTS", str(MAX_OBJECTS))),
    )
    context.logger.info("SAM2 tracker initialized (Redis state, single-frame track_step)")


def handler(context, event):
    return serve(context, event, context.user_data.operation)
