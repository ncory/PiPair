"""HTTP API smoke tests (simulated renderer)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from pipair.backend_sim import SimBackend
from pipair.config import Store
from pipair.engine import Engine
from pipair.media import MediaLibrary
from pipair.web import build_app

HW = {"model": None, "family": None, "is_pi": False, "memory_mb": None}


@pytest.fixture
async def client(tmp_path):
    (tmp_path / "media").mkdir()
    Image.new("RGB", (320, 180), "red").save(tmp_path / "media" / "red.png")
    store = Store(tmp_path / "state.json")
    lib = MediaLibrary(tmp_path / "media", tmp_path / "cache", HW)
    eng = Engine(store, lib, lambda s, h, e: SimBackend(s, h, e, load_delay=0.02), HW)
    app = build_app(store, lib, eng, HW)
    async with TestClient(TestServer(app)) as c:
        await eng.start(autoplay=False)
        yield c


async def test_playlist_crud_and_transport(client):
    r = await client.post("/api/playlists", json={
        "id": "lobby", "name": "Lobby", "items": [{"media": "red.png", "duration": 5}],
        "transition": "dissolve"})
    assert r.status == 201
    body = await r.json()
    assert body["items"][0]["kind"] == "image" and not body["items"][0]["missing"]

    r = await client.post("/api/playlists", json={"id": "lobby"})
    assert r.status == 400  # duplicate

    r = await client.patch("/api/playlists/lobby", json={"name": "Lobby 2"})
    assert (await r.json())["name"] == "Lobby 2"

    r = await client.get("/api/playlists/lobby/play?transition=dip&duration=0.2&color=%23ffffff")
    assert r.status == 200
    for _ in range(50):
        st = await (await client.get("/api/status")).json()
        if st["state"] == "playing" and st["item"]:
            break
        await asyncio.sleep(0.02)
    assert st["item"]["media"] == "red.png"

    assert (await (await client.post("/api/pause")).json())["state"] == "paused"
    assert (await (await client.post("/api/toggle")).json())["state"] == "playing"
    assert (await (await client.post("/api/stop", json={"transition": "cut"})).json())["state"] == "stopped"

    r = await client.post("/api/play", json={"playlist": "nope"})
    assert r.status == 404
    r = await client.post("/api/next", json={"transition": {"type": "wipe"}})
    assert r.status == 400

    r = await client.delete("/api/playlists/lobby")
    assert r.status == 200


async def test_media_upload_and_delete(client, tmp_path):
    data = aiohttp.FormData()
    data.add_field("file", b"\x89PNG fake", filename="../evil name.png")
    r = await client.post("/api/media", data=data)
    assert r.status == 201
    name = (await r.json())["uploaded"][0]
    assert name == "evil name.png"

    await client.post("/api/playlists", json={"id": "p", "items": ["red.png"]})
    r = await client.delete("/api/media/red.png")
    assert r.status == 409
    r = await client.delete("/api/media/red.png?force=1")
    assert (await r.json())["removed_from"] == ["p"]
    pl = await (await client.get("/api/playlists/p")).json()
    assert pl["items"] == []

    data = aiohttp.FormData()
    data.add_field("file", b"MZ", filename="virus.exe")
    assert (await client.post("/api/media", data=data)).status == 400


async def test_settings_and_system(client):
    r = await client.patch("/api/settings", json={"background_color": "#112233", "audio": {"volume": 40}})
    s = await r.json()
    assert s["background_color"] == "#112233" and s["audio"]["volume"] == 40
    assert s["audio"]["enabled"] is True  # nested merge kept other keys
    assert (await client.patch("/api/settings", json={"output": {"fps": 500}})).status == 400
    sysinfo = await (await client.get("/api/system")).json()
    assert sysinfo["renderer"]["backend"] == "sim"
    assert (await client.get("/api/preview.jpg")).status == 200
    doc = await (await client.get("/api/docs")).text()
    assert doc.startswith("# PiPair HTTP API")


async def test_websocket_pushes_status(client):
    ws = await client.ws_connect("/api/ws")
    msg = await ws.receive_json(timeout=2)
    assert msg["type"] == "status"
    await client.post("/api/playlists", json={"id": "w", "items": ["red.png"]})
    types = set()
    for _ in range(5):
        types.add((await ws.receive_json(timeout=2))["type"])
        if "playlist" in types:
            break
    assert "playlist" in types
    await ws.close()
