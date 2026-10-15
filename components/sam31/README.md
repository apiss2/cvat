# SAM3.1 image interaction and polygon tracking

The optional `sam31` extension adds `pth-sam31-interactor` and `pth-sam31-tracker`.
It shares CVAT's tracking UI with SAM2 and uses the existing image-interaction UI.
The Python/CUDA environment, neural adapter, state codec and Redis service remain
separate from SAM2. Both functions are started by `cvatctl up`.

**Experimental until real-checkpoint GPU tests and browser acceptance tests pass
on the deployment host.** CPU contracts do not establish accuracy, latency or VRAM
requirements. No CVAT core or upstream SAM source patch is required.

## Checkpoint placement and deployment

Add `sam31` to `CVAT_EXTENSIONS` in the private deployment `.env` and place the
compatible merged checkpoint outside the checkout:

```dotenv
CVAT_EXTENSIONS=sam2,sam31
SAM31_CHECKPOINT_HOST=/opt/models/sam3.1_multiplex.pt
SAM31_REDIS_PASSWORD=<independent 32 to 128 character random password>
SAM31_REDIS_VOLUME=
SAM31_REDIS_MAXMEMORY=2gb
SAM31_STATE_MAX_BYTES=268435456
SAM31_SESSION_TTL_SECONDS=28800
SAM31_GPU_DEVICE=0
```

Previously, local checkpoint placement was rejected in favor of automatic
Hugging Face downloads. Fresh builds consequently needed an HF account, access
approval and credentials. We now choose pre-placed weights so the deployment
host can use an already-obtained checkpoint without HF authentication or another
weight download. Obtain the weight through an authorized source and follow its
license. Do not commit weights or publish licensed images without authorization.
See the [deployment decision](../extensions/MODEL_SELECTION.md#sam31-checkpoint-placement).

`cvatctl` checks the local file before startup and embeds it in both images at
`/opt/nuclio/sam3.1_multiplex.pt`. It never downloads SAM3.1 weights automatically.
Runtime uses the embedded weights offline, without host weight mounts. No manual
expected-SHA setting is required; content identity still rejects old model state.

```sh
components/extensions/cvatctl --env-file /path/to/deployment.env down
# Set SAM31_CHECKPOINT_HOST while stopped.
components/extensions/cvatctl --env-file /path/to/deployment.env up
components/extensions/cvatctl --env-file /path/to/deployment.env check
# Rebuild both functions, or add image / tracker:
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31
# Restart existing images; the source weight file is not needed:
components/extensions/cvatctl --env-file /path/to/deployment.env up --no-build
```

Keep `.env` mode `0600` and its checkpoint path unchanged for `--no-build`.
Normal builds copy the current local weight, including same-path replacements.
The default Redis volume is `<COMPOSE_PROJECT_NAME>_sam31_redis_data`; it cannot
be shared with SAM2. Disabled functions stay stopped and volumes are retained.

## Model and state contract

The adapter pins Meta's SAM repository to
`0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc`. It uses the SAM3.1 multiplex tracker and
shared visual backbone; text/detection heads are not instantiated. Missing or
incompatible tracking parameters cause startup to fail. The image uses Python
3.12, PyTorch 2.10 and CUDA 12.8, with compilation and FlashAttention 3 disabled.
PyAV 16.0.1 supplies the Python 3.12 binary dependency; psutil is required by the
upstream model loader. These are SAM3.1 build fixes, independent of AV1 support.
A compatible NVIDIA driver/container runtime and matching nuctl are required.

One batched encoder/tracking step handles one to 16 fixed polygon seeds per
frame. Only the initial frame's memory, bounded recent mask memory and object
pointers survive in Redis. Validated safetensors store the CPU tensors, not
pickle. Atomic revision checks and stored replies support response-loss retries
and worker recreation. A changed checkpoint/adapter/precision rejects old state.
The automatic content identity is not a manual approval-hash check.

The image decoder, contour conversion and Redis transaction code are shared
byte-for-byte with SAM2. The tracker explicitly selects the `SAM31` namespace;
source code is not rewritten during packaging. Neural models and state codecs
are not mixed between the two families.

## CVAT interaction

For image interaction, select **SAM3.1 (GPU, experimental)** in the existing AI
controls. Positive/negative points and an optional rectangle return a full CVAT
mask, retaining holes and disconnected regions. Requests use a fresh one-object
multiplex state and `run_mem_encoder=False`. Only one image embedding is cached;
point/mask history is not shared between requests. This function does not use Redis.

For tracking, use **Menu > SAM3.1: ポリゴンを追跡…** in a 2D job. Select one to 16
unlocked polygon shapes and an inclusive frame range ending at most 10,000 frame
indices after the start. Both endpoints are included, so a contiguous range can
contain 10,001 frames. The job bounds, two-million-coordinate browser budget,
state byte limit and Redis capacity still apply; maxima do not guarantee completion.

The shared dialog provides frame navigation, contour previews, progress and
cancellation. After success, only selected shapes are replaced as one undoable
action. Seed geometry, labels, groups and attributes are retained. Other shapes
and tracks are unchanged. Seed edits during tracking are rejected, deleted frames
are skipped, and an outside keyframe prevents extrapolation beyond the requested
range. Saving is manual. Errors or cancellation leave original annotations intact.
Polygon tracks retain the largest external contour, not holes/disconnected parts.

## Verification

```sh
python -m pytest --noconftest -q components/extensions/tests \
  components/sam31/tests/test_runtime.py \
  components/sam31/tests/test_review_limits.py \
  components/sam31/tests/test_image_parity.py
node cvat-ui/plugins/sam2/tests/run-tests.cjs
node --test components/model_registry/tests/classification-ui.test.cjs
```

CPU contracts use model, command and service doubles. For real GPU checks, copy
`tests/gpu_smoke.py` into the built function container and run it there:

```sh
docker cp components/sam31/tests/gpu_smoke.py <function-container>:/tmp/gpu_smoke.py
docker exec <function-container> python /tmp/gpu_smoke.py --objects 16 --frames 40
```

The GPU script checks loading, finite outputs, restored/pruned state against an
unbounded reference, and point/box interaction on synthetic images. It is not an
ultrasound accuracy benchmark. Also validate real Redis restart/retry/conflict,
the complete UI build/browser workflow, long videos and representative ultrasound
images. Image and tracking functions use separate model instances; check GPU
memory when running them together with SAM2 and UltraSAM.
