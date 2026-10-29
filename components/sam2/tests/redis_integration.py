#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""REAL Redis + synthetic SAM2 contracts. Not part of the default unit-test run.

SAM2_TEST_REDIS_URL must refer to a disposable test Redis. Only random-prefix
keys created here are removed; this script never calls FLUSHDB or shuts Redis down.
Run `python -m pytest components/sam2/tests/redis_integration.py` from the repository root.
Missing Redis/redis-py is an error, not a pass.
"""
import base64
import io
import os
from pathlib import Path
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
import uuid
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "serverless/pytorch/facebookresearch/sam2/nuclio"))
from PIL import Image
from fakes import FakePredictor
from redis_store import RedisStore
from redis_tracker import RedisTracker
from temporal_video import TemporalVideo
from protocol import ProtocolError


class TestRealRedis:
    @pytest.fixture(autouse=True)
    def redis_session(self, request):
        url = os.getenv("SAM2_TEST_REDIS_URL")
        if not url:
            pytest.fail("Set SAM2_TEST_REDIS_URL to a disposable Redis test service", pytrace=False)
        import redis

        self.client = redis.Redis.from_url(url, decode_responses=False, socket_timeout=10)
        request.addfinalizer(self.client.close)
        self.client.ping()
        self.prefix = "sam2-test:" + uuid.uuid4().hex + ":"
        request.addfinalizer(self.remove_test_keys)
        self.store = RedisStore(self.client, prefix=self.prefix, errors=(redis.exceptions.RedisError,))
        self.meta = dict(identity="a" * 64, count=1, width=24, height=20)
        self.shape = {"type": "polygon", "points": [2, 2, 12, 2, 12, 12, 2, 12]}

    def remove_test_keys(self):
        for key in self.client.scan_iter(match=self.prefix + "*", count=100):
            self.client.unlink(key)

    def test_real_lua_create_cas_retry_and_conflict(self):
        sid = secrets.token_urlsafe(32)
        assert self.store.create(sid, b'state-0', self.meta, [self.shape])
        assert not self.store.create(sid, b'other', self.meta, [None])
        reply = self.store.commit(sid, 0, "a" * 64, b"state-1", [None])
        assert reply == [None]
        assert self.store.commit(sid, 0, 'a' * 64, b'must-not-save', [self.shape]) == [None]
        assert self.store.load(sid).payload == b'state-1'
        with pytest.raises(ProtocolError):
            self.store.commit(sid, 0, "b" * 64, b"conflicting", [self.shape])

    def test_real_concurrent_cas(self):
        sid = secrets.token_urlsafe(32)
        self.store.create(sid, b"state-0", self.meta, [self.shape])
        barrier = threading.Barrier(2)
        def attempt(digest):
            barrier.wait(timeout=5)
            try:
                self.store.commit(sid, 0, digest, digest.encode(), [self.shape])
                return True
            except ProtocolError as error:
                assert error.status == 409
                return False
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, ["a" * 64, "b" * 64]))
        assert sum(results) == 1
        assert self.store.load(sid).revision == 1

    def test_missing_key_is_not_resurrected(self):
        sid = secrets.token_urlsafe(32)
        self.store.create(sid, b"state-0", self.meta, [self.shape])
        self.client.delete(self.store._key(sid))
        with pytest.raises(ProtocolError):
            self.store.commit(sid, 0, "a" * 64, b"state-1", [self.shape])
        assert not self.client.exists(self.store._key(sid))

    def test_worker_reconstruction_uses_redis_snapshot(self):
        stream = io.BytesIO()
        Image.new("RGB", (24, 20), 90).save(stream, format="PNG")
        image = {"image": base64.b64encode(stream.getvalue()).decode()}
        first = RedisTracker(TemporalVideo(FakePredictor()), self.store, "a" * 64)
        initial = first({**image, "shapes": [self.shape]})
        del first
        second = RedisTracker(TemporalVideo(FakePredictor()), self.store, "a" * 64)
        args = {**image, "states": initial["states"]}
        advanced = second(args)
        assert advanced['states'][0]['seq'] == 1
        third = RedisTracker(TemporalVideo(FakePredictor()), self.store, "a" * 64)
        assert third(args) == advanced
        assert third.video.predictor.step_calls == 0
        assert self.client.ttl(self.store._key(initial['states'][0]['id'])) > 0
