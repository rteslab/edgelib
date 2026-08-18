"""설정에 맞춘 **돌아가는 예제 프로그램**을 낸다

Copyright (c) 2026 RTES Co., Ltd. All rights reserved.

`codegen.py` 는 **형**을 낸다 — 구조체와 클래스. 여기는 **프로그램**을 낸다. 둘을
한 파일에 두지 않는 이유는 쓰임이 다르기 때문이다. 형은 응용이 계속 include 하는
것이고, 예제는 한 번 읽고 자기 코드로 갈아타는 발판이다.

**고객이 처음 여는 파일이다.** API 문서를 읽기 전에 이것부터 돌려 보고, 여기서
자기 코드를 시작한다. 그래서 API 를 종류별로 한 번씩 다 밟아 준다 — 어떤 것이
있는지 목록으로 아는 것과, 그것이 실제로 어떻게 불리는지 보는 것은 다르다.

실시간 구간은 **모니터처럼 제자리에서 갱신한다.** 찍어 내리면 1초마다 화면이 밀려
정작 바뀐 값이 안 보인다. 그래서 출력 함수는 찍지 않고 **줄 목록을 돌려주고**,
호출부가 한 프레임을 통째로 만들어 한 번에 그린다 — 깜빡임도 없고, 고객이 자기
UI 로 가져가기도 쉽다.

생성되는 글은 **영어다.** 고객에게 나가는 산출물이고, 리눅스 기본 폰트에서 한글이
깨지는 자리가 있다.
"""

from __future__ import annotations

from .codegen import _fields, _groups, _has_qual, _is_bool, _scaled

ISDU_STD = [
    (0x0010, "VendorName"),
    (0x0012, "ProductName"),
    (0x0015, "SerialNumber"),
    (0x0017, "FirmwareRevision"),
    (0x0018, "ApplicationSpecificTag"),
]


def _port_modes(cfg: dict) -> dict:
    """(노드, 포트) → 모드 문자열. 설정 파일의 `nodes` 절이 출처다."""
    out = {}
    for nd in cfg.get("nodes", []):
        for pt in nd.get("ports", []):
            out[(nd["address"], pt["port"])] = pt.get("mode", "DEACTIVATED")
    return out


def _pd_ports(cfg: dict) -> list:
    """PD 가 오가는 (노드, 포트)."""
    return sorted({(g["node"], g["port"]) for g in _groups(cfg)})


def _isdu_ports(cfg: dict) -> list:
    """**IO-Link 포트만.** SIO 에는 디바이스가 없어 ISDU 가 성립하지 않는다 —
    물으면 `0x4003`(DEVICE_NOT_ACCESSIBLE) 이 돌아오고, 그건 모듈이 옳다.
    고객이 처음 여는 파일에 거절 스무 줄이 지나가면 안 된다."""
    modes = _port_modes(cfg)
    return [k for k in _pd_ports(cfg) if modes.get(k, "").startswith("IOL_")]


def _do_cq_out(cfg: dict) -> list:
    """C/Q 를 구동하는 출력 그룹 — `(노드, 포트, 필드이름)`.

    **필드 이름을 지어내지 않고 `_fields()` 에서 가져온다.** 생성 구조체의 이름은
    `image.py` 의 엔트리 이름이 정하고 그것은 바뀔 수 있다. 여기서 따로 적어 두면
    어긋나는데, dataclass 는 없는 속성에 대입해도 조용히 만들어 주므로 증상이
    "아무 일도 안 일어남" 으로 나온다.
    """
    modes = _port_modes(cfg)
    out = []
    for g in _groups(cfg):
        if g["dir"] != "out":
            continue
        if modes.get((g["node"], g["port"])) != "DO_CQ":
            continue
        for nm, _e in _fields(g):
            out.append((g["node"], g["port"], nm))
            break                      # SIO 출력은 비트 하나뿐이다
    return sorted(out)


def emit(cfg: dict, name: str) -> str:
    """`<name>_example.py` 의 내용. 이름·단위·비트가 이 설비 것으로 박혀 나온다."""
    groups = _groups(cfg)
    ins = [g for g in groups if g["dir"] == "in"]
    outs = [g for g in groups if g["dir"] == "out"]

    parts = [
        _header(name),
        _constants(name, _isdu_ports(cfg)),
        _screen(),
        _isdu_worker(),
        _pd_lines(ins, _do_cq_out(cfg)),
        _event_lines(),
        _survey(),
        _main(outs, _do_cq_out(cfg)),
    ]
    return "\n\n".join(p.rstrip() for p in parts) + "\n"


# ── 머리말 ──────────────────────────────────────────────────────────────────
def _header(name: str) -> str:
    return f'''#!/usr/bin/env python3
"""{name} - a worked example for libedgelib

EdgeConfig generated this from the modules it found on the bus, so the names,
units and bit positions below are the ones on your machine. Copy it, delete
what you do not need, and start your application here.

It walks the whole API once, in the order you would use it:

    1. open the bus and see what is on it   node_count / node_info / image_size
    2. ask the master and the ports         iol_master_ident / iol_port_status
                                            iol_readback_port_configuration
    3. read device parameters               iol_device_read / iol_device_write
    4. go to RUN                            setmode / getmode
    5. read process data by name            In.read  (image_in underneath)
    6. write outputs                        Out.write (image_out underneath)
    7. watch for trouble                    get_event / clear_event

Then it holds a live monitor showing 5, 7 and 3 at the same time, redrawn in
place so you watch values change instead of scrolling past them.

**Cyclic traffic never stops** while an ISDU is in flight. The background
thread inside libedgelib keeps the frames going even when your Python code is
busy for a second - that is why the watchdog does not trip.

    python3 {name}_example.py [seconds]
"""

import sys
import threading
import time

from edgelib import EdgeBus, EdgeDeviceError, EdgeError, PortMode, State

from {name}_pd import In, Out'''


def _constants(name: str, ports: list) -> str:
    items = "\n".join(f'    (0x{i:04X}, "{n}"),' for i, n in ISDU_STD)
    pl = ", ".join(f"({n}, {p})" for n, p in ports)
    return f'''CONFIG = "{name}.json"

# Standard IO-Link parameters (spec Table B.8). Every device carries these at
# the same index, whoever made it. Put your own index here to read a vendor
# parameter - the call is identical.
ISDU_ITEMS = [
{items}
]

# **IO-Link ports only.** A port in SIO mode has no Device behind it, so asking
# it for a ProductName comes back 0x4003 (DEVICE_NOT_ACCESSIBLE) - and the
# module is right to refuse. Their C/Q and DI arrive as process data instead.
PORTS = [{pl}]'''


