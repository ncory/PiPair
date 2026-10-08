"""Engine behaviour against the simulated backend (real time, short media)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from pipair import backend_sim
from pipair.backend_sim import SimBackend
from pipair.config import Store, ValidationError
from pipair.engine import Engine
from pipair.media import MediaLibrary
from pipair.backend import NoDisplayError, classify_error

HW = {"model": None, "family": None, "is_pi": False, "memory_mb": None}
CUT = {"type": "cut"}


@pytest.fixture
def data(tmp_path: Path) -> Path:
    media = tmp_path / "media"
    media.mkdir()
    for name, color in (("red.png", "red"), ("green.png", "green"), ("blue.jpg", "blue")):
        Image.new("RGB", (64, 36), color).save(media / name)
    for name in ("clip.mp4", "clip2.mp4", "old-4x3.mp4"):
        (media / name).write_bytes(b"not really a video")
    return tmp_path


@pytest.fixture(autouse=True)
def short_videos(monkeypatch):
    monkeypatch.setattr(backend_sim, "DEFAULT_VIDEO_SECONDS", 1.0)


async def make_engine(data: Path, playlists: list[dict], default: str | None = None,
                      autoplay: bool = True, **settings) -> Engine:
    store = Store(data / "state.json")
    for pl in playlists:
        if pl["id"] in store.playlists:
            store.replace_playlist(pl["id"], pl)
        else:
            store.create_playlist(pl)
    factory = settings.pop("factory", None)
    store.update_settings({"default_playlist": default or playlists[0]["id"],
                           "default_transition": CUT, **settings})
    if factory is not None:
        settings["factory"] = factory
    lib = MediaLibrary(data / "media", data / "cache", HW)
    factory = settings.pop("factory", None) or (lambda s, h, e: SimBackend(s, h, e, load_delay=0.05))
    eng = Engine(store, lib, factory, HW)
    await eng.start(autoplay=autoplay)
    return eng


def items(*names, duration=0.5, **kw):
    return [{"media": n, "duration": duration, **kw} for n in names]


async def wait_for(pred, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def test_autoplay_and_loop(data):
    eng = await make_engine(data, [{"id": "default", "items": items("red.png", "green.png")}])
    await wait_for(lambda: eng.state == "playing" and eng.current is not None)
    assert eng.target.index == 0
    await wait_for(lambda: eng.target.index == 1)
    await wait_for(lambda: eng.target.index == 0)  # looped
    assert len(eng.backend.layers) <= 3
    await eng.shutdown()


async def test_dissolve_timing(data):
    eng = await make_engine(data, [{
        "id": "default", "items": items("red.png", "green.png", duration=1.0),
        "transition": {"type": "dissolve", "duration": 0.4}}])
    await wait_for(lambda: eng.current is not None)
    a = eng.current
    await wait_for(lambda: eng.current is not a, timeout=3)
    b = eng.current
    # B starts exactly when A has 0.4 s left
    assert b.start_time == pytest.approx(a.start_time + 0.6, abs=0.01)
    sim: SimBackend = eng.backend
    assert sim.alpha(b, b.start_time + 0.2) == pytest.approx(0.5, abs=0.01)
    assert sim.alpha(b, b.start_time + 0.5) == pytest.approx(1.0)
    # full-frame incoming picture: outgoing stays opaque underneath
    assert sim.alpha(a, b.start_time + 0.2) == pytest.approx(1.0)
    await wait_for(lambda: a.state == "removed", timeout=2)
    await eng.shutdown()


@pytest.mark.parametrize("incoming", [
    {"media": "old-4x3.mp4"},                                 # letterboxed
    {"media": "clip2.mp4", "offset_x": -5, "offset_y": 2},   # nudged a few pixels
])
async def test_dissolve_is_a_true_crossfade(data, incoming):
    """The outgoing picture never fades (no dip toward the background); it
    is hidden exactly when the incoming one is fully opaque."""
    eng = await make_engine(data, [{
        "id": "default", "items": [{"media": "clip.mp4"}, incoming],
        "transition": {"type": "dissolve", "duration": 0.4}}])
    await wait_for(lambda: eng.current is not None)
    a = eng.current
    await wait_for(lambda: eng.current is not a, timeout=3)
    b = eng.current
    assert not b.full_frame
    sim: SimBackend = eng.backend
    t0 = b.start_time
    for dt in (0.0, 0.1, 0.2, 0.39):
        assert sim.alpha(a, t0 + dt) == pytest.approx(1.0)
    assert sim.alpha(b, t0 + 0.2) == pytest.approx(0.5, abs=0.01)
    assert sim.alpha(a, t0 + 0.4) == pytest.approx(0.0)
    assert sim.alpha(b, t0 + 0.4) == pytest.approx(1.0)
    await eng.shutdown()


async def test_stop_with_dissolve_fades_out(data):
    eng = await make_engine(data, [{"id": "default", "items": items("red.png", duration=30)}])
    await wait_for(lambda: eng.current is not None)
    a = eng.current
    await eng.stop({"type": "dissolve", "duration": 0.4, "color": "#000000"})
    t0 = eng.transition_until - 0.4
    assert eng.backend.alpha(a, t0 + 0.2) == pytest.approx(0.5, abs=0.01)
    await eng.shutdown()


async def test_dip_through_color(data):
    eng = await make_engine(data, [{
        "id": "default", "items": items("red.png", "green.png", duration=1.0),
        "transition": {"type": "dip", "duration": 0.6, "color": "#ffffff"}}])
    await wait_for(lambda: eng.current is not None)
    a = eng.current
    await wait_for(lambda: eng.current is not a, timeout=3)
    b = eng.current
    sim: SimBackend = eng.backend
    t0 = a.start_time + 0.4  # A.end - 0.6
    assert b.start_time == pytest.approx(t0 + 0.3, abs=0.01)  # new item starts at the midpoint
    assert sim.dip_color == "#ffffff"
    assert [round(t - t0, 3) for t, _ in sim.dip_kfs] == [0.0, 0.3, 0.6]
    await eng.shutdown()


async def test_videos_play_to_their_duration(data):
    eng = await make_engine(data, [{"id": "default", "items": [
        {"media": "clip.mp4"}, {"media": "clip2.mp4"}]}])
    await wait_for(lambda: eng.current is not None)
    a = eng.current
    assert eng.duration(a) == 1.0
    await wait_for(lambda: eng.current is not a, timeout=3)
    assert eng.current.start_time == pytest.approx(a.start_time + 1.0, abs=0.01)
    await eng.shutdown()


async def test_end_stop(data):
    eng = await make_engine(data, [{"id": "default", "items": items("red.png", duration=1.0),
                                    "end_action": "stop"}])
    await wait_for(lambda: eng.state == "playing" and eng.current)
    layer = eng.current
    await wait_for(lambda: eng.state == "stopped", timeout=3)
    # the fade-out was scheduled for the item's end, not when it was requested
    assert eng.outgoing and eng.outgoing[0][1] >= layer.start_time + 1.0
    await wait_for(lambda: not eng.backend.layers, timeout=2)
    await eng.shutdown()


async def test_end_hold(data):
    eng = await make_engine(data, [{"id": "default", "items": items("red.png", duration=0.3),
                                    "end_action": "hold"}])
    await wait_for(lambda: eng.state == "held", timeout=3)
    assert eng.current is not None and eng.current.state == "playing"
    await eng.shutdown()


async def test_end_goto(data):
    eng = await make_engine(data, [
        {"id": "intro", "items": items("red.png", duration=0.3),
         "end_action": {"type": "goto", "playlist": "main"}},
        {"id": "main", "items": items("green.png", "blue.jpg", duration=0.3)},
    ], default="intro")
    await wait_for(lambda: eng.target and eng.target.playlist == "main", timeout=3)
    assert eng.status()["playlist"]["id"] == "main"
    await eng.shutdown()


async def test_missing_media_is_skipped(data):
    eng = await make_engine(data, [{"id": "default",
                                    "items": items("red.png", "gone.png", "green.png", duration=0.3)}])
    seen = set()
    for _ in range(100):
        if eng.current:
            seen.add(eng.current.source.media)
        if "green.png" in seen:
            break
        await asyncio.sleep(0.02)
    assert "green.png" in seen
    assert "gone.png" in (eng.last_error or "")
    await eng.shutdown()


async def test_all_missing_stops(data):
    eng = await make_engine(data, [{"id": "default", "items": items("a.png", "b.png")}],
                            autoplay=False)
    await eng.play("default")
    await wait_for(lambda: eng.state == "stopped", timeout=3)
    await asyncio.sleep(0.3)
    assert eng.state == "stopped"
    await eng.shutdown()


async def test_pause_freezes_position(data):
    eng = await make_engine(data, [{"id": "default", "items": items("red.png", "green.png",
                                                                     duration=1.0)}])
    await wait_for(lambda: eng.current is not None)
    await asyncio.sleep(0.3)
    eng.pause()
    layer = eng.current
    pos = layer.position(eng.backend.now())
    await asyncio.sleep(1.2)  # longer than the item: must not advance
    assert eng.current is layer and eng.state == "paused"
    assert layer.position(eng.backend.now()) == pytest.approx(pos, abs=0.01)
    eng.resume()
    await wait_for(lambda: eng.current is not layer, timeout=2)
    await eng.shutdown()


async def test_manual_transport(data):
    eng = await make_engine(data, [{"id": "default",
                                    "items": items("red.png", "green.png", "blue.jpg", duration=30)}])
    await wait_for(lambda: eng.current is not None)
    await eng.next()
    await wait_for(lambda: eng.target.index == 1)
    await eng.previous()
    await wait_for(lambda: eng.target.index == 0)
    await eng.previous()  # wraps
    await wait_for(lambda: eng.target.index == 2)
    await eng.play("default", 1, {"type": "dissolve", "duration": 0.2, "color": "#000000"})
    await wait_for(lambda: eng.target.index == 1)
    await eng.stop()
    assert eng.state == "stopped"
    await eng.play()  # resumes last playlist
    await wait_for(lambda: eng.state == "playing")
    await eng.shutdown()


async def test_rapid_commands_bound_layers(data):
    eng = await make_engine(data, [{"id": "default",
                                    "items": items("red.png", "green.png", "blue.jpg", duration=30),
                                    "transition": {"type": "dissolve", "duration": 1}}])
    await wait_for(lambda: eng.current is not None)
    for _ in range(10):
        await eng.next()
    await asyncio.sleep(0.3)
    live = [lyr for lyr in eng.backend.layers.values()]
    assert len(live) <= 3
    await eng.shutdown()


async def test_edit_current_playlist(data):
    eng = await make_engine(data, [{"id": "default",
                                    "items": items("red.png", "green.png", "blue.jpg", duration=0.6)}])
    await wait_for(lambda: eng.current is not None)
    pl = eng.store.get_playlist("default")
    # remove the playing item; playback continues with what is now at its spot
    eng.store.replace_playlist("default", {**pl, "items": pl["items"][1:]})
    await wait_for(lambda: eng.current and eng.current.source.media == "green.png", timeout=3)
    # rename the playlist while it plays
    eng.store.replace_playlist("default", {**eng.store.get_playlist("default"), "id": "renamed"})
    assert eng.target.playlist == "renamed"
    assert eng.store.settings["default_playlist"] == "renamed"
    await eng.shutdown()


def test_validation(tmp_path):
    store = Store(tmp_path / "state.json")
    with pytest.raises(ValidationError):
        store.create_playlist({"id": "Bad Id"})
    with pytest.raises(ValidationError):
        store.create_playlist({"id": "x", "items": [{"media": "a.exe"}]})
    with pytest.raises(ValidationError):
        store.create_playlist({"id": "x", "transition": {"type": "wipe"}})
    with pytest.raises(ValidationError):
        store.update_settings({"output": {"rotation": 45}})
    pl = store.create_playlist({"id": "x", "items": ["a.png"], "transition": "dip"})
    assert pl["transition"] == {"type": "dip", "duration": 1.0, "color": "#000000"}
    assert pl["items"][0]["uid"]
    # persisted
    assert "x" in Store(tmp_path / "state.json").playlists


async def test_live_position_offset(data):
    eng = await make_engine(data, [{"id": "default",
                                    "items": items("red.png", "green.png", duration=30)}])
    await wait_for(lambda: eng.current is not None)
    layer = eng.current
    uid = eng.target.uid
    assert layer.offset == (0, 0) and layer.full_frame
    eng.pause()
    eng.store.update_item("default", uid, {"offset_x": 40, "offset_y": -12})
    assert layer.offset == (40, -12)  # applied live, while paused
    assert not layer.full_frame  # moved: background shows at the edges
    assert eng.store.get_playlist("default")["items"][0]["offset_x"] == 40
    # a newly loaded layer for the item starts at the saved offset
    eng.resume()
    await eng.play("default", 0, CUT)
    await wait_for(lambda: eng.current is not layer)
    assert eng.current.offset == (40, -12)
    with pytest.raises(ValidationError):
        eng.store.update_item("default", uid, {"offset_x": "left"})
    await eng.shutdown()


async def test_loop_item(data):
    eng = await make_engine(data, [{"id": "default", "items": [
        {"media": "clip.mp4"}, {"media": "red.png", "duration": 0.3}]}])
    await wait_for(lambda: eng.current is not None)
    eng.set_loop_item(True)
    first = eng.current
    await wait_for(lambda: eng.current is not first, timeout=3)
    assert eng.target.index == 0 and eng.current.source.media == "clip.mp4"  # restarted
    eng.set_loop_item(False)
    await wait_for(lambda: eng.target.index == 1, timeout=3)
    eng.set_loop_item(True)
    img = eng.current
    await asyncio.sleep(0.8)  # well past the image's 0.3 s
    assert eng.current is img and eng.state == "playing"
    eng.set_loop_item(False)
    await wait_for(lambda: eng.current is not img, timeout=3)
    await eng.shutdown()


async def test_constrained_display_freezes_outgoing_video(data):
    """Where two videos can't be on screen at once, a video-to-video dissolve
    freezes the outgoing clip, then fades the new one in over the still."""
    from pipair import engine as engine_mod

    eng = await make_engine(data, [{
        "id": "default", "items": [{"media": "clip.mp4"}, {"media": "clip2.mp4"}, {"media": "red.png", "duration": 5}],
        "transition": {"type": "dissolve", "duration": 0.4}}], autoplay=False)
    eng.backend.video_overlap_ok = False
    await eng.play("default", 0, CUT)
    await wait_for(lambda: eng.current is not None)
    a = eng.current
    await wait_for(lambda: eng.current is not a, timeout=3)
    b = eng.current
    sim: SimBackend = eng.backend
    t_freeze = sim.frozen[a.id]
    assert t_freeze == pytest.approx(a.start_time + 0.6, abs=0.01)  # the usual dissolve start
    assert b.start_time == pytest.approx(t_freeze + engine_mod.FREEZE_LEAD, abs=0.001)
    assert sim.alpha(a, b.start_time + 0.39) == pytest.approx(1.0)  # still under the fade
    assert sim.alpha(b, b.start_time + 0.2) == pytest.approx(0.5, abs=0.01)
    assert sim.alpha(a, b.start_time + 0.4) == pytest.approx(0.0)
    # video -> image: no freeze needed
    await wait_for(lambda: eng.current is not b, timeout=3)
    assert b.id not in sim.frozen
    await eng.shutdown()


# ---------------------------------------------------------------- bus errors

def test_error_from_a_live_layer_fails_only_that_item():
    assert classify_error(5, True, "v4l2h264dec4", None) == "layer"


def test_error_from_a_removed_layer_is_stale_not_fatal():
    """The bug that took a player down: one decoder inside layer5 fails, the
    engine drops the layer, and layer5's demuxer then reports too. That second
    message must not restart the renderer -- it describes a layer that is gone."""
    assert classify_error(5, False, "qtdemux4", None) == "stale"


def test_error_outside_any_layer_is_fatal():
    assert classify_error(None, False, "mixer", None) == "fatal"


def test_audio_sink_error_is_reported_as_audio():
    assert classify_error(None, False, "asink", "default:CARD=vc4hdmi") == "audio"


def test_audio_sink_error_without_a_device_is_fatal():
    assert classify_error(None, False, "asink", None) == "fatal"


def test_a_layer_owns_its_error_even_when_named_like_the_audio_sink():
    """Attribution to a layer wins: a live layer is never mistaken for the
    pipeline's audio sink."""
    assert classify_error(2, True, "asink", "default:CARD=vc4hdmi") == "layer"


