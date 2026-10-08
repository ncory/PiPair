"""Persistent state: settings and playlists, with validation.

Everything lives in a single JSON file (``<data>/state.json``) that is written
atomically on every change. All access happens on the asyncio thread, so no
locking is needed.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from .media import media_kind

log = logging.getLogger(__name__)

TRANSITION_TYPES = ("cut", "dissolve", "dip")
END_ACTIONS = ("loop", "stop", "hold", "goto")
FITS = ("contain", "cover", "stretch")
ROTATIONS = (0, 90, 180, 270)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SIZE_RE = re.compile(r"^(auto|\d{3,4}x\d{3,4})$")
MODE_RE = re.compile(r"^(auto|\d{3,4}x\d{3,4}(@\d{2,3})?)$")
MAX_TRANSITION_SECONDS = 30.0
MAX_OFFSET = 10000  # pixels
MAX_AV_OFFSET_MS = 2000
GPI_FIRE_ON = ("close", "open", "both")
GPI_PULLS = ("up", "down", "none")
GPI_ACTIONS = ("play", "next", "previous", "stop", "pause", "resume", "toggle", "loop_item")
GPI_MAX_PIN = 27  # BCM GPIO numbers on the 40-pin header

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_playlist": "default",
    "default_transition": {"type": "dissolve", "duration": 1.0, "color": "#000000"},
    "default_image_duration": 10.0,
    "default_fit": "contain",
    "background_color": "#000000",
    "output": {"mode": "auto", "render_size": "auto", "fps": 30, "rotation": 0},
    "audio": {"enabled": True, "device": "auto", "volume": 100},
    "gpi": {"enabled": True, "inputs": []},
    # The Bluetooth link to the head. `peer` is the head's Bluetooth name or
    # address ("" = any PiPair head in range). `av_offset_ms` moves the
    # picture relative to the sound (positive = picture later).
    "link": {"peer": "", "av_offset_ms": 0},
}

# Settings that require the video pipeline to be rebuilt when changed.
PIPELINE_SETTINGS = ("output", "audio.enabled", "audio.device", "link.av_offset_ms")


class ValidationError(ValueError):
    pass


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _num(value: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{name} must be a number") from None
    if not lo <= value <= hi:
        raise ValidationError(f"{name} must be between {lo:g} and {hi:g}")
    return float(value)


def _color(value: Any, name: str) -> str:
    if not isinstance(value, str) or not COLOR_RE.match(value):
        raise ValidationError(f"{name} must be a color like #000000")
    return value.lower()


def validate_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValidationError(
            "id must be 1-64 chars: lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit"
        )
    return value


def validate_transition(value: Any, name: str = "transition",
                        allow_null: bool = True) -> dict | None:
    if value is None:
        if allow_null:
            return None
        raise ValidationError(f"{name} is required")
    if isinstance(value, str):
        value = {"type": value}
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    t = value.get("type", "dissolve")
    if t not in TRANSITION_TYPES:
        raise ValidationError(f"{name}.type must be one of {', '.join(TRANSITION_TYPES)}")
    return {
        "type": t,
        "duration": _num(value.get("duration", 1.0), f"{name}.duration", 0, MAX_TRANSITION_SECONDS),
        "color": _color(value.get("color", "#000000"), f"{name}.color"),
    }


def validate_settings(s: dict) -> dict:
    out = _deep_merge(DEFAULT_SETTINGS, s)
    dp = out["default_playlist"]
    if dp is not None and dp != "":
        validate_id(dp)
    else:
        out["default_playlist"] = None
    out["default_transition"] = validate_transition(
        out["default_transition"], "default_transition", allow_null=False)
    out["default_image_duration"] = _num(
        out["default_image_duration"], "default_image_duration", 0.1, 86400)
    if out["default_fit"] not in FITS:
        raise ValidationError(f"default_fit must be one of {', '.join(FITS)}")
    out["background_color"] = _color(out["background_color"], "background_color")
    o = out["output"]
    if not isinstance(o.get("mode"), str) or not MODE_RE.match(o["mode"]):
        raise ValidationError("output.mode must be 'auto', WIDTHxHEIGHT or WIDTHxHEIGHT@HZ")
    if not isinstance(o.get("render_size"), str) or not SIZE_RE.match(o["render_size"]):
        raise ValidationError("output.render_size must be 'auto' or WIDTHxHEIGHT")
    o["fps"] = int(_num(o["fps"], "output.fps", 1, 60))
    if o["rotation"] not in ROTATIONS:
        raise ValidationError("output.rotation must be 0, 90, 180 or 270")
    a = out["audio"]
    a["enabled"] = bool(a["enabled"])
    if not isinstance(a["device"], str) or not a["device"]:
        raise ValidationError("audio.device must be 'auto' or an ALSA device name")
    a["volume"] = _num(a["volume"], "audio.volume", 0, 100)
    out["gpi"] = validate_gpi(out["gpi"])
    ln = out["link"]
    if not isinstance(ln, dict) or not isinstance(ln.get("peer"), str) or len(ln["peer"]) > 64:
        raise ValidationError("link.peer must be a Bluetooth name or address (or empty)")
    out["link"] = {"peer": ln["peer"].strip(),
                   "av_offset_ms": int(_num(ln.get("av_offset_ms", 0), "link.av_offset_ms",
                                            -MAX_AV_OFFSET_MS, MAX_AV_OFFSET_MS))}
    return out


def validate_gpi_action(a: Any, name: str) -> dict:
    if isinstance(a, str):
        a = {"type": a}
    if not isinstance(a, dict) or a.get("type") not in GPI_ACTIONS:
        raise ValidationError(f"{name}.type must be one of {', '.join(GPI_ACTIONS)}")
    t = a["type"]
    out: dict[str, Any] = {"type": t}
    if t == "play":
        out["playlist"] = validate_id(a.get("playlist"))
        out["index"] = int(_num(a.get("index") or 0, f"{name}.index", 0, 100000))
    if t in ("play", "next", "previous", "stop"):
        out["transition"] = validate_transition(a.get("transition"), f"{name}.transition")
    if t == "loop_item":
        mode = a.get("mode", "toggle")
        if mode not in ("on", "off", "toggle"):
            raise ValidationError(f"{name}.mode must be on, off or toggle")
        out["mode"] = mode
    return out


def validate_gpi(g: Any) -> dict:
    if not isinstance(g, dict):
        raise ValidationError("gpi must be an object")
    inputs = g.get("inputs") or []
    if not isinstance(inputs, list):
        raise ValidationError("gpi.inputs must be a list")
    out, line_cfg, ids = [], {}, set()
    for i, inp in enumerate(inputs):
        name = f"gpi.inputs[{i}]"
        if not isinstance(inp, dict):
            raise ValidationError(f"{name} must be an object")
        pin = int(_num(inp.get("pin"), f"{name}.pin", 0, GPI_MAX_PIN))
        pull = inp.get("pull", "up")
        if pull not in GPI_PULLS:
            raise ValidationError(f"{name}.pull must be one of {', '.join(GPI_PULLS)}")
        fire_on = inp.get("fire_on", "close")
        if fire_on not in GPI_FIRE_ON:
            raise ValidationError(f"{name}.fire_on must be one of {', '.join(GPI_FIRE_ON)}")
        v = {
            "id": str(inp.get("id") or secrets.token_hex(3)),
            "name": str(inp.get("name") or f"GPIO {pin}")[:100],
            "pin": pin,
            "fire_on": fire_on,
            "pull": pull,
            # closing a contact to GND reads low when pulled up
            "active_low": bool(inp.get("active_low", pull != "down")),
            "debounce_ms": int(_num(inp.get("debounce_ms", 20), f"{name}.debounce_ms", 0, 1000)),
            "holdoff_ms": int(_num(inp.get("holdoff_ms", 300), f"{name}.holdoff_ms", 0, 60000)),
            "action": validate_gpi_action(inp.get("action"), f"{name}.action"),
        }
        if v["id"] in ids:
            raise ValidationError(f"{name}.id '{v['id']}' is used twice")
        ids.add(v["id"])
        line = (v["pull"], v["active_low"], v["debounce_ms"])
        if line_cfg.setdefault(pin, line) != line:
            raise ValidationError(f"inputs on GPIO {pin} must use the same pull, active level "
                                  "and debounce")
        out.append(v)
    return {"enabled": bool(g.get("enabled", True)), "inputs": out}


def validate_item(item: Any, idx: int) -> dict:
    name = f"items[{idx}]"
    if isinstance(item, str):
        item = {"media": item}
    if not isinstance(item, dict):
        raise ValidationError(f"{name} must be an object")
    media = item.get("media")
    if not isinstance(media, str) or not media or "/" in media or media.startswith("."):
        raise ValidationError(f"{name}.media must be a media file name")
    if media_kind(media) is None:
        raise ValidationError(f"{name}.media has an unsupported file type")
    uid = item.get("uid") or secrets.token_hex(4)
    out = {"uid": str(uid), "media": media}
    dur = item.get("duration")
    out["duration"] = None if dur in (None, "") else _num(dur, f"{name}.duration", 0.1, 86400)
    out["transition"] = validate_transition(item.get("transition"), f"{name}.transition")
    fit = item.get("fit")
    if fit not in (None, "", *FITS):
        raise ValidationError(f"{name}.fit must be one of {', '.join(FITS)}")
    out["fit"] = fit or None
    for k in ("offset_x", "offset_y"):
        out[k] = int(_num(item.get(k) or 0, f"{name}.{k}", -MAX_OFFSET, MAX_OFFSET))
    return out


def validate_playlist(p: Any, pid: str | None = None) -> dict:
    if not isinstance(p, dict):
        raise ValidationError("playlist must be an object")
    pid = validate_id(p.get("id", pid))
    name = p.get("name") or pid
    if not isinstance(name, str) or len(name) > 200:
        raise ValidationError("name must be a string up to 200 characters")
    items = p.get("items", [])
    if not isinstance(items, list):
        raise ValidationError("items must be a list")
    end = p.get("end_action") or {"type": "loop"}
    if isinstance(end, str):
        end = {"type": end}
    if end.get("type") not in END_ACTIONS:
        raise ValidationError(f"end_action.type must be one of {', '.join(END_ACTIONS)}")
    end_out = {"type": end["type"], "playlist": None, "transition": None}
    if end["type"] == "goto":
        end_out["playlist"] = validate_id(end.get("playlist"))
        end_out["transition"] = validate_transition(end.get("transition"), "end_action.transition")
    return {
        "id": pid,
        "name": name.strip() or pid,
        "items": [validate_item(it, i) for i, it in enumerate(items)],
        "transition": validate_transition(p.get("transition")),
        "end_action": end_out,
    }


class Store:
    """Settings + playlists, persisted to JSON."""

    def __init__(self, path: Path):
        self.path = path
        self.settings: dict = validate_settings({})
        self.playlists: dict[str, dict] = {}
        self._listeners: list[Callable[[str, Any], None]] = []
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            self.playlists = {
                "default": validate_playlist({"id": "default", "name": "Default"})
            }
            self.save()
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            backup = self.path.with_suffix(".corrupt.json")
            log.error("could not read %s (%s); moving it to %s", self.path, e, backup)
            os.replace(self.path, backup)
            return self._load()
        try:
            self.settings = validate_settings(data.get("settings", {}))
        except ValidationError as e:
            log.error("invalid settings in state file, using defaults: %s", e)
        for p in data.get("playlists", []):
            try:
                pl = validate_playlist(p)
                self.playlists[pl["id"]] = pl
            except ValidationError as e:
                log.error("dropping invalid playlist %r: %s", p.get("id"), e)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"settings": self.settings, "playlists": list(self.playlists.values())}
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    def on_change(self, fn: Callable[[str, Any], None]) -> None:
        self._listeners.append(fn)

    def _changed(self, what: str, detail: Any = None) -> None:
        self.save()
        for fn in self._listeners:
            try:
                fn(what, detail)
            except Exception:  # noqa: BLE001 - never let a listener break a save
                log.exception("change listener failed")

    # -- settings ----------------------------------------------------------
    def update_settings(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValidationError("settings must be an object")
        old = self.settings
        new = validate_settings(_deep_merge(old, patch))
        self.settings = new
        rebuild = any(_get_path(old, k) != _get_path(new, k) for k in PIPELINE_SETTINGS)
        self._changed("settings", {"rebuild": rebuild, "old": old})
        return new

    # -- playlists ---------------------------------------------------------
    def get_playlist(self, pid: str) -> dict | None:
        return self.playlists.get(pid)

    def create_playlist(self, data: dict) -> dict:
        pl = validate_playlist(data)
        if pl["id"] in self.playlists:
            raise ValidationError(f"playlist '{pl['id']}' already exists")
        self.playlists[pl["id"]] = pl
        self._changed("playlist", pl["id"])
        return pl

    def replace_playlist(self, pid: str, data: dict, partial: bool = False) -> dict:
        cur = self.playlists.get(pid)
        if cur is None:
            raise KeyError(pid)
        merged = {**cur, **data} if partial else {"id": pid, **data}
        pl = validate_playlist(merged)
        new_id = pl["id"]
        if new_id != pid:
            if new_id in self.playlists:
                raise ValidationError(f"playlist '{new_id}' already exists")
            del self.playlists[pid]
            self._rename_references(pid, new_id)
        self.playlists[new_id] = pl
        self._changed("playlist", {"id": new_id, "old_id": pid})
        return pl

    def update_item(self, pid: str, uid: str, patch: dict) -> dict:
        """Change fields of one playlist item (e.g. live position nudges)."""
        pl = self.playlists.get(pid)
        if pl is None:
            raise KeyError(pid)
        if not isinstance(patch, dict):
            raise ValidationError("item patch must be an object")
        for i, it in enumerate(pl["items"]):
            if it["uid"] == uid:
                new = validate_item({**it, **patch, "uid": uid}, i)
                pl["items"][i] = new
                self._changed("playlist", {"id": pid, "old_id": pid, "item": uid})
                return new
        raise KeyError(uid)

    def delete_playlist(self, pid: str) -> None:
        if pid not in self.playlists:
            raise KeyError(pid)
        del self.playlists[pid]
        self._changed("playlist", {"id": None, "old_id": pid})

    def _rename_references(self, old: str, new: str) -> None:
        if self.settings.get("default_playlist") == old:
            self.settings["default_playlist"] = new
        for pl in self.playlists.values():
            if pl["end_action"].get("playlist") == old:
                pl["end_action"]["playlist"] = new
        for inp in self.settings["gpi"]["inputs"]:
            if inp["action"].get("playlist") == old:
                inp["action"]["playlist"] = new

    def remove_media_references(self, name: str) -> list[str]:
        touched = []
        for pl in self.playlists.values():
            before = len(pl["items"])
            pl["items"] = [it for it in pl["items"] if it["media"] != name]
            if len(pl["items"]) != before:
                touched.append(pl["id"])
        if touched:
            self._changed("playlist", None)
        return touched

    def media_usage(self, name: str) -> list[str]:
        return [pl["id"] for pl in self.playlists.values()
                if any(it["media"] == name for it in pl["items"])]


def _get_path(d: dict, dotted: str) -> Any:
    for part in dotted.split("."):
        d = d.get(part) if isinstance(d, dict) else None
    return d
