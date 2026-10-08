#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""REAL Redis + synthetic SAM2 contracts. Not part of the default unit-test run.

SAM2_TEST_REDIS_URL must refer to a disposable test Redis. Only random-prefix
keys created here are removed; this script never calls FLUSHDB or shuts Redis down.
Run `python tests/redis_integration.py`. Missing Redis/redis-py is an error, not a pass.
"""
import base64
import copy
import io
import os
from pathlib import Path
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nuclio"))
from PIL import Image
from fakes import FakePredictor
from redis_store import RedisStore
from redis_tracker import RedisTracker
from temporal_video import TemporalVideo
from protocol import ProtocolError


class RealRedisTests(unittest.TestCase):
    def setUp(self):
        import redis
        url = os.environ["SAM2_TEST_REDIS_URL"]
        self.client = redis.Redis.from_url(url, decode_responses=False, socket_timeout=10)
        self.client.ping()
        self.prefix = "sam2-test:" + uuid.uuid4().hex + ":"
        self.store = RedisStore(self.client, prefix=self.prefix, errors=(redis.exceptions.RedisError,))
        self.meta = dict(identity="a" * 64, count=1, width=24, height=20)
        self.shape = {"type": "polygon", "points": [2, 2, 12, 2, 12, 12, 2, 12]}

    def tearDown(self):
        for key in self.client.scan_iter(match=self.prefix + "*", count=100):
            self.client.unlink(key)
        self.client.close()

    def test_real_lua_create_cas_retry_and_conflict(self):
        sid = secrets.token_urlsafe(32)
        self.assertTrue(self.store.create(sid, b"state-0", self.meta, [self.shape]))
        self.assertFalse(self.store.create(sid, b"other", self.meta, [None]))
        reply = self.store.commit(sid, 0, "a" * 64, b"state-1", [None])
        self.assertEqual(reply, [None])
        self.assertEqual(self.store.commit(sid, 0, "a" * 64, b"must-not-save", [self.shape]), [None])
        self.assertEqual(self.store.load(sid).payload, b"state-1")
        with self.assertRaises(ProtocolError):
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
                self.assertEqual(error.status, 409)
                return False
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, ["a" * 64, "b" * 64]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.load(sid).revision, 1)

    def test_missing_key_is_not_resurrected(self):
        sid = secrets.token_urlsafe(32)
        self.store.create(sid, b"state-0", self.meta, [self.shape])
        self.client.delete(self.store._key(sid))
        with self.assertRaises(ProtocolError):
            self.store.commit(sid, 0, "a" * 64, b"state-1", [self.shape])
        self.assertFalse(self.client.exists(self.store._key(sid)))

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
        self.assertEqual(advanced["states"][0]["seq"], 1)
        third = RedisTracker(TemporalVideo(FakePredictor()), self.store, "a" * 64)
        self.assertEqual(third(args), advanced)
        self.assertEqual(third.video.predictor.step_calls, 0)
        self.assertGreater(self.client.ttl(self.store._key(initial["states"][0]["id"])), 0)


if __name__ == "__main__":
    if not os.getenv("SAM2_TEST_REDIS_URL"):
        raise SystemExit("Set SAM2_TEST_REDIS_URL to a disposable Redis test service")
    unittest.main(verbosity=2)
