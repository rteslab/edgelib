"""IODD 파서 — 프로세스 데이터 배치와 ISDU 파라미터 목록

IODD 는 디바이스 제조사가 배포하는 XML 이다. 여기서 두 가지를 뽑는다.

* **`ProcessDataIn`/`Out`** — 어느 비트가 무엇인지. 이것이 있어야 PD 바이트를
  값으로 해석할 수 있고, 커미셔닝이 만드는 **프로세스 이미지 맵**의 재료가 된다.
* **`Variable`** — ISDU 로 읽고 쓸 수 있는 파라미터. `accessRights` 가 읽기 전용과
  쓰기 가능을 가른다.

주의할 것이 하나 있다. **이름은 직계 자식에서만 찾는다.** `RecordItem` 이나
`Variable` 안에는 `SingleValue`(열거 항목)마다 또 `Name` 이 있어서, 후손을 훑으면
"ON"·"Error" 같은 **값의 이름**을 항목 이름으로 잘못 집는다.

비트 오프셋은 **LSB 기준**이다 (규격 §B.1.1). `bitOffset = 64` 인 32비트 값은
96비트 PD 에서 앞쪽 4바이트다 — 큰 오프셋이 먼저 전송된다.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


def _tag(e) -> str:
    return e.tag.split("}")[-1]


def _att(e) -> dict:
    return {k.split("}")[-1]: v for k, v in e.attrib.items()}


def _kids(e, name: str) -> list:
    return [c for c in list(e) if _tag(c) == name]


def _kid(e, name: str):
    k = _kids(e, name)
    return k[0] if k else None


def _first(e, *names):
    """이름들 중 먼저 찾아지는 직계 자식.

    **`_kid(e,"A") or _kid(e,"B")` 로 쓰면 안 된다.** ElementTree 의 Element 는
    자식 수로 참·거짓이 정해져서 `<Datatype xsi:type="Float32T"/>` 처럼 자식 없는
    엘리먼트가 거짓이 된다 — 찾았는데도 못 찾은 것처럼 다음으로 넘어가고, 그 결과
    형을 잃은 값이 16진수로 표시된다.
    """
    for n in names:
        k = _kid(e, n)
        if k is not None:
            return k
    return None


@dataclass
class Scale:
    """물리량 환산 — `물리값 = 원시값 × gradient + offset` (규격 §12.3).

    **조건이 붙는다.** 같은 항목이 단위 설정에 따라 다른 계수를 갖는다. 그
    조건은 메뉴를 가리키는 `MenuRef` 의 `Condition` 에 적혀 있고, 가리키는 것은
    장치에서 읽을 수 있는 파라미터다 — 그래서 계산이 아니라 **조회**로 정해진다.
    """
    gradient: float = 1.0
    offset: float = 0.0
    unit_code: int = 0
    decimals: int = -1       # displayFormat "Dec.N" — 소수점 자리수. -1 이면 미지정
    conds: tuple = ()        # ((variable_index, value), …)

    def holds(self, settings: dict) -> bool:
        return all(settings.get(i) == v for i, v in self.conds)

    @property
    def unit(self) -> str:
        return UNITS.get(self.unit_code, "")

    def fmt(self, v: float) -> str:
        """**IODD 가 정한 자리수로 적는다.** `displayFormat` 이 그 자리수다 —
        온도는 `Dec.1`, 적산은 `Dec.0`. 지수 표기는 쓰지 않는다: 유량 적산처럼
        큰 값이 `1e+11` 로 나오면 자릿수를 세어야 읽힌다."""
        n = self.decimals
        if n < 0:
            n = 0 if float(v).is_integer() else 3
        return f"{v:,.{n}f}"


@dataclass
class PdItem:
    """프로세스 데이터 한 항목."""
    subindex: int
    bit_offset: int          # LSB 기준
    bit_length: int
    dtype: str               # UIntegerT · IntegerT · BooleanT · Float32T · …
    name: str = ""
    unit: str = ""
    low: float | None = None
    high: float | None = None
    scales: list = field(default_factory=list)

    def scale_for(self, settings: dict | None):
        """지금 설정에 맞는 환산. 못 고르면 None — **아무거나 쓰지 않는다.**"""
        if not self.scales:
            return None
        if len(self.scales) == 1:
            return self.scales[0]
        if not settings:
            return None
        hit = [sc for sc in self.scales if sc.holds(settings)]
        return hit[0] if len(hit) == 1 else None

    def physical(self, raw, settings: dict | None = None):
        sc = self.scale_for(settings)
        return raw if sc is None else raw * sc.gradient + sc.offset

    def value_text(self, raw, settings: dict | None = None) -> str:
        sc = self.scale_for(settings)
        if sc is None:
            return f"{raw:,}" if isinstance(raw, int) else f"{raw:g}"
        u = sc.unit
        return sc.fmt(raw * sc.gradient + sc.offset) + (f" {u}" if u else "")

    def range_text(self, settings: dict | None = None) -> str:
        if self.low is None or self.high is None:
            return ""
        sc = self.scale_for(settings)
        if sc is None:
            return f"{self.low:,.0f} .. {self.high:,.0f}"
        u = sc.unit
        return (f"{sc.fmt(self.low * sc.gradient + sc.offset)} .. "
                f"{sc.fmt(self.high * sc.gradient + sc.offset)}"
                + (f" {u}" if u else ""))

    @property
    def is_bool(self) -> bool:
        return self.dtype == "BooleanT"

    def byte_span(self, total_bits: int) -> tuple[int, int]:
        """이 항목이 걸치는 바이트 범위 `[시작, 끝)`. 표시용이다."""
        hi_bit = self.bit_offset + self.bit_length - 1
        first = (total_bits - 1 - hi_bit) // 8
        last = (total_bits - 1 - self.bit_offset) // 8
        return first, last + 1

    def extract(self, data: bytes, total_bits: int):
        """PD 바이트열에서 이 항목의 값을 꺼낸다.

        전선 위는 **MSB 우선**이므로 전체를 큰 정수로 본 뒤 비트로 자른다.
        정수형은 `int`, 부동소수형은 `float` 를 돌려준다.
        """
        if len(data) * 8 < total_bits:
            return 0
        whole = int.from_bytes(data[:(total_bits + 7) // 8], "big")
        raw = (whole >> self.bit_offset) & ((1 << self.bit_length) - 1)

        if self.dtype == "IntegerT" and (raw >> (self.bit_length - 1)) & 1:
            raw -= 1 << self.bit_length       # 2의 보수
        elif self.dtype in ("Float32T", "Float64T"):
            n = 4 if self.dtype == "Float32T" else 8
            return struct.unpack(">" + ("f" if n == 4 else "d"),
                                 raw.to_bytes(n, "big"))[0]
        return raw

    def insert(self, buf: bytearray, total_bits: int, value) -> None:
        """이 항목 자리에만 값을 써 넣는다 — `extract` 의 역이다.

        **다른 항목을 건드리지 않는다.** 출력 PD 한 바이트에 여러 항목이 비트로
        나뉘어 있는 경우가 흔해서, 통째로 덮으면 옆 항목이 같이 바뀐다.
        """
        n = (total_bits + 7) // 8
        if len(buf) < n:
            return
        if self.dtype in ("Float32T", "Float64T"):
            w = 4 if self.dtype == "Float32T" else 8
            raw = int.from_bytes(
                struct.pack(">" + ("f" if w == 4 else "d"), float(value)), "big")
        else:
            raw = int(value) & ((1 << self.bit_length) - 1)

        whole = int.from_bytes(bytes(buf[:n]), "big")
        mask = ((1 << self.bit_length) - 1) << self.bit_offset
        whole = (whole & ~mask) | ((raw << self.bit_offset) & mask)
        buf[:n] = whole.to_bytes(n, "big")


@dataclass
class PdLayout:
    bit_length: int = 0
    items: list[PdItem] = field(default_factory=list)

    @property
    def byte_length(self) -> int:
        return (self.bit_length + 7) // 8


@dataclass
class ValueName:
    value: int
    name: str


@dataclass
class Param:
    """ISDU 로 접근하는 파라미터 하나."""
    index: int
    subindex: int = 0
    name: str = ""
    dtype: str = ""
    bit_length: int = 0
    access: str = "ro"           # ro · wo · rw
    unit: str = ""
    low: float | None = None
    high: float | None = None
    default: str = ""            # 공장 초기값. IODD 가 안 적으면 빈 칸으로 둔다
    values: list[ValueName] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return "w" in self.access

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def byte_length(self) -> int:
        return max(1, (self.bit_length + 7) // 8)

    @property
    def choices(self) -> str:
        """넣을 수 있는 값. 열거가 있으면 그것이 곧 범위다."""
        if self.values:
            return " / ".join(f"{v.value}={v.name}" for v in self.values)
        if self.low is not None and self.high is not None:
            return f"{self.low:g} .. {self.high:g}"
        if self.dtype == "BooleanT":
            return "true / false"
        if self.dtype == "StringT":
            return f"text, max {self.byte_length} B"
        # **범위가 없으면 지어내지 않는다.** 형이 담을 수 있는 폭은 장치가 받는
        # 폭과 다르다 — 형만 알려 주고 판단은 사람에게 남긴다.
        if self.dtype and self.bit_length:
            return f"{self.dtype} {self.bit_length}b"
        return ""


@dataclass
class Iodd:
    path: str = ""
    var_index: dict = field(default_factory=dict)   # Variable id → index
    vendor_id: int = 0
    device_id: int = 0
    vendor: str = ""
    product: str = ""
    pd_in: PdLayout = field(default_factory=PdLayout)
    pd_out: PdLayout = field(default_factory=PdLayout)
    params: list[Param] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (f"{self.product}   VID {self.vendor_id}  DID {self.device_id}   "
                f"PD in {self.pd_in.byte_length} B / out {self.pd_out.byte_length} B")


class IoddError(Exception):
    pass


def load(path: str | Path) -> Iodd:
    """IODD 를 읽는다. `.xml` 도 되고 벤더가 배포하는 `.zip` 도 된다."""
    p = Path(path)
    if not p.exists():
        raise IoddError(f"file not found: {p}")

    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")
                     and "IODD" in n.upper()]
            if not names:
                names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise IoddError("no IODD XML inside the zip")
            data = z.read(sorted(names)[0])
        root = ET.fromstring(data)
    else:
        root = ET.parse(p).getroot()

    return _parse(root, str(p))


# ── 규격이 정한 것들 ────────────────────────────────────────────────────────
#
# 단위 기호와 표준 변수(VendorName·ProductName·…)는 **IODD 규격 배포물**에 들어
# 있다. 라이선스가 있는 자료라 저장소에 넣지 않고, IODD 파일과 같은 폴더에 두면
# 그때 읽는다:
#
#   IODD-StandardUnitDefinitions1.1.xml   unitCode → 기호 (688개)
#   IODD-StandardDefinitions1.1.xml       StdVariableRef → 인덱스·이름
#
# 없으면 조용히 넘어간다 — 기호가 없으면 숫자만 보이고, 그것은 틀린 것이 아니다.
UNITS: dict[int, str] = {}
STD_VARS: dict[str, dict] = {}
_STD_ROOT = [None]              # 표준 정의 XML 뿌리 — 이름·형을 여기서 꺼낸다
_std_loaded = False


def load_standard(folder: str | Path) -> None:
    """규격 배포물을 한 번 읽어 둔다. 폴더에 없으면 아무 일도 하지 않는다."""
    global _std_loaded
    if _std_loaded:
        return
    _std_loaded = True
    root_dir = Path(folder)

    f = root_dir / "IODD-StandardUnitDefinitions1.1.xml"
    if f.is_file():
        try:
            for e in ET.parse(f).getroot().iter():
                if _tag(e) != "Unit":
                    continue
                a = _att(e)
                if a.get("code") and a.get("abbr"):
                    UNITS[int(a["code"])] = a["abbr"]
        except (ET.ParseError, OSError, ValueError):
            pass

    f = root_dir / "IODD-StandardDefinitions1.1.xml"
    if f.is_file():
        try:
            r = ET.parse(f).getroot()
            _STD_ROOT[0] = r
            for e in r.iter():
                if _tag(e) != "Variable":
                    continue
                a = _att(e)
                if a.get("id") and a.get("index") is not None:
                    STD_VARS[a["id"]] = {"index": int(a["index"]),
                                         "access": a.get("accessRights", "ro"),
                                         "el": e}
        except (ET.ParseError, OSError, ValueError):
            pass


# 이미 읽은 것은 다시 파싱하지 않는다. 250 KB XML 을 포트 고를 때마다 훑으면
# 화면이 눈에 띄게 멈춘다. mtime 이 바뀌면 다시 읽는다.
_CACHE: dict[str, tuple[float, Iodd]] = {}


def load_cached(path: str | Path) -> Iodd:
    p = Path(path)
    key = str(p)
    mtime = p.stat().st_mtime
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    d = load(p)
    _CACHE[key] = (mtime, d)
    return d


def index(folder: str | Path) -> dict[tuple[int, int], Iodd]:
    """폴더의 IODD 를 **VID/DID 로** 색인한다.

    장치가 자기 신원을 말해 주므로 파일 이름에 기대지 않는다 — 이름은 벤더마다
    다르고 바뀌지만 VID/DID 는 그 장치 자체다.

    같은 신원이 여럿이면 **먼저 온 것을 쓴다.** 골라 줄 근거가 없는 상태에서
    말없이 뒤엣것으로 덮으면 어느 파일이 쓰였는지 알 수 없게 된다.
    """
    out: dict[tuple[int, int], Iodd] = {}
    root = Path(folder)
    if not root.is_dir():
        return out
    load_standard(root)             # 단위·표준 변수는 여기 같이 놓인다
    for p in sorted(root.iterdir()):
        if p.suffix.lower() not in (".xml", ".zip"):
            continue
        try:
            d = load_cached(p)
        except (IoddError, OSError, ET.ParseError):
            continue                      # 못 읽는 파일 하나가 나머지를 막지 않는다
        if d.vendor_id and d.device_id:
            out.setdefault((d.vendor_id, d.device_id), d)
    return out


def _deref(e, types: dict):
    """`DatatypeRef` 면 전역 정의로 바꿔 준다.

    **규격 예제가 거의 이 방식이다.** `<DatatypeRef datatypeId="D_PDin"/>` 하나만
    두고 실체는 `DatatypeCollection` 에 있다. 따라가지 않으면 형도 항목도 통째로
    비어 프로세스 데이터가 없는 장치처럼 보인다.
    """
    if e is None:
        return None
    if _tag(e) != "DatatypeRef":
        return e
    return types.get(_att(e).get("datatypeId", ""))


def _parse(root, path: str) -> Iodd:
    io = Iodd(path=path)

    # 전역 형 정의. `id` 로 참조된다.
    types = {}
    for e in root.iter():
        if _tag(e) == "Datatype" and "id" in _att(e):
            types[_att(e)["id"]] = e

    # ── 텍스트 — 영어를 우선 쓰고 없으면 첫 번째 언어 ────────────────────────
    texts: dict[str, str] = {}
    langs = [e for e in root.iter() if _tag(e) in ("PrimaryLanguage", "Language")]
    en = next((L for L in langs if _att(L).get("lang", "").startswith("en")), None)
    for L in ([en] if en is not None else langs[:1]):
        if L is None:
            continue
        for t in _kids(L, "Text"):
            a = _att(t)
            texts[a.get("id", "")] = a.get("value", "")

    def name_of(e) -> str:
        """**직계 자식만** — 후손을 훑으면 값의 이름을 집는다."""
        n = _kid(e, "Name")
        return texts.get(_att(n).get("textId", ""), "") if n is not None else ""

    # ── 신원 ────────────────────────────────────────────────────────────────
    for e in root.iter():
        if _tag(e) == "DeviceIdentity":
            a = _att(e)
            io.vendor_id = int(a.get("vendorId", 0) or 0)
            io.vendor = a.get("vendorName", "")
            # IODD 1.1 은 여기 deviceId 를 둔다. 판에 따라 DeviceVariant 에만
            # 있는 경우가 있어 둘 다 본다.
            if a.get("deviceId"):
                io.device_id = int(a["deviceId"])
            break

    for e in root.iter():
        if _tag(e) == "DeviceVariant":
            a = _att(e)
            if not io.device_id and a.get("deviceId"):
                io.device_id = int(a["deviceId"])
            nm = _kid(e, "Name")
            if nm is not None and not io.product:
                io.product = texts.get(_att(nm).get("textId", ""), "")
            if io.device_id and io.product:
                break
    if not io.product:
        io.product = texts.get("TI_Device_Name", "")

    # ── 프로세스 데이터 ─────────────────────────────────────────────────────
    for e in root.iter():
        t = _tag(e)
        if t in ("ProcessDataIn", "ProcessDataOut"):
            layout = _pd_layout(e, name_of, texts, types)
            if t == "ProcessDataIn" and layout.bit_length:
                io.pd_in = layout
            elif t == "ProcessDataOut" and layout.bit_length:
                io.pd_out = layout

    # ── ISDU 파라미터 ───────────────────────────────────────────────────────
    for v in root.iter():
        if _tag(v) != "Variable":
            continue
        a = _att(v)
        if "index" not in a:
            continue
        io.params.extend(_variable(v, a, name_of, texts, types))

    # ── 표준 변수 ───────────────────────────────────────────────────────────
    # `<StdVariableRef id="V_ProductName"/>` 하나만 적히고 정의는 규격 배포물에
    # 있다. **예제마다 16개씩 이렇게 들어 있다** — 따라가지 않으면 VendorName·
    # ProductName·SerialNumber 같은 기본 항목이 통째로 빠진다.
    _std_refs(root, io, texts)

    io.params.sort(key=lambda p: (p.index, p.subindex))

    # ── 환산 계수 ───────────────────────────────────────────────────────────
    # **메뉴에 적혀 있다.** 프로세스 데이터를 가리키는 `RecordItemRef` 가
    # `gradient`·`offset`·`unitCode` 를 들고 있고, 어느 것을 쓸지는 그 메뉴를
    # 가리키는 `MenuRef` 의 `Condition` 이 정한다 (예: `V_Unit_T == 0` 이면 ℃).
    _scales(root, io)
    return io


def _scales(root, io: Iodd) -> None:
    for v in root.iter():
        if _tag(v) == "Variable" and "index" in _att(v):
            a = _att(v)
            io.var_index[a.get("id", "")] = int(a["index"])

    # 메뉴 id → 그 메뉴를 가리키는 MenuRef 들의 조건
    cond: dict[str, list] = {}
    for m in root.iter():
        if _tag(m) != "MenuRef":
            continue
        mid = _att(m).get("menuId", "")
        cs = []
        for c in _kids(m, "Condition"):
            ca = _att(c)
            idx = io.var_index.get(ca.get("variableId", ""))
            if idx is not None and ca.get("value") is not None:
                cs.append((idx, int(ca["value"])))
        if cs:
            cond.setdefault(mid, []).extend(cs)

    by_sub: dict[int, list] = {}
    for m in root.iter():
        if _tag(m) != "Menu":
            continue
        mid = _att(m).get("id", "")
        for r in m:
            a = _att(r)
            if _tag(r) != "RecordItemRef" or "gradient" not in a:
                continue
            if a.get("variableId") != "V_ProcessDataInput":
                continue
            try:
                df = a.get("displayFormat", "")
                dec = int(df.split(".")[1]) if df.startswith("Dec.") else -1
                sc = Scale(float(a["gradient"]), float(a.get("offset", 0) or 0),
                           int(a.get("unitCode", 0) or 0), dec,
                           tuple(cond.get(mid, [])))
                sub = int(a.get("subindex", 0))
            except (TypeError, ValueError):
                continue
            # 같은 (계수, 조건)이 여러 메뉴에 나온다 — 한 번만 담는다
            lst = by_sub.setdefault(sub, [])
            if not any(x == sc for x in lst):
                lst.append(sc)

    for it in io.pd_in.items:
        it.scales = by_sub.get(it.subindex, [])


# 폭이 형에 붙박이인 것들. IODD 는 이런 형에 `bitLength` 를 적지 않는다 —
# 없다고 1비트로 두면 값이 한 바이트로 잘린다.
_FIXED_BITS = {"Float32T": 32, "Float64T": 64, "BooleanT": 1}


def _std_refs(root, io: Iodd, texts: dict) -> None:
    if not STD_VARS:
        return                       # 규격 배포물이 없으면 조용히 넘어간다
    std_texts = _texts_of(_STD_ROOT[0]) if _STD_ROOT[0] is not None else {}

    def std_name(e) -> str:
        n = _kid(e, "Name")
        if n is None:
            return ""
        tid = _att(n).get("textId", "")
        return texts.get(tid) or std_texts.get(tid, "")

    have = {(p.index, p.subindex) for p in io.params}
    std_types = {}
    if _STD_ROOT[0] is not None:
        for e in _STD_ROOT[0].iter():
            if _tag(e) == "Datatype" and "id" in _att(e):
                std_types[_att(e)["id"]] = e

    for r in root.iter():
        if _tag(r) != "StdVariableRef":
            continue
        a = _att(r)
        std = STD_VARS.get(a.get("id", ""))
        if std is None:
            continue
        el = std.get("el")
        if el is None:
            continue
        made = _variable(el, _att(el), std_name, std_texts, std_types)
        for prm in made:
            # IODD 쪽이 접근권한·기본값을 덮어쓸 수 있다 (규격 §10.4)
            prm.access = a.get("accessRights", prm.access)
            if a.get("defaultValue") is not None:
                prm.default = a["defaultValue"]
            if (prm.index, prm.subindex) not in have:
                io.params.append(prm)
                have.add((prm.index, prm.subindex))


def _texts_of(root) -> dict:
    out = {}
    langs = [e for e in root.iter() if _tag(e) in ("PrimaryLanguage", "Language")]
    en = next((L for L in langs if _att(L).get("lang", "").startswith("en")), None)
    for L in ([en] if en is not None else langs[:1]):
        if L is None:
            continue
        for t in _kids(L, "Text"):
            a = _att(t)
            out[a.get("id", "")] = a.get("value", "")
    return out


def _simple(e) -> tuple[str, int]:
    """`SimpleDatatype` 또는 `Datatype` 에서 (형, 비트수)."""
    a = _att(e)
    ty = a.get("type", "")
    bl = a.get("bitLength")
    if bl:
        return ty, int(bl)
    return ty, _FIXED_BITS.get(ty, 1)


def _range(e) -> tuple[float | None, float | None]:
    vr = _kid(e, "ValueRange")
    if vr is None:
        return None, None
    a = _att(vr)
    try:
        return float(a.get("lowerValue")), float(a.get("upperValue"))
    except (TypeError, ValueError):
        return None, None


def _pd_layout(e, name_of, texts, types: dict | None = None) -> PdLayout:
    types = types or {}
    dt = _deref(_first(e, "Datatype", "DatatypeRef"), types)
    total = int(_att(e).get("bitLength", 0) or 0)
    if dt is None:
        return PdLayout()

    lay = PdLayout(bit_length=total or int(_att(dt).get("bitLength", 0) or 0))

    items = _kids(dt, "RecordItem")
    if not items:
        # 레코드가 아니라 단일 값인 경우
        ty, bl = _simple(dt)
        lo, hi = _range(dt)
        lay.items.append(PdItem(1, 0, bl or lay.bit_length, ty,
                                name_of(e) or "value", low=lo, high=hi))
        return lay

    for ri in items:
        a = _att(ri)
        sd = _deref(_first(ri, "SimpleDatatype", "Datatype", "DatatypeRef"), types)
        ty, bl = _simple(sd) if sd is not None else ("", 1)
        lo, hi = _range(sd) if sd is not None else (None, None)
        lay.items.append(PdItem(
            subindex=int(a.get("subindex", 0)),
            bit_offset=int(a.get("bitOffset", 0)),
            bit_length=bl,
            dtype=ty,
            name=name_of(ri),
            low=lo, high=hi,
        ))

    lay.items.sort(key=lambda i: -i.bit_offset)     # 전송 순서대로
    return lay


def _variable(v, a, name_of, texts, types: dict | None = None) -> list[Param]:
    types = types or {}
    index = int(a.get("index"))
    access = a.get("accessRights", "ro")
    base_name = name_of(v)
    # 공장 초기값. 단일 값은 Variable 에, 레코드는 서브인덱스마다 `RecordItemInfo`
    # 에 붙는다 — 같은 것을 두 군데에 적어 둔 것이 아니라 층이 다르다.
    dflt = a.get("defaultValue", "")
    ri_dflt = {int(_att(x).get("subindex", 0)): _att(x).get("defaultValue", "")
               for x in _kids(v, "RecordItemInfo")}

    dt = _deref(_first(v, "Datatype", "DatatypeRef"), types)
    if dt is None:
        return [Param(index, 0, base_name, "", 0, access, default=dflt)]

    if _att(dt).get("type") == "RecordT":
        out = []
        for ri in _kids(dt, "RecordItem"):
            ra = _att(ri)
            sub = int(ra.get("subindex", 0))
            sd = _deref(_first(ri, "SimpleDatatype", "Datatype", "DatatypeRef"),
                        types)
            ty, bl = _simple(sd) if sd is not None else ("", 1)
            lo, hi = _range(sd) if sd is not None else (None, None)
            vals = _single_values(sd, name_of) if sd is not None else []
            out.append(Param(index, sub,
                             name_of(ri) or base_name, ty, bl,
                             ra.get("accessRightRestriction", access),
                             low=lo, high=hi,
                             default=ri_dflt.get(sub, ""), values=vals))
        return out or [Param(index, 0, base_name, "RecordT", 0, access,
                             default=dflt)]

    ty, bl = _simple(dt)
    lo, hi = _range(dt)
    return [Param(index, 0, base_name, ty, bl, access, low=lo, high=hi,
                  default=dflt, values=_single_values(dt, name_of))]


def _single_values(dt, name_of) -> list[ValueName]:
    """`SingleValue` 열거. 이것이 있으면 그것이 곧 넣을 수 있는 값의 전부다."""
    out = []
    for sv in _kids(dt, "SingleValue"):
        sa = _att(sv)
        try:
            out.append(ValueName(int(sa.get("value")), name_of(sv)))
        except (TypeError, ValueError):
            pass
    return out
