"""Entry point: python3 -m pipair [options]"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import os
import signal
import socket
from pathlib import Path

from aiohttp import web

from . import __version__
from . import hw as hwmod
from .config import Store
from .engine import Engine
from .gpi import GpiManager
from .media import MediaLibrary
from .web import build_app

log = logging.getLogger("pipair")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="pipair",
                                description="Synchronized two-device video player")
    p.add_argument("--role", choices=("controller", "head", "standalone"),
                   default=os.environ.get("PIPAIR_ROLE", "controller"),
                   help="controller: sound, web UI and API, drives the head over Bluetooth. "
                        "head: shows the picture, no UI. standalone: one device does both "
                        "[env PIPAIR_ROLE]")
    p.add_argument("--link", choices=("auto", "ble", "loopback"),
                   default=os.environ.get("PIPAIR_LINK", "auto"),
                   help="controller: how to reach the head. loopback runs a head inside this "
                        "process (development); auto = ble on a Pi, loopback elsewhere")
    p.add_argument("--name", default=os.environ.get("PIPAIR_NAME") or None,
                   help="head: Bluetooth name to advertise (default: the hostname)")
    p.add_argument("--data", default=os.environ.get("PIPAIR_DATA", "./data"),
                   help="data directory (state, media, cache) [env PIPAIR_DATA]")
    p.add_argument("--host", default=os.environ.get("PIPAIR_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PIPAIR_PORT", "8080")))
    p.add_argument("--backend", choices=("auto", "kms", "gl", "sim"),
                   default=os.environ.get("PIPAIR_BACKEND", "auto"),
                   help="video renderer: kms (hardware display planes; the Pi default), gl "
                        "(GStreamer GL compositing), sim (simulated, no video), auto")
    p.add_argument("--sink", choices=("kms", "fake", "window"),
                   default=os.environ.get("PIPAIR_SINK", "kms"),
                   help="gl renderer output: kms (HDMI via GBM), window (desktop), fake (headless)")
    p.add_argument("--no-autoplay", action="store_true", help="don't start the default playlist")
    p.add_argument("--unpair", action="store_true",
                   help="forget this device's partner (in --data) and exit")
    p.add_argument("--log-level", default=os.environ.get("PIPAIR_LOG", "info"))
    return p.parse_args()


def make_backend_factory(args, hw: dict):
    kind = {"gst": "gl"}.get(args.backend, args.backend)
    if kind == "auto":
        kind = "kms" if hw["is_pi"] else "sim"
    if kind == "kms":
        from .backend_kms import KmsBackend

        log.info("using KMS plane renderer")
        return KmsBackend
    if kind == "sim":
        from .backend_sim import SimBackend

        log.info("using simulated renderer (no video output)")
        return SimBackend
    if args.sink == "kms":
        from .backend_gst import configure_environment  # noqa: F401 (env must precede GL init)

        out = hwmod.pick_output()
        log.info("HDMI output: %s", out or "none found")
        configure_environment(out)
    from .backend_gst import GstBackend

    return lambda settings, hw_, emit: GstBackend(settings, hw_, emit, sink=args.sink)


def make_audio_factory(args, hw: dict):
    """The controller's own renderer: sound only."""
    if args.backend == "sim" or (args.backend == "auto" and not hw["is_pi"]):
        from .backend_sim import SimBackend

        return SimBackend
    from .backend_audio import AudioBackend

    return AudioBackend


async def wait_for_stop() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("shutting down")


async def run_head(args, data: Path, hw: dict) -> None:
    from .ble_peripheral import BlePeripheralLink
    from .head import Head
    from .pairing import HeadLink, Identity

    store = Store(data / "state.json")
    library = MediaLibrary(data / "media", data / "cache", hw)
    name = args.name or socket.gethostname()
    link = HeadLink(BlePeripheralLink(name=name), Identity(data / "pairing.json"), name)
    head = Head(store, library, make_backend_factory(args, hw), hw, link)
    link.on_notice = head.set_notice
    await head.start()
    log.info("head is up; media folder %s", library.media_dir)
    await wait_for_stop()
    await head.shutdown()


async def main_async(args) -> None:
    data = Path(args.data).resolve()
    hw = hwmod.detect()
    if args.unpair:
        from .pairing import Identity

        Identity(data / "pairing.json").set_peer(None)
        print(f"Pairing forgotten ({data / 'pairing.json'}). Restart the service.")
        return
    log.info("PiPair %s (%s) on %s; data in %s", __version__, args.role,
             hw["model"] or "non-Pi host", data)
    if args.role == "head":
        await run_head(args, data, hw)
        return
    store = Store(data / "state.json")
    library = MediaLibrary(data / "media", data / "cache", hw)
    port = inner_head = None
    if args.role == "standalone":
        factory = make_backend_factory(args, hw)
    else:
        from .backend_pair import HeadPort, PairBackend

        kind = args.link
        if kind == "auto":
            kind = "ble" if hw["is_pi"] and args.backend != "sim" else "loopback"
        from .pairing import ControllerLink, HeadLink, Identity

        if kind == "ble":
            from .ble_central import BleCentralLink

            radio = BleCentralLink()
        else:
            # Development: a head in this process, sharing the media folder.
            # It starts out unpaired, like a real one: pair it in the web UI.
            from .head import Head
            from .link import LoopbackLink

            radio, head_radio = LoopbackLink.pair()
            head_link = HeadLink(head_radio, Identity(data / "head-pairing.json"),
                                 "simulated head")
            inner_head = Head(Store(data / "head-state.json"), library,
                              make_backend_factory(args, hw), hw, head_link)
            head_link.on_notice = inner_head.set_notice
            log.info("no Bluetooth: running a head inside this process")
        link = ControllerLink(radio, Identity(data / "pairing.json"), socket.gethostname())
        port = HeadPort(link)
        audio = make_audio_factory(args, hw)
        factory = lambda settings, hw_, emit: PairBackend(settings, hw_, emit, audio, port)  # noqa: E731
    engine = Engine(store, library, factory, hw)
    gpi = GpiManager(store, engine)
    app = build_app(store, library, engine, hw, gpi, port)
    # Don't let lingering HTTP connections hold up shutdown (aiohttp's default
    # is 60 s, longer than systemd waits); the parameter moved between versions.
    runner_kw = {"shutdown_timeout": 2.0} if _accepts(web.AppRunner, "shutdown_timeout") else {}
    site_kw = {"shutdown_timeout": 2.0} if _accepts(web.TCPSite, "shutdown_timeout") else {}
    runner = web.AppRunner(app, access_log=None, **runner_kw)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port, **site_kw)
    await site.start()
    log.info("web UI on http://%s:%d/", args.host, args.port)
    await engine.start(autoplay=not args.no_autoplay)
    if inner_head:
        await inner_head.start()
    if port:
        await port.start()
    await gpi.start()

    await wait_for_stop()
    loop = asyncio.get_running_loop()
    t = loop.time()
    await runner.cleanup()
    if port:
        await port.stop()
    if inner_head:
        await inner_head.shutdown()
    log.info("stopped in %.1f s", loop.time() - t)


def _accepts(cls, param: str) -> bool:
    import inspect

    try:
        return param in inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # Most of the work happens in C (GStreamer, libdrm via ctypes), where a bug
    # kills the process with SIGSEGV and no Python traceback. faulthandler
    # prints the Python stack of every thread to stderr first, which systemd
    # captures in the journal -- usually enough to place the crash without a
    # core dump. Costs nothing while the process is healthy.
    faulthandler.enable()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
