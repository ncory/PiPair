"""GPI: fire playlists and transport commands from GPIO contact closures.

Each configured input watches one GPIO line (BCM numbering) for edges and runs
an action when the contact closes, opens, or both. Lines are read through the
kernel's GPIO character device via libgpiod (python3-libgpiod), with kernel
debounce; a per-input hold-off additionally ignores re-triggers.

Typical wiring: a switch or relay contact between the GPIO pin and GND, with
the internal pull-up enabled (the default). Closing the contact pulls the pin
low, which counts as "close" (active_low).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from .config import Store
from .engine import Engine

log = logging.getLogger(__name__)

# BCM GPIO number -> physical pin on the 40-pin header
HEADER_PINS = {2: 3, 3: 5, 4: 7, 17: 11, 27: 13, 22: 15, 10: 19, 9: 21, 11: 23, 0: 27, 5: 29,
               6: 31, 13: 33, 19: 35, 26: 37, 14: 8, 15: 10, 18: 12, 23: 16, 24: 18, 25: 22,
               8: 24, 7: 26, 1: 28, 12: 32, 16: 36, 20: 38, 21: 40}
GROUND_PINS = (6, 9, 14, 20, 25, 30, 34, 39)
PIN_NOTES = {0: "ID EEPROM (avoid)", 1: "ID EEPROM (avoid)", 2: "I2C SDA, has a fixed pull-up",
             3: "I2C SCL, has a fixed pull-up", 14: "UART TX", 15: "UART RX"}
CHIP_LABELS = ("pinctrl-bcm2835", "pinctrl-bcm2711", "pinctrl-bcm2712", "pinctrl-rp1")


class LibgpiodDriver:
    """GPIO access through libgpiod v2."""

    def __init__(self):
        import gpiod  # noqa: F401 - raises ImportError when unavailable

        self.gpiod = gpiod
        self.chip = self._find_chip()

    def _find_chip(self) -> str:
        import glob

        gpiod = self.gpiod
        paths = sorted(p for p in glob.glob("/dev/gpiochip*") if gpiod.is_gpiochip_device(p))
        for path in paths:
            try:
                with gpiod.Chip(path) as chip:
                    if chip.get_info().label in CHIP_LABELS:
                        return path
            except OSError:
                continue
        if paths:
            return paths[0]
        raise OSError("no GPIO chip found")

    def open(self, pin: int, pull: str, active_low: bool, debounce_ms: int) -> "_LibgpiodLine":
        from datetime import timedelta

        from gpiod.line import Bias, Direction, Edge

        settings = self.gpiod.LineSettings(
            direction=Direction.INPUT, edge_detection=Edge.BOTH, active_low=active_low,
            bias={"up": Bias.PULL_UP, "down": Bias.PULL_DOWN, "none": Bias.DISABLED}[pull],
            debounce_period=timedelta(milliseconds=debounce_ms))
        req = self.gpiod.request_lines(self.chip, consumer="pipair", config={pin: settings})
        return _LibgpiodLine(self, req, pin)


class _LibgpiodLine:
    def __init__(self, driver: LibgpiodDriver, req, pin: int):
        self.driver = driver
        self.req = req
        self.pin = pin
        self.fd = req.fd

    def read_events(self) -> list[str]:
        """Pending edges as "close" (became active) / "open" (became inactive)."""
        rising = self.driver.gpiod.EdgeEvent.Type.RISING_EDGE
        return ["close" if ev.event_type == rising else "open"
                for ev in self.req.read_edge_events()]

    def is_active(self) -> bool:
        from gpiod.line import Value

        return self.req.get_value(self.pin) == Value.ACTIVE

    def close(self) -> None:
        self.req.release()


class GpiManager:
    def __init__(self, store: Store, engine: Engine,
                 driver_factory: Callable[[], Any] | None = LibgpiodDriver):
        self.store = store
        self.engine = engine
        self.driver_factory = driver_factory
        self.driver = None
        self.error: str | None = None
        self.lines: dict[int, Any] = {}
        self.pin_errors: dict[int, str] = {}
        self.levels: dict[int, bool] = {}
        self.stats: dict[str, dict] = {}
        self._last_fire: dict[str, float] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        store.on_change(self._on_store_change)

    # --------------------------------------------------------------- setup
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.apply()

    def stop(self) -> None:
        self._release()

    def on_event(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def _on_store_change(self, what: str, detail: Any) -> None:
        if what == "settings" and self.loop and detail and \
                detail.get("old", {}).get("gpi") != self.store.settings["gpi"]:
            self.apply()

    def _release(self) -> None:
        for line in self.lines.values():
            if self.loop:
                self.loop.remove_reader(line.fd)
            try:
                line.close()
            except Exception:  # noqa: BLE001
                log.exception("releasing GPIO line failed")
        self.lines.clear()
        self.levels.clear()
        self.pin_errors.clear()

    def apply(self) -> None:
        """(Re)claim GPIO lines for the current configuration."""
        self._release()
        cfg = self.store.settings["gpi"]
        if not cfg["enabled"] or not cfg["inputs"]:
            return
        if self.driver is None:
            try:
                self.driver = self.driver_factory() if self.driver_factory else None
                self.error = None if self.driver else "GPIO disabled"
            except ImportError:
                self.error = "GPIO support unavailable (python3-libgpiod not installed)"
            except OSError as e:
                self.error = f"GPIO unavailable: {e}"
            if self.driver is None:
                log.warning("%s", self.error)
                return
        for inp in cfg["inputs"]:
            pin = inp["pin"]
            if pin in self.lines or pin in self.pin_errors:
                continue
            try:
                line = self.driver.open(pin, inp["pull"], inp["active_low"], inp["debounce_ms"])
            except OSError as e:
                self.pin_errors[pin] = str(e) or e.__class__.__name__
                log.error("GPIO %d: %s", pin, self.pin_errors[pin])
                continue
            self.lines[pin] = line
            try:
                self.levels[pin] = line.is_active()
            except OSError:
                pass
            self.loop.add_reader(line.fd, self._readable, pin)
        log.info("GPI watching GPIO %s", ", ".join(str(p) for p in sorted(self.lines)) or "-")

    # -------------------------------------------------------------- events
    def _readable(self, pin: int) -> None:
        line = self.lines.get(pin)
        if line is None:
            return
        try:
            events = line.read_events()
        except OSError as e:
            log.error("GPIO %d read failed: %s", pin, e)
            return
        for what in events:
            self.levels[pin] = what == "close"
            self._notify({"type": "gpi", "pin": pin, "event": what})
            for inp in self.store.settings["gpi"]["inputs"]:
                if inp["pin"] == pin and inp["fire_on"] in (what, "both"):
                    self._trigger(inp, source=f"GPIO {pin} {what}")

    def _trigger(self, inp: dict, source: str) -> None:
        now = time.monotonic()
        last = self._last_fire.get(inp["id"])
        if last is not None and (now - last) * 1000 < inp["holdoff_ms"]:
            log.info("GPI %s: ignored (hold-off)", inp["name"])
            return
        self._last_fire[inp["id"]] = now
        self.fire(inp["id"], source)

    def fire(self, input_id: str, source: str = "test") -> dict:
        inp = next((i for i in self.store.settings["gpi"]["inputs"] if i["id"] == input_id), None)
        if inp is None:
            raise KeyError(input_id)
        st = self.stats.setdefault(input_id, {"count": 0, "last": None, "last_error": None})
        st["count"] += 1
        st["last"] = time.time()
        st["last_error"] = None
        log.info("GPI %s (%s): %s", inp["name"], source, inp["action"]["type"])
        asyncio.get_running_loop().create_task(self._run(inp, st))
        self._notify({"type": "gpi", "input": input_id, "fired": True, "source": source})
        return st

    async def _run(self, inp: dict, st: dict) -> None:
        a, e = inp["action"], self.engine
        try:
            t = a["type"]
            if t == "play":
                await e.play(a["playlist"], a.get("index", 0), a.get("transition"))
            elif t == "next":
                await e.next(a.get("transition"))
            elif t == "previous":
                await e.previous(a.get("transition"))
            elif t == "stop":
                await e.stop(a.get("transition"))
            elif t == "pause":
                e.pause()
            elif t == "resume":
                e.resume()
            elif t == "toggle":
                e.toggle_pause()
            elif t == "loop_item":
                mode = a.get("mode", "toggle")
                e.set_loop_item(not e.loop_item if mode == "toggle" else mode == "on")
        except KeyError as ex:
            st["last_error"] = f"playlist '{ex.args[0]}' not found"
        except Exception as ex:  # noqa: BLE001
            st["last_error"] = str(ex)
        if st["last_error"]:
            log.error("GPI %s: %s", inp["name"], st["last_error"])
            self._notify({"type": "gpi", "input": inp["id"], "error": st["last_error"]})

    def _notify(self, msg: dict) -> None:
        for fn in self._listeners:
            try:
                fn(msg)
            except Exception:  # noqa: BLE001
                log.exception("GPI listener failed")

    # --------------------------------------------------------------- state
    def state(self) -> dict:
        cfg = self.store.settings["gpi"]
        inputs = []
        for inp in cfg["inputs"]:
            pin = inp["pin"]
            level = self.levels.get(pin)
            inputs.append({
                **inp,
                "header_pin": HEADER_PINS.get(pin),
                "state": None if level is None else ("closed" if level else "open"),
                "error": self.pin_errors.get(pin),
                **self.stats.get(inp["id"], {"count": 0, "last": None, "last_error": None}),
            })
        return {
            "enabled": cfg["enabled"],
            "available": self.driver is not None,
            "chip": getattr(self.driver, "chip", None),
            "error": self.error,
            "inputs": inputs,
            "pins": [{"gpio": g, "header_pin": p, "note": PIN_NOTES.get(g)}
                     for g, p in sorted(HEADER_PINS.items())],
            "ground_pins": list(GROUND_PINS),
        }
