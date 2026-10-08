"""Pairing: one controller, one head, and nobody else.

Several PiPair sets can run in the same room. Each controller must drive only
its own head, and a head must obey only its own controller. Both follow from
a secret the two share, set up once by pairing and kept in ``pairing.json``.

Pairing (the head must be unpaired):

    controller                                head
    p1  id, name, commit=H(pk_c | n_c)  ─►
                                        ◄─    p2  id, name, pk_h, n_h
    p3  pk_c, n_c                       ─►    (checks the commitment)
        both: key = HKDF(X25519(...)), code = 4 digits of HMAC(key)
        the head shows the code on its display; the UI shows the controller's
    p4  HMAC(key, "confirm-c")          ─►    once the user says they match
                                        ◄─    p5  HMAC(key, "confirm-h")

    The commitment is what makes four digits enough: someone in the middle
    has to fix their own key before seeing ours, so they get one blind guess
    at making the two codes agree.

Every connection after that:

    a1  id, n_c                         ─►
                                        ◄─    a2  id, n_h, HMAC(key, "auth-h" | n_c | n_h)
    a3  HMAC(key, "auth-c" | n_c | n_h) ─►
        both derive two session keys; from here every message is sealed
        (signed and numbered, see link.py). Messages are not encrypted:
        nothing in them is secret.

ControllerLink and HeadLink wrap a transport and look like a Link to the code
above them. That code sees "connected" only once the partner has proved
itself, so nothing else in PiPair needs to know about any of this.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Callable

from .link import Link

log = logging.getLogger(__name__)

PAIRING_SECONDS = 120.0  # how long a pairing may wait for the user
STRANGER_SECONDS = 10.0  # a connection that hasn't proved itself by then is dropped
RETRY_SECONDS = 15.0  # controller: wait before trying a head that turned us away
CONFIRM_SECONDS = 10.0

# ----------------------------------------------------------------- crypto
# X25519 as specified in RFC 7748, in plain Python. It is used a handful of
# times in a device's life, with keys that are thrown away afterwards, so
# neither its speed nor its timing matters; in exchange PiPair needs no
# cryptography package.
_P = 2 ** 255 - 19
_A24 = 121665
X25519_BASE = (9).to_bytes(32, "little")


def x25519(scalar: bytes, u: bytes) -> bytes:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k_int = int.from_bytes(k, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (k_int >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def hkdf(secret: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt, secret, hashlib.sha256).digest()
    out, block, n = b"", b"", 0
    while len(out) < length:
        n += 1
        block = hmac.new(prk, block + info + bytes([n]), hashlib.sha256).digest()
        out += block
    return out[:length]


def _mac(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"|".join(parts), hashlib.sha256).digest()


def commitment(pk: bytes, nonce: bytes) -> str:
    return hashlib.sha256(pk + nonce).hexdigest()


def pairing_key(secret_key: bytes, their_pk: bytes, cid: str, hid: str, pk_c: bytes,
                pk_h: bytes, n_c: bytes, n_h: bytes) -> bytes | None:
    shared = x25519(secret_key, their_pk)
    if not any(shared):
        return None  # a degenerate public key: refuse
    info = b"pipair-pair-v1|" + cid.encode() + b"|" + hid.encode() + b"|" + pk_c + pk_h
    return hkdf(shared, n_c + n_h, info)


def pairing_code(key: bytes) -> str:
    return f"{int.from_bytes(_mac(key, b'code')[:4], 'big') % 10000:04d}"


def session_keys(key: bytes, n_c: bytes, n_h: bytes) -> tuple[bytes, bytes]:
    """(controller -> head, head -> controller)"""
    return _mac(key, b"c2h", n_c, n_h), _mac(key, b"h2c", n_c, n_h)


def _unhex(value: object, length: int) -> bytes:
    data = bytes.fromhex(value) if isinstance(value, str) else b""
    if len(data) != length:
        raise ValueError("bad field")
    return data


# ---------------------------------------------------------------- identity
class Identity:
    """This device's id and its partner, kept in a file only we can read."""

    def __init__(self, path: Path):
        self.path = path
        self.id = secrets.token_hex(8)
        self.peer: dict | None = None  # {"id", "key" (hex), "name", "address"?}
        try:
            data = json.loads(path.read_text())
            self.id = data["id"]
            self.peer = data.get("peer") or None
        except (OSError, ValueError, KeyError):
            self._save()

    @property
    def key(self) -> bytes | None:
        return bytes.fromhex(self.peer["key"]) if self.peer else None

    def set_peer(self, peer: dict | None) -> None:
        self.peer = peer
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"id": self.id, "peer": self.peer}, f, indent=2)
        os.replace(tmp, self.path)


