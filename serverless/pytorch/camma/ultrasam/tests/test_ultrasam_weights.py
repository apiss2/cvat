# SPDX-License-Identifier: MIT
import hashlib

import pytest

torch = pytest.importorskip("torch")
from ultrasam_model import checkpoint_digest, strict_checkpoint_load


def test_strict_load_copies_every_tensor():
    target = torch.nn.Linear(2, 3)
    state = {
        name: torch.ones_like(value) for name, value in target.state_dict().items()
    }
    strict_checkpoint_load(target, {"state_dict": state})
    assert all(
        torch.equal(value, state[name]) for name, value in target.state_dict().items()
    )


@pytest.mark.parametrize(
    "mutation", ["missing", "unexpected", "shape", "dtype", "nonfinite", "plain_sam"]
)
def test_incompatible_checkpoint_is_rejected_without_partial_loading(mutation):
    target = torch.nn.Linear(2, 3)
    original = {name: value.clone() for name, value in target.state_dict().items()}
    state = {name: torch.ones_like(value) for name, value in original.items()}
    checkpoint = {"state_dict": state}
    if mutation == "missing":
        del state["bias"]
    elif mutation == "unexpected":
        state["wrong"] = torch.ones(1)
    elif mutation == "shape":
        state["weight"] = torch.ones(4, 2)
    elif mutation == "dtype":
        state["weight"] = state["weight"].double()
    elif mutation == "nonfinite":
        state["weight"][0, 0] = float("nan")
    elif mutation == "plain_sam":
        checkpoint = state
    with pytest.raises(RuntimeError):
        strict_checkpoint_load(target, checkpoint)
    for name, value in target.state_dict().items():
        torch.testing.assert_close(value, original[name])


def test_sha256_is_checked_before_deserializing_checkpoint(tmp_path, monkeypatch):
    import ultrasam_model

    checkpoint = tmp_path / "untrusted.pth"
    checkpoint.write_bytes(b"not the official checkpoint")
    assert (
        checkpoint_digest(checkpoint)
        == hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        ultrasam_model.subprocess,
        "check_output",
        lambda *a, **k: ultrasam_model.UPSTREAM_COMMIT,
    )

    def unexpected_load(*args, **kwargs):
        pytest.fail("Unverified checkpoint must not be deserialized")

    monkeypatch.setattr(torch, "load", unexpected_load)
    with pytest.raises(RuntimeError, match="SHA256"):
        ultrasam_model.build_model(
            device="cpu", checkpoint_path=checkpoint, upstream_root=tmp_path
        )
