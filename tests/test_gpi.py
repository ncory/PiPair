"""GPI triggers, with a fake GPIO driver."""

from __future__ import annotations

import asyncio
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from pipair.backend_sim import SimBackend
from pipair.config import Store, ValidationError
from pipair.engine import Engine
from pipair.gpi import GpiManager
from pipair.media import MediaLibrary
from pipair.web import build_app

HW = {"model": None, "family": None, "is_pi": False, "memory_mb": None}


class FakeLine:
    def __init__(self, pin: int):
        self.pin = pin
        self.fd, self._w = os.pipe()
        self.pending: list[str] = []
        self.active = False
        self.closed = False

    def edge(self, what: str) -> None:
        self.active = what == "close"
        self.pending.append(what)
        os.write(self._w, b"x")

    def read_events(self) -> list[str]:
        os.read(self.fd, 4096)
        out, self.pending = self.pending, []
        return out

    def is_active(self) -> bool:
        return self.active

    def close(self) -> None:
        self.closed = True
        os.close(self.fd)
        os.close(self._w)


class FakeDriver:
    chip = "/dev/gpiochip-fake"
    busy = {13}

    def __init__(self):
        self.lines: dict[int, FakeLine] = {}
        self.opened: list[tuple] = []

    def open(self, pin, pull, active_low, debounce_ms):
        if pin in self.busy:
            raise OSError("Device or resource busy")
        self.opened.append((pin, pull, active_low, debounce_ms))
        self.lines[pin] = FakeLine(pin)
        return self.lines[pin]


@pytest.fixture
async def rig(tmp_path):
    (tmp_path / "media").mkdir()
    for n, c in (("a.png", "red"), ("b.png", "blue")):
        Image.new("RGB", (64, 36), c).save(tmp_path / "media" / n)
    store = Store(tmp_path / "state.json")
    store.create_playlist({"id": "alpha", "items": [{"media": "a.png", "duration": 30}]})
    store.create_playlist({"id": "beta", "items": [{"media": "b.png", "duration": 30}]})
    store.update_settings({"default_playlist": None, "default_transition": {"type": "cut"}})
    lib = MediaLibrary(tmp_path / "media", tmp_path / "cache", HW)
    eng = Engine(store, lib, lambda s, h, e: SimBackend(s, h, e, load_delay=0.02), HW)
    await eng.start(autoplay=False)
    driver = FakeDriver()
    gpi = GpiManager(store, eng, driver_factory=lambda: driver)
    await gpi.start()
    yield store, eng, gpi, driver
    gpi.stop()
    await eng.shutdown()


def inputs(*specs):
    return {"gpi": {"inputs": list(specs)}}


