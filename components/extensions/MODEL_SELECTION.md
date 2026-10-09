# SAM model selection

Set these variables in the persistent `.env` passed to `cvatctl --env-file`.
The manager deliberately ignores conflicting inherited shell exports. Both image
interaction and polygon tracking use the same selected model within each family.
Weights remain embedded in function images, not runtime bind mounts.

## SAM2.1

`SAM2_MODEL` selects the official checkpoint and its matching configuration.
Omission or an empty value retains the previous small model. Supported values:

| SAM2_MODEL | Configuration |
|---|---|
| `sam2.1_hiera_tiny` | `configs/sam2.1/sam2.1_hiera_t.yaml` |
| `sam2.1_hiera_small` (default) | `configs/sam2.1/sam2.1_hiera_s.yaml` |
| `sam2.1_hiera_base_plus` | `configs/sam2.1/sam2.1_hiera_b+.yaml` |
| `sam2.1_hiera_large` | `configs/sam2.1/sam2.1_hiera_l.yaml` |

Unknown names are errors. `SAM2_CONFIG` and `SAM2_CHECKPOINT` are derived runtime
settings, not independent cvatctl overrides; this prevents mismatched model pairs.

```dotenv
SAM2_MODEL=sam2.1_hiera_small
SAM2_CHECKPOINT_HOST=
```

With an empty `SAM2_CHECKPOINT_HOST`, the build downloads the selected official
SAM2.1 checkpoint. To use fine-tuned weights, set an absolute path outside the
checkout and select the matching architecture with `SAM2_MODEL`:

```dotenv
SAM2_MODEL=sam2.1_hiera_large
SAM2_CHECKPOINT_HOST=/srv/cvat-models/sam2.1_hiera_large_ultrasound.pt
```

Local files are copied once into a private build source shared by both function
builds. The official download step is omitted. The runtime loads
`/opt/nuclio/checkpoints/<SAM2_MODEL>.pt`; an arbitrary local filename is allowed.

## SAM3.1

The existing `SAM31_CHECKPOINT_HOST` selects the build input. The normal model
remains the official merged `sam3.1_multiplex.pt`:

```dotenv
SAM31_CHECKPOINT_HOST=/srv/cvat-models/sam3.1_multiplex.pt
```

Point it at a compatible merged fine-tuned checkpoint to change weights. There
is no artificial size selector: the fixed multiplex architecture is unchanged,
and weights from a different architecture are not supported. Both functions use
the selected weights at `/opt/nuclio/sam3.1_multiplex.pt` inside their images.
SAM3.1 source weights must already have been obtained with appropriate access.
An empty setting is not an instruction to download or invent missing weights.

## Rebuilds and existing images

Stop before changing model settings and run a normal `up` to build/deploy them.
Normal deployment always rebuilds local-checkpoint images, including replacement
of a file at the same path. Unchanged Docker layers can still be cached.

```sh
components/extensions/cvatctl --env-file /path/to/deployment.env down
# Edit the model settings or replace the local checkpoint while stopped.
components/extensions/cvatctl --env-file /path/to/deployment.env up
components/extensions/cvatctl --env-file /path/to/deployment.env check
```

For an existing-image restart, `up --no-build` uses the last built weights. Keep
the source path settings unchanged; the original files need not still exist.
This mode does not read source checkpoint contents and does not apply their
changes. Moving or clearing a configured path changes the selected image tag.

There is no manual expected-hash setting or checkpoint checksum comparison.
Automatic hashes remain for model/state identity, request replay, image-embedding
reuse and source-derived image names. Removing those would allow incompatible
state or stale results to be reused. The unused SAM2 checksum manifest is removed.
Content identity does not authenticate the source of downloaded weights.

Changing model weights can invalidate saved tracking sessions; restart those
runs from their seed frame. GPU capacity and inference compatibility of larger
or fine-tuned models require testing on the deployment host.