# ------------------------------------------------------------------- links
class _SecureLink(Link):
    """What ControllerLink and HeadLink share: wrapping a transport."""

    def __init__(self, inner: Link, identity: Identity, name: str):
        super().__init__()
        self.inner = inner
        self.identity = identity
        self.name = name
        self.kind = inner.kind
        self._listeners: list[Callable[[], None]] = []
        self._timer: asyncio.TimerHandle | None = None
        inner.set_handlers(self._inner_message, self._inner_state)

    # Each end has one clock, and it is the transport's.
    @property
    def clock(self) -> Callable[[], float]:
        return self.inner.clock

    @clock.setter
    def clock(self, fn: Callable[[], float]) -> None:
        if hasattr(self, "inner"):
            self.inner.clock = fn

    async def start(self) -> None:
        await self.inner.start()

    async def stop(self) -> None:
        self._cancel_timer()
        await self.inner.stop()

    def send(self, msg: dict, blob: bytes = b"", stamp: str | None = None,
             sealed: bool = True) -> bool:
        return self.connected and self.inner.send(msg, blob, stamp, sealed=True)

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("pairing listener failed")

    def _plain(self, msg: dict) -> None:
        self.inner.send(msg)

    def _later(self, delay: float, fn: Callable[[], None]) -> None:
        self._cancel_timer()
        self._timer = asyncio.get_running_loop().call_later(delay, fn)

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _inner_state(self, up: bool) -> None:
        raise NotImplementedError

    def _handle(self, msg: dict) -> None:
        raise NotImplementedError

    def _inner_message(self, msg: dict, blob: bytes, rx: float) -> None:
        if self.inner.rx_sealed:
            if self.connected and not self._sealed(msg) and self._on_message:
                self._on_message(msg, blob, rx)
            return
        if self.connected:
            return  # once a session is up, only our partner's signed messages count
        try:
            self._handle(msg)
        except (KeyError, ValueError, TypeError):
            log.warning("ignoring a malformed %r message", msg.get("t"))

    def _sealed(self, msg: dict) -> bool:
        """A signed message meant for this layer. True if it was consumed."""
        return False