# ── 화면 ────────────────────────────────────────────────────────────────────
def _screen() -> str:
    """제자리 갱신. **터미널이 아니면 그냥 찍는다** — `| tee log` 로 받아 두는
    것이 흔한데, 거기에 커서 제어 문자가 섞이면 로그를 읽을 수 없다."""
    return '''class Screen:
    """Redraws one frame in place, like a monitor.

    Printing line after line scrolls the values you are trying to watch off the
    top. Drawing over the same rows keeps your eye in one spot.

    If output is not a terminal - piped to a file or `tee` - it falls back to
    plain printing. Cursor escapes in a log file make it unreadable.
    """

    HOME = "\\033[H"          # cursor to top-left
    CLEAR = "\\033[2J"        # clear whole screen
    CLEAR_EOL = "\\033[K"     # clear to end of line
    CLEAR_EOS = "\\033[J"     # clear to end of screen
    HIDE = "\\033[?25l"
    SHOW = "\\033[?25h"

    def __init__(self):
        self.tty = sys.stdout.isatty()

    def __enter__(self):
        if self.tty:
            sys.stdout.write(self.CLEAR + self.HIDE)
            sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        if self.tty:
            sys.stdout.write(self.SHOW + "\\n")
            sys.stdout.flush()

    def draw(self, lines):
        if not self.tty:
            print("\\n".join(lines) + "\\n")
            return
        # Clear each line as we write it, then wipe whatever is left below.
        # Without that a shorter frame leaves the tail of the previous one.
        body = "".join(l + self.CLEAR_EOL + "\\n" for l in lines)
        sys.stdout.write(self.HOME + body + self.CLEAR_EOS)
        sys.stdout.flush()'''


# ── ISDU ────────────────────────────────────────────────────────────────────
def _isdu_worker() -> str:
    return '''class IsduReader(threading.Thread):
    """Reads device parameters in the background, **one transaction at a time**.

    That is a rule, not a style choice. The module holds one ISDU job per port;
    starting a second before the first is collected comes back EDGE_ERR_BUSY.
    Since `iol_device_read()` returns only once the transaction has finished,
    keeping to one at a time is just a sequential loop - the next request goes
    out on the line after this one, never beside it.

    It runs off the main thread so the monitor stays smooth. A single ISDU can
    take hundreds of milliseconds; the process data should not stop for it.
    """

    def __init__(self, bus, ports, items, pause_s=2.0):
        super().__init__(daemon=True)
        self.bus = bus
        self.ports = ports
        self.items = items
        self.pause_s = pause_s
        self.results = {}          # (node, port, index) -> (label, text)
        self.busy = None           # what is on the wire right now
        self.done = 0              # transactions completed
        self.lock = threading.Lock()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            for node, port in self.ports:
                for index, label in self.items:
                    if self._stop.is_set():
                        return
                    self._one(node, port, index, label)
            # One full sweep done. Wait, then go round again.
            self._stop.wait(self.pause_s)

    def _one(self, node, port, index, label):
        with self.lock:
            self.busy = (node, port, label)

        try:
            data = self.bus.iol_device_read(node, port, index)
            text = data.decode("ascii", "replace").rstrip("\\0 ").strip()
            if not text:
                text = data.hex(" ") or "(empty)"
        except EdgeDeviceError as e:
            # The device refused. Not every device carries every parameter,
            # so this is an answer - not a failure of the bus.
            text = "rejected, ErrorType 0x%04X" % e.error_type
        except EdgeError as e:
            text = "failed - %s" % e

        # **The transaction is over at this line.** Only now does the next
        # request become allowed.
        with self.lock:
            self.results[(node, port, index)] = (label, text)
            self.busy = None
            self.done += 1

    def lines(self):
        """Display lines for the monitor - it does not print either."""
        with self.lock:
            results, busy, done = dict(self.results), self.busy, self.done

        head = "  isdu    %d transactions completed" % done
        if busy:
            head += "   |  in flight: node %d port %d %s" % busy
        out = [head]
        for (node, port, _index), (label, text) in sorted(results.items()):
            out.append("      node %d port %d  %-24s %s"
                       % (node, port, label, text))
        return out'''


