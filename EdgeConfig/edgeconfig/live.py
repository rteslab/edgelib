"""edgelib 으로 도는 세션 — `session.Session` 과 같은 자리에 끼운다

Copyright (c) 2026 RTES Co., Ltd. All rights reserved.

`session.py` 는 주기 교환을 **파이썬 스레드**로 돌린다. 커미셔닝에는 그것으로 충분했다
— 보려는 것이 "RUN 으로 올라가 PD 가 흐른다"는 사실뿐이었기 때문이다. 다만 실측에서
6 ms 주기는 버티지 못했다. GIL 과 GC 가 수십 ms 를 통째로 삼키면 워치독(주기의 3배)이
물리고, 그 뒤로는 손실 100 % 가 된다.

**여기서는 그 스레드가 C 안에 있다.** 같은 하드웨어에서 3.013 ms(계산 하한)까지
손실 이벤트 없이 돌았다. 파이썬은 스냅샷을 복사해 갈 뿐이라 늦어도 프레임은 제때 나간다.

`Session` 과 **같은 이름·같은 반환형**을 쓴다. 그래야 GUI 가 어느 쪽으로 도는지 몰라도
되고, edgelib 이 없는 자리에서는 `session.Session` 으로 그대로 돌아갈 수 있다.

    try:
        from .live import LiveSession as Session
    except ImportError:
        from .session import Session
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .session import (PdSnapshot, SessionError, STATE_NAME, STATE_PREOP,
                      STATE_RUN, STATE_STARTUP, cycletime_us)

# edgelib 이 없으면 여기서 ImportError 가 난다 — 부르는 쪽이 그것으로 갈린다
from edgelib import (EdgeBus, EdgeDeviceError, EdgeError, PortConfig, State)

__all__ = ["LiveSession", "LiveSnapshot", "SessionError"]


@dataclass
class LiveSnapshot(PdSnapshot):
    """`PdSnapshot` 과 같되 손실률의 출처가 다르다.

    파이썬 세션은 자기가 낸 프레임을 세어 손실을 알았다. edgelib 은 그 통계를 주지
    않는다 — **응용이 알아야 할 것은 "몇 개를 놓쳤나"가 아니라 "지금 이 값을 써도
    되나"** 라는 것이 API 의 판단이고(§6.5), 그래서 손실은 문턱을 넘을 때 이벤트로만
    올라온다. 여기 담기는 것이 그 이벤트가 실어 온 관측 손실률이다.
    """
    lib_loss_pct: float = 0.0

    @property
    def loss_pct(self) -> float:
        return self.lib_loss_pct

_STATE_TO_EDGE = {STATE_STARTUP: State.STARTUP, STATE_PREOP: State.PREOP,
                  STATE_RUN: State.RUN}
_EDGE_TO_STATE = {int(v): k for k, v in _STATE_TO_EDGE.items()}
_EDGE_TO_STATE[int(State.FAILSAFE)] = 3


class LiveSession:
    """모듈 하나와의 세션. **버스는 edgelib 이 통째로 잡는다.**

    edgelib 은 버스 전체를 보고 세션은 노드 하나를 본다. 그 차이는 여기서 흡수한다 —
    이미지에서 이 노드의 구간만 잘라 내는 것이 전부다.
    """

    #: 화면에 무엇을 띄울지 가르는 표시. 파이썬 세션에는 이 속성이 없다
    engine = "edgelib"

    def __init__(self, bus: EdgeBus, node: int, log=None):
        self.bus = bus
        self.node = node
        self._log = log or (lambda _m: None)
        self._lock = threading.Lock()

        self.state = STATE_STARTUP
        self.snap = LiveSnapshot()
        self.pd_len: dict[int, int] = {}
        self.pd_out_len: dict[int, int] = {}
        self.cycle_s = 0.100

        self.out_qual = 0
        self.out_data: dict[int, bytearray] = {}

        self.events_active: dict[tuple[int, int], dict] = {}
        self.events_recent: list[dict] = []
        self.events_overflow = False

        self._read_layout()

    # ── PD 배치 ─────────────────────────────────────────────────────────────
    def _read_layout(self) -> None:
        """PD 가 이미지의 어디에 얼마만큼 앉는지 **다시** 읽는다.

        한 번 읽어 두면 되는 값이 아니다. 포트 모드를 바꾸면 그 포트가 차지하는
        바이트 수가 달라지고 뒤쪽 포트가 통째로 밀린다. 전원을 켜면 모든 포트가
        `DEACTIVATED` 이므로 처음 읽은 값은 전부 0 이다 — 그것을 들고 있으면
        RUN 에 들어가도 자를 길이가 0 이라 값이 하나도 안 보인다.
        """
        info = self.bus.node_info(self.node)
        self._in_off = info.image_in_off
        self._out_off = info.image_out_off
        self._in_len = info.pd_in
        self._out_len = info.pd_out
        for p in info.ports:
            self.pd_len[p.port] = p.pd_in
            self.pd_out_len[p.port] = p.pd_out

    # ── 열기 ────────────────────────────────────────────────────────────────
    @classmethod
    def open(cls, node: int | None = None, config_path: str | None = None,
             log=None) -> "LiveSession":
        """버스를 열고 노드 하나에 붙는다. `node` 가 None 이면 처음 것."""
        bus = EdgeBus(config_path)
        if node is None:
            found = bus.nodes()
            if not found:
                bus.close()
                raise SessionError("no module answered on the bus")
            node = found[0].address
        return cls(bus, node, log)

    # ── 상태 전이 ───────────────────────────────────────────────────────────
    def to_preop(self) -> None:
        try:
            self.bus.setmode(State.PREOP)
        except EdgeError as e:
            raise SessionError(str(e)) from e
        self.state = STATE_PREOP

    def to_run(self, ports: dict, cycle_s: float | None = None) -> None:
        """`ports` 는 포트 → (in, out) 크기. **크기는 이미 edgelib 이 안다** —
        받아 두기만 하고, 어긋나면 그것대로 알린다."""
        self._read_layout()      # 여기까지 오는 길이 여럿이라 한 번 더 확인한다
        for p, (i, o) in ports.items():
            if self.pd_len.get(p, i) != i or self.pd_out_len.get(p, o) != o:
                self._log(f"port {p}: caller says PD {i}/{o}, "
                          f"the module says {self.pd_len.get(p)}/"
                          f"{self.pd_out_len.get(p)}")
        if cycle_s:
            self.cycle_s = cycle_s
        # **주기는 설정 파일이 정한다.** 자동 구성으로 열었으면 그 안의 기본값이고,
        # 그것을 바꾸려면 설정 파일을 바꿔야 한다 — 라이브러리가 지킬 값이기 때문이다.
        self.out_qual = 0
        self.out_data = {p: bytearray(n) for p, n in self.pd_out_len.items() if n}

        try:
            self.bus.setmode(State.RUN)
        except EdgeError as e:
            raise SessionError(str(e)) from e

        self.state = STATE_RUN
        self.snap = LiveSnapshot()
        # **한 줄이면 된다.** 주기도 워치독도 화면 위쪽 Bus 칸에 이미 있고,
        # 어느 엔진으로 도는지는 상태 표시가 손실률을 띄우는 것으로 갈린다.
        self._log("entered RUN")

    def stop_run(self) -> None:
        try:
            self.bus.setmode(State.PREOP)
        except EdgeError as e:
            raise SessionError(str(e)) from e
        self.state = STATE_PREOP

    def close(self) -> None:
        try:
            self.bus.close()
        except Exception:       # noqa: BLE001 — 닫는 중의 실패는 삼킨다
            pass

    # ── 프로세스 데이터 ─────────────────────────────────────────────────────
    def poll(self) -> PdSnapshot:
        """스냅샷을 새로 뜬다. **버스를 타지 않는다** — C 스레드가 갱신한 것을 복사한다.

        GUI 는 자기 주기(수백 ms)로 이것을 부르면 되고, 그 사이 실제 주기 교환은
        설정한 간격으로 계속 돌고 있다.
        """
        img = self.bus.image_in()
        seg = img[self._in_off:self._in_off + self._in_len]

        prev = self.snap
        snap = LiveSnapshot(cycles=prev.cycles + 1,
                            lib_loss_pct=getattr(prev, "lib_loss_pct", 0.0))
        if seg:
            snap.mst = seg[0]
            snap.ok = True
            off = 1
            for p in sorted(self.pd_len):
                n = self.pd_len[p]
                if n and off + n <= len(seg):
                    snap.ports[p] = bytes(seg[off:off + n])
                off += n
        # **손실은 이벤트가 말한다.** 라이브러리가 세운 0x0102 / 0x0101 이 그것이고,
        # 그 qualifier 가 관측 손실률(%)이다 (API §6.5).
        snap.lib_loss_pct = 0.0
        for ev in self.bus.get_event():
            if ev.from_library and ev.channel == self.node:
                snap.lib_loss_pct = float(ev.qualifier)
        self.snap = snap
        return snap

    def set_output(self, port: int, data: bytes) -> None:
        with self._lock:
            n = self.pd_out_len.get(port, 0)
            if n:
                self.out_data[port] = bytearray(bytes(data)[:n].ljust(n, b"\x00"))
            self._push_out()

    def set_oe(self, port: int, on: bool) -> None:
        """포트의 출력 허용 비트. **끄면 장치가 자기 안전값으로 간다.**"""
        with self._lock:
            bit = 1 << (port - 1)
            self.out_qual = (self.out_qual | bit) if on else (self.out_qual & ~bit)
            self._push_out()

    def _push_out(self) -> None:
        """이 노드 구간을 채워 이미지 전체를 다시 쓴다.

        edgelib 은 **부분 갱신을 받지 않는다** — 짧은 것을 받아 앞쪽만 갈아 끼우면
        뒤쪽 노드가 언제 적 값인지 아무도 모르게 되기 때문이다. 그래서 전체를 만든다.
        """
        total = self.bus.image_size()[1]
        img = bytearray(total)
        seg = bytearray([self.out_qual])
        for p in sorted(self.pd_out_len):
            n = self.pd_out_len[p]
            if n:
                seg += bytes(self.out_data.get(p) or bytearray(n))[:n]
        img[self._out_off:self._out_off + len(seg)] = seg
        self.bus.image_out(bytes(img))

    # ── 포트 설정 ───────────────────────────────────────────────────────────
    def write_port_config(self, port: int, mode: int) -> None:
        try:
            self.bus.iol_port_configuration(self.node, port, PortConfig(mode=mode))
        except EdgeError as e:
            raise SessionError(f"port {port} config failed: {e}") from e
        # 모드가 바뀌면 PD 자리가 달라진다. 라이브러리는 이미 다시 계산했고,
        # 세션도 따라가야 한다 — 안 그러면 RUN 에서 값이 안 보인다.
        self._read_layout()

    def port_status(self, port: int) -> dict | None:
        try:
            s = self.bus.iol_port_status(self.node, port)
        except EdgeError:
            return None
        return {
            "status": s.status, "quality": s.quality,
            "revision_id": s.revision_id, "rate": s.rate,
            "cycletime": s.cycletime, "pd_in": s.pd_in, "pd_out": s.pd_out,
            "vendor_id": s.vendor_id, "device_id": s.device_id,
            "diag": [],
        }

    # ── 이벤트 ──────────────────────────────────────────────────────────────
    #
    # 사본은 edgelib 이 든다 (C 쪽 cycle.c). 여기서는 GUI 가 쓰는 모양으로 옮길 뿐이다.
    def _refresh(self) -> None:
        self.events_active.clear()
        for ev in self.bus.get_event():
            if ev.node not in (0, self.node):
                continue
            self.events_active[(ev.channel, ev.code)] = {
                "node": ev.node or self.node, "channel": ev.channel,
                "mode": ev.mode, "type": ev.type, "code": ev.code,
                "value": ev.qualifier, "ts_ms": ev.timestamp_ms,
                "from_library": ev.from_library,
            }

    def events_resync(self) -> None:
        self._refresh()

    def events_poll(self) -> None:
        self._refresh()

    def has_events(self) -> bool:
        return bool(self.events_active)

    def clear_errors(self) -> bool:
        """**해소된 것만** 지운다. 고장이 계속되는 중이면 지워지지 않는다."""
        try:
            self.bus.clear_event(self.node)
        except EdgeError as e:
            raise SessionError(str(e)) from e
        self._refresh()
        gone = not any(e["type"] == 3 for e in self.events_active.values())
        self._log("errors cleared" if gone else "errors remain - fault still active")
        return gone

    # ── ISDU ────────────────────────────────────────────────────────────────
    def isdu_read(self, port: int, index: int, subindex: int = 0,
                  timeout: float = 8.0) -> bytes:
        try:
            return self.bus.iol_device_read(self.node, port, index, subindex,
                                            timeout_s=timeout)
        except EdgeDeviceError as e:
            raise SessionError(
                f"device rejected - ErrorType 0x{e.error_type:04X}") from e
        except EdgeError as e:
            raise SessionError(str(e)) from e

    def isdu_write(self, port: int, index: int, data: bytes,
                   subindex: int = 0, timeout: float = 8.0) -> None:
        try:
            self.bus.iol_device_write(self.node, port, index, data, subindex,
                                      timeout_s=timeout)
        except EdgeDeviceError as e:
            raise SessionError(
                f"device rejected - ErrorType 0x{e.error_type:04X}") from e
        except EdgeError as e:
            raise SessionError(str(e)) from e


# session.py 에서 가져다 쓰는 것들을 여기서도 내보낸다 — 부르는 쪽이 어느 모듈을
# 쓰는지에 따라 import 를 갈라 쓰지 않게 한다
__all__ += ["PdSnapshot", "STATE_NAME", "STATE_PREOP", "STATE_RUN",
            "STATE_STARTUP", "cycletime_us"]
