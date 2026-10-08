"""The controller's renderer: sound here, picture on the head.

PairBackend looks like any other backend to the engine. Behind it are two
renderers: a local one that plays the audio of each item, and the head, which
shows the picture and is driven over the link with the same calls the engine
makes (create a layer, start it at T, fade it with these keyframes...). Times
are translated from this device's running time into the head's clock using
the offset that HeadPort keeps measuring.

    engine ── PairBackend ─┬─ local backend (audio)
                           └─ HeadPort ══ link ══ Head (pipair.head) ── video backend

The link can drop at any moment. While it is down the engine carries on with
audio alone and picture commands are discarded; the head keeps showing what
it has. When the head (re)appears the backend asks the engine to resync,
which restarts the current item on both devices together.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .backend import Backend, Keyframes, Layer, Source
from .link import PROTOCOL, ClockSync, Link

log = logging.getLogger(__name__)

PING_FAST = 0.05  # s between pings while the clock offset is still unknown
PING_SLOW = 0.5
PONG_TIMEOUT = 3.0  # no reply for this long: treat the head as gone
SNAPSHOT_TIMEOUT = 4.0


class HeadPort:
    """The controller's end of the link. Outlives the backends that use it.

    Keeps the clock offset fresh, greets the head with the output settings it
    should use, and remembers what the head said about itself (display size,
    media on its card, health).
    """

    def __init__(self, link: Link):
        self.link = link
        self.clock = link.clock
        self.sync = ClockSync()
        self.ready = False  # connected, clocks synced, head has a working renderer
        self.head: dict | None = None  # the head's last hello
        self.media: dict[str, int] | None = None  # file name -> size on the head
        self.status: dict = {}  # the head's periodic report
        self.backend: PairBackend | None = None
        self._want: dict | None = None  # settings the head should have
        self._sent: dict | None = None  # settings in the last hello
        self._seq = 0
        self._last_pong = 0.0
        self._task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []

    async def start(self) -> None:
        self.link.set_handlers(self._on_message, self._on_state)
        await self.link.start()
        self._task = asyncio.create_task(self._pinger())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.link.stop()

    def on_change(self, fn: Callable[[], None]) -> None:
        """Call `fn` when the link state or the head's media list changes."""
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("link listener failed")

    def send(self, msg: dict, blob: bytes = b"") -> bool:
        return self.link.send(msg, blob)

    def to_head(self, mono: float) -> float:
        """A time on this device's clock, expressed on the head's clock."""
        return mono + self.sync.offset

    def push_settings(self, settings: dict) -> bool:
        """Make sure the head has these output settings.

        Returns True if a hello went out: the head will answer, and that
        answer makes the backend ask for a resync.
        """
        self._want = {"output": copy.deepcopy(settings["output"]),
                      "bg": settings["background_color"]}
        if not (self.link.connected and self.sync.synced) or self._want == self._sent:
            return False
        self._hello()
        return True

    def _hello(self) -> None:
        self._set_ready(False)
        self._sent = copy.deepcopy(self._want)
        self.link.send({"t": "hello", "v": PROTOCOL, "s": self._want})

    def _set_ready(self, ready: bool) -> None:
        if ready == self.ready:
            return
        self.ready = ready
        log.info("head %s", "ready" if ready else "not available")
        if self.backend:
            self.backend.on_link(ready)
        self._changed()

    # -- link callbacks --------------------------------------------------------
    def _on_state(self, up: bool) -> None:
        self.sync.reset()
        self._sent = None
        self._last_pong = self.clock()
        if not up:
            self.status = {}
            self._set_ready(False)
        self._changed()

    def _on_message(self, msg: dict, blob: bytes, rx: float) -> None:
        t = msg.get("t")
        if t == "pong":
            self._last_pong = rx
            was = self.sync.synced
            self.sync.add(msg["t0"], msg["t1"], msg["t2"], rx)
            if self.sync.synced and not was and self._want is not None:
                self._hello()
        elif t == "hello":
            self.head = msg
            if "media" in msg:
                self.media = msg["media"]
            if self.backend:
                self.backend.apply_head()
            # A head whose renderer is down (no display) can't show anything yet.
            self._set_ready(False)
            if msg.get("ok") and self.sync.synced:
                self._set_ready(True)
            self._changed()
        elif t == "media":
            self.media = msg.get("files") or {}
            self._changed()
        elif t == "info":
            self.status = msg
        elif self.backend:
            self.backend.on_head(msg, blob)

    async def _pinger(self) -> None:
        while True:
            if self.link.connected:
                if self.sync.synced and self.clock() - self._last_pong > PONG_TIMEOUT:
                    log.warning("head stopped answering")
                    self.sync.reset()
                    self._sent = None
                    self._set_ready(False)
                self._seq += 1
                self.link.send({"t": "ping", "n": self._seq}, stamp="t0")
            await asyncio.sleep(PING_SLOW if self.sync.synced else PING_FAST)

    def info(self) -> dict:
        head = self.head or {}
        return {**self.link.info(), **self.sync.info(), "ready": self.ready,
                "head": {"name": head.get("name"), "version": head.get("ver"),
                         "ok": head.get("ok"), "no_display": head.get("nodisp"),
                         "media_files": len(self.media) if self.media is not None else None,
                         "health": self.status.get("health")} if self.head else None}