# ── 프로세스 데이터 ─────────────────────────────────────────────────────────
def _pd_lines(ins: list, do_cq: list) -> str:
    """**섞어 놓으면 못 읽는다.** IO-Link 디바이스가 보낸 값과, 모듈이 자기 핀에서
    읽은 값(C/Q · DI)은 출처가 다르다. 한 줄에 늘어놓으면 온도 옆에 DI 가 앉아
    무엇이 어디서 온 것인지 알 수 없다. 그래서 줄을 가른다.

        node 1 port 1  IO-Link  PQ ok
          data   temperature 28.5 °C   pressure -0.01 bar   ...
          pin2   DI off

        node 1 port 3  SIO
          C/Q    readback ON
          pin2   DI ON
    """
    L = ['def pd_lines(pd, cq_out=False):',
         '    """Display lines, grouped by where the value came from:',
         '    `data` is the IO-Link Device, `C/Q` and `pin2` are the module\'s',
         '    own pins. Returns lines - the caller draws the frame."""',
         '    out = []']
    if not ins:
        L.append("    return out")
        return "\n".join(L)

    L.append("")
    L.append("    def cell(name, text):")
    L.append('        return "%-26s %-11s" % (name[:26], text)')
    L.append("")
    L.append("    def rows(tag, cells):")
    L.append('        """3 칸씩 끊어 `tag` 를 첫 줄에만 붙인다."""')
    L.append("        for i in range(0, len(cells), 3):")
    L.append('            head = tag if i == 0 else ""')
    L.append('            out.append("      %-6s %s"')
    L.append('                       % (head, "".join(cells[i:i + 3]).rstrip()))')

    for g in ins:
        tag = f"n{g['node']}_p{g['port']}"
        where = f"node {g['node']} port {g['port']}"
        fields = _fields(g)

        # 이름으로 출처를 가른다 — image.py 가 "DI"·"C/Q"·"C/Q readback" 으로
        # 이름 붙이므로 식별자는 di · cq · cq_readback 이 된다
        #  가 "C/Q readback" 을 c_q_readback 으로 만든다 — 슬래시가
        # 밑줄이 되기 때문이다. 접두사를 그 모양으로 본다.
        pin2 = [(nm, e) for nm, e in fields if nm == "di"]
        cq = [(nm, e) for nm, e in fields if nm.startswith("c_q")]
        data = [(nm, e) for nm, e in fields
                if nm != "di" and not nm.startswith("c_q")]

        kind = "SIO" if (cq and not data) else "IO-Link"
        sio = bool(cq and not data)
        drives = any(nd == g["node"] and pt == g["port"]
                     for nd, pt, _f in do_cq)
        L.append("")
        L.append(f"    # ---- {where} ----")
        L.append(f"    p = pd.{tag}")
        if _has_qual(g):
            # `valid` 는 **디바이스의** PD 가 이번 사이클에 유효한가다.
            L.append(f'    out.append("  {where}   {kind}   %s"')
            L.append('               % ("PQ ok" if p.valid'
                     ' else "PQ=0 - do not use"))')
        else:
            # SIO 에는 그런 것이 없다 — 읽은 값이 선의 전압 그 자체라 무효를
            # 선언할 디바이스가 없다 (Table E.10 각주 a).
            L.append(f'    out.append("  {where}   {kind}")')

        if sio:
            # **한 줄에 하나씩.** 값이 둘셋뿐이라 표로 만들 이유가 없고, 세로로
            # 세워 두면 셋이 어긋난 순간 - 보낸 값은 ON 인데 되읽기가 off 인
            # 것 같은 - 이 바로 보인다.
            def row(lbl, expr):
                L.append('    out.append("      %-16s %s"')
                L.append(f'               % ("{lbl}", "ON" if {expr} else "off"))')

            if drives:
                # 보낸 값은 입력 이미지에 없다 — 우리가 쥔 출력 상태에서 온다
                row("C/Q Output", "cq_out")
            for nm, _e in cq:
                row("C/Q Readback" if nm.endswith("readback") else "C/Q Input",
                    f"p.{nm}")
            for nm, _e in pin2:
                row("DI (pin 2)", f"p.{nm}")
            continue

        for label, group in (("data", data), ("C/Q", cq), ("pin2", pin2)):
            if not group:
                continue
            L.append("    cells = []")
            for nm, e in group:
                if _is_bool(e):
                    L.append(f'    cells.append(cell("{nm}",'
                             f' "ON" if p.{nm} else "off"))')
                elif _scaled(e):
                    d = e.get("decimals")
                    d = d if isinstance(d, int) and d >= 0 else 2
                    unit = f" {e['unit']}" if e.get("unit") else ""
                    L.append(f'    cells.append(cell("{nm}",'
                             f' f"{{p.{nm}:,.{d}f}}{unit}"))')
                else:
                    unit = f" {e['unit']}" if e.get("unit") else ""
                    L.append(f'    cells.append(cell("{nm}", f"{{p.{nm}}}{unit}"))')
            L.append(f'    rows("{label}", cells)')

    L.append("")
    L.append("    return out")
    return "\n".join(L)

# ── 이벤트 ──────────────────────────────────────────────────────────────────
def _event_lines() -> str:
    return '''def event_lines(bus):
    """Display lines for whatever is standing right now.

    Event codes do not overlap, so the number alone tells you the source:

        0x0000-0x0FFF   the backplane itself
        0x1000-         the IO-Link device   (spec Table D.1)
        0x1800-         the IO-Link port     (spec Table D.2)

    `node == 0` marks something the library observed about the link rather than
    something a module reported. Then `channel` is the node it was watching and
    `qualifier` is the frame loss it measured, in percent.

    This is the only way to tell **why** a port's `valid` went false - a device
    that declared its own data invalid looks exactly like a link that dropped.
    """
    events = bus.get_event()
    if not events:
        return ["  events  none"]

    out = []
    for e in events:
        if e.from_library:
            where, extra = "link to node %d" % e.channel, "loss %d%%" % e.qualifier
        else:
            where = "node %d %s" % (
                e.node, "module" if e.channel == 0 else "port %d" % e.channel)
            extra = "t=%d ms" % e.timestamp_ms
        out.append("  event   %-18s 0x%04X  %-8s %-12s %s"
                   % (where, e.code, e.type_name, e.mode_name, extra))
    return out'''


