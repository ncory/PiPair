"""Minimal libdrm (DRM/KMS atomic modesetting) bindings via ctypes.

Just enough to drive the display controller directly: pick a connector and
mode, set up a CRTC, and put framebuffers on hardware planes with per-plane
position, alpha and z-order, all in single atomic commits. Framebuffers come
either from CPU-filled "dumb" buffers or from dmabufs exported by the video
decoder (zero copy).
"""

from __future__ import annotations

import ctypes as C
import mmap
import os
import select
import struct
from dataclasses import dataclass

u16, u32, u64 = C.c_uint16, C.c_uint32, C.c_uint64
_drm = C.CDLL("libdrm.so.2", use_errno=True)

DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
DRM_CLIENT_CAP_ATOMIC = 3
DRM_MODE_TYPE_PREFERRED = 1 << 3
DRM_MODE_CONNECTED = 1
DRM_MODE_PAGE_FLIP_EVENT = 0x01
DRM_MODE_ATOMIC_NONBLOCK = 0x0200
DRM_MODE_ATOMIC_ALLOW_MODESET = 0x0400
DRM_EVENT_FLIP_COMPLETE = 0x02
DRM_MODE_FB_MODIFIERS = 1 << 1
PLANE_TYPE_OVERLAY, PLANE_TYPE_PRIMARY, PLANE_TYPE_CURSOR = 0, 1, 2
CONNECTOR_TYPES = {11: "HDMI-A", 12: "HDMI-B", 3: "DVI-I", 10: "DisplayPort", 14: "DSI",
                   16: "DPI", 15: "Virtual", 18: "Writeback", 5: "Composite"}


def fourcc(code: str) -> int:
    return int.from_bytes(code.encode(), "little")


YU12, NV12, XR24, AR24 = fourcc("YU12"), fourcc("NV12"), fourcc("XR24"), fourcc("AR24")


class ModeInfo(C.Structure):
    _fields_ = [("clock", u32), ("hdisplay", u16), ("hsync_start", u16), ("hsync_end", u16),
                ("htotal", u16), ("hskew", u16), ("vdisplay", u16), ("vsync_start", u16),
                ("vsync_end", u16), ("vtotal", u16), ("vscan", u16), ("vrefresh", u32),
                ("flags", u32), ("type", u32), ("name", C.c_char * 32)]


class _Res(C.Structure):
    _fields_ = [("count_fbs", C.c_int), ("fbs", C.POINTER(u32)),
                ("count_crtcs", C.c_int), ("crtcs", C.POINTER(u32)),
                ("count_connectors", C.c_int), ("connectors", C.POINTER(u32)),
                ("count_encoders", C.c_int), ("encoders", C.POINTER(u32)),
                ("min_width", u32), ("max_width", u32), ("min_height", u32), ("max_height", u32)]


class _Connector(C.Structure):
    _fields_ = [("connector_id", u32), ("encoder_id", u32), ("connector_type", u32),
                ("connector_type_id", u32), ("connection", C.c_int), ("mmWidth", u32),
                ("mmHeight", u32), ("subpixel", C.c_int), ("count_modes", C.c_int),
                ("modes", C.POINTER(ModeInfo)), ("count_props", C.c_int),
                ("props", C.POINTER(u32)), ("prop_values", C.POINTER(u64)),
                ("count_encoders", C.c_int), ("encoders", C.POINTER(u32))]


class _Encoder(C.Structure):
    _fields_ = [("encoder_id", u32), ("encoder_type", u32), ("crtc_id", u32),
                ("possible_crtcs", u32), ("possible_clones", u32)]


class _PlaneRes(C.Structure):
    _fields_ = [("count_planes", u32), ("planes", C.POINTER(u32))]


class _Plane(C.Structure):
    _fields_ = [("count_formats", u32), ("formats", C.POINTER(u32)), ("plane_id", u32),
                ("crtc_id", u32), ("fb_id", u32), ("crtc_x", u32), ("crtc_y", u32),
                ("x", u32), ("y", u32), ("possible_crtcs", u32), ("gamma_size", u32)]


class _ObjProps(C.Structure):
    _fields_ = [("count_props", u32), ("props", C.POINTER(u32)), ("prop_values", C.POINTER(u64))]


