"""edgelib — EdgeX 슬라이스 I/O 백플레인 마스터 (파이썬 바인딩)

Copyright (c) 2026 RTES Co., Ltd. All rights reserved.

**ctypes 래퍼다.** 주기 통신이 C 스레드에서 돌므로 GIL 과 무관하게 워치독이
지켜진다 — 파이썬이 GC 로 수십 ms 를 삼켜도 프레임은 제때 나간다. 순수 파이썬으로
같은 것을 쓰면 6 ms 주기에서 이미 워치독이 물린다(실측). 그것이 이 계층을 C 위에
올린 이유의 전부다.

    from edgelib import EdgeBus, State
    from line_a_pd import In, Out

    with EdgeBus("line_a.json") as bus:
        bus.setmode(State.RUN)
        pd = In.read(bus)
        if pd.n1_p1.valid:
            print(pd.n1_p1.temperature)

오류는 **예외**로 올린다. C 의 음수 결과 코드를 그대로 돌려주면 파이썬 쪽에서
검사를 잊기 쉽고, 그 실수가 조용히 지나간다.
"""

from __future__ import annotations

import ctypes as C
import enum
import os
import sys
from ctypes.util import find_library
from dataclasses import dataclass

__all__ = [
    "EdgeBus", "State", "PortMode", "PortStatus", "IQ", "PortPower",
    "Event", "NodeInfo", "PortInfo", "PortConfig", "PortStatusInfo", "MasterIdent",
    "EdgeError", "EdgeTimeoutError", "EdgeDeviceError", "EdgeStateError",
]

# ── 결과 코드 (API §5.1) ─────────────────────────────────────────────────────
OK = 0
ERR_BAD_PARAM = -1
ERR_BAD_STATE = -2
ERR_BAD_CHANNEL = -3
ERR_BUSY = -4
ERR_UNSUPPORTED = -5
ERR_TIMEOUT = -6
ERR_DEVICE = -7
ERR_COMMS = -8
ERR_NO_MEM = -9

MAX_PORTS = 8


class State(enum.IntEnum):
    STARTUP = 0
    PREOP = 1
    RUN = 2
    FAILSAFE = 3


class PortMode(enum.IntEnum):
    DEACTIVATED = 0
    IOL_MANUAL = 1
    IOL_AUTOSTART = 2
    DI_CQ = 3
    DO_CQ = 4


class IQ(enum.IntEnum):
    """I/Q (M12 핀 2) 의 쓰임. **출력 둘은 이 보드에 경로가 없다.**"""
    NOT_SUPPORTED = 0
    DIGITAL_INPUT = 1
    DIGITAL_OUTPUT = 2
    POWER2 = 5


class PortPower(enum.IntEnum):
    """포트 전원 (규격 Table E.9).

    참·거짓이 아니라 셋이다 — 끄고 그대로 두는 것과 잠깐 껐다 켜는 것은 다른 일이다.
    """
    ONE_TIME_OFF = 0    # 껐다가 off_ms 뒤에 자동으로 켠다
    OFF = 1             # 끄고 그대로 둔다
    ON = 2              # 켠다


#: 규격 E.9 — "Minimum PowerOffTime shall be 500 ms". ONE_TIME_OFF 에서만 쓴다.
PWR_OFF_MS_MIN = 500


class PortStatus(enum.IntEnum):
    NO_DEVICE = 0
    DEACTIVATED = 1
    PORT_DIAG = 2
    OPERATE = 4
    DI_CQ = 5
    DO_CQ = 6
    PORT_POWER_OFF = 254
    NOT_AVAILABLE = 255


# ── 예외 ─────────────────────────────────────────────────────────────────────
class EdgeError(Exception):
    """모든 edgelib 오류의 뿌리."""

    def __init__(self, code: int, msg: str = ""):
        self.code = code
        super().__init__(msg or _lib.edgelib_error_msg(code).decode())


class EdgeTimeoutError(EdgeError):
    """응답이 없었다."""


class EdgeStateError(EdgeError):
    """지금 상태에서 안 되는 요청."""


class EdgeDeviceError(EdgeError):
    """디바이스가 거부했다. `error_type` 에 IO-Link ErrorType 2 바이트가 있다."""

    def __init__(self, code: int, error_type: int = 0, msg: str = ""):
        self.error_type = error_type
        super().__init__(code, msg or f"device rejected - ErrorType 0x{error_type:04X}")


