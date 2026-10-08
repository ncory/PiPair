"""Simulated backend for development and tests (no video output).

It models timing exactly like the real compositor (layers, keyframed alpha,
pause offsets, EOS) and can render a preview frame with Pillow, so the web UI
and playlist logic can be exercised on any machine.
"""

from __future__ import annotations

import asyncio
import io
import time

from .backend import (Backend, Keyframes, Layer, Source, covers, eval_keyframes, fit_rect,
                      hex_to_rgb)

DEFAULT_VIDEO_SECONDS = 8.0


class SimBackend(Backend):
    name = "sim"

    def __init__(self, settings, hw, emit, load_delay: float = 0.25):
        super().__init__(settings, hw, emit)
        self.load_delay = load_delay
        self.t0 = time.monotonic()
        self.layers: dict[int, dict] = {}
        self.z = 0
        self.background = settings["background_color"]
        self.dip_color = "#000000"
        self.dip_kfs: Keyframes = []
        self.master_volume = settings["audio"]["volume"]
        self._task: asyncio.Task | None = None
        self.frozen: dict[int, float] = {}
        size = settings["output"]["render_size"]
        if size != "auto":
            w, h = (int(x) for x in size.split("x"))
            self.render_size = (w, h)
        if settings["output"]["rotation"] in (90, 270):
            self.render_size = self.render_size[::-1]

    async def start(self) -> None:
        self._task = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self.layers.clear()

    def now(self) -> float:
        return time.monotonic() - self.t0

    def create_layer(self, source: Source) -> Layer:
        layer = Layer(source)
        layer.created = self.now()
        self.z += 1
        self.layers[layer.id] = {"layer": layer, "z": self.z, "alpha": [], "alpha_base": 0.0,
                                 "volume": [], "volume_base": 0.0}
        if source.kind == "video":
            layer.duration = source.duration_hint or DEFAULT_VIDEO_SECONDS
        self._update_cover(layer)
        asyncio.get_running_loop().call_later(self.load_delay, self._ready, layer)
        return layer

    def _ready(self, layer: Layer) -> None:
        if layer.state != "loading":
            return
        if not layer.source.path.exists():
            layer.state = "error"
            layer.error = "file not found"
            self.emit("error", layer, layer.error)
            return
        layer.state = "ready"
        self.emit("ready", layer, None)

    def start_layer(self, layer: Layer, at: float) -> None:
        layer.start_time = at
        layer.state = "playing"

    def set_alpha(self, layer: Layer, kfs: Keyframes) -> None:
        d = self.layers.get(layer.id)
        if d:
            d["alpha_base"] = eval_keyframes(d["alpha"], self.now(), d["alpha_base"])
            d["alpha"] = sorted(kfs)

    def set_volume(self, layer: Layer, kfs: Keyframes) -> None:
        d = self.layers.get(layer.id)
        if d:
            d["volume_base"] = eval_keyframes(d["volume"], self.now(), d["volume_base"])
            d["volume"] = sorted(kfs)

    def dip(self, color: str, kfs: Keyframes) -> None:
        self.dip_color = color
        self.dip_kfs = sorted(kfs)

    def _rect(self, layer: Layer, w: int, h: int) -> tuple[int, int, int, int]:
        """Placement in a w x h frame (4:3 if the file name says so, for tests)."""
        aspect = (4, 3) if "4x3" in layer.source.media else (w, h)
        if layer.source.kind == "image":
            x, y, rw, rh = 0, 0, w, h
        else:
            x, y, rw, rh = fit_rect(*aspect, w, h, layer.source.fit)
        sx, sy = w / self.render_size[0], h / self.render_size[1]
        return x + round(layer.offset[0] * sx), y + round(layer.offset[1] * sy), rw, rh

    def _update_cover(self, layer: Layer) -> None:
        layer.full_frame = covers(self._rect(layer, *self.render_size), *self.render_size)

    def set_position(self, layer: Layer, dx: int, dy: int) -> None:
        layer.offset = (dx, dy)
        self._update_cover(layer)

    def freeze(self, layer: Layer, at: float) -> None:
        self.frozen[layer.id] = at

    def pause_layer(self, layer: Layer) -> None:
        pass  # position is frozen by the engine via layer.paused_at

    def resume_layer(self, layer: Layer, shift: float) -> None:
        pass

    def remove_layer(self, layer: Layer) -> None:
        layer.state = "removed"
        self.layers.pop(layer.id, None)

    def set_background(self, color: str) -> None:
        self.background = color

    def set_master_volume(self, volume: float) -> None:
        self.master_volume = volume

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            now = self.now()
            for d in list(self.layers.values()):
                layer = d["layer"]
                if (layer.source.kind == "video" and layer.state == "playing" and not layer.eos
                        and layer.duration is not None and layer.position(now) >= layer.duration):
                    layer.eos = True
                    self.emit("eos", layer, None)

    # -- introspection (tests + preview) ------------------------------------
    def alpha(self, layer: Layer, t: float | None = None) -> float:
        d = self.layers.get(layer.id)
        if not d:
            return 0.0
        return eval_keyframes(d["alpha"], self.now() if t is None else t, d["alpha_base"])

    def visible(self) -> list[tuple[Layer, float]]:
        now = self.now()
        out = []
        for d in sorted(self.layers.values(), key=lambda d: d["z"]):
            layer = d["layer"]
            if layer.state == "playing" and layer.start_time is not None and now >= layer.start_time:
                a = eval_keyframes(d["alpha"], now, d["alpha_base"])
                if a > 0.001:
                    out.append((layer, a))
        return out

    async def snapshot(self) -> bytes | None:
        return await asyncio.to_thread(self._render, self.visible(),
                                       eval_keyframes(self.dip_kfs, self.now(), 0.0), self.now())

    def _render(self, visible, dip_alpha: float, now: float) -> bytes:
        from PIL import Image, ImageDraw

        rw, rh = self.render_size
        w, h = 640, max(1, round(640 * rh / rw))
        frame = Image.new("RGB", (w, h), hex_to_rgb(self.background))
        for layer, a in visible:
            src = layer.source
            x, y, pw, ph = self._rect(layer, w, h)
            pic = frame.copy()
            if src.kind == "image":
                with Image.open(src.path) as im:
                    pic.paste(im.convert("RGB").resize((pw, ph)), (x, y))
            else:
                hue = (layer.id * 67) % 255
                ImageDraw.Draw(pic).rectangle([x, y, x + pw - 1, y + ph - 1],
                                              fill=(40, hue // 2 + 40, 120 + hue // 3))
                pos = layer.position(now)
                ImageDraw.Draw(pic).text((x + 20, y + 20),
                                         f"{src.media}\n{pos:6.2f}s / {layer.duration:.2f}s",
                                         fill=(255, 255, 255))
            frame = Image.blend(frame, pic, a)
        if dip_alpha > 0.001:
            frame = Image.blend(frame, Image.new("RGB", (w, h), hex_to_rgb(self.dip_color)), dip_alpha)
        buf = io.BytesIO()
        frame.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    def info(self) -> dict:
        return {"backend": self.name, "render_size": list(self.render_size),
                "fps": self.settings["output"]["fps"], "display": None, "layers": len(self.layers)}
