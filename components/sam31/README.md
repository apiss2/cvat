# SAM3.1 image interaction and polygon tracking

This optional extension adds `pth-sam31-interactor` and `pth-sam31-tracker`. It does not replace SAM2 or
UltraSAM, patch CVAT core, or modify the upstream SAM source. SAM2 and SAM3.1 use
the same polygon-selection dialog and tracking action implementation. The model,
Python/CUDA image, checkpoint, state codec and Redis service remain separate.

**Experimental until real-checkpoint GPU smoke tests and CVAT browser acceptance
checks pass on the deployment host.** CPU tests use model/command doubles. They
do not establish working GPU inference, accuracy, latency or VRAM requirements.

## Model and state contract

The adapter pins Meta's SAM repository to
`0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc`. It uses the SAM3.1 multiplex tracker
and shared visual backbone from the official merged `sam3.1_multiplex.pt`
checkpoint. It does not instantiate text/detection heads. Missing tracking
weights cause startup to fail instead of leaving randomly initialized weights.

The adapter runs one batched encoder/tracking step per frame for one to 16 fixed
polygon seeds. The previous four-object limit was an adapter/storage policy, not
the multiplex capacity: the pinned model is configured with 16 multiplex slots.
The initial frame, bounded recent mask memory and object-pointer horizon are
stored as CPU tensors in Redis. State uses validated safetensors, not pickle.
Atomic revisions and stored replies support replay after response loss or worker
restart; a changed checkpoint/adapter/precision rejects old state.

Only image decoding, contour conversion and Redis transaction helpers are shared
with SAM2. `deploy.py` stages these helpers with a separate SAM31 environment and
key prefix; it does not import the SAM2 neural model or state codec at runtime.
SAM2's corresponding object, serialization and deployment limits are also 16.
Its default state byte limit is 256 MiB, increased from 64 MiB. SAM3.1 keeps its
256 MiB default. Independent byte, pixel, output-coordinate and Redis memory
limits still apply; selecting the maximum range/count is not a promise that every
video or polygon complexity fits those budgets.

Source references:
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model_builder.py
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model/video_tracking_multiplex.py
- https://github.com/facebookresearch/sam3/blob/0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc/sam3/model/multiplex_utils.py

## Deployment through cvatctl

Obtain authorized access to `facebook/sam3.1`, accept the model license and obtain
its checkpoint. Keep the downloaded source file and access tokens outside the
checkout. At build time, cvatctl copies only the approved checkpoint into a
private staging directory and verifies the bytes copied. Both function images
embed `/opt/nuclio/sam3.1_multiplex.pt`; neither mounts external weights or downloads
weights at runtime. Access tokens and Redis credentials are not staged as source.
Treat images containing the gated checkpoint as licensed model artifacts and do
not publish them to an unauthorized registry.

Append `sam31` to the existing `CVAT_EXTENSIONS` list, preserving other selected
extensions. Configure the following entries in the same private deployment
`.env` used by `components/extensions/cvatctl`:

```dotenv
# Example selection; retain your actual existing extension list.
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry,sam31
# Build-only input. May be empty for an existing-image deployment with --no-build.
SAM31_CHECKPOINT_HOST=/absolute/path/outside/cvat/sam3.1_multiplex.pt
SAM31_CHECKPOINT_SHA256=<sha256sum output>
SAM31_REDIS_PASSWORD=<separate 32 to 128 character random password>
SAM31_REDIS_VOLUME=
SAM31_REDIS_MAXMEMORY=2gb
SAM31_STATE_MAX_BYTES=268435456
SAM31_SESSION_TTL_SECONDS=28800
SAM31_GPU_DEVICE=0
```

The default Redis volume is `<COMPOSE_PROJECT_NAME>_sam31_redis_data`; it is an
external persistent volume managed by cvatctl. SAM2 and SAM3.1 may not share it.
`cvatctl init` generates independent Redis passwords, but an existing environment
file must be updated explicitly. Keep `.env` mode `0600`.

The common manager selects the SAM3.1 plugin, serverless support and Compose
file automatically. Do not add manual SAM3.1 entries to
`CVAT_CLIENT_PLUGINS` or `CVAT_EXTRA_COMPOSE_FILES`.

```sh
# Use the existing deployment environment/state directory for every operation.
components/extensions/cvatctl --env-file /path/to/deployment.env down
# Edit the configuration above while stopped.
components/extensions/cvatctl --env-file /path/to/deployment.env up
components/extensions/cvatctl --env-file /path/to/deployment.env check

# Explicit SAM3.1 function redeployment under the same operation lock/state:
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31
# SAM2-compatible per-function selection:
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31 image
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31 tracker
```

