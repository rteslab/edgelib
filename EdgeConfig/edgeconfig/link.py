"""RS-485 링크 — 시리얼 + DIR GPIO + 트랜잭션

반이중이라 **송신이 끝난 것을 확인한 뒤에야 DIR 을 내릴 수 있다.** `tcdrain()` 이
커널 버퍼를 비우지만 그것만으로는 마지막 비트가 선로에 실리기 전일 수 있어, 보레이트에서
계산한 만큼 더 기다린다.

커널 RS485 모드를 반드시 켠다. 꺼져 있으면 `tcdrain()` 이 마지막 비트까지 기다리지 않아
프레임 꼬리가 잘린다 — 실측에서 64 B 왕복이 켜짐 100/100 대 꺼짐 0/100 이었다.
"""

from __future__ import annotations

import fcntl
import struct
import time

import gpiod
import serial
from gpiod.line import Direction, Value

from . import proto

# 커널 RS485 (asm-generic/ioctls.h · linux/serial.h)
TIOCGRS485 = 0x542E
TIOCSRS485 = 0x542F
SER_RS485_ENABLED = 0x01
SER_RS485_RTS_ON_SEND = 0x02

DEFAULT_PORT = "/dev/ttyAMA3"
DEFAULT_BAUD = 3_000_000
DEFAULT_CHIP = "/dev/gpiochip0"
DEFAULT_DIR_GPIO = 22


class LinkError(Exception):
    pass


class Link:
    """버스 한 가닥. `with` 로 쓰면 닫힘이 보장된다."""

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD,
                 chip: str = DEFAULT_CHIP, dir_gpio: int = DEFAULT_DIR_GPIO):
        self.port = port
        self.baud = baud

        # 마지막 비트가 선로를 떠날 때까지 — 정지비트 여유를 넉넉히 본다
        self.guard_s = 12.0 / baud
        self.idle_s = 0.0005          # 이만큼 조용하면 프레임이 끝난 것으로 본다

        self._ser: serial.Serial | None = None
        self._req: gpiod.LineRequest | None = None

        try:
            self._ser = serial.Serial(port, baud, timeout=0, write_timeout=1.0)
        except serial.SerialException as e:
            raise LinkError(f"cannot open {port}: {e}") from e

        self._enable_rs485()

        try:
            self._req = gpiod.request_lines(
                chip,
                consumer="edgeconfig",
                config={dir_gpio: gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.INACTIVE)},
            )
        except OSError as e:
            self._ser.close()
            raise LinkError(
                f"cannot claim GPIO{dir_gpio}: {e}\n"
                f"  check whether another process is using the bus") from e

        self._dir = dir_gpio

    # ── 자원 ────────────────────────────────────────────────────────────────
    def close(self) -> None:
        if self._req is not None:
            self._req.set_value(self._dir, Value.INACTIVE)
            self._req.release()
            self._req = None
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def __enter__(self) -> "Link":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _enable_rs485(self) -> None:
        fd = self._ser.fileno()
        try:
            buf = bytearray(32)       # struct serial_rs485
            fcntl.ioctl(fd, TIOCGRS485, buf)
            flags = struct.unpack_from("I", buf, 0)[0]
            if not (flags & SER_RS485_ENABLED):
                struct.pack_into("I", buf, 0,
                                 flags | SER_RS485_ENABLED | SER_RS485_RTS_ON_SEND)
                fcntl.ioctl(fd, TIOCSRS485, buf)
        except OSError as e:
            raise LinkError(
                f"cannot enable kernel RS485 mode: {e}\n"
                f"  without it the tail of each frame is cut") from e

    def _set_dir(self, tx: bool) -> None:
        self._req.set_value(self._dir, Value.ACTIVE if tx else Value.INACTIVE)

    def _send(self, data: bytes) -> None:
        self._set_dir(True)
        try:
            self._ser.write(data)
            self._ser.flush()          # tcdrain
            time.sleep(self.guard_s)   # 마지막 비트가 선로를 떠날 때까지
        finally:
            self._set_dir(False)       # 예외가 나도 선을 놓는다

    def _recv(self, first_timeout: float) -> bytes:
        deadline = time.monotonic() + first_timeout
        buf = bytearray()
        while True:
            n = self._ser.in_waiting
            if n:
                buf += self._ser.read(n)
                deadline = time.monotonic() + self.idle_s
                continue
            if time.monotonic() >= deadline:
                return bytes(buf)
            time.sleep(50e-6)

    # ── 트랜잭션 ────────────────────────────────────────────────────────────
    def xact(self, addr: int, pdu: bytes, timeout: float = 0.3) -> bytes | None:
        """요청 하나 → 응답 하나. 응답이 없거나 깨졌으면 None."""
        self._ser.reset_input_buffer()
        self._send(proto.build(addr, pdu))

        raw = self._recv(timeout)
        if not raw:
            return None
        try:
            _used, _addr, rsp = proto.parse(raw)
        except proto.FrameError:
            return None
        return rsp

    def send_broadcast(self, pdu: bytes) -> None:
        """응답이 없는 커맨드. 브로드캐스트는 아무도 답하지 않는다."""
        self._send(proto.build(proto.ADDR_BROADCAST, pdu))
        time.sleep(0.005)
