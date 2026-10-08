# SPDX-License-Identifier: MIT
"""CVAT's signed-state tracker protocol with shared multiplex memory in Redis."""
import hashlib
import secrets
import threading

from geometry import polygon_mask, polygon_shape
from protocol import MAX_OBJECTS, ProtocolError, decode_image
from state_codec import StateCodec


class Tracker:
    def __init__(self, video, store, identity):
        self.video, self.store, self.identity = video, store, identity
        self.codec = StateCodec(identity, max_bytes=store.max_bytes)
        self.model_lock = threading.Lock()

    @staticmethod
    def tokens(sid, revision, count):
        return [{"v": 1, "id": sid, "seq": revision, "obj": index, "count": count} for index in range(count)]

    def parse(self, states):
        first = states[0]
        if not isinstance(first, dict):
            raise ProtocolError("Invalid SAM3.1 tracker state")
        sid, revision = first.get("id"), first.get("seq")
        self.store._key(sid)
        for index, token in enumerate(states):
            if (not isinstance(token, dict) or set(token) != {"v", "id", "seq", "obj", "count"}
                    or any(type(token.get(key)) is not int for key in ("v", "seq", "obj", "count"))
                    or token != {"v": 1, "id": sid, "seq": revision, "obj": index, "count": len(states)}):
                raise ProtocolError("Use all SAM3.1 states from one run in their original order", 409)
        if not 0 <= revision < 2**31 - 1:
            raise ProtocolError("Invalid state revision")
        return sid, revision

    def __call__(self, data):
        image = decode_image(data)
        seeds, states = data.get("shapes", []), data.get("states", [])
        if not isinstance(seeds, list) or not isinstance(states, list):
            raise ProtocolError("shapes and states must be arrays")
        if max(len(seeds), len(states)) > MAX_OBJECTS:
            raise ProtocolError(f"Select one to {MAX_OBJECTS} polygons", 413)
        if not states:
            if not seeds:
                raise ProtocolError("Provide at least one polygon seed")
            masks = [polygon_mask(seed, image.size) for seed in seeds]
            with self.model_lock:
                snapshot, predictions = self.video.initialize(image, masks)
            payload = self.codec.encode(snapshot)
            shapes = [polygon_shape(mask) for mask in predictions]
            meta = dict(identity=self.identity, count=len(seeds), width=image.width, height=image.height)
            for _ in range(3):
                sid = secrets.token_urlsafe(32)
                if self.store.create(sid, payload, meta, shapes):
                    return {"states": self.tokens(sid, 0, len(seeds)), "shapes": shapes}
            raise RuntimeError("Unable to allocate a tracking state")
        if seeds and (len(seeds) != len(states) or any(seed is not None for seed in seeds)):
            raise ProtocolError("Continue with states only; start a new run for corrections")
        sid, revision = self.parse(states)
        record = self.store.load(sid)
        if record.meta["identity"] != self.identity:
            raise ProtocolError("SAM3.1 model changed; start a new tracking run", 409)
        if record.meta["count"] != len(states) or image.size != (record.meta["width"], record.meta["height"]):
            raise ProtocolError("Object count or frame dimensions changed")
        digest = hashlib.sha256(f"{sid}:{revision}:{image.width}:{image.height}:".encode())
        digest.update(image.tobytes())
        request = digest.hexdigest()
        if record.revision == revision + 1 and record.request == request:
            return {"states": self.tokens(sid, record.revision, len(states)), "shapes": record.shapes}
        if record.revision != revision:
            raise ProtocolError("Stale or out-of-order tracker state", 409)
        snapshot = self.codec.decode(record.payload)
        if (snapshot.index != revision or snapshot.count != len(states)
                or (snapshot.width, snapshot.height) != image.size):
            raise ProtocolError("Inconsistent stored tracking state", 409)
        with self.model_lock:
            updated, predictions = self.video.advance(image, snapshot)
        payload = self.codec.encode(updated)
        shapes = [polygon_shape(mask) for mask in predictions]
        # Return the winner's polygons when identical requests race after inference.
        shapes = self.store.commit(sid, revision, request, payload, shapes)
        return {"states": self.tokens(sid, revision + 1, len(states)), "shapes": shapes}
