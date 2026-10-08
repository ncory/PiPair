"""BLE transport for the controller: looks for heads and connects to one.

The controller is the GATT client (central). It scans for devices offering
the PiPair service and keeps a list of the ones it has seen. `target` says
which of them to connect to: one head's Bluetooth address, "" for none (just
keep looking), or None for whichever turns up first. Once connected it
writes packets to the head's RX characteristic without waiting for a
response and receives packets as notifications on TX. If the connection
drops it goes back to scanning, forever.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .link import MAX_PACKET, MIN_PACKET, RX_UUID, SERVICE_UUID, TX_UUID, QueuedLink

log = logging.getLogger(__name__)

SCAN_SECONDS = 6.0
CONNECT_TIMEOUT = 15.0
RETRY_SECONDS = 1.0
SEEN_SECONDS = 45.0  # a head not heard from for this long leaves the list


class BleCentralLink(QueuedLink):
    kind = "ble"

    def __init__(self, target: str | None = None):
        super().__init__()
        self.target = target
        self.state = "starting"  # starting | searching | connecting | connected | error
        self.device_name: str | None = None
        self.device_address: str | None = None
        self.last_error: str | None = None
        self.seen: dict[str, dict] = {}  # address -> what its advertisement said
        self._task: asyncio.Task | None = None
        self._retarget = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._set_connected(False)

    def set_target(self, address: str | None) -> None:
        if address != self.target:
            self.target = address
            self._retarget.set()  # ends a scan, or a connection to the wrong head

    def drop(self) -> None:
        self._retarget.set()

    def discovered(self) -> list[dict]:
        now = time.monotonic()
        live = self.device_address if self.connected else None
        return sorted(({"address": a, "name": d["name"], "rssi": d["rssi"],
                        "connected": a == live}
                       for a, d in self.seen.items()
                       if a == live or now - d["t"] < SEEN_SECONDS),
                      key=lambda d: -(d["rssi"] or -999))

    def _wanted(self, address: str) -> bool:
        if self.target is None:
            return True
        return bool(self.target) and address.lower() == self.target.lower()

    async def _find(self, scanner_cls):
        """Scan for a while, noting every head; return the one we want, if seen."""
        hits: list = []
        found = asyncio.Event()

        def on_advert(device, adv) -> None:
            if SERVICE_UUID not in [u.lower() for u in (adv.service_uuids or [])]:
                return
            self.seen[device.address] = {"name": adv.local_name or device.name,
                                         "rssi": adv.rssi, "t": time.monotonic()}
            if self._wanted(device.address) and not hits:
                hits.append(device)
                found.set()

        self._retarget.clear()
        async with scanner_cls(detection_callback=on_advert, service_uuids=[SERVICE_UUID]):
            waits = [asyncio.create_task(found.wait()), asyncio.create_task(self._retarget.wait())]
            await asyncio.wait(waits, timeout=SCAN_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            for w in waits:
                w.cancel()
        return hits[0] if hits and self._wanted(hits[0].address) else None

    async def _run(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as e:
            self.state, self.last_error = "error", f"bleak is not installed: {e}"
            log.error("Bluetooth link unavailable: %s", self.last_error)
            return
        loop = asyncio.get_running_loop()
        while True:
            pump: asyncio.Task | None = None
            try:
                self.state = "searching"
                device = await self._find(BleakScanner)
                if device is None:
                    continue
                self.state = "connecting"
                self.device_name, self.device_address = device.name, device.address
                log.info("found head %s (%s); connecting", device.name, device.address)
                lost = asyncio.Event()
                client = BleakClient(
                    device, timeout=CONNECT_TIMEOUT,
                    disconnected_callback=lambda _c: loop.call_soon_threadsafe(lost.set))
                async with client:
                    self.max_packet = self._packet_size(client)
                    await client.start_notify(TX_UUID, lambda _ch, data: self._packet_in(bytes(data)))
                    self.state, self.last_error = "connected", None
                    self._retarget.clear()
                    self._set_connected(True)

                    async def write(packet: bytes) -> None:
                        await client.write_gatt_char(RX_UUID, packet, response=False)

                    pump = asyncio.create_task(self._pump(write))
                    waits = {pump, asyncio.create_task(lost.wait()),
                             asyncio.create_task(self._retarget.wait())}
                    done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                    for w in pending - {pump}:
                        w.cancel()
                    if pump in done and pump.exception():
                        raise pump.exception()
                log.info("head disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - BlueZ/D-Bus errors of every kind
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("Bluetooth link: %s", self.last_error)
            finally:
                if pump:
                    pump.cancel()
                self._flush()
                self._set_connected(False)
            self.state = "searching"
            await asyncio.sleep(RETRY_SECONDS)

    @staticmethod
    def _packet_size(client) -> int:
        ch = client.services.get_characteristic(RX_UUID)
        if ch is None:
            raise RuntimeError("the device does not offer the PiPair service")
        n = getattr(ch, "max_write_without_response_size", MIN_PACKET) or MIN_PACKET
        return max(MIN_PACKET, min(int(n), MAX_PACKET))

    def info(self) -> dict:
        return {**super().info(), "state": self.state, "device": self.device_name,
                "address": self.device_address, "last_error": self.last_error}