# ── 한 번씩 훑기 ────────────────────────────────────────────────────────────
def _survey() -> str:
    """**설정 API 를 한 번씩 다 밟는다.** 목록으로 아는 것과 불리는 모습을 보는
    것은 다르고, 고객이 알고 싶은 것은 뒤쪽이다."""
    return '''def survey(bus):
    """Walk the read-only side of the API once and print what it says.

    Everything here works in PREOP, before any cyclic traffic starts. This is
    where commissioning code lives - check that the bus is what you expect,
    then go to RUN. It scrolls, unlike the monitor further down: you read it
    once and move on.
    """
    print("=" * 72)
    print("1. what is on the bus")
    print("=" * 72)

    in_bytes, out_bytes = bus.image_size()
    print("  process image   in %d B / out %d B" % (in_bytes, out_bytes))
    print("  nodes           %d" % bus.node_count())

    for node in bus.nodes():
        # `image_in_off` is where this node starts inside the whole-bus image.
        # You rarely need it - the generated classes already know - but it is
        # there when you want to slice one node out by hand.
        print("  node %d  %s  serial %s  PD %d/%d  at image offset %d/%d"
              % (node.address, node.type_name, node.serial,
                 node.pd_in, node.pd_out,
                 node.image_in_off, node.image_out_off))
        for p in node.ports:
            print("      port %d  mode %-14s PD %d/%d  vendor %d device %d"
                  % (p.port, PortMode(p.mode).name, p.pd_in, p.pd_out,
                     p.vendor_id, p.device_id))

    print("")
    print("=" * 72)
    print("2. ask the master and the ports")
    print("=" * 72)

    for node in bus.nodes():
        try:
            ident = bus.iol_master_ident(node.address)
            print("  node %d  master vendor %d  id %d  %d ports  type %d"
                  % (node.address, ident.vendor_id, ident.master_id,
                     ident.port_count, ident.master_type))
        except EdgeError as e:
            print("  node %d  master identification: %s" % (node.address, e))

        for p in node.ports:
            try:
                st = bus.iol_port_status(node.address, p.port)
            except EdgeError as e:
                print("      port %d  status: %s" % (p.port, e))
                continue
            # `rate` 0 means no link was detected; 1..3 are COM1..COM3.
            print("      port %d  %-14s COM%d  cycle %d us  PD %d/%d  rev 0x%02X"
                  % (p.port, st.status_name, st.rate, st.cycletime_us,
                     st.pd_in, st.pd_out, st.revision_id))

            # Read back what the module actually has, rather than what we
            # think we wrote. They differ when a port failed to start.
            try:
                cfg = bus.iol_readback_port_configuration(node.address, p.port)
                print("              configured as %s, validation %d, IQ %d"
                      % (PortMode(cfg.mode).name, cfg.validation,
                         cfg.iq_behavior))
            except EdgeError as e:
                print("              readback: %s" % e)


def read_parameters_once(bus):
    """Section 3 - one pass of ISDU reads, done in the foreground.

    The background reader does the same thing on a loop. Doing it once here
    shows the plain shape of the call: it blocks until the device has answered,
    and either returns the bytes or raises.
    """
    print("")
    print("=" * 72)
    print("3. device parameters (ISDU)")
    print("=" * 72)

    for node, port in PORTS:
        print("  node %d port %d" % (node, port))
        for index, label in ISDU_ITEMS:
            try:
                data = bus.iol_device_read(node, port, index)
                text = data.decode("ascii", "replace").rstrip("\\0 ").strip()
                print("      %-24s %s" % (label, text or data.hex(" ")))
            except EdgeDeviceError as e:
                print("      %-24s rejected, ErrorType 0x%04X"
                      % (label, e.error_type))
            except EdgeError as e:
                print("      %-24s %s" % (label, e))

    # Writing works the same way. ApplicationSpecificTag (0x0018) is a label
    # for people and changes nothing about how the device behaves, which makes
    # it the safe one to try:
    #
    #     bus.iol_device_write(node, port, 0x0018, b"LINE-A-01")
    #
    # If a transaction hangs, `bus.iol_abort(node, port)` cancels it.'''


# ── main ────────────────────────────────────────────────────────────────────
def _main(outs: list, do_cq: list) -> str:
    L = ['''def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60

    # Opening the bus reads the config, claims the serial port and GPIO, starts
    # the background thread and runs the STARTUP scan. It verifies that what is
    # on the bus matches the config and fails if it does not - that check is
    # the reason for having a config file at all.
    #
    # **Always close it.** `with` guarantees that even when you raise.
    with EdgeBus(CONFIG) as bus:
        survey(bus)
        read_parameters_once(bus)

        print("")
        print("=" * 72)
        print("4. go to RUN")
        print("=" * 72)

        # setmode(RUN) sends SET_CYCLE_TIME from the config first. Skip that and
        # the module refuses - it will not hold outputs without a watchdog. The
        # watchdog is three times the cycle; miss that window and every output
        # on the module drops to its safe value.
        print("  state before  %s" % bus.getmode().name)
        bus.setmode(State.RUN)
        print("  state now     %s" % bus.getmode().name)''']

    if outs:
        L.append('''
        # ---- 6. outputs ---------------------------------------------------
        # Writing a value and saying "use it" are two different things.
        # `enable` is the OE bit. With it clear the device holds its own
        # fail-safe value no matter what we send, which is what you want until
        # you are sure of what is wired up.
        #
        # The whole image goes out at once, so `out` is your output state - not
        # a patch applied on top of something else.
        out = Out()''')
        for g in outs:
            tag = f"n{g['node']}_p{g['port']}"
            names = [nm for nm, _e in _fields(g)]
            if _has_qual(g):
                L.append(f"        out.{tag}.enable = False"
                         f"        # <- set True when you mean it")
            if names:
                L.append(f"        # {tag}: {', '.join(names[:4])}")
        L.append("        out.write(bus)")
        if do_cq:
            L.append("")
            L.append("        # ---- C/Q as a digital output ------------------"
                     "------------")
            L.append("        # Toggled about once a second below, so you can"
                     " watch the line")
            L.append("        # move. With C/Q wired back to the DI pin the"
                     " same image shows")
            L.append("        # it return - drive, readback and DI should"
                     " agree.")
            L.append("        #")
            L.append("        # There is no OE here. OE is the frame that tells"
                     " an IO-Link Device")
            L.append("        # its outputs are valid (spec 11.7.3.2); a SIO"
                     " port has no session")
            L.append("        # and no Device, so the level goes out on its"
                     " own.")
            L.append("")
            L.append("        def set_cq(level):")
            L.append('            """Drive every C/Q output at once."""')
            # **필드 이름은 생성기가 정한다.** "C/Q" 를 식별자로 만들면
            # `c_q` 다 — 여기서 `cq` 라고 적으면 dataclass 가 없는 속성을
            # 조용히 만들어, 오류도 없이 아무 일도 일어나지 않는다.
            for nd, pt, fld in do_cq:
                L.append(f"            out.n{nd}_p{pt}.{fld} = level")
            L.append("            out.write(bus)")

    L.append(f"        HAS_CQ_OUT = {bool(do_cq)}")
    L.append('''
        print("")
        print("  starting the monitor - Ctrl-C to stop")
        time.sleep(1.2)

        # ---- 5 + 7 + 3, live ----------------------------------------------
        # No IO-Link port means nothing to ask over ISDU - a SIO port has no
        # Device behind it, so the reader would only collect refusals.
        isdu = IsduReader(bus, PORTS, ISDU_ITEMS) if PORTS else None
        if isdu is not None:
            isdu.start()

        started = time.monotonic()
        try:
            with Screen() as screen:
                while time.monotonic() - started < seconds:
                    elapsed = time.monotonic() - started

                    # Reading the image does not touch the bus. The background
                    # thread already fetched it, so this is a copy - call it as
                    # often as you like, it costs nothing on the wire.
                    # **약 1 Hz.** 초의 정수부가 홀수면 ON — 타이머를 따로
                    # 두지 않아도 되고, 화면 갱신 주기와 무관하게 일정하다.
                    level = (int(elapsed) % 2) == 1
                    if HAS_CQ_OUT:
                        set_cq(level)

                    frame = ["%s   state %s   %5.1f s / %d s%s"
                             % (CONFIG, bus.getmode().name, elapsed, seconds,
                                ("   C/Q " + ("ON" if level else "off"))
                                if HAS_CQ_OUT else ""),
                             "-" * 72]
                    frame += pd_lines(In.read(bus), level)
                    frame.append("")
                    frame += event_lines(bus)
                    frame.append("")
                    if isdu is not None:
                        frame += isdu.lines()

                    screen.draw(frame)
                    time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            if isdu is not None:
                isdu.stop()
            if HAS_CQ_OUT:
                set_cq(False)          # 켠 채로 두고 나가지 않는다

            # Drop out of RUN on the way down. If the process just dies the
            # watchdog does it for you, but then an error is latched and has to
            # be cleared before the bus will run again:
            #
            #     bus.clear_event()      # clears only what has been resolved
            bus.setmode(State.PREOP)
            print("state %s" % bus.getmode().name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())''')
    return "\n".join(L)


