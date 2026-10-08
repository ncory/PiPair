"""Controller + head, joined by the loopback link, both on simulated renderers.

The head's clock is deliberately far from the controller's, so anything that
forgets to translate a time shows up as a layer starting minutes off.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from pipair import backend_sim
from pipair.backend_pair import HeadPort, PairBackend
from pipair.backend_sim import SimBackend
from pipair.config import Store
from pipair.engine import Engine
from pipair.head import Head
from pipair.link import LoopbackLink
from pipair.media import MediaLibrary

HW = {"model": None, "family": None, "is_pi": False, "memory_mb": None}
CUT = {"type": "cut"}
SKEW = 4321.0  # head clock - controller clock (s)


@pytest.fixture(autouse=True)
def short_videos(monkeypatch):
    monkeypatch.setattr(backend_sim, "DEFAULT_VIDEO_SECONDS", 1.0)


class Rig:
    """A controller engine and a head, each with its own data folder."""

    def __init__(self, tmp: Path, head_files=("a.mp4", "b.mp4"), ctrl_files=("a.mp4", "b.mp4")):
        self.tmp = tmp
        for side, names in (("ctrl", ctrl_files), ("head", head_files)):
            (tmp / side / "media").mkdir(parents=True)
            for n in names:
                (tmp / side / "media" / n).write_bytes(b"not really a video")
        self.link, self.head_link = LoopbackLink.pair(delay=0.002)
        self.head_link.clock = lambda: time.monotonic() + SKEW
        self.head_backends: list[SimBackend] = []
        self.audio_backends: list[SimBackend] = []
        self.store = Store(tmp / "ctrl" / "state.json")
        self.lib = MediaLibrary(tmp / "ctrl" / "media", tmp / "ctrl" / "cache", HW)
        self.port = HeadPort(self.link)
        self.head = Head(Store(tmp / "head" / "state.json"),
                         MediaLibrary(tmp / "head" / "media", tmp / "head" / "cache", HW),
                         self._head_backend, HW, self.head_link)
        self.engine = Engine(self.store, self.lib, self._pair_backend, HW)

    def _head_backend(self, settings, hw, emit):
        b = SimBackend(settings, hw, emit, load_delay=0.03)
        self.head_backends.append(b)
        return b

    def _audio_backend(self, settings, hw, emit):
        b = SimBackend(settings, hw, emit, load_delay=0.03)
        self.audio_backends.append(b)
        return b

    def _pair_backend(self, settings, hw, emit):
        return PairBackend(settings, hw, emit, self._audio_backend, self.port)

    async def start(self, playlist: list[str], end: str = "loop", link: bool = True) -> None:
        self.store.replace_playlist("default", {
            "id": "default", "name": "Default", "items": [{"media": n} for n in playlist],
            "end_action": {"type": end}})
        self.store.update_settings({"default_transition": CUT})
        await self.engine.start()
        await self.head.start()
        if link:
            await self.port.start()

    async def stop(self) -> None:
        await self.engine.shutdown()
        await self.port.stop()
        await self.head.shutdown()

    @property
    def head_sim(self) -> SimBackend:
        return self.head_backends[-1]

    @property
    def audio_sim(self) -> SimBackend:
        return self.audio_backends[-1]

    def head_playing(self) -> list:
        return [lyr for lyr, _a in self.head_sim.visible()]

    def start_times(self) -> tuple[float, float]:
        """When the current item starts: (controller's audio, head's picture), both on
        the controller's clock."""
        cur = self.engine.current
        head_layer = self.head.layers[cur.id]
        audio = self.audio_sim.t0 + cur.start_time
        picture = self.head_sim.t0 + head_layer.start_time
        return audio, picture


