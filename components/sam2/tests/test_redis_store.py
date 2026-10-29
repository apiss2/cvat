# SPDX-License-Identifier: MIT
import json
import secrets
import pytest
from fakes import FakeRedis
from redis_store import RedisStore
from protocol import ProtocolError


def setup_store():
    client = FakeRedis()
    store = RedisStore(client)
    sid = secrets.token_urlsafe(32)
    meta = dict(identity="a" * 64, count=1, width=24, height=20)
    store.create(sid, b"initial", meta, [None])
    return client, store, sid


def test_collision_does_not_overwrite():
    client, store, sid = setup_store()
    assert not store.create(sid, b"other", {}, [])
    assert store.load(sid).payload == b"initial"


def test_identical_late_cas_returns_committed_response_not_recomputed_result():
    _, store, sid = setup_store()
    assert store.commit(sid, 0, "b" * 64, b"one", [None]) == [None]
    assert store.commit(sid, 0, "b" * 64, b"different", ["unused"]) == [None]
    assert store.load(sid).payload == b"one"


@pytest.mark.parametrize("field,value", [(b"revision", b"-1"), (b"meta", b"{}"),
                                           (b"shapes", b"{}"), (b"request", b"bad")])
def test_corrupt_record_rejected(field, value):
    client, store, sid = setup_store()
    client.data[store._key(sid)][field] = value
    with pytest.raises(ProtocolError):
        store.load(sid)


@pytest.mark.parametrize("sid", [None, "../state", "a" * 42, "a" * 44, "\n" * 43])
def test_untrusted_redis_key_rejected(sid):
    with pytest.raises(ProtocolError):
        RedisStore(FakeRedis()).load(sid)


@pytest.mark.parametrize("ttl", [0, -1, 59, 604801, True])
def test_invalid_ttl(ttl):
    with pytest.raises(ValueError):
        RedisStore(FakeRedis(), ttl=ttl)