# ── C 예제 ──────────────────────────────────────────────────────────────────
#
# 파이썬 예제와 **같은 것을 같은 순서로** 한다. 두 언어의 화면이 같아야 고객이
# "파이썬으로 되던 것이 C 로도 되는가" 를 눈으로 확인할 수 있다.

def emit_c(cfg: dict, name: str) -> str:
    """`<name>_example.c` — 파이썬 예제와 같은 일을 C 로."""
    groups = _groups(cfg)
    ins = [g for g in groups if g["dir"] == "in"]
    outs = [g for g in groups if g["dir"] == "out"]
    # PORTS[] 는 ISDU 에만 쓰인다. 파이썬 쪽 emit() 과 같은 목록을 써야 한다 —
    # _pd_ports() 를 주면 SIO 포트까지 물어보고 0x4003 거절이 줄줄이 찍힌다.
    ports = _isdu_ports(cfg)

    isdu = "\n".join('    { 0x%04X, "%s" },' % (i, n) for i, n in ISDU_STD)
    plist = "\n".join("    { %d, %d }," % (nd, pt) for nd, pt in ports)

    L = []
    a = L.append
    a(_c_head(name))
    a("")
    a("#define ISDU_COUNT  %d" % len(ISDU_STD))
    a("#define PORT_COUNT  %d" % len(ports))
    # 3열 x 34자 + 들여쓰기. 넉넉히 잡는다 — 모자라면 컴파일러가 잘라내기를
    # 경고하고, 경고를 안고 배포하면 고객이 그것부터 의심한다
    # 셀 3개(각 64) + 들여쓰기. 컴파일러가 최악을 보고 잘라내기를 경고하므로
    # 그 최악보다 크게 잡는다 — 경고를 안고 배포하면 고객이 그것부터 의심한다
    a("#define LINE_MAX    208")
    a("#define FRAME_MAX   64")
    a("")
    a("static const struct { uint16_t index; const char *name; }")
    a("ISDU_ITEMS[ISDU_COUNT] = {")
    a(isdu)
    a("};")
    a("")
    a("static const struct { uint8_t node; uint8_t port; }")
    a("PORTS[PORT_COUNT] = {")
    a(plist)
    a("};")
    a("")
    a(_c_screen())
    a("")
    a(_c_isdu())
    a("")
    a(_c_pd(ins))
    a("")
    a(_c_events())
    a("")
    a(_c_survey())
    a("")
    a(_c_main(name, outs))
    return "\n".join(L) + "\n"


def _c_head(name: str) -> str:
    return f'''/*
 * {name} - a worked example for libedgelib (C)
 *
 * EdgeConfig generated this from the modules it found on the bus, so the names,
 * units and bit positions below are the ones on your machine. Copy it, delete
 * what you do not need, and start your application here.
 *
 * It does the same thing as {name}_example.py, in the same order, so you can
 * hold the two side by side.
 *
 *     1. open the bus and see what is on it   node_count / node_info / image_size
 *     2. ask the master and the ports         iol_master_ident / iol_port_status
 *                                             iol_readback_port_configuration
 *     3. read device parameters               iol_device_read / iol_device_write
 *     4. go to RUN                            setmode / getmode
 *     5. read process data by name            edgex_in_read (image_in underneath)
 *     6. write outputs                        edgex_out_write
 *     7. watch for trouble                    get_event / clear_event
 *
 * Then it holds a live monitor showing 5, 7 and 3 at once, redrawn in place.
 *
 * **Cyclic traffic never stops** while an ISDU is in flight - the background
 * thread inside libedgelib keeps the frames going.
 *
 *     cc -O2 -std=c99 -D_DEFAULT_SOURCE -o {name}_example {name}_example.c \\
 *        -ledgelib -pthread
 *     ./{name}_example 30
 */

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "{name}.h"        /* EdgeConfig generated this - it pulls in edgelib.h */

#define CONFIG "{name}.json"'''


def _c_screen() -> str:
    return '''/* ---- screen ----------------------------------------------------------- */
/* Redraw one frame in place, like a monitor. Printing line after line scrolls
   the values you want to watch off the top. If stdout is not a terminal we
   just print - cursor escapes in a log file make it unreadable. */
static int g_tty = 0;

static void screen_begin(void)
{
    g_tty = isatty(1);
    if (g_tty) { fputs("\\033[2J\\033[?25l", stdout); fflush(stdout); }
}

static void screen_end(void)
{
    if (g_tty) { fputs("\\033[?25h\\n", stdout); fflush(stdout); }
}

static void screen_draw(char lines[][LINE_MAX], int n)
{
    if (!g_tty) {
        for (int i = 0; i < n; i++) { puts(lines[i]); }
        putchar('\\n');
        fflush(stdout);
        return;
    }
    fputs("\\033[H", stdout);
    for (int i = 0; i < n; i++) {
        /* clear to end of line, or the tail of a longer previous frame stays */
        printf("%s\\033[K\\n", lines[i]);
    }
    fputs("\\033[J", stdout);
    fflush(stdout);
}

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}'''


