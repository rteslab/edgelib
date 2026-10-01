"""장치 카탈로그 — UID 앞 4 바이트가 곧 장치 종류다 (코어 §3.5.1)

```
UID = [category][model][variant][hw_rev] [serial 8 B]
       0x40      0x01    0x04     0x00     ← IO-Link 마스터 4포트
       └──────── 이 넷이 장치 종류 ────────┘
```

`variant` 가 채널·포트 수를 담으므로, **DI·DO·AI·AO 는 이 표만으로 프로세스 데이터
크기가 확정된다.** IO-Link 마스터만 다르다 — 포트 수까지는 알지만 포트당 크기는 꽂힌
디바이스가 정한다. 그것이 EdgeConfig 가 IODD 를 읽어야 하는 이유다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CAT_IOLINK = 0x40
CAT_DI = 0x10
CAT_DO = 0x20
CAT_AI = 0x30
CAT_AO = 0x50


@dataclass(frozen=True)
class DeviceType:
    category: int
    model: int
    variant: int
    name: str
    channels: int
    pd_in: int          # 모듈 전체 입력 바이트. IO-Link 는 한정자만 (아래 참조)
    pd_out: int
    per_port: bool = False   # True 면 포트별 크기를 따로 정해야 한다

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.category, self.model, self.variant)


# 지금 실물로 확인된 것만 올린다. 모르는 것을 표에 넣으면 그것이 맞는지 확인할 길이 없다.
_TYPES: list[DeviceType] = [
    DeviceType(CAT_IOLINK, 0x01, 0x04, "IO-Link Master 4-port",
               channels=4, pd_in=1, pd_out=1, per_port=True),
]

_BY_KEY = {t.key: t for t in _TYPES}


def lookup(category: int, model: int, variant: int) -> DeviceType | None:
    """모르는 조합이면 None. **추측하지 않는다.**"""
    return _BY_KEY.get((category, model, variant))


def describe(category: int, model: int, variant: int, hw_rev: int) -> str:
    t = lookup(category, model, variant)
    if t is not None:
        return t.name
    return f"unknown (cat {category:#04x} model {model:#04x} var {variant:#04x})"


@dataclass
class PortInfo:
    """IO-Link 포트 하나. 크기는 IODD 나 실물에서 온다."""
    port: int
    mode: str = "DEACTIVATED"
    pd_in: int = 0
    pd_out: int = 0
    vendor_id: int = 0
    device_id: int = 0
    status: str = ""            # 탐색 시점의 PortStatusInfo 이름
    product: str = ""           # 알아냈으면 제품명


@dataclass
class Node:
    """탐색으로 찾았거나 설정에 적힌 모듈 하나."""
    address: int
    category: int
    model: int
    variant: int
    hw_rev: int
    serial: bytes = b""
    # "auto" 면 탐색 순서대로 받고, "fixed" 면 이 주소를 UID 로 되찾아 준다.
    # 모듈이 주소를 기억하지 못하므로 근거는 설정 파일에만 남는다.
    addr_mode: str = "auto"
    ports: list[PortInfo] = field(default_factory=list)

    @property
    def type_name(self) -> str:
        return describe(self.category, self.model, self.variant, self.hw_rev)

    @property
    def dev_type(self) -> DeviceType | None:
        return lookup(self.category, self.model, self.variant)

    @property
    def is_iolink(self) -> bool:
        return self.category == CAT_IOLINK

    @property
    def serial_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.serial)

    @property
    def uid_hex(self) -> str:
        head = bytes([self.category, self.model, self.variant, self.hw_rev])
        return f"{head.hex(' ').upper()} | {self.serial_hex}"