def _check(rc: int, error_type: int = 0) -> None:
    if rc == OK:
        return
    if rc == ERR_TIMEOUT:
        raise EdgeTimeoutError(rc)
    if rc == ERR_BAD_STATE:
        raise EdgeStateError(rc)
    if rc == ERR_DEVICE:
        raise EdgeDeviceError(rc, error_type)
    raise EdgeError(rc)


# ── C 구조체 ─────────────────────────────────────────────────────────────────
class _CEvent(C.Structure):
    _fields_ = [("node", C.c_uint8), ("channel", C.c_uint8),
                ("mode", C.c_uint8), ("type", C.c_uint8),
                ("code", C.c_uint16), ("qualifier", C.c_uint16),
                ("timestamp_ms", C.c_uint32)]


class _CPort(C.Structure):
    _fields_ = [("port", C.c_uint8), ("mode", C.c_uint8),
                ("pd_in", C.c_uint8), ("pd_out", C.c_uint8),
                ("vendor_id", C.c_uint16), ("device_id", C.c_uint32)]


class _CNode(C.Structure):
    _fields_ = [("address", C.c_uint8), ("category", C.c_uint8),
                ("model", C.c_uint8), ("variant", C.c_uint8),
                ("hw_rev", C.c_uint8), ("serial", C.c_char * 24),
                ("type_name", C.c_char * 48),
                ("pd_in", C.c_uint16), ("pd_out", C.c_uint16),
                ("image_in_off", C.c_uint16), ("image_out_off", C.c_uint16),
                ("port_count", C.c_uint8), ("ports", _CPort * MAX_PORTS)]


class _CMasterIdent(C.Structure):
    _fields_ = [("vendor_id", C.c_uint16), ("master_id", C.c_uint32),
                ("port_count", C.c_uint8), ("master_type", C.c_uint8),
                ("product", C.c_char * 32)]


class _CPortCfg(C.Structure):
    _fields_ = [("mode", C.c_uint8), ("validation", C.c_uint8),
                ("iq_behavior", C.c_uint8), ("cycletime", C.c_uint8),
                ("vendor_id", C.c_uint16), ("device_id", C.c_uint32)]


class _CPortStatus(C.Structure):
    _fields_ = [("status", C.c_uint8), ("quality", C.c_uint8),
                ("revision_id", C.c_uint8), ("rate", C.c_uint8),
                ("cycletime", C.c_uint8), ("pd_in", C.c_uint8),
                ("pd_out", C.c_uint8), ("vendor_id", C.c_uint16),
                ("device_id", C.c_uint32), ("cycletime_us", C.c_uint32)]


# ── 파이썬 쪽 값 ─────────────────────────────────────────────────────────────
@dataclass
class Event:
    node: int
    channel: int
    mode: int
    type: int
    code: int
    qualifier: int
    timestamp_ms: int

    @property
    def from_library(self) -> bool:
        """`node == 0` 이면 라이브러리 자신의 관찰이다 (통신 품질). 이때
        `channel` 이 어느 노드를 본 것인지 가리킨다."""
        return self.node == 0

    @property
    def type_name(self) -> str:
        return {1: "notify", 2: "warning", 3: "error"}.get(self.type, "?")

    @property
    def mode_name(self) -> str:
        return {1: "single shot", 2: "disappeared", 3: "active"}.get(self.mode, "?")

    @property
    def source(self) -> str:
        """코드 대역이 출처를 가른다 — 겹치지 않게 나눠 둔 이유가 이것이다."""
        if self.code >= 0x1800:
            return "port"
        if self.code >= 0x1000:
            return "device"
        return "backplane"


@dataclass
class PortInfo:
    port: int
    mode: int
    pd_in: int
    pd_out: int
    vendor_id: int
    device_id: int


@dataclass
class NodeInfo:
    address: int
    category: int
    model: int
    variant: int
    hw_rev: int
    serial: str
    type_name: str
    pd_in: int
    pd_out: int
    image_in_off: int
    image_out_off: int
    ports: list


@dataclass
class MasterIdent:
    vendor_id: int
    master_id: int
    port_count: int
    master_type: int


@dataclass
class PortConfig:
    mode: int = PortMode.DEACTIVATED
    validation: int = 0
    iq_behavior: int = 0
    cycletime: int = 0
    vendor_id: int = 0
    device_id: int = 0


@dataclass
class PortStatusInfo:
    status: int
    quality: int
    revision_id: int
    rate: int
    cycletime: int
    pd_in: int
    pd_out: int
    vendor_id: int
    device_id: int
    cycletime_us: int

    @property
    def status_name(self) -> str:
        try:
            return PortStatus(self.status).name
        except ValueError:
            return f"?{self.status}"


