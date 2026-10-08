# SPDX-License-Identifier: MIT
"""Versioned, allocation-bounded SAM3.1 tensor state. No pickle deserialization."""
import json
import math
import re
import struct

import torch
from safetensors.torch import load, save
from protocol import MAX_PIXELS, ProtocolError
from temporal_video import MEMORY_FIELDS, Snapshot

DTYPES = {"F32": 4, "BF16": 2, "F16": 2}
NAME = re.compile(r"(c|n)\.(0|[1-9][0-9]{0,9})\.(obj_ptr|maskmem_features|maskmem_pos_enc|image_features|image_pos_enc)\Z")


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


class StateCodec:
    def __init__(self, identity, *, max_bytes=256 * 1024**2):
        if not re.fullmatch(r"[0-9a-f]{64}", identity) or not 1024 <= max_bytes <= 512 * 1024**2:
            raise ValueError("Invalid SAM3.1 state identity or byte limit")
        self.identity, self.max_bytes = identity, max_bytes

    def encode(self, snapshot):
        tensors = {}
        for kind, name in (("c", "cond_frame_outputs"), ("n", "non_cond_frame_outputs")):
            for frame, memory in snapshot.outputs[name].items():
                for field, value in memory.items():
                    if field not in MEMORY_FIELDS:
                        raise ValueError("Unknown multiplex memory field")
                    value = value[-1] if field == "maskmem_pos_enc" else value
                    if not isinstance(value, torch.Tensor) or value.dtype not in (torch.float32, torch.float16, torch.bfloat16):
                        raise ValueError("Unexpected multiplex memory tensor")
                    tensors[f"{kind}.{frame}.{field}"] = value.detach().cpu().contiguous().clone()
        if sum(value.numel() * value.element_size() for value in tensors.values()) > self.max_bytes:
            raise ProtocolError("Temporal memory exceeds SAM31_STATE_MAX_BYTES", 413)
        metadata = dict(schema=1, identity=self.identity, index=snapshot.index,
                        width=snapshot.width, height=snapshot.height, count=snapshot.count)
        result = save(tensors, metadata={"state": json.dumps(metadata, separators=(",", ":"))})
        # Validate our outgoing schema as well as the incoming allocation bounds.
        self._inspect(result)
        return result

    def _inspect(self, data):
        if not isinstance(data, bytes) or not 8 < len(data) <= self.max_bytes:
            raise ProtocolError("Temporal memory exceeds SAM31_STATE_MAX_BYTES or is invalid", 413)
        size = struct.unpack("<Q", data[:8])[0]
        if not 2 <= size <= min(65536, len(data) - 8):
            raise ValueError("Invalid tensor header size")
        header = json.loads(data[8:8 + size], object_pairs_hook=unique_json)
        metadata = json.loads(header.pop("__metadata__")["state"], object_pairs_hook=unique_json)
        if set(metadata) != {"schema", "identity", "index", "width", "height", "count"}:
            raise ValueError("Invalid state metadata")
        if type(metadata["schema"]) is not int or metadata["schema"] != 1 or metadata["identity"] != self.identity:
            raise ProtocolError("SAM3.1 model or state schema changed; start a new tracking run", 409)
        if any(type(metadata[key]) is not int for key in ("index", "width", "height", "count")):
            raise ValueError("Invalid metadata types")
        if not (0 <= metadata["index"] < 2**31 and 1 <= metadata["count"] <= 4
                and metadata["width"] > 0 and metadata["height"] > 0
                and metadata["width"] * metadata["height"] <= MAX_PIXELS):
            raise ValueError("Invalid metadata bounds")
        if not 5 <= len(header) <= 512:
            raise ValueError("Invalid tensor count")
        groups, total = {}, 0
        for name, info in header.items():
            match = NAME.fullmatch(name)
            if not match:
                raise ValueError("Invalid tensor name")
            kind, frame, field = match.groups()
            frame = int(frame)
            if frame > metadata["index"] or (kind == "c" and frame != 0) or (kind == "n" and frame == 0):
                raise ValueError("Invalid memory frame")
            groups.setdefault((kind, frame), set()).add(field)
            shape, dtype = info["shape"], info["dtype"]
            ranks = (3,) if field in ("obj_ptr", "image_features", "image_pos_enc") else (4, 5)
            if dtype not in DTYPES or len(shape) not in ranks or any(type(dim) is not int or not 1 <= dim <= 16384 for dim in shape):
                raise ValueError("Invalid tensor shape or dtype")
            total += math.prod(shape) * DTYPES[dtype]
            if total > self.max_bytes:
                raise ValueError("Tensor allocation exceeds state limit")
            start, stop = info["data_offsets"]
            if (type(start) is not int or type(stop) is not int or start < 0
                    or stop - start != math.prod(shape) * DTYPES[dtype] or stop > len(data) - 8 - size):
                raise ValueError("Invalid tensor offsets")
        if groups.get(("c", 0)) != set(MEMORY_FIELDS):
            raise ValueError("Missing seed memory")
        if metadata["index"] and groups.get(("n", metadata["index"])) != set(MEMORY_FIELDS):
            raise ValueError("Missing latest memory")
        for fields in groups.values():
            if fields not in ({"obj_ptr"}, set(MEMORY_FIELDS)):
                raise ValueError("Incomplete temporal memory")
        return metadata

    def decode(self, data):
        try:
            metadata = self._inspect(data)
            tensors = load(data)  # CPU tensors only, after checking allocation sizes.
            outputs = {"cond_frame_outputs": {}, "non_cond_frame_outputs": {}}
            for name, value in tensors.items():
                kind, frame, field = NAME.fullmatch(name).groups()
                group = outputs["cond_frame_outputs" if kind == "c" else "non_cond_frame_outputs"]
                group.setdefault(int(frame), {})[field] = [value] if field == "maskmem_pos_enc" else value
            return Snapshot(metadata["index"], metadata["width"], metadata["height"], metadata["count"], outputs)
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError("Stored SAM3.1 state is invalid or incompatible", 409) from exc
