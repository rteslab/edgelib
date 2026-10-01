"""탐색과 주소 배정 — 코어 §3.5

**탐색은 `STARTUP` 에서만 일어난다.** 그 상태에는 워치독도 주기 교환도 없어 실시간
제약이 없고, 그래서 이 도구가 파이썬만으로 설 수 있다.

주소 배정은 UID 접두 일치로 한다 (§3.5.2). 주소가 없는 노드는 자기 주소를 말할 수
없으므로 **브로드캐스트로 묻고 UID 로 지목**하는 것 말고는 방법이 없다.
"""

from __future__ import annotations

import time

from .catalog import Node, PortInfo
from .link import Link

# 코어 커맨드 (§6.1)
CMD_SET_STATE = 0x03
CMD_DISCOVER = 0x30
CMD_SET_ADDRESS = 0x31
CMD_GET_UID = 0x32

# 클래스 대역 — IO-Link (카테고리 0x40)
CMD_IOL_PORT_STATUS = 0x43

STATE_STARTUP = 0
STATE_PREOP = 1

ST_OK = 0
STATUS_RESULT_MASK = 0x0F
STATUS_STATE_MASK = 0x30
STATUS_STATE_SHIFT = 4

UID_LEN = 12

PSI_NAME = {
    0: "NO_DEVICE", 1: "DEACTIVATED", 2: "PORT_DIAG", 3: "PREOPERATE",
    4: "OPERATE", 5: "DI_CQ", 6: "DO_CQ", 254: "PORT_POWER_OFF",
    255: "NOT_AVAILABLE",
}


def _ask(link: Link, addr: int, cmd: int, arg: bytes = b"",
         timeout: float = 0.3) -> bytes | None:
    """[CMD][arg…] → [CMD][STATUS][…]. CMD 가 되돌아오지 않으면 버린다."""
    rsp = link.xact(addr, bytes([cmd]) + arg, timeout)
    if rsp is None or len(rsp) < 2 or rsp[0] != cmd:
        return None
    return rsp


def set_state(link: Link, state: int) -> None:
    """브로드캐스트라 응답이 없다. 낙오한 노드를 합류시키려면 다시 보내면 된다."""
    link.send_broadcast(bytes([CMD_SET_STATE, state]))


def _parse_uid(uid: bytes) -> tuple[int, int, int, int, bytes]:
    return uid[0], uid[1], uid[2], uid[3], uid[4:12]


def set_address(link: Link, uid: bytes, addr: int) -> bool:
    """노드 하나의 주소를 바꾼다.

    **`STARTUP` 에서만 받는다** (§6.1) — 운전 중인 노드의 주소가 바뀌는 일을
    막아 둔 것이다. 그래서 여기서 상태를 내렸다가 다시 올린다.

    지목은 UID 로 한다. 주소를 바꾸는 중이라 주소로 부를 수 없기 때문이다.
    """
    if len(uid) != UID_LEN or not (1 <= addr <= 126):
        return False
    set_state(link, STATE_STARTUP)
    time.sleep(0.05)
    ack = _ask(link, 0x7F, CMD_SET_ADDRESS, uid + bytes([addr]), timeout=0.3)
    ok = ack is not None and (ack[1] & STATUS_RESULT_MASK) == ST_OK
    set_state(link, STATE_PREOP)
    time.sleep(0.05)
    return ok


