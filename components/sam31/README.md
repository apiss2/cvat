# SAM3.1 image interaction and polygon tracking

This optional extension adds `pth-sam31-interactor` and `pth-sam31-tracker`.
It uses the same polygon-selection dialog and tracking action as SAM2 without
patching CVAT core or the upstream SAM source. Python/CUDA images, checkpoints,
state codecs and Redis services remain separate from SAM2 and UltraSAM.

**Experimental until real-checkpoint GPU smoke tests and CVAT browser acceptance
checks pass on the deployment host.** CPU tests use model/command doubles, not
real GPU inference, and do not establish accuracy, latency or VRAM requirements.

## Model selection and checkpoint management

The default architecture is the SAM3.1 multiplex tracker and shared visual
backbone from the official merged `sam3.1_multiplex.pt` checkpoint. The upstream
SAM source is pinned to `0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc`.
Text/detection heads are not instantiated. Select another compatible merged
checkpoint through `SAM31_CHECKPOINT_HOST`; this does not switch to a SAM3 or
SAM2 architecture. Missing tracking parameters cause startup to fail.

For SAM2 model presets and local fine-tuned checkpoints, see
[model selection](../extensions/MODEL_SELECTION.md).

Obtain authorized access to `facebook/sam3.1`, accept its model license and keep
the source weights and access tokens outside the checkout. At build time,
cvatctl copies the selected file into a private staging directory. Both function
images embed `/opt/nuclio/sam3.1_multiplex.pt`; neither mounts external weights nor
downloads weights at runtime. Tokens and Redis credentials are not staged as
source. Do not publish images containing licensed weights to unauthorized users.

There is no operator-entered expected checkpoint hash or checksum comparison.
The runtime still computes a content identity to reject tracking state generated
by different weights. This is not an authenticity check. Use trusted compatible
weights; `weights_only=True` and strict parameter loading remain enabled.

## Deployment through cvatctl

Append `sam31` to the existing extension list and configure the same private
`.env` used by `components/extensions/cvatctl`. Preserve other selected features.

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry,sam31
SAM31_CHECKPOINT_HOST=/absolute/path/outside/cvat/sam3.1_multiplex.pt
SAM31_REDIS_PASSWORD=<separate 32 to 128 character random password>
SAM31_REDIS_VOLUME=
SAM31_REDIS_MAXMEMORY=2gb
SAM31_STATE_MAX_BYTES=268435456
SAM31_SESSION_TTL_SECONDS=28800
SAM31_GPU_DEVICE=0
```

The default persistent Redis volume is `<COMPOSE_PROJECT_NAME>_sam31_redis_data`.
SAM2 and SAM3.1 may not share a Redis volume. `cvatctl init` generates independent
passwords; update existing environment files explicitly and retain mode `0600`.
The manager selects plugins and Compose overlays automatically. Do not add
manual SAM3.1 entries to `CVAT_CLIENT_PLUGINS` or `CVAT_EXTRA_COMPOSE_FILES`.

```sh
components/extensions/cvatctl --env-file /path/to/deployment.env down
# Update source and model settings while stopped.
components/extensions/cvatctl --env-file /path/to/deployment.env up
components/extensions/cvatctl --env-file /path/to/deployment.env check

# Explicit redeployment; all is the default.
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31 all
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31 image
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31 tracker
```

A normal `up` or explicit SAM3.1 deployment rebuilds both selected function
images, including when the file was replaced at the same path. Docker may reuse
unchanged build layers. Image tags identify source code, Nuclio version and the
configured source path; they are not weight-content digests.

`up --no-build` deliberately reuses the last built images and rejects missing
images before startup. Keep the checkpoint path **setting unchanged** even when
the original file is no longer available. This mode does not incorporate source
file changes. Use a normal build to load new weights. Changing the configured
path requires stop/reconfigure/start. Runtime checkpoint mounts are rejected.

`up` health-checks Redis before restoring functions. `status`, `check`, `down`
and restart-policy restoration cover both functions. To disable SAM3.1, stop the
deployment, remove `sam31` from the extension list, then start it again. Suspended
functions stay stopped and stored data is retained. `deploy.py` delegates its
command-line entry point to the common manager and operation lock.

Existing deployments must be stopped with their original configuration before
installing these changes, then started with a regular `up` to build new images.
Keep an authorized backup of source weights for future rebuilds. Deployment
requires a compatible NVIDIA driver, GPU container runtime and nuctl matching the
dashboard. Images use Python 3.12, PyTorch 2.10 and CUDA 12.8; FlashAttention 3 and
compilation are disabled.

## Tracking and single-image inference

Open a 2D job and choose **Menu > SAM3.1: ポリゴンを追跡…**. Select one to 16
unlocked polygon shapes and a start/end frame range. The inclusive end must be
within the job and no more than 10,000 frame indices after the start. Both models
share selection, contour previews, progress, cancellation and Undo controls.
The seed polygon is converted to a mask; no text prompt is required.

Successful tracking replaces only selected shapes as one undoable action,
preserving seed geometry, labels, groups and attributes. Saving remains manual.
Errors, cancellation or detected seed edits leave original annotations unchanged.
Deleted frames are skipped, and an outside keyframe prevents extrapolation past
the requested range. Existing tracks cannot be used as seeds. Polygon output
keeps the largest external contour, not holes or disconnected components.

For a single image, select **SAM3.1 (GPU, experimental)** in CVAT's existing AI
interaction controls. Positive/negative points and an optional box use the same
request/response contract as SAM2 and return a full CVAT mask, preserving holes
and disconnected regions. No custom single-image UI is added.

The same multiplex checkpoint is used for both functions. Each image request
uses the full current prompt, a fresh one-object state and
`run_mem_encoder=False`. Only one image's features are cached; prompt/mask/video
history does not cross requests. The image function does not connect to Redis.
The functions create separate model instances; test simultaneous GPU memory use.

## State and verification

Tracking runs one shared encoder/tracking step per frame for the fixed object
set. The seed, bounded recent mask memory and object-pointer history are stored
as CPU tensors in Redis, using validated safetensors rather than pickle.
Atomic revisions and saved replies support response-loss retries and worker
restart. Changed weights, adapter or precision reject incompatible old state.
Independent pixel, state-byte, coordinate-output and Redis-capacity limits remain;
maximum frame/object selections do not guarantee completion within those limits.

CPU tests require pytest, PyYAML, numpy, Pillow, CPU PyTorch, safetensors and
OpenCV. They do not require model weights. CLI tests replace external commands.

```sh
python -m pytest --noconftest -q components/sam31/tests/test_runtime.py components/sam31/tests/test_review_limits.py components/sam31/tests/test_cvatctl.py components/sam31/tests/test_image_parity.py
node cvat-ui/plugins/sam2/tests/run-tests.cjs
```

After building, run the GPU smoke test inside a function container:

```sh
docker cp components/sam31/tests/gpu_smoke.py <function-container>:/tmp/gpu_smoke.py
docker exec <function-container> python /tmp/gpu_smoke.py --objects 16 --frames 40
```

The smoke test checks real checkpoint loading, finite masks, per-frame restoration
and agreement with unpruned inference on synthetic images. It is not an accuracy
benchmark. Also validate non-square image prompts, full-mask output, the complete
UI build/browser workflow, real Redis restart/retry/conflict behavior, concurrent
jobs, GPU-memory coexistence and representative ultrasound images/videos before
production use. Keep SAM2 available during this validation.

Upstream contracts:
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model_builder.py
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model/video_tracking_multiplex.py
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model/multiplex_utils.py
