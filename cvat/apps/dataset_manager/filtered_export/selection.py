# SPDX-License-Identifier: MIT
"""Pure frame selection. Frame numbers are CVAT's zero-based task-local indices."""

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Iterable, Mapping

# Only image exporters that honor TaskData.get_included_frames/group_by_frame.
# In particular, CVAT for video accesses tracks/tags directly and is NOT safe.
SUPPORTED_FORMATS = (
    "CVAT for images 1.1",
    "COCO 1.0",
    "YOLO 1.1",
    "Ultralytics YOLO Detection 1.0",
    "Ultralytics YOLO Segmentation 1.0",
    "Ultralytics YOLO Oriented Bounding Boxes 1.0",
    "Ultralytics YOLO Pose 1.0",
    "ITGformat 1.0",
)


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _integer(value, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**31 - 1:
        raise ValueError(f"{name} must be an integer between {minimum} and 2147483647")
    return value


@dataclass(frozen=True)
class FrameFilter:
    mode: str
    tag_ids: tuple[int, ...] = ()
    start: int = 0
    stop: int | None = None
    step: int = 1

    @classmethod
    def parse(cls, value: Mapping) -> "FrameFilter":
        if not isinstance(value, dict):
            raise ValueError("filter must be an object")
        if set(value) - {"mode", "tag_ids", "start", "stop", "step"}:
            raise ValueError("Unknown filter field")
        mode = value.get("mode")
        if mode not in ("any", "all", "none", "untagged", "all_frames"):
            raise ValueError("Unknown tag matching mode")
        ids = value.get("tag_ids", [])
        if not isinstance(ids, list) or len(ids) > 1000:
            raise ValueError("tag_ids must be a list with at most 1000 entries")
        ids = tuple(sorted({_integer(i, "tag_id", 1) for i in ids}))
        if mode in ("any", "all", "none") and not ids:
            raise ValueError("Select at least one tag")
        if mode in ("untagged", "all_frames") and ids:
            raise ValueError("This matching mode does not accept tag_ids")
        start = _integer(value.get("start", 0), "start")
        stop = value.get("stop")
        if stop is not None:
            _integer(stop, "stop")
            if stop < start:
                raise ValueError("stop must be greater than or equal to start")
        return cls(mode, ids, start, stop, _integer(value.get("step", 1), "step", 1))

    def to_dict(self) -> dict:
        return {**asdict(self), "tag_ids": list(self.tag_ids)}

    def select(self, available: Iterable[int], tags: Iterable[Mapping]) -> set[int]:
        # Intersect with the native export scope, not range(size): deleted frames
        # and validation placeholders must never be accidentally reintroduced.
        candidates = {
            frame for frame in available
            if frame >= self.start and (self.stop is None or frame <= self.stop)
            and (frame - self.start) % self.step == 0
        }
        if self.mode == "all_frames":
            return candidates
        by_frame: dict[int, set[int]] = {}
        for tag in tags:
            if tag["frame"] in candidates:
                by_frame.setdefault(tag["frame"], set()).add(tag["label_id"])
        wanted = set(self.tag_ids)
        if self.mode == "untagged":
            return candidates - by_frame.keys()
        if self.mode == "none":
            return {frame for frame in candidates if not wanted & by_frame.get(frame, set())}
        if self.mode == "all":
            return {frame for frame, labels in by_frame.items() if wanted <= labels}
        return {frame for frame, labels in by_frame.items() if wanted & labels}
