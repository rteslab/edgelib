"""백플레인 프레이밍 — 코어 스펙 §3

**CRC 파라미터와 폭 규칙은 슬레이브의 `bp_crc.c` 와 한 글자도 달라선 안 된다.**
어긋나면 두 노드가 서로의 프레임을 영원히 폐기하는데, 증상은 "가끔 통신이 안 된다"로
나타나 추적이 극히 어렵다 (§3.7). 그래서 아래 셋을 그대로 옮기고, `selftest()` 가
§3.7 의 테스트 벡터로 확인한다.

파이썬으로 다시 쓴 이유는 커미셔닝 탐색에 실시간 제약이 없기 때문이다 — 탐색은
`STARTUP` 에서만 일어나고 그 상태에는 워치독도 주기 교환도 없다. 그래서 libedgelib
없이도 이 도구가 혼자 선다.
"""

from __future__ import annotations

# ── 프레임 상수 (§3.1) ────────────────────────────────────────────────────────
SOF = 0xA5
ADDR_MASK = 0x7F
ADDR_BROADCAST = 0x7F
ADDR_UNASSIGNED = 0x00
HDR_SHORT = 3  # SOF + [P|ADDR] + LEN

# CRC 폭 전환 경계 — 보호 길이 L 기준 (§3.4)
CRC_L_MAX_8 = 14
CRC_L_MAX_16 = 4093

# "123456789" 에 대한 check 값 (§3.4)
CRC8_CHECK = 0xDF
CRC16_CHECK = 0x29B1
CRC32C_CHECK = 0xE3069283


def _table8(poly: int) -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if (c & 0x80) else (c << 1) & 0xFF
        t.append(c)
    return t


def _table16(poly: int) -> list[int]:
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
        t.append(c)
    return t


def _table32_reflected(poly: int) -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if (c & 1) else c >> 1
        t.append(c & 0xFFFFFFFF)
    return t


_T8 = _table8(0x2F)                    # CRC-8/AUTOSAR
_T16 = _table16(0x1021)                # CRC-16/CCITT, init 0xFFFF
_T32 = _table32_reflected(0x82F63B78)  # CRC-32C, 반사형


def crc8(data: bytes) -> int:
    c = 0xFF
    for b in data:
        c = _T8[c ^ b]
    return c ^ 0xFF


def crc16(data: bytes) -> int:
    """init 은 0xFFFF 다 — XMODEM(0) 이 아니다."""
    c = 0xFFFF
    for b in data:
        c = ((c << 8) ^ _T16[((c >> 8) ^ b) & 0xFF]) & 0xFFFF
    return c


def crc32c(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = (c >> 8) ^ _T32[(c ^ b) & 0xFF]
    return c ^ 0xFFFFFFFF


def parity8(v: int) -> int:
    v &= 0xFF
    v ^= v >> 4
    v ^= v >> 2
    v ^= v >> 1
    return v & 1


def crc_width(prot_len: int) -> int:
    """보호 길이가 폭을 정한다 (§3.4). 길이를 협상하지 않는 이유가 이것이다."""
    if prot_len <= CRC_L_MAX_8:
        return 1
    if prot_len <= CRC_L_MAX_16:
        return 2
    return 4


def build(addr: int, pdu: bytes) -> bytes:
    """PDU 를 프레임으로 감싼다.

    보호 구간은 `[P|ADDR]` 부터 PDU 끝까지다 — **SOF 도 CRC 자신도 빠진다** (§3.1).
    """
    if not pdu or len(pdu) > 255:
        raise ValueError(f"PDU length {len(pdu)} is outside the short-form range (1..255)")

    out = bytearray(3 + len(pdu))
    out[0] = SOF
    out[1] = (parity8(len(pdu)) << 7) | (addr & ADDR_MASK)
    out[2] = len(pdu)
    out[3:] = pdu

    prot = out[1:]                      # [P|ADDR] + LEN + PDU
    w = crc_width(len(prot))
    if w == 1:
        out += bytes([crc8(prot)])
    elif w == 2:
        out += crc16(prot).to_bytes(2, "little")   # 전선 위는 리틀엔디안
    else:
        out += crc32c(prot).to_bytes(4, "little")
    return bytes(out)


class FrameError(Exception):
    pass


class NeedMore(FrameError):
    pass


def parse(buf: bytes) -> tuple[int, int, bytes]:
    """버퍼 앞에서 프레임 하나를 해석한다.

    @return (소비한 바이트 수, ADDR, PDU)
    """
    if len(buf) < 1:
        raise NeedMore("waiting for SOF")
    if buf[0] != SOF:
        raise FrameError("SOF mismatch")
    if len(buf) < HDR_SHORT:
        raise NeedMore("waiting for header")

    p = (buf[1] >> 7) & 1
    ln = buf[2]

    # **LEN 을 쓰기 전에 판정한다** (§3.3). 깨진 길이로 버퍼를 읽으면 그 다음이 없다.
    if parity8(ln) != p:
        raise FrameError("LEN parity mismatch")
    if ln == 0:
        raise FrameError("extended frame - not supported by this tool")

    prot_len = 2 + ln
    w = crc_width(prot_len)
    total = HDR_SHORT + ln + w
    if len(buf) < total:
        raise NeedMore("waiting for body")

    prot = buf[1:1 + prot_len]
    if w == 1:
        exp = bytes([crc8(prot)])
    elif w == 2:
        exp = crc16(prot).to_bytes(2, "little")
    else:
        exp = crc32c(prot).to_bytes(4, "little")

    if exp != buf[HDR_SHORT + ln:total]:
        raise FrameError("CRC mismatch")

    return total, buf[1] & ADDR_MASK, bytes(buf[HDR_SHORT:HDR_SHORT + ln])


def selftest() -> None:
    """코어 §3.7 테스트 벡터. 틀리면 슬레이브와 절대 통신되지 않는다."""
    chk = b"123456789"
    assert crc8(chk) == CRC8_CHECK, f"crc8 {crc8(chk):#04x}"
    assert crc16(chk) == CRC16_CHECK, f"crc16 {crc16(chk):#06x}"
    assert crc32c(chk) == CRC32C_CHECK, f"crc32c {crc32c(chk):#010x}"

    # 왕복 — 폭이 갈리는 두 지점을 다 밟는다
    for pdu in (b"\x32", b"\x20" + bytes(40)):
        f = build(3, pdu)
        used, addr, out = parse(f)
        assert used == len(f) and addr == 3 and out == pdu