def _c_isdu() -> str:
    return '''/* ---- ISDU ------------------------------------------------------------- */
/* Reads device parameters in the background, **one transaction at a time**.

   That is a rule, not a style choice. The module holds one ISDU job per port;
   starting a second before the first is collected comes back EDGE_ERR_BUSY.
   `edgelib_iol_device_read()` returns only once the transaction has finished,
   so a plain sequential loop is exactly the rule.

   It runs off the main thread so the monitor stays smooth - one ISDU can take
   hundreds of milliseconds and the process data should not wait for it. */
typedef struct {
    edgelib_t      *bus;
    pthread_t       tid;
    pthread_mutex_t lock;
    volatile int    stop;
    char            text[PORT_COUNT][ISDU_COUNT][64];
    int             busy_port;      /* -1 when nothing is on the wire */
    int             busy_item;
    long            done;
} isdu_t;

static void isdu_one(isdu_t *r, int pi, int ii)
{
    pthread_mutex_lock(&r->lock);
    r->busy_port = pi;
    r->busy_item = ii;
    pthread_mutex_unlock(&r->lock);

    uint8_t buf[128];
    uint16_t len = (uint16_t)sizeof buf;
    char out[64];

    const int rc = edgelib_iol_device_read(r->bus, PORTS[pi].node,
                                           PORTS[pi].port,
                                           ISDU_ITEMS[ii].index, 0,
                                           buf, &len, 0.0);
    if (rc == EDGE_ERR_DEVICE) {
        /* The device refused. Not every device carries every parameter, so
           this is an answer - not a failure of the bus. */
        snprintf(out, sizeof out, "rejected, ErrorType 0x%02X%02X",
                 buf[0], buf[1]);
    } else if (rc != EDGE_OK) {
        snprintf(out, sizeof out, "failed - %s", edgelib_error_msg(rc));
    } else {
        while (len > 0 && (buf[len - 1] == 0 || buf[len - 1] == ' ')) { len--; }
        if (len == 0) {
            snprintf(out, sizeof out, "(empty)");
        } else {
            const int n = (len < 60) ? (int)len : 60;
            snprintf(out, sizeof out, "%.*s", n, (const char *)buf);
        }
    }

    /* **The transaction is over here.** Only now is the next one allowed. */
    pthread_mutex_lock(&r->lock);
    snprintf(r->text[pi][ii], sizeof r->text[pi][ii], "%s", out);
    r->busy_port = -1;
    r->done++;
    pthread_mutex_unlock(&r->lock);
}

static void *isdu_run(void *arg)
{
    isdu_t *r = (isdu_t *)arg;
    while (!r->stop) {
        for (int pi = 0; pi < PORT_COUNT && !r->stop; pi++) {
            for (int ii = 0; ii < ISDU_COUNT && !r->stop; ii++) {
                isdu_one(r, pi, ii);
            }
        }
        for (int i = 0; i < 20 && !r->stop; i++) { usleep(100000); }
    }
    return NULL;
}

static void isdu_start(isdu_t *r, edgelib_t *bus)
{
    memset(r, 0, sizeof *r);
    r->bus = bus;
    r->busy_port = -1;
    pthread_mutex_init(&r->lock, NULL);
    pthread_create(&r->tid, NULL, isdu_run, r);
}

static void isdu_stop(isdu_t *r)
{
    r->stop = 1;
    pthread_join(r->tid, NULL);
    pthread_mutex_destroy(&r->lock);
}

static int isdu_lines(isdu_t *r, char out[][LINE_MAX], int at)
{
    pthread_mutex_lock(&r->lock);
    if (r->busy_port >= 0) {
        snprintf(out[at++], LINE_MAX,
                 "  isdu    %ld transactions completed   |  in flight: "
                 "node %d port %d %s",
                 r->done, PORTS[r->busy_port].node, PORTS[r->busy_port].port,
                 ISDU_ITEMS[r->busy_item].name);
    } else {
        snprintf(out[at++], LINE_MAX, "  isdu    %ld transactions completed",
                 r->done);
    }
    for (int pi = 0; pi < PORT_COUNT; pi++) {
        for (int ii = 0; ii < ISDU_COUNT; ii++) {
            if (r->text[pi][ii][0] == 0) { continue; }
            snprintf(out[at++], LINE_MAX, "      node %d port %d  %-24s %s",
                     PORTS[pi].node, PORTS[pi].port, ISDU_ITEMS[ii].name,
                     r->text[pi][ii]);
        }
    }
    pthread_mutex_unlock(&r->lock);
    return at;
}'''


def _c_pd(ins: list) -> str:
    """신호를 하나도 빼지 않고 값과 함께. 파이썬 쪽과 같은 3열 배치다."""
    L = ["/* ---- process data ------------------------------------------------- */",
         "/* Every signal with its current value. Fills `out` and returns the new",
         "   line count - it does not print, so the caller draws one whole frame. */",
         "static int pd_lines(const edgex_in_t *in, char out[][LINE_MAX], int at)",
         "{",
         "    char cell[3][64];",
         "    int c = 0;",
         ""]
    for g in ins:
        tag = f"n{g['node']}_p{g['port']}"
        where = f"node {g['node']} port {g['port']}"
        L.append(f"    /* ---- {where} ---- */")
        if _has_qual(g):
            L.append(f'    snprintf(out[at++], LINE_MAX, "  {where}   %s",')
            L.append(f'             in->{tag}.valid ? "PQ ok"'
                     f' : "PQ=0 - DO NOT USE THIS CYCLE");')
        else:
            # SIO 포트에는 `valid` 가 없다 — codegen 이 만들지 않는다. 여기서
            # 그대로 찍으면 생성된 예제가 컴파일되지 않는다 (Table E.10 각주 a).
            L.append(f'    snprintf(out[at++], LINE_MAX, "  {where}");')
        L.append("    c = 0;")
        for nm, e in _fields(g):
            if _is_bool(e):
                val = f'in->{tag}.{nm} ? "ON" : "off"'
                L.append(f'    snprintf(cell[c++], 64, "%-28.28s %-12s",'
                         f' "{nm}", {val});')
            elif _scaled(e):
                d = e.get("decimals")
                d = d if isinstance(d, int) and d >= 0 else 2
                unit = f" {e['unit']}" if e.get("unit") else ""
                L.append(f'    {{ char v[24]; snprintf(v, 24, "%.{d}f{unit}",'
                         f' in->{tag}.{nm});')
                L.append(f'      snprintf(cell[c++], 64, "%-28.28s %-12s",'
                         f' "{nm}", v); }}')
            else:
                unit = f" {e['unit']}" if e.get("unit") else ""
                L.append(f'    {{ char v[24]; snprintf(v, 24, "%lld{unit}",'
                         f' (long long)in->{tag}.{nm});')
                L.append(f'      snprintf(cell[c++], 64, "%-28.28s %-12s",'
                         f' "{nm}", v); }}')
            L.append("    if (c == 3) { snprintf(out[at++], LINE_MAX,"
                     ' "      %s%s%s", cell[0], cell[1], cell[2]); c = 0; }')
        L.append("    if (c > 0) {")
        L.append('        snprintf(out[at++], LINE_MAX, "      %s%s",')
        L.append('                 cell[0], (c > 1) ? cell[1] : "");')
        L.append("        c = 0;")
        L.append("    }")
        L.append("")
    L.append("    return at;")
    L.append("}")
    return "\n".join(L)