def _read_ports(link: Link, node: Node, nport: int = 4) -> None:
    """IO-Link 포트 상태를 읽어 채운다. 크기는 **지금 꽂힌 디바이스**가 보고한 값이다."""
    void_ab = bytes([0xFF, 0xF0])
    for p in range(1, nport + 1):
        rsp = _ask(link, node.address, CMD_IOL_PORT_STATUS, bytes([p]) + void_ab)
        info = PortInfo(port=p)
        # PortStatusList (Table E.4) — ArgBlock 시작이 rsp[2] 이므로 octet k 는 rsp[2+k]
        if rsp is not None and (rsp[1] & STATUS_RESULT_MASK) == ST_OK and len(rsp) >= 17:
            ab = rsp[2:]
            info.status = PSI_NAME.get(ab[2], f"?{ab[2]}")
            info.pd_in = ab[7]
            info.pd_out = ab[8]
            info.vendor_id = (ab[9] << 8) | ab[10]
            # DeviceID 는 octet 11 에서 시작하는 U32 다 — 유효한 것은 하위 3옥텟
            info.device_id = (ab[12] << 16) | (ab[13] << 8) | ab[14]
            info.mode = "IOL_AUTOSTART" if ab[2] in (3, 4) else "DEACTIVATED"
        else:
            info.status = "?"
        node.ports.append(info)


def scan(link: Link, assign_from: int | None = None,
         read_ports: bool = True, log=None) -> list[Node]:
    """버스를 훑어 노드를 찾는다.

    @param assign_from  주소를 자동 배정할 시작 번호. `None` 이면 배정하지 않고
                        이미 주소가 있는 노드만 찾는다.
    @return 찾은 노드들

    **탐색 전에 STARTUP 으로 내린다.** `DISCOVER`·`SET_ADDRESS` 는 그 상태에서만
    받는다 (§6.1) — 운전 중인 노드의 주소를 바꾸는 일이 없도록 막아 둔 것이다.
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    say("go to STARTUP")
    set_state(link, STATE_STARTUP)
    time.sleep(0.05)

    nodes: list[Node] = []

    # 1) 이미 주소가 있는 노드를 훑는다
    say("looking for addressed nodes (1..126)")
    for addr in range(1, 127):
        rsp = _ask(link, addr, CMD_GET_UID, timeout=0.02)
        if rsp is None or len(rsp) < 2 + UID_LEN:
            continue
        cat, model, var, hw, ser = _parse_uid(rsp[2:2 + UID_LEN])
        node = Node(address=addr, category=cat, model=model,
                    variant=var, hw_rev=hw, serial=ser)
        nodes.append(node)
        say(f"  node {addr}  {node.type_name}")

    # 2) 미할당 노드에 주소를 준다
    if assign_from is not None:
        used = {n.address for n in nodes}
        nxt = assign_from
        say("looking for unassigned nodes")

        while True:
            # prefix_len = 0 → 접두 조건 없음. 미할당 노드만 답한다 (§3.5.2)
            rsp = _ask(link, 0x7F, CMD_DISCOVER, bytes([0]), timeout=0.1)
            if rsp is None or len(rsp) != 2 + UID_LEN:
                break

            uid = rsp[2:2 + UID_LEN]
            while nxt in used:
                nxt += 1
            if nxt > 126:
                say("  no free address left (1..126)")
                break

            ack = _ask(link, 0x7F, CMD_SET_ADDRESS, uid + bytes([nxt]), timeout=0.2)
            if ack is None or (ack[1] & STATUS_RESULT_MASK) != ST_OK:
                say(f"  failed to assign address {nxt}")
                break

            cat, model, var, hw, ser = _parse_uid(uid)
            node = Node(address=nxt, category=cat, model=model,
                        variant=var, hw_rev=hw, serial=ser)
            nodes.append(node)
            used.add(nxt)
            say(f"  node {nxt} assigned  {node.type_name}")
            nxt += 1

    # 3) IO-Link 모듈은 포트까지 본다 — PREOP 이라야 포트가 돈다
    if read_ports and any(n.is_iolink for n in nodes):
        say("reading port status")
        set_state(link, STATE_PREOP)
        time.sleep(0.05)
        for n in nodes:
            if n.is_iolink:
                _read_ports(link, n)
                for p in n.ports:
                    if p.status not in ("DEACTIVATED", "?"):
                        say(f"  node {n.address} port {p.port}  {p.status}"
                            f"  PD {p.pd_in}/{p.pd_out}")
        set_state(link, STATE_STARTUP)

    nodes.sort(key=lambda n: n.address)
    say(f"done - {len(nodes)} node(s)")
    return nodes
