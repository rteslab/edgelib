"""설정 JSON 에서 **응용 코드가 쓸 형**을 뽑는다 — C 구조체와 파이썬 클래스

프로세스 데이터는 결국 바이트열이다. 응용이 그것을 매번 손으로 풀면 세 가지가
어긋난다 — **비트 위치**(한 바이트에 여덟 항목이 들어간다), **환산 계수**(283 은
28.3 ℃ 다), 그리고 **어느 조각이 어느 노드·포트 것인지**. 셋 다 커미셔닝이 이미
알아낸 것이라, 코드로 굳혀 주면 응용은 이름만 쓰면 된다.

```c
edgex_in_t in;
edgex_in_read(bus, &in);
if (in.n1_p1.valid) { printf("%.1f °C\\n", in.n1_p1.temperature); }
```

**이미지는 통째로 오간다.** 포트별로 조각을 받아 오프셋을 맞추는 일이 없다 —
`byte` 는 애초에 버스 전체 이미지 기준이므로, 조각을 받으면 노드 2 항목이 버퍼
밖을 짚는다. 한정자도 여기 들어온다: `PQ` 는 입력 구조체의 `valid`, `OE` 는 출력
구조체의 `enable` 이다. 둘 다 이미지 안의 비트일 뿐이라 별도의 API 가 필요 없다.

**JSON 을 읽는 코드를 대신하는 것이 아니다.** 라이브러리는 그대로 JSON 을 읽고
버스를 잡는다. 이 파일이 만드는 것은 그 위에서 값을 이름으로 다루기 위한 껍데기이고,
컴파일 시점에 굳으므로 오타가 빌드에서 잡힌다. **의존은 한 방향이다** — 생성 코드가
`edgelib.h` 를 부르지, 라이브러리가 생성 코드를 알지 못한다.
"""

from __future__ import annotations

import keyword
import re

_C_RESERVED = {"double", "float", "int", "char", "bool", "short", "long",
               "signed", "unsigned", "struct", "union", "enum", "static",
               "const", "void", "if", "else", "for", "while", "return"}


