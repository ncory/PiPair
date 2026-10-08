"""BLE transport for the head: a GATT server registered with BlueZ over D-Bus.

The head is the peripheral. It advertises the PiPair service and waits for
the controller to connect. Packets from the controller arrive as writes to
the RX characteristic; packets to the controller leave as notifications on
TX. No pairing or bonding is involved.

Note: no ``from __future__ import annotations`` here. dbus-fast reads the
D-Bus type signatures from the method annotations, and they must stay plain
strings.
"""

import asyncio
import logging
import socket

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from .link import MAX_PACKET, MIN_PACKET, RX_UUID, SERVICE_UUID, TX_UUID, QueuedLink

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
BASE = "/org/pipair"
SERVICE_PATH = BASE + "/service0"
RETRY_SECONDS = 5.0


class _Service(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.GattService1")

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return SERVICE_UUID

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True


class _Characteristic(ServiceInterface):
    def __init__(self, uuid, flags, on_write=None, on_notify=None):
        super().__init__("org.bluez.GattCharacteristic1")
        self.uuid = uuid
        self.flags = flags
        self.on_write = on_write
        self.on_notify = on_notify
        self.value = b""

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return SERVICE_PATH

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self.flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return self.value

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        if self.on_write:
            mtu = options.get("mtu")
            self.on_write(bytes(value), mtu.value if mtu is not None else None)

    @method()
    def StartNotify(self):
        if self.on_notify:
            self.on_notify(True)

    @method()
    def StopNotify(self):
        if self.on_notify:
            self.on_notify(False)

    def notify(self, packet):
        self.value = packet
        self.emit_properties_changed({"Value": packet})


class _Advertisement(ServiceInterface):
    def __init__(self, name):
        super().__init__("org.bluez.LEAdvertisement1")
        self.name = name

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [SERVICE_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.name

    # Without the "general discoverable" flag BlueZ scanners ignore the advert.
    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return True

    @method()
    def Release(self):
        log.warning("BlueZ released the advertisement")


class BlePeripheralLink(QueuedLink):
    kind = "ble"

    def __init__(self, name=None, adapter="hci0"):
        super().__init__()
        # Kept short: the name shares the 31-byte scan response.
        self.name = (name or socket.gethostname())[:20]
        self.adapter_path = f"/org/bluez/{adapter}"
        self.state = "starting"  # starting | advertising | connected | error
        self.last_error = None
        self._task = None
        self._pump_task = None
        self._bus = None
        self._tx = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        for t in (self._task, self._pump_task):
            if t:
                t.cancel()
        if self._bus:
            self._bus.disconnect()
        self._set_connected(False)

    async def _run(self):
        """Register with BlueZ, and again whenever the D-Bus connection is lost."""
        while True:
            try:
                await self._register()
                self.state, self.last_error = "advertising", None
                log.info("advertising as '%s'", self.name)
                await self._bus.wait_for_disconnect()
                log.warning("lost the D-Bus connection to BlueZ")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - D-Bus errors of every kind
                self.state, self.last_error = "error", f"{type(e).__name__}: {e}"
                log.warning("Bluetooth setup failed: %s", self.last_error)
            self._drop()
            await asyncio.sleep(RETRY_SECONDS)

    async def _register(self):
        bus = self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        rx = _Characteristic(RX_UUID, ["write-without-response", "write"], on_write=self._on_write)
        self._tx = _Characteristic(TX_UUID, ["notify"], on_notify=self._on_subscribe)
        bus.export(SERVICE_PATH, _Service())
        bus.export(SERVICE_PATH + "/char0", rx)
        bus.export(SERVICE_PATH + "/char1", self._tx)
        bus.export(BASE + "/adv0", _Advertisement(self.name))

        intro = await bus.introspect(BLUEZ, self.adapter_path)
        adapter = bus.get_proxy_object(BLUEZ, self.adapter_path, intro)
        props = adapter.get_interface("org.freedesktop.DBus.Properties")
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await adapter.get_interface("org.bluez.GattManager1").call_register_application(BASE, {})
        await adapter.get_interface("org.bluez.LEAdvertisingManager1") \
            .call_register_advertisement(BASE + "/adv0", {})

    # -- BlueZ callbacks -------------------------------------------------------
    def _on_write(self, packet, mtu):
        if mtu:
            self.max_packet = max(MIN_PACKET, min(int(mtu) - 3, MAX_PACKET))
        self._packet_in(packet)

    def _on_subscribe(self, on):
        """The controller (un)subscribed to notifications: that is our connect signal."""
        if on:
            self._drop()
            self.state = "connected"
            self._pump_task = asyncio.create_task(self._pump(self._tx.notify))
            self._set_connected(True)
        else:
            self._drop()
            self.state = "advertising"

    def _drop(self):
        if self._pump_task:
            self._pump_task.cancel()
            self._pump_task = None
        self._flush()
        self.max_packet = MIN_PACKET
        self._set_connected(False)

    def info(self):
        return {**super().info(), "state": self.state, "name": self.name,
                "last_error": self.last_error}
