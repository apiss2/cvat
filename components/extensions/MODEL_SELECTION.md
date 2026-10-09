# SAM model selection and common deployment

Set model names in the private `.env` read by `cvatctl --env-file`. Both image
interaction and tracking use the same selection within a model family. The
manager's persistent settings take precedence over inherited shell exports.

```dotenv
SAM2_MODEL=sam2.1_hiera_small
SAM31_MODEL=facebook/sam3.1
HF_TOKEN=
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

## SAM3.1 models

`SAM31_MODEL` is a Hugging Face model name in `owner/repository` form. Its default
is `facebook/sam3.1`, the original official model. Another named repository must
contain a **compatible merged `sam3.1_multiplex.pt`**. Different model structures,
SAM3 image-only weights and repackaged safetensors files are not interchangeable.
No local checkpoint path is required or accepted as the model name.

The deployer obtains the checkpoint over HTTPS during image preparation, copies
it into both images at `/opt/nuclio/sam3.1_multiplex.pt`, and removes its temporary
build directory after deployment. Runtime functions have `HF_HUB_OFFLINE=1` and
neither mount external weights nor download them during inference.

The official repository requires access approval and acceptance of its terms:
https://huggingface.co/facebook/sam3.1
After obtaining access, set a read-capable `HF_TOKEN` in the same private `.env`.
The downloader uses that credential only for the Hugging Face request. It is
not forwarded to redirected download hosts, subprocesses, build directives,
image source files, function runtime environments or image-name fingerprints.
Keep the configuration file mode `0600`. Do not publish licensed images to an
unauthorized registry. No alternate mirror is selected on authentication failure.

## Builds and restarts

```sh
components/extensions/cvatctl --env-file /path/to/deployment.env down
# Edit SAM2_MODEL or SAM31_MODEL while stopped.
components/extensions/cvatctl --env-file /path/to/deployment.env up
components/extensions/cvatctl --env-file /path/to/deployment.env check
```

`up --no-build` uses matching already-built images. It needs neither download
credentials nor SAM3.1 source files/network access. Missing images fail before
startup. SAM3.1 image identification depends on the model name, source and build
recipe, not an administrator's local checkpoint path. Unchanged running images
can also be reused by ordinary `up`.

Named repositories are fetched from their current `main` revision when a build
is needed. An unchanged running image is not polled for upstream weight changes.
Use `deploy-sam31` to explicitly rebuild/refresh the same model name; `image`,
`tracker` or `all` selects the redeployment scope. Both functions remain selected
by ordinary `up`. The optional SAM2 local-path setting must remain unchanged for
`--no-build`; it uses the last built contents, not changes to that source file.

There is no expected-SHA setting or manual checkpoint checksum comparison.
Automatic content identity remains for rejecting incompatible saved tracking
state, as do hashes for request replay, image-embedding reuse and image names.
Content identity and strict loading do not authenticate a model's publisher.

## Shared implementation

`components/extensions/functions.py` owns named-model resolution, structured
Nuclio definition generation, temporary source preparation, project creation,
image reuse checks and deployment for SAM2, SAM3.1 and UltraSAM. Each family's
`components/<family>/build.json` retains only its Python/CUDA/dependency recipe.
There are no parallel YAML string-rewriting or SAM3.1-only deployment paths.
The common `cvatctl` owns locking, Compose lifecycle and recorded state.

SAM2 and SAM3.1 use byte-identical protocol, geometry and Redis helper source.
Their handlers pass `SAM2` or `SAM31` explicitly when creating the Redis store.
Neural adapters and state codecs remain independent. Redis services/volumes and
passwords remain separate; both image and tracking functions still start by default.