# ── 라이브러리 적재 ──────────────────────────────────────────────────────────
def _load():
    """`EDGELIB_PATH` → 패키지 옆 → 표준 경로 순으로 찾는다.

    개발 중에는 빌드 트리의 .so 를 그대로 쓰고 싶고, 설치 후에는 표준 경로에 있다.
    둘 다 되게 하지 않으면 개발할 때마다 설치해야 한다.
    """
    names = []
    env = os.environ.get("EDGELIB_PATH")
    if env:
        names.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    for up in (here, os.path.join(here, "..", "..")):
        names.append(os.path.join(up, "libedgelib.so"))
    names.append("libedgelib.so")
    found = find_library("edgelib")
    if found:
        names.append(found)

    last = None
    for n in names:
        try:
            return C.CDLL(n)
        except OSError as e:      # noqa: PERF203 — 후보를 차례로 시도한다
            last = e
    raise ImportError(
        f"libedgelib.so 를 찾지 못했습니다. EDGELIB_PATH 로 지정하거나 "
        f"`sudo make install` 하세요 ({last})")


_lib = _load()

_lib.edgelib_open.restype = C.c_void_p
_lib.edgelib_open.argtypes = [C.c_char_p]
_lib.edgelib_close.argtypes = [C.c_void_p]
_lib.edgelib_error_msg.restype = C.c_char_p
_lib.edgelib_error_msg.argtypes = [C.c_int]
_lib.edgelib_last_error.restype = C.c_char_p
_lib.edgelib_last_error.argtypes = [C.c_void_p]
_lib.edgelib_setmode.argtypes = [C.c_void_p, C.c_int]
_lib.edgelib_getmode.argtypes = [C.c_void_p, C.POINTER(C.c_int)]
_lib.edgelib_node_count.argtypes = [C.c_void_p]
_lib.edgelib_node_info.argtypes = [C.c_void_p, C.c_uint8, C.POINTER(_CNode)]
_lib.edgelib_get_event.argtypes = [C.c_void_p, C.POINTER(_CEvent), C.c_int,
                                   C.POINTER(C.c_int)]
_lib.edgelib_clear_event.argtypes = [C.c_void_p, C.c_uint8]
_lib.edgelib_param_read.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                    C.c_uint16, C.POINTER(C.c_uint32)]
_lib.edgelib_param_write.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                     C.c_uint16, C.c_uint32]
_lib.edgelib_iol_master_ident.argtypes = [C.c_void_p, C.c_uint8,
                                          C.POINTER(_CMasterIdent)]
_lib.edgelib_iol_port_configuration.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                                C.POINTER(_CPortCfg)]
_lib.edgelib_iol_readback_port_configuration.argtypes = [
    C.c_void_p, C.c_uint8, C.c_uint8, C.POINTER(_CPortCfg)]
_lib.edgelib_iol_port_status.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                         C.POINTER(_CPortStatus)]
_lib.edgelib_iol_port_power.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                        C.c_int, C.c_uint16]
_lib.edgelib_iol_device_read.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                         C.c_uint16, C.c_uint8,
                                         C.POINTER(C.c_uint8),
                                         C.POINTER(C.c_uint16), C.c_double]
_lib.edgelib_iol_device_write.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                          C.c_uint16, C.c_uint8,
                                          C.POINTER(C.c_uint8), C.c_uint16,
                                          C.c_double]
_lib.edgelib_iol_abort.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8]
_lib.edgelib_iol_pd_in_iq.argtypes = [C.c_void_p, C.c_uint8, C.c_uint8,
                                      C.POINTER(C.c_uint8)]
_lib.edgelib_image_in.argtypes = [C.c_void_p, C.POINTER(C.c_uint8),
                                  C.POINTER(C.c_uint16)]
_lib.edgelib_image_out.argtypes = [C.c_void_p, C.POINTER(C.c_uint8), C.c_uint16]
_lib.edgelib_image_size.argtypes = [C.c_void_p, C.POINTER(C.c_uint16),
                                    C.POINTER(C.c_uint16)]