def ident(name: str, taken: set | None = None) -> str:
    """사람이 붙인 이름을 식별자로. `OUT2(PRES)` · `P_HHH/LLL` 같은 것이 온다."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    s = re.sub(r"_+", "_", s) or "item"
    if s[0].isdigit():
        s = "v" + s
    if s in _C_RESERVED or keyword.iskeyword(s):
        s += "_"
    if taken is not None:
        base, n = s, 2
        while s in taken:
            s = f"{base}{n}"
            n += 1
        taken.add(s)
    return s


def _quals(cfg: dict) -> dict:
    """노드마다 한정자 바이트가 하나 — 입력은 `MST`, 출력은 `OE` (클래스 문서 §2).

    포트 k 의 비트는 `k - 1` 이다. 입력의 상위 니블 `ISDU_RDY` 는 여기서 뽑지
    않는다 — ISDU 완료를 기다리는 것은 라이브러리 안의 일이라 응용의 관심사가 아니다.

    **`role` 이 없는 옛 설정 파일도 읽는다.** 그 자리를 가리키는 것은 결국
    `port == 0` 이고, 모듈 수준 항목은 한정자뿐이다.
    """
    out: dict[tuple, int] = {}
    for direction in ("in", "out"):
        for e in cfg["process_image"][direction]["entries"]:
            if e.get("role") or e["port"] == 0:
                out.setdefault((e["node"], direction), e["byte"])
    return out


def _groups(cfg: dict) -> list[dict]:
    """(노드, 포트, 방향) 마다 하나. 한정자는 데이터가 아니라 `qual` 로 따로 든다."""
    q = _quals(cfg)
    out: dict[tuple, dict] = {}
    for direction in ("in", "out"):
        for e in cfg["process_image"][direction]["entries"]:
            if e.get("role") or e["port"] == 0:
                continue
            key = (e["node"], e["port"], direction)
            g = out.setdefault(key, {"node": e["node"], "port": e["port"],
                                     "dir": direction, "items": [],
                                     "qual": q.get((e["node"], direction))})
            g["items"].append(e)
    return [out[k] for k in sorted(out)]


def _is_bool(e: dict) -> bool:
    return e.get("type") == "BooleanT"


def _scaled(e: dict) -> bool:
    return "scale" in e and not _is_bool(e)


def _c_type(e: dict) -> str:
    """C 쪽 필드 형. **부호와 폭을 뭉치지 않는다.**

    `int64_t` 하나로 다 받으면 8바이트 UIntegerT 의 최상위 비트가 선 순간 음수가
    된다. IO-Link 의 UIntegerT 는 64비트까지 가므로 일어날 수 있는 일이고, 일어나면
    값이 뒤집힌 채로 조용히 흐른다.
    """
    if _is_bool(e):
        return "bool"
    if _scaled(e):
        return "double"
    if e.get("bits", 8) < 8 and "bit" in e:
        return "uint8_t"                     # 한 바이트 안의 몇 비트
    return "int64_t" if e.get("type") == "IntegerT" else "uint64_t"


def _qual_name(direction: str) -> str:
    return "valid" if direction == "in" else "enable"


def _has_qual(g: dict) -> bool:
    """이 그룹에 한정자(`valid` / `enable`)를 만들 것인가.

    **SIO 포트에는 만들지 않는다.** 규격이 `PQI` 를 DI 에서 의미 없다고 하고
    (Table E.10 각주 a), `OE` 는 디바이스에게 "이 출력이 유효하다"고 알리는
    IO-Link 프레임이라 세션이 없는 SIO 에는 보낼 상대가 없다 (§11.7.3.2 —
    "No IO-Link session exists in this mode"). 실측으로도 OE 와 무관하게
    C/Q 가 구동된다.

    없는 것을 "언제나 참"으로 채우면 응용은 그것이 정보인 줄 안다.
    """
    if g["qual"] is None:
        return False
    names = [e["name"] for e in g["items"]]
    return not any(nm.startswith("C/Q") for nm in names)


def _fields(g: dict) -> list[tuple]:
    """한정자 이름을 먼저 잡아 둔다 — IODD 에 `Valid` 라는 항목이 있으면 그쪽이 밀린다."""
    taken: set = {_qual_name(g["dir"])} if _has_qual(g) else set()
    return [(ident(e["name"], taken), e) for e in g["items"]]


# ── C ───────────────────────────────────────────────────────────────────────
def emit_c(cfg: dict, guard: str = "EDGEX_IMAGE_H") -> str:
    L: list[str] = []
    a = L.append
    in_bytes = cfg["process_image"]["in"]["bytes"]
    out_bytes = cfg["process_image"]["out"]["bytes"]

    a("/* EdgeConfig 가 만든 파일 — 고치지 마세요.")
    a(" *")
    a(" * 프로세스 이미지 전체를 이름으로 다루기 위한 구조체입니다. 비트 위치와")
    a(" * 환산 계수는 커미셔닝이 정한 것이 그대로 굳어 있습니다.")
    a(" *")
    a(" *     edgex_in_t in;")
    a(" *     edgex_in_read(bus, &in);")
    a(" *     if (in.n1_p1.valid) { ... in.n1_p1.temperature ... }")
    a(" */")
    a(f"#ifndef {guard}")
    a(f"#define {guard}")
    a("")
    a("#include <stdbool.h>")
    a("#include <stdint.h>")
    a("")
    a('#include "edgelib.h"')
    a("")
    a(f"#define EDGEX_IMAGE_IN_BYTES   {in_bytes}")
    a(f"#define EDGEX_IMAGE_OUT_BYTES  {out_bytes}")
    a("")
    a("/* 이미지에서 정수를 꺼낸다 — 전선 위는 MSB 우선이다.")
    a(" *")
    a(" * **64비트로 받는다.** 4바이트 부호 없는 값은 32비트에 담으면 최상위 비트가")
    a(" * 선 순간 음수가 되어, 적산 유량 같은 큰 값이 조용히 뒤집힌다. */")
    a("static inline uint64_t edgex_u(const uint8_t *img, uint16_t at, uint8_t n)")
    a("{")
    a("    uint64_t v = 0;")
    a("    for (uint8_t i = 0; i < n; i++) { v = (v << 8) | img[at + i]; }")
    a("    return v;")
    a("}")
    a("")
    a("static inline int64_t edgex_i(const uint8_t *img, uint16_t at, uint8_t n)")
    a("{")
    a("    const uint64_t v = edgex_u(img, at, n);")
    a("    const uint64_t sign = (uint64_t)1 << (n * 8u - 1u);")
    a("    return (v & sign) ? (int64_t)(v - (sign << 1)) : (int64_t)v;")
    a("}")
    a("")

    groups = _groups(cfg)
    for g in groups:
        tag = f"edgex_n{g['node']}_p{g['port']}_{g['dir']}"
        fields = _fields(g)

        a(f"/* 노드 {g['node']} 포트 {g['port']} — "
          f"{'입력' if g['dir'] == 'in' else '출력'} */")
        a("typedef struct {")
        if _has_qual(g):
            if g["dir"] == "in":
                a("    bool   valid;   /* PQ — 0 이면 이번 사이클 값을 쓰지 마세요 */")
            else:
                a("    bool   enable;  /* OE — 0 이면 디바이스가 페일세이프 값을 쥔다 */")
        for name, e in fields:
            t = _c_type(e)
            u = (f"   /* {e['unit']} */"
                 if (_scaled(e) and e.get("unit")) else "")
            a(f"    {t:<8} {name};{u}")
        a(f"}} {tag}_t;")
        a("")

        if g["dir"] == "in":
            a(f"static inline void {tag}_decode(const uint8_t *img, {tag}_t *o)")
            a("{")
            if _has_qual(g):
                a(f"    o->valid = ((img[{g['qual']}] >> {g['port'] - 1}) & 1u) != 0u;")
            for name, e in fields:
                a(f"    {_c_get(e, name)}")
            a("}")
        else:
            a("/* 자기 비트만 고친다 — 한정자 바이트는 포트끼리 나눠 쓴다. */")
            a(f"static inline void {tag}_encode(const {tag}_t *o, uint8_t *img)")
            a("{")
            if _has_qual(g):
                b, bit = g["qual"], g["port"] - 1
                a(f"    img[{b}] = (uint8_t)((img[{b}] & ~{1 << bit:#04x}u) | "
                  f"((o->enable ? 1u : 0u) << {bit}));")
            for name, e in fields:
                a(f"    {_c_put(e, name)}")
            a("}")
        a("")

    a(_c_aggregate(groups, "in", in_bytes))
    a(_c_aggregate(groups, "out", out_bytes))
    a(f"#endif /* {guard} */")
    return "\n".join(L) + "\n"


def _c_aggregate(groups: list[dict], direction: str, total: int) -> str:
    """이미지 전체 — 포트 구조체를 모아 놓은 것 하나와, 버스에 대는 함수 하나."""
    gs = [g for g in groups if g["dir"] == direction]
    if not gs or total <= 0:
        return ""
    L: list[str] = []
    a = L.append
    name = f"edgex_{direction}"
    a(f"/* 이미지 전체 — {'입력' if direction == 'in' else '출력'} */")
    a("typedef struct {")
    for g in gs:
        a(f"    edgex_n{g['node']}_p{g['port']}_{direction}_t "
          f"n{g['node']}_p{g['port']};")
    a(f"}} {name}_t;")
    a("")

    if direction == "in":
        a(f"static inline void {name}_decode(const uint8_t *img, {name}_t *o)")
        a("{")
        for g in gs:
            a(f"    edgex_n{g['node']}_p{g['port']}_in_decode"
              f"(img, &o->n{g['node']}_p{g['port']});")
        a("}")
        a("")
        a("/* 한 번의 호출로 이미지를 받아 이름 붙은 값으로 푼다. */")
        a(f"static inline int {name}_read(edgelib_t *bus, {name}_t *o)")
        a("{")
        a("    uint8_t  img[EDGEX_IMAGE_IN_BYTES];")
        a("    uint16_t len = (uint16_t)sizeof img;")
        a("    const int rc = edgelib_image_in(bus, img, &len);")
        a(f"    if (rc == EDGE_OK) {{ {name}_decode(img, o); }}")
        a("    return rc;")
        a("}")
    else:
        a("/* 이미지를 통째로 만든다 — 쓰지 않는 자리는 0 이다. */")
        a(f"static inline void {name}_encode(const {name}_t *o, uint8_t *img)")
        a("{")
        a("    for (uint16_t i = 0; i < EDGEX_IMAGE_OUT_BYTES; i++) "
          "{ img[i] = 0; }")
        for g in gs:
            a(f"    edgex_n{g['node']}_p{g['port']}_out_encode"
              f"(&o->n{g['node']}_p{g['port']}, img);")
        a("}")
        a("")
        a(f"static inline int {name}_write(edgelib_t *bus, const {name}_t *o)")
        a("{")
        a("    uint8_t img[EDGEX_IMAGE_OUT_BYTES];")
        a(f"    {name}_encode(o, img);")
        a("    return edgelib_image_out(bus, img, (uint16_t)sizeof img);")
        a("}")
    a("")
    return "\n".join(L)


def _c_get(e: dict, name: str) -> str:
    byte, ln = e["byte"], e["length"]
    if _is_bool(e) or (e.get("bits", 8) < 8 and "bit" in e):
        return (f"o->{name} = (img[{byte}] >> {e['bit']}) & "
                f"{(1 << e.get('bits', 1)) - 1};")
    signed_ = e.get("type") == "IntegerT"
    raw = (f"edgex_i(img, {byte}, {ln})" if signed_
           else f"edgex_u(img, {byte}, {ln})")
    if _scaled(e):
        sc = e["scale"]
        # **곱하기 전에 double 로 넓힌다.** 정수끼리 곱해 넘치면 그 다음은 없다
        return (f"o->{name} = (double){raw} * {sc['gradient']!r} "
                f"+ {sc['offset']!r};")
    return f"o->{name} = ({_c_type(e)}){raw};"


def _c_put(e: dict, name: str) -> str:
    byte = e["byte"]
    if _is_bool(e) or (e.get("bits", 8) < 8 and "bit" in e):
        mask = ((1 << e.get("bits", 1)) - 1) << e["bit"]
        return (f"img[{byte}] = (uint8_t)((img[{byte}] & ~{mask:#04x}u) | "
                f"((o->{name} ? 1u : 0u) << {e['bit']}));")
    ln = e["length"]
    if _scaled(e):
        sc = e["scale"]
        val = f"(uint64_t)(int64_t)((o->{name} - {sc['offset']!r}) "
        val += f"/ {sc['gradient']!r})"
    else:
        val = f"(uint64_t)o->{name}"
    lines = [f"{{ uint64_t v = {val};"]
    for i in range(ln):
        shift = 8 * (ln - 1 - i)
        lines.append(f"img[{byte + i}] = (uint8_t)(v >> {shift});")
    lines.append("}")
    return " ".join(lines)


# ── Python ──────────────────────────────────────────────────────────────────
def emit_py(cfg: dict) -> str:
    L: list[str] = []
    a = L.append
    in_bytes = cfg["process_image"]["in"]["bytes"]
    out_bytes = cfg["process_image"]["out"]["bytes"]

    a('"""EdgeConfig 가 만든 파일 — 고치지 마세요.')
    a("")
    a("프로세스 이미지 전체를 이름으로 다루기 위한 클래스입니다. 비트 위치와")
    a("환산 계수는 커미셔닝이 정한 것이 그대로 굳어 있습니다.")
    a("")
    a("    pd = In.read(bus)")
    a("    if pd.n1_p1.valid:")
    a("        print(pd.n1_p1.temperature)")
    a('"""')
    a("")
    a("from dataclasses import dataclass, field")
    a("")
    a(f"IN_BYTES = {in_bytes}")
    a(f"OUT_BYTES = {out_bytes}")
    a("")
    a("")
    a("def _u(img, at, n):")
    a('    """MSB 우선."""')
    a("    return int.from_bytes(bytes(img[at:at + n]), 'big')")
    a("")
    a("")
    a("def _i(img, at, n):")
    a("    return int.from_bytes(bytes(img[at:at + n]), 'big', signed=True)")
    a("")

    groups = _groups(cfg)
    for g in groups:
        cls = f"N{g['node']}P{g['port']}{'In' if g['dir'] == 'in' else 'Out'}"
        fields = _fields(g)

        a("")
        a("@dataclass")
        a(f"class {cls}:")
        a(f'    """노드 {g["node"]} 포트 {g["port"]} — '
          f'{"입력" if g["dir"] == "in" else "출력"}."""')
        if _has_qual(g):
            if g["dir"] == "in":
                a("    valid: bool = False    # PQ — False 면 이번 사이클 값을 쓰지 마세요")
            else:
                a("    enable: bool = False   # OE — False 면 디바이스가 페일세이프 값을 쥔다")
        for name, e in fields:
            if _is_bool(e):
                a(f"    {name}: bool = False")
            elif _scaled(e):
                u = f"    # {e['unit']}" if e.get("unit") else ""
                a(f"    {name}: float = 0.0{u}")
            else:
                # 파이썬 int 는 폭이 없다 — C 쪽 형을 주석으로 남겨 대조하기 쉽게
                a(f"    {name}: int = 0    # {_c_type(e)}")
        a("")
        if g["dir"] == "in":
            a("    @classmethod")
            a("    def decode(cls, img):")
            a("        o = cls()")
            if _has_qual(g):
                a(f"        o.valid = bool((img[{g['qual']}] >> "
                  f"{g['port'] - 1}) & 1)")
            for name, e in fields:
                a(f"        {_py_get(e, name)}")
            a("        return o")
        else:
            a("    def encode(self, img):")
            a('        """`img` 는 bytearray 여야 한다 — 자기 비트만 고친다."""')
            if _has_qual(g):
                b, bit = g["qual"], g["port"] - 1
                a(f"        img[{b}] = (img[{b}] & ~{1 << bit:#04x}) | "
                  f"((1 if self.enable else 0) << {bit})")
            for name, e in fields:
                a(f"        {_py_put(e, name)}")
            a("        return img")

    a(_py_aggregate(groups, "in", in_bytes))
    a(_py_aggregate(groups, "out", out_bytes))
    return "\n".join(L) + "\n"


