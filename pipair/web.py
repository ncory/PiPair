"""HTTP API + web UI (aiohttp). See docs/API.md for the reference."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import unquote
from typing import Any

from aiohttp import WSCloseCode, WSMsgType, web

from . import __version__
from . import hw as hwmod
from .config import Store, ValidationError, validate_transition
from .engine import Engine
from .gpi import GpiManager
from .media import MediaLibrary, media_kind

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
API_DOC = Path(__file__).parent.parent / "docs" / "API.md"
TICKER = web.AppKey("ticker", asyncio.Task)
PROBE = web.AppKey("probe", asyncio.Task)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def _err(status: int, msg: str) -> web.Response:
    return _json({"error": msg}, status)


@web.middleware
async def errors_mw(request: web.Request, handler):
    try:
        return await handler(request)
    except ValidationError as e:
        return _err(400, str(e))
    except KeyError as e:
        return _err(404, f"not found: {e.args[0] if e.args else ''}")
    except ValueError as e:
        return _err(400, str(e))
    except web.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("request failed: %s %s", request.method, request.path)
        return _err(500, str(e))


@web.middleware
async def cors_mw(request: web.Request, handler):
    # Trusted-LAN tool: let any web page / control system call the API.
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


class Api:
    def __init__(self, store: Store, library: MediaLibrary, engine: Engine, hw: dict,
                 gpi: GpiManager | None = None, port=None):
        self.store = store
        self.library = library
        self.engine = engine
        self.hw = hw
        self.gpi = gpi
        self.port = port  # backend_pair.HeadPort when there is a head, else None
        # pairing.ControllerLink when heads are paired with, else None
        self.pairing = port.link if port and hasattr(port.link, "confirm") else None
        if port:
            port.on_change(self._head_changed)
        if self.pairing:
            self.pairing.on_change(lambda: self.broadcast({"type": "link"}))
        if gpi:
            gpi.on_event(self.broadcast)
        self.sockets: set[web.WebSocketResponse] = set()
        self.started = time.time()
        engine.on_status(lambda st: self.broadcast({"type": "status", "status": st}))
        store.on_change(lambda what, _d: self.broadcast({"type": what}))

    # -------------------------------------------------------------- helpers
    async def params(self, request: web.Request) -> dict:
        """Merge query-string params and a JSON body (body wins)."""
        out: dict[str, Any] = dict(request.query)
        if request.body_exists and request.content_type == "application/json":
            try:
                body = await request.json()
            except json.JSONDecodeError:
                raise ValidationError("request body is not valid JSON") from None
            if not isinstance(body, dict):
                raise ValidationError("request body must be a JSON object")
            out.update(body)
        return out

    @staticmethod
    def transition_param(p: dict) -> dict | None:
        t = p.get("transition")
        if t is None:
            return None
        if isinstance(t, str):  # query-string form: ?transition=dip&duration=2&color=%23fff
            t = {"type": t}
            if "duration" in p:
                t["duration"] = p["duration"]
            if "color" in p:
                t["color"] = p["color"]
        return validate_transition(t)

    def broadcast(self, msg: dict) -> None:
        if not self.sockets:
            return
        data = json.dumps(msg, default=str)
        for ws in list(self.sockets):
            if ws.closed:
                self.sockets.discard(ws)
                continue
            asyncio.ensure_future(ws.send_str(data))

    def _head_changed(self) -> None:
        """The link went up or down, or the head's files changed."""
        self.broadcast({"type": "media"})
        self.broadcast({"type": "status", "status": self.engine.status()})
        self.broadcast({"type": "link"})

    def head_warning(self, name: str, size: int | None = None) -> str | None:
        """Why the head can't show this file, if we know it can't."""
        files = self.port.media if self.port else None
        if files is None:
            return None  # no head, or it has not told us yet
        if name not in files:
            return "Not on the head: copy this file to the head's media folder."
        if size is not None and files[name] != size:
            return "The head has a different version of this file (the sizes differ)."
        return None

    async def ticker(self) -> None:
        """Push position updates to UI clients while something is playing."""
        while True:
            await asyncio.sleep(1.0)
            if self.sockets and self.engine.state in ("playing", "paused", "held"):
                self.broadcast({"type": "status", "status": self.engine.status()})

    def playlist_out(self, pl: dict) -> dict:
        items = []
        for it in pl["items"]:
            e = dict(it)
            e["kind"] = media_kind(it["media"])
            e["missing"] = not self.library.exists(it["media"])
            e["head_warning"] = self.head_warning(it["media"])
            info = self.library.info(it["media"])
            e["media_duration"] = info.get("duration") if info else None
            items.append(e)
        return {**pl, "items": items}

    # ------------------------------------------------------------ transport
    async def status(self, request):
        return _json(self.engine.status())

    async def play(self, request):
        p = await self.params(request)
        pid = p.get("playlist")
        index = int(p.get("index", 0))
        await self.engine.play(pid, index, self.transition_param(p))
        return _json(self.engine.status())

    async def play_playlist(self, request):
        p = await self.params(request)
        await self.engine.play(request.match_info["id"], int(p.get("index", 0)),
                               self.transition_param(p))
        return _json(self.engine.status())

    async def pause(self, request):
        self.engine.pause()
        return _json(self.engine.status())

    async def resume(self, request):
        self.engine.resume()
        return _json(self.engine.status())

    async def toggle(self, request):
        self.engine.toggle_pause()
        return _json(self.engine.status())

    async def stop(self, request):
        p = await self.params(request)
        await self.engine.stop(self.transition_param(p))
        return _json(self.engine.status())

    async def next(self, request):
        p = await self.params(request)
        await self.engine.next(self.transition_param(p))
        return _json(self.engine.status())

    async def previous(self, request):
        p = await self.params(request)
        await self.engine.previous(self.transition_param(p))
        return _json(self.engine.status())

    # ------------------------------------------------------------ playlists
    async def list_playlists(self, request):
        return _json([self.playlist_out(pl) for pl in self.store.playlists.values()])

    async def get_playlist(self, request):
        pl = self.store.get_playlist(request.match_info["id"])
        if pl is None:
            raise KeyError(request.match_info["id"])
        return _json(self.playlist_out(pl))

    async def create_playlist(self, request):
        body = await self.params(request)
        return _json(self.playlist_out(self.store.create_playlist(body)), 201)

    async def put_playlist(self, request):
        body = await self.params(request)
        pl = self.store.replace_playlist(request.match_info["id"], body,
                                         partial=request.method == "PATCH")
        return _json(self.playlist_out(pl))

    async def patch_item(self, request):
        body = await self.params(request)
        self.store.update_item(request.match_info["id"], request.match_info["uid"], body)
        return _json(self.playlist_out(self.store.get_playlist(request.match_info["id"])))

    async def loop_item(self, request):
        p = await self.params(request)
        v = p.get("enabled", "toggle")
        if v == "toggle":
            enabled = not self.engine.loop_item
        else:
            enabled = v is True or str(v).lower() in ("1", "true", "yes", "on")
        self.engine.set_loop_item(enabled)
        return _json(self.engine.status())

    async def delete_playlist(self, request):
        self.store.delete_playlist(request.match_info["id"])
        return _json({"deleted": request.match_info["id"]})

    # ---------------------------------------------------------------- media
    async def list_media(self, request):
        items = self.library.list()
        for e in items:
            e["used_by"] = self.store.media_usage(e["name"])
            warn = self.head_warning(e["name"], e["size"])
            if warn:
                e["warnings"] = [warn, *e["warnings"]]
        return _json(items)

    async def get_media(self, request):
        name = request.match_info["name"]
        e = self.library.entry(name)
        if e is None:
            raise KeyError(name)
        if e["info"] is None:
            await self.library.probe(name)
            e = self.library.entry(name)
        e["used_by"] = self.store.media_usage(name)
        return _json(e)

    async def media_file(self, request):
        name = request.match_info["name"]
        if not self.library.exists(name):
            raise KeyError(name)
        return web.FileResponse(self.library.path(name))

    async def media_thumb(self, request):
        name = request.match_info["name"]
        if not self.library.exists(name):
            raise KeyError(name)
        path = await asyncio.to_thread(self.library.thumbnail, name)
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    async def upload_media(self, request):
        reader = await request.multipart()
        saved = []
        while True:
            part = await reader.next()
            if part is None:
                break
            if not part.filename:
                continue
            if media_kind(part.filename) is None:
                raise ValidationError(f"{part.filename}: unsupported file type")
            tmp = self.library.media_dir / f".upload-{secrets.token_hex(6)}"
            try:
                with open(tmp, "wb") as f:
                    while chunk := await part.read_chunk(1 << 20):
                        await asyncio.to_thread(f.write, chunk)
                name = self.library.unique_name(unquote(part.filename))
                os.replace(tmp, self.library.media_dir / name)
            finally:
                if tmp.exists():
                    tmp.unlink()
            saved.append(name)
            asyncio.create_task(self._probe_and_announce(name))
        self.broadcast({"type": "media"})
        return _json({"uploaded": saved}, 201)

    async def _probe_and_announce(self, name: str) -> None:
        await self.library.probe(name)
        self.broadcast({"type": "media"})

    async def delete_media(self, request):
        name = request.match_info["name"]
        if not self.library.exists(name):
            raise KeyError(name)
        used = self.store.media_usage(name)
        force = request.query.get("force") in ("1", "true", "yes")
        if used and not force:
            return _json({"error": f"{name} is used by playlists: {', '.join(used)}; "
                                   "add ?force=1 to delete it and remove it from them",
                          "used_by": used}, 409)
        if used:
            self.store.remove_media_references(name)
        self.library.delete(name)
        self.broadcast({"type": "media"})
        return _json({"deleted": name, "removed_from": used})

    # ------------------------------------------------------------- settings
    async def get_settings(self, request):
        return _json(self.store.settings)

    async def patch_settings(self, request):
        body = await self.params(request)
        return _json(self.store.update_settings(body))

    # ------------------------------------------------------------------ gpi
    def _gpi(self) -> GpiManager:
        if self.gpi is None:
            raise web.HTTPServiceUnavailable(text="GPI not available")
        return self.gpi

    async def get_gpi(self, request):
        return _json(self._gpi().state())

    async def put_gpi(self, request):
        gpi = self._gpi()
        body = await self.params(request)
        self.store.update_settings({"gpi": body})
        return _json(gpi.state())

    async def fire_gpi(self, request):
        gpi = self._gpi()
        gpi.fire(request.match_info["id"], source="API")
        return _json(gpi.state())

    # ----------------------------------------------------------------- link
    def _pairing(self):
        if self.pairing is None:
            raise web.HTTPServiceUnavailable(text="this player has no head to pair with")
        return self.pairing

    def link_out(self) -> dict:
        p = self._pairing()
        return {**p.info(), "heads": p.discovered()}

    async def get_link(self, request):
        return _json(self.link_out())

    async def link_pair(self, request):
        body = await self.params(request)
        address = body.get("address")
        if not isinstance(address, str) or not address:
            raise ValidationError("address is required")
        self._pairing().pair(address)
        return _json(self.link_out())

    async def link_confirm(self, request):
        self._pairing().confirm()
        return _json(self.link_out())

    async def link_cancel(self, request):
        self._pairing().cancel()
        return _json(self.link_out())

    async def link_unpair(self, request):
        self._pairing().unpair()
        return _json(self.link_out())

    # --------------------------------------------------------------- system
    async def system(self, request):
        return _json({
            "version": __version__,
            "hardware": self.hw,
            # With a head, the display is the head's and is described by "renderer".
            "outputs": [] if self.port else hwmod.hdmi_outputs(),
            "link": self.port.info() if self.port else None,
            "health": self.engine.health(),
            "renderer": self.engine.backend.info() if self.engine.backend else None,
            "uptime": round(time.time() - self.started),
        })

    async def restart_renderer(self, request):
        await self.engine.restart_backend("requested via API")
        return _json(self.engine.status())

    async def preview(self, request):
        data = await self.engine.backend.snapshot() if self.engine.backend else None
        if not data:
            raise web.HTTPServiceUnavailable(text="no preview available")
        return web.Response(body=data, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def docs(self, request):
        return web.Response(text=API_DOC.read_text() if API_DOC.exists() else "# API\n",
                            content_type="text/markdown", charset="utf-8")

    async def ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.sockets.add(ws)
        await ws.send_str(json.dumps({"type": "status", "status": self.engine.status()},
                                     default=str))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.sockets.discard(ws)
        return ws

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


def build_app(store: Store, library: MediaLibrary, engine: Engine, hw: dict,
              gpi: GpiManager | None = None, port=None) -> web.Application:
    api = Api(store, library, engine, hw, gpi, port)
    app = web.Application(middlewares=[cors_mw, errors_mw], client_max_size=4 * 1024 * 1024)
    r = app.router
    transport = [
        ("/api/play", api.play), ("/api/pause", api.pause), ("/api/resume", api.resume),
        ("/api/toggle", api.toggle), ("/api/stop", api.stop), ("/api/next", api.next),
        ("/api/previous", api.previous), ("/api/playlists/{id}/play", api.play_playlist),
        ("/api/loop-item", api.loop_item),
    ]
    for path, handler in transport:
        # GET is accepted too so simple show controllers / browsers can trigger cues.
        r.add_post(path, handler)
        r.add_get(path, handler)
    r.add_get("/api/status", api.status)
    r.add_get("/api/playlists", api.list_playlists)
    r.add_post("/api/playlists", api.create_playlist)
    r.add_get("/api/playlists/{id}", api.get_playlist)
    r.add_put("/api/playlists/{id}", api.put_playlist)
    r.add_patch("/api/playlists/{id}", api.put_playlist)
    r.add_delete("/api/playlists/{id}", api.delete_playlist)
    r.add_patch("/api/playlists/{id}/items/{uid}", api.patch_item)
    r.add_get("/api/media", api.list_media)
    r.add_post("/api/media", api.upload_media)
    r.add_get("/api/media/{name}", api.get_media)
    r.add_get("/api/media/{name}/file", api.media_file)
    r.add_get("/api/media/{name}/thumb", api.media_thumb)
    r.add_delete("/api/media/{name}", api.delete_media)
    r.add_get("/api/settings", api.get_settings)
    r.add_patch("/api/settings", api.patch_settings)
    r.add_put("/api/settings", api.patch_settings)
    r.add_get("/api/gpi", api.get_gpi)
    r.add_put("/api/gpi", api.put_gpi)
    r.add_post("/api/gpi/{id}/fire", api.fire_gpi)
    r.add_get("/api/gpi/{id}/fire", api.fire_gpi)
    r.add_get("/api/link", api.get_link)
    r.add_post("/api/link/pair", api.link_pair)
    r.add_post("/api/link/confirm", api.link_confirm)
    r.add_post("/api/link/cancel", api.link_cancel)
    r.add_post("/api/link/unpair", api.link_unpair)
    r.add_get("/api/system", api.system)
    r.add_post("/api/system/restart-renderer", api.restart_renderer)
    r.add_get("/api/preview.jpg", api.preview)
    r.add_get("/api/docs", api.docs)
    r.add_get("/api/ws", api.ws)
    r.add_get("/", api.index)
    r.add_static("/static/", STATIC)
    r.add_route("OPTIONS", "/{tail:.*}", lambda req: web.Response())

    async def on_startup(app):
        app[TICKER] = asyncio.create_task(api.ticker())
        app[PROBE] = asyncio.create_task(library.probe_all())

    async def on_shutdown(app):
        # Open UI pages hold WebSockets that never end by themselves; close
        # them so shutdown doesn't wait out aiohttp's connection grace period.
        for ws in list(api.sockets):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutting down")

    async def on_cleanup(app):
        app[TICKER].cancel()
        if gpi:
            gpi.stop()
        await engine.shutdown()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    return app
