# SPDX-License-Identifier: MIT
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import pytest
from fakes import FakePredictor, FakeRedis
from temporal_video import TemporalVideo
from redis_store import RedisStore
from redis_tracker import RedisTracker
from protocol import ProtocolError

IDENTITY = "a" * 64


def make(client=None, ttl=28800, identity=IDENTITY):
    client = client if client is not None else FakeRedis()
    predictor = FakePredictor()
    tracker = RedisTracker(TemporalVideo(predictor), RedisStore(client, ttl=ttl), identity)
    return tracker, client, predictor


def seed(tracker, image_body, polygon, count=1):
    return tracker({**image_body(), "shapes": [copy.deepcopy(polygon)] * count})


def test_restart_and_different_worker_restore_without_local_sessions(image_body, polygon):
    first, client, _ = make()
    initial = seed(first, image_body, polygon, 2)
    del first
    second, _, predictor = make(client)
    result = second({**image_body(91), "states": initial["states"], "shapes": [None, None]})
    assert [token["seq"] for token in result["states"]] == [1, 1]
    assert predictor.forward_calls == 1 and predictor.step_calls == 2
    assert len(client.data) == 1


def test_same_pixels_next_revision_is_a_new_frame_but_old_revision_is_retry(image_body, polygon):
    tracker, client, predictor = make()
    initial = seed(tracker, image_body, polygon)
    args = {**image_body(), "states": initial["states"]}
    first = tracker(args)
    assert first == tracker(args)
    assert predictor.step_calls == 2
    second = tracker({**image_body(), "states": first["states"]})
    assert second["states"][0]["seq"] == 2
    with pytest.raises(ProtocolError, match="Stale"):
        tracker(args)


def test_acknowledgement_loss_is_recoverable_on_another_worker(image_body, polygon):
    tracker, client, _ = make()
    initial = seed(tracker, image_body, polygon)
    args = {**image_body(91), "states": initial["states"]}
    client.lose_next_commit_reply = True
    with pytest.raises(ProtocolError) as error:
        tracker(args)
    assert error.value.status == 503
    restarted, _, predictor = make(client)
    reply = restarted(args)
    assert reply["states"][0]["seq"] == 1
    assert predictor.step_calls == 0  # Redis has the committed response already.


@pytest.mark.parametrize("different_image", [False, True])
def test_two_worker_race_has_one_commit(image_body, polygon, different_image):
    first, client, _ = make()
    initial = seed(first, image_body, polygon)
    second, _, _ = make(client)
    barrier = threading.Barrier(2)
    for worker in (first, second):
        original = worker.video.advance
        def synchronized(*args, original=original):
            barrier.wait(timeout=10)
            return original(*args)
        worker.video.advance = synchronized
    def call(worker, value):
        try:
            return worker({**image_body(value), "states": initial["states"]})
        except ProtocolError as error:
            return error
    with ThreadPoolExecutor(2) as executor:
        a = executor.submit(call, first, 91)
        b = executor.submit(call, second, 92 if different_image else 91)
        results = [a.result(timeout=20), b.result(timeout=20)]
    record = first.store.load(initial["states"][0]["id"])
    assert record.revision == 1
    if different_image:
        assert sum(isinstance(result, ProtocolError) for result in results) == 1
    else:
        assert results[0] == results[1]


def test_failure_on_second_object_leaves_whole_batch_unchanged(image_body, polygon):
    tracker, client, predictor = make()
    initial = seed(tracker, image_body, polygon, 2)
    before = copy.deepcopy(client.data)
    original = predictor.track_step
    calls = [0]
    def fail(**args):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("simulated GPU failure")
        return original(**args)
    predictor.track_step = fail
    with pytest.raises(RuntimeError):
        tracker({**image_body(91), "states": initial["states"]})
    assert client.data == before
    predictor.track_step = original
    assert tracker({**image_body(91), "states": initial["states"]})["states"][0]["seq"] == 1


def test_ttl_refresh_and_expiry(image_body, polygon):
    tracker, client, _ = make(ttl=60)
    initial = seed(tracker, image_body, polygon)
    client.now += 59
    next_reply = tracker({**image_body(91), "states": initial["states"]})
    client.now += 59
    next_reply = tracker({**image_body(92), "states": next_reply["states"]})
    client.now += 60
    with pytest.raises(ProtocolError, match="expired"):
        tracker({**image_body(93), "states": next_reply["states"]})


def test_expiration_during_inference_does_not_recreate_session(image_body, polygon):
    tracker, client, _ = make(ttl=60)
    initial = seed(tracker, image_body, polygon)
    original = tracker.video.advance
    def slow(*args):
        result = original(*args)
        client.now += 61
        return result
    tracker.video.advance = slow
    with pytest.raises(ProtocolError, match="expired during"):
        tracker({**image_body(91), "states": initial["states"]})
    assert not client.data


def test_outage_fails_closed_without_new_state(image_body, polygon):
    tracker, client, predictor = make()
    initial = seed(tracker, image_body, polygon)
    client.fail = True
    with pytest.raises(ProtocolError) as error:
        tracker({**image_body(91), "states": initial["states"]})
    assert error.value.status == 503 and predictor.step_calls == 1
    client.fail = False
    assert tracker({**image_body(91), "states": initial["states"]})["states"][0]["seq"] == 1


def test_model_upgrade_rejects_old_state_even_on_retry(image_body, polygon):
    tracker, client, _ = make()
    initial = seed(tracker, image_body, polygon)
    args = {**image_body(91), "states": initial["states"]}
    tracker(args)
    upgraded, _, _ = make(client, identity="b" * 64)
    with pytest.raises(ProtocolError, match="model changed"):
        upgraded(args)


@pytest.mark.parametrize("mutation", [
    lambda states: states[::-1],
    lambda states: states[:1],
    lambda states: [states[0], states[0]],
    lambda states: [{**states[0], "seq": True}, states[1]],
    lambda states: [{**states[0], "v": 1}, states[1]],
    lambda states: [{**states[0], "id": "../bad"}, states[1]],
])
def test_invalid_or_partial_batch_tokens_rejected(image_body, polygon, mutation):
    tracker, _, _ = make()
    initial = seed(tracker, image_body, polygon, 2)
    with pytest.raises(ProtocolError):
        tracker({**image_body(91), "states": mutation(initial["states"])})


def test_frame_dimensions_and_shape_reinitialization_rejected(image_body, polygon):
    tracker, _, _ = make()
    initial = seed(tracker, image_body, polygon)
    with pytest.raises(ProtocolError, match="dimensions"):
        tracker({**image_body(91, size=(30, 20)), "states": initial["states"]})
    with pytest.raises(ProtocolError, match="states only"):
        tracker({**image_body(91), "states": initial["states"], "shapes": [polygon]})


def test_latest_snapshot_overwrites_same_redis_key(image_body, polygon):
    tracker, client, _ = make()
    reply = seed(tracker, image_body, polygon)
    sid = reply["states"][0]["id"]
    for i in range(140):
        reply = tracker({**image_body(), "states": reply["states"]})
    assert reply["states"][0]["seq"] == 140
    assert len(client.data) == 1 and reply["states"][0]["id"] == sid
