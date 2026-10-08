"""The head: shows the picture, as told by the controller over the link.

The head has no playlists and no web UI. It runs a video renderer (the same
KMS backend as PiPlayer, with audio off) and executes the controller's
commands on it: create a layer for a media file, start it at time T, fade it
with these keyframes, remove it. Times arrive on the head's own monotonic
clock (the controller does the conversion) and are turned into the renderer's
running time here.

Media files must already be on the head, under the same names the controller
uses. The head tells the controller which files it has.

If the controller goes quiet, the head keeps showing what it has, and when
the clip on screen ends it loops that clip on its own so the picture stays
alive. When the controller returns it takes over again with a fresh cue.
"""

from __future__ import annotations

import asyncio
import copy
import io
import logging
import socket
from typing import Callable

from . import __version__
from . import hw as hwmod
from .backend import Backend, Layer, NoDisplayError, Source
from .config import Store, ValidationError
from .link import PROTOCOL, Link
from .media import MediaLibrary

log = logging.getLogger(__name__)

LINK_TIMEOUT = 3.0  # no ping for this long: the controller is gone
STALE_SECONDS = 6.0  # layers from before a reset are removed after this long at most
REPORT_SECONDS = 3.0
MEDIA_POLL_SECONDS = 5.0
PREVIEW_WIDTH = 320
RESTART_BACKOFF = (2, 5, 10, 30)
NO_DISPLAY_RETRY = 30


