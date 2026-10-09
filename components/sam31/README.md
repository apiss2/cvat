# SAM3.1 image interaction and polygon tracking

The optional `sam31` extension adds `pth-sam31-interactor` and `pth-sam31-tracker`.
It shares CVAT's tracking UI with SAM2 and uses the existing image-interaction UI.
The Python/CUDA environment, neural adapter, state codec and Redis service remain
separate from SAM2. Both functions are started by `cvatctl up`.

**Experimental until real-checkpoint GPU tests and browser acceptance tests pass
on the deployment host.** CPU contracts do not establish accuracy, latency or VRAM
requirements. No CVAT core or upstream SAM source patch is required.

## Named model and deployment

Add `sam31` to `CVAT_EXTENSIONS` in the existing private deployment `.env`:

```dotenv
CVAT_EXTENSIONS=itgformat,sam2,ultrasam,model_registry,sam31
SAM31_MODEL=facebook/sam3.1
HF_TOKEN=
SAM31_REDIS_PASSWORD=<independent 32 to 128 character random password>
SAM31_REDIS_VOLUME=
SAM31_REDIS_MAXMEMORY=2gb
SAM31_STATE_MAX_BYTES=268435456
SAM31_SESSION_TTL_SECONDS=28800
SAM31_GPU_DEVICE=0
```

The official model requires Hugging Face access approval. Set `HF_TOKEN` to a
read-capable credential after obtaining access. **Do not place a checkpoint on
the deployment host manually.** The common deployer downloads
`sam3.1_multiplex.pt` from the named repository into a temporary build directory
and embeds it in both function images. The token is used only by that downloader;
it is excluded from subprocess environments, build contexts and runtime settings.

To select fine-tuned weights, set `SAM31_MODEL=owner/repository` for a repository
containing the same compatible merged checkpoint format. This changes the weights,
not the multiplex architecture. Unset/empty `SAM31_MODEL` retains the official
default. No model-path or expected-SHA compatibility setting is provided.

```sh
components/extensions/cvatctl --env-file /path/to/deployment.env down
# Change model/settings while stopped.
components/extensions/cvatctl --env-file /path/to/deployment.env up
components/extensions/cvatctl --env-file /path/to/deployment.env check
# Explicitly refresh/rebuild both functions, or add image / tracker:
components/extensions/cvatctl --env-file /path/to/deployment.env deploy-sam31
# Existing-image restart; no weight source or download credential is needed:
components/extensions/cvatctl --env-file /path/to/deployment.env up --no-build
```

Only `cvatctl` deploys these functions. It uses the common structured generator
and `components/sam31/build.json`, not a separate SAM3.1 deployment program.
See [model selection and common deployment](../extensions/MODEL_SELECTION.md)
for build/reuse semantics and credential handling. Keep `.env` mode `0600`.

The default Redis volume is `<COMPOSE_PROJECT_NAME>_sam31_redis_data`. It is
persistent and cannot be shared with SAM2. `cvatctl init` generates independent
passwords. The manager selects plugins/Compose files, starts and health-checks
both selected Redis services before restoring functions, and manages status,
checks, stops and restart policies. To disable SAM3.1, stop first, remove `sam31`
from the selection, then start again. Disabled functions remain stopped and
volumes are retained. Do not publish licensed function images without authorization.

## Model and state contract

The adapter pins Meta's SAM repository to
`0570b3a5be9c4e694f23d85232fb55f4a6f1f7fc`. It uses the SAM3.1 multiplex tracker and
shared visual backbone; text/detection heads are not instantiated. Missing or
incompatible tracking parameters cause startup to fail. The image uses Python
3.12, PyTorch 2.10 and CUDA 12.8, with compilation and FlashAttention 3 disabled.
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
