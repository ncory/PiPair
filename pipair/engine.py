"""Playback engine: playlists, scheduling and transitions.

Runs entirely on the asyncio loop. The renderer backend does the actual
decoding/compositing; the engine decides *what* plays *when* and describes
transitions as keyframed alpha/volume ramps at exact running times, so the
visual timing does not depend on how promptly Python gets scheduled.

Timeline of an automatic transition from item A to item B:

    A starts ─────────────── A.end - lead: preload B (decoded, first frame held)
                             A.end - T - lookahead: request switch at A.end - T
                             A.end - T: B unblocked, fades in over T seconds
                             A.end: A has finished; removed shortly after
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import hw as hwmod
from .backend import NoDisplayError, Backend, Layer, Source, eval_keyframes
from .config import Store
from .media import MediaLibrary, media_kind

log = logging.getLogger(__name__)

TICK = 0.04  # scheduler period (s)
PRELOAD_LEAD = 4.0  # start loading the next item this long before it's needed
LOOKAHEAD = 0.35  # schedule switches this far ahead so they land frame-exact
START_MARGIN = 0.08  # minimum time between "now" and a layer's start
FREEZE_LEAD = 0.25  # freeze-mode dissolves: time to convert the frozen frame
HIDE_RAMP = 0.002  # "instant" hides are a 2 ms ramp so earlier keyframes stay valid
LOAD_TIMEOUT = 20.0
RESTART_BACKOFF = (2, 5, 10, 30)

STOP = "stop"
HOLD = "hold"


@dataclass
class Target:
    playlist: str
    index: int
    uid: str

    def same(self, other: Any) -> bool:
        return (isinstance(other, Target) and other.playlist == self.playlist
                and other.uid == self.uid)


@dataclass
class Preload:
    target: Target
    task: asyncio.Task


@dataclass
class Pending:
    target: Target
    transition: dict
    layer: Layer
    at: float
    manual: bool
    created: float


class Engine:
    def __init__(self, store: Store, library: MediaLibrary,
                 backend_factory: Callable[..., Backend], hw: dict):
        self.store = store
        self.library = library
        self.backend_factory = backend_factory
        self.hw = hw
        self.backend: Backend | None = None
        self.backend_ok = False
        self.state = "starting"  # starting | playing | paused | held | stopped | dormant | error
        # True while the renderer cannot run for want of a display. Not a
        # fault: playback resumes by itself when one is plugged in again.
        self.no_display = False
        self.current: Layer | None = None
        self.target: Target | None = None
        self.last_playlist: str | None = None
        self.outgoing: list[tuple[Layer, float]] = []
        self.preload: Preload | None = None
        self.pending: Pending | None = None
        self.token = 0
        self.auto_requested_for: int | None = None
        self.transition_until = 0.0
        self.fail_count = 0
        self.loop_item = False  # repeat the current item instead of advancing
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.errors: list[dict] = []
        self._listeners: list[Callable[[dict], None]] = []
        self._tick_task: asyncio.Task | None = None
        self._restart_attempts = 0
        self._restarting = False
        self._restart_scheduled = False
        self._resync_reason: str | None = None  # a resync asked for mid-restart
        # The item most recently asked for (None after a stop). A renderer
        # restart resumes this, so a restart that lands while an item is still
        # loading does not forget it.
        self._intent: Target | None = None
        self._resume_target: Target | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        store.on_change(self._on_store_change)

    # ------------------------------------------------------------------ setup
    async def start(self, autoplay: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        self.backend_ok = await self._start_backend()
        self._tick_task = asyncio.create_task(self._tick_loop())
        if autoplay:
            dp = self.store.settings.get("default_playlist")
            pl = self.store.get_playlist(dp) if dp else None
            if pl and pl["items"] and not self.backend_ok:
                self._resume_target = self._target(pl, 0)
            elif pl and pl["items"]:
                await self.play(dp)
            else:
                self.state = "stopped"
                if dp and pl is None:
                    self._error(f"default playlist '{dp}' does not exist")
                elif dp:  # e.g. a fresh install: nothing to play yet, not an error
                    log.info("default playlist '%s' is empty; nothing to play", dp)
        else:
            self.state = "stopped"
        self._notify()

    async def shutdown(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        if self.backend:
            await self.backend.stop()

    async def _start_backend(self) -> bool:
        try:
            self.backend = self.backend_factory(self.store.settings, self.hw, self._emit_threadsafe)
            await self.backend.start()
            self.backend.set_background(self.store.settings["background_color"])
            self.backend.set_master_volume(self.store.settings["audio"]["volume"])
            self.no_display = False
            return True
        except NoDisplayError as e:
            # Expected whenever the Pi runs with the display off or unplugged.
            # Logged quietly: it repeats every retry until a display appears.
            self.no_display = True
            log.info("no display connected; waiting for one")
            self._emit_threadsafe("fatal", None, f"no display connected: {e}")
            return False
        except Exception as e:  # noqa: BLE001
            self.no_display = False
            log.exception("renderer failed to start")
            self._emit_threadsafe("fatal", None, f"renderer failed to start: {e}")
            return False

    async def restart_backend(self, reason: str = "", resume: Target | None = None) -> None:
        """Tear down and rebuild the renderer, resuming the current item."""
        if self._restarting:
            return
        self._restarting = True
        try:
            log.warning("restarting renderer%s", f": {reason}" if reason else "")
            if resume is None:
                resume = self._intent
            self.token += 1
            self.current = None
            self.pending = None
            self.preload = None
            self.outgoing = []
            if self.backend:
                try:
                    await self.backend.stop()
                except Exception:  # noqa: BLE001
                    log.exception("error stopping backend")
            self.backend_ok = await self._start_backend()
            if not self.backend_ok:
                self._resume_target = resume or self._resume_target
                return
            self.state = "stopped"
            if resume and self.store.get_playlist(resume.playlist):
                await self.play(resume.playlist, resume.index, {"type": "cut"})
            self._restart_attempts = 0
        finally:
            self._restarting = False
            self._notify()
            if self._resync_reason:
                self._resync(self._resync_reason)

    # ------------------------------------------------------------ listeners
    def on_status(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        st = self.status()
        for fn in self._listeners:
            try:
                fn(st)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")

    def _error(self, msg: str) -> None:
        log.error(msg)
        now = time.time()
        self.last_error = msg
        self.last_error_at = now
        self.errors.append({"time": now, "message": msg})
        del self.errors[:-20]

    # ------------------------------------------------------- transitions
    def _default_transition(self) -> dict:
        return self.store.settings["default_transition"]

    def item_transition(self, pl: dict, index: int) -> dict:
        items = pl["items"]
        it = items[index] if 0 <= index < len(items) else {}
        return it.get("transition") or pl.get("transition") or self._default_transition()

    def next_after(self, target: Target) -> tuple[Target | str, dict]:
        """What follows `target` automatically, and the transition into it."""
        pl = self.store.get_playlist(target.playlist)
        if pl is None or not pl["items"]:
            return STOP, self._default_transition()
        n = target.index + 1
        if n < len(pl["items"]):
            return self._target(pl, n), self.item_transition(pl, n)
        end = pl["end_action"]
        if end["type"] == "loop":
            return self._target(pl, 0), self.item_transition(pl, 0)
        if end["type"] == "hold":
            return HOLD, self._default_transition()
        if end["type"] == "goto":
            nxt = self.store.get_playlist(end["playlist"])
            if nxt and nxt["items"]:
                return self._target(nxt, 0), end["transition"] or self.item_transition(nxt, 0)
            self._error(f"playlist '{pl['id']}' ends with goto '{end['playlist']}', "
                        "which is missing or empty; stopping")
        return STOP, pl.get("transition") or self._default_transition()

    @staticmethod
    def _target(pl: dict, index: int) -> Target:
        return Target(pl["id"], index, pl["items"][index]["uid"])

    # ------------------------------------------------------------ commands
    async def play(self, playlist: str | None = None, index: int = 0,
                   transition: dict | None = None) -> None:
        if playlist is None:
            if self.state == "paused":
                self.resume()
                return
            if self.state in ("playing", "held") and self.current:
                return
            playlist = self.last_playlist or self.store.settings.get("default_playlist")
            if not playlist:
                raise ValueError("no playlist given and no default playlist set")
        pl = self.store.get_playlist(playlist)
        if pl is None:
            raise KeyError(playlist)
        if not pl["items"]:
            raise ValueError(f"playlist '{playlist}' is empty")
        if not 0 <= index < len(pl["items"]):
            raise ValueError(f"index must be between 0 and {len(pl['items']) - 1}")
        self.fail_count = 0
        await self._switch(self._target(pl, index),
                           transition or self.item_transition(pl, index), manual=True)

    async def stop(self, transition: dict | None = None) -> None:
        if transition is None:
            pl = self.store.get_playlist(self.target.playlist) if self.target else None
            transition = (pl or {}).get("transition") or self._default_transition()
        await self._switch(STOP, transition, manual=True)

    async def next(self, transition: dict | None = None) -> None:
        if not self.target:
            return await self.play(transition=transition)
        nxt, trans = self.next_after(self.target)
        if nxt == HOLD:
            return
        await self._switch(nxt, transition or trans, manual=True)

    async def previous(self, transition: dict | None = None) -> None:
        if not self.target:
            return await self.play(transition=transition)
        pl = self.store.get_playlist(self.target.playlist)
        if not pl or not pl["items"]:
            return
        idx = (self.target.index - 1) % len(pl["items"])
        await self._switch(self._target(pl, idx), transition or self.item_transition(pl, idx),
                           manual=True)

    def pause(self) -> None:
        if self.state != "playing" or not self.current or not self.backend:
            return
        self.current.paused_at = self.backend.now()
        self.backend.pause_layer(self.current)
        self.state = "paused"
        self._notify()

    def resume(self) -> None:
        if self.state != "paused" or not self.current or not self.backend:
            return
        layer = self.current
        shift = self.backend.now() + self._margin() - layer.paused_at
        layer.paused_total += shift
        layer.paused_at = None
        self.backend.resume_layer(layer, shift)
        self.state = "playing"
        if self.pending and self.pending.layer.state == "ready":
            self._execute_pending()
        self._notify()

    def toggle_pause(self) -> None:
        if self.state == "paused":
            self.resume()
        else:
            self.pause()

    # ------------------------------------------------------------- switching
    async def _switch(self, target: Target | str, transition: dict, manual: bool,
                      at: float | None = None) -> None:
        assert self.backend
        if isinstance(target, Target):
            self._intent = target
        elif target == STOP:
            self._intent = None
        if not self.backend_ok:
            # renderer is down; remember the request and apply it on recovery
            self._resume_target = target if isinstance(target, Target) else None
            self.state = "error"
            return
        self.token += 1
        tok = self.token
        if self.pending:
            self._drop(self.pending.layer)
            self.pending = None
        if target == STOP or target == HOLD:
            self._discard_preload()
            if target == STOP:
                self._execute(STOP, transition, None, self.backend.now() if at is None else at)
            return
        assert isinstance(target, Target)
        if self.preload and self.preload.target.same(target):
            pre = self.preload
            self.preload = None
            layer = await pre.task
        else:
            self._discard_preload()
            layer = await self._make_layer(target)
        if tok != self.token:  # superseded by a newer command while loading
            self._drop(layer)
            return
        now = self.backend.now()
        self.pending = Pending(target, transition, layer, now if at is None else at, manual, now)
        if layer.state == "error":
            self._on_layer_error(layer, layer.error or "failed to load")
        elif layer.state == "ready" and (manual or self.state != "paused"):
            self._execute_pending()
        else:
            self._notify()  # show "loading"

    def _execute_pending(self) -> None:
        p = self.pending
        assert p
        self.pending = None
        self._execute(p.target, p.transition, p.layer, p.at)

    def _execute(self, target: Target | str, trans: dict, layer: Layer | None, at: float) -> None:
        b = self.backend
        assert b
        now = b.now()
        t0 = max(at, now + self._margin())
        old = self.current
        for lyr, _ in self.outgoing:  # anything still fading out goes away now
            b.remove_layer(lyr)
        self.outgoing = []
        kind = trans["type"]
        d = float(trans.get("duration") or 0)
        if d <= 0:
            kind = "cut"

        # New layers get fresh curves; the outgoing layer's curve is
        # *extended* (keeping any ramp still in progress, e.g. its own
        # fade-in when items are short) so it never jumps.
        if kind == "cut":
            if layer:
                b.start_layer(layer, t0)
                self._curve(layer, "alpha", [(t0, 1.0)])
                self._curve(layer, "volume", [(t0, 1.0)])
            if old:
                self._extend(old, "alpha", t0 - HIDE_RAMP, t0, 0.0)
                self._extend(old, "volume", t0 - HIDE_RAMP, t0, 0.0)
                self.outgoing.append((old, t0 + 0.5))
            self.transition_until = t0
        elif kind == "dissolve":
            # Where the display can't show two videos at once, the outgoing
            # video freezes at t0 (its frame is converted to a still) and the
            # incoming one starts once that still is ready.
            freeze = (layer is not None and old is not None and not b.video_overlap_ok
                      and layer.source.kind == "video" and old.source.kind == "video")
            if freeze:
                b.freeze(old, t0)
                t0_audio, t0 = t0, t0 + FREEZE_LEAD
            else:
                t0_audio = t0
            if layer:
                b.start_layer(layer, t0)
                self._curve(layer, "alpha", [(t0, 0.0), (t0 + d, 1.0)])
                self._curve(layer, "volume", [(t0, 0.0), (t0 + d, 1.0)])
            if old:
                # True crossfade: the outgoing picture stays as it is
                # underneath while the new one fades in on top, and is hidden
                # on the frame the new one reaches full opacity. (Fading both
                # would dip toward the background mid-way.) With nothing
                # incoming (stop), the old picture fades out to the background.
                if layer is None:
                    self._extend(old, "alpha", t0, t0 + d, 0.0)
                else:
                    self._extend(old, "alpha", t0 + d - HIDE_RAMP, t0 + d, 0.0)
                self._extend(old, "volume", t0_audio, t0 + d, 0.0)
                self.outgoing.append((old, t0 + d + 0.3))
            self.transition_until = t0 + d
        else:  # dip through a color
            mid = t0 + d / 2
            b.dip(trans.get("color", "#000000"), [(t0, 0.0), (mid, 1.0), (t0 + d, 0.0)])
            if layer:
                b.start_layer(layer, mid)
                self._curve(layer, "alpha", [(mid, 1.0)])
                self._curve(layer, "volume", [(mid, 0.0), (t0 + d, 1.0)])
            if old:
                self._extend(old, "alpha", mid - HIDE_RAMP, mid, 0.0)
                self._extend(old, "volume", t0, mid, 0.0)
                self.outgoing.append((old, mid + 0.3))
            self.transition_until = t0 + d

        self.current = layer
        self.auto_requested_for = None
        if isinstance(target, Target):
            self.target = target
            self.last_playlist = target.playlist
            self.state = "playing"
            self.fail_count = 0
        else:
            self.target = None
            self.state = "stopped"
        self._notify()

    def _curve(self, layer: Layer, prop: str, kfs: list[tuple[float, float]]) -> None:
        """Set a layer's alpha/volume keyframes (and remember them)."""
        kfs = sorted(kfs)
        layer.curves[prop] = kfs
        (self.backend.set_alpha if prop == "alpha" else self.backend.set_volume)(layer, kfs)

    def _extend(self, layer: Layer, prop: str, t_from: float, t_to: float, value: float) -> None:
        """Ramp from wherever the layer's curve is at `t_from` to `value` at `t_to`."""
        kfs = layer.curves.get(prop, [])
        start = eval_keyframes(kfs, t_from, 1.0)
        kept = [kf for kf in kfs if kf[0] < t_from]
        self._curve(layer, prop, kept + [(t_from, start), (t_to, value)])

    async def _make_layer(self, target: Target) -> Layer:
        """Resolve a playlist item to a Source and start loading it."""
        assert self.backend
        pl = self.store.get_playlist(target.playlist)
        item = pl["items"][target.index] if pl and target.index < len(pl["items"]) else None
        settings = self.store.settings
        if item is None:
            return self._failed_layer(Source("video", self.library.media_dir, "?"), "item vanished")
        name = item["media"]
        fit = item.get("fit") or settings["default_fit"]
        offset = (item.get("offset_x") or 0, item.get("offset_y") or 0)
        kind = media_kind(name)
        if not self.library.exists(name):
            return self._failed_layer(Source(kind or "video", self.library.media_dir, name),
                                      "file not found")
        try:
            if kind == "image" and self.backend.renders_images:
                src = Source("image", self.library.path(name), name, fit, offset=offset,
                             uid=item["uid"])
            elif kind == "image":
                w, h = self.backend.render_size
                path = await asyncio.to_thread(self.library.rendered_image, name, w, h, fit,
                                               settings["background_color"])
                src = Source("image", path, name, "stretch", offset=offset, uid=item["uid"])
            else:
                info = self.library.info(name) or await self.library.probe(name) or {}
                src = Source("video", self.library.path(name), name, fit, info.get("duration"),
                             offset=offset, uid=item["uid"])
        except Exception as e:  # noqa: BLE001 - PIL errors, OS errors, ...
            return self._failed_layer(Source(kind or "video", self.library.media_dir, name), str(e))
        try:
            layer = self.backend.create_layer(src)
        except Exception as e:  # noqa: BLE001 - renderer couldn't build it
            log.exception("could not create layer for %s", name)
            return self._failed_layer(src, f"renderer error: {e}")
        layer.created = self.backend.now()
        if kind == "image":
            layer.duration = item.get("duration") or settings["default_image_duration"]
        return layer

    @staticmethod
    def _failed_layer(src: Source, msg: str) -> Layer:
        layer = Layer(src)
        layer.state = "error"
        layer.error = msg
        return layer

    def _drop(self, layer: Layer | None) -> None:
        if layer and layer.state != "removed" and self.backend:
            self.backend.remove_layer(layer)

    def _discard_preload(self) -> None:
        pre = self.preload
        self.preload = None
        if not pre:
            return
        if pre.task.done():
            if not pre.task.cancelled() and pre.task.exception() is None:
                self._drop(pre.task.result())
        else:
            pre.task.add_done_callback(
                lambda t: self._drop(t.result())
                if not t.cancelled() and t.exception() is None else None)

    def _ensure_preload(self, target: Target) -> None:
        if self.preload and self.preload.target.same(target):
            return
        self._discard_preload()
        self.preload = Preload(target, asyncio.create_task(self._make_layer(target)))

    # --------------------------------------------------------------- events
    def _emit_threadsafe(self, event: str, layer: Layer | None, detail: object) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._on_event, event, layer, detail)

    def _on_event(self, event: str, layer: Layer | None, detail: object) -> None:
        if event == "fatal":
            self._error(f"renderer failed: {detail}")
            asyncio.create_task(self._restart_after_failure(str(detail)))
            return
        if event == "resync":
            self._resync(str(detail))
            return
        if layer is None or layer.state == "removed":
            return
        if event == "ready":
            p = self.pending
            if p and p.layer is layer and (p.manual or self.state != "paused"):
                self._execute_pending()
        elif event == "eos":
            layer.eos = True
        elif event == "error":
            layer.state = "error"
            layer.error = str(detail)
            self._on_layer_error(layer, str(detail))

    async def _restart_after_failure(self, reason: str) -> None:
        if self._restart_scheduled:
            return
        self._restart_scheduled = True
        # Remember what to resume across repeated failures (e.g. no display yet).
        if self.state in ("playing", "paused", "held") and self.target:
            self._resume_target = self.target
        delay = RESTART_BACKOFF[min(self._restart_attempts, len(RESTART_BACKOFF) - 1)]
        self._restart_attempts += 1
        self.state = "dormant" if self.no_display else "error"
        self._notify()
        await asyncio.sleep(delay)
        self._restart_scheduled = False
        await self.restart_backend(reason, resume=self._resume_target)
        if self.backend_ok:
            self._resume_target = None

    def _resync(self, reason: str) -> None:
        """Start the current item again on a fresh renderer.

        Asked for by a backend whose other half (the head) has just appeared
        or come back: it is not a fault, and the only way to line the two
        devices up again is to start the item together.
        """
        if self._restarting:
            self._resync_reason = reason  # picked up when that restart finishes
            return
        self._resync_reason = None
        asyncio.create_task(self.restart_backend(reason))

    def _margin(self) -> float:
        return getattr(self.backend, "start_margin", START_MARGIN)

    def _on_layer_error(self, layer: Layer, msg: str) -> None:
        self._error(f"{layer.source.media}: {msg}")
        p = self.pending
        if p and p.layer is layer:
            self.pending = None
            self._drop(layer)
            self._skip_failed(p.target, p.transition, p.manual)
        elif layer is self.current:
            layer.eos = True  # treat a mid-playback failure as the end of the item
        elif not (self.preload and self.preload.task.done()
                  and self.preload.task.result() is layer):
            self._drop(layer)
        self._notify()

    def _skip_failed(self, target: Target, transition: dict, manual: bool) -> None:
        self.fail_count += 1
        pl = self.store.get_playlist(target.playlist)
        limit = max(3, len(pl["items"]) + 1) if pl else 3
        if self.fail_count >= limit:
            self._error("too many consecutive failures; stopping")
            self.fail_count = 0
            asyncio.create_task(self._switch(STOP, {"type": "cut"}, manual=True))
            return
        nxt, _ = self.next_after(target)
        if nxt == HOLD or nxt == STOP:
            if self.current is None or nxt == STOP:
                asyncio.create_task(self._switch(STOP, transition, manual=True))
            return
        asyncio.create_task(self._switch(nxt, transition, manual=manual))

    def _on_store_change(self, what: str, detail: Any) -> None:
        if what == "settings":
            s = self.store.settings
            if detail and detail.get("rebuild"):
                if self._tick_task:  # before start() the new settings are simply the settings
                    asyncio.create_task(self.restart_backend("output settings changed"))
                return
            if self.backend:
                self.backend.set_background(s["background_color"])
                self.backend.set_master_volume(s["audio"]["volume"])
        elif what == "playlist":
            if isinstance(detail, dict) and self.target and detail.get("old_id") == self.target.playlist:
                if detail.get("id"):
                    self.target.playlist = detail["id"]
                    if self.last_playlist == detail["old_id"]:
                        self.last_playlist = detail["id"]
            self._resync_target()
            self._apply_live_item_props()
        self._notify()

    def _active_layers(self) -> list[Layer]:
        layers = [self.current] + [lyr for lyr, _ in self.outgoing]
        if self.pending:
            layers.append(self.pending.layer)
        if self.preload and self.preload.task.done() and not self.preload.task.cancelled() \
                and self.preload.task.exception() is None:
            layers.append(self.preload.task.result())
        return [lyr for lyr in layers if lyr is not None and lyr.state != "removed"]

    def _apply_live_item_props(self) -> None:
        """Push item edits that can change on the fly (position) to live layers."""
        if not self.backend:
            return
        items = {it["uid"]: it for pl in self.store.playlists.values() for it in pl["items"]}
        for layer in self._active_layers():
            it = items.get(layer.source.uid or "")
            if it is None:
                continue
            offset = (it.get("offset_x") or 0, it.get("offset_y") or 0)
            if offset != layer.offset:
                self.backend.set_position(layer, *offset)

    def set_loop_item(self, enabled: bool) -> None:
        self.loop_item = bool(enabled)
        self._notify()

    def _resync_target(self) -> None:
        """Keep the current position meaningful after playlist edits."""
        t = self.target
        if not t:
            return
        pl = self.store.get_playlist(t.playlist)
        if not pl:
            return  # keeps playing the current item; next_after() will stop
        for i, it in enumerate(pl["items"]):
            if it["uid"] == t.uid:
                t.index = i
                return
        # current item was removed: continue with whatever is now at its spot
        t.index = min(t.index, len(pl["items"])) - 1

    # ----------------------------------------------------------------- tick
    async def _tick_loop(self) -> None:
        while True:
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("tick failed")
            await asyncio.sleep(TICK)

    def _tick(self) -> None:
        b = self.backend
        if not b or self._restarting:
            return
        now = b.now()
        for layer, until in list(self.outgoing):
            if now >= until:
                b.remove_layer(layer)
                self.outgoing.remove((layer, until))

        p = self.pending
        if p and p.layer.state == "loading" and now - p.created > LOAD_TIMEOUT:
            p.layer.state = "error"
            self._on_layer_error(p.layer, "timed out while loading")
            return

        cur = self.current
        if self.state != "playing" or cur is None or self.pending or self.target is None:
            return
        if self.auto_requested_for == cur.id:
            return
        if self.loop_item:
            if cur.source.kind == "image":
                return  # an image simply stays up
            nxt, trans = self.target, {"type": "cut", "duration": 0, "color": "#000000"}
        else:
            nxt, trans = self.next_after(self.target)
        dur = self.duration(cur)
        pos = cur.position(now)
        if nxt == HOLD:
            if cur.eos or (dur is not None and pos >= dur):
                self.state = "held"
                self._notify()
            return
        tdur = 0.0 if trans["type"] == "cut" else float(trans["duration"])
        if dur is None:
            if cur.eos:
                self._request_auto(nxt, trans, now)
            return
        remaining = dur - pos
        if isinstance(nxt, Target) and remaining <= PRELOAD_LEAD + tdur:
            self._ensure_preload(nxt)
        if cur.eos or remaining <= tdur + getattr(b, "lookahead", LOOKAHEAD):
            at = cur.start_time + cur.paused_total + max(0.0, dur - tdur)
            self._request_auto(nxt, trans, at)

    def _request_auto(self, nxt: Target | str, trans: dict, at: float) -> None:
        assert self.current
        self.auto_requested_for = self.current.id
        asyncio.create_task(self._switch(nxt, trans, manual=False, at=at))

    def duration(self, layer: Layer) -> float | None:
        if layer.source.kind == "image":
            t = self.target
            pl = self.store.get_playlist(t.playlist) if t else None
            if pl and t and t.index < len(pl["items"]) and pl["items"][t.index]["uid"] == t.uid:
                # live value, so duration edits apply to the image on screen
                return pl["items"][t.index].get("duration") or self.store.settings["default_image_duration"]
            return layer.duration or self.store.settings["default_image_duration"]
        return layer.duration or layer.source.duration_hint

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        b = self.backend
        info = b.info() if b else None
        now = b.now() if b else 0.0
        cur, t = self.current, self.target
        pl = self.store.get_playlist(t.playlist) if t else None
        out: dict[str, Any] = {
            "state": self.state,
            "playlist": {"id": pl["id"], "name": pl["name"]} if pl else None,
            "index": t.index if t else None,
            "item": None,
            "position": None,
            "duration": None,
            "next": None,
            "loading": bool(self.pending and self.pending.layer.state == "loading"),
            "transitioning": now < self.transition_until,
            "loop_item": self.loop_item,
            "last_error": self.last_error,
            # When it happened, so a caller can tell a live problem from one
            # that was logged days ago. `state` says whether anything is wrong
            # *now*; last_error is history and is never cleared silently.
            "last_error_at": self.last_error_at,
            # One field for monitoring to watch, present in every state --
            # including when the renderer is down and "output" is null.
            # True/False from the renderer or the dormancy flag, None when
            # the backend has no concept of a display (the simulator).
            "display_connected": self._display_connected(info),
            "output": info,
        }
        if cur and t:
            out["item"] = {"uid": t.uid, "media": cur.source.media, "kind": cur.source.kind}
            out["position"] = round(cur.position(now), 2)
            d = self.duration(cur)
            out["duration"] = round(d, 2) if d else None
            nxt, trans = self.next_after(t)
            if self.loop_item:
                nxt, trans = t, {"type": "cut", "duration": 0, "color": "#000000"}
            if isinstance(nxt, Target):
                npl = self.store.get_playlist(nxt.playlist)
                out["next"] = {"playlist": nxt.playlist, "index": nxt.index,
                               "media": npl["items"][nxt.index]["media"] if npl else None,
                               "transition": trans}
            else:
                out["next"] = {"action": nxt}
        return out

    def _display_connected(self, info: dict | None) -> bool | None:
        if self.no_display:
            return False
        disp = (info or {}).get("display")
        if isinstance(disp, dict) and "connected" in disp:
            return bool(disp["connected"])
        return None

    def health(self) -> dict:
        return {**hwmod.health(), "errors": self.errors[-10:]}
