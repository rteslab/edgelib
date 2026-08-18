"""프로세스 이미지 — PD 의 어느 바이트가 무엇인지

**커미셔닝의 핵심 산출물이다.** 응용 코드가 PD 를 받아 해석하려면 이 맵이 있어야
한다. 화면에 띄우고 JSON 에도 넣는다.

맵은 계산으로 나온다 — 고르는 것이 아니다 (클래스 §2.2):

```
in_data   [MST][포트1 PD_IN][포트2 PD_IN][포트3][포트4]
out_data  [OE ][포트1 PD_OUT][포트2 PD_OUT]…
            ↑ 한정자 1 B      ↑ 포트 1번부터 빈틈 없이 이어 붙인다
```

포트별 길이가 정해지면 오프셋은 따라 나온다. **그래서 길이가 바뀌면 뒤쪽이 전부
밀린다** — 운전 중에 맵을 바꾸지 않는 이유다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .iodd import Iodd, PdItem

DIR_IN = "in"
DIR_OUT = "out"


@dataclass
class Entry:
    """이미지 한 칸. 바이트 하나일 수도, 여러 바이트에 걸친 값일 수도 있다."""
    direction: str          # in · out
    byte: int               # 이미지 안의 시작 바이트
    length: int             # 바이트 수
    node: int
    port: int               # 0 이면 모듈 수준 (한정자)
    name: str
    dtype: str = ""
    bit_offset: int = 0     # 포트 PD 안에서의 LSB 기준 비트 오프셋
    bit_length: int = 0
    subindex: int = 0
    unit: str = ""
    note: str = ""
    # RUN 중 값을 뽑는 데 쓴다. JSON 에는 나가지 않는다 — 파일은 배치를 적는
    # 것이지 그때그때의 값을 적는 것이 아니다.
    item: PdItem | None = None
    total_bits: int = 0
    role: str = ""          # "mst" · "oe" — 한정자는 이름이 아니라 이것으로 찾는다

    @property
    def where(self) -> str:
        if self.port == 0:
            return f"Node {self.node}"
        return f"Node {self.node} Port {self.port}"

    @property
    def span(self) -> str:
        if self.length <= 1 and self.bit_length and self.bit_length < 8:
            return f"{self.byte}.{self.bit_offset % 8}"
        if self.length == 1:
            return str(self.byte)
        return f"{self.byte}..{self.byte + self.length - 1}"


@dataclass
class PortSpec:
    """이미지를 만드는 데 필요한 포트 정보."""
    port: int
    mode: str = "DEACTIVATED"
    pd_in: int = 0
    pd_out: int = 0
    iodd: Iodd | None = None
    product: str = ""
    vendor_id: int = 0
    device_id: int = 0
    # PortConfigList 의 나머지 (Table E.3). 지금 툴은 전부 0 으로 쓰지만,
    # **라이브러리가 같은 설정을 재현하려면 무엇을 썼는지 적혀 있어야 한다.**
    validation: int = 0          # Validation & Backup — 0 none · 1 V1.0 · 2 V1.1
    iq_behavior: int = 0
    cycletime: int = 0           # 0 = FreeRunning (Table B.3 옥텟)


@dataclass
class NodeSpec:
    node: int
    type_name: str = ""
    # UID 앞 4바이트 — 라이브러리가 **맞는 모듈인지 확인할 근거**다 (코어 §3.5.1).
    # 주소는 배정된 것일 뿐이라 그것만으로는 같은 장치인지 알 수 없다.
    category: int = 0
    model: int = 0
    variant: int = 0
    hw_rev: int = 0
    serial: str = ""
    addr_mode: str = "auto"
    ports: list[PortSpec] = field(default_factory=list)

    @property
    def pd_in(self) -> int:
        return 1 + sum(p.pd_in for p in self.ports)      # 한정자 1 B + 포트들

    @property
    def pd_out(self) -> int:
        return 1 + sum(p.pd_out for p in self.ports)


@dataclass
class ProcessImage:
    entries: list[Entry] = field(default_factory=list)
    in_bytes: int = 0
    out_bytes: int = 0

    def by_dir(self, d: str) -> list[Entry]:
        return [e for e in self.entries if e.direction == d]

    def to_json(self, settings: dict | None = None) -> dict:
        """응용 코드가 그대로 쓸 수 있는 형태.

        **환산까지 굳혀서 적는다.** 운전 중에는 IODD 가 없고, 단위 설정을 읽어
        계수를 고르는 일은 커미셔닝의 몫이다 — 여기서 정하지 않으면 응용은 283 을
        받고 그것이 무엇인지 알 방법이 없다.
        """
        def one(e: Entry) -> dict:
            d = {"byte": e.byte, "length": e.length,
                 "node": e.node, "port": e.port, "name": e.name}
            if e.role:
                d["role"] = e.role
                # 한정자는 포트 번호가 곧 비트다 — 이름을 파싱하게 두지 않는다
                d["bit_per_port"] = {"port": "bit = port - 1",
                                     "high_nibble": "ISDU_RDY"}                     if e.role == "mst" else {"port": "bit = port - 1"}
            if e.dtype:
                d["type"] = e.dtype
            if e.bit_length and e.bit_length < 8:
                d["bit"] = e.bit_offset % 8
                d["bits"] = e.bit_length      # 폭. 1비트가 아닌 것이 있다
            if e.subindex:
                d["subindex"] = e.subindex

            it = e.item
            if it is not None:
                sc = it.scale_for((settings or {}).get((e.node, e.port)))
                if sc is not None:
                    d["scale"] = {"gradient": sc.gradient, "offset": sc.offset}
                    if sc.unit:
                        d["unit"] = sc.unit
                    if sc.decimals >= 0:
                        d["decimals"] = sc.decimals
                if it.low is not None and it.high is not None:
                    d["min"], d["max"] = it.low, it.high
            elif e.unit:
                d["unit"] = e.unit
            return d

        return {
            # 여러 바이트 값은 **MSB 우선**이다 (IO-Link 규격). 한 번만 적는다.
            "byte_order": "big",
            "in":  {"bytes": self.in_bytes,
                    "entries": [one(e) for e in self.by_dir(DIR_IN)]},
            "out": {"bytes": self.out_bytes,
                    "entries": [one(e) for e in self.by_dir(DIR_OUT)]},
        }


def build(nodes: list[NodeSpec]) -> ProcessImage:
    """노드 구성에서 이미지를 만든다.

    노드가 여럿이면 노드 순서대로 이어 붙인다 — 각 노드가 자기 한정자 바이트로
    시작하는 덩어리를 갖는다.
    """
    img = ProcessImage()
    in_off = out_off = 0

    for n in sorted(nodes, key=lambda x: x.node):
        # ── 한정자 (클래스 §2) ──────────────────────────────────────────────
        # 이름이 곧 설명이다 — 따로 적어 두면 표에서 그 칸만 읽게 된다
        img.entries.append(Entry(
            DIR_IN, in_off, 1, n.node, 0, "MST  (PQ / ISDU_RDY)", role="mst"))
        in_off += 1

        img.entries.append(Entry(
            DIR_OUT, out_off, 1, n.node, 0, "OE  (output enable)", role="oe"))
        out_off += 1

        for p in sorted(n.ports, key=lambda x: x.port):
            in_off = _port_entries(img, DIR_IN, in_off, n, p)
            out_off = _port_entries(img, DIR_OUT, out_off, n, p)

    img.in_bytes = in_off
    img.out_bytes = out_off
    return img


def _port_entries(img: ProcessImage, direction: str, off: int,
                  n: NodeSpec, p: PortSpec) -> int:
    size = p.pd_in if direction == DIR_IN else p.pd_out
    if size <= 0:
        return off

    # **DI(핀 2)는 in 구간의 마지막 한 바이트다.** 모듈의 `app_iol_pd.c` 가 그
    # 자리에 싣는다 — 디바이스 것을 앞에 두어야 DI 를 더해도 앞쪽 오프셋이 밀리지
    # 않고, 이미 나간 설정 파일의 해석이 그대로 산다.
    #
    # 안 쓰는 포트(DEACTIVATED)에는 없다. 모드와 무관하게 그 외에는 언제나 있다 —
    # IO-Link 포트에도 붙는다.
    di = 1 if (direction == DIR_IN and p.mode != "DEACTIVATED") else 0
    body = size - di

    if p.mode in ("DI_CQ", "DO_CQ"):
        # ── SIO — 핀 4 (C/Q) ────────────────────────────────────────────
        # 모드가 방향을 정한다: `DI_CQ` 면 읽는 비트, `DO_CQ` 면 구동하는 비트.
        # 반대쪽에는 자리가 없다 — `DI_CQ` 포트의 출력을 만들어 두면 응용이
        # 그것을 써도 되는 줄 안다.
        if body > 0:
            # **되읽기는 보낸 값과 다른 것이다.** 단락·과전류로 드라이버가 접히면
            # 1 을 보내도 선은 0 이다 — 이름을 같게 두면 응용이 그 차이를 못 본다.
            # 같은 핀이라도 방향에 따라 뜻이 다르므로 이름을 셋으로 가른다.
            if direction == DIR_OUT:
                name = "C/Q Output"          # 우리가 구동하는 값
            elif p.mode == "DO_CQ":
                name = "C/Q Readback"        # 선에서 되읽은 값
            else:
                name = "C/Q Input"           # DI_CQ - 그냥 읽는 값
            img.entries.append(Entry(
                direction, off, 1, n.node, p.port, name,
                dtype="BooleanT", bit_offset=0, bit_length=1, total_bits=8,
                note="SIO pin 4"))
    elif body > 0:
        layout = None
        if p.iodd is not None:
            layout = p.iodd.pd_in if direction == DIR_IN else p.iodd.pd_out

        if layout is None or not layout.items:
            # IODD 가 없으면 이름을 붙일 수 없다. **모르는 것을 지어내지 않는다**
            # — 자리는 잡되 원시 바이트로 둔다.
            img.entries.append(Entry(
                direction, off, body, n.node, p.port,
                f"PD {'IN' if direction == DIR_IN else 'OUT'} (raw - no IODD)"))
        else:
            total_bits = layout.bit_length
            for it in layout.items:
                first, last = it.byte_span(total_bits)
                img.entries.append(Entry(
                    direction, off + first, max(1, last - first),
                    n.node, p.port, it.name or f"sub{it.subindex}",
                    dtype=it.dtype, bit_offset=it.bit_offset,
                    bit_length=it.bit_length, subindex=it.subindex,
                    unit=it.unit, item=it, total_bits=total_bits))

    if di:
        img.entries.append(Entry(
            DIR_IN, off + body, 1, n.node, p.port, "DI",
            dtype="BooleanT", bit_offset=0, bit_length=1, total_bits=8,
            note="pin 2, input only"))

    return off + size


# ── 사이클 시간 계산 ────────────────────────────────────────────────────────
#
# **고르는 값이 아니라 나오는 값이다.** PD 크기가 정해지면 주기 교환에 드는 시간이
# 따라 나오고, 사람이 정할 것은 "비주기에 얼마를 더 줄까" 하나뿐이다.
#
#   프레임 = SOF(1) + P|ADDR(1) + LEN(1) + PDU + CRC(폭은 보호길이가 정한다)
#   한 왕복 = 요청 프레임 + 턴어라운드 + 응답 프레임
#
# 턴어라운드는 슬레이브가 **반드시** 기다리는 시간이다 (`BP_CFG_T_TURNAROUND_US`).
# 마스터가 유저스페이스에서 DIR 을 내리기 때문에 필요한 값이라, 커널이 DE 를 맡게
# 되면 수십 µs 로 내려가고 그만큼 주기가 짧아진다.
PORT = "/dev/ttyAMA3"
GPIO_CHIP = "/dev/gpiochip0"
DIR_GPIO = 22
BAUD = 3_000_000
BITS_PER_BYTE = 10              # 8N1
TURNAROUND_US = 1000            # BP_CFG_T_TURNAROUND_US — 슬레이브가 반드시 기다린다
PDU_MAX = 255                   # 단축형만 쓴다 (BP_CFG_PDU_MAX_EXT = 0)

# **턴어라운드가 지난다고 곧바로 나가지 않는다.** 슬레이브의 송신 폴링이 유휴
# 태스크(우선순위 1)에 얹혀 있어 상한이 없다 (`bp_pl_rs485.c`). 실측에서
# `CMD_GET_UID` 946만 회 왕복이 최소 1.43 · 평균 2.04 · 최대 5.94 ms 였는데
# (코어 §3.6), 전선 시간은 0.08 ms 뿐이니 나머지가 전부 이 지연이다.
#
# **그래서 계산값을 그대로 쓰면 안 된다.** 계산으로 나오는 것은 아무것도 늦지
# 않았을 때의 하한이고, 실제 주기는 거기에 여유를 얹어 사람이 정한다.
CYCLE_SAFETY = 2                # 참고용 — 하한 대비 안전 배수
# 화면 기본값. 파이썬이 여유 있게 지킬 수 있는 값으로 둔다 — 계산 하한은
# C 가 돌 때의 값이고, 이 툴은 그 설정을 확인하는 자리이지 그 속도로 도는
# 자리가 아니다.
CYCLE_DEFAULT_US = 100_000


def frame_us(pdu: int, baud: int = BAUD) -> float:
    from .proto import crc_width
    nbytes = 3 + pdu + crc_width(2 + pdu)
    return nbytes * BITS_PER_BYTE * 1e6 / baud


def txn_us(req_pdu: int, rsp_pdu: int, baud: int = BAUD) -> float:
    """한 왕복 = 요청 프레임 + 턴어라운드 + 응답 프레임."""
    return frame_us(req_pdu, baud) + TURNAROUND_US + frame_us(rsp_pdu, baud)


def cyclic_us(nodes: list[NodeSpec], baud: int = BAUD) -> int:
    """주기 교환만 돌 때의 한 사이클. 노드마다 한 왕복이다.

    요청 PDU = `[CMD] + OE + 포트 출력`, 응답 PDU = `[CMD][STATUS] + MST + 포트 입력`.
    """
    total = sum(txn_us(1 + n.pd_out, 2 + n.pd_in, baud) for n in nodes)
    return int(round(total)) or 1


# 비주기도 **요청과 응답이 다 있다.** 다만 큰 쪽은 늘 한 방향뿐이다.
#
#   ISDU 쓰기 : 요청 = 인덱스 + 데이터(큼)   응답 = VoidBlock (4 B)
#   ISDU 회수 : 요청 = 포트 하나 (2 B)        응답 = 데이터(큼)
#
# 그래서 최악은 `큰 프레임 + 작은 프레임` 이지 `큰 것 두 개`가 아니다.
ASYNC_SMALL_PDU = 4


def async_worst_us(baud: int = BAUD) -> int:
    """비주기 한 건의 최악. **한 사이클에 하나만 보낸다**는 규칙이 있어서 이
    시간만 얹으면 최악 사이클이 나온다. 프레임 상한이 정해져 있으므로 계산된다."""
    return int(round(txn_us(PDU_MAX, ASYNC_SMALL_PDU, baud)))


def cycle_min_us(nodes: list[NodeSpec], baud: int = BAUD) -> int:
    """한 사이클의 **하한** — 주기 교환 전부 + 비주기 한 건.

    아무것도 늦지 않았을 때의 값이다. 실제로 쓸 주기는 여기에 여유를 얹는다.
    """
    return cyclic_us(nodes, baud) + async_worst_us(baud)


SCHEMA = 1


def to_config(nodes: list[NodeSpec], img: ProcessImage,
              cycle_us: int = 0, cycle_min_us: int = 0,
              settings: dict | None = None) -> dict:
    """라이브러리가 읽고 초기화하는 파일.

    이미지 맵만으로는 부족하다. 라이브러리가 버스를 잡으려면 셋이 더 있어야 한다.

    * **노드 신원** — 주소는 배정된 것이라 그것만으로 같은 장치인지 알 수 없다.
      UID 앞 4바이트가 있어야 "설정할 때 그 모듈이 맞는가"를 확인한다 (코어 §3.5.1).
    * **포트 모드** — 모듈은 전원을 넣으면 모든 포트가 `DEACTIVATED` 로 뜨고
      NV 에 아무것도 남기지 않는다 (`iol_cm_init`). 누군가는 켜 줘야 한다.
    * **사이클 시간** — `SET_CYCLE_TIME` 을 먼저 통보하지 않으면 슬레이브가 RUN 을
      거부한다 (코어 §5.2).

    **값은 넣지 않는다.** 이 파일은 배치와 구성을 적는 것이고, 그때그때의 PD 값은
    라이브러리가 버스에서 읽는다.
    """
    img_json = img.to_json(settings)
    return {
        "edgelib_config": SCHEMA,
        # **버스 하나를 통째로 적는다.** 노드는 그 안의 목록일 뿐이다.
        "bus": {
            # **라이브러리가 어느 선을 잡을지 여기 적는다.** 안 적으면 기본값으로
            # 열리는데, 보드가 둘 이상 생기는 순간 그 기본값이 틀린 답이 된다.
            "port": PORT,
            "chip": GPIO_CHIP,
            "dir_gpio": DIR_GPIO,
            "baud": BAUD,
            "turnaround_us": TURNAROUND_US,
            # 계산 하한 — 주기 교환 전부 + 비주기 한 건. 참고용이다.
            "cycle_min_us": cycle_min_us,
            # 실제로 돌 주기. `SET_CYCLE_TIME` 에 그대로 나간다.
            "cycle_us": cycle_us,
            # 규칙에서 나오는 값이지만 적어 둔다 — 읽는 쪽이 3배를 다시 계산하다
            # 어긋나는 것보다 낫다 (코어 §5.2).
            "watchdog_us": cycle_us * 3,
            "node_count": len(nodes),
            "pd_in_bytes": img_json["in"]["bytes"],
            "pd_out_bytes": img_json["out"]["bytes"],
        },
        "nodes": [
            {
                "address": n.node,
                "address_mode": n.addr_mode,
                "type": n.type_name,
                "uid": {"category": n.category, "model": n.model,
                        "variant": n.variant, "hw_rev": n.hw_rev,
                        "serial": n.serial},
                "pd_in": n.pd_in, "pd_out": n.pd_out,
                "ports": [
                    {
                        "port": p.port,
                        "mode": p.mode,
                        # 모듈에 그대로 쓸 수 있는 값들 (Table E.3)
                        "validation": p.validation,
                        "iq_behavior": p.iq_behavior,
                        "cycletime": p.cycletime,
                        "pd_in": p.pd_in, "pd_out": p.pd_out,
                        "vendor_id": p.vendor_id, "device_id": p.device_id,
                        "product": p.product,
                    }
                    for p in sorted(n.ports, key=lambda x: x.port)
                ],
            }
            for n in sorted(nodes, key=lambda x: x.node)
        ],
        "process_image": img_json,
    }


def decode_port(item_data: bytes, layout, ) -> list[tuple[PdItem, int]]:
    """포트 PD 한 덩이를 항목별 원시값으로 푼다. 물리 단위 환산은 하지 않는다 —
    IODD 가 배율을 늘 명시하지는 않기 때문이다."""
    return [(it, it.extract(item_data, layout.bit_length)) for it in layout.items]