def _c_events() -> str:
    return '''/* ---- events ----------------------------------------------------------- */
/* Event codes do not overlap, so the number alone tells you the source:

       0x0000-0x0FFF   the backplane itself
       0x1000-         the IO-Link device   (spec Table D.1)
       0x1800-         the IO-Link port     (spec Table D.2)

   `node == 0` marks something the library observed about the link rather than
   something a module reported; then `channel` is the node it watched and
   `qualifier` is the frame loss in percent.

   This is the only way to tell **why** a port's `valid` went false. */
static int event_lines(edgelib_t *bus, char out[][LINE_MAX], int at)
{
    edge_event_t ev[32];
    int n = 0;
    edgelib_get_event(bus, ev, 32, &n);

    if (n == 0) {
        snprintf(out[at++], LINE_MAX, "  events  none");
        return at;
    }
    for (int i = 0; i < n; i++) {
        const char *src = (ev[i].code >= 0x1800) ? "port"
                        : (ev[i].code >= 0x1000) ? "device" : "backplane";
        if (ev[i].node == 0u) {
            snprintf(out[at++], LINE_MAX,
                     "  event   %-9s link to node %-3u 0x%04X  loss %u %%",
                     src, ev[i].channel, ev[i].code, ev[i].qualifier);
        } else {
            snprintf(out[at++], LINE_MAX,
                     "  event   %-9s node %u %-7s 0x%04X  type %u  t=%u ms",
                     src, ev[i].node,
                     ev[i].channel == 0u ? "module" : "port",
                     ev[i].code, ev[i].type, ev[i].timestamp_ms);
        }
    }
    return at;
}'''


def _c_survey() -> str:
    return '''/* ---- one pass over the configuration API ------------------------------ */
/* Everything here works in PREOP, before any cyclic traffic starts. This is
   where commissioning code lives: check the bus is what you expect, then RUN.
   It scrolls, unlike the monitor - you read it once and move on. */
static void survey(edgelib_t *bus)
{
    printf("========================================================\\n");
    printf("1. what is on the bus\\n");
    printf("========================================================\\n");

    uint16_t in_b = 0, out_b = 0;
    edgelib_image_size(bus, &in_b, &out_b);
    printf("  process image   in %u B / out %u B\\n", in_b, out_b);
    printf("  nodes           %d\\n", edgelib_node_count(bus));

    for (int a = 1; a <= 126; a++) {
        edge_node_t n;
        if (edgelib_node_info(bus, (uint8_t)a, &n) != EDGE_OK) { continue; }
        printf("  node %u  %s  serial %s  PD %u/%u  at image offset %u/%u\\n",
               n.address, n.type_name, n.serial, n.pd_in, n.pd_out,
               n.image_in_off, n.image_out_off);
        for (int p = 0; p < n.port_count; p++) {
            printf("      port %u  mode %u  PD %u/%u  vendor %u device %u\\n",
                   n.ports[p].port, n.ports[p].mode,
                   n.ports[p].pd_in, n.ports[p].pd_out,
                   n.ports[p].vendor_id, n.ports[p].device_id);
        }

        printf("\\n  ---- master and ports ----\\n");
        edge_master_ident_t id;
        if (edgelib_iol_master_ident(bus, n.address, &id) == EDGE_OK) {
            printf("  node %u  master vendor %u  id %u  %u ports  type %u\\n",
                   n.address, id.vendor_id, id.master_id, id.port_count,
                   id.master_type);
        }
        for (int p = 0; p < n.port_count; p++) {
            edge_port_status_t st;
            if (edgelib_iol_port_status(bus, n.address, n.ports[p].port, &st)
                != EDGE_OK) { continue; }
            /* `rate` 0 means no link was detected; 1..3 are COM1..COM3 */
            printf("      port %u  status %u  COM%u  cycle %u us  PD %u/%u\\n",
                   n.ports[p].port, st.status, st.rate, st.cycletime_us,
                   st.pd_in, st.pd_out);

            /* Read back what the module actually has, not what we think we
               wrote. They differ when a port failed to start. */
            edge_port_cfg_t cfg;
            if (edgelib_iol_readback_port_configuration(
                    bus, n.address, n.ports[p].port, &cfg) == EDGE_OK) {
                printf("              configured mode %u, validation %u\\n",
                       cfg.mode, cfg.validation);
            }
        }
    }
}

static void read_parameters_once(edgelib_t *bus)
{
    printf("\\n========================================================\\n");
    printf("3. device parameters (ISDU)\\n");
    printf("========================================================\\n");

    for (int pi = 0; pi < PORT_COUNT; pi++) {
        printf("  node %u port %u\\n", PORTS[pi].node, PORTS[pi].port);
        for (int ii = 0; ii < ISDU_COUNT; ii++) {
            uint8_t buf[128];
            uint16_t len = (uint16_t)sizeof buf;
            const int rc = edgelib_iol_device_read(
                bus, PORTS[pi].node, PORTS[pi].port,
                ISDU_ITEMS[ii].index, 0, buf, &len, 0.0);
            if (rc == EDGE_ERR_DEVICE) {
                printf("      %-24s rejected, ErrorType 0x%02X%02X\\n",
                       ISDU_ITEMS[ii].name, buf[0], buf[1]);
            } else if (rc != EDGE_OK) {
                printf("      %-24s %s\\n", ISDU_ITEMS[ii].name,
                       edgelib_error_msg(rc));
            } else {
                while (len > 0 && (buf[len-1] == 0 || buf[len-1] == ' ')) { len--; }
                printf("      %-24s %.*s\\n", ISDU_ITEMS[ii].name,
                       (int)len, buf);
            }
        }
    }

    /* Writing works the same way. ApplicationSpecificTag (0x0018) is a label
       for people and changes nothing about how the device behaves:

           edgelib_iol_device_write(bus, node, port, 0x0018, 0,
                                    (const uint8_t *)"LINE-A-01", 9, 0.0);

       If a transaction hangs, edgelib_iol_abort(bus, node, port) cancels it. */
}'''


