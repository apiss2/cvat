# SPDX-License-Identifier: MIT
"""CVAT tracker payload adapter. Weights are local; all temporal state is in Redis."""
import hashlib
import secrets
import threading
from geometry import polygon_mask, polygon_shape
from protocol import ProtocolError, decode_image
from state_codec import StateCodec


class RedisTracker:
    def __init__(self, video, store, identity, *, max_objects=4):
        if type(max_objects) is not int or not 1 <= max_objects <= 4:
            raise ValueError("max_objects must be 1..4")
        self.video, self.store, self.identity, self.max_objects = video, store, identity, max_objects
        self.codec = StateCodec(identity, max_bytes=store.max_bytes)
        # This protects the predictor within ONE process, not session state. Different
        # Nuclio workers do not share this lock and coordinate through Redis CAS.
        self.model_lock = threading.Lock()

    @staticmethod
    def _tokens(sid, revision, count):
        return [{"v": 2, "id": sid, "seq": revision, "obj": i, "count": count}
                for i in range(count)]

    def _parse_tokens(self, states):
        first = states[0]
        if not isinstance(first, dict):
            raise ProtocolError("Invalid tracker state")
        sid, revision = first.get("id"), first.get("seq")
        self.store._key(sid)  # Strict, bounded opaque-ID validation, no client-supplied Redis key.
        for i, token in enumerate(states):
            if (not isinstance(token, dict) or set(token) != {"v", "id", "seq", "obj", "count"} or
                    any(type(token.get(key)) is not int for key in ("v", "seq", "obj", "count")) or
                    token != {"v": 2, "id": sid, "seq": revision, "obj": i, "count": len(states)}):
                raise ProtocolError("Use all signed states from the same run, in their original order", 409)
        if not 0 <= revision < 2**31 - 1:
            raise ProtocolError("Invalid state revision")
        return sid, revision

    def __call__(self, data):
        image = decode_image(data)
        shapes, states = data.get("shapes", []), data.get("states", [])
        if not isinstance(shapes, list) or not isinstance(states, list):
            raise ProtocolError("shapes and states must be arrays")
        if max(len(shapes), len(states)) > self.max_objects:
            raise ProtocolError("Too many objects in one request", 413)
        if not states:
            if not shapes:
                raise ProtocolError("Provide at least one polygon seed")
            masks = [polygon_mask(shape, image.size) for shape in shapes]
            with self.model_lock:
                snapshot, predictions = self.video.initialize(image, masks)
            payload = self.codec.encode(snapshot)
            result = [polygon_shape(mask) for mask in predictions]
            meta = dict(identity=self.identity, count=len(shapes), width=image.width, height=image.height)
            for _ in range(3):
                sid = secrets.token_urlsafe(32)
                if self.store.create(sid, payload, meta, result):
                    return {"states": self._tokens(sid, 0, len(shapes)), "shapes": result}
            raise RuntimeError("Unable to allocate a unique tracking state")
        if shapes and (len(shapes) != len(states) or any(shape is not None for shape in shapes)):
            raise ProtocolError("Continue with states only; initialize a new run for corrections")
        sid, revision = self._parse_tokens(states)
        record = self.store.load(sid)
        if record.meta["identity"] != self.identity:
            raise ProtocolError("SAM2 model changed; start a new tracking run", 409)
        if record.meta["count"] != len(states) or image.size != (record.meta["width"], record.meta["height"]):
            raise ProtocolError("Object count or video frame dimensions changed")
        digest = hashlib.sha256()
        digest.update(f"{sid}:{revision}:{image.width}:{image.height}:".encode())
        digest.update(image.tobytes())
        request = digest.hexdigest()
        if record.revision == revision + 1 and record.request == request:
            # Return the committed reply without inference after a lost response/restart.
            return {"states": self._tokens(sid, record.revision, len(states)), "shapes": record.shapes}
        if record.revision != revision:
            raise ProtocolError("Stale or out-of-order tracker state", 409)
        snapshot = self.codec.decode(record.payload)
        if (snapshot.index != revision or len(snapshot.objects) != len(states) or
                (snapshot.width, snapshot.height) != image.size):
            raise ProtocolError("Inconsistent stored tracking state", 409)
        with self.model_lock:
            updated, predictions = self.video.advance(image, snapshot)
        payload = self.codec.encode(updated)
        shapes = [polygon_shape(mask) for mask in predictions]
        result = self.store.commit(sid, revision, request, payload, shapes)
        return {"states": self._tokens(sid, revision + 1, len(states)), "shapes": result}
