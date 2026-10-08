# SAM3.1 polygon tracking

This optional extension adds `pth-sam31-tracker`. It does not replace SAM2 or
UltraSAM, patch CVAT core, or modify the upstream SAM source. The UI shares the
polygon selection dialog; the model, Python/CUDA image, checkpoint, temporal
state codec and Redis service are separate.

**Experimental until the real-checkpoint GPU smoke test and CVAT browser
acceptance checks below pass on the deployment host.** CPU contract tests use a
fake predictor. They do not demonstrate working SAM3.1 inference or its accuracy.
The complete Docker image and CVAT UI build must also be validated locally.

## Model and state contract

The adapter pins Meta's SAM repository to
`0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc`. It uses the SAM3.1 multiplex tracker
and shared visual backbone from the official merged `sam3.1_multiplex.pt`
checkpoint. It does not instantiate text/detection heads. Missing tracking
weights cause startup to fail rather than leaving randomly initialized weights.

The adapter runs one batched encoder/tracking step per frame for one to four
fixed polygon seeds, then stores bounded CPU tensor memory in Redis. It retains
the initial conditioning frame, the recent mask memories and the object-pointer
horizon required by the pinned forward-only implementation. State uses
safetensors with bounded, validated JSON metadata, never pickle. Atomic Redis
revision checks and stored replies support retries after lost responses or a
worker restart. A changed checkpoint/adapter/precision rejects old state.

The package reuses only image decoding, contour conversion and Redis transaction
helpers from the existing SAM2 extension. `deploy.py` stages these helpers with a
separate SAM31 environment/prefix; it never imports the SAM2 neural model or
its temporal state format at runtime. No session is kept in process-local GPU
memory between requests.

Source references:
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model_builder.py
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model/video_tracking_multiplex.py
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model/multiplex_utils.py

## Deployment

Obtain approved access to `facebook/sam3.1` on Hugging Face and accept the model's
license before downloading its checkpoint. Keep the checkpoint outside the CVAT
checkout. Neither Hugging Face tokens nor weights are embedded in the build
context. The deployed function mounts the checkpoint read-only and disables
runtime Hugging Face downloads.

Append to the existing deployment environment file used by `cvatctl`; do not
replace existing extension or site settings. Preserve its `0600` permissions. Client plugin paths use
`:` separators and extra Compose paths use `;` separators.

```dotenv
# Append these entries to existing lists, rather than discarding other entries.
CVAT_CLIENT_PLUGINS=plugins/sam31
CVAT_EXTRA_COMPOSE_FILES=components/sam31/docker-compose.sam31.yml
SAM31_CHECKPOINT_HOST=/absolute/path/outside/cvat/sam3.1_multiplex.pt
SAM31_CHECKPOINT_SHA256=<sha256sum output>
SAM31_REDIS_PASSWORD=<separate 32 to 128 character random password>
SAM31_REDIS_MAXMEMORY=2gb
SAM31_STATE_MAX_BYTES=268435456
SAM31_SESSION_TTL_SECONDS=28800
SAM31_GPU_DEVICE=0
```

The existing manager automatically appends enabled SAM2/registry plugin paths.
Do **not** add `sam31` to `CVAT_EXTENSIONS`: this optional component uses the
existing extra-plugin and extra-Compose interfaces instead of extending that
manager's hard-coded feature list. A deployment already using SAM2, UltraSAM or
the model registry already includes the serverless overlay. Otherwise also add
`components/serverless/docker-compose.serverless.yml` before the SAM3.1 overlay.

Rebuild/apply the configured deployment with the existing extension management
procedure. Then, from the repository root, deploy this function separately:

```sh
python components/sam31/deploy.py --env-file /path/to/existing-deployment.env
```

Deployment requires a Docker host with a compatible NVIDIA driver, GPU container
runtime and an `nuctl` version matching the existing Nuclio dashboard. The
function image uses Python 3.12, PyTorch 2.10 and CUDA 12.8. FlashAttention 3 and
compilation are disabled. VRAM requirements and performance on the team's
ultrasound images have not been measured; no speed or accuracy improvement over
SAM2 is assumed. Redeploy with `deploy.py` after changing SAM3.1 source/settings;
the ordinary manager does not automatically build this optional function.

## Tracking in CVAT

Open a 2D annotation job and select **Menu > SAM3.1: ポリゴンを追跡…**. Set the
start frame and inclusive end frame. Select one to four unlocked polygon shapes
shown with their object IDs, labels and contour previews. The start-frame shape
is the initial mask; there is no text prompt. **開始フレームを表示** navigates to it.

The chosen shapes are replaced by tracks as one undoable action after all
requests succeed. Other shapes/tracks are left unchanged. The original seed
contour, labels, groups and attributes are preserved. Changes are not saved
automatically. Cancellation, seed edits during inference and errors discard the
pending replacement. An outside keyframe terminates extrapolation beyond the
requested range. Deleted frames are skipped.

The current scope is forward tracking, one to four independent polygon shapes,
at most 1000 frame indices after the start. Existing tracks are not seeds.
Polygon tracks retain the largest external contour and cannot represent holes
or disconnected components. SAM2 uses the same dialog through its own menu item
and its original deployed function.

## Verification

CPU tests require pytest, numpy, Pillow, CPU PyTorch and safetensors. Contour
conversion additionally requires OpenCV. They do not require the model weights.

```sh
python -m pytest components/sam31/tests/test_runtime.py -q
node cvat-ui/plugins/sam2/tests/run-tests.cjs
```

After building the image, copy `tests/gpu_smoke.py` to the running function
container and run it with that container's configured environment and mounted
checkpoint. Substitute the actual function container name or ID:

```sh
docker cp components/sam31/tests/gpu_smoke.py <function-container>:/tmp/gpu_smoke.py
docker exec <function-container> python /tmp/gpu_smoke.py --objects 4 --frames 40
```

This uses real GPU inference and checks strict checkpoint loading, finite masks,
codec restoration on every frame, bounded state size, and mask equality against
unpruned forward inference past both memory horizons. It is a contract check on
synthetic images, not an ultrasound accuracy benchmark or a real Redis test.

Before production use, also validate the complete CVAT UI build and browser
workflow; test Redis-backed continuation after a worker restart, identical
request replay after response loss, conflicting requests, and wrong-model state
rejection. Check concurrent jobs and representative ultrasound videos. A failure
must leave original annotations untouched. Keep SAM2 available while doing these
checks. Disabling SAM3.1 means stopping/deleting its Nuclio function and removing
its plugin/overlay entries; do not delete the existing SAM2 Redis volume.
