"""Audio-only renderer for the controller.

Plays the sound of each item and nothing else:

    [layer: file ─ decodebin ─ audio ─ queue ─ convert ─┐
    [layer ...]                                         ├ audiomixer ─ volume ─ alsasink
    audiotestsrc (silence, keeps the pipeline live) ────┘

It reuses the layer machinery of GstBackend (preroll behind a blocking pad
probe, start at an exact running time, keyframed volume), so audio starts and
fades on the same timeline the head uses for the picture. The video stream of
a file is demuxed but never decoded: decodebin is told to leave it alone and
it goes to a fakesink. Image items have no sound and are ready at once.
"""

from __future__ import annotations

import logging
from typing import Any

from gi.repository import Gst

from .backend import Keyframes, Layer, Source
from .backend_gst import GstBackend, _LayerGst, _structure

log = logging.getLogger(__name__)


class AudioBackend(GstBackend):
    name = "audio"
    prefer_hdmi_audio = False  # the controller's speakers are on the jack or a DAC

    def __init__(self, settings: dict, hw: dict, emit):
        super().__init__(settings, hw, emit, sink="fake")  # no GL/GBM setup
        self.sink_mode = "audio"  # (not "fake": audio goes to a real device)

    def _build(self) -> Gst.Pipeline:
        audio = self._audio_desc()
        desc = audio or "audiotestsrc is-live=true wave=silence ! fakesink sync=true"
        log.info("pipeline: audio %s", self.audio_device if audio else "off")
        pipe = Gst.parse_launch(desc)
        if not isinstance(pipe, Gst.Pipeline):  # a single chain parses to a bin
            p = Gst.Pipeline.new()
            p.add(pipe)
            pipe = p
        self.mixer = None
        self.amixer = pipe.get_by_name("amix")
        return pipe

    # --------------------------------------------------------------- layers
    def create_layer(self, source: Source) -> Layer:
        if source.kind != "image":
            return super().create_layer(source)
        layer = Layer(source)
        self.layers[layer.id] = layer
        self._post(self._image_ready, layer)
        return layer

    def _image_ready(self, layer: Layer) -> None:
        if layer.state == "loading":
            layer.state = "ready"
            self.emit("ready", layer, None)

    def _build_layer(self, layer: Layer, b: Gst.Bin) -> None:
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        src = Gst.ElementFactory.make("filesrc")
        src.set_property("location", str(layer.source.path))
        dec = Gst.ElementFactory.make("decodebin")
        b.add(src)
        b.add(dec)
        src.link(dec)
        g.handlers.append((dec, dec.connect("autoplug-continue", self._autoplug_continue)))
        g.handlers.append((dec, dec.connect("pad-added", self._on_pad_added, layer)))
        g.handlers.append((dec, dec.connect("no-more-pads", self._on_no_more_pads, layer)))

    @staticmethod
    def _autoplug_continue(dec, pad, caps) -> bool:
        """Stop decodebin at encoded video: expose the stream instead of decoding it."""
        el = pad.get_parent_element()
        factory = el.get_factory() if el else None
        if factory is None or factory.get_name() == "typefind":
            return True  # the file's own type (video/quicktime...), not a stream
        if not caps or not caps.get_size():
            return True
        with _structure(caps) as st:
            name = st.get_name()
        return not name.startswith(("video/", "image/"))

    def _on_pad_added(self, dec, pad, layer: Layer) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        kind = ""
        if caps and caps.get_size():
            with _structure(caps) as st:
                kind = st.get_name()
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        with g.lock:
            if kind == "audio/x-raw" and "audio" not in g.pads and self.amixer is not None:
                self._add_output(layer, "audio", pad)
                return
        # video (left encoded), extra audio streams, subtitles
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
        self._check_ready(layer)

    def _check_ready(self, layer: Layer) -> None:
        """Ready once every stream we play is prerolled (a silent file: at once)."""
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        with g.lock:
            if g.ready_sent or not g.no_more_pads:
                return
            if any(k not in g.blocked for k in g.pads):
                return
            g.ready_sent = True
        self._post(self._layer_ready, layer)

    def _on_event_probe(self, pad, info, layer: Layer, kind: str) -> Gst.PadProbeReturn:
        ev = info.get_event()
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        if ev.type == Gst.EventType.SEGMENT:
            g.segments[kind] = ev.parse_segment()
        elif ev.type == Gst.EventType.EOS:
            self._post(self.emit, "eos", layer, None)
        return Gst.PadProbeReturn.OK

    def start_layer(self, layer: Layer, at: float) -> None:
        if getattr(layer, "g", None) is None:  # an image: nothing to play
            layer.start_time = at
            layer.state = "playing"
            return
        # g.rt0 stays unset here, so stream time zero lands on `at`: the same
        # instant the head puts the first video frame on screen.
        super().start_layer(layer, at)

    # ------------------------------------------------- picture: not ours
    def set_alpha(self, layer: Layer, kfs: Keyframes) -> None:
        pass

    def dip(self, color: str, kfs: Keyframes) -> None:
        pass

    def set_background(self, color: str) -> None:
        pass

    def set_position(self, layer: Layer, dx: int, dy: int) -> None:
        layer.offset = (dx, dy)

    async def snapshot(self) -> bytes | None:
        return None

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "audio_device": self.audio_device,
                "layers": len(self.layers)}
