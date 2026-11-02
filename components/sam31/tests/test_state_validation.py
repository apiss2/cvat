# SPDX-License-Identifier: MIT
"""Reject malformed saved state before handing bytes to the tensor loader."""

import json
import struct

import pytest


def rewrite(data, mutation):
    size = struct.unpack("<Q", data[:8])[0]
    header = json.loads(data[8:8 + size])
    mutation(header)
    encoded = json.dumps(header, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 8)
    return struct.pack("<Q", len(encoded)) + encoded + data[8 + size:]


@pytest.fixture
def saved_state(sam31_runtime, sam31_snapshot):
    codec = sam31_runtime.state_codec.StateCodec("a" * 64)
    return codec, codec.encode(sam31_snapshot(index=2))


@pytest.fixture
def reject_before_load(sam31_runtime, monkeypatch):
    def unexpected_load(_):
        pytest.fail("Malformed state reached the safetensors loader")
    monkeypatch.setattr(sam31_runtime.state_codec, "load", unexpected_load)
    def reject(codec, data, status=409):
        with pytest.raises(sam31_runtime.protocol.ProtocolError) as error:
            codec.decode(data)
        assert error.value.status == status
    return reject


@pytest.mark.parametrize("data", [b"", b"12345678", bytearray(b"not a bytes object")])
def test_invalid_envelope_is_rejected(saved_state, reject_before_load, data):
    reject_before_load(saved_state[0], data, 413)


@pytest.mark.parametrize("size", [0, 1, 65537, 2**63])
def test_invalid_header_size_is_rejected(saved_state, reject_before_load, size):
    codec, data = saved_state
    reject_before_load(codec, struct.pack("<Q", size) + data[8:])


@pytest.mark.parametrize("key,value", [
    ("schema", True), ("schema", 2), ("identity", "b" * 64),
    ("index", True), ("width", 2.0), ("height", "20"), ("count", False),
    ("index", -1), ("index", 2**31), ("width", 0), ("height", -1),
    ("width", 100_000_000), ("count", 0), ("count", 17),
])
def test_invalid_metadata_rejected_before_tensor_load(saved_state, reject_before_load, key, value):
    codec, data = saved_state
    def mutation(header):
        metadata = json.loads(header["__metadata__"]["state"])
        metadata[key] = value
        header["__metadata__"]["state"] = json.dumps(metadata)
    reject_before_load(codec, rewrite(data, mutation))


@pytest.mark.parametrize("change", ["missing_field", "extra_field", "duplicate_key", "not_object"])
def test_metadata_schema_is_exact(saved_state, reject_before_load, change):
    codec, data = saved_state
    def mutation(header):
        metadata = json.loads(header["__metadata__"]["state"])
        if change == "missing_field":
            metadata.pop("index")
        elif change == "extra_field":
            metadata["unexpected"] = 1
        elif change == "not_object":
            metadata = []
        encoded = json.dumps(metadata)
        if change == "duplicate_key":
            encoded = '{"count":2,' + encoded[1:]
        header["__metadata__"]["state"] = encoded
    reject_before_load(codec, rewrite(data, mutation))


@pytest.mark.parametrize("name", [
    "not-a-memory-name", "c.00.obj_ptr", "c.1.obj_ptr", "n.0.obj_ptr", "n.3.obj_ptr",
])
def test_tensor_names_and_frame_indices_are_validated(saved_state, reject_before_load, name):
    codec, data = saved_state
    def mutation(header):
        header[name] = header.pop("c.0.obj_ptr")
    reject_before_load(codec, rewrite(data, mutation))


@pytest.mark.parametrize("field,value", [
    ("dtype", "I64"), ("shape", [1, 16]), ("shape", [1, 0, 2]),
    ("shape", [1, -1, 2]), ("shape", [1, True, 2]), ("shape", [1, 16385, 2]),
    ("data_offsets", [False, 128]), ("data_offsets", [0, True]),
    ("data_offsets", [-1, 127]), ("data_offsets", [0, 1]),
    ("data_offsets", [10**8, 10**8 + 128]),
])
def test_tensor_layout_rejected_before_tensor_load(saved_state, reject_before_load, field, value):
    codec, data = saved_state
    reject_before_load(codec, rewrite(data, lambda header: header["c.0.obj_ptr"].update({field: value})))


@pytest.mark.parametrize("change", ["too_few", "missing_seed", "missing_latest", "partial_history"])
def test_required_memory_groups_are_present(saved_state, reject_before_load, change):
    codec, data = saved_state
    def mutation(header):
        if change == "too_few":
            for key in list(header):
                if key != "__metadata__" and key != "c.0.obj_ptr":
                    header.pop(key)
        elif change == "missing_seed":
            header.pop("c.0.obj_ptr")
        elif change == "missing_latest":
            for key in list(header):
                if key.startswith("n.2."):
                    header.pop(key)
        else:
            header["n.1.maskmem_features"] = header["c.0.maskmem_features"]
    reject_before_load(codec, rewrite(data, mutation))


def test_overlapping_descriptors_cannot_bypass_total_allocation_limit(saved_state, sam31_runtime, reject_before_load):
    _, data = saved_state
    size = struct.unpack("<Q", data[:8])[0]
    payload_size = len(data) - 8 - size
    def mutation(header):
        for name, info in header.items():
            if name == "__metadata__":
                continue
            rank = 4 if name.endswith(("maskmem_features", "maskmem_pos_enc")) else 3
            info.update(shape=[1] * (rank - 1) + [payload_size // 4], data_offsets=[0, payload_size])
    corrupted = rewrite(data, mutation)
    codec = sam31_runtime.state_codec.StateCodec("a" * 64, max_bytes=max(1024, len(corrupted)))
    reject_before_load(codec, corrupted)


def test_duplicate_tensor_names_are_rejected(saved_state, reject_before_load):
    codec, data = saved_state
    size = struct.unpack("<Q", data[:8])[0]
    header = data[8:8 + size].strip()
    repeated = b'{"c.0.obj_ptr":{},' + header[1:]
    repeated += b" " * (-len(repeated) % 8)
    reject_before_load(codec, struct.pack("<Q", len(repeated)) + repeated + data[8 + size:])


def test_valid_pointer_only_history_is_retained(sam31_runtime, sam31_snapshot):
    codec = sam31_runtime.state_codec.StateCodec("a" * 64)
    state = sam31_snapshot(index=2)
    state.outputs["non_cond_frame_outputs"][1] = {
        "obj_ptr": state.outputs["cond_frame_outputs"][0]["obj_ptr"]
    }
    restored = codec.decode(codec.encode(state))
    assert set(restored.outputs["non_cond_frame_outputs"][1]) == {"obj_ptr"}
    assert restored.index == 2 and restored.count == 2