class ControllerLink(_SecureLink):
    """The controller's side: finds its head, proves itself, and can pair with a new one."""

    def __init__(self, inner: Link, identity: Identity, name: str = "controller"):
        super().__init__(inner, identity, name)
        # unpaired | searching | authenticating | ready | rejected | pairing | confirm
        self.state = "searching" if identity.peer else "unpaired"
        self.reason: str | None = None
        self._pairing: dict | None = None
        self._nonce = b""
        inner.set_target(identity.peer["address"] if identity.peer else "")

    # ------------------------------------------------------------- commands
    def discovered(self) -> list[dict]:
        return self.inner.discovered()

    def pair(self, address: str) -> None:
        """Start pairing with the head at `address`. The code arrives via info()."""
        if self.identity.peer:
            raise ValueError("already paired; forget the current head first")
        seen = {h["address"]: h for h in self.inner.discovered()}
        if address not in seen:
            raise KeyError(address)
        sk = secrets.token_bytes(32)
        self._pairing = {"address": address, "name": seen[address].get("name"), "sk": sk,
                         "pk": x25519(sk, X25519_BASE), "n": secrets.token_bytes(16),
                         "code": None, "key": None}
        self._set_state("pairing")
        self._later(PAIRING_SECONDS, lambda: self.cancel("timed out"))
        if self.inner.connected and self.inner.target == address:
            self._send_p1()
        else:
            self.inner.set_target(address)

    def confirm(self) -> None:
        """The user says the two codes match."""
        p = self._pairing
        if not p or self.state != "confirm":
            raise ValueError("no pairing is waiting for confirmation")
        self._plain({"t": "p4", "m": _mac(p["key"], b"confirm-c").hex()})
        self._later(CONFIRM_SECONDS, lambda: self.cancel("the head did not answer"))

    def cancel(self, why: str = "cancelled") -> None:
        if not self._pairing:
            return
        self._plain({"t": "px", "why": why})
        self._pairing = None
        self._cancel_timer()
        self.reason = None if why == "cancelled" else f"Pairing failed: {why}"
        self._set_state("unpaired")
        # Leave the head free for someone else, once the "px" has gone out.
        self._later(0.3, lambda: self.inner.set_target(""))

    def unpair(self) -> None:
        """Forget the head (and tell it to forget us, if it can hear)."""
        if not self.identity.peer:
            return
        if self.connected:
            self.inner.send({"t": "unpair"}, sealed=True)
        self.identity.set_peer(None)
        self._cancel_timer()
        self.reason = None
        self._set_connected(False)
        self._set_state("unpaired")
        self._later(0.3, self._release)

    def _release(self) -> None:
        self.inner.clear_session()
        self.inner.set_target("")

    def _set_state(self, state: str) -> None:
        self.state = state
        self._changed()

    # ------------------------------------------------------------ transport
    def _inner_state(self, up: bool) -> None:
        if not up:
            self.inner.clear_session()
            self._set_connected(False)
            if self._pairing and self.state == "confirm":
                self.cancel("the connection was lost")
            elif self.state in ("authenticating", "ready"):
                self._set_state("searching")
            return
        if self._pairing:
            self._send_p1()
        elif self.identity.peer:
            self._nonce = secrets.token_bytes(16)
            self._set_state("authenticating")
            self._plain({"t": "a1", "c": self.identity.id, "n": self._nonce.hex()})

    def _send_p1(self) -> None:
        p = self._pairing
        self._plain({"t": "p1", "c": self.identity.id, "name": self.name,
                     "commit": commitment(p["pk"], p["n"])})

    def _handle(self, msg: dict) -> None:
        t = msg.get("t")
        p = self._pairing
        if t == "a2" and self.state == "authenticating":
            if msg.get("st"):
                self._turned_away("the head is not paired with this controller"
                                  if msg["st"] == "other" else "the head has been reset")
                return
            key, n_h = self.identity.key, _unhex(msg["n"], 16)
            good = _mac(key, b"auth-h", self._nonce, n_h)
            if msg.get("h") != self.identity.peer["id"] or \
                    not hmac.compare_digest(_unhex(msg["m"], 32), good):
                self._turned_away("the head could not prove who it is")
                return
            self._plain({"t": "a3", "m": _mac(key, b"auth-c", self._nonce, n_h).hex()})
            self.inner.set_session(*session_keys(key, self._nonce, n_h))
            self.reason = None
            self.state = "ready"
            self._set_connected(True)
            self._changed()
        elif t == "ax" and self.state == "authenticating":
            self._turned_away("the head refused this controller")
        elif t == "p2" and p and self.state == "pairing":
            pk_h, n_h = _unhex(msg["pk"], 32), _unhex(msg["n"], 16)
            hid = str(msg["h"])
            key = pairing_key(p["sk"], pk_h, self.identity.id, hid, p["pk"], pk_h, p["n"], n_h)
            if key is None:
                self.cancel("the head sent an invalid key")
                return
            p.update(hid=hid, name=msg.get("name") or p["name"], key=key,
                     code=pairing_code(key))
            self._plain({"t": "p3", "pk": p["pk"].hex(), "n": p["n"].hex()})
            self._set_state("confirm")
        elif t == "p5" and p and self.state == "confirm":
            if not hmac.compare_digest(_unhex(msg["m"], 32), _mac(p["key"], b"confirm-h")):
                self.cancel("the head's confirmation was wrong")
                return
            self.identity.set_peer({"id": p["hid"], "key": p["key"].hex(), "name": p["name"],
                                    "address": p["address"]})
            self._pairing = None
            self._cancel_timer()
            log.info("paired with head %s (%s)", p["name"], p["address"])
            self._inner_state(True)  # straight on to a signed session
        elif t == "px" and p:
            why = {"paired": "that head is already paired with another controller"}.get(
                msg.get("why"), str(msg.get("why") or "the head refused"))
            self._pairing = None
            self._cancel_timer()
            self.reason = f"Pairing failed: {why}"
            self._set_state("unpaired")
            self.inner.set_target("")

    def _turned_away(self, why: str) -> None:
        """Our head won't have us. Let go of it, and look again in a while."""
        log.warning("head rejected the session: %s", why)
        self.reason = why
        self._set_state("rejected")
        self.inner.set_target("")
        self._later(RETRY_SECONDS, self._retry)

    def _retry(self) -> None:
        if self.identity.peer and self.state == "rejected":
            self._set_state("searching")
            self.inner.set_target(self.identity.peer["address"])

    def info(self) -> dict:
        peer, p = self.identity.peer, self._pairing
        return {**self.inner.info(), "connected": self.connected,
                "radio": self.inner.connected, "pair_state": self.state, "reason": self.reason,
                "paired": {"name": peer.get("name"), "address": peer.get("address")}
                if peer else None,
                "pairing": {"address": p["address"], "name": p["name"], "code": p["code"]}
                if p else None}


