# SAM model selection and common deployment

Set model inputs in the private `.env` read by `cvatctl --env-file`. Both image
interaction and tracking use the same selection within a model family. The
manager's persistent settings take precedence over inherited shell exports.

```dotenv
SAM2_MODEL=sam2.1_hiera_small
SAM31_CHECKPOINT_HOST=/opt/models/sam3.1_multiplex.pt
```

## SAM2.1 models

`SAM2_MODEL` selects the checkpoint and its matching configuration together:

| Model name | Configuration |
|---|---|
| `sam2.1_hiera_tiny` | `configs/sam2.1/sam2.1_hiera_t.yaml` |
| `sam2.1_hiera_small` (default) | `configs/sam2.1/sam2.1_hiera_s.yaml` |
| `sam2.1_hiera_base_plus` | `configs/sam2.1/sam2.1_hiera_b+.yaml` |
| `sam2.1_hiera_large` | `configs/sam2.1/sam2.1_hiera_l.yaml` |

The common deployer downloads the selected official weights once into a private
build directory and embeds them in both function images. Unknown names fail.
An unset/empty model name retains the default. `SAM2_CONFIG` and
`SAM2_CHECKPOINT` are derived runtime settings, not independent model overrides.

For optional SAM2 fine-tuned weights, `SAM2_CHECKPOINT_HOST` can name an existing
nonempty file outside the checkout. Select its matching structure in `SAM2_MODEL`.
This override disables the official download. Normal deployment rebuilds local
checkpoint images even when a file is replaced at the same path.

## SAM3.1 checkpoint placement

SAM3.1 uses a pre-placed **compatible merged `sam3.1_multiplex.pt`**. Set its
absolute host path outside the checkout in the private deployment configuration:

```dotenv
SAM31_CHECKPOINT_HOST=/opt/models/sam3.1_multiplex.pt
```

We previously rejected host-managed checkpoints in favor of automatic named-model
downloads. Fresh SAM3.1 builds then required a Hugging Face account, access
approval and download credentials. We now deliberately use checkpoint placement
so deployment hosts can build from an already-obtained weight without HF login,
tokens or another weight download. Obtaining the licensed weight remains the
operator's responsibility. Keep weights out of Git and public image registries.
Replace the former `SAM31_MODEL` / `HF_TOKEN` configuration with the host path.

The common deployer validates a nonempty file before startup, copies it into a
private build directory, and embeds it in both images at
`/opt/nuclio/sam3.1_multiplex.pt`. Missing weights fail without a download fallback.
SAM3 image-only weights and repackaged safetensors files are not interchangeable.
Runtime uses `HF_HUB_OFFLINE=1`, without external weight mounts or downloads.
No manually supplied expected SHA is required; strict model loading and automatic
content identity still reject incompatible weights and saved tracking state.

## Builds and restarts

```sh
components/extensions/cvatctl --env-file /path/to/deployment.env down
# Edit SAM2_MODEL or SAM31_CHECKPOINT_HOST while stopped.
components/extensions/cvatctl --env-file /path/to/deployment.env up
components/extensions/cvatctl --env-file /path/to/deployment.env check
```

Ordinary deployment rebuilds local checkpoint images, including same-path weight
replacements. Source files, model selection, local source path and build recipe
contribute to image identification. SAM2's official named presets still download
automatically when no local SAM2 override is configured.

`up --no-build` uses matching already-built images. Keep the checkpoint path
setting unchanged; the source file need not remain present. It uses embedded
contents, not later changes to the source. Missing images fail before startup.
Nuclio `neverBuild` prevents accidental rebuilding without the embedded weight.
Restarted function containers have their Nuclio routing refreshed for new IPs.
Use `deploy-sam31` (`image`, `tracker` or `all`) to rebuild selected functions.

## Shared implementation

`components/extensions/functions.py` owns model resolution, structured Nuclio
definitions, checkpoint staging, image reuse and deployment for SAM2, SAM3.1 and
UltraSAM. Each family's `components/<family>/build.json` retains its dependency
recipe. The common `cvatctl` owns locking, Compose lifecycle and recorded state.

SAM2 and SAM3.1 share byte-identical protocol, geometry and Redis helper source.
Neural adapters, state codecs, Redis volumes and passwords remain separate.
Image and tracking functions each load their own model; both start by default.
