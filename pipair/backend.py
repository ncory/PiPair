"""Renderer backend interface.

The engine (playlist logic) talks to a backend through this small API. All
times are *running time* in seconds, as returned by ``Backend.now()``.
Animations are expressed as keyframes ``[(time, value), ...]`` that the
backend applies frame-accurately; before the first keyframe the property
keeps whatever value it had.

Backends report asynchronous happenings by calling ``emit(event, layer,
detail)`` from any thread; the engine marshals these onto its event loop.
Events: ``ready`` (first frame decoded and held), ``eos`` (end of media),
``error`` (layer failed; detail = message), ``fatal`` (whole pipeline failed;
layer = None).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

class NoDisplayError(RuntimeError):
    """A backend could not start because no display is connected.

    The engine treats this as dormancy rather than failure: it keeps
    retrying, reports it distinctly through the API, and resumes whatever
    was playing once a display appears.
    """


Keyframes = list[tuple[float, float]]
EmitFn = Callable[[str, "Layer | None", object], None]


def classify_error(layer_id: int | None, layer_live: bool, src_name: str,
                   audio_device: str | None) -> str:
    """What a renderer error means, from where it came from.

    ``layer_id`` is the layer an element belongs to (None if it belongs to no
    layer); ``layer_live`` says whether that layer is still registered.

    Returns one of:

    ``"layer"``
        One item failed. The engine drops it and moves on.
    ``"stale"``
        A layer that has already been removed. Backends unregister a layer
        before its elements have finished winding down, so one failure inside
        it (or an ordinary teardown) makes its siblings report afterwards.
        Those describe something that no longer exists: treating them as
        ``"fatal"`` restarts the renderer and turns a skipped item into a
        black screen.
    ``"audio"``
        The audio sink failed; the caller blacklists the device and restarts.
    ``"fatal"``
        Anything outside a layer: the pipeline itself is broken.
    """
    if layer_id is not None:
        return "layer" if layer_live else "stale"
    if src_name == "asink" and audio_device:
        return "audio"
    return "fatal"

_layer_ids = itertools.count(1)


@dataclass
class Source:
    kind: str  # "video" | "image"
    path: Path
    media: str  # media file name, for display
    fit: str = "contain"
    duration_hint: float | None = None  # probed video duration
    offset: tuple[int, int] = (0, 0)  # user position offset in output pixels
    uid: str | None = None  # playlist item this layer plays


class Layer:
    """One piece of media loaded into the compositor."""

    def __init__(self, source: Source):
        self.id = next(_layer_ids)
        self.source = source
        self.state = "loading"  # loading | ready | playing | error | removed
        self.start_time: float | None = None
        self.paused_at: float | None = None
        self.paused_total = 0.0
        self.duration: float | None = None  # reported by the backend when known
        self.full_frame = True  # covers the whole output (no letterbox bars)
        self.offset: tuple[int, int] = source.offset
        self.curves: dict[str, list[tuple[float, float]]] = {}  # engine's alpha/volume keyframes
        self.eos = False
        self.error: str | None = None
        self.created = 0.0

    def position(self, now: float) -> float:
        if self.start_time is None:
            return 0.0
        ref = self.paused_at if self.paused_at is not None else now
        return max(0.0, ref - self.start_time - self.paused_total)

    def __repr__(self) -> str:
        return f"<Layer {self.id} {self.source.media} {self.state}>"


def fit_rect(src_w: float, src_h: float, dst_w: int, dst_h: int,
             fit: str) -> tuple[int, int, int, int]:
    """Placement (x, y, w, h) of a src_w x src_h picture in the output."""
    if fit == "stretch" or src_w <= 0 or src_h <= 0:
        return 0, 0, dst_w, dst_h
    scale = (max if fit == "cover" else min)(dst_w / src_w, dst_h / src_h)
    w, h = round(src_w * scale), round(src_h * scale)
    # snap to full frame when within a couple of pixels (e.g. 1920x1088 streams)
    if abs(w - dst_w) <= 2:
        w = dst_w
    if abs(h - dst_h) <= 2:
        h = dst_h
    return (dst_w - w) // 2, (dst_h - h) // 2, w, h


def covers(rect: tuple[int, int, int, int], dst_w: int, dst_h: int) -> bool:
    x, y, w, h = rect
    return x <= 0 and y <= 0 and x + w >= dst_w and y + h >= dst_h


def eval_keyframes(kfs: Keyframes, t: float, base: float) -> float:
    if not kfs or t < kfs[0][0]:
        return base
    for (t1, v1), (t2, v2) in zip(kfs, kfs[1:]):
        if t1 <= t < t2:
            return v1 + (v2 - v1) * (t - t1) / (t2 - t1) if t2 > t1 else v2
    return kfs[-1][1]


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


class Backend:
    name = "base"

    def __init__(self, settings: dict, hw: dict, emit: EmitFn):
        self.settings = settings
        self.hw = hw
        self.emit = emit
        self.render_size: tuple[int, int] = (1920, 1080)

    # True when the backend fits images to the output itself (the head does,
    # at its own resolution); the engine then hands it the original file.
    renders_images = False

    # False when two full-size videos can't be on screen at once (Pi 3 at 50/60 Hz):
    # the engine then freezes the outgoing video during video-to-video dissolves.
    video_overlap_ok = True

    def freeze(self, layer: Layer, at: float) -> None:
        """Hold a video layer's picture on its current frame from running time `at`."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def now(self) -> float: raise NotImplementedError
    def create_layer(self, source: Source) -> Layer: raise NotImplementedError
    def start_layer(self, layer: Layer, at: float) -> None: raise NotImplementedError
    def set_alpha(self, layer: Layer, kfs: Keyframes) -> None: raise NotImplementedError
    def set_volume(self, layer: Layer, kfs: Keyframes) -> None: raise NotImplementedError
    def dip(self, color: str, kfs: Keyframes) -> None: raise NotImplementedError
    def set_position(self, layer: Layer, dx: int, dy: int) -> None: raise NotImplementedError
    def pause_layer(self, layer: Layer) -> None: raise NotImplementedError
    def resume_layer(self, layer: Layer, shift: float) -> None: raise NotImplementedError
    def remove_layer(self, layer: Layer) -> None: raise NotImplementedError
    def set_background(self, color: str) -> None: raise NotImplementedError
    def set_master_volume(self, volume: float) -> None: raise NotImplementedError
    async def snapshot(self) -> bytes | None: return None
    def info(self) -> dict: return {"backend": self.name}
