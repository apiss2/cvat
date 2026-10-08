# SPDX-License-Identifier: CC-BY-NC-SA-4.0
"""Interactive point adapter for the original UltraSAM decoder.

Adapted from CAMMA-public/UltraSam (Adrien Meyer and contributors), commit
ff3157b1fca8b1d963d9138372768e1fecad71e9, SAMPaddingGenerator.forward.
Changes: one active instance; variable positive/negative points; reuse the
official process_prompt method; return raw decoder inputs without dataset I/O.
https://github.com/CAMMA-public/UltraSam
"""

import torch
import torch.nn.functional as F


def encode_points(encoder, point_coords, point_labels, embedding_index):
    """Coordinates are already resized and shifted by the upstream +0.5 offset."""
    if (
        point_coords.ndim != 2
        or point_coords.shape[1] != 2
        or point_labels.shape != point_coords.shape[:1]
        or len(point_coords) < 1
    ):
        raise ValueError("Expected N point coordinates and N binary point labels")
    if (
        not torch.isfinite(point_coords).all()
        or not ((point_labels == 0) | (point_labels == 1)).all()
    ):
        raise ValueError("Expected finite coordinates and binary point labels")
    device = point_coords.device
    n_points = len(point_coords)
    # A no-point token follows point prompts, as in the official single-point path.
    n_queries = n_points + 1 + encoder.n_output_tokens
    if encoder.n_output_tokens != 5:
        raise RuntimeError("Unsupported UltraSAM output-token configuration")
    points = torch.zeros((1, 1, n_queries, 2), dtype=torch.float32, device=device)
    points[0, 0, :n_points] = point_coords
    labels = torch.full(
        (1, 1, n_queries),
        embedding_index.NOT_A_POINT.value,
        dtype=torch.long,
        device=device,
    )
    labels[0, 0, :n_points] = torch.where(
        point_labels.bool(), embedding_index.POS.value, embedding_index.NEG.value
    )
    labels[0, 0, -5:] = torch.tensor(
        [
            embedding_index.MASK_OUT.value,
            embedding_index.MASK_OUT_1.value,
            embedding_index.MASK_OUT_2.value,
            embedding_index.MASK_OUT_3.value,
            embedding_index.IOU_OUT.value,
        ],
        dtype=torch.long,
        device=device,
    )
    height, width = encoder.image_embedding_size
    dense = (
        encoder.label_encoder.label_embedding.weight[
            embedding_index.NON_INIT_MASK_EMBED.value
        ]
        .reshape(1, 1, -1, 1, 1)
        .expand(1, 1, -1, height, width)
    )
    padding = torch.zeros((1, 1, n_queries), dtype=torch.float32, device=device)
    attn_mask = torch.zeros((n_queries, n_queries), dtype=torch.float32, device=device)
    padding, attn_mask, points, labels, embedded, dense = encoder.process_prompt(
        padding, attn_mask, points, labels, dense
    )
    return dict(
        prompt_padding_masks=padding,
        attn_mask=attn_mask,
        padded_points=points,
        padded_labels=labels,
        pts_embed=embedded,
        dense_embed=dense,
    )


def restore_mask(logits, padded_size, resized_size, original_size):
    """Resize logits, crop padding, restore the original raster, then threshold."""
    if logits.ndim != 2 or not torch.isfinite(logits).all():
        raise ValueError("Expected finite two-dimensional mask logits")
    if any(
        len(size) != 2 or min(size) <= 0
        for size in (padded_size, resized_size, original_size)
    ):
        raise ValueError("Invalid image shape")
    if any(a > b for a, b in zip(resized_size, padded_size)):
        raise ValueError("Resized image exceeds the padded shape")
    mask = F.interpolate(
        logits[None, None].float(),
        size=padded_size,
        mode="bilinear",
        align_corners=False,
    )
    mask = mask[..., : resized_size[0], : resized_size[1]]
    mask = F.interpolate(mask, size=original_size, mode="bilinear", align_corners=False)
    return mask[0, 0] > 0