@dataclass
class _Pair:
    layer: Layer  # what the engine holds
    local: Layer | None = None  # the audio layer, if the item can have sound
    remote: str = "none"  # none | loading | ready | error
    local_done: bool = False  # local side prerolled (or failed, or absent)
    ready_sent: bool = False


class PairBackend(Backend):
    name = "pair"
    renders_images = True  # images are fitted on the head, at its resolution
    # A cue has to cross the radio before its time comes, so schedule further
    # ahead than a local renderer needs.
    start_margin = 0.25
    lookahead = 0.6

    def __init__(self, settings: dict, hw: dict, emit, local_factory: Callable[..., Backend],
                 port: HeadPort):
        super().__init__(settings, hw, emit)
        self.port = port
        self.local = local_factory(settings, hw, self._local_emit)
        self.pairs: dict[int, _Pair] = {}  # engine layer id -> pair
        self.by_local: dict[int, _Pair] = {}  # local layer id -> pair
        self.loop: asyncio.AbstractEventLoop | None = None
        self.fps = settings["output"]["fps"]
        self.av_offset = float(settings.get("link", {}).get("av_offset_ms", 0)) / 1000
        self._snap: asyncio.Future | None = None
        self.render_size = (1280, 720)
        self.apply_head()

    def apply_head(self) -> None:
        h = self.port.head
        if h:
            if h.get("size"):
                self.render_size = (int(h["size"][0]), int(h["size"][1]))
            self.video_overlap_ok = bool(h.get("overlap", True))
            self.fps = h.get("fps") or self.fps

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.local.start()
        self.port.backend = self
        # Either the head needs new settings (it answers, and that triggers a
        # resync onto a fresh backend) or it is told to forget our old layers.
        if not self.port.push_settings(self.settings) and self.port.ready:
            self.port.send({"t": "reset"})

    async def stop(self) -> None:
        if self.port.backend is self:
            self.port.backend = None
        if self._snap and not self._snap.done():
            self._snap.cancel()
        await self.local.stop()

    def now(self) -> float:
        return self.local.now()

    # ---------------------------------------------------------------- time
    def _t(self, rt: float) -> float:
        """A running time of ours as a time on the head's clock."""
        mono = self.port.clock() + (rt - self.local.now())
        return round(self.port.to_head(mono) + self.av_offset, 4)

    def _kfs(self, kfs: Keyframes) -> list[list[float]]:
        return [[self._t(t), round(float(v), 4)] for t, v in kfs]

    def _remote(self, layer: Layer) -> _Pair | None:
        """The layer's pair, if the head knows the layer."""
        p = self.pairs.get(layer.id)
        return p if p and p.remote in ("loading", "ready") and self.port.ready else None

    # -------------------------------------------------------------- layers
    def create_layer(self, source: Source) -> Layer:
        layer = Layer(source)
        p = self.pairs[layer.id] = _Pair(layer)
        if source.kind == "video":
            p.local = self.local.create_layer(source)
            self.by_local[p.local.id] = p
        else:
            p.local_done = True
        if self.port.ready:
            p.remote = "loading"
            self.port.send({"t": "new", "id": layer.id, "k": source.kind, "m": source.media,
                            "fit": source.fit, "off": list(source.offset),
                            "bg": self.settings["background_color"]})
        elif p.local_done and self.loop:
            self.loop.call_soon(self._check_ready, p)
        return layer

    def _check_ready(self, p: _Pair) -> None:
        if p.ready_sent or p.layer.state != "loading":
            return
        if not p.local_done or p.remote == "loading":
            return
        p.ready_sent = True
        p.layer.state = "ready"
        self.emit("ready", p.layer, None)

    def start_layer(self, layer: Layer, at: float) -> None:
        p = self.pairs.get(layer.id)
        if p and p.local:
            self.local.start_layer(p.local, at)
        if self._remote(layer):
            self.port.send({"t": "start", "id": layer.id, "at": self._t(at)})
        layer.start_time = at
        layer.state = "playing"

    def set_alpha(self, layer: Layer, kfs: Keyframes) -> None:
        if self._remote(layer):
            self.port.send({"t": "alpha", "id": layer.id, "k": self._kfs(kfs)})

    def set_volume(self, layer: Layer, kfs: Keyframes) -> None:
        p = self.pairs.get(layer.id)
        if p and p.local:
            self.local.set_volume(p.local, kfs)

    def dip(self, color: str, kfs: Keyframes) -> None:
        if self.port.ready:
            self.port.send({"t": "dip", "c": color, "k": self._kfs(kfs)})

    def set_position(self, layer: Layer, dx: int, dy: int) -> None:
        layer.offset = (dx, dy)
        if self._remote(layer):
            self.port.send({"t": "pos", "id": layer.id, "off": [dx, dy]})

    def freeze(self, layer: Layer, at: float) -> None:
        if self._remote(layer):
            self.port.send({"t": "freeze", "id": layer.id, "at": self._t(at)})

    def pause_layer(self, layer: Layer) -> None:
        p = self.pairs.get(layer.id)
        if p and p.local:
            self.local.pause_layer(p.local)
        if self._remote(layer):
            self.port.send({"t": "pause", "id": layer.id})

    def resume_layer(self, layer: Layer, shift: float) -> None:
        p = self.pairs.get(layer.id)
        if p and p.local:
            self.local.resume_layer(p.local, shift)
        if self._remote(layer):
            self.port.send({"t": "resume", "id": layer.id, "shift": round(shift, 4)})

    def remove_layer(self, layer: Layer) -> None:
        p = self.pairs.pop(layer.id, None)
        layer.state = "removed"
        if not p:
            return
        if p.local:
            self.by_local.pop(p.local.id, None)
            self.local.remove_layer(p.local)
        if p.remote != "none" and self.port.ready:
            self.port.send({"t": "rm", "id": layer.id})

    def set_background(self, color: str) -> None:
        if self.port.ready:
            self.port.send({"t": "bg", "c": color})

    def set_master_volume(self, volume: float) -> None:
        self.local.set_master_volume(volume)

    # --------------------------------------------------------------- events
    def _local_emit(self, event: str, layer: Layer | None, detail: object) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._on_local, event, layer, detail)

    def _on_local(self, event: str, layer: Layer | None, detail: object) -> None:
        if event == "fatal":
            self.emit("fatal", None, detail)
            return
        p = self.by_local.get(layer.id) if layer else None
        if not p:
            return
        if event == "ready":
            p.local_done = True
            if p.layer.duration is None:
                p.layer.duration = p.local.duration
            self._check_ready(p)
        elif event == "eos":
            # The picture decides when an item has ended; sound only stands in
            # for it while the head is away.
            if p.remote not in ("loading", "ready") or not self.port.ready:
                self.emit("eos", p.layer, None)
        elif event == "error":
            # No usable sound is not a reason to drop the item if the head can
            # still show it.
            log.warning("%s: audio failed: %s", p.layer.source.media, detail)
            self.by_local.pop(p.local.id, None)
            self.local.remove_layer(p.local)
            p.local = None
            p.local_done = True
            if p.remote == "none":
                self.emit("error", p.layer, f"audio: {detail}")
            else:
                self._check_ready(p)

    def on_head(self, msg: dict, blob: bytes) -> None:
        """A message from the head about one of our layers (or a snapshot)."""
        t = msg.get("t")
        if t == "snap":
            if self._snap and not self._snap.done():
                self._snap.set_result(blob or None)
            return
        if t != "ev":
            return
        p = self.pairs.get(msg.get("id"))
        if not p:
            return  # a layer we have already dropped
        e = msg.get("e")
        if "full" in msg:
            p.layer.full_frame = bool(msg["full"])
        if e == "ready":
            p.remote = "ready"
            if msg.get("dur"):
                p.layer.duration = float(msg["dur"])
            self._check_ready(p)
        elif e == "eos":
            self.emit("eos", p.layer, None)
        elif e == "error":
            p.remote = "error"
            self.emit("error", p.layer, f"head: {msg.get('msg') or 'failed'}")

    def on_link(self, ready: bool) -> None:
        if ready:
            self.emit("resync", None, "head connected")
            return
        # The head is gone: whatever we were waiting on from it will not come.
        for p in list(self.pairs.values()):
            if p.remote == "loading":
                p.remote = "none"
                self._check_ready(p)
            elif p.remote == "ready":
                p.remote = "none"
                if p.local and p.local.eos:
                    self.emit("eos", p.layer, None)
        if self._snap and not self._snap.done():
            self._snap.set_result(None)

    # -------------------------------------------------------------- preview
    async def snapshot(self) -> bytes | None:
        if not self.port.ready or not self.loop:
            return None
        if self._snap is None or self._snap.done():  # one request in flight at a time
            self._snap = self.loop.create_future()
            self.port.send({"t": "snap"})
        try:
            return await asyncio.wait_for(asyncio.shield(self._snap), SNAPSHOT_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._snap = None
            return None

    # ----------------------------------------------------------------- info
    def info(self) -> dict[str, Any]:
        """The head's renderer report (what the UI's display settings expect),
        with this device's audio and the link state added."""
        head = self.port.head or {}
        out: dict[str, Any] = dict(self.port.status.get("out") or head.get("out") or {})
        if not self.port.ready:
            out.pop("display", None)
        out.setdefault("backend", self.name)
        out["render_size"] = list(self.render_size)
        out["fps"] = self.fps
        out["audio_device"] = self.local.info().get("audio_device")
        out["av_offset_ms"] = round(self.av_offset * 1000)
        out["link"] = self.port.info()
        return out
