# SPDX-License-Identifier: MIT
from ultrasam_protocol import serve


def init_context(context):
    import json

    from ultrasam_runtime import run_probe

    context.logger.info("Checking UltraSAM CUDA runtime before model initialization")
    runtime = run_probe("cuda")
    context.logger.info(
        "UltraSAM runtime probe passed: " + json.dumps(runtime, sort_keys=True)
    )
    from ultrasam_interactor import ImageInteractor
    from ultrasam_model import UltraSamPredictor, build_model, warmup_predictor

    predictor = UltraSamPredictor(build_model(device="cuda"))
    context.logger.info("Running UltraSAM model warmup before accepting requests")
    warmup_predictor(predictor)
    context.user_data.operation = ImageInteractor(predictor)
    context.logger.info("UltraSAM image interactor initialized; GPU warmup passed")


def handler(context, event):
    return serve(context, event, context.user_data.operation)