# ── 버스 ─────────────────────────────────────────────────────────────────────
class EdgeBus:
    """버스 하나. **`with` 로 쓰세요** — 닫지 않으면 스레드가 남아 포트를 뭅니다."""

    def __init__(self, config_path: str | None = None):
        p = config_path.encode() if config_path else None
        h = _lib.edgelib_open(p)
        if not h:
            raise EdgeError(ERR_COMMS,
                            _lib.edgelib_last_error(None).decode(errors="replace"))
        self._h = C.c_void_p(h)
        self._in_bytes = C.c_uint16(0)
        self._out_bytes = C.c_uint16(0)
        _lib.edgelib_image_size(self._h, C.byref(self._in_bytes),
                                C.byref(self._out_bytes))

    # ── 자원 ────────────────────────────────────────────────────────────────
    def close(self) -> None:
        if getattr(self, "_h", None):
            _lib.edgelib_close(self._h)
            self._h = None

    def __enter__(self) -> "EdgeBus":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:       # noqa: BLE001 — 소멸자에서의 실패는 삼킨다
            pass

    def _err(self) -> str:
        return _lib.edgelib_last_error(self._h).decode(errors="replace")

    def _check(self, rc: int, error_type: int = 0) -> None:
        if rc != OK and self._err():
            # 라이브러리가 남긴 한 줄이 결과 코드보다 훨씬 많은 것을 말해 준다
            if rc == ERR_TIMEOUT:
                raise EdgeTimeoutError(rc, self._err())
            if rc == ERR_BAD_STATE:
                raise EdgeStateError(rc, self._err())
            if rc == ERR_DEVICE:
                raise EdgeDeviceError(rc, error_type, self._err())
            raise EdgeError(rc, self._err())
        _check(rc, error_type)

    # ── 상태 ────────────────────────────────────────────────────────────────
    def setmode(self, target: State) -> None:
        self._check(_lib.edgelib_setmode(self._h, int(target)))

    def getmode(self) -> State:
        v = C.c_int(0)
        _check(_lib.edgelib_getmode(self._h, C.byref(v)))
        return State(v.value)

    # ── 구성 ────────────────────────────────────────────────────────────────
    def node_count(self) -> int:
        return _lib.edgelib_node_count(self._h)

    def node_info(self, node: int) -> NodeInfo:
        c = _CNode()
        self._check(_lib.edgelib_node_info(self._h, node, C.byref(c)))
        return NodeInfo(
            address=c.address, category=c.category, model=c.model,
            variant=c.variant, hw_rev=c.hw_rev,
            serial=c.serial.decode(errors="replace"),
            type_name=c.type_name.decode(errors="replace"),
            pd_in=c.pd_in, pd_out=c.pd_out,
            image_in_off=c.image_in_off, image_out_off=c.image_out_off,
            ports=[PortInfo(p.port, p.mode, p.pd_in, p.pd_out,
                            p.vendor_id, p.device_id)
                   for p in c.ports[:c.port_count]])

    def nodes(self) -> list:
        """붙어 있는 노드 전부. 주소는 1..126 이라 그 범위만 훑는다."""
        out = []
        for a in range(1, 127):
            try:
                out.append(self.node_info(a))
            except EdgeError:
                continue
        return out

    # ── 진단 ────────────────────────────────────────────────────────────────
    def get_event(self, max_events: int = 64) -> list:
        buf = (_CEvent * max_events)()
        n = C.c_int(0)
        self._check(_lib.edgelib_get_event(self._h, buf, max_events, C.byref(n)))
        return [Event(e.node, e.channel, e.mode, e.type, e.code,
                      e.qualifier, e.timestamp_ms) for e in buf[:n.value]]

    def clear_event(self, node: int = 0) -> None:
        self._check(_lib.edgelib_clear_event(self._h, node))

    # ── 모듈 파라미터 ───────────────────────────────────────────────────────
    # 모듈 **자신의** 설정이다 — 포트에 꽂힌 디바이스의 파라미터(ISDU)가 아니다.
    # `channel` 은 0 이 모듈 자신, 1~n 이 채널. 값은 언제나 32 비트다 (코어 §6.2).
    def param_read(self, node: int, index: int, channel: int = 0) -> int:
        v = C.c_uint32(0)
        self._check(_lib.edgelib_param_read(self._h, node, channel, index,
                                            C.byref(v)))
        return v.value

    def param_write(self, node: int, index: int, value: int,
                    channel: int = 0) -> None:
        self._check(_lib.edgelib_param_write(self._h, node, channel, index,
                                             value))

    # ── IO-Link — 규격의 SMI 서비스 그대로 ──────────────────────────────────
    def iol_master_ident(self, node: int) -> MasterIdent:
        c = _CMasterIdent()
        self._check(_lib.edgelib_iol_master_ident(self._h, node, C.byref(c)))
        return MasterIdent(c.vendor_id, c.master_id, c.port_count, c.master_type)

    def iol_port_configuration(self, node: int, port: int,
                               cfg: PortConfig) -> None:
        c = _CPortCfg(cfg.mode, cfg.validation, cfg.iq_behavior,
                      cfg.cycletime, cfg.vendor_id, cfg.device_id)
        self._check(_lib.edgelib_iol_port_configuration(self._h, node, port,
                                                        C.byref(c)))

    def iol_readback_port_configuration(self, node: int, port: int) -> PortConfig:
        c = _CPortCfg()
        self._check(_lib.edgelib_iol_readback_port_configuration(
            self._h, node, port, C.byref(c)))
        return PortConfig(c.mode, c.validation, c.iq_behavior, c.cycletime,
                          c.vendor_id, c.device_id)

    def iol_port_status(self, node: int, port: int) -> PortStatusInfo:
        c = _CPortStatus()
        self._check(_lib.edgelib_iol_port_status(self._h, node, port, C.byref(c)))
        return PortStatusInfo(c.status, c.quality, c.revision_id, c.rate,
                              c.cycletime, c.pd_in, c.pd_out, c.vendor_id,
                              c.device_id, c.cycletime_us)

    def iol_port_power(self, node: int, port: int, mode: int,
                       off_ms: int = 0) -> None:
        """그 포트의 `L+` 를 끊었다 붙인다 — 디바이스를 다시 세우는 마지막 수단.

        `mode` 는 `PortPower` 셋 중 하나다. `off_ms` 는 `ONE_TIME_OFF` 에서만
        뜻이 있고 `PWR_OFF_MS_MIN` 보다 짧으면 디바이스가 전원이 내려간 줄 모른다.
        """
        self._check(_lib.edgelib_iol_port_power(self._h, node, port,
                                                int(mode), off_ms))

    def iol_device_read(self, node: int, port: int, index: int,
                        subindex: int = 0, max_len: int = 232,
                        timeout_s: float = 0.0) -> bytes:
        buf = (C.c_uint8 * max_len)()
        ln = C.c_uint16(max_len)
        rc = _lib.edgelib_iol_device_read(self._h, node, port, index, subindex,
                                          buf, C.byref(ln), timeout_s)
        if rc == ERR_DEVICE:
            et = (buf[0] << 8) | buf[1] if ln.value >= 2 else 0
            raise EdgeDeviceError(rc, et)
        self._check(rc)
        return bytes(buf[:ln.value])

    def iol_device_write(self, node: int, port: int, index: int, data: bytes,
                         subindex: int = 0, timeout_s: float = 0.0) -> None:
        buf = (C.c_uint8 * max(1, len(data)))(*data) if data else (C.c_uint8 * 1)()
        self._check(_lib.edgelib_iol_device_write(self._h, node, port, index,
                                                  subindex, buf, len(data),
                                                  timeout_s))

    def iol_abort(self, node: int, port: int) -> None:
        self._check(_lib.edgelib_iol_abort(self._h, node, port))

    def iol_pd_in_iq(self, node: int, port: int) -> int:
        """핀 2 (I/Q) 의 지금 입력 상태 — 0 또는 1.

        **`iq_behavior` 가 `IQ.DIGITAL_INPUT` 이라야 답한다.** 아니면
        `EdgeError`(unsupported) 다 — 설정하지 않은 핀을 읽으려 한 것이다.
        주기 데이터가 아니라 물을 때마다 버스를 한 번 탄다.
        """
        v = C.c_uint8(0)
        self._check(_lib.edgelib_iol_pd_in_iq(self._h, node, port, C.byref(v)))
        return v.value

    # ── 프로세스 데이터 ─────────────────────────────────────────────────────
    def image_size(self) -> tuple:
        """지금 이미지 크기. **캐시하지 않는다** — 포트 모드를 바꾸면 달라진다."""
        _lib.edgelib_image_size(self._h, C.byref(self._in_bytes),
                                C.byref(self._out_bytes))
        return (self._in_bytes.value, self._out_bytes.value)

    def image_in(self) -> bytes:
        """입력 이미지 **전체**. 버스를 타지 않으므로 즉시 돌아온다."""
        n = self._in_bytes.value
        buf = (C.c_uint8 * max(1, n))()
        ln = C.c_uint16(n)
        _check(_lib.edgelib_image_in(self._h, buf, C.byref(ln)))
        return bytes(buf[:ln.value])

    def image_out(self, data: bytes) -> None:
        """출력 이미지 **전체**를 갈아 끼운다. 실제 전송은 다음 주기다."""
        buf = (C.c_uint8 * max(1, len(data)))(*data)
        self._check(_lib.edgelib_image_out(self._h, buf, len(data)))
