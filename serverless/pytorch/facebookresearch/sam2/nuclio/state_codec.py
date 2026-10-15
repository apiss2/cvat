# SPDX-License-Identifier: MIT
"""Bounded safetensors + JSON codec. Never pickle or torch.load state data."""
import json
import re
import struct
import torch
from safetensors.torch import save, load
from protocol import ProtocolError, MAX_PIXELS, MAX_OBJECTS
from temporal_video import Snapshot

NAME = re.compile(r"o(0|[1-9][0-9]?)\.(c|n)\.(0|[1-9][0-9]{0,9})\.(ptr|mem|pos)\Z")
DTYPES = {"F32": 4, "BF16": 2, "F16": 2}


class StateCodec:
    def __init__(self, identity, *, max_bytes=256 * 1024 * 1024):
        if not re.fullmatch(r"[0-9a-f]{64}", identity) or not 1024 <= max_bytes <= 256 * 1024 * 1024:
            raise ValueError("Invalid model identity or state byte limit")
        self.identity, self.max_bytes = identity, max_bytes

    def encode(self, snapshot):
        if not 1 <= len(snapshot.objects) <= MAX_OBJECTS:
            raise ProtocolError(f"Provide 1..{MAX_OBJECTS} objects", 413)
        tensors = {}
        for obj, outputs in enumerate(snapshot.objects):
            for kind, name in (("c", "cond_frame_outputs"), ("n", "non_cond_frame_outputs")):
                for frame, memory in outputs[name].items():
                    prefix = f"o{obj}.{kind}.{frame}"
                    tensors[prefix + ".ptr"] = memory["obj_ptr"].detach().cpu().contiguous().clone()
                    if "maskmem_features" in memory:
                        tensors[prefix + ".mem"] = memory["maskmem_features"].detach().cpu().contiguous().clone()
                        tensors[prefix + ".pos"] = memory["maskmem_pos_enc"][-1].detach().cpu().contiguous().clone()
        total = sum(value.numel() * value.element_size() for value in tensors.values())
        if total > self.max_bytes:
            raise ProtocolError("Temporal memory exceeds SAM2_STATE_MAX_BYTES", 413)
        metadata = dict(schema=2, identity=self.identity, index=snapshot.index,
                        width=snapshot.width, height=snapshot.height, count=len(snapshot.objects))
        data = save(tensors, metadata={"state": json.dumps(metadata, separators=(",", ":"))})
        if len(data) > self.max_bytes:
            raise ProtocolError("Temporal memory exceeds SAM2_STATE_MAX_BYTES", 413)
        return data

    def decode(self, data):
        try:
            return self._decode(data)
        except ProtocolError:
            raise
        except Exception as exc:
            # Do not leak tensor contents, state IDs, or raw Redis data in errors.
            raise ProtocolError("Stored SAM2 state is invalid or incompatible", 409) from exc

    def _decode(self, data):
        if not isinstance(data, bytes) or not 8 < len(data) <= self.max_bytes:
            raise ValueError("State size")
        length = struct.unpack("<Q", data[:8])[0]
        if not 2 <= length <= min(256 * 1024, len(data) - 8):
            raise ValueError("Header size")
        header = json.loads(data[8:8 + length])
        metadata = json.loads(header.pop("__metadata__")["state"])
        if set(metadata) != {"schema", "identity", "index", "width", "height", "count"}:
            raise ValueError("Metadata fields")
        if type(metadata["schema"]) is not int or metadata["schema"] != 2 or metadata["identity"] != self.identity:
            raise ProtocolError("SAM2 model or state schema changed; start a new tracking run", 409)
        for key in ("index", "width", "height", "count"):
            if type(metadata[key]) is not int:
                raise ValueError("Metadata types")
        if not (0 <= metadata["index"] < 2**31 and 1 <= metadata["count"] <= MAX_OBJECTS and
                0 < metadata["width"] <= MAX_PIXELS and 0 < metadata["height"] <= MAX_PIXELS and
                metadata["width"] * metadata["height"] <= MAX_PIXELS):
            raise ValueError("Metadata bounds")
        if not 3 <= len(header) <= 4096:
            raise ValueError("Tensor count")
        # Inspect allocations before letting safetensors construct CPU tensors.
        for name, info in header.items():
            match = NAME.fullmatch(name)
            if not match:
                raise ValueError("Tensor name")
            obj, kind, frame, field = match.groups()
            if int(obj) >= metadata["count"] or int(frame) > metadata["index"]:
                raise ValueError("Tensor index")
            if (kind == "c" and frame != "0") or (kind == "n" and frame == "0"):
                raise ValueError("Conditioning frame")
            shape, dtype = info["shape"], info["dtype"]
            if dtype not in DTYPES or len(shape) != (2 if field == "ptr" else 4):
                raise ValueError("Tensor dtype/rank")
            if shape[0] != 1 or any(type(dim) is not int or not 1 <= dim <= 4096 for dim in shape):
                raise ValueError("Tensor shape")
            elements = 1
            for dim in shape:
                elements *= dim
            if elements * DTYPES[dtype] > self.max_bytes:
                raise ValueError("Tensor allocation")
        tensors = load(data)  # CPU only; arbitrary Python object deserialization is impossible.
        objects = [{"cond_frame_outputs": {}, "non_cond_frame_outputs": {}}
                   for _ in range(metadata["count"])]
        for name, tensor in tensors.items():
            obj, kind, frame, field = NAME.fullmatch(name).groups()
            group = objects[int(obj)]["cond_frame_outputs" if kind == "c" else "non_cond_frame_outputs"]
            item = group.setdefault(int(frame), {})
            key = {"ptr": "obj_ptr", "mem": "maskmem_features", "pos": "maskmem_pos_enc"}[field]
            item[key] = [tensor] if field == "pos" else tensor
        for outputs in objects:
            if set(outputs["cond_frame_outputs"]) != {0}:
                raise ValueError("Missing seed memory")
            for kind, memories in outputs.items():
                for memory in memories.values():
                    fields = set(memory)
                    if fields not in ({"obj_ptr"}, {"obj_ptr", "maskmem_features", "maskmem_pos_enc"}):
                        raise ValueError("Incomplete memory")
                    if kind == "cond_frame_outputs" and fields == {"obj_ptr"}:
                        raise ValueError("Missing conditioning memory")
        return Snapshot(metadata["index"], metadata["width"], metadata["height"], objects)