def _py_aggregate(groups: list[dict], direction: str, total: int) -> str:
    gs = [g for g in groups if g["dir"] == direction]
    if not gs or total <= 0:
        return ""
    L: list[str] = []
    a = L.append
    cls = "In" if direction == "in" else "Out"
    a("")
    a("")
    a("@dataclass")
    a(f"class {cls}:")
    a(f'    """이미지 전체 — {"입력" if direction == "in" else "출력"}."""')
    for g in gs:
        sub = f"N{g['node']}P{g['port']}{cls}"
        a(f"    n{g['node']}_p{g['port']}: {sub} = "
          f"field(default_factory={sub})")
    a("")
    if direction == "in":
        a("    @classmethod")
        a("    def decode(cls, img):")
        a("        return cls(")
        for g in gs:
            a(f"            n{g['node']}_p{g['port']}="
              f"N{g['node']}P{g['port']}In.decode(img),")
        a("        )")
        a("")
        a("    @classmethod")
        a("    def read(cls, bus):")
        a('        """한 번의 호출로 이미지를 받아 이름 붙은 값으로 푼다."""')
        a("        return cls.decode(bus.image_in())")
    else:
        a("    def encode(self, img=None):")
        a('        """쓰지 않는 자리는 0 이다."""')
        a("        img = bytearray(OUT_BYTES) if img is None else img")
        for g in gs:
            a(f"        self.n{g['node']}_p{g['port']}.encode(img)")
        a("        return img")
        a("")
        a("    def write(self, bus):")
        a("        return bus.image_out(bytes(self.encode()))")
    return "\n".join(L)


