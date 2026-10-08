"""BLE transport for the controller: scans for the head and connects to it.

The controller is the GATT client (central). It writes packets to the head's
RX characteristic without waiting for a response and receives packets as
notifications on TX. If the connection drops it scans and connects again,
forever.
"""

from __future__ import annotations

import asyncio
import logging

from .link import MAX_PACKET, MIN_PACKET, RX_UUID, SERVICE_UUID, TX_UUID, QueuedLink

log = logging.getLogger(__name__)

SCAN_SECONDS = 8.0
CONNECT_TIMEOUT = 15.0
RETRY_SECONDS = 1.0


class BleCentralLink(QueuedLink):
    kind = "ble"

    def __init__(self, peer: str = ""):
        """`peer` picks one head by Bluetooth name or address; empty = any PiPair head."""
        super().__init__()
        self.peer = peer.strip().lower()
        self.state = "starting"  # starting | searching | connecting | connected | error
        self.device_name: str | None = None
        self.device_address: str | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._set_connected(False)

    def _match(self, device, adv) -> bool:
        if SERVICE_UUID not in [u.lower() for u in (adv.service_uuids or [])]:
            return False
        if not self.peer:
            return True
        names = {(device.address or "").lower(), (adv.local_name or "").lower(),
                 (device.name or "").lower()}
        return self.peer in names

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
                device = await BleakScanner.find_device_by_filter(
                    self._match, timeout=SCAN_SECONDS, service_uuids=[SERVICE_UUID])
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
                    self._set_connected(True)

                    async def write(packet: bytes) -> None:
                        await client.write_gatt_char(RX_UUID, packet, response=False)

                    pump = asyncio.create_task(self._pump(write))
                    waiter = asyncio.create_task(lost.wait())
                    done, _ = await asyncio.wait({pump, waiter},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    waiter.cancel()
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
        return {**super().info(), "state": self.state, "peer": self.peer or None,
                "device": self.device_name, "address": self.device_address,
                "last_error": self.last_error}