# ------------------------------------------------------------ no display

class _DisplayThatArrivesLate:
    """Backend factory whose first `fail_times` renderers refuse to start.

    Mimics a Pi whose monitor is off: the renderer cannot open a display,
    and a later attempt succeeds once one is plugged in.
    """

    def __init__(self, fail_times: int, exc: Exception | None = None):
        self.remaining = fail_times
        self.exc = exc if exc is not None else NoDisplayError("no connected display found")

    def __call__(self, settings, hw, emit):
        sim = SimBackend(settings, hw, emit, load_delay=0.05)
        if self.remaining > 0:
            self.remaining -= 1
            exc = self.exc

            async def refuse():
                raise exc

            sim.start = refuse
        return sim


async def test_no_display_is_reported_as_dormant_not_error(data):
    eng = await make_engine(data, [{"id": "a", "name": "A", "items": items("clip.mp4")}],
                            factory=_DisplayThatArrivesLate(99))
    await wait_for(lambda: eng.state == "dormant")
    st = eng.status()
    assert st["state"] == "dormant"
    # One field monitoring can watch, present even though the renderer is down.
    assert st["display_connected"] is False
    # the renderer object exists but never opened a display
    assert (st["output"] or {}).get("display") is None
    assert "no display" in (st["last_error"] or "").lower()
    await eng.stop()