def _c_main(name: str, outs: list) -> str:
    L = ["/* ---- main ------------------------------------------------------------- */",
         "int main(int argc, char **argv)",
         "{",
         "    const int seconds = (argc > 1) ? atoi(argv[1]) : 60;",
         "",
         "    /* Opening the bus reads the config, claims the serial port and GPIO,",
         "       starts the background thread and runs the STARTUP scan. It checks",
         "       that the bus matches the config and fails if it does not - that",
         "       check is the reason for having a config file at all. */",
         "    edgelib_t *bus = edgelib_open(CONFIG);",
         "    if (bus == NULL) {",
         '        fprintf(stderr, "open failed: %s\\n", edgelib_last_error(NULL));',
         "        return 1;",
         "    }",
         "",
         "    survey(bus);",
         "    read_parameters_once(bus);",
         "",
         '    printf("\\n========================================================\\n");',
         '    printf("4. go to RUN\\n");',
         '    printf("========================================================\\n");',
         "",
         "    /* setmode(RUN) sends SET_CYCLE_TIME from the config first. Skip that",
         "       and the module refuses - it will not hold outputs without a",
         "       watchdog. The watchdog is three times the cycle. */",
         "    if (edgelib_setmode(bus, EDGE_STATE_RUN) != EDGE_OK) {",
         '        fprintf(stderr, "RUN failed: %s\\n", edgelib_last_error(bus));',
         "        edgelib_close(bus);",
         "        return 1;",
         "    }",
         '    printf("  state RUN\\n");']

    if outs:
        L += ["",
              "    /* ---- 6. outputs ---------------------------------------------",
              "       Writing a value and saying \"use it\" are two different things.",
              "       `enable` is the OE bit; with it clear the device holds its own",
              "       fail-safe value no matter what we send. The whole image goes",
              "       out at once, so `out` is your output state. */",
              "    edgex_out_t out;",
              "    memset(&out, 0, sizeof out);"]
        for g in outs:
            tag = f"n{g['node']}_p{g['port']}"
            # SIO 출력에는 `enable` 이 없다 — OE 는 IO-Link 프레임이라 세션이
            # 없는 포트에는 보낼 상대가 없다 (§11.7.3.2). codegen 과 맞춘다.
            if _has_qual(g):
                L.append(f"    out.{tag}.enable = false;"
                         f"      /* <- set true when you mean it */")
        L.append("    edgex_out_write(bus, &out);")

    L += ["",
          '    printf("\\n  starting the monitor - Ctrl-C to stop\\n");',
          "    sleep(1);",
          "",
          "    /* ---- 5 + 7 + 3, live ------------------------------------------ */",
          "    isdu_t isdu;",
          "    isdu_start(&isdu, bus);",
          "",
          "    char frame[FRAME_MAX][LINE_MAX];",
          "    const double t0 = now_s();",
          "    screen_begin();",
          "    while (now_s() - t0 < (double)seconds) {",
          "        EdgeState_e st;",
          "        edgelib_getmode(bus, &st);",
          "",
          "        int at = 0;",
          '        snprintf(frame[at++], LINE_MAX, "%s   state %d   %5.1f s / %d s",',
          "                 CONFIG, (int)st, now_s() - t0, seconds);",
          '        snprintf(frame[at++], LINE_MAX,'
          ' "------------------------------------------------------------");',
          "",
          "        /* Reading the image does not touch the bus - the background",
          "           thread already fetched it. This is a copy. */",
          "        edgex_in_t in;",
          "        if (edgex_in_read(bus, &in) == EDGE_OK) {",
          "            at = pd_lines(&in, frame, at);",
          "        }",
          '        snprintf(frame[at++], LINE_MAX, "%s", "");',
          "        at = event_lines(bus, frame, at);",
          '        snprintf(frame[at++], LINE_MAX, "%s", "");',
          "        at = isdu_lines(&isdu, frame, at);",
          "",
          "        if (at > FRAME_MAX) { at = FRAME_MAX; }",
          "        screen_draw(frame, at);",
          "        usleep(200000);",
          "    }",
          "    screen_end();",
          "",
          "    isdu_stop(&isdu);",
          "",
          "    /* Drop out of RUN on the way down. If the process just dies the",
          "       watchdog does it, but then an error is latched and has to be",
          "       cleared (edgelib_clear_event) before the bus will run again. */",
          "    edgelib_setmode(bus, EDGE_STATE_PREOP);",
          "    edgelib_close(bus);",
          "    return 0;",
          "}"]
    return "\n".join(L)


def emit_makefile(name: str) -> str:
    """C 예제를 한 줄로 짓게 한다. 주석으로 적어 두는 것보다 낫다 —
    고객이 컴파일 옵션을 옮겨 적다 틀리는 일이 없어진다."""
    return f"""# {name} - build the C example
#
#     make            build
#     ./{name}_example 30
#
# libedgelib must be installed (sudo bash install.sh in the edgelib tree).

CC     ?= cc
CFLAGS += -O2 -std=c99 -Wall -Wextra -D_DEFAULT_SOURCE
LDLIBS += -ledgelib -pthread

{name}_example: {name}_example.c {name}.h
	$(CC) $(CFLAGS) -I. -o $@ $< $(LDLIBS)

clean:
	rm -f {name}_example

.PHONY: clean
"""
