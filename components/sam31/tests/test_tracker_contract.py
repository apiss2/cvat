# SPDX-License-Identifier: MIT
"""Validate tracker requests and publication boundaries using the real codec."""

import base64
import copy
import io
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest


SEED = {"type": "polygon", "points": [2, 2, 15, 2, 15, 15, 2, 15]}


def image_request(color="black"):
    image = Image.new("RGB", (24, 20), color)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return {"image": base64.b64encode(stream.getvalue()).decode()}


@pytest.fixture
def tracker(sam31_runtime, sam31_snapshot):
    class Video:
        def __init__(self):
            self.calls = []
        def initialize(self, image, masks):
            self.calls.append("initialize")
            return sam31_snapshot(count=len(masks)), masks
        def advance(self, image, state):
            self.calls.append("advance")
            return sam31_snapshot(index=state.index + 1, count=state.count), [
                np.ones((image.height, image.width), dtype=bool) for _ in range(state.count)
            ]

    class Store:
        max_bytes = 256 * 1024**2
        prefix = "test:sam31:"
        _key = sam31_runtime.redis_store.RedisStore._key
        def __init__(self):
            self.records, self.created, self.commits = {}, [], []
        def create(self, sid, payload, meta, shapes):
            self.created.append(sid)
            self.records[sid] = SimpleNamespace(
                revision=0, payload=payload, meta=meta, shapes=shapes, request=""
            )
            return True
        def load(self, sid):
            return self.records[sid]
        def commit(self, sid, expected, request, payload, shapes):
            record = self.records[sid]
            assert record.revision == expected
            self.commits.append((sid, expected, request))
            record.revision += 1
            record.request, record.payload, record.shapes = request, payload, shapes
            return shapes

    video, store = Video(), Store()
    return sam31_runtime.tracker.Tracker(video, store, "a" * 64)


@pytest.fixture
def initialized(tracker):
    result = tracker({**image_request(), "shapes": [SEED, SEED]})
    tracker.video.calls.clear()
    return result


@pytest.mark.parametrize("arguments,status", [
    ({"shapes": None}, 400), ({"states": {}}, 400),
    ({}, 400), ({"shapes": [SEED] * 17}, 413), ({"states": [None] * 17}, 413),
])
def test_invalid_request_has_no_model_or_store_effects(tracker, sam31_runtime, arguments, status):
    with pytest.raises(sam31_runtime.protocol.ProtocolError) as error:
        tracker({**image_request(), **arguments})
    assert error.value.status == status
    assert tracker.video.calls == [] and tracker.store.records == {}


@pytest.mark.parametrize("states", [
    [None], [{"id": "bad"}],
    [{"v": 1, "id": "a" * 43, "seq": True, "obj": 0, "count": 1}],
    [{"v": 1, "id": "a" * 43, "seq": -1, "obj": 0, "count": 1}],
    [{"v": 1, "id": "a" * 43, "seq": 2**31 - 1, "obj": 0, "count": 1}],
    [{"v": 1, "id": "a" * 43, "seq": 0, "obj": 0, "count": 1, "extra": 1}],
    [{"v": 1, "id": "a" * 43, "seq": 0, "obj": 1, "count": 1}],
])
def test_malformed_tokens_are_rejected_before_loading_state(tracker, sam31_runtime, states, monkeypatch):
    monkeypatch.setattr(tracker.store, "load", lambda *_: pytest.fail("Malformed state reached storage"))
    with pytest.raises(sam31_runtime.protocol.ProtocolError):
        tracker({**image_request(), "states": states})
    assert tracker.video.calls == []


@pytest.mark.parametrize("collisions", [2, 3])
def test_initial_state_collision_retries_are_bounded(tracker, sam31_runtime, monkeypatch, collisions):
    ids = iter([letter * 43 for letter in "abc"])
    monkeypatch.setattr(sam31_runtime.tracker.secrets, "token_urlsafe", lambda size: next(ids))
    create = tracker.store.create
    attempts = []
    def collision(sid, *args):
        attempts.append(sid)
        return False if len(attempts) <= collisions else create(sid, *args)
    monkeypatch.setattr(tracker.store, "create", collision)
    if collisions == 3:
        with pytest.raises(RuntimeError, match="Unable to allocate"):
            tracker({**image_request(), "shapes": [SEED]})
        assert tracker.store.records == {}
    else:
        result = tracker({**image_request(), "shapes": [SEED]})
        assert result["states"][0]["id"] == "c" * 43
    assert attempts == [letter * 43 for letter in "abc"]
    assert tracker.video.calls == ["initialize"]


