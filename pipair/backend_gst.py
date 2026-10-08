"""GStreamer renderer: hardware decode -> GL compositor -> KMS (no X/Wayland).

Pipeline (one per process lifetime, rebuilt when output settings change):

    videotestsrc(bg color, live) ─ glupload ─┐
    [layer bins, added/removed at runtime] ──┤ glvideomixerelement ─ caps ─ tee ─ queue ─ glimagesink (GBM/KMS)
    videotestsrc(dip color, live) ─ glupload ─┘                             └ valve ─ glcolorscale ─ gldownload ─ jpegenc ─ appsink (preview)

    audiotestsrc(silence, live) ─┐
    [layer audio] ───────────────┤ audiomixer ─ volume ─ alsasink (HDMI)

Each playlist item becomes a "layer" bin (filesrc ! decodebin ! glupload !
glcolorconvert, plus an audio branch). A layer is prerolled with a blocking
pad probe holding its first frame; to start it at running time T the probe is
removed after setting the pad offset so that frame lands exactly at T.
Fades are GstController keyframes on the mixer pads' alpha/volume, which the
aggregators evaluate per output frame.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import re
import threading
from typing import Any

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstController", "1.0")
from gi.repository import GLib, Gst, GstController  # noqa: E402

from . import hw as hwmod  # noqa: E402
from .backend import (Backend, Keyframes, Layer, Source, classify_error, covers,  # noqa: E402
                      fit_rect, hex_to_rgb)

log = logging.getLogger(__name__)
SEC = Gst.SECOND
Gst.init(None)

# Known-harmless GStreamer criticals, filtered so they don't flood the journal.
#  - "gst_memory_resize: assertion 'gst_memory_is_writable (mem)' failed":
#    when a frozen frame has been imported by the ISP (freeze-mode dissolves),
#    its memory is shared, and the decoder's pool can't reset it on teardown.
#    Nothing is lost: the pool just doesn't reuse that buffer. ~2 per freeze.
_QUIET = ("gst_memory_resize: assertion 'gst_memory_is_writable (mem)' failed",)


def _log_filter(domain, level, message, _data=None):
    if message and any(q in message for q in _QUIET):
        return
    GLib.log_default_handler(domain, level, message, None)


for _domain in ("GStreamer",):
    GLib.log_set_handler(_domain, GLib.LogLevelFlags.LEVEL_CRITICAL
                         | GLib.LogLevelFlags.LEVEL_WARNING, _log_filter, None)

ROTATE = {0: "none", 90: "clockwise", 180: "rotate-180", 270: "counterclockwise"}
PREVIEW_WIDTH = 480
DIP_ZORDER = 1_000_000
AUDIO_CAPS = "audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved"

_LAYER_NAME = re.compile(r"^layer(\d+)(?:_|$)")

# ALSA devices that failed to open; skipped on the next rebuild so a missing
# or silent HDMI sink doesn't take video down with it.
_broken_audio_devices: set[str] = set()


def configure_environment(output: dict | None) -> None:
    """Point GStreamer's GL stack at the HDMI connector via GBM/KMS.

    Must run before the first GL element is created. Explicit environment
    variables set by the user win.
    """
    os.environ.setdefault("GST_GL_WINDOW", "gbm")
    os.environ.setdefault("GST_GL_PLATFORM", "egl")
    os.environ.setdefault("GST_GL_API", "gles2")
    if output:
        os.environ.setdefault("GST_GL_GBM_DRM_DEVICE", output["card"])
        os.environ.setdefault("GST_GL_GBM_DRM_CONNECTOR", output["connector"])


@contextlib.contextmanager
def _structure(caps: Gst.Caps, index: int = 0):
    """caps.get_structure(), across gst-python versions.

    From 1.26 it returns a StructureWrapper that must be used as a context
    manager; older versions return the Gst.Structure directly.
    """
    s = caps.get_structure(index)
    if isinstance(s, Gst.Structure):
        yield s
    else:
        with s as st:
            yield st


def _color_argb(color: str) -> int:
    r, g, b = hex_to_rgb(color)
    return (0xFF << 24) | (r << 16) | (g << 8) | b


class _LayerGst:
    """GStreamer objects belonging to one layer."""

    def __init__(self) -> None:
        self.bin: Gst.Bin | None = None
        self.pads: dict[str, Gst.Pad] = {}  # "video"/"audio" -> ghost src pad
        self.mix_pads: dict[str, Gst.Pad] = {}
        self.block_probes: dict[str, int] = {}
        self.blocked: set[str] = set()
        self.segments: dict[str, Gst.Segment] = {}
        self.rt0: int | None = None  # running time of first video frame (no offset)
        self.no_more_pads = False
        self.bindings: dict[str, GstController.InterpolationControlSource] = {}
        self.lock = threading.Lock()
        self.ready_sent = False
        self.rect: tuple[int, int, int, int] | None = None  # fitted placement, before offset
        self.extra_elements: list[Gst.Element] = []  # per-layer elements outside the bin
        # Every signal handler and pad probe registered for this layer. Their
        # closures reference the layer, so unless they're all removed at
        # teardown the bin (and its bus sockets, decoder buffers...) is never
        # freed: C-side references Python's GC can't see or break.
        self.handlers: list[tuple[Gst.Object, int]] = []
        self.probes: list[tuple[Gst.Pad, int]] = []


class GstBackend(Backend):
    name = "gstreamer"
    prefer_hdmi_audio = True  # what audio.device "auto" resolves to (see hw.audio_device)

    def __init__(self, settings: dict, hw: dict, emit, sink: str = "kms"):
        super().__init__(settings, hw, emit)
        self.sink_mode = sink
        self.output = hwmod.pick_output() if sink == "kms" else None
        self.render_size = self._render_size()
        self.fps = settings["output"]["fps"]
        self.pipeline: Gst.Pipeline | None = None
        self.mixer: Gst.Element | None = None
        self.amixer: Gst.Element | None = None
        self.layers: dict[int, Layer] = {}
        self.z = 10
        self.loop: asyncio.AbstractEventLoop | None = None
        self.teardown = concurrent.futures.ThreadPoolExecutor(1, "pipair-teardown")
        self.audio_device: str | None = None
        self.dropped = 0
        self.rendered = 0
        self._snap_waiters: list[asyncio.Future] = []
        self._dip_cs: GstController.InterpolationControlSource | None = None
        self._stopping = False

    # ------------------------------------------------------------ geometry
    def _render_size(self) -> tuple[int, int]:
        o = self.settings["output"]
        if o["render_size"] != "auto":
            w, h = (int(x) for x in o["render_size"].split("x"))
        else:
            mode = (self.output or {}).get("preferred_mode") or "1920x1080"
            try:
                w, h = (int(x) for x in mode.rstrip("i").split("x"))
            except ValueError:
                w, h = 1920, 1080
            # Composite at no more than ~1080p; the sink scales to the mode.
            scale = min(1.0, (1920 * 1080 / (w * h)) ** 0.5, 2048 / max(w, h))
            w, h = int(w * scale), int(h * scale)
        w, h = w - w % 2, h - h % 2
        return (h, w) if o["rotation"] in (90, 270) else (w, h)

    # ------------------------------------------------------------- pipeline
    def _sink_desc(self) -> str:
        rot = ROTATE[self.settings["output"]["rotation"]]
        if self.sink_mode == "fake":
            return "fakesink name=sink sync=true qos=true"
        return (f"glimagesink name=sink force-aspect-ratio=true handle-events=false "
                f"rotate-method={rot} qos=true")

    def _audio_desc(self) -> str | None:
        a = self.settings["audio"]
        if not a["enabled"]:
            return None
        if self.sink_mode == "fake":
            self.audio_device = "fakesink"
            sink = "fakesink sync=true"
        else:
            dev = hwmod.audio_device(a["device"], self.prefer_hdmi_audio)
            if not dev or dev in _broken_audio_devices:
                if dev:
                    log.warning("audio device %s failed earlier; audio disabled", dev)
                return None
            self.audio_device = dev
            sink = f'alsasink name=asink device="{dev}" sync=true'
        return (f"audiotestsrc is-live=true wave=silence ! {AUDIO_CAPS} ! amix. "
                f"audiomixer name=amix ! {AUDIO_CAPS} ! audioconvert ! audioresample "
                f"! volume name=master ! {sink}")

    def _build(self) -> Gst.Pipeline:
        w, h = self.render_size
        f = self.fps
        pw = PREVIEW_WIDTH
        ph = max(2, round(pw * h / w / 2) * 2)
        color = f"video/x-raw,format=RGBA,width=16,height=16,framerate={f}/1"
        desc = (
            f"glvideomixerelement name=mix background=black "
            f"! video/x-raw(memory:GLMemory),format=RGBA,width={w},height={h},"
            f"framerate={f}/1,pixel-aspect-ratio=1/1 ! tee name=t "
            f"t. ! queue max-size-buffers=3 max-size-time=0 max-size-bytes=0 ! {self._sink_desc()} "
            f"t. ! queue leaky=downstream max-size-buffers=1 ! valve name=pvalve drop=true "
            f"! glcolorscale ! video/x-raw(memory:GLMemory),width={pw},height={ph},"
            f"pixel-aspect-ratio=1/1 ! gldownload ! videoconvert ! jpegenc quality=80 "
            f"! appsink name=psink emit-signals=true sync=false async=false max-buffers=1 drop=true "
            f"videotestsrc name=bg is-live=true pattern=solid-color ! {color} "
            f"! glupload ! glcolorconvert name=bgconv ! mix. "
            f"videotestsrc name=dip is-live=true pattern=solid-color ! {color} "
            f"! glupload ! glcolorconvert name=dipconv ! mix. "
        )
        audio = self._audio_desc()
        if audio:
            desc += audio
        log.info("building pipeline %dx%d@%d (%s, audio: %s)", w, h, f, self.sink_mode,
                 self.audio_device if audio else "off")
        pipe = Gst.parse_launch(desc)
        self.mixer = pipe.get_by_name("mix")
        self.amixer = pipe.get_by_name("amix")
        bg_pad = pipe.get_by_name("bgconv").get_static_pad("src").get_peer()
        dip_pad = pipe.get_by_name("dipconv").get_static_pad("src").get_peer()
        for pad, z, alpha in ((bg_pad, 0, 1.0), (dip_pad, DIP_ZORDER, 0.0)):
            pad.set_property("zorder", z)
            pad.set_property("alpha", alpha)
            pad.set_property("width", w)
            pad.set_property("height", h)
        self._dip_cs = self._bind(dip_pad, "alpha")
        pipe.get_by_name("psink").connect("new-sample", self._on_preview_sample)
        return pipe

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.pipeline = self._build()
        bus = self.pipeline.get_bus()
        bus.set_sync_handler(self._on_bus_message)
        self._drain_task = asyncio.create_task(self._drain_bus(bus))
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.emit("fatal", None, "pipeline failed to start")

    async def _drain_bus(self, bus: Gst.Bus) -> None:
        """Free the messages the sync handler let through (see _on_bus_message)."""
        while True:
            await asyncio.sleep(0.25)
            while bus.pop() is not None:
                pass

    async def stop(self) -> None:
        self._stopping = True
        pipe = self.pipeline
        self.pipeline = None
        if getattr(self, "_drain_task", None):
            self._drain_task.cancel()
        if pipe:
            await asyncio.to_thread(pipe.set_state, Gst.State.NULL)
            bus = pipe.get_bus()
            bus.set_sync_handler(None)
            bus.set_flushing(True)  # drops anything still queued
        self.layers.clear()
        self.teardown.shutdown(wait=False)

    def now(self) -> float:
        pipe = self.pipeline
        clock = pipe.get_clock() if pipe else None
        if clock is None:
            return 0.0
        return max(0, clock.get_time() - pipe.get_base_time()) / SEC

    # ----------------------------------------------------------- bus/events
    def _post(self, fn, *args) -> None:
        if self.loop and not self._stopping:
            self.loop.call_soon_threadsafe(fn, *args)

    def _on_bus_message(self, bus, msg) -> Gst.BusSyncReply:
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            lid, layer = self._layer_ref(msg.src)
            kind = classify_error(lid, layer is not None, msg.src.get_name(), self.audio_device)
            # A stale error is routine (every teardown can produce one), so it
            # is not worth a line in the journal at the default level.
            log.log(logging.DEBUG if kind == "stale" else logging.ERROR,
                    "gst error from %s: %s (%s)", msg.src.get_path_string(), err.message, dbg)
            if kind == "layer":
                self._post(self.emit, "error", layer, err.message)
            elif kind == "stale":
                pass  # the layer it describes is already gone
            elif kind == "audio":
                _broken_audio_devices.add(self.audio_device)
                self._post(self.emit, "fatal", None, f"audio output failed: {err.message}")
            else:
                self._post(self.emit, "fatal", None, err.message)
        elif t == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            log.warning("gst warning from %s: %s", msg.src.get_path_string(), err.message)
        elif t == Gst.MessageType.QOS and msg.src.get_name() == "sink":
            _fmt, processed, dropped = msg.parse_qos_stats()
            self.rendered, self.dropped = processed, dropped
        elif t == Gst.MessageType.LATENCY:
            self._post(self._recalc_latency)
        # PASS, not DROP: with a Python sync handler, gst-python leaks a
        # reference to every message it returns DROP for, and a message holds
        # its source element, so every element that ever posted a message
        # (decoders, bins...) was never freed. ~4 sockets per playlist item
        # leaked this way until the process hit its file limit (~250 items).
        # _drain_bus() pops and frees the passed messages.
        return Gst.BusSyncReply.PASS

    def _recalc_latency(self) -> None:
        if self.pipeline:
            self.pipeline.recalculate_latency()

    def _layer_ref(self, obj) -> tuple[int | None, Layer | None]:
        """The layer an element belongs to: inside its bin, or named layerN_*.

        Returns (id, layer). The id is set whenever the element sits under a
        layerN bin, even once that layer has been unregistered by
        remove_layer(); the layer is None in that case, which marks a message
        as stale rather than as a failure of the pipeline itself.
        """
        while obj is not None:
            name = obj.get_name() if hasattr(obj, "get_name") else ""
            m = _LAYER_NAME.match(name)
            if m:
                lid = int(m.group(1))
                return lid, self.layers.get(lid)
            obj = obj.get_parent()
        return None, None
        return None

    # --------------------------------------------------------------- layers
    def create_layer(self, source: Source) -> Layer:
        layer = Layer(source)
        g = _LayerGst()
        layer.g = g  # type: ignore[attr-defined]
        self.z += 1
        layer.z = self.z  # type: ignore[attr-defined]
        b = Gst.Bin.new(f"layer{layer.id}")
        g.bin = b
        # The bin must be in the pipeline before its pads can link to the mixer.
        self.pipeline.add(b)
        self.layers[layer.id] = layer
        try:
            self._build_layer(layer, b)
        except Exception:
            self.remove_layer(layer)
            raise
        b.sync_state_with_parent()
        return layer

    def _build_layer(self, layer: Layer, b: Gst.Bin) -> None:
        source = layer.source
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        src = Gst.ElementFactory.make("filesrc")
        src.set_property("location", str(source.path))
        b.add(src)
        if source.kind == "image":
            chain = [src]
            for name, props in (("jpegdec", {}), ("videoconvert", {}), ("imagefreeze", {}),
                                ("capsfilter", {"caps": Gst.Caps.from_string(
                                    "video/x-raw,framerate=1/1")})):
                el = Gst.ElementFactory.make(name)
                for k, v in props.items():
                    el.set_property(k, v)
                b.add(el)
                chain.append(el)
            for a, c in zip(chain, chain[1:]):
                a.link(c)
            g.no_more_pads = True
            self._add_output(layer, "video", chain[-1].get_static_pad("src"))
        else:
            dec = Gst.ElementFactory.make("decodebin")
            b.add(dec)
            src.link(dec)
            g.handlers.append((dec, dec.connect("pad-added", self._on_pad_added, layer)))
            g.handlers.append((dec, dec.connect("no-more-pads", self._on_no_more_pads, layer)))

    def _on_pad_added(self, dec, pad, layer: Layer) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        kind = ""
        if caps and caps.get_size():
            with _structure(caps) as st:
                kind = st.get_name().split("/")[0]
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        with g.lock:
            if kind == "video" and "video" not in g.pads:
                self._add_output(layer, "video", pad)
                return
            if kind == "audio" and "audio" not in g.pads and self.amixer is not None:
                self._add_output(layer, "audio", pad)
                return
        # anything else (extra streams, subtitles, audio with audio disabled)
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", False)
        sink.set_property("async", False)
        g.bin.add(sink)
        sink.sync_state_with_parent()
        pad.link(sink.get_static_pad("sink"))

    def _on_no_more_pads(self, dec, layer: Layer) -> None:
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        with g.lock:
            g.no_more_pads = True
            if "video" not in g.pads:
                self._post(self.emit, "error", layer, "no playable video stream")
                return
        self._check_ready(layer)

    # Hooks a subclass overrides to change where video goes (see backend_kms).
    def _video_elements(self) -> list[tuple[str, dict]]:
        """Elements between the decoder and the ghost pad for video."""
        return [("glupload", {}), ("glcolorconvert", {})]

    def _attach_video(self, layer: Layer, ghost: Gst.Pad) -> None:
        """Connect a layer's video ghost pad to the compositor."""
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        w, h = self.render_size
        mpad = self.mixer.request_pad_simple("sink_%u")
        mpad.set_property("zorder", layer.z)  # type: ignore[attr-defined]
        mpad.set_property("alpha", 0.0)
        mpad.set_property("repeat-after-eos", True)
        g.bindings["alpha"] = self._bind(mpad, "alpha")
        g.mix_pads["video"] = mpad
        self._place(layer, g.rect or (0, 0, w, h))
        ghost.link(mpad)

    def _add_output(self, layer: Layer, kind: str, pad: Gst.Pad) -> None:
        """Convert `pad` for its consumer, ghost it out of the bin, and link it."""
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        if kind == "video":
            spec = self._video_elements()
        else:
            spec = [("queue", {}), ("audioconvert", {}), ("audioresample", {})]
        els = []
        for name, props in spec:
            el = Gst.ElementFactory.make(name)
            for k, v in props.items():
                el.set_property(k, v)
            els.append(el)
        for el in els:
            g.bin.add(el)
        for a, c in zip(els, els[1:]):
            a.link(c)
        ghost = Gst.GhostPad.new(kind, els[-1].get_static_pad("src"))
        ghost.set_active(True)
        g.bin.add_pad(ghost)
        g.pads[kind] = ghost

        if kind == "video":
            self._attach_video(layer, ghost)
        else:
            mpad = self.amixer.request_pad_simple("sink_%u")
            mpad.set_property("volume", 0.0)
            g.bindings["volume"] = self._bind(mpad, "volume")
            g.mix_pads[kind] = mpad
            ghost.link(mpad)
        g.probes.append((ghost, ghost.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM,
                                                self._on_event_probe, layer, kind)))
        g.block_probes[kind] = ghost.add_probe(
            Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER, self._on_first_buffer, layer, kind)
        # Only now let data in, so the first buffer can't slip past the probe.
        for el in els:
            el.sync_state_with_parent()
        pad.link(els[0].get_static_pad("sink"))

    @staticmethod
    def _bind(pad: Gst.Pad, prop: str) -> GstController.InterpolationControlSource:
        cs = GstController.InterpolationControlSource.new()
        cs.set_property("mode", GstController.InterpolationMode.LINEAR)
        pad.add_control_binding(GstController.DirectControlBinding.new_absolute(pad, prop, cs))
        return cs

    def _on_event_probe(self, pad, info, layer: Layer, kind: str) -> Gst.PadProbeReturn:
        ev = info.get_event()
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        t = ev.type
        if t == Gst.EventType.SEGMENT:
            g.segments[kind] = ev.parse_segment()
        elif t == Gst.EventType.CAPS and kind == "video":
            self._apply_geometry(layer, ev.parse_caps())
        elif t == Gst.EventType.EOS and kind == "video":
            self._post(self.emit, "eos", layer, None)
        return Gst.PadProbeReturn.OK

    def _apply_geometry(self, layer: Layer, caps: Gst.Caps) -> None:
        with _structure(caps) as s:
            ok_w, vw = s.get_int("width")
            ok_h, vh = s.get_int("height")
            ok, pn, pd = s.get_fraction("pixel-aspect-ratio")
        if not (ok_w and ok_h):
            return
        dw = vw * pn / pd if ok and pd else vw
        w, h = self.render_size
        self._place(layer, fit_rect(dw, vh, w, h, layer.source.fit))

    def _place(self, layer: Layer, rect: tuple[int, int, int, int]) -> None:
        """Position the layer's mixer pad: fitted rect plus the user offset."""
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        g.rect = rect
        x, y, rw, rh = rect
        dx, dy = layer.offset
        placed = (x + dx, y + dy, rw, rh)
        mpad = g.mix_pads.get("video")
        if mpad:
            for k, v in zip(("xpos", "ypos", "width", "height"), placed):
                mpad.set_property(k, v)
        layer.full_frame = covers(placed, *self.render_size)

    def set_position(self, layer: Layer, dx: int, dy: int) -> None:
        layer.offset = (dx, dy)
        g: _LayerGst | None = getattr(layer, "g", None)
        if g:
            self._place(layer, g.rect or (0, 0, *self.render_size))

    def _on_first_buffer(self, pad, info, layer: Layer, kind: str) -> Gst.PadProbeReturn:
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        with g.lock:
            if kind in g.blocked:
                return Gst.PadProbeReturn.OK
            g.blocked.add(kind)
            if kind == "video":
                buf = info.get_buffer()
                seg = g.segments.get("video")
                rt = seg.to_running_time(Gst.Format.TIME, buf.pts) if seg and buf.pts != Gst.CLOCK_TIME_NONE else 0
                g.rt0 = rt if rt != Gst.CLOCK_TIME_NONE else 0
        self._check_ready(layer)
        return Gst.PadProbeReturn.OK  # stay blocked until start_layer()

    def _check_ready(self, layer: Layer) -> None:
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        with g.lock:
            if g.ready_sent or not g.no_more_pads or "video" not in g.blocked:
                return
            if any(k not in g.blocked for k in g.pads):
                return
            g.ready_sent = True
        self._post(self._layer_ready, layer)

    def _layer_ready(self, layer: Layer) -> None:
        if layer.state != "loading":
            return
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        if layer.source.kind == "video":
            ok, dur = g.bin.query_duration(Gst.Format.TIME)
            if ok and dur > 0:
                layer.duration = dur / SEC
        layer.state = "ready"
        self.emit("ready", layer, None)

    def start_layer(self, layer: Layer, at: float) -> None:
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        offset = int(at * SEC) - (g.rt0 or 0)
        for kind, pad in g.pads.items():
            pad.set_offset(offset)
            pid = g.block_probes.pop(kind, None)
            if pid:
                pad.remove_probe(pid)
        layer.start_time = at
        layer.state = "playing"

    def pause_layer(self, layer: Layer) -> None:
        g: _LayerGst | None = getattr(layer, "g", None)
        if not g:
            return
        for kind, pad in g.pads.items():
            if kind not in g.block_probes:
                g.block_probes[kind] = pad.add_probe(
                    Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER,
                    lambda *a: Gst.PadProbeReturn.OK)

    def resume_layer(self, layer: Layer, shift: float) -> None:
        g: _LayerGst | None = getattr(layer, "g", None)
        if not g:
            return
        for kind, pad in g.pads.items():
            pad.set_offset(pad.get_offset() + int(shift * SEC))
            pid = g.block_probes.pop(kind, None)
            if pid:
                pad.remove_probe(pid)

    # ------------------------------------------------------------ animation
    @staticmethod
    def _set_keyframes(cs, kfs: Keyframes) -> None:
        if cs is None:
            return
        cs.unset_all()
        for t, v in kfs:
            cs.set(int(max(0.0, t) * SEC), float(v))

    def set_alpha(self, layer: Layer, kfs: Keyframes) -> None:
        g = getattr(layer, "g", None)
        if g:
            self._set_keyframes(g.bindings.get("alpha"), kfs)

    def set_volume(self, layer: Layer, kfs: Keyframes) -> None:
        g = getattr(layer, "g", None)
        if g:
            self._set_keyframes(g.bindings.get("volume"), kfs)

    def dip(self, color: str, kfs: Keyframes) -> None:
        if self.pipeline:
            self.pipeline.get_by_name("dip").set_property("foreground-color", _color_argb(color))
        self._set_keyframes(self._dip_cs, kfs)

    def set_background(self, color: str) -> None:
        if self.pipeline:
            self.pipeline.get_by_name("bg").set_property("foreground-color", _color_argb(color))

    def set_master_volume(self, volume: float) -> None:
        el = self.pipeline.get_by_name("master") if self.pipeline else None
        if el:
            el.set_property("volume", max(0.0, min(1.0, volume / 100)))

    # -------------------------------------------------------------- removal
    def remove_layer(self, layer: Layer) -> None:
        self.layers.pop(layer.id, None)
        g: _LayerGst | None = getattr(layer, "g", None)
        layer.state = "removed"
        if not g or not g.bin:
            return
        # Swallow everything the layer still produces, then release blocked
        # streaming threads so they hit the drop probe and wind down.
        for kind, pad in g.pads.items():
            g.probes.append((pad, pad.add_probe(Gst.PadProbeType.DATA_DOWNSTREAM,
                                                lambda *a: Gst.PadProbeReturn.DROP)))
            pid = g.block_probes.pop(kind, None)
            if pid:
                pad.remove_probe(pid)
        self.teardown.submit(self._teardown, g, self.pipeline,
                             {"video": self.mixer, "audio": self.amixer})

    @staticmethod
    def _teardown(g: _LayerGst, pipe, mixers) -> None:
        try:
            for el in g.extra_elements:  # consumers living outside the bin
                el.set_state(Gst.State.NULL)
                if pipe:
                    pipe.remove(el)
            # Releasing the mixer pad first sets it flushing, which wakes any
            # streaming thread waiting inside the aggregator; then the bin
            # can shut down without stalling.
            for kind, mpad in g.mix_pads.items():
                for prop in ("alpha", "volume"):
                    binding = mpad.get_control_binding(prop)
                    if binding:
                        mpad.remove_control_binding(binding)
                mixers[kind].release_request_pad(mpad)
            g.bin.set_state(Gst.State.NULL)
            if pipe:
                pipe.remove(g.bin)
        except Exception:  # noqa: BLE001
            log.exception("layer teardown failed")
        finally:
            GstBackend._release_refs(g)

    @staticmethod
    def _release_refs(g: _LayerGst) -> None:
        """Drop every reference between the layer and its GStreamer objects."""
        for obj, hid in g.handlers:
            try:
                obj.disconnect(hid)
            except Exception:  # noqa: BLE001
                pass
        for pad, pid in g.probes:
            pad.remove_probe(pid)
        for kind, pid in g.block_probes.items():
            pad = g.pads.get(kind if kind in g.pads else "video")
            if pad is not None:
                pad.remove_probe(pid)
        g.handlers.clear()
        g.probes.clear()
        g.block_probes.clear()
        g.pads.clear()
        g.mix_pads.clear()
        g.extra_elements.clear()
        g.bindings.clear()
        g.segments.clear()
        g.bin = None

    # ------------------------------------------------------------- preview
    def _on_preview_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        self.pipeline and self.pipeline.get_by_name("pvalve").set_property("drop", True)
        if sample:
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                data = bytes(info.data)
                buf.unmap(info)
                self._post(self._deliver_snapshot, data)
        return Gst.FlowReturn.OK

    def _deliver_snapshot(self, data: bytes) -> None:
        waiters, self._snap_waiters = self._snap_waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(data)

    async def snapshot(self) -> bytes | None:
        if not self.pipeline:
            return None
        fut = asyncio.get_running_loop().create_future()
        self._snap_waiters.append(fut)
        self.pipeline.get_by_name("pvalve").set_property("drop", False)
        try:
            return await asyncio.wait_for(fut, 3.0)
        except asyncio.TimeoutError:
            return None

    # ---------------------------------------------------------------- info
    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "sink": self.sink_mode,
            "render_size": list(self.render_size),
            "fps": self.fps,
            "display": {"connector": self.output["connector"],
                        "mode": self.output["preferred_mode"],
                        "connected": self.output["connected"]} if self.output else None,
            "audio_device": self.audio_device,
            "frames_rendered": self.rendered,
            "frames_dropped": self.dropped,
            "layers": len(self.layers),
        }
