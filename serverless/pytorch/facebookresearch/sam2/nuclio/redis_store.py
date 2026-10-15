# SPDX-License-Identifier: MIT
"""Shared Redis state storage with explicit model namespaces and atomic revisions."""
from dataclasses import dataclass
import json
import os
import re
from protocol import ProtocolError, MAX_PIXELS, MAX_OBJECTS

CREATE = """-- tracker-create
if redis.call('EXISTS', KEYS[1]) ~= 0 then return 0 end
redis.call('HSET', KEYS[1], 'revision', '0', 'payload', ARGV[1],
           'meta', ARGV[2], 'request', '', 'shapes', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""
CAS = """-- tracker-cas
local revision = redis.call('HGET', KEYS[1], 'revision')
if not revision then return {-1, ''} end
local current = tonumber(revision)
local expected = tonumber(ARGV[1])
if current == expected + 1 and redis.call('HGET', KEYS[1], 'request') == ARGV[2] then
    return {2, redis.call('HGET', KEYS[1], 'shapes')}
end
if current ~= expected then return {0, ''} end
redis.call('HSET', KEYS[1], 'revision', tostring(expected + 1),
           'payload', ARGV[3], 'request', ARGV[2], 'shapes', ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return {1, ARGV[4]}
"""


def json_bytes(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


@dataclass(frozen=True)
class Record:
    revision: int
    payload: bytes
    meta: dict
    request: str
    shapes: list


class RedisStore:
    def __init__(self, client, *, ttl=28800, prefix="cvat:sam2:",
                 max_bytes=256 * 1024 * 1024, errors=(OSError, TimeoutError)):
        if type(ttl) is not int or not 60 <= ttl <= 604800:
            raise ValueError("State TTL must be 60..604800 seconds")
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{1,100}", prefix):
            raise ValueError("Invalid Redis prefix")
        self.client, self.ttl, self.prefix = client, ttl, prefix
        self.max_bytes, self.errors = max_bytes, errors

    @classmethod
    def from_env(cls, namespace: str):
        import redis
        if namespace not in ("SAM2", "SAM31"):
            raise ValueError("Unknown tracker namespace")
        def setting(name, default=None):
            return os.getenv(namespace + "_" + name, default)
        client = redis.Redis(
            host=setting("REDIS_HOST", namespace.lower() + "_redis"),
            port=int(setting("REDIS_PORT", "6379")),
            db=int(setting("REDIS_DB", "0")),
            username=setting("REDIS_USERNAME") or None,
            password=setting("REDIS_PASSWORD") or None,
            ssl=setting("REDIS_TLS", "false").lower() == "true",
            socket_connect_timeout=5, socket_timeout=30, decode_responses=False,
            health_check_interval=30,
        )
        store = cls(client, ttl=int(setting("SESSION_TTL_SECONDS", "28800")),
                    prefix=setting("REDIS_PREFIX", f"cvat:{namespace.lower()}:"),
                    max_bytes=int(setting("STATE_MAX_BYTES", str(256 * 1024 * 1024))),
                    errors=(redis.exceptions.RedisError, OSError, TimeoutError))
        store._call(client.ping)
        return store

    def _key(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid):
            raise ProtocolError("Invalid tracker state")
        return self.prefix + sid

    def _call(self, method, *args):
        try:
            return method(*args)
        except self.errors as exc:
            raise ProtocolError("Tracking state storage is unavailable; retry the same request", 503) from exc

    def _check_payload(self, payload):
        if not isinstance(payload, bytes) or len(payload) > self.max_bytes:
            raise ProtocolError("Temporal memory exceeds the configured state limit", 413)

    def create(self, sid, payload, meta, shapes):
        self._check_payload(payload)
        return bool(self._call(self.client.eval, CREATE, 1, self._key(sid), payload,
                               json_bytes(meta), json_bytes(shapes), self.ttl))

    def load(self, sid):
        fields = self._call(self.client.hgetall, self._key(sid))
        if not fields:
            raise ProtocolError("Tracking state expired or was lost; start a new tracking run", 409)
        try:
            if set(fields) != {b"revision", b"payload", b"meta", b"request", b"shapes"}:
                raise ValueError("Unexpected state fields")
            self._check_payload(fields[b"payload"])
            if len(fields[b"meta"]) > 1024 or len(fields[b"shapes"]) > 8 * 1024 * 1024:
                raise ValueError("Oversized state metadata")
            revision = int(fields[b"revision"])
            meta = json.loads(fields[b"meta"])
            shapes = json.loads(fields[b"shapes"])
            request = fields[b"request"].decode("ascii")
            if not 0 <= revision < 2**31 or not isinstance(shapes, list):
                raise ValueError("Invalid revision or shapes")
            if not isinstance(meta, dict) or set(meta) != {"identity", "count", "width", "height"}:
                raise ValueError("Invalid metadata")
            if (not isinstance(meta["identity"], str) or not re.fullmatch(r"[0-9a-f]{64}", meta["identity"]) or
                    type(meta["count"]) is not int or not 1 <= meta["count"] <= MAX_OBJECTS or
                    len(shapes) != meta["count"]):
                raise ValueError("Invalid model or object count")
            if any(type(meta[key]) is not int or meta[key] <= 0 for key in ("width", "height")) or meta["width"] * meta["height"] > MAX_PIXELS:
                raise ValueError("Invalid dimensions")
            if (revision == 0) != (request == ""):
                raise ValueError("Invalid request revision")
            if request and not re.fullmatch(r"[0-9a-f]{64}", request):
                raise ValueError("Invalid request fingerprint")
            return Record(revision, fields[b"payload"], meta, request, shapes)
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ProtocolError("Stored tracking state is invalid", 409) from exc

    def commit(self, sid, expected, request, payload, shapes):
        self._check_payload(payload)
        status, saved_shapes = self._call(
            self.client.eval, CAS, 1, self._key(sid), expected, request, payload,
            json_bytes(shapes), self.ttl,
        )
        if status == -1:
            raise ProtocolError("Tracking state expired during inference; start a new run", 409)
        if status == 0:
            raise ProtocolError("Another request advanced this tracking state; refresh or restart", 409)
        if status not in (1, 2):
            raise RuntimeError("Invalid Redis CAS result")
        return json.loads(saved_shapes)
