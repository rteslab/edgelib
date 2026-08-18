"""EdgeX — 네트워크 전체의 설정과 산출물

**여기가 버스 전체를 정하는 자리다.** 사이클 시간과 내보낼 설정 파일은 어느 한
노드에 속하지 않는다. 그래서 트리에서 `Controller` 를 고를 때만 나온다.

**프로세스 데이터도 여기 있다.** 위에서 정한 주기로 **모든 노드를 한꺼번에**
돌려 보는 자리다 — 그 주기로 실제 통신이 버티는지가 여기서 드러난다. 놓친
사이클을 세어 보여주므로, 손실이 나면 주기를 올리면 된다.

내보내는 JSON 이 이 툴의 **최종 산출물**이다. 라이브러리가 이것만 읽고 버스를
잡을 수 있어야 하므로, 배치뿐 아니라 **노드 신원**과 **포트 모드**와 **사이클
시간**까지 담는다 — 근거는 `image.to_config()` 머리말.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import codegen
from .. import example
from .. import image as img_mod
from .. import session as session_mod
from ..paths import examples_root
from .imagepanel import ImagePanel


class MasterPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self._min = 0

        # ── 버스 ────────────────────────────────────────────────────────────
        top = ttk.Frame(self)
        top.pack(fill="x")
        # 상자는 **설정하는 것만** 감싼다 — 내보내기 버튼까지 넣으면 그것도
        # 버스 설정의 일부처럼 보인다.
        box = ttk.LabelFrame(top, text="Bus", padding=8)
        box.pack(side="left")

        # **하한은 계산해서 주고, 실제 주기는 사람이 정한다.** 계산으로 나오는
        # 것은 아무것도 늦지 않았을 때의 값이라 그대로 쓰면 위태롭다 — 기본을
        # 두 배로 잡아 둔다.
        ttk.Label(box, text="Minimum").grid(row=0, column=0, sticky="w")
        self.lbl_min = ttk.Label(box, text="-", width=8, anchor="e",
                                 font=("monospace", 9))
        self.lbl_min.grid(row=0, column=1, padx=(8, 2), sticky="w")
        self.lbl_min_why = ttk.Label(box, text="ms", foreground="#777")
        self.lbl_min_why.grid(row=0, column=2, sticky="w")

        ttk.Label(box, text="Cycle time",
                  font=("sans", 9, "bold")).grid(row=1, column=0, sticky="w",
                                                 pady=(6, 0))
        self.var_cycle = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.var_cycle, width=8).grid(
            row=1, column=1, padx=(8, 2), sticky="w", pady=(6, 0))
        self.lbl_cycle_why = ttk.Label(box, text="ms", foreground="#555")
        self.lbl_cycle_why.grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.var_cycle.trace_add("write", lambda *_: self._show_cycle())

        # **버스 전체를 하나로 내보낸다.** 이 툴의 산출물이라 눈에 띄어야 한다.
        right = ttk.Frame(top)
        right.pack(side="right", padx=(16, 4))
        try:
            ttk.Style().configure("Export.TButton", padding=(16, 10),
                                  font=("sans", 10, "bold"))
        except tk.TclError:
            pass
        ttk.Button(right, text="Export configuration", style="Export.TButton",
                   command=self._export).pack(anchor="e", fill="x")
        # **예제는 설정과 따로 낸다.** 설정은 설비가 바뀔 때마다 다시 내지만
        # 예제는 한 번 받아 자기 코드로 갈아타는 것이라, 같은 버튼에 묶으면
        # 이미 고쳐 쓰던 파일을 말없이 덮어쓰게 된다.
        ttk.Button(right, text="Generate example code", style="Export.TButton",
                   command=self._gen_example).pack(anchor="e", fill="x",
                                                   pady=(6, 0))

        # ── 노드 ────────────────────────────────────────────────────────────
        # **높이를 고정한다.** 이것도 늘리면 아래 프로세스 이미지가 몇 픽셀로
        # 눌려 검은 띠만 남는다 — 표는 눌려도 헤더 테두리를 그리기 때문이다.
        nf = ttk.LabelFrame(self, text="Nodes", padding=6)
        nf.pack(fill="x", pady=(8, 0))

        # **노드와 포트를 함께 세운다.** 하나를 고르면 아래 프로세스 데이터가
        # 그것만 보여준다 — 노드가 여럿이면 한 화면에 다 띄워 봐야 읽히지 않는다.
        # **평평하게 둔다.** 줄마다 "ID 1 / Port 1" 로 어디인지 다 적히므로
        # 계층이 없어도 읽히고, PD 가 없는 포트는 아예 빼서 짧게 유지한다.
        cols = ("what", "status", "pdin", "pdout", "uid")
        self.tv = ttk.Treeview(nf, columns=cols, show="tree headings", height=3)
        self.tv.heading("#0", text="ID / Port")
        self.tv.column("#0", width=130, anchor="w")
        for c, t, w in (("what", "Module / Device", 210),
                        ("status", "Status", 110),
                        ("pdin", "PD in", 55), ("pdout", "PD out", 55),
                        ("uid", "Value (hex)", 250)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w")
        self.tv.pack(fill="x")
        self.tv.bind("<<TreeviewSelect>>", self._on_pick)


        # 위에서 정한 주기로 모든 노드를 돌린다. 손실이 나면 주기가 짧은 것이다.
        pif = ttk.LabelFrame(self, text="Process data (RUN)", padding=6)
        pif.pack(fill="both", expand=True, pady=(8, 0))
        self.image = ImagePanel(pif, self.app, title=pif)
        self.image.pack(fill="both", expand=True)

    # ── 값 ──────────────────────────────────────────────────────────────────
    def update_pd(self, snap, node: int) -> None:
        self._show_values(snap, node)
        self.image.update_pd(snap, node)

    def clear_values(self) -> None:
        self.image.clear_values()

    # ── 시간 ────────────────────────────────────────────────────────────────
    def _cycle_us(self) -> int:
        try:
            return max(0, int(round(float(self.var_cycle.get()) * 1000)))
        except ValueError:
            return -1

    def _show_cycle(self) -> None:
        us = self._cycle_us()
        if us <= 0:
            self.lbl_cycle_why.configure(text="ms", foreground="#555")
            return
        if us < self._min:
            self.lbl_cycle_why.configure(
                text=f"ms   below the minimum {self._min / 1000:.1f} ms",
                foreground="#C0522E")
            return
        self.lbl_cycle_why.configure(
            text=f"ms   watchdog {3 * us / 1000:.1f} ms", foreground="#555")

    def _show_times(self, specs) -> None:
        self._min = img_mod.cycle_min_us(specs) if specs else 0
        self.lbl_min.configure(text=f"{self._min / 1000:.1f}")
        self.lbl_min_why.configure(
            text="ms   process data + one async transfer   (theoretical floor)")
        # **기본값은 하한과 무관하게 100 ms 다.**
        #
        # 하한은 전선과 턴어라운드가 정하는 값이라 C 로 돌 때의 이야기고, 이 툴은
        # 파이썬으로 돈다. 실측에서 100 ms 는 손실 0 % 였지만 6 ms 이하는 RUN 진입
        # 자체가 되지 않았다 — 파이썬이 못 따라가는 것으로 보이며, 실제 한계는
        # edgelib(C)으로 옮긴 뒤에 다시 본다.
        if not self.var_cycle.get().strip():
            self.var_cycle.set(f"{img_mod.CYCLE_DEFAULT_US / 1000:.0f}")
        self._show_cycle()

    def confirm_cycle(self) -> float | None:
        """RUN 직전에 부른다. 초 단위 주기를 주고, 그만두면 None.

        **하한보다 짧으면 물어본다.** 하한은 아무것도 늦지 않았을 때의 값이라
        그 아래로 내리면 놓치는 사이클이 생긴다 — 막지는 않는다. 얼마나 놓치는지
        보는 것이 이 화면의 쓸모이기 때문이다.
        """
        us = self._cycle_us()
        if us <= 0:
            messagebox.showerror("Cycle time", "Enter a cycle time first")
            return None
        if us < self._min:
            if not messagebox.askokcancel(
                    "Below the minimum",
                    f"Cycle time {us / 1000:.1f} ms is below the "
                    f"recommended minimum {self._min / 1000:.1f} ms.\n\n"
                    "Frames may be lost. Run anyway?"):
                return None
        return us / 1e6

    # ── 화면 ────────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        keep = self.tv.selection()
        self.tv.delete(*self.tv.get_children())
        specs = self._specs()
        for n in specs:
            # 노드 줄은 두지 않는다 — 고를 수 있는 것은 실제로 데이터가 오가는
            # 포트뿐이고, 노드 요약은 위 카드와 트리가 이미 보여준다.
            live = self._node_ports(n.node)
            for p in sorted(n.ports, key=lambda x: x.port):
                if not p.pd_in and not p.pd_out:
                    continue                    # 내보낼 것도 받을 것도 없다
                d = self.app.port_panel.iodds.get((n.node, p.port))
                self.tv.insert(
                    "", "end", iid=f"n{n.node}p{p.port}",
                    text=f"ID {n.node} / Port {p.port}",
                    values=((d.product if d else None) or p.product
                            or (f"VID {p.vendor_id} DID {p.device_id}"
                                if p.vendor_id else "-"),
                            live.get(p.port) or p.mode, p.pd_in, p.pd_out, ""))
        for iid in keep:
            if self.tv.exists(iid):
                self.tv.selection_set(iid)

        self._show_times(specs)
        self._on_pick()

    def _show_values(self, snap, node: int) -> None:
        """포트별 PD 를 16진수 그대로. **해석 전의 원본**이라, 표에서 값이
        이상해 보일 때 어디까지가 사실인지 여기서 갈린다."""
        for iid in self.tv.get_children():
            if "p" not in iid[1:]:
                continue
            n, p = iid[1:].split("p")
            if int(n) != node:
                continue
            data = snap.ports.get(int(p))
            self.tv.set(iid, "uid", data.hex(" ") if data else "")

    def _node_ports(self, addr: int) -> dict:
        n = next((x for x in self.app.nodes if x.address == addr), None)
        return {p.port: p.status for p in n.ports} if n else {}

    def _on_pick(self, _e=None) -> None:
        """고른 것만 아래에 보여준다. 아무것도 안 골랐으면 전부."""
        sel = self.tv.selection()
        if not sel:
            self.image.refresh()
            return
        iid = sel[0]
        if "p" in iid[1:]:
            node, port = iid[1:].split("p")
            self.image.refresh(only=int(node), only_port=int(port))
        else:
            self.image.refresh(only=int(iid[1:]))

    # ── 산출물 ──────────────────────────────────────────────────────────────
    def _specs(self) -> list:
        out = []
        for n in self.app.nodes:
            spec = img_mod.NodeSpec(
                node=n.address, type_name=n.type_name,
                category=n.category, model=n.model, variant=n.variant,
                hw_rev=n.hw_rev, serial=n.serial.hex().upper(),
                addr_mode=n.addr_mode)
            for p in n.ports:
                d = self.app.port_panel.iodds.get((n.address, p.port))
                spec.ports.append(img_mod.PortSpec(
                    port=p.port, mode=p.mode, pd_in=p.pd_in, pd_out=p.pd_out,
                    iodd=d, product=d.product if d else p.product,
                    vendor_id=p.vendor_id, device_id=p.device_id))
            out.append(spec)
        return out

    # ── 산출물 ──────────────────────────────────────────────────────────
    def _out_dir(self) -> Path:
        """Export 대화상자가 처음 여는 자리. 없으면 만든다.

        예제 쪽은 이 자리를 쓰지 않는다 — 낼 때마다 `examples/<날짜_시각>/` 을
        새로 만들어 거기 넣는다.
        """
        prev = getattr(self, "_last_stem", None)
        if prev is None:
            return examples_root()          # 배포본의 examples/ (paths.py)
        d = prev.parent
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 만들 수 없는 자리면 대화상자가 알아서 홈으로 연다
            return Path.cwd()
        return d

    def _config_now(self):
        """지금 화면 상태로 설정을 만든다. 못 만들면 None."""
        if not self.app.nodes:
            messagebox.showinfo("Nothing to export", "Scan for modules first")
            return None
        us = self._cycle_us()
        if us <= 0:
            messagebox.showerror("Input error", "Cycle time must be a number")
            return None
        # RUN 과 같은 판정을 쓴다 (`app.no_iodd_ports`) — 두 곳에 두면 어긋난다
        if self.app.warn_no_iodd("Export"):
            return None
        specs = self._specs()
        img = img_mod.build(specs)
        return img_mod.to_config(specs, img, us, img_mod.cycle_min_us(specs),
                                 settings=self.app.dev_values), img

    def _emit(self, stem: Path, cfg: dict) -> list:
        """설정과 형을 한 자리에 낸다 (Export configuration).

        **한 자리에서 다 낸다.** 파일 이름이 서로를 부르기 때문이다 — 예제는
        `<이름>_pd.py` 를 import 하고 `<이름>.json` 을 읽는다. 버튼마다 따로
        이름을 지으면 언젠가 어긋나고, 어긋나면 고객은 import 첫 줄에서 막힌다.
        """
        base = stem.name
        guard = f"{base.upper().replace(chr(45), chr(95))}_H"

        (stem.parent / f"{base}.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        (stem.parent / f"{base}.h").write_text(
            codegen.emit_c(cfg, guard=guard), encoding="utf-8")
        (stem.parent / f"{base}_pd.py").write_text(
            codegen.emit_py(cfg), encoding="utf-8")

        self._last_stem = stem
        return [f"{base}.json", f"{base}.h", f"{base}_pd.py"]

    def _emit_examples(self, folder: Path, base: str, cfg: dict) -> list:
        """언어별로 폴더를 나눠 낸다.

        **설정 파일을 양쪽에 복사한다.** 위에 하나 두고 `../` 로 부르게 하면
        폴더 하나만 떼어 간 고객이 못 돌린다. 생성물이라 중복이 문제될 것도 없다 —
        둘 다 같은 자리에서 같은 `cfg` 로 나온다.
        """
        cdir, pdir = folder / "c", folder / "python"
        cdir.mkdir(parents=True, exist_ok=True)
        pdir.mkdir(parents=True, exist_ok=True)

        text = json.dumps(cfg, ensure_ascii=False, indent=2)
        guard = f"{base.upper().replace(chr(45), chr(95))}_H"

        (cdir / f"{base}.json").write_text(text, encoding="utf-8")
        (cdir / f"{base}.h").write_text(
            codegen.emit_c(cfg, guard=guard), encoding="utf-8")
        (cdir / f"{base}_example.c").write_text(
            example.emit_c(cfg, base), encoding="utf-8")
        (cdir / "Makefile").write_text(
            example.emit_makefile(base), encoding="utf-8")

        (pdir / f"{base}.json").write_text(text, encoding="utf-8")
        (pdir / f"{base}_pd.py").write_text(
            codegen.emit_py(cfg), encoding="utf-8")
        (pdir / f"{base}_example.py").write_text(
            example.emit(cfg, base), encoding="utf-8")

        return [f"c/{base}.json", f"c/{base}.h", f"c/{base}_example.c",
                "c/Makefile",
                f"python/{base}.json", f"python/{base}_pd.py",
                f"python/{base}_example.py"]

    def _export(self) -> None:
        made = self._config_now()
        if made is None:
            return
        cfg, img = made

        path = filedialog.asksaveasfilename(
            title="Save configuration", defaultextension=".json",
            initialdir=str(self._out_dir()), initialfile="line_a.json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return

        # **설정과 형은 언제나 같이 낸다.** 따로 내게 두면 언젠가 한쪽만
        # 갱신되고, 그때 비트 위치가 조용히 어긋난다 — 값이 틀렸다는 것을
        # 아무도 모른다. 예제만 빼는 이유는 그것이 고객이 고쳐 쓰는 파일이라
        # 말없이 덮어쓰면 안 되기 때문이다.
        files = self._emit(Path(path).with_suffix(""), cfg)

        self.app._log(f"exported {path}  "
                      f"(in {img.in_bytes} B / out {img.out_bytes} B)")
        messagebox.showinfo(
            "Saved", f"{Path(path).parent}\n\n" + "\n".join(files))

    def _gen_example(self) -> None:
        """돌아가는 예제 한 벌을 **새 폴더에** 낸다.

        낼 때마다 `examples/<날짜_시각>/` 을 새로 만든다. 덮어쓰기를 묻지 않는
        대신 앞에 낸 것이 그대로 남으므로, 설정을 바꿔 가며 여러 번 뽑아 놓고
        비교할 수 있다 — 커미셔닝은 원래 그렇게 진행된다.

        네 파일을 한 자리에 같이 낸다. 예제만 있으면 돌지 않기 때문이다 —
        `<이름>.json` 을 읽고 `<이름>_pd.py` 를 import 한다.
        """
        made = self._config_now()
        if made is None:
            return
        cfg, _img = made

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = examples_root() / stamp
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Cannot write", f"{folder}\n\n{e}")
            return

        base = "edgex"
        files = self._emit_examples(folder, base, cfg)

        self.app._log(f"generated {folder}")
        messagebox.showinfo(
            "Saved",
            f"{folder}\n\n" + "\n".join(files)
            + "\n\nEach folder runs on its own."
            + f"\n\n    cd {folder}/python && python3 {base}_example.py 30"
            + f"\n    cd {folder}/c && make && ./{base}_example 30")


_MODE = {1: "single shot", 2: "disappeared", 3: "active"}
_TYPE = {1: "notify", 2: "warning", 3: "error"}


def _detail(e: dict) -> str:
    """코드 대역이 출처를 말한다 (클래스 §4 · 규격 D.1/D.2)."""
    c = e["code"]
    if c >= 0x1800:
        src = "port"
    elif c >= 0x1000:
        src = "device"
    else:
        src = "backplane"
    v = f"   value {e['value']}" if e["value"] else ""
    return f"{src}   t+{e['ts_ms'] / 1000:.1f} s{v}"