@pytest.mark.parametrize("seeds", [[SEED, SEED], [None], [None, SEED]])
def test_continuation_rejects_new_or_partial_seeds(tracker, initialized, sam31_runtime, seeds):
    with pytest.raises(sam31_runtime.protocol.ProtocolError, match="states only"):
        tracker({**image_request(), "states": initialized["states"], "shapes": seeds})
    assert tracker.video.calls == [] and tracker.store.commits == []


def test_null_seed_placeholders_continue_the_same_run(tracker, initialized):
    result = tracker({**image_request(), "states": initialized["states"], "shapes": [None, None]})
    assert result["states"] == [dict(token, seq=1) for token in initialized["states"]]
    assert len(result["shapes"]) == 2 and tracker.video.calls == ["advance"]


@pytest.mark.parametrize("key,value", [("identity", "b" * 64), ("count", 1), ("width", 12), ("height", 10)])
def test_changed_model_or_request_dimensions_never_advance(tracker, initialized, sam31_runtime, key, value):
    record = tracker.store.records[initialized["states"][0]["id"]]
    record.meta[key] = value
    with pytest.raises(sam31_runtime.protocol.ProtocolError):
        tracker({**image_request(), "states": initialized["states"]})
    assert tracker.video.calls == [] and tracker.store.commits == []


@pytest.mark.parametrize("change", [{"index": 1}, {"count": 1}, {"size": (12, 10)}])
def test_inconsistent_snapshot_is_rejected_before_model_execution(
    tracker, initialized, sam31_runtime, sam31_snapshot, change,
):
    record = tracker.store.records[initialized["states"][0]["id"]]
    record.payload = tracker.codec.encode(sam31_snapshot(**change))
    with pytest.raises(sam31_runtime.protocol.ProtocolError, match="Inconsistent"):
        tracker({**image_request(), "states": initialized["states"]})
    assert tracker.video.calls == [] and tracker.store.commits == []


def test_retry_uses_cached_winner_but_different_image_is_a_conflict(tracker, initialized, sam31_runtime):
    request = {**image_request(), "states": initialized["states"]}
    result = tracker(request)
    assert tracker(request) == result
    with pytest.raises(sam31_runtime.protocol.ProtocolError, match="out-of-order") as error:
        tracker({**image_request("red"), "states": initialized["states"]})
    assert error.value.status == 409
    assert tracker.video.calls == ["advance"] and len(tracker.store.commits) == 1


def test_concurrent_identical_request_returns_committed_shapes(tracker, initialized, monkeypatch):
    winning_shapes = [None, None]
    def commit(sid, expected, request, payload, candidates):
        assert expected == 0 and candidates != winning_shapes
        assert tracker.codec.decode(payload).index == 1
        return winning_shapes
    monkeypatch.setattr(tracker.store, "commit", commit)
    result = tracker({**image_request(), "states": initialized["states"]})
    assert result["shapes"] is winning_shapes
    assert all(token["seq"] == 1 for token in result["states"])


def test_failed_inference_leaves_state_unchanged_and_releases_lock(tracker, initialized, monkeypatch):
    before = copy.deepcopy(vars(tracker.store.records[initialized["states"][0]["id"]]))
    def fail(*_):
        raise RuntimeError("inference failed")
    monkeypatch.setattr(tracker.video, "advance", fail)
    with pytest.raises(RuntimeError, match="inference failed"):
        tracker({**image_request(), "states": initialized["states"]})
    assert vars(tracker.store.records[initialized["states"][0]["id"]]) == before
    assert tracker.store.commits == []
    assert tracker.model_lock.acquire(blocking=False)
    tracker.model_lock.release()


def test_corrupt_saved_payload_never_reaches_inference(tracker, initialized, sam31_runtime):
    record = tracker.store.records[initialized["states"][0]["id"]]
    record.payload = b"invalid state bytes"
    with pytest.raises(sam31_runtime.protocol.ProtocolError, match="invalid or incompatible"):
        tracker({**image_request(), "states": initialized["states"]})
    assert tracker.video.calls == [] and tracker.store.commits == []
