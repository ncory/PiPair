"""Hardware discovery: Pi model, HDMI output, audio device, health."""

from __future__ import annotations

import glob
import os
import re
import subprocess
from pathlib import Path


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return None


def detect() -> dict:
    model = _read("/proc/device-tree/model") or ""
    family = None
    m = re.search(r"Raspberry Pi (\d+)|Raspberry Pi Compute Module (\d+)", model)
    if m:
        family = f"pi{m.group(1) or m.group(2)}"
    elif "Raspberry Pi Zero 2" in model:
        family = "pi3"  # same SoC generation as the Pi 3
    return {
        "model": model or None,
        "family": family,
        "is_pi": bool(family),
        "memory_mb": _mem_mb(),
    }


def _mem_mb() -> int | None:
    txt = _read("/proc/meminfo") or ""
    m = re.search(r"MemTotal:\s+(\d+)", txt)
    return int(m.group(1)) // 1024 if m else None


def hdmi_outputs() -> list[dict]:
    """Connected HDMI connectors with their DRM card and preferred mode."""
    out = []
    for status_file in sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status")):
        conn_dir = os.path.dirname(status_file)
        name = os.path.basename(conn_dir)  # e.g. card1-HDMI-A-1
        card, _, connector = name.partition("-")
        modes = (_read(os.path.join(conn_dir, "modes")) or "").split()
        out.append({
            "card": f"/dev/dri/{card}",
            "connector": connector,
            "connected": _read(status_file) == "connected",
            "preferred_mode": modes[0] if modes else None,
            "modes": list(dict.fromkeys(modes)),
        })
    return out


def pick_output() -> dict | None:
    outs = hdmi_outputs()
    for o in outs:
        if o["connected"]:
            return o
    return outs[0] if outs else None


def audio_cards() -> list[str]:
    """ALSA card ids, in card order (e.g. ["vc4hdmi", "Headphones"])."""
    cards = _read("/proc/asound/cards") or ""
    # Lines look like: " 0 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi"
    return [m.group(1) for m in re.finditer(r"^\s*\d+\s+\[(\S+)\s*\]", cards, re.M)]


def audio_device(setting: str, prefer_hdmi: bool = True) -> str | None:
    """Resolve the audio.device setting to an ALSA device string (or None).

    "auto" is the HDMI output by default. With `prefer_hdmi` off (the
    controller, which has no display) it is the first card that is neither
    HDMI nor the built-in jack (a USB or I2S DAC), then the jack, then HDMI.
    """
    if setting and setting != "auto":
        return setting
    cards = audio_cards()
    hdmi = [c for c in cards if c.startswith("vc4hdmi")]
    jack = [c for c in cards if c == "Headphones"]
    other = [c for c in cards if c not in hdmi and c not in jack]
    order = hdmi if prefer_hdmi else other + jack + hdmi
    return f"default:CARD={order[0]}" if order else None


def health() -> dict:
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    out: dict = {"temperature_c": round(int(temp) / 1000, 1) if temp and temp.isdigit() else None}
    try:
        load = os.getloadavg()
        out["load"] = [round(x, 2) for x in load]
    except OSError:
        out["load"] = None
    try:
        r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                           text=True, timeout=2)
        m = re.search(r"0x([0-9a-fA-F]+)", r.stdout)
        if m:
            bits = int(m.group(1), 16)
            out["throttled"] = {
                "raw": hex(bits),
                "under_voltage_now": bool(bits & 0x1),
                "throttled_now": bool(bits & 0x4),
                "under_voltage_since_boot": bool(bits & 0x10000),
                "throttled_since_boot": bool(bits & 0x40000),
            }
    except (OSError, subprocess.SubprocessError):
        pass
    mem = _read("/proc/meminfo") or ""
    m = re.search(r"MemAvailable:\s+(\d+)", mem)
    out["memory_available_mb"] = int(m.group(1)) // 1024 if m else None
    return out
