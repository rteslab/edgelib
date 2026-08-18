"""살아 있는 버스 세션 — 연결 · PREOP · RUN

**RUN 에 올라가면 주기 교환을 멈출 수 없다.** 슬레이브가 통신을 감시하고 있어(코어 §5.2)
그 시간 안에 프레임이 안 가면 출력이 물리적으로 내려간다. 그래서 여기 배경 스레드가
`RUN` 인 동안 계속 돈다.

`STARTUP`·`PREOP` 에는 감시가 없다. 그래서 탐색과 파라미터 작업은 느긋해도 되고, 그
덕에 이 도구가 파이썬만으로 선다.

ISDU 는 스레드가 대신 보낸다 — 사용자 스레드가 직접 쓰면 주기 교환과 프레임이 겹친다.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from .link import Link

# 코어 커맨드
CMD_SET_STATE = 0x03
CMD_GET_EVENT = 0x06
CMD_CLEAR_ERRORS = 0x08
CMD_SET_CYCLE_TIME = 0x12
CMD_CYCLIC_EXCHANGE = 0x20
CMD_GET_UID = 0x32

# 클래스 대역 — IO-Link
CMD_IOL_PORT_CFG_WRITE = 0x41
CMD_IOL_PORT_STATUS = 0x43
CMD_IOL_ISDU_READ = 0x48
CMD_IOL_ISDU_WRITE = 0x49
CMD_IOL_ISDU_RESULT = 0x4E
CMD_IOL_ISDU_ABORT = 0x4F

STATE_STARTUP, STATE_PREOP, STATE_RUN, STATE_FAILSAFE = 0, 1, 2, 3
STATE_NAME = {0: "STARTUP", 1: "PREOP", 2: "RUN", 3: "FAILSAFE"}

ST_OK, ST_PENDING, ST_BAD_PARAM, ST_BAD_STATE = 0, 1, 2, 3
ST_TIMEOUT, ST_DEVICE_ERR = 8, 9
ST_NAME = {0: "OK", 1: "PENDING", 2: "BAD_PARAM", 3: "BAD_STATE", 4: "BAD_CHANNEL",
           5: "BUSY", 6: "UNSUPPORTED", 7: "NOT_SPECIFIED", 8: "TIMEOUT",
           9: "DEVICE_ERR"}

STATUS_RESULT_MASK = 0x0F
STATUS_STATE_MASK = 0x30
STATUS_STATE_SHIFT = 4

# 주기 교환 간격. 워치독은 이것의 3배다 (§5.2).
#
# **커미셔닝 툴은 빠를 필요가 없다.** 여기서 보려는 것은 "RUN 으로 올라가 PD 가
# 흐른다"는 사실뿐이고, 실제 제어 주기는 C 로 도는 edgelib 이 낸다. 파이썬 GIL·GC 가
# 수십 ms 를 통째로 삼켜도 T_WD 300 ms 안에는 들어오도록 크게 잡았다.
CYCLE_S = 0.100


# ── Table E.4 옥텟 풀이 ─────────────────────────────────────────────────────
COM_NAME = {0: "no link", 1: "COM1 4.8k", 2: "COM2 38.4k", 3: "COM3 230.4k"}


def cycletime_us(octet: int) -> int:
    """Table B.3 — 상위 2비트가 시간 기준, 하위 6비트가 배수."""
    mult = octet & 0x3F
    base = (octet >> 6) & 0x03
    if base == 0:
        return max(400, mult * 100)
    if base == 1:
        return 6400 + mult * 400
    if base == 2:
        return 32000 + mult * 1600
    return 400


class SessionError(Exception):
    pass


def parse_event(e: bytes) -> dict:
    """이벤트 레코드 12 옥텟 (§8.2)."""
    return {
        "node": e[0], "channel": e[1], "mode": e[2], "type": e[3],
        "code": e[4] | (e[5] << 8),
        "value": e[6] | (e[7] << 8),
        "ts_ms": e[8] | (e[9] << 8) | (e[10] << 16) | (e[11] << 24),
    }


def read_events(link: Link, addr: int, resync: bool = True) -> list[dict]:
    """노드 하나의 이벤트를 훑는다. **세션 없이도 된다.**

    여러 노드를 한 화면에 모으려면 세션(노드 하나에 묶인다) 밖에서 물어야 한다.
    `RESYNC` 는 첫 요청에만 건다 — 매번 걸면 라운드마다 전체가 다시 미확인이 되어
    루프가 끝나지 않는다 (§8.3).
    """
    out: list[dict] = []
    for rnd in range(8):
        arg = bytes([0x10 if (resync and rnd == 0) else 0x00, 0x00])
        r = link.xact(addr, bytes([CMD_GET_EVENT]) + arg, 0.5)
        if (r is None or len(r) < 5 or r[0] != CMD_GET_EVENT
                or (r[1] & STATUS_RESULT_MASK) != ST_OK):
            return out
        body = r[5:]
        n = len(body) // 12
        for i in range(n):
            ev = parse_event(body[i * 12:(i + 1) * 12])
            ev["addr"] = addr
            out.append(ev)
        if not (r[1] & 0x80) or n == 0:
            return out
    return out


def clear_errors(link: Link, addr: int) -> bool:
    """해소된 것만 지운다 (§5.3). ERR 이 내려갔으면 True."""
    link.xact(addr, bytes([CMD_CLEAR_ERRORS]), 0.5)
    r = link.xact(addr, bytes([CMD_GET_UID]), 0.3)
    return (r is not None and len(r) >= 2 and r[0] == CMD_GET_UID
            and not (r[1] & 0x40))


@dataclass
class PdSnapshot:
    """마지막 주기 교환 결과. 스레드가 갱신하고 GUI 가 읽는다."""
    ok: bool = False
    mst: int = 0
    ports: dict[int, bytes] = field(default_factory=dict)
    rtt_ms: float = 0.0
    cycles: int = 0
    errors: int = 0          # 응답이 없었거나 거절당한 사이클
    late: int = 0            # 정한 주기 안에 못 끝낸 사이클
    rtt_max: float = 0.0

    @property
    def loss_pct(self) -> float:
        return 100.0 * self.errors / self.cycles if self.cycles else 0.0

    def pq(self, port: int) -> bool:
        return bool((self.mst >> (port - 1)) & 1)

    def rdy(self, port: int) -> bool:
        return bool((self.mst >> (4 + port - 1)) & 1)


class Session:
    """모듈 하나와의 세션. GUI 는 이것만 붙들면 된다."""

    def __init__(self, link: Link, node: int, log=None):
        self.link = link
        self.node = node
        self.state = STATE_STARTUP
        self.snap = PdSnapshot()
        self.pd_len: dict[int, int] = {}       # 포트 → PD in 크기
        self.pd_out_len: dict[int, int] = {}

        # 내보낼 출력. **주기 스레드가 매 사이클 이것을 읽어 보낸다.**
        # `out_qual` 은 OE 한정자 — 비트가 0 이면 그 포트의 출력은 장치의
        # 안전값으로 간다. 기본은 전부 0 이다: 사람이 켜기 전에는 아무것도 내보내지
        # 않는 편이 안전하다.
        self.out_qual = 0
        self.out_data: dict[int, bytearray] = {}

        # 사용자가 정한 주기. **여기서 실제로 그 주기로 돌아 본다** — 파이썬이
        # 버티지 못하면 놓친 사이클로 드러나고, 그게 주기를 정하는 근거가 된다.
        self.cycle_s = CYCLE_S
        self._told_fail = False

        # 이벤트 사본 — 슬레이브는 미확인분만 주므로 마스터가 들고 있어야 한다
        self.events_active: dict[tuple[int, int], dict] = {}
        self.events_recent: list[dict] = []
        self.events_overflow = False
        self._evt_flag = False

        self._log = log or (lambda _m: None)
        self._lock = threading.Lock()          # 링크는 한 번에 하나만 쓴다
        self._jobs: queue.Queue = queue.Queue()
        self._run = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 저수준 ──────────────────────────────────────────────────────────────
    def _ask(self, cmd: int, arg: bytes = b"", timeout: float = 0.3,
             retries: int = 0) -> bytes | None:
        """요청 하나. `retries` 는 **다시 물어도 답이 같은 커맨드에만** 준다.

        슬레이브는 요청 수신 후 1 ms 를 기다렸다가 답하고(`BP_CFG_T_TURNAROUND_US`),
        실측 왕복은 1.4~5.9 ms 다. 그동안 파이썬이 GIL 을 뺏기면 응답을 통째로
        놓친다 — GUI 가 도는 중 1654 프레임에 1번 관측됐다.

        **재시도로 가릴 수 있는 것과 없는 것이 갈린다.** 상태를 바꾸는 커맨드나
        커서를 옮기는 이벤트 회수는 두 번 보내면 뜻이 달라지므로 기본값 0 이다.
        근본 해결은 통신을 C 로 옮기는 것이다 (설계 §2).
        """
        for _ in range(retries + 1):
            rsp = self.link.xact(self.node, bytes([cmd]) + arg, timeout)
            if rsp is not None and len(rsp) >= 2 and rsp[0] == cmd:
                return rsp
        return None

    def _set_state(self, st: int) -> None:
        self.link.send_broadcast(bytes([CMD_SET_STATE, st]))
        time.sleep(0.02)

    def _probe(self) -> int:
        rsp = self._ask(CMD_GET_UID, retries=2)
        if rsp is None:
            return -1
        return (rsp[1] & STATUS_STATE_MASK) >> STATUS_STATE_SHIFT

    # ── 상태 전이 ───────────────────────────────────────────────────────────
    def to_preop(self) -> None:
        """STARTUP → PREOP. 여기서 포트 설정과 파라미터를 만진다."""
        with self._lock:
            self._set_state(STATE_STARTUP)
            self._set_state(STATE_PREOP)
            st = self._probe()
        if st != STATE_PREOP:
            raise SessionError(f"PREOP failed (state {STATE_NAME.get(st, st)})")
        self.state = STATE_PREOP
        self._log("entered PREOP")

    def to_run(self, ports: dict[int, tuple[int, int]],
               cycle_s: float | None = None) -> None:
        """PREOP → RUN. `ports` 는 포트 → (in, out) 크기.

        **사이클 시간을 먼저 통보해야 한다** — 안 하면 슬레이브가 RUN 을 거부한다
        (§5.2). 감시 없이 출력을 든 채 올라가는 것을 막는 방어선이다.
        """
        self.pd_len = {p: n[0] for p, n in ports.items()}
        self.pd_out_len = {p: n[1] for p, n in ports.items()}
        for p, n in ports.items():
            if n[1]:
                self.out_data.setdefault(p, bytearray(n[1]))

        # **OE 는 사람이 켠다.** 켜는 순간 우리가 보내는 값(기본 0)이 실제로
        # 장치에 가므로, 무엇이 물려 있는지 아는 사람만 결정할 수 있다.
        self.out_qual = 0

        with self._lock:
            # 소거되지 않은 에러가 있으면 RUN 에 못 간다 (§5.3)
            rsp = self._ask(CMD_GET_UID, retries=2)
            if rsp is not None and (rsp[1] & 0x40):
                self._log("error latch present - clearing")
                self._ask(CMD_CLEAR_ERRORS)

            # **FAILSAFE 에서 되돌아온다.** 주기를 너무 짧게 잡아 워치독이 물리면
            # 모듈이 여기 남는데, `SET_CYCLE_TIME` 은 PREOP 에서만 받는다 (§6.1).
            # 그대로 두면 "한 번 놓치면 다시 못 올라감"이 되어, 주기를 시험해 보는
            # 일 자체가 불가능해진다.
            st = self._probe()
            if st != STATE_PREOP:
                self._log(f"in {STATE_NAME.get(st, st)} - returning to PREOP")
                self._ask(CMD_CLEAR_ERRORS)
                # **FAILSAFE 에서 나가는 길은 STARTUP 하나뿐이다** (§5 전이표).
                # 곧바로 PREOP 을 지시하면 BAD_STATE 로 거절당한다 — 에러가 남은
                # 노드가 FAILSAFE 를 경유해 올라오는 길을 막아 둔 것이다.
                self._set_state(STATE_STARTUP)
                self._set_state(STATE_PREOP)
                st = self._probe()
                if st != STATE_PREOP:
                    raise SessionError(
                        f"cannot leave {STATE_NAME.get(st, st)}")

            if cycle_s:
                self.cycle_s = cycle_s
            t_us = int(self.cycle_s * 1e6)
            # 공칭과 상한을 같은 값으로 준다 — 워치독이 이것의 3배다 (§5.2)
            arg = t_us.to_bytes(4, "little") + t_us.to_bytes(4, "little")
            r = self._ask(CMD_SET_CYCLE_TIME, arg)
            if r is None or (r[1] & STATUS_RESULT_MASK) != ST_OK:
                raise SessionError("SET_CYCLE_TIME rejected")

            # **SET_STATE 는 브로드캐스트라 확인이 없다** — 프레임이 유실되면
            # 아무도 모른다. 그래서 물어보고, 아니면 다시 보낸다. 멱등하므로
            # 다시 보내는 것이 정상 수단이다 (§5, "낙오한 노드를 합류시킨다").
            st = -1
            for _ in range(3):
                self._set_state(STATE_RUN)
                st = self._probe()
                if st == STATE_RUN:
                    break
                self._log(f"RUN not taken (state {STATE_NAME.get(st, st)})"
                          f" - retrying")

        if st != STATE_RUN:
            raise SessionError(f"RUN failed (state {STATE_NAME.get(st, st)})")

        # 확인이 끝났으니 곧바로 펌프를 돌린다. 워치독은 RUN 진입과 동시에 도는데
        # 위 확인 왕복은 몇 ms 라 `T_WD` 안에 넉넉히 들어온다.
        self.state = STATE_RUN
        self.snap = PdSnapshot()               # 통계는 이번 RUN 것만 센다
        self._told_fail = False
        self._run.set()
        self._ensure_thread()

        self._log(f"entered RUN - cycle {self.cycle_s * 1e3:.1f} ms, "
                  f"watchdog {self.cycle_s * 3e3:.1f} ms")

    def set_output(self, port: int, data: bytes) -> None:
        """다음 사이클에 나갈 출력 바이트를 바꾼다."""
        with self._lock:
            n = self.pd_out_len.get(port, 0)
            if n:
                self.out_data[port] = bytearray(bytes(data)[:n].ljust(n, bytes(1)))

    def set_oe(self, port: int, on: bool) -> None:
        """포트의 출력 허용 비트. **끄면 장치가 자기 안전값으로 간다.**"""
        with self._lock:
            bit = 1 << (port - 1)
            self.out_qual = (self.out_qual | bit) if on else (self.out_qual & ~bit)

    def stop_run(self) -> None:
        """RUN 을 떠난다. 주기 교환이 멈추므로 **반드시 상태를 내려야 한다.**"""
        self._run.clear()
        time.sleep(self.cycle_s * 2)
        with self._lock:
            self._set_state(STATE_PREOP)
            self.state = STATE_PREOP
        self._log("back to PREOP")

    def close(self) -> None:
        self._run.clear()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        try:
            with self._lock:
                self._set_state(STATE_STARTUP)
        except Exception:       # noqa: BLE001 — 닫는 중의 실패는 삼킨다
            pass

    # ── 주기 스레드 ─────────────────────────────────────────────────────────
    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._run.is_set():
                time.sleep(0.01)
                continue

            t0 = time.monotonic()
            with self._lock:
                out = bytearray([self.out_qual])
                for p in sorted(self.pd_out_len):
                    n = self.pd_out_len[p]
                    if not n:
                        continue
                    buf = self.out_data.get(p) or bytearray(n)
                    out += bytes(buf[:n]).ljust(n, bytes(1))

            # **기다리는 시간은 사이클을 넘지 않는다.** 고정 200 ms 로 두면
            # 응답 하나를 놓쳤을 때 그 200 ms 동안 버스가 조용해지고, 워치독
            # (사이클의 3배)이 물려 모듈이 FAILSAFE 로 떨어진다 — 그 뒤로는 모든
            # 주기 교환이 BAD_STATE 라 손실 100 % 가 된다. 한 번 놓치는 대가는
            # 한 사이클이어야 한다.
            wait = max(0.004, min(self.cycle_s, 0.05))
            with self._lock:
                rsp = self._ask(CMD_CYCLIC_EXCHANGE, bytes(out), timeout=wait)
            rtt = (time.monotonic() - t0) * 1e3

            prev = self.snap
            snap = PdSnapshot(cycles=prev.cycles + 1, errors=prev.errors,
                              late=prev.late, rtt_ms=rtt,
                              rtt_max=max(prev.rtt_max, rtt))
            if rtt > self.cycle_s * 1e3:
                snap.late += 1          # 이 사이클은 정한 주기를 넘겼다
            if rsp is None or (rsp[1] & STATUS_RESULT_MASK) != ST_OK:
                snap.errors += 1
                # **첫 실패는 이유를 남긴다.** 개수만 세면 "응답이 없었다"와
                # "거절당했다"가 같은 숫자로 보여, 주기를 의심하는 데서 멈춘다.
                if not self._told_fail:
                    self._told_fail = True
                    if rsp is None:
                        self._log(f"cycle: no response within {wait * 1e3:.0f} ms"
                                  f" (sent {len(out)} B)")
                    else:
                        rc = rsp[1] & STATUS_RESULT_MASK
                        got = (rsp[1] & STATUS_STATE_MASK) >> STATUS_STATE_SHIFT
                        self._log(f"cycle refused: {ST_NAME.get(rc, rc)}"
                                  f" in {STATE_NAME.get(got, got)}"
                                  f"  (STATUS 0x{rsp[1]:02X}, {len(rsp)} B)")
                # 상태가 틀려서 거절당하는 것은 놓친 프레임과 전혀 다른 일이다.
                # 워치독에 걸려 FAILSAFE 로 떨어지면 이 뒤로 영영 안 돌아온다 —
                # 조용히 손실만 세면 사용자는 주기를 의심하게 된다.
                if rsp is not None and (rsp[1] & STATUS_RESULT_MASK) == ST_BAD_STATE:
                    got = (rsp[1] & STATUS_STATE_MASK) >> STATUS_STATE_SHIFT
                    if got != STATE_RUN:
                        self.state = got
                        self._run.clear()
                        self._log(f"left RUN - now {STATE_NAME.get(got, got)}"
                                  f" (watchdog?) - cycle may be too short")
            else:
                # EVT 는 매 응답에 실려 온다 (§8.2). 루프에 분기가 하나만 생긴다.
                self._evt_flag = bool(rsp[1] & 0x80)
                data = rsp[2:]
                snap.ok = True
                snap.mst = data[0] if data else 0
                off = 1
                for p in sorted(self.pd_len):
                    n = self.pd_len[p]
                    if n and off + n <= len(data):
                        snap.ports[p] = bytes(data[off:off + n])
                    off += n
            self.snap = snap

            # 다음 사이클까지. 늦었으면 곧바로 다음을 돈다
            left = self.cycle_s - (time.monotonic() - t0)
            if left > 0:
                time.sleep(left)

    # ── 포트 설정 ───────────────────────────────────────────────────────────
    def write_port_config(self, port: int, mode: int) -> None:
        """PREOP 전용. 설정을 바꾸면 그 포트가 재시작된다."""
        ab = bytes([0x80, 0x00, mode, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        with self._lock:
            r = self._ask(CMD_IOL_PORT_CFG_WRITE, bytes([port]) + ab, timeout=0.5)
        if r is None or (r[1] & STATUS_RESULT_MASK) != ST_OK:
            raise SessionError(f"port {port} config failed")

    def port_status(self, port: int) -> dict | None:
        """PortStatusList (Table E.4). **한 번 물어보면 다 온다** — 링크가 지금
        어떤 조건으로 돌고 있는지가 전부 여기 실려 있고, 따로 물을 방법도 없다."""
        with self._lock:
            r = self._ask(CMD_IOL_PORT_STATUS, bytes([port, 0xFF, 0xF0]),
                         timeout=0.5, retries=2)
        if r is None or (r[1] & STATUS_RESULT_MASK) != ST_OK or len(r) < 17:
            return None
        ab = r[2:]
        n_diag = ab[15]
        diag = [((ab[16 + 3 * i]), (ab[17 + 3 * i] << 8) | ab[18 + 3 * i])
                for i in range(n_diag) if len(ab) >= 19 + 3 * i]
        return {
            "status": ab[2],
            "quality": ab[3],           # bit0 PD invalid · bit1 PDout invalid
            "revision_id": ab[4],       # 상위 니블 major, 하위 minor
            "rate": ab[5],              # 0=미검출, 1..3 = COM1..COM3
            "cycletime": ab[6],         # Table B.3 부호화 옥텟
            "pd_in": ab[7], "pd_out": ab[8],
            "vendor_id": (ab[9] << 8) | ab[10],
            "device_id": (ab[12] << 16) | (ab[13] << 8) | ab[14],
            "diag": diag,
        }

    # ── 이벤트 ──────────────────────────────────────────────────────────────
    #
    # 슬레이브는 **미확인분만** 돌려준다 (`acked` 가 커서). 한 번 읽은 항목은 다음
    # 회수에 안 나오므로, 마스터가 사본을 들고 있지 않으면 화면에서 사라진다.
    #
    # 그래서 세션이 사본을 유지한다:
    #   mode 3 appeared     → 유지
    #   mode 2 disappeared  → 제거 (해소됐다)
    #   mode 1 single shot  → 최근 것만 따로. 순간이지 상태가 아니다

    def events_resync(self) -> None:
        """사본을 처음부터 만든다.

        앞서 다른 프로그램이 읽어 간 항목은 `EVT` 가 내려가 있어 그냥 물으면 안
        온다. 전체를 다시 받는 길이 `filter = 0x10` 하나다 (§8.3).

        **RESYNC 는 첫 요청에만 건다** — 매번 걸면 라운드마다 테이블 전체가 다시
        미확인이 되어 루프가 끝나지 않는다.
        """
        self.events_active.clear()
        self.events_recent.clear()
        self._fetch_events(resync=True)

    def events_poll(self) -> None:
        """`STATUS.EVT` 가 서 있을 때만 부르면 된다. 안 서 있으면 받을 것이 없다."""
        self._fetch_events(resync=False)

    def _fetch_events(self, resync: bool) -> None:
        for rnd in range(8):
            arg = bytes([0x10 if (resync and rnd == 0) else 0x00, 0x00])
            with self._lock:
                r = self._ask(CMD_GET_EVENT, arg, timeout=0.5)
            if r is None or (r[1] & STATUS_RESULT_MASK) != ST_OK or len(r) < 5:
                return

            self.events_overflow |= bool(r[2] & 0x01)
            body = r[5:]
            n = len(body) // 12
            for i in range(n):
                self._put_event(body[i * 12:(i + 1) * 12])

            # 확인 처리 뒤의 EVT 가 "더 있음"을 말한다. 별도 비트가 없는 이유다.
            if not (r[1] & 0x80) or n == 0:
                return

    def _put_event(self, e: bytes) -> None:
        ev = {
            "node": e[0], "channel": e[1], "mode": e[2], "type": e[3],
            "code": e[4] | (e[5] << 8),
            "value": e[6] | (e[7] << 8),
            "ts_ms": e[8] | (e[9] << 8) | (e[10] << 16) | (e[11] << 24),
        }
        key = (ev["channel"], ev["code"])

        if ev["mode"] == 1:                       # single shot — 지나간 일이다
            self.events_recent.insert(0, ev)
            del self.events_recent[8:]
            return
        if ev["mode"] == 2:                       # 해소됐다
            self.events_active.pop(key, None)
            return
        self.events_active[key] = ev              # 성립 중

    def clear_errors(self) -> bool:
        """`CMD_CLEAR_ERRORS` — **해소된 것만** 지운다 (§5.3).

        고장이 계속되는 중이면 지워지지 않는다. 고치지 않고 소거만 반복해 RUN 에
        들어가는 길을 막는 것이 이 규칙의 전부다.

        @return ERR 이 내려갔으면 True
        """
        with self._lock:
            self._ask(CMD_CLEAR_ERRORS, timeout=0.5)
            r = self._ask(CMD_GET_UID)
        gone = (r is not None) and not (r[1] & 0x40)
        self._log("errors cleared" if gone else "errors remain - fault still active")
        return gone

    def has_events(self) -> bool:
        """마지막 주기 응답의 `STATUS.EVT`. RUN 중에는 매 사이클 온다."""
        return self._evt_flag

    # ── ISDU ────────────────────────────────────────────────────────────────
    def isdu_read(self, port: int, index: int, subindex: int = 0,
                  timeout: float = 8.0) -> bytes:
        """개시 → RDY 대기 → 회수. **RUN 중에도 주기 교환을 멈추지 않는다.**"""
        ab = bytes([0x30, 0x01, index >> 8, index & 0xFF, subindex])
        with self._lock:
            r = self._ask(CMD_IOL_ISDU_READ, bytes([port]) + ab, timeout=0.5)
        if r is None or (r[1] & STATUS_RESULT_MASK) != ST_OK:
            rc = r[1] & STATUS_RESULT_MASK if r else -1
            raise SessionError(f"ISDU read rejected ({ST_NAME.get(rc, rc)})")
        return self._isdu_collect(port, timeout)

    def isdu_write(self, port: int, index: int, data: bytes,
                   subindex: int = 0, timeout: float = 8.0) -> None:
        ab = bytes([0x30, 0x00, index >> 8, index & 0xFF, subindex]) + data
        with self._lock:
            r = self._ask(CMD_IOL_ISDU_WRITE, bytes([port]) + ab, timeout=0.5)
        if r is None or (r[1] & STATUS_RESULT_MASK) != ST_OK:
            rc = r[1] & STATUS_RESULT_MASK if r else -1
            raise SessionError(f"ISDU write rejected ({ST_NAME.get(rc, rc)})")
        self._isdu_collect(port, timeout)

    def _isdu_collect(self, port: int, timeout: float) -> bytes:
        """결과가 나올 때까지 되묻는다.

        **응답이 없는 것은 실패가 아니다.** 회수는 몇 번을 물어도 같은 답이 나오는
        질문이라, 한 번 놓쳤으면 다시 물으면 된다. 반이중 버스에서 파이썬이 방향
        전환 직후에 밀리면 슬레이브의 답이 통째로 사라지는 일이 실제로 있다 —
        1654 프레임에 1번, GUI 가 돌 때만.

        끝을 정하는 것은 전체 `timeout` 이다. 연속 유실은 따로 세어, 링크가 정말
        죽은 것을 "곧 오겠지"로 오래 기다리지 않는다.
        """
        end = time.monotonic() + timeout
        misses = 0
        while True:
            with self._lock:
                r = self._ask(CMD_IOL_ISDU_RESULT, bytes([port]),
                             timeout=0.5, retries=1)
            if r is None:
                misses += 1
                if misses > 3 or time.monotonic() >= end:
                    raise SessionError(
                        f"ISDU collect: no response ({misses} in a row)")
                time.sleep(0.01)
                continue
            misses = 0

            rc = r[1] & STATUS_RESULT_MASK
            if rc == ST_PENDING:
                if time.monotonic() >= end:
                    raise SessionError("ISDU timeout")
                time.sleep(0.02)
                continue

            ab = r[2:]
            if rc == ST_OK:
                if len(ab) >= 2 and ab[0] == 0xFF and ab[1] == 0xF0:
                    return b""                       # VoidBlock — 쓰기 완료
                return bytes(ab[5:]) if len(ab) > 5 else b""

            if len(ab) >= 4 and ab[0] == 0xFF and ab[1] == 0xFF:
                raise SessionError(
                    f"device rejected - ErrorType 0x{ab[2]:02X}{ab[3]:02X}")
            raise SessionError(f"ISDU failed ({ST_NAME.get(rc, rc)})")