class HeadLink(_SecureLink):
    """The head's side: obeys its controller only, and shows a code when pairing."""

    def __init__(self, inner: Link, identity: Identity, name: str = "head"):
        super().__init__(inner, identity, name)
        self.on_notice: Callable[[list[str] | None], None] | None = None
        self._auth: tuple[bytes, bytes] | None = None  # nonces of a session being set up
        self._pairing: dict | None = None
        self._stranger: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        self._notice()
        await super().start()

    async def stop(self) -> None:
        if self._stranger:
            self._stranger.cancel()
        await super().stop()

    def forget(self) -> None:
        self.identity.set_peer(None)
        self._set_connected(False)
        self.inner.clear_session()
        self._notice()
        self._changed()

    def _notice(self) -> None:
        """What the display should say (nothing at all once paired)."""
        if self.on_notice is None:
            return
        if self._pairing and self._pairing.get("code"):
            self.on_notice(["Pairing code", self._pairing["code"]])
        elif self.identity.peer is None:
            self.on_notice([self.name, "Ready to pair"])
        else:
            self.on_notice(None)

    # ------------------------------------------------------------ transport
    def _inner_state(self, up: bool) -> None:
        if self._stranger:
            self._stranger.cancel()
            self._stranger = None
        self._auth = None
        if up:
            self._stranger = asyncio.get_running_loop().call_later(
                STRANGER_SECONDS, self._check_stranger)
            return
        self.inner.clear_session()
        self._set_connected(False)
        self._end_pairing()

    def _check_stranger(self) -> None:
        self._stranger = None
        if self.inner.connected and not self.connected and not self._pairing:
            log.warning("dropping a connection that did not identify itself")
            self.inner.drop()

    def _end_pairing(self) -> None:
        if self._pairing:
            self._pairing = None
            self._cancel_timer()
            self._notice()

    def _handle(self, msg: dict) -> None:
        t = msg.get("t")
        peer, p = self.identity.peer, self._pairing
        if t == "a1":
            if peer is None or msg.get("c") != peer["id"]:
                self._plain({"t": "a2", "h": self.identity.id, "name": self.name,
                             "st": "unpaired" if peer is None else "other"})
                return
            n_c, n_h = _unhex(msg["n"], 16), secrets.token_bytes(16)
            self._auth = (n_c, n_h)
            self._plain({"t": "a2", "h": self.identity.id, "n": n_h.hex(),
                         "m": _mac(self.identity.key, b"auth-h", n_c, n_h).hex()})
        elif t == "a3" and self._auth and peer:
            n_c, n_h = self._auth
            self._auth = None
            key = self.identity.key
            if not hmac.compare_digest(_unhex(msg["m"], 32), _mac(key, b"auth-c", n_c, n_h)):
                self._plain({"t": "ax"})
                return
            c2h, h2c = session_keys(key, n_c, n_h)
            self.inner.set_session(h2c, c2h)
            self._set_connected(True)
        elif t == "p1":
            if peer is not None:
                self._plain({"t": "px", "why": "paired"})
                return
            sk = secrets.token_bytes(32)
            self._pairing = p = {"cid": str(msg["c"]), "name": str(msg.get("name") or ""),
                                 "commit": str(msg["commit"]), "sk": sk,
                                 "pk": x25519(sk, X25519_BASE), "n": secrets.token_bytes(16),
                                 "code": None, "key": None}
            self._later(PAIRING_SECONDS, self._end_pairing)
            self._plain({"t": "p2", "h": self.identity.id, "name": self.name,
                         "pk": p["pk"].hex(), "n": p["n"].hex()})
        elif t == "p3" and p and p["key"] is None:
            pk_c, n_c = _unhex(msg["pk"], 32), _unhex(msg["n"], 16)
            key = None
            if hmac.compare_digest(commitment(pk_c, n_c), p["commit"]):
                key = pairing_key(p["sk"], pk_c, p["cid"], self.identity.id, pk_c, p["pk"],
                                  n_c, p["n"])
            if key is None:
                self._plain({"t": "px", "why": "the key exchange did not check out"})
                self._end_pairing()
                return
            p.update(key=key, code=pairing_code(key))
            self._notice()
        elif t == "p4" and p and p["key"]:
            if not hmac.compare_digest(_unhex(msg["m"], 32), _mac(p["key"], b"confirm-c")):
                self._plain({"t": "px", "why": "the confirmation was wrong"})
                self._end_pairing()
                return
            self.identity.set_peer({"id": p["cid"], "key": p["key"].hex(), "name": p["name"]})
            self._plain({"t": "p5", "m": _mac(p["key"], b"confirm-h").hex()})
            log.info("paired with controller %s", p["name"])
            self._pairing = None
            self._cancel_timer()
            self._notice()
            self._changed()
        elif t == "px":
            self._end_pairing()

    def _sealed(self, msg: dict) -> bool:
        if msg.get("t") != "unpair":
            return False
        log.info("the controller released this head")
        self.forget()
        asyncio.get_running_loop().call_later(0.2, self.inner.drop)
        return True

    def info(self) -> dict:
        peer = self.identity.peer
        return {**self.inner.info(), "connected": self.connected,
                "radio": self.inner.connected, "paired": peer.get("name") if peer else None}