async def wait_for(pred, timeout=6.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def synced_and_playing(rig: Rig):
    def check() -> bool:
        cur = rig.engine.current
        return bool(rig.port.ready and rig.engine.state == "playing" and cur
                    and cur.id in rig.head.layers and rig.head_playing())
    return check


async def test_head_starts_the_picture_when_the_controller_starts_the_sound(tmp_path):
    rig = Rig(tmp_path)
    await rig.start(["a.mp4"])
    try:
        await wait_for(synced_and_playing(rig))
        audio, picture = rig.start_times()
        assert abs(audio - picture) < 0.02  # despite a 72-minute clock difference
        assert rig.engine.status()["output"]["link"]["ready"]
    finally:
        await rig.stop()


async def test_picture_offset_setting_shifts_the_head(tmp_path):
    rig = Rig(tmp_path)
    await rig.start(["a.mp4"])
    try:
        await wait_for(synced_and_playing(rig))
        rig.store.update_settings({"link": {"av_offset_ms": 150}})  # restarts the item
        await wait_for(lambda: rig.engine.backend.av_offset == 0.15 and synced_and_playing(rig)())
        audio, picture = rig.start_times()
        assert abs((picture - audio) - 0.150) < 0.02
    finally:
        await rig.stop()


async def test_items_advance_together(tmp_path):
    rig = Rig(tmp_path)
    await rig.start(["a.mp4", "b.mp4"])
    try:
        await wait_for(lambda: synced_and_playing(rig)()
                       and rig.engine.status()["item"]["media"] == "b.mp4")
        await wait_for(lambda: [lyr.source.media for lyr in rig.head_playing()] == ["b.mp4"])
        audio, picture = rig.start_times()
        assert abs(audio - picture) < 0.02
    finally:
        await rig.stop()


async def test_controller_plays_alone_then_resyncs_when_the_head_appears(tmp_path):
    rig = Rig(tmp_path)
    await rig.start(["a.mp4"], link=False)
    try:
        await wait_for(lambda: rig.engine.state == "playing")
        assert not rig.port.ready and not rig.head.layers
        first_backend = rig.engine.backend
        await rig.port.start()
        await wait_for(synced_and_playing(rig))
        assert rig.engine.backend is not first_backend  # restarted to line both up
        audio, picture = rig.start_times()
        assert abs(audio - picture) < 0.02
        assert rig.engine.last_error is None  # a head arriving is not a fault
    finally:
        await rig.stop()


async def test_link_loss_keeps_sound_going_and_the_head_loops_on_its_own(tmp_path):
    rig = Rig(tmp_path)
    await rig.start(["a.mp4", "b.mp4"])
    try:
        await wait_for(synced_and_playing(rig))
        rig.link.set_up(False)
        await wait_for(lambda: not rig.port.ready)
        shown = rig.head_playing()[0].source.media
        # The controller carries on through the playlist with sound alone...
        await wait_for(lambda: rig.engine.status()["item"]["media"] != shown)
        assert rig.engine.state == "playing"
        # ...and the head, told nothing, loops the clip it had.
        await wait_for(lambda: rig.head.top is not None and rig.head.top.id not in rig.head.ids)
        assert [lyr.source.media for lyr in rig.head_playing()][-1] == shown
        # Back together: one picture, started with the sound.
        rig.link.set_up(True)
        await wait_for(synced_and_playing(rig))
        await wait_for(lambda: len(rig.head_playing()) == 1)
        audio, picture = rig.start_times()
        assert abs(audio - picture) < 0.02
    finally:
        await rig.stop()


async def test_file_missing_on_the_head_is_reported_and_skipped(tmp_path):
    rig = Rig(tmp_path, head_files=("b.mp4",))
    await rig.start(["a.mp4", "b.mp4"])
    try:
        await wait_for(lambda: synced_and_playing(rig)()
                       and rig.engine.status()["item"]["media"] == "b.mp4")
        assert "a.mp4" in (rig.engine.last_error or "") and "head" in rig.engine.last_error
        assert rig.port.media == {"b.mp4": len(b"not really a video")}
    finally:
        await rig.stop()


async def test_pause_and_resume_reach_the_head(tmp_path):
    rig = Rig(tmp_path)
    await rig.start(["a.mp4"])
    try:
        await wait_for(synced_and_playing(rig))
        calls = []
        sim = rig.head_sim
        sim.pause_layer = lambda layer: calls.append("pause")
        sim.resume_layer = lambda layer, shift: calls.append(("resume", round(shift, 1)))
        rig.engine.pause()
        await asyncio.sleep(0.3)
        rig.engine.resume()
        await wait_for(lambda: len(calls) == 2)
        assert calls[0] == "pause" and calls[1][0] == "resume" and calls[1][1] >= 0.3
    finally:
        await rig.stop()


async def test_output_settings_reach_the_head_and_survive_its_restart(tmp_path):
    rig = Rig(tmp_path)
    await rig.start(["a.mp4"])
    try:
        await wait_for(synced_and_playing(rig))
        rig.store.update_settings({"output": {"render_size": "1280x720"}})
        await wait_for(lambda: rig.head.store.settings["output"]["render_size"] == "1280x720"
                       and len(rig.head_backends) == 2)
        await wait_for(synced_and_playing(rig))
        assert rig.engine.backend.render_size == (1280, 720)
        assert rig.engine.status()["output"]["render_size"] == [1280, 720]
    finally:
        await rig.stop()


async def test_image_items_are_fitted_on_the_head(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    rig = Rig(tmp_path)
    for side in ("ctrl", "head"):
        Image.new("RGB", (64, 36), "red").save(tmp_path / side / "media" / "red.png")
    rig.store.update_settings({"default_image_duration": 0.6})
    await rig.start(["red.png", "a.mp4"])
    try:
        await wait_for(lambda: rig.port.ready and rig.head_backends
                       and [lyr.source.kind for lyr in rig.head_playing()] == ["image"])
        layer = rig.head_playing()[0]
        assert layer.source.path.parent.name == "images"  # the head's own rendered copy
        assert rig.tmp / "head" in layer.source.path.parents
    finally:
        await rig.stop()


async def test_api_reports_the_link_and_files_the_head_lacks(tmp_path):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from pipair.web import build_app

    rig = Rig(tmp_path, head_files=("b.mp4",))
    app = build_app(rig.store, rig.lib, rig.engine, HW, None, rig.port)
    async with TestClient(TestServer(app)) as client:
        await rig.start(["a.mp4", "b.mp4"])
        try:
            await wait_for(lambda: rig.port.ready and rig.port.media is not None)
            system = await (await client.get("/api/system")).json()
            assert system["link"]["ready"] and system["link"]["head"]["media_files"] == 1
            assert system["outputs"] == []  # the display is the head's
            media = {m["name"]: m for m in await (await client.get("/api/media")).json()}
            assert "Not on the head" in media["a.mp4"]["warnings"][0]
            assert not any("head" in w for w in media["b.mp4"]["warnings"])
            pl = await (await client.get("/api/playlists/default")).json()
            assert [bool(it["head_warning"]) for it in pl["items"]] == [True, False]
            r = await client.patch("/api/settings", json={"link": {"av_offset_ms": 5000}})
            assert r.status == 400
            # the head gets the file: the warning clears without a reload of anything
            (tmp_path / "head" / "media" / "a.mp4").write_bytes(b"not really a video")
            rig.head._media = None  # don't wait out the head's poll interval
            rig.head.link.send({"t": "media", "files": rig.head._list_media()})
            await wait_for(lambda: "a.mp4" in rig.port.media)
            pl = await (await client.get("/api/playlists/default")).json()
            assert not any(it["head_warning"] for it in pl["items"])
        finally:
            await rig.port.stop()
            await rig.head.shutdown()
