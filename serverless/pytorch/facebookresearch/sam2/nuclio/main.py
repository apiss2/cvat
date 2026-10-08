# SPDX-License-Identifier: MIT
from protocol import serve

def init_context(context):
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor
    from image_model import ImageInteractor
    from model_common import CHECKPOINT, MODEL_CONFIG, require_gpu
    require_gpu()
    model = build_sam2(MODEL_CONFIG, CHECKPOINT, device="cuda", apply_postprocessing=False)
    context.user_data.operation = ImageInteractor(SAM2ImagePredictor(model))
    context.logger.info("SAM2 image interactor initialized")

def handler(context, event):
    return serve(context, event, context.user_data.operation)
