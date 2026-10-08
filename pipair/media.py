"""Media library: file listing, probing, uploads and image pre-rendering.

Images are never fed to the GPU at their original size (the Pi 3's GPU tops
out at 2048x2048 textures, and decoding a 24MP JPEG takes seconds). Instead
each image is rendered once, with Pillow, to exactly the output resolution
with the fit mode and letterbox color baked in, and cached as a JPEG.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".mpg", ".mpeg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
_SAFE_RE = re.compile(r"[^A-Za-z0-9._ ()+-]")


def media_kind(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = _SAFE_RE.sub("_", name).lstrip(".")
    stem, ext = os.path.splitext(name)
    return (stem[:180] or "file") + ext.lower()


class MediaLibrary:
    def __init__(self, media_dir: Path, cache_dir: Path, hw: dict):
        self.media_dir = media_dir
        self.cache_dir = cache_dir
        self.hw = hw
        media_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "images").mkdir(parents=True, exist_ok=True)
        (cache_dir / "thumbs").mkdir(parents=True, exist_ok=True)
        self._probe_path = cache_dir / "probe.json"
        try:
            self._probe_cache: dict[str, dict] = json.loads(self._probe_path.read_text())
        except (OSError, json.JSONDecodeError):
            self._probe_cache = {}
        self._probing: dict[str, asyncio.Future] = {}

    # -- listing -----------------------------------------------------------
    def path(self, name: str) -> Path:
        p = (self.media_dir / name).resolve()
        if p.parent != self.media_dir.resolve():
            raise ValueError("invalid media name")
        return p

    def exists(self, name: str) -> bool:
        try:
            return self.path(name).is_file()
        except ValueError:
            return False

    def _key(self, p: Path) -> str:
        st = p.stat()
        return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"

    def entry(self, name: str) -> dict | None:
        try:
            p = self.path(name)
            st = p.stat()
        except (ValueError, OSError):
            return None
        kind = media_kind(name)
        info = self._probe_cache.get(self._key(p))
        return {
            "name": name,
            "kind": kind,
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "info": info,
            "warnings": self.warnings(kind, info),
        }

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.media_dir.iterdir(), key=lambda p: p.name.lower()):
            if p.is_file() and not p.name.startswith(".") and media_kind(p.name):
                e = self.entry(p.name)
                if e:
                    out.append(e)
        return out

    def info(self, name: str) -> dict | None:
        try:
            return self._probe_cache.get(self._key(self.path(name)))
        except (ValueError, OSError):
            return None

    def warnings(self, kind: str | None, info: dict | None) -> list[str]:
        if kind != "video" or not info:
            return []
        if info.get("error"):
            return [f"Could not read file: {info['error']}"]
        w = []
        codec = (info.get("video_codec") or "").lower()
        model = self.hw.get("family")
        if not codec:
            w.append("No video stream found")
        elif model == "pi3" and "h.264" not in codec and "avc" not in codec:
            w.append(f"{info['video_codec']} is not hardware-decoded on a Pi 3; "
                     "re-encode as H.264")
        elif model == "pi4" and not any(c in codec for c in ("h.264", "avc", "h.265", "hevc")):
            w.append(f"{info['video_codec']} is not hardware-decoded on a Pi 4; "
                     "re-encode as H.264 or H.265")
        width, height = info.get("width") or 0, info.get("height") or 0
        if model == "pi3" and (width > 1920 or height > 1088):
            w.append(f"{width}x{height} exceeds the Pi 3 decoder limit of 1920x1080")
        fps = info.get("fps") or 0
        if model == "pi3" and fps > 31 and width * height > 1280 * 720:
            w.append(f"{fps:g} fps at {width}x{height} may drop frames on a Pi 3; "
                     "30 fps is recommended")
        return w

    # -- probing -----------------------------------------------------------
    async def probe(self, name: str) -> dict | None:
        try:
            p = self.path(name)
            key = self._key(p)
        except (ValueError, OSError):
            return None
        if key in self._probe_cache:
            return self._probe_cache[key]
        if key in self._probing:
            return await self._probing[key]
        fut = asyncio.get_running_loop().create_future()
        self._probing[key] = fut
        try:
            info = await asyncio.to_thread(_probe_file, p)
        except Exception as e:  # noqa: BLE001
            info = {"error": str(e)}
        self._probe_cache[key] = info
        self._probing.pop(key, None)
        fut.set_result(info)
        self._save_probe_cache()
        return info

    async def probe_all(self) -> None:
        for e in self.list():
            if e["info"] is None:
                await self.probe(e["name"])

    def _save_probe_cache(self) -> None:
        live = set()
        for p in self.media_dir.iterdir():
            if p.is_file():
                live.add(self._key(p))
        self._probe_cache = {k: v for k, v in self._probe_cache.items() if k in live}
        tmp = self._probe_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._probe_cache))
        os.replace(tmp, self._probe_path)

    # -- uploads / deletes -------------------------------------------------
    def unique_name(self, name: str) -> str:
        name = safe_filename(name)
        stem, ext = os.path.splitext(name)
        n = 2
        while (self.media_dir / name).exists():
            name = f"{stem} ({n}){ext}"
            n += 1
        return name

    def delete(self, name: str) -> None:
        self.path(name).unlink()
        self._save_probe_cache()

    # -- image rendering ---------------------------------------------------
    def rendered_image(self, name: str, width: int, height: int, fit: str,
                       background: str) -> Path:
        """Return a JPEG of `name` rendered to exactly width x height (blocking)."""
        src = self.path(name)
        st = src.stat()
        h = hashlib.sha1(
            f"{name}|{st.st_size}|{st.st_mtime_ns}|{width}x{height}|{fit}|{background}".encode()
        ).hexdigest()[:16]
        out = self.cache_dir / "images" / f"{h}.jpg"
        if out.exists():
            return out
        from PIL import Image, ImageOps

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, background)
                bg.alpha_composite(im)
                im = bg
            im = im.convert("RGB")
            canvas = Image.new("RGB", (width, height), background)
            if fit == "stretch":
                canvas = im.resize((width, height), Image.LANCZOS)
            elif fit == "cover":
                canvas = ImageOps.fit(im, (width, height), Image.LANCZOS)
            else:
                scale = min(width / im.width, height / im.height)
                size = (max(1, round(im.width * scale)), max(1, round(im.height * scale)))
                im = im.resize(size, Image.LANCZOS)
                canvas.paste(im, ((width - size[0]) // 2, (height - size[1]) // 2))
            tmp = out.with_suffix(".tmp")
            canvas.save(tmp, "JPEG", quality=92)
            os.replace(tmp, out)
        self._prune_image_cache()
        return out

    def _prune_image_cache(self, keep: int = 400) -> None:
        files = sorted((self.cache_dir / "images").glob("*.jpg"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            p.unlink(missing_ok=True)

    def thumbnail(self, name: str) -> Path | None:
        """Small JPEG thumbnail for the web UI (images only; blocking)."""
        if media_kind(name) != "image":
            return None
        src = self.path(name)
        st = src.stat()
        h = hashlib.sha1(f"{name}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
        out = self.cache_dir / "thumbs" / f"{h}.jpg"
        if not out.exists():
            from PIL import Image, ImageOps

            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((320, 180))
                im.save(out, "JPEG", quality=80)
        return out


# -- probing backends --------------------------------------------------------

def _probe_file(p: Path) -> dict:
    kind = media_kind(p.name)
    if kind == "image":
        from PIL import Image

        with Image.open(p) as im:
            return {"width": im.width, "height": im.height, "format": im.format}
    try:
        return _probe_gst(p)
    except ImportError:
        pass
    if shutil.which("ffprobe"):
        return _probe_ffprobe(p)
    return {}


def _probe_gst(p: Path) -> dict:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import Gst, GstPbutils

    Gst.init(None)
    disc = GstPbutils.Discoverer.new(15 * Gst.SECOND)
    try:
        res = disc.discover_uri(p.resolve().as_uri())
    except Exception as e:  # noqa: BLE001 - GLib.Error
        return {"error": str(e).split(":")[-1].strip() or "unreadable"}
    out: dict[str, Any] = {}
    dur = res.get_duration()
    if dur and dur != Gst.CLOCK_TIME_NONE:
        out["duration"] = dur / Gst.SECOND
    vids = res.get_video_streams()
    if vids:
        v = vids[0]
        out["width"], out["height"] = v.get_width(), v.get_height()
        if v.get_framerate_denom():
            out["fps"] = round(v.get_framerate_num() / v.get_framerate_denom(), 3)
        caps = v.get_caps()
        out["video_codec"] = GstPbutils.pb_utils_get_codec_description(caps) if caps else None
    auds = res.get_audio_streams()
    if auds:
        caps = auds[0].get_caps()
        out["audio_codec"] = GstPbutils.pb_utils_get_codec_description(caps) if caps else "audio"
    return out


def _probe_ffprobe(p: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", str(p)],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        return {"error": r.stderr.strip() or "unreadable"}
    d = json.loads(r.stdout)
    out: dict[str, Any] = {}
    if "duration" in d.get("format", {}):
        out["duration"] = float(d["format"]["duration"])
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and "width" not in out:
            out["width"], out["height"] = s.get("width"), s.get("height")
            names = {"h264": "H.264 (High Profile)", "hevc": "H.265 (HEVC)"}
            out["video_codec"] = names.get(s.get("codec_name"), s.get("codec_name"))
            num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
            if den and int(den):
                out["fps"] = round(int(num) / int(den), 3)
        elif s.get("codec_type") == "audio" and "audio_codec" not in out:
            out["audio_codec"] = s.get("codec_name")
    return out