class Head:
    def __init__(self, store: Store, library: MediaLibrary,
                 backend_factory: Callable[..., Backend], hw: dict, link: Link):
        self.store = store
        self.library = library
        self.backend_factory = backend_factory
        self.hw = hw
        self.link = link
        self.backend: Backend | None = None
        self.backend_ok = False
        self.no_display = False
        self.layers: dict[int, Layer] = {}  # controller's layer id -> our layer
        self.ids: dict[int, int] = {}  # our layer id -> controller's layer id
        self.making: dict[int, asyncio.Task] = {}  # image layers being rendered
        self.stale: list[Layer] = []  # layers a reset has orphaned, still on screen
        self.top: Layer | None = None  # the layer most recently started
        self.orphan: Layer | None = None  # replacement being loaded while the link is down
        # Text put on the display while the head is unpaired or pairing (see
        # pairing.py). Never shown once the head has a controller.
        self.notice_lines: list[str] | None = None
        self.notice: Layer | None = None
        self._notice_task: asyncio.Task | None = None
        self.last_ping = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._restarts = 0
        self._restart_task: asyncio.Task | None = None
        self._media: dict[str, int] | None = None
        self._stale_timer: asyncio.TimerHandle | None = None
        self._handlers = {
            "hello": self._m_hello, "reset": self._m_reset, "new": self._m_new,
            "start": self._m_start, "alpha": self._m_alpha, "dip": self._m_dip,
            "pos": self._m_pos, "freeze": self._m_freeze, "pause": self._m_pause,
            "resume": self._m_resume, "rm": self._m_rm, "bg": self._m_bg, "snap": self._m_snap,
        }

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.link.set_handlers(self._on_message, self._on_link)
        await self._start_backend()
        if not self.backend_ok:
            self._schedule_restart()
        self._apply_notice()
        await self.link.start()
        self._tasks = [asyncio.create_task(self._report_loop()),
                       asyncio.create_task(self._media_loop())]

    async def shutdown(self) -> None:
        for t in self._tasks + list(self.making.values()) + [self._restart_task,
                                                             self._notice_task]:
            if t:
                t.cancel()
        await self.link.stop()
        if self.backend:
            await self.backend.stop()

    def _settings(self) -> dict:
        s = copy.deepcopy(self.store.settings)
        s["audio"]["enabled"] = False  # sound is the controller's job
        return s

    async def _start_backend(self) -> None:
        try:
            self.backend = self.backend_factory(self._settings(), self.hw, self._emit)
            await self.backend.start()
            self.backend.set_background(self.store.settings["background_color"])
            self.backend_ok, self.no_display = True, False
            self._restarts = 0
        except NoDisplayError:
            log.info("no display connected; waiting for one")
            self.backend_ok, self.no_display = False, True
        except Exception:  # noqa: BLE001
            log.exception("renderer failed to start")
            self.backend_ok, self.no_display = False, False

    async def _restart_backend(self, delay: float = 0.0) -> None:
        """Rebuild the renderer (forgetting every layer) and tell the controller."""
        self.backend_ok = False
        if delay:
            await asyncio.sleep(delay)
        for t in self.making.values():
            t.cancel()
        self.making.clear()
        self.layers.clear()
        self.ids.clear()
        self.stale = []
        self.top = self.orphan = self.notice = None
        if self.backend:
            try:
                await self.backend.stop()
            except Exception:  # noqa: BLE001
                log.exception("error stopping the renderer")
        await self._start_backend()
        self._restart_task = None
        if not self.backend_ok:
            self._schedule_restart()
        self._apply_notice()
        self._send_hello()

    def _schedule_restart(self, immediate: bool = False) -> None:
        if self._restart_task and not self._restart_task.done():
            return
        if immediate:
            delay = 0.0
        elif self.no_display:
            delay = NO_DISPLAY_RETRY
        else:
            delay = RESTART_BACKOFF[min(self._restarts, len(RESTART_BACKOFF) - 1)]
            self._restarts += 1
        self._restart_task = asyncio.create_task(self._restart_backend(delay))

    # --------------------------------------------------------------- notice
    def set_notice(self, lines: list[str] | None) -> None:
        """Show these lines of text on the display, or take the text down."""
        if lines != self.notice_lines:
            self.notice_lines = lines
            self._apply_notice()

    def _apply_notice(self) -> None:
        if self._notice_task:
            self._notice_task.cancel()
            self._notice_task = None
        if self.notice:
            self._remove(self.notice)
            self.notice = None
        if self.notice_lines and self.backend and self.backend_ok:
            self._notice_task = asyncio.create_task(self._make_notice(list(self.notice_lines)))

    async def _make_notice(self, lines: list[str]) -> None:
        try:
            path = self.library.cache_dir / "notice.png"
            await asyncio.to_thread(_render_notice, lines, self.backend.render_size, path)
            self.notice = self.backend.create_layer(Source("image", path, "notice", "stretch"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a missing notice must not stop the head
            log.exception("could not show %r", lines)

    # ----------------------------------------------------------------- time
    def _rt(self, t: float) -> float:
        """A time on our monotonic clock as a renderer running time."""
        return self.backend.now() + (t - self.link.clock())

    def _kfs(self, kfs: list) -> list[tuple[float, float]]:
        return [(self._rt(float(t)), float(v)) for t, v in kfs]

    def _live(self) -> bool:
        return self.link.connected and self.link.clock() - self.last_ping < LINK_TIMEOUT

    # ------------------------------------------------------------- messages
    def _on_link(self, up: bool) -> None:
        if up:
            self.last_ping = self.link.clock()

    def _on_message(self, msg: dict, blob: bytes, rx: float) -> None:
        t = msg.get("t")
        if t == "ping":
            self.last_ping = rx
            self.link.send({"t": "pong", "n": msg.get("n"), "t0": msg.get("t0"),
                            "t1": round(rx, 5)}, stamp="t2")
            return
        fn = self._handlers.get(t)
        if fn is None:
            log.debug("ignoring unknown message %r", t)
            return
        if t != "hello" and not (self.backend and self.backend_ok):
            return  # nothing to render on; the controller is told when that changes
        fn(msg)

    def _send_hello(self) -> None:
        if not self.link.connected:
            return
        b = self.backend
        ok = bool(b and self.backend_ok)
        self._media = self._list_media()
        self.link.send({
            "t": "hello", "v": PROTOCOL, "ver": __version__, "name": socket.gethostname(),
            "ok": ok, "nodisp": self.no_display,
            "size": list(b.render_size) if ok else None,
            "fps": getattr(b, "fps", None) if ok else None,
            "overlap": b.video_overlap_ok if ok else True,
            "out": b.info() if ok else None,
            "media": self._media,
        })

    def _m_hello(self, msg: dict) -> None:
        s = msg.get("s") or {}
        patch: dict = {}
        if isinstance(s.get("output"), dict) and s["output"] != self.store.settings["output"]:
            patch["output"] = s["output"]
        if s.get("bg") and s["bg"] != self.store.settings["background_color"]:
            patch["background_color"] = s["bg"]
        rebuild = False
        if patch:
            try:
                self.store.update_settings(patch)
                rebuild = "output" in patch
            except ValidationError as e:
                log.warning("ignoring settings from the controller: %s", e)
        if rebuild:
            log.info("output settings changed; restarting the renderer")
            if self._restart_task:
                self._restart_task.cancel()
                self._restart_task = None
            self._schedule_restart(immediate=True)  # says hello when it is back
            return
        if "background_color" in patch and self.backend and self.backend_ok:
            self.backend.set_background(patch["background_color"])
        self._send_hello()

    def _m_reset(self, msg: dict) -> None:
        """The controller starts over. Keep the picture up until it shows something new."""
        for t in self.making.values():
            t.cancel()
        self.making.clear()
        self.stale += [lyr for lyr in self.layers.values() if lyr.state != "removed"]
        if self.orphan:
            self.stale.append(self.orphan)
        self.layers.clear()
        self.ids.clear()
        self.orphan = None
        self._clear_stale_in(STALE_SECONDS)

    def _clear_stale_in(self, delay: float) -> None:
        if self._stale_timer:
            self._stale_timer.cancel()
        self._stale_timer = self._loop.call_later(max(0.0, delay), self._clear_stale)

    def _clear_stale(self) -> None:
        self._stale_timer = None
        stale, self.stale = self.stale, []
        for lyr in stale:
            self._remove(lyr)

    def _m_new(self, msg: dict) -> None:
        lid, name, kind = msg["id"], msg["m"], msg.get("k", "video")
        offset = tuple(msg.get("off") or (0, 0))
        fit = msg.get("fit") or "contain"
        if not self.library.exists(name):
            self._event(lid, "error", msg="file is not on the head")
            return
        if kind == "image":
            self.making[lid] = asyncio.create_task(
                self._make_image(lid, name, fit, offset, msg.get("bg") or "#000000"))
        else:
            self._create(lid, Source("video", self.library.path(name), name, fit, offset=offset))

    async def _make_image(self, lid: int, name: str, fit: str, offset: tuple,
                          bg: str) -> None:
        try:
            w, h = self.backend.render_size
            path = await asyncio.to_thread(self.library.rendered_image, name, w, h, fit, bg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - PIL errors, OS errors
            self.making.pop(lid, None)
            self._event(lid, "error", msg=str(e))
            return
        self.making.pop(lid, None)
        self._create(lid, Source("image", path, name, "stretch", offset=offset))

    def _create(self, lid: int, src: Source) -> None:
        try:
            layer = self.backend.create_layer(src)
        except Exception as e:  # noqa: BLE001 - renderer couldn't build it
            log.exception("could not create a layer for %s", src.media)
            self._event(lid, "error", msg=f"renderer error: {e}")
            return
        self.layers[lid] = layer
        self.ids[layer.id] = lid

    def _m_start(self, msg: dict) -> None:
        layer = self.layers.get(msg["id"])
        if not layer or layer.state == "removed":
            return
        at = self._rt(float(msg["at"]))
        self.backend.start_layer(layer, at)
        self.top = layer
        if self.stale:  # the new picture replaces whatever a reset left behind
            self._clear_stale_in(at - self.backend.now() + 0.1)

    def _m_alpha(self, msg: dict) -> None:
        layer = self.layers.get(msg["id"])
        if layer and layer.state != "removed":
            self.backend.set_alpha(layer, self._kfs(msg["k"]))

    def _m_dip(self, msg: dict) -> None:
        self.backend.dip(msg.get("c") or "#000000", self._kfs(msg["k"]))

    def _m_pos(self, msg: dict) -> None:
        layer = self.layers.get(msg["id"])
        if layer and layer.state != "removed":
            self.backend.set_position(layer, int(msg["off"][0]), int(msg["off"][1]))
            self._event(msg["id"], "geom", full=layer.full_frame)

    def _m_freeze(self, msg: dict) -> None:
        layer = self.layers.get(msg["id"])
        if layer and layer.state != "removed":
            self.backend.freeze(layer, self._rt(float(msg["at"])))

    def _m_pause(self, msg: dict) -> None:
        layer = self.layers.get(msg["id"])
        if layer and layer.state != "removed":
            self.backend.pause_layer(layer)

    def _m_resume(self, msg: dict) -> None:
        layer = self.layers.get(msg["id"])
        if layer and layer.state != "removed":
            self.backend.resume_layer(layer, float(msg.get("shift") or 0))

    def _m_rm(self, msg: dict) -> None:
        lid = msg["id"]
        task = self.making.pop(lid, None)
        if task:
            task.cancel()
        layer = self.layers.pop(lid, None)
        if layer:
            self._remove(layer)

    def _m_bg(self, msg: dict) -> None:
        try:
            self.store.update_settings({"background_color": msg["c"]})
        except ValidationError:
            return
        self.backend.set_background(msg["c"])

    def _m_snap(self, msg: dict) -> None:
        asyncio.create_task(self._snapshot())

    async def _snapshot(self) -> None:
        try:
            data = await self.backend.snapshot() if self.backend else None
            if data:
                data = await asyncio.to_thread(_shrink, data)
        except Exception:  # noqa: BLE001
            log.exception("snapshot failed")
            data = None
        self.link.send({"t": "snap"}, data or b"")

    def _remove(self, layer: Layer) -> None:
        self.ids.pop(layer.id, None)
        if layer is self.top:
            self.top = None
        if layer.state != "removed" and self.backend:
            self.backend.remove_layer(layer)

    # ------------------------------------------------------ renderer events
    def _emit(self, event: str, layer: Layer | None, detail: object) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._on_event, event, layer, detail)

    def _event(self, lid: int, event: str, **extra) -> None:
        self.link.send({"t": "ev", "e": event, "id": lid, **extra})

    def _on_event(self, event: str, layer: Layer | None, detail: object) -> None:
        if event == "fatal":
            log.error("renderer failed: %s", detail)
            self.backend_ok = False
            self._send_hello()
            self._schedule_restart()
            return
        if layer is None or layer.state == "removed":
            return
        if layer is self.notice:
            if event == "ready":
                at = self.backend.now() + 0.05
                self.backend.start_layer(layer, at)
                self.backend.set_alpha(layer, [(at, 1.0)])
            return
        if layer is self.orphan:
            self._orphan_event(event, layer)
            return
        lid = self.ids.get(layer.id)
        if event == "ready":
            if lid is not None:
                self._event(lid, "ready", dur=layer.duration, full=layer.full_frame)
        elif event == "eos":
            if lid is not None:
                self._event(lid, "eos")
            if layer is self.top and not self._live():
                self._orphan_loop(layer)
        elif event == "error":
            layer.state = "error"
            if lid is not None:
                self._event(lid, "error", msg=str(detail))

    # ------------------------------------------- on our own (link is down)
    def _orphan_loop(self, ended: Layer) -> None:
        """The clip on screen ended and nobody is telling us what comes next: loop it."""
        if ended.source.kind != "video" or self.orphan:
            return
        log.info("controller is away; looping %s", ended.source.media)
        try:
            self.orphan = self.backend.create_layer(ended.source)
        except Exception:  # noqa: BLE001
            log.exception("could not reload %s", ended.source.media)
            self.orphan = None

    def _orphan_event(self, event: str, layer: Layer) -> None:
        if event == "error":
            self.orphan = None
            self._remove(layer)
            return
        if event != "ready":
            return
        self.orphan = None
        if self._live():  # the controller is back and has its own plans
            self._remove(layer)
            return
        old = self.top
        at = self.backend.now() + 0.1
        self.backend.start_layer(layer, at)
        self.backend.set_alpha(layer, [(at, 1.0)])
        self.top = layer
        if old:
            lid = self.ids.pop(old.id, None)
            if lid is not None:
                self.layers.pop(lid, None)
            self._loop.call_later(0.4, self._remove, old)
        # Owned by nobody on the controller; a reset sweeps it up with the rest.
        self.stale.append(layer)

    # ------------------------------------------------------------- reporting
    def _list_media(self) -> dict[str, int]:
        return {e["name"]: e["size"] for e in self.library.list()}

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(REPORT_SECONDS)
            if not self._live():
                continue
            try:
                health = await asyncio.to_thread(hwmod.health)
                out = self.backend.info() if self.backend and self.backend_ok else None
                self.link.send({"t": "info", "out": out, "health": health})
            except Exception:  # noqa: BLE001
                log.exception("status report failed")

    async def _media_loop(self) -> None:
        while True:
            await asyncio.sleep(MEDIA_POLL_SECONDS)
            if not self._live():
                continue
            files = await asyncio.to_thread(self._list_media)
            if files != self._media:
                self._media = files
                self.link.send({"t": "media", "files": files})


def _render_notice(lines: list[str], size: tuple[int, int], path) -> None:
    """White text centred on black; the last line (the code) is the big one."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = size
    im = Image.new("RGB", (w, h), "black")
    draw = ImageDraw.Draw(im)

    def font(px: int):
        try:
            return ImageFont.load_default(size=px)  # Pillow >= 10.1
        except TypeError:
            return ImageFont.load_default()

    sizes = [h // 12] * (len(lines) - 1) + [h // 5]
    boxes = []
    for text, px in zip(lines, sizes):
        f = font(px)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=f)
        boxes.append((text, f, right - left, bottom - top, left, top))
    gap = h // 20
    y = (h - sum(b[3] for b in boxes) - gap * (len(boxes) - 1)) // 2
    for text, f, tw, th, left, top in boxes:
        draw.text(((w - tw) // 2 - left, y - top), text, font=f, fill="white")
        y += th + gap
    im.save(path)


def _shrink(jpeg: bytes) -> bytes:
    """Re-encode a preview small enough to send over the link in a moment."""
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as im:
        im = im.convert("RGB")
        if im.width > PREVIEW_WIDTH:
            im = im.resize((PREVIEW_WIDTH, max(1, round(im.height * PREVIEW_WIDTH / im.width))))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=60)
        return buf.getvalue()
