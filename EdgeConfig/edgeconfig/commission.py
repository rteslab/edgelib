"""커미셔닝 한 번에 — 버스를 읽어 설정 파일과 응용 코드를 낸다

Copyright (c) 2026 RTES Co., Ltd. All rights reserved.

GUI 로 하던 일을 명령 한 줄로 한다. 하는 일이 넷이다.

1. **edgelib 으로** 버스를 훑는다 — 노드·포트·PD 크기는 장치가 말해 준다
2. IODD 를 VID/DID 로 찾아 PD 항목의 이름·비트 위치·환산을 얻는다
3. 환산이 조건부면 그 조건이 가리키는 **디바이스 파라미터를 ISDU 로 읽는다** —
   "℃ 인지 ℉ 인지"는 장치 설정에 달렸고, 그것을 모르면 계수를 고를 수 없다
4. `<이름>.json` · `<이름>.h` · `<이름>_pd.py` 를 낸다

**환산 조건을 실제로 읽는 것이 요점이다.** 안 읽으면 `scale_for()` 가 후보를 못
좁혀 None 을 주고, 응용은 283 을 받아 그것이 28.3 ℃ 인 줄 모른다.

    python3 -m edgeconfig.commission line_a --cycle-us 10000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codegen, image as img_mod, iodd as iodd_mod

MODE_NAME = {0: "DEACTIVATED", 1: "IOL_MANUAL", 2: "IOL_AUTOSTART",
             3: "DI_CQ", 4: "DO_CQ"}


def _needed_indices(iodd) -> set:
    """환산을 고르는 데 필요한 디바이스 파라미터 번호."""
    out: set = set()
    for layout in (iodd.pd_in, iodd.pd_out):
        if layout is None:
            continue
        for it in layout.items:
            for sc in it.scales:
                for idx, _v in sc.conds:
                    out.add(idx)
    return out


def _read_settings(bus, node: int, port: int, want: set, say) -> dict:
    """조건이 가리키는 파라미터를 ISDU 로 읽는다. 못 읽은 것은 넣지 않는다 —
    **모르는 것을 0 으로 채우면 엉뚱한 계수가 확실히 골라진다.**"""
    got = {}
    for idx in sorted(want):
        try:
            data = bus.iol_device_read(node, port, idx)
        except Exception as e:                       # noqa: BLE001
            say(f"      파라미터 {idx} 를 못 읽음 ({e}) — 환산 후보를 못 좁힙니다")
            continue
        if not data:
            continue
        got[idx] = int.from_bytes(data, "big")
        say(f"      파라미터 {idx} = {got[idx]}")
    return got


def commission(name: str, cycle_us: int = 0, iodd_dir: str | None = None,
               config_path: str | None = None, say=print) -> dict:
    """버스를 읽어 설정과 코드를 낸다. 낸 파일 경로를 담은 dict 를 돌려준다."""
    from edgelib import EdgeBus, State           # 여기서만 필요하다

    folder = iodd_dir or str(Path(__file__).parent / "iodd")
    catalog = iodd_mod.index(folder)
    say(f"IODD {len(catalog)} 종 색인 ({folder})")

    say(f"버스 열기 ({config_path or '자동 구성'})")
    with EdgeBus(config_path) as bus:
        bus.setmode(State.PREOP)                 # 포트는 PREOP 이라야 돈다

        specs, settings = [], {}
        for n in bus.nodes():
            say(f"  노드 {n.address}  {n.type_name}  {n.serial}")
            ports = []
            for p in n.ports:
                mode = MODE_NAME.get(p.mode, "DEACTIVATED")
                if p.pd_in == 0 and p.pd_out == 0:
                    ports.append(img_mod.PortSpec(port=p.port, mode=mode))
                    continue

                d = catalog.get((p.vendor_id, p.device_id))
                say(f"    포트 {p.port}  PD {p.pd_in}/{p.pd_out}  "
                    f"VID {p.vendor_id} DID {p.device_id}  "
                    f"IODD {'있음' if d else '없음'}")

                if d is not None:
                    want = _needed_indices(d)
                    if want:
                        settings[(n.address, p.port)] = _read_settings(
                            bus, n.address, p.port, want, say)

                ports.append(img_mod.PortSpec(
                    port=p.port, mode=mode, pd_in=p.pd_in, pd_out=p.pd_out,
                    iodd=d, product=(d.product if d else ""),
                    vendor_id=p.vendor_id, device_id=p.device_id))

            specs.append(img_mod.NodeSpec(
                node=n.address, type_name=n.type_name, category=n.category,
                model=n.model, variant=n.variant, hw_rev=n.hw_rev,
                serial=n.serial, ports=ports))

        bus.setmode(State.STARTUP)

    img = img_mod.build(specs)
    cmin = img_mod.cycle_min_us(specs)
    # **기본은 계산 하한의 두 배다.** 하한은 프레임이 딱 들어가는 시간이라, 그대로
    # 쓰면 한 번 튀는 것을 흡수할 여유가 없다.
    us = cycle_us or max(2 * cmin, 1000)
    cfg = img_mod.to_config(specs, img, us, cmin, settings=settings)

    out = {}
    out["json"] = f"{name}.json"
    Path(out["json"]).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    out["h"] = f"{name}.h"
    Path(out["h"]).write_text(
        codegen.emit_c(cfg, guard=f"{name.upper()}_H"), encoding="utf-8")

    out["py"] = f"{name}_pd.py"
    Path(out["py"]).write_text(codegen.emit_py(cfg), encoding="utf-8")

    say(f"\n이미지 in {img.in_bytes} B / out {img.out_bytes} B")
    say(f"주기 {us} us  (계산 하한 {cmin} us, 워치독 {us * 3} us)")
    say("낸 것: " + " · ".join(out.values()))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="버스를 읽어 설정 파일과 응용 코드를 낸다")
    ap.add_argument("name", help="낼 파일의 이름 (확장자 없이)")
    ap.add_argument("--cycle-us", type=int, default=0,
                    help="주기. 0 이면 계산 하한의 2배")
    ap.add_argument("--iodd-dir", default=None)
    ap.add_argument("--config", default=None,
                    help="기존 설정으로 열기. 없으면 자동 구성")
    a = ap.parse_args(argv)
    try:
        commission(a.name, a.cycle_us, a.iodd_dir, a.config)
    except Exception as e:                           # noqa: BLE001
        print(f"실패: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
