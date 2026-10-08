"""Wire format, clock sync and the loopback link."""

from __future__ import annotations

import asyncio
import random

from pipair.link import (ClockSync, LoopbackLink, Reassembler, decode, encode, fragment)


def roundtrip(msg: dict, blob: bytes, max_packet: int) -> tuple[dict, bytes]:
    r = Reassembler()
    out = None
    for p in fragment(encode(msg, blob), max_packet):
        assert len(p) <= max_packet
        assert out is None  # only the last packet completes the message
        out = r.feed(p)
    assert out is not None
    return decode(out)


def test_messages_survive_any_packet_size():
    msg = {"t": "alpha", "id": 12, "k": [[1234.5678, 0.0], [1235.5678, 1.0]]}
    blob = bytes(range(256)) * 9
    for size in (20, 21, 100, 244):
        assert roundtrip(msg, b"", size) == (msg, b"")
        assert roundtrip(msg, blob, size) == (msg, blob)


def test_reassembler_ignores_a_message_it_joined_halfway():
    first, second = encode({"n": 1, "pad": "x" * 100}), encode({"n": 2})
    packets = fragment(first, 20)[2:] + fragment(second, 20)
    r = Reassembler()
    got = [d for d in (r.feed(p) for p in packets) if d is not None]
    assert [decode(d)[0]["n"] for d in got] == [2]


def test_clock_sync_finds_the_offset_despite_jitter():
    rng = random.Random(7)
    true_offset = 5021.337  # head clock - controller clock
    sync = ClockSync()
    t = 100.0
    for _ in range(24):
        out, back = 0.004 + rng.random() * 0.03, 0.004 + rng.random() * 0.03
        t0 = t
        t1 = t0 + out + true_offset
        t2 = t1 + 0.001
        t3 = t2 - true_offset + back
        sync.add(t0, t1, t2, t3)
        t += 0.5
    assert sync.synced
    # The best sample's error is half the difference between its two legs.
    assert abs(sync.offset - true_offset) < 0.008


def test_clock_sync_rejects_impossible_samples():
    sync = ClockSync()
    sync.add(10.0, 500.0, 500.2, 10.1)  # the peer "held" the ping longer than the round trip
    assert not sync.samples


async def test_loopback_delivers_in_order_and_reports_state():
    a, b = LoopbackLink.pair(delay=0.001, max_packet=20)
    got, states = [], []
    b.set_handlers(lambda m, blob, rx: got.append((m, blob)), states.append)
    a.set_handlers(lambda m, blob, rx: None, lambda up: None)
    assert not a.send({"n": 0})  # not connected yet
    await a.start()
    await b.start()
    assert a.connected and b.connected and states == [True]
    for n in range(1, 6):
        assert a.send({"n": n, "pad": "y" * 50}, b"blob" if n == 3 else b"")
    await asyncio.sleep(0.05)
    assert [m["n"] for m, _ in got] == [1, 2, 3, 4, 5]
    assert got[2][1] == b"blob"
    a.set_up(False)
    assert states == [True, False] and not a.send({"n": 9})


async def test_stamp_is_applied_on_send():
    a, b = LoopbackLink.pair(delay=0.001)
    a.clock = lambda: 42.0
    got = []
    b.set_handlers(lambda m, blob, rx: got.append(m), lambda up: None)
    a.set_handlers(lambda m, blob, rx: None, lambda up: None)
    await a.start()
    await b.start()
    a.send({"t": "ping"}, stamp="t0")
    await asyncio.sleep(0.02)
    assert got == [{"t": "ping", "t0": 42.0}]