def _py_get(e: dict, name: str) -> str:
    byte, ln = e["byte"], e["length"]
    if _is_bool(e) or (e.get("bits", 8) < 8 and "bit" in e):
        mask = (1 << e.get("bits", 1)) - 1
        v = f"(img[{byte}] >> {e['bit']}) & {mask}"
        return f"o.{name} = bool({v})" if _is_bool(e) else f"o.{name} = {v}"
    raw = (f"_i(img, {byte}, {ln})" if e.get("type") == "IntegerT"
           else f"_u(img, {byte}, {ln})")
    if _scaled(e):
        sc = e["scale"]
        return f"o.{name} = {raw} * {sc['gradient']!r} + {sc['offset']!r}"
    return f"o.{name} = {raw}"


def _py_put(e: dict, name: str) -> str:
    byte = e["byte"]
    if _is_bool(e) or (e.get("bits", 8) < 8 and "bit" in e):
        mask = ((1 << e.get("bits", 1)) - 1) << e["bit"]
        return (f"img[{byte}] = (img[{byte}] & ~{mask:#04x}) | "
                f"((1 if self.{name} else 0) << {e['bit']})")
    ln = e["length"]
    if _scaled(e):
        sc = e["scale"]
        val = f"int((self.{name} - {sc['offset']!r}) / {sc['gradient']!r})"
    else:
        val = f"int(self.{name})"
    return (f"img[{byte}:{byte + ln}] = "
            f"({val} & {(1 << (8 * ln)) - 1}).to_bytes({ln}, 'big')")
