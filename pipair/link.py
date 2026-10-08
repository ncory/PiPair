"""The controller <-> head link: messages, framing and clock sync.

The two devices exchange small JSON messages (optionally with a binary blob
attached) over Bluetooth LE. This module holds everything that does not
depend on the radio: the wire format, the clock-offset estimator, the
``Link`` interface the rest of the code talks to, and an in-process loopback
used for development and tests. The BLE transports are in ble_central.py
(controller) and ble_peripheral.py (head).

Wire format. A message is UTF-8 JSON, or ``0x00 | len(json) u16 | json |
blob`` when it carries a blob. BLE packets are small, so a message travels as
one or more packets, each with a one-byte header saying whether it starts
and/or ends a message.

Time. Each device stamps with its own ``time.monotonic()``. The controller
pings the head several times a second and keeps an estimate of the difference
between the two clocks (see ClockSync), so it can tell the head "start this
at T" in the head's own time.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import struct
import time
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)

PROTOCOL = 1
SERVICE_UUID = "b5f90001-aa8d-4e1f-9c2b-3a7d5e6f1c40"
RX_UUID = "b5f90002-aa8d-4e1f-9c2b-3a7d5e6f1c40"  # controller -> head (write without response)
TX_UUID = "b5f90003-aa8d-4e1f-9c2b-3a7d5e6f1c40"  # head -> controller (notify)

F_START = 0x01
F_END = 0x02
MIN_PACKET = 20  # ATT payload at the default MTU of 23
MAX_PACKET = 244  # one LE data packet with data length extension
MAX_MESSAGE = 4 << 20
MAX_QUEUE = 400  # messages waiting to be sent before new ones are refused

SEALED = 0x01  # first byte of a signed message (plain ones start with "{" or 0x00)
TAG_BYTES = 8
LOOPBACK_ADDRESS = "sim:head"

MessageFn = Callable[[dict, bytes, float], None]
StateFn = Callable[[bool], None]


def encode(msg: dict, blob: bytes = b"") -> bytes:
    body = json.dumps(msg, separators=(",", ":")).encode()
    if not blob:
        return body
    return b"\x00" + struct.pack(">H", len(body)) + body + blob


def decode(data: bytes) -> tuple[dict, bytes]:
    if data[:1] == b"\x00":
        (n,) = struct.unpack(">H", data[1:3])
        msg, blob = json.loads(data[3:3 + n]), data[3 + n:]
    else:
        msg, blob = json.loads(data), b""
    if not isinstance(msg, dict):
        raise ValueError("message is not an object")
    return msg, blob


def seal(key: bytes, counter: int, data: bytes) -> bytes:
    """Sign one encoded message: marker, counter, the message, truncated HMAC."""
    head = bytes([SEALED]) + struct.pack(">I", counter)
    tag = hmac.new(key, head + data, hashlib.sha256).digest()[:TAG_BYTES]
    return head + data + tag


def unseal(key: bytes, sealed: bytes) -> tuple[int, bytes] | None:
    """(counter, message) if the signature is good, else None."""
    if len(sealed) < 5 + TAG_BYTES or sealed[0] != SEALED:
        return None
    body, tag = sealed[:-TAG_BYTES], sealed[-TAG_BYTES:]
    good = hmac.new(key, body, hashlib.sha256).digest()[:TAG_BYTES]
    if not hmac.compare_digest(tag, good):
        return None
    return struct.unpack(">I", body[1:5])[0], body[5:]


def fragment(data: bytes, max_packet: int) -> list[bytes]:
    """Split one encoded message into packets of at most `max_packet` bytes."""
    room = max(1, max_packet - 1)
    chunks = [data[i:i + room] for i in range(0, len(data), room)] or [b""]
    last = len(chunks) - 1
    return [bytes([(F_START if i == 0 else 0) | (F_END if i == last else 0)]) + c
            for i, c in enumerate(chunks)]


class Reassembler:
    """Rebuilds messages from packets. Packets arrive in order (ATT guarantees it)."""

    def __init__(self, limit: int = MAX_MESSAGE):
        self.limit = limit
        self.buf: bytearray | None = None

    def reset(self) -> None:
        self.buf = None

    def feed(self, packet: bytes) -> bytes | None:
        if not packet:
            return None
        flags = packet[0]
        if flags & F_START:
            self.buf = bytearray()
        if self.buf is None:
            return None  # joined in the middle of a message
        self.buf += packet[1:]
        if len(self.buf) > self.limit:
            self.buf = None
            return None
        if flags & F_END:
            data, self.buf = bytes(self.buf), None
            return data
        return None


class ClockSync:
    """Estimates (peer clock - local clock) from ping round trips.

    Each sample is the classic four timestamps: t0 sent, t1 received by the
    peer, t2 reply sent by the peer, t3 reply received. Radio and scheduling
    delays only ever add to a round trip, so the sample with the shortest
    round trip in the recent window has the least room for error; its offset
    is the estimate. The window is short enough that crystal drift (tens of
    ppm) stays well under a millisecond across it.
    """

    def __init__(self, window: int = 24, min_samples: int = 4):
        self.samples: deque[tuple[float, float]] = deque(maxlen=window)  # (rtt, offset)
        self.min_samples = min_samples

    def reset(self) -> None:
        self.samples.clear()

    def add(self, t0: float, t1: float, t2: float, t3: float) -> None:
        rtt = (t3 - t0) - (t2 - t1)
        if rtt < 0:
            return  # clocks stepped or a stale reply; not usable
        self.samples.append((rtt, ((t1 - t0) + (t2 - t3)) / 2))

    @property
    def synced(self) -> bool:
        return len(self.samples) >= self.min_samples

    @property
    def offset(self) -> float:
        return min(self.samples)[1] if self.samples else 0.0

    @property
    def rtt(self) -> float | None:
        return min(self.samples)[0] if self.samples else None

    def info(self) -> dict:
        if not self.samples:
            return {"synced": False, "rtt_ms": None, "rtt_median_ms": None}
        rtts = sorted(s[0] for s in self.samples)
        return {"synced": self.synced, "rtt_ms": round(rtts[0] * 1000, 1),
                "rtt_median_ms": round(rtts[len(rtts) // 2] * 1000, 1)}


class Link:
    """One end of the controller <-> head connection.

    Everything happens on the asyncio loop. ``send`` never blocks: it queues
    the message and returns False if there is no connection (or the queue is
    full), in which case the message is gone. ``stamp`` names a field that
    is set to this end's clock at the moment the message actually leaves, so
    time spent queued does not count against a ping.

    Once the two ends share session keys (see pairing.py) a message can be
    sent ``sealed``: signed and numbered, so the receiver can tell it came
    from its partner and is not a replay. ``rx_sealed`` says whether the
    message being delivered was.

    A controller's transport can also look around: ``discovered()`` lists
    the heads it can see, and ``target`` picks which one it connects to
    (None = any, "" = none, else that head's address).
    """

    kind = "none"

    def __init__(self) -> None:
        self.connected = False
        self.clock: Callable[[], float] = time.monotonic
        self.target: str | None = None
        self.rx_sealed = False
        self._on_message: MessageFn | None = None
        self._on_state: StateFn | None = None
        self._tx_key: bytes | None = None
        self._rx_key: bytes | None = None
        self._tx_n = 0
        self._rx_n = 0

    def set_handlers(self, on_message: MessageFn, on_state: StateFn) -> None:
        self._on_message = on_message
        self._on_state = on_state

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def send(self, msg: dict, blob: bytes = b"", stamp: str | None = None,
             sealed: bool = False) -> bool:
        raise NotImplementedError

    def info(self) -> dict:
        return {"transport": self.kind, "connected": self.connected}

    def discovered(self) -> list[dict]:
        return []

    def set_target(self, address: str | None) -> None:
        self.target = address

    def drop(self) -> None:
        """Disconnect whoever is connected. The transport may connect again."""

    def set_session(self, tx_key: bytes, rx_key: bytes) -> None:
        self._tx_key, self._rx_key = tx_key, rx_key
        self._tx_n = self._rx_n = 0

    def clear_session(self) -> None:
        self._tx_key = self._rx_key = None

    # -- for transports ------------------------------------------------------
    def _pack(self, msg: dict, blob: bytes, stamp: str | None, sealed: bool) -> bytes | None:
        """Encode a message as it leaves: stamp it, then sign it if asked."""
        if stamp:
            msg = {**msg, stamp: round(self.clock(), 5)}
        data = encode(msg, blob)
        if not sealed:
            return data
        if self._tx_key is None:
            return None  # the session ended while this was queued
        self._tx_n += 1
        return seal(self._tx_key, self._tx_n, data)

    def _deliver(self, data: bytes, rx: float) -> None:
        sealed = data[:1] == bytes([SEALED])
        if sealed:
            opened = unseal(self._rx_key, data) if self._rx_key else None
            if opened is None or opened[0] <= self._rx_n:
                log.warning("dropping a message that is not from our partner")
                return
            self._rx_n, data = opened
        try:
            msg, blob = decode(data)
        except (ValueError, struct.error):
            log.warning("dropping a malformed message (%d bytes)", len(data))
            return
        self.rx_sealed = sealed
        if self._on_message:
            try:
                self._on_message(msg, blob, rx)
            except Exception:  # noqa: BLE001
                log.exception("link message handler failed: %s", msg.get("t"))

    def _set_connected(self, up: bool) -> None:
        if up == self.connected:
            return
        self.connected = up
        log.info("link %s", "up" if up else "down")
        if self._on_state:
            try:
                self._on_state(up)
            except Exception:  # noqa: BLE001
                log.exception("link state handler failed")


class QueuedLink(Link):
    """A link whose transport writes packets one at a time from a sender task."""

    def __init__(self) -> None:
        super().__init__()
        self.max_packet = MIN_PACKET
        self.reasm = Reassembler()
        self._queue: deque[tuple[dict, bytes, str | None, bool]] = deque()
        self._wake = asyncio.Event()
        self.sent = 0
        self.received = 0

    def send(self, msg: dict, blob: bytes = b"", stamp: str | None = None,
             sealed: bool = False) -> bool:
        if not self.connected or len(self._queue) >= MAX_QUEUE:
            return False
        self._queue.append((msg, blob, stamp, sealed))
        self._wake.set()
        return True

    def _packet_in(self, packet: bytes) -> None:
        rx = self.clock()
        data = self.reasm.feed(packet)
        if data is not None:
            self.received += 1
            self._deliver(data, rx)

    def _flush(self) -> None:
        self._queue.clear()
        self.reasm.reset()
        self.clear_session()

    async def _pump(self, write: Callable[[bytes], object]) -> None:
        """Send queued messages through `write` (sync or async) until cancelled."""
        while True:
            while not self._queue:
                self._wake.clear()
                await self._wake.wait()
            data = self._pack(*self._queue.popleft())
            if data is None:
                continue
            for packet in fragment(data, self.max_packet):
                r = write(packet)
                if asyncio.iscoroutine(r):
                    await r
                else:
                    await asyncio.sleep(0)  # let other tasks run between packets
            self.sent += 1

    def info(self) -> dict:
        return {**super().info(), "packet_bytes": self.max_packet,
                "messages_sent": self.sent, "messages_received": self.received}


class LoopbackLink(Link):
    """Two ends joined in-process, for development and tests.

    Messages go through the real encoder, packetizer and reassembler, and
    arrive after `delay` seconds. Each end has its own clock, so a test can
    give the two ends clocks that disagree. The "head" end is discoverable
    under LOOPBACK_ADDRESS, and the link honours `target` like a radio does.
    """

    kind = "loopback"

    def __init__(self, delay: float = 0.002, max_packet: int = MAX_PACKET):
        super().__init__()
        self.delay = delay
        self.max_packet = max_packet
        self.peer: LoopbackLink | None = None
        self.reasm = Reassembler()
        self.started = False
        self.cut = False

    @classmethod
    def pair(cls, delay: float = 0.002,
             max_packet: int = MAX_PACKET) -> tuple[LoopbackLink, LoopbackLink]:
        a, b = cls(delay, max_packet), cls(delay, max_packet)
        a.peer, b.peer = b, a
        return a, b

    async def start(self) -> None:
        self.started = True
        self._refresh()

    async def stop(self) -> None:
        self.started = False
        self._refresh()

    def set_up(self, up: bool) -> None:
        """Connect or cut the link (both ends see it)."""
        for end in (self, self.peer):
            if end:
                end.cut = not up
        self._refresh()

    def set_target(self, address: str | None) -> None:
        self.target = address
        self._refresh()

    def drop(self) -> None:
        if self.connected:
            self._apply(False)
            asyncio.get_running_loop().call_later(0.05, self._refresh)

    def discovered(self) -> list[dict]:
        if not (self.peer and self.peer.started):
            return []
        return [{"address": LOOPBACK_ADDRESS, "name": "simulated head", "rssi": -40,
                 "connected": self.connected}]

    def _refresh(self) -> None:
        ends = [e for e in (self, self.peer) if e]
        up = (len(ends) == 2 and all(e.started and not e.cut for e in ends)
              and all(e.target in (None, LOOPBACK_ADDRESS) for e in ends))
        self._apply(up)

    def _apply(self, up: bool) -> None:
        for end in (self, self.peer):
            if end and end.connected != up:
                end.reasm.reset()
                end.clear_session()
                end._set_connected(up)

    def send(self, msg: dict, blob: bytes = b"", stamp: str | None = None,
             sealed: bool = False) -> bool:
        peer = self.peer
        if not self.connected or peer is None:
            return False
        data = self._pack(msg, blob, stamp, sealed)
        if data is None:
            return False
        packets = fragment(data, self.max_packet)
        asyncio.get_running_loop().call_later(self.delay, peer._receive, packets)
        return True

    def _receive(self, packets: list[bytes]) -> None:
        if not self.connected:
            return
        rx = self.clock()
        for p in packets:
            data = self.reasm.feed(p)
            if data is not None:
                self._deliver(data, rx)