`up` starts and health-checks Redis before restoring functions, then deploys the
SAM3.1 interactor and tracker. `up --no-build` (also accepted before `up`) reuses
matching local images and rejects missing images before startup. The original
checkpoint input is not needed for this mode; its approved SHA256 remains in the
configuration. Image fingerprints include the checkpoint SHA256, SAM3.1 source,
function definition, shared helpers and Nuclio version. Changing weights requires
rebuilding the images. Moving the build-only source file does not change runtime
identity. Runtime checkpoint mounts are rejected.

When migrating from the older externally mounted SAM3.1 deployment, use the same
state directory for `down`, install the reviewed source/configuration changes,
then run a regular `up` to build both new images. After successful validation,
subsequent restarts may use `up --no-build` without the source checkpoint. Keep
an authorized backup of the original checkpoint for future rebuilds.

`status`, `check`, `down` and restart-policy restoration include SAM3.1. To disable
it, run `down`, remove `sam31` from `CVAT_EXTENSIONS`, then run `up`; its suspended
functions stay stopped and its Redis volume is retained. No SAM2 volume is deleted.
`deploy.py` is a packaging helper; invoking its command-line entry also delegates
to the common manager instead of creating a second deployment state.

Deployment requires a compatible NVIDIA driver, GPU container runtime and nuctl
matching the dashboard. The function image uses Python 3.12, PyTorch 2.10 and
CUDA 12.8. FlashAttention 3 and compilation are disabled. Real-image builds and
inference still require validation on the deployment host.

## Tracking in CVAT

Open a 2D annotation job and select **Menu > SAM3.1: ポリゴンを追跡…**. Set the
start frame and inclusive end frame. Select one to 16 unlocked polygon shapes
shown with IDs, labels and contour previews. **開始フレームを表示** navigates to the
seed frame. There is no text prompt; the seed polygon is converted to a mask.

Both models use exactly the same controls for frame range, selection, previews,
progress and cancellation. The permitted end is at most 10,000 frame indices
beyond the start, and must remain within the job. The seed and end are both
included, so a contiguous range can contain 10,001 frames in total. The browser
keeps its two-million-coordinate result budget; complex contours can reach that
limit earlier. Errors or cancellation leave the original annotations unchanged.

Selected shapes become tracks as one undoable action after all requests succeed.
Other shapes and existing tracks are unchanged. Seed geometry, labels, groups and
attributes are preserved; saving remains manual. Seed edits during tracking are
rejected. Deleted frames are skipped and an outside keyframe terminates unwanted
extrapolation beyond the range. Existing tracks are not seed inputs.

Polygon output retains the largest external contour, not holes or disconnected
components. The separate single-image interactor instead returns a full CVAT
mask, preserving disconnected regions and holes.

## Single-image inference in CVAT

Select **SAM3.1 (GPU, experimental)** in CVAT's existing AI interaction controls.
Positive and negative clicks and an optional rectangular box follow the same
request and response contract as SAM2. No custom single-image UI is introduced.
The same SAM3.1 multiplex checkpoint and interactive segmentation head are used;
SAM3 image weights and SAM2 weights are not substituted.

Each call uses the complete current prompt, a fresh one-object multiplex state,
and `run_mem_encoder=False`. Only one image's features are cached; no prompt,
mask or video history crosses requests. The image function does not connect to
Redis. Deploying both functions creates separate model instances and can require
additional GPU memory; verify coexistence with SAM2 on the target GPU.

## Verification

CPU tests require pytest, numpy, Pillow, CPU PyTorch, safetensors and OpenCV. They
do not require model weights. CLI tests replace external commands, not the manager:

```sh
python -m pytest --noconftest components/sam31/tests/test_runtime.py components/sam31/tests/test_review_limits.py components/sam31/tests/test_cvatctl.py components/sam31/tests/test_image_parity.py -q
node cvat-ui/plugins/sam2/tests/run-tests.cjs
```

After building the image, copy and run `tests/gpu_smoke.py` in the function
container with its configured environment and embedded checkpoint:

```sh
docker cp components/sam31/tests/gpu_smoke.py <function-container>:/tmp/gpu_smoke.py
docker exec <function-container> python /tmp/gpu_smoke.py --objects 4 --frames 40
```

This checks real checkpoint loading, finite masks, restoration each frame and
agreement with unpruned inference on synthetic images, not ultrasound accuracy.
Before production use, additionally validate maximum-count GPU tracking, the
complete UI build/browser workflow, real Redis restart/replay/conflict behavior,
concurrent jobs and representative ultrasound videos. Validate single-image
positive/negative clicks, box-only and combined prompts on non-square images,
mask output and GPU-memory coexistence of both functions. A failure must leave the
original annotations untouched. Keep SAM2 available during validation.