async def test_dormant_player_resumes_when_a_display_appears(data):
    eng = await make_engine(data, [{"id": "a", "name": "A", "items": items("clip.mp4", "clip2.mp4")}],
                            factory=_DisplayThatArrivesLate(1))
    await wait_for(lambda: eng.state == "dormant")
    assert eng.status()["display_connected"] is False
    # the retry builds a renderer that starts, as when a monitor is switched on
    await wait_for(lambda: eng.state == "playing", timeout=10)
    assert eng.current is not None
    assert eng.status()["display_connected"] is not False
    await eng.stop()


async def test_a_real_start_failure_is_still_an_error(data):
    """Only a missing display is dormancy; anything else stays a fault."""
    eng = await make_engine(data, [{"id": "a", "name": "A", "items": items("clip.mp4")}],
                            factory=_DisplayThatArrivesLate(99, RuntimeError("card on fire")))
    await wait_for(lambda: eng.state == "error")
    assert eng.state == "error"
    assert eng.no_display is False
    # not False: we do not claim the display is gone when we do not know
    assert eng.status()["display_connected"] is not False
    await eng.stop()


async def test_last_error_carries_a_timestamp(data):
    """An error from startup should be distinguishable from a live fault, so
    the API stamps it rather than leaving a bare string that never clears."""
    eng = await make_engine(data, [{"id": "a", "name": "A", "items": items("clip.mp4")}],
                            factory=_DisplayThatArrivesLate(99))
    await wait_for(lambda: eng.state == "dormant")
    st = eng.status()
    assert st["last_error"]
    assert isinstance(st["last_error_at"], float)
    assert st["last_error_at"] <= __import__("time").time()
    await eng.stop()


async def test_no_error_means_no_timestamp(data):
    eng = await make_engine(data, [{"id": "a", "name": "A", "items": items("red.png")}])
    await wait_for(lambda: eng.state == "playing")
    st = eng.status()
    assert st["last_error"] is None
    assert st["last_error_at"] is None
    await eng.stop()