class _Prop(C.Structure):
    _fields_ = [("prop_id", u32), ("flags", u32), ("name", C.c_char * 32),
                ("count_values", C.c_int), ("values", C.POINTER(u64)),
                ("count_enums", C.c_int), ("enums", C.c_void_p),
                ("count_blobs", C.c_int), ("blob_ids", C.POINTER(u32))]


def _sig(name, restype, *argtypes):
    f = getattr(_drm, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


_P = C.POINTER
drmSetClientCap = _sig("drmSetClientCap", C.c_int, C.c_int, u64, u64)
drmModeGetResources = _sig("drmModeGetResources", _P(_Res), C.c_int)
drmModeFreeResources = _sig("drmModeFreeResources", None, _P(_Res))
drmModeGetConnector = _sig("drmModeGetConnector", _P(_Connector), C.c_int, u32)
drmModeFreeConnector = _sig("drmModeFreeConnector", None, _P(_Connector))
drmModeGetEncoder = _sig("drmModeGetEncoder", _P(_Encoder), C.c_int, u32)
drmModeFreeEncoder = _sig("drmModeFreeEncoder", None, _P(_Encoder))
drmModeGetPlaneResources = _sig("drmModeGetPlaneResources", _P(_PlaneRes), C.c_int)
drmModeFreePlaneResources = _sig("drmModeFreePlaneResources", None, _P(_PlaneRes))
drmModeGetPlane = _sig("drmModeGetPlane", _P(_Plane), C.c_int, u32)
drmModeFreePlane = _sig("drmModeFreePlane", None, _P(_Plane))
drmModeObjectGetProperties = _sig("drmModeObjectGetProperties", _P(_ObjProps), C.c_int, u32, u32)
drmModeFreeObjectProperties = _sig("drmModeFreeObjectProperties", None, _P(_ObjProps))
drmModeGetProperty = _sig("drmModeGetProperty", _P(_Prop), C.c_int, u32)
drmModeFreeProperty = _sig("drmModeFreeProperty", None, _P(_Prop))
drmModeCreatePropertyBlob = _sig("drmModeCreatePropertyBlob", C.c_int, C.c_int, C.c_void_p, C.c_size_t, _P(u32))
drmModeDestroyPropertyBlob = _sig("drmModeDestroyPropertyBlob", C.c_int, C.c_int, u32)
drmModeAtomicAlloc = _sig("drmModeAtomicAlloc", C.c_void_p)
drmModeAtomicFree = _sig("drmModeAtomicFree", None, C.c_void_p)
drmModeAtomicAddProperty = _sig("drmModeAtomicAddProperty", C.c_int, C.c_void_p, u32, u32, u64)
drmModeAtomicCommit = _sig("drmModeAtomicCommit", C.c_int, C.c_int, C.c_void_p, u32, C.c_void_p)
drmModeAddFB2 = _sig("drmModeAddFB2", C.c_int, C.c_int, u32, u32, u32, _P(u32 * 4), _P(u32 * 4),
                     _P(u32 * 4), _P(u32), u32)
drmModeAddFB2WithModifiers = _sig("drmModeAddFB2WithModifiers", C.c_int, C.c_int, u32, u32, u32,
                                  _P(u32 * 4), _P(u32 * 4), _P(u32 * 4), _P(u64 * 4), _P(u32),
                                  u32)
drmModeRmFB = _sig("drmModeRmFB", C.c_int, C.c_int, u32)
drmModeCreateDumbBuffer = _sig("drmModeCreateDumbBuffer", C.c_int, C.c_int, u32, u32, u32, u32,
                               _P(u32), _P(u32), _P(u64))
drmModeMapDumbBuffer = _sig("drmModeMapDumbBuffer", C.c_int, C.c_int, u32, _P(u64))
drmModeDestroyDumbBuffer = _sig("drmModeDestroyDumbBuffer", C.c_int, C.c_int, u32)
drmPrimeFDToHandle = _sig("drmPrimeFDToHandle", C.c_int, C.c_int, C.c_int, _P(u32))
drmCloseBufferHandle = _sig("drmCloseBufferHandle", C.c_int, C.c_int, u32)


class DrmError(OSError):
    pass


class NoConnectedDisplay(DrmError):
    """No display is plugged in (or it is powered off and dropped the link).

    Distinct from other DRM failures because it is expected and temporary:
    the renderer cannot run without a display, but should resume when one
    appears rather than treating it as a fault.
    """


def _check(ret: int, what: str) -> int:
    if ret < 0:
        err = C.get_errno() or -ret
        raise DrmError(err, f"{what}: {os.strerror(err)}")
    return ret


@dataclass
class Output:
    connector_id: int
    name: str
    crtc_id: int
    crtc_index: int
    mode: ModeInfo

    @property
    def size(self) -> tuple[int, int]:
        return self.mode.hdisplay, self.mode.vdisplay

    @property
    def refresh(self) -> int:
        return self.mode.vrefresh

    def mode_name(self) -> str:
        return f"{self.mode.hdisplay}x{self.mode.vdisplay}@{self.mode.vrefresh}"


@dataclass
class DumbBuffer:
    handle: int
    fb_id: int
    width: int
    height: int
    pitch: int
    map: mmap.mmap


class Card:
    """An open DRM device, used as DRM master for one output."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        _check(drmSetClientCap(self.fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1), "universal planes")
        _check(drmSetClientCap(self.fd, DRM_CLIENT_CAP_ATOMIC, 1), "atomic modesetting")
        self._props: dict[int, dict[str, int]] = {}
        self._prime_handles: dict[int, int] = {}  # gem handle -> refcount

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    # -- discovery ---------------------------------------------------------
    def outputs(self) -> list[tuple[str, bool, list[ModeInfo], int]]:
        res = drmModeGetResources(self.fd)
        if not res:
            raise DrmError(0, "drmModeGetResources failed (not a KMS device?)")
        out = []
        try:
            for i in range(res.contents.count_connectors):
                c = drmModeGetConnector(self.fd, res.contents.connectors[i])
                if not c:
                    continue
                cc = c.contents
                name = f"{CONNECTOR_TYPES.get(cc.connector_type, 'Unknown')}-{cc.connector_type_id}"
                modes = [ModeInfo.from_buffer_copy(cc.modes[j]) for j in range(cc.count_modes)]
                out.append((name, cc.connection == DRM_MODE_CONNECTED, modes, cc.connector_id))
                drmModeFreeConnector(c)
        finally:
            drmModeFreeResources(res)
        return out

    def pick_output(self, connector: str | None = None, mode: str | None = None,
                    allow_custom: bool = False) -> Output:
        """Choose a connected connector, its mode and a CRTC that can drive it.

        `mode` is "auto" (the display's preferred mode) or "WIDTHxHEIGHT[@HZ]".
        """
        res = drmModeGetResources(self.fd)
        try:
            crtcs = [res.contents.crtcs[i] for i in range(res.contents.count_crtcs)]
            for i in range(res.contents.count_connectors):
                c = drmModeGetConnector(self.fd, res.contents.connectors[i])
                cc = c.contents
                name = f"{CONNECTOR_TYPES.get(cc.connector_type, 'Unknown')}-{cc.connector_type_id}"
                if (connector and name != connector) or cc.connection != DRM_MODE_CONNECTED \
                        or cc.count_modes == 0:
                    drmModeFreeConnector(c)
                    continue
                modes = [ModeInfo.from_buffer_copy(cc.modes[j]) for j in range(cc.count_modes)]
                chosen = _choose_mode(modes, mode, allow_custom)
                possible = 0
                for j in range(cc.count_encoders):
                    e = drmModeGetEncoder(self.fd, cc.encoders[j])
                    if e:
                        possible |= e.contents.possible_crtcs
                        drmModeFreeEncoder(e)
                conn_id = cc.connector_id
                drmModeFreeConnector(c)
                for idx, crtc in enumerate(crtcs):
                    if possible & (1 << idx):
                        return Output(conn_id, name, crtc, idx, chosen)
            raise NoConnectedDisplay(0, "no connected display found")
        finally:
            drmModeFreeResources(res)

    def planes(self, crtc_index: int) -> list[dict]:
        """Planes usable on the CRTC, with type, formats and zpos range."""
        pr = drmModeGetPlaneResources(self.fd)
        out = []
        try:
            for i in range(pr.contents.count_planes):
                pid = pr.contents.planes[i]
                p = drmModeGetPlane(self.fd, pid)
                pc = p.contents
                if pc.possible_crtcs & (1 << crtc_index):
                    fmts = {pc.formats[j] for j in range(pc.count_formats)}
                    props = self.properties(pid)
                    out.append({"id": pid, "type": props.get("type", 0), "formats": fmts,
                                "has_alpha": "alpha" in self.prop_ids(pid),
                                "has_zpos": "zpos" in self.prop_ids(pid)})
                drmModeFreePlane(p)
        finally:
            drmModeFreePlaneResources(pr)
        return out

    # -- properties ---------------------------------------------------------
    def prop_ids(self, obj_id: int) -> dict[str, int]:
        if obj_id not in self._props:
            ids = {}
            props = drmModeObjectGetProperties(self.fd, obj_id, 0)
            if props:
                for i in range(props.contents.count_props):
                    p = drmModeGetProperty(self.fd, props.contents.props[i])
                    ids[p.contents.name.decode()] = p.contents.prop_id
                    drmModeFreeProperty(p)
                drmModeFreeObjectProperties(props)
            self._props[obj_id] = ids
        return self._props[obj_id]

    def properties(self, obj_id: int) -> dict[str, int]:
        out = {}
        props = drmModeObjectGetProperties(self.fd, obj_id, 0)
        if props:
            for i in range(props.contents.count_props):
                p = drmModeGetProperty(self.fd, props.contents.props[i])
                out[p.contents.name.decode()] = props.contents.prop_values[i]
                drmModeFreeProperty(p)
            drmModeFreeObjectProperties(props)
        return out

    def mode_blob(self, mode: ModeInfo) -> int:
        blob = u32()
        _check(drmModeCreatePropertyBlob(self.fd, C.byref(mode), C.sizeof(mode), C.byref(blob)),
               "create mode blob")
        return blob.value

    def commit(self, changes: dict[int, dict[str, int]], flags: int = 0) -> int:
        """Atomically apply {object_id: {property: value}}. Returns 0 or -errno."""
        req = drmModeAtomicAlloc()
        try:
            for obj, props in changes.items():
                ids = self.prop_ids(obj)
                for name, value in props.items():
                    if name in ids:
                        drmModeAtomicAddProperty(req, obj, ids[name], int(value) & 0xFFFFFFFFFFFFFFFF)
            ret = drmModeAtomicCommit(self.fd, req, flags, None)
            return -C.get_errno() if ret < 0 else 0
        finally:
            drmModeAtomicFree(req)

    def read_events(self, timeout: float) -> int:
        """Wait up to `timeout` s for DRM events; returns the number of flips completed."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return 0
        data = os.read(self.fd, 4096)
        flips = 0
        off = 0
        while off + 8 <= len(data):
            etype, length = struct.unpack_from("II", data, off)
            if etype == DRM_EVENT_FLIP_COMPLETE:
                flips += 1
            off += max(length, 8)
        return flips

    # -- framebuffers --------------------------------------------------------
    def add_fb(self, width: int, height: int, fmt: int, handles, pitches, offsets,
               modifier: int = 0) -> int:
        h, p, o = (u32 * 4)(*handles), (u32 * 4)(*pitches), (u32 * 4)(*offsets)
        fb = u32()
        if modifier:  # e.g. the Pi 4 HEVC decoder's SAND128 tiling
            mods = (u64 * 4)(*[modifier if hd else 0 for hd in handles])
            _check(drmModeAddFB2WithModifiers(self.fd, width, height, fmt, C.byref(h), C.byref(p),
                                              C.byref(o), C.byref(mods), C.byref(fb),
                                              DRM_MODE_FB_MODIFIERS), "AddFB2WithModifiers")
        else:
            _check(drmModeAddFB2(self.fd, width, height, fmt, C.byref(h), C.byref(p), C.byref(o),
                                 C.byref(fb), 0), "AddFB2")
        return fb.value

    def rm_fb(self, fb_id: int) -> None:
        if fb_id:
            drmModeRmFB(self.fd, fb_id)

    def dumb_buffer(self, width: int, height: int) -> DumbBuffer:
        """A CPU-writable XRGB8888 framebuffer."""
        handle, pitch, size = u32(), u32(), u64()
        _check(drmModeCreateDumbBuffer(self.fd, width, height, 32, 0, C.byref(handle),
                                       C.byref(pitch), C.byref(size)), "create dumb buffer")
        offset = u64()
        _check(drmModeMapDumbBuffer(self.fd, handle.value, C.byref(offset)), "map dumb buffer")
        m = mmap.mmap(self.fd, size.value, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                      offset=offset.value)
        fb = self.add_fb(width, height, XR24, (handle.value, 0, 0, 0), (pitch.value, 0, 0, 0),
                         (0, 0, 0, 0))
        return DumbBuffer(handle.value, fb, width, height, pitch.value, m)

    def destroy_dumb(self, buf: DumbBuffer) -> None:
        self.rm_fb(buf.fb_id)
        buf.map.close()
        drmModeDestroyDumbBuffer(self.fd, buf.handle)

    def import_dmabuf(self, dmabuf_fd: int) -> int:
        """GEM handle for a dmabuf (refcounted; release with release_handle)."""
        h = u32()
        _check(drmPrimeFDToHandle(self.fd, dmabuf_fd, C.byref(h)), "import dmabuf")
        self._prime_handles[h.value] = self._prime_handles.get(h.value, 0) + 1
        return h.value

    def release_handle(self, handle: int) -> None:
        n = self._prime_handles.get(handle, 0) - 1
        if n <= 0:
            self._prime_handles.pop(handle, None)
            drmCloseBufferHandle(self.fd, handle)
        else:
            self._prime_handles[handle] = n


# Standard CEA-861 timings, used to force a mode the display doesn't list.
# (w, h, hz): (clock kHz, hsync_start, hsync_end, htotal, vsync_start, vsync_end, vtotal)
CEA_MODES = {
    (1920, 1080, 30): (74250, 2008, 2052, 2200, 1084, 1089, 1125),  # VIC 34
    (1920, 1080, 25): (74250, 2448, 2492, 2640, 1084, 1089, 1125),  # VIC 33
    (1920, 1080, 24): (74250, 2558, 2602, 2750, 1084, 1089, 1125),  # VIC 32
    (1920, 1080, 50): (148500, 2448, 2492, 2640, 1084, 1089, 1125),  # VIC 31
    (1920, 1080, 60): (148500, 2008, 2052, 2200, 1084, 1089, 1125),  # VIC 16
    (1280, 720, 50): (74250, 1720, 1760, 1980, 725, 730, 750),  # VIC 19
    (1280, 720, 60): (74250, 1390, 1430, 1650, 725, 730, 750),  # VIC 4
}
DRM_MODE_TYPE_USERDEF = 1 << 5
DRM_MODE_FLAG_PHSYNC, DRM_MODE_FLAG_PVSYNC = 1 << 0, 1 << 2


def cea_mode(w: int, h: int, hz: int) -> ModeInfo | None:
    t = CEA_MODES.get((w, h, hz))
    if t is None:
        return None
    clock, hss, hse, ht, vss, vse, vt = t
    return ModeInfo(clock=clock, hdisplay=w, hsync_start=hss, hsync_end=hse, htotal=ht,
                    vdisplay=h, vsync_start=vss, vsync_end=vse, vtotal=vt, vrefresh=hz,
                    flags=DRM_MODE_FLAG_PHSYNC | DRM_MODE_FLAG_PVSYNC,
                    type=DRM_MODE_TYPE_USERDEF, name=f"{w}x{h}".encode())


def _choose_mode(modes: list[ModeInfo], want: str | None, allow_custom: bool = False) -> ModeInfo:
    if want and want != "auto":
        size, _, hz = want.partition("@")
        w, h = (int(x) for x in size.split("x"))
        cands = [m for m in modes if m.hdisplay == w and m.vdisplay == h
                 and not m.flags & 0x10]  # skip interlaced
        if hz:
            exact = [m for m in cands if m.vrefresh == int(hz)]
            if not exact and allow_custom:
                forced = cea_mode(w, h, int(hz))
                if forced is not None:
                    return forced  # not advertised by the display: forced
            cands = exact or cands
        if cands:
            return max(cands, key=lambda m: m.vrefresh if not hz else -abs(m.vrefresh - int(hz)))
    for m in modes:
        if m.type & DRM_MODE_TYPE_PREFERRED:
            return m
    return modes[0]


def fill_xrgb(buf: DumbBuffer, color: tuple[int, int, int]) -> None:
    r, g, b = color
    row = bytes((b, g, r, 0xFF)) * buf.width + b"\0" * (buf.pitch - 4 * buf.width)
    buf.map.seek(0)
    buf.map.write(row * buf.height)