async def settle(pred, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < end, "condition not reached"
        await asyncio.sleep(0.02)


async def test_close_fires_play(rig):
    store, eng, gpi, driver = rig
    store.update_settings(inputs({"name": "Button", "pin": 17, "action": {"type": "play", "playlist": "beta"}}))
    assert driver.opened == [(17, "up", True, 20)]  # pull-up, active low by default
    driver.lines[17].edge("open")  # releasing doesn't fire a "close" input
    await asyncio.sleep(0.1)
    assert eng.target is None
    driver.lines[17].edge("close")
    await settle(lambda: eng.target and eng.target.playlist == "beta")
    st = gpi.state()["inputs"][0]
    assert st["count"] == 1 and st["state"] == "closed" and st["header_pin"] == 11


async def test_holdoff_and_both_edges(rig):
    store, eng, gpi, driver = rig
    store.update_settings(inputs(
        {"id": "c", "pin": 5, "fire_on": "close", "holdoff_ms": 500, "action": {"type": "play", "playlist": "alpha"}},
        {"id": "o", "pin": 5, "fire_on": "open", "holdoff_ms": 0, "action": {"type": "play", "playlist": "beta"}},
    ))
    line = driver.lines[5]
    line.edge("close")
    line.edge("close")  # chatter inside the hold-off: ignored
    await settle(lambda: eng.target and eng.target.playlist == "alpha")
    assert gpi.stats["c"]["count"] == 1
    line.edge("open")
    await settle(lambda: eng.target and eng.target.playlist == "beta")
    assert len(driver.opened) == 1  # one line request serves both inputs


async def test_transport_actions(rig):
    store, eng, gpi, driver = rig
    await eng.play("alpha")
    await settle(lambda: eng.state == "playing" and eng.current)
    store.update_settings(inputs(
        {"id": "p", "pin": 22, "action": "toggle"},
        {"id": "l", "pin": 23, "action": {"type": "loop_item", "mode": "on"}},
        {"id": "s", "pin": 24, "action": {"type": "stop", "transition": "cut"}},
    ))
    driver.lines[22].edge("close")
    await settle(lambda: eng.state == "paused")
    driver.lines[23].edge("close")
    await settle(lambda: eng.loop_item)
    driver.lines[24].edge("close")
    await settle(lambda: eng.state == "stopped")


async def test_errors_are_reported(rig):
    store, eng, gpi, driver = rig
    store.update_settings(inputs(
        {"id": "x", "pin": 17, "action": {"type": "play", "playlist": "nope"}},
        {"id": "b", "pin": 13, "action": "next"},  # line busy
    ))
    gpi.fire("x")
    await settle(lambda: gpi.stats["x"]["last_error"])
    assert "nope" in gpi.stats["x"]["last_error"]
    st = {i["id"]: i for i in gpi.state()["inputs"]}
    assert "busy" in st["b"]["error"]
    with pytest.raises(KeyError):
        gpi.fire("missing")


async def test_reconfigure_and_rename(rig):
    store, eng, gpi, driver = rig
    store.update_settings(inputs({"id": "a", "pin": 17, "action": {"type": "play", "playlist": "alpha"}}))
    first = driver.lines[17]
    store.update_settings(inputs({"id": "a", "pin": 27, "action": {"type": "play", "playlist": "alpha"}}))
    assert first.closed and 27 in gpi.lines and 17 not in gpi.lines
    pl = store.get_playlist("alpha")
    store.replace_playlist("alpha", {**pl, "id": "renamed"})
    assert store.settings["gpi"]["inputs"][0]["action"]["playlist"] == "renamed"
    store.update_settings({"gpi": {"enabled": False}})
    assert not gpi.lines


def test_validation(tmp_path):
    store = Store(tmp_path / "s.json")
    with pytest.raises(ValidationError):
        store.update_settings(inputs({"pin": 40, "action": "next"}))
    with pytest.raises(ValidationError):
        store.update_settings(inputs({"pin": 4, "action": {"type": "explode"}}))
    with pytest.raises(ValidationError):  # same pin, different pull
        store.update_settings(inputs({"pin": 4, "action": "next"},
                                     {"pin": 4, "pull": "down", "action": "previous"}))
    s = store.update_settings(inputs({"pin": 4, "pull": "down", "action": "next"}))
    assert s["gpi"]["inputs"][0]["active_low"] is False  # pull-down => active high


async def test_api(rig):
    store, eng, gpi, driver = rig
    app = build_app(store, eng.library, eng, HW, gpi)
    async with TestClient(TestServer(app)) as c:
        r = await c.put("/api/gpi", json={"inputs": [
            {"id": "go", "name": "Go", "pin": 17, "action": {"type": "play", "playlist": "beta"}}]})
        body = await r.json()
        assert r.status == 200 and body["available"] and body["inputs"][0]["name"] == "Go"
        r = await c.post("/api/gpi/go/fire")
        assert (await r.json())["inputs"][0]["count"] == 1
        await settle(lambda: eng.target and eng.target.playlist == "beta")
        assert (await c.put("/api/gpi", json={"inputs": [{"pin": "x"}]})).status == 400
        assert (await c.post("/api/gpi/nope/fire")).status == 404
