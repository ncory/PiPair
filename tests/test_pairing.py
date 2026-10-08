"""Pairing and signed sessions, over the loopback link."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from pipair.link import LOOPBACK_ADDRESS, LoopbackLink, seal, unseal
from pipair.pairing import (X25519_BASE, ControllerLink, HeadLink, Identity, commitment,
                            x25519)


async def wait_for(pred, timeout=4.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def test_x25519_matches_rfc_7748():
    h = bytes.fromhex
    assert x25519(h("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
                  h("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")) == \
        h("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")
    alice = h("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    bob = h("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
    alice_pk, bob_pk = x25519(alice, X25519_BASE), x25519(bob, X25519_BASE)
    assert alice_pk == h("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    assert bob_pk == h("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
    assert x25519(alice, bob_pk) == x25519(bob, alice_pk) == \
        h("4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742")


def test_seal_rejects_tampering_and_wrong_keys():
    key = b"k" * 32
    sealed = seal(key, 7, b'{"t":"start"}')
    assert unseal(key, sealed) == (7, b'{"t":"start"}')
    assert unseal(b"x" * 32, sealed) is None
    flipped = bytearray(sealed)
    flipped[8] ^= 1
    assert unseal(key, bytes(flipped)) is None
    assert unseal(key, b'{"t":"start"}') is None


class Pair:
    """A controller and a head on a loopback radio, each with its own identity file."""

    def __init__(self, tmp: Path, controller: str = "ctrl", head: str = "head"):
        self.radio, self.head_radio = LoopbackLink.pair(delay=0.001)
        self.ctrl = ControllerLink(self.radio, Identity(tmp / f"{controller}.json"), "ctrl")
        self.head = HeadLink(self.head_radio, Identity(tmp / f"{head}.json"), "the-head")
        self.notices: list = []
        self.head.on_notice = self.notices.append
        self.to_head: list[dict] = []
        self.to_ctrl: list[dict] = []
        self.head.set_handlers(lambda m, b, rx: self.to_head.append(m), lambda up: None)
        self.ctrl.set_handlers(lambda m, b, rx: self.to_ctrl.append(m), lambda up: None)

    async def start(self):
        await self.head.start()
        await self.ctrl.start()
        return self

    async def stop(self):
        await self.ctrl.stop()
        await self.head.stop()

    async def do_pairing(self):
        self.ctrl.pair(LOOPBACK_ADDRESS)
        await wait_for(lambda: self.ctrl.state == "confirm")
        self.ctrl.confirm()
        await wait_for(lambda: self.ctrl.connected and self.head.connected)


async def test_pairing_shows_the_same_code_on_both_and_opens_a_session(tmp_path):
    p = await Pair(tmp_path).start()
    try:
        assert p.notices == [["the-head", "Ready to pair"]]
        assert not p.radio.connected  # an unpaired controller connects to nobody
        assert [h["address"] for h in p.ctrl.discovered()] == [LOOPBACK_ADDRESS]
        p.ctrl.pair(LOOPBACK_ADDRESS)
        await wait_for(lambda: p.ctrl.state == "confirm" and len(p.notices) == 2)
        code = p.ctrl.info()["pairing"]["code"]
        assert len(code) == 4 and code.isdigit()
        assert p.notices[-1] == ["Pairing code", code]
        assert not p.ctrl.connected and p.head.identity.peer is None  # not until the user says so
        p.ctrl.confirm()
        await wait_for(lambda: p.ctrl.connected and p.head.connected)
        assert p.notices[-1] is None  # a paired head shows no text
        assert p.ctrl.identity.peer["address"] == LOOPBACK_ADDRESS
        assert p.ctrl.identity.key == p.head.identity.key
        assert (tmp_path / "ctrl.json").stat().st_mode & 0o077 == 0
        assert p.ctrl.send({"t": "new", "id": 1}) and p.head.send({"t": "ev", "id": 1})
        await wait_for(lambda: p.to_head and p.to_ctrl)
        assert p.to_head == [{"t": "new", "id": 1}] and p.to_ctrl == [{"t": "ev", "id": 1}]
    finally:
        await p.stop()


async def test_a_paired_set_reconnects_without_asking(tmp_path):
    p = await Pair(tmp_path).start()
    await p.do_pairing()
    await p.stop()
    again = await Pair(tmp_path).start()  # same identity files: "after a reboot"
    try:
        await wait_for(lambda: again.ctrl.connected and again.head.connected)
        assert again.notices == [None]
    finally:
        await again.stop()


async def test_head_obeys_only_its_own_controller(tmp_path):
    p = await Pair(tmp_path).start()
    await p.do_pairing()
    await p.stop()
    # A second controller that believes it is paired with this head, with a key of its own.
    other = Identity(tmp_path / "other.json")
    other.set_peer({"id": Identity(tmp_path / "head.json").id, "key": "11" * 32,
                    "name": "the-head", "address": LOOPBACK_ADDRESS})
    intruder = await Pair(tmp_path, controller="other").start()
    try:
        await wait_for(lambda: intruder.ctrl.state == "rejected")
        assert not intruder.ctrl.connected and not intruder.head.connected
        assert "not paired with this controller" in intruder.ctrl.reason
        assert not intruder.radio.connected  # and it lets go of the head
        with pytest.raises(ValueError):
            intruder.ctrl.pair(LOOPBACK_ADDRESS)  # it thinks it has a head already
    finally:
        await intruder.stop()


async def test_a_paired_head_refuses_to_pair_again(tmp_path):
    p = await Pair(tmp_path).start()
    await p.do_pairing()
    await p.stop()
    fresh = await Pair(tmp_path, controller="newcomer").start()
    try:
        fresh.ctrl.pair(LOOPBACK_ADDRESS)
        await wait_for(lambda: fresh.ctrl.state == "unpaired" and fresh.ctrl.reason)
        assert "already paired" in fresh.ctrl.reason
        assert fresh.head.identity.peer["name"] == "ctrl"
    finally:
        await fresh.stop()


async def test_unsigned_and_replayed_messages_are_ignored_once_a_session_is_up(tmp_path):
    p = await Pair(tmp_path).start()
    try:
        await p.do_pairing()
        captured = []
        deliver = p.head_radio._deliver
        p.head_radio._deliver = lambda data, rx: (captured.append(data), deliver(data, rx))
        p.ctrl.send({"t": "start", "id": 1})
        await wait_for(lambda: len(p.to_head) == 1)
        p.radio.send({"t": "start", "id": 666})  # straight onto the radio, unsigned
        deliver(captured[0], 0.0)  # the genuine message, played back
        await asyncio.sleep(0.05)
        assert p.to_head == [{"t": "start", "id": 1}]
        p.ctrl.send({"t": "start", "id": 2})  # and the real controller is unaffected
        await wait_for(lambda: len(p.to_head) == 2)
    finally:
        await p.stop()


async def test_pairing_fails_if_the_key_does_not_match_the_commitment(tmp_path):
    p = await Pair(tmp_path).start()
    try:
        # Someone in the middle swaps the controller's key after it has committed.
        send = p.radio.send

        def meddle(msg, *a, **kw):
            if msg.get("t") == "p3":
                msg = {**msg, "pk": x25519(b"\x07" * 32, X25519_BASE).hex()}
            return send(msg, *a, **kw)

        p.radio.send = meddle
        p.ctrl.pair(LOOPBACK_ADDRESS)
        await wait_for(lambda: p.ctrl.state == "unpaired" and p.ctrl.reason)
        assert "did not check out" in p.ctrl.reason
        assert p.head.identity.peer is None and p.notices[-1] == ["the-head", "Ready to pair"]
        assert commitment(b"a" * 32, b"n" * 16) != commitment(b"b" * 32, b"n" * 16)
    finally:
        await p.stop()


async def test_cancel_and_unpair(tmp_path):
    p = await Pair(tmp_path).start()
    try:
        p.ctrl.pair(LOOPBACK_ADDRESS)
        await wait_for(lambda: p.ctrl.state == "confirm" and len(p.notices) == 2)
        p.ctrl.cancel()
        await wait_for(lambda: p.notices[-1] == ["the-head", "Ready to pair"]
                       and not p.radio.connected)
        assert p.ctrl.identity.peer is None and p.head.identity.peer is None

        await p.do_pairing()
        p.ctrl.unpair()
        await wait_for(lambda: p.head.identity.peer is None and not p.radio.connected)
        assert p.ctrl.identity.peer is None and p.ctrl.state == "unpaired"
        assert not p.ctrl.connected and not p.head.connected
        assert p.notices[-1] == ["the-head", "Ready to pair"]
        await p.do_pairing()  # and it can be paired afresh
    finally:
        await p.stop()


async def test_head_drops_a_caller_that_never_identifies_itself(tmp_path, monkeypatch):
    from pipair import pairing

    monkeypatch.setattr(pairing, "STRANGER_SECONDS", 0.1)
    radio, head_radio = LoopbackLink.pair(delay=0.001)
    head = HeadLink(head_radio, Identity(tmp_path / "head.json"), "the-head")
    radio.set_handlers(lambda m, b, rx: None, lambda up: None)
    await head.start()
    await radio.start()  # a bare radio: connects, says nothing
    try:
        assert head_radio.connected
        drops = []
        head_radio.drop = lambda: drops.append(1)
        await wait_for(lambda: drops)
    finally:
        await head.stop()
        await radio.stop()
