"""포트 설정 · ISDU 파라미터 · 프로세스 데이터

하는 일은 하나다 — **장치의 값을 읽고, 원하는 값으로 쓴다.**

IODD 가 있으면 각 파라미터의 **넣을 수 있는 값**과 **공장 초기값**을 같이 보여준다.
없으면 빈 칸으로 둔다. 규격이 정한 범위와 이 장치가 받는 범위는 다르므로 **형만 보고
지어내지 않는다.**

읽기 전용은 보여만 주고, 쓰기 가능하고 연결돼 있을 때만 편집칸을 연다 — 못 쓰는 칸을
열어 두면 사용자가 눌러 보고 나서야 안 된다는 걸 안다.
"""

from __future__ import annotations

import shutil
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from .. import iodd as iodd_mod
from .. import session as session_mod
from ..catalog import Node
from ..discover import PSI_NAME
from ..session import SessionError

PORT_MODES = ["DEACTIVATED", "IOL_AUTOSTART", "IOL_MANUAL", "DI_CQ", "DO_CQ"]
_MODE_VAL = {"DEACTIVATED": 0, "IOL_MANUAL": 1, "IOL_AUTOSTART": 2,
             "DI_CQ": 3, "DO_CQ": 4}

IODD_DIR = Path(__file__).resolve().parent.parent / "iodd"


class PortPanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.node: Node | None = None
        self.port: int | None = None
        self.online = False

        self.iodds: dict[tuple[int, int], iodd_mod.Iodd] = {}   # (node, port) → IODD
        self._param_rows: list[tuple] = []

        self._build()

    # ── 화면 ────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        top = ttk.LabelFrame(self, text="Port setup", padding=8)
        top.pack(fill="x")

        # 두 줄이 같은 격자를 쓴다 — 라벨·입력칸·버튼·설명이 세로로 맞아떨어진다.
        # 안쪽에 프레임을 하나 더 두고 pack 하면 열이 어긋난다.
        top.columnconfigure(1, minsize=280)
        top.columnconfigure(3, weight=1)

        ttk.Label(top, text="Mode").grid(row=0, column=0, sticky="w")
        self.var_mode = tk.StringVar(value="DEACTIVATED")
        self.cmb_mode = ttk.Combobox(top, textvariable=self.var_mode,
                                     values=PORT_MODES, state="readonly")
        self.cmb_mode.grid(row=0, column=1, padx=8, sticky="we")
        self.cmb_mode.bind("<<ComboboxSelected>>", self._on_mode)

        self.btn_mode_write = ttk.Button(top, text="Apply port mode", state="disabled",
                                         command=self._write_mode)
        self.btn_mode_write.grid(row=0, column=2, sticky="we")

        self.lbl_status = ttk.Label(top, text="", foreground="#555", anchor="w")
        self.lbl_status.grid(row=0, column=3, padx=12, sticky="we")

        # IODD — IO-Link 모드일 때만 뜻이 있다
        self.lbl_iodd_cap = ttk.Label(top, text="IODD")
        self.lbl_iodd_cap.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_iodd = tk.StringVar(value="")
        # **파일 이름만 보여준다.** 경로는 폴더가 정해져 있어 늘 같고, 길어서
        # 정작 봐야 할 제품명을 밀어낸다.
        self.ent_iodd = ttk.Entry(top, textvariable=self.var_iodd, state="readonly")
        self.ent_iodd.grid(row=1, column=1, padx=8, sticky="we", pady=(8, 0))
        self.btn_iodd = ttk.Button(top, text="Browse...", command=self._pick_iodd)
        self.btn_iodd.grid(row=1, column=2, sticky="we", pady=(8, 0))
        self.lbl_iodd = ttk.Label(top, text="", foreground="#555", anchor="w")
        self.lbl_iodd.grid(row=1, column=3, padx=12, sticky="we", pady=(8, 0))


        # 링크가 지금 어떤 조건으로 도는가 — 전송률·리비전·사이클·PD 유효성.
        # **ISDU 가 아니라 포트 상태다.** ISDU 표에 섞으면 어떻게 얻은 값인지가
        # 흐려지고, 별도 상자를 만들면 세로가 모자라 아래가 밀린다.
        ttk.Label(top, text="Link").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.lbl_link = ttk.Label(top, text="-", foreground="#555", anchor="w")
        self.lbl_link.grid(row=2, column=1, columnspan=3, padx=8, sticky="we",
                           pady=(8, 0))

        # ── 파라미터 ────────────────────────────────────────────────────────
        pf = ttk.LabelFrame(self, text="ISDU parameters", padding=6)
        pf.pack(fill="both", expand=True, pady=(8, 0))

        bar = ttk.Frame(pf)
        bar.pack(fill="x", pady=(0, 4))
        self.btn_read_all = ttk.Button(bar, text="Read all", state="disabled",
                                       command=self._read_all)
        self.btn_read_all.pack(side="left")
        # 지금 장치에 있는 값을 마스터의 Data Storage 에 올려 둔다 — 장치를 갈면
        # 마스터가 되돌려 준다. 자리만 잡아 두었다.
        self.btn_ds = ttk.Button(bar, text="Store to DS", state="disabled",
                                 command=self._store_ds)
        self.btn_ds.pack(side="left", padx=6)
        self.lbl_pcount = ttk.Label(bar, text="", foreground="#777")
        self.lbl_pcount.pack(side="left", padx=10)

        cols = ("index", "access", "type", "name", "range", "default", "device")
        self.tv = ttk.Treeview(pf, columns=cols, show="headings", height=8)
        for c, t, w in (("index", "Index", 70), ("access", "Access", 55),
                        ("type", "Type", 95), ("name", "Name", 200),
                        ("range", "Range / choices", 210),
                        ("default", "Default", 80), ("device", "Device", 100)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w")
        # **힌트줄을 표보다 먼저 붙인다.** 표를 `side="left"` 로 먼저 붙이면 남는
        # 자리가 오른쪽 구석 조각뿐이라 아래 줄이 화면 밖으로 밀려난다.
        self.lbl_phint = ttk.Label(pf, text="", foreground="#777", anchor="w")
        self.lbl_phint.pack(side="bottom", fill="x", pady=(4, 0))

        sb = ttk.Scrollbar(pf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tv.bind("<<TreeviewSelect>>", self._on_param)

        # 고칠 값이 보이는 자리에서 고친다 — 값이 한 군데에만 있으니 어디를
        # 바꾸는 중인지 헷갈릴 데가 없다
        self.tv.bind("<Double-1>", self._begin_edit)
        self.tv.bind("<Return>", self._begin_edit)
        self._editor = None
        self._edit_iid = None
        self._read_gen = 0
        # 흐린 줄 = IODD 가 넣을 값을 안 알려 준 것. 손대지 말라는 표시다
        self.tv.tag_configure("unknown", foreground="#999")



    # ── 외부에서 부르는 것 ──────────────────────────────────────────────────
    def show(self, node: Node | None, port: int | None) -> None:
        self._cancel_edit()
        self.node, self.port = node, port
        if node is None or port is None:
            self.cmb_mode.configure(state="disabled")
            self.lbl_status.configure(text="Select a port")
            self._fill_params(None)
            return

        self.cmb_mode.configure(state="readonly")
        p = next((x for x in node.ports if x.port == port), None)
        if p is not None:
            self.var_mode.set(p.mode)
            # PD 길이는 Link 줄이 살아 있는 값으로 보여준다 — 여기 또 적지 않는다.
            # VID/DID 는 남긴다: **장치가 말한 신원**이고, 바로 아래 IODD 줄의
            # 신원과 나란히 놓여야 물린 파일이 맞는지 눈으로 대조된다.
            self.lbl_status.configure(
                text=f"{p.status}"
                     + (f"   VID {p.vendor_id} DID {p.device_id}"
                        if p.vendor_id else ""))

        key = (node.address, port)
        d = self.iodds.get(key)
        if d is None and p is not None:
            d = self._match_iodd(p)
        self.var_iodd.set(Path(d.path).name if d else "")
        self.lbl_iodd.configure(text=d.summary if d else self._no_iodd_hint(p))
        self._fill_params(d)
        self._on_mode()
        self.refresh_link()
        # 포트를 고른 것이 곧 "이 포트를 보겠다"는 뜻이다. 연결돼 있으면 바로 읽는다 —
        # Connect 를 먼저 하든 포트를 먼저 고르든 결과가 같아야 한다.
        self.auto_read()

    def refresh_link(self) -> None:
        """PortStatusList 를 한 번 읽어 링크 조건을 보여준다."""
        s = self.app.session
        if s is None or self.port is None:
            self.lbl_link.configure(text="connect to read link status")
            return
        try:
            st = s.port_status(self.port)
        except SessionError:
            st = None
        if st is None:
            self.lbl_link.configure(text="-")
            return

        rev = st["revision_id"]
        us = session_mod.cycletime_us(st["cycletime"])
        bits = [
            session_mod.COM_NAME.get(st["rate"], f"rate {st['rate']}"),
            f"rev {rev >> 4}.{rev & 0x0F}",
            # Table E.4 옥텟 6 은 MasterCycleTime 이다. 다만 이 툴은 포트를 늘
            # FreeRunning(PortCycleTime 0)으로 설정하고, 그때 SM 은
            # `time = cfg ? cfg : mincyc` 로 **장치의 MinCycleTime** 을 넣는다
            # (`iol_sm` buildValueList). 고정 주기를 설정하게 되면 이 값은
            # 그 설정값이 되므로 이름도 같이 고쳐야 한다.
            f"Min. CycleTime {us / 1000:.1f} ms",
            f"PD in {st['pd_in']} B / out {st['pd_out']} B",
            # PQI 는 0 이 유효다 — 비트가 서면 그 PD 를 쓰면 안 된다
            "PD invalid" if st["quality"] & 0x01 else "PD valid",
        ]
        if st["pd_out"]:
            bits.append("PDout invalid" if st["quality"] & 0x02 else "PDout valid")
        # 진단 코드는 여기 두지 않는다 — 이벤트 칸이 이벤트를 다루는 자리다
        self.lbl_link.configure(text="   ".join(bits))

    def set_online(self, on: bool) -> None:
        self.online = on
        st = "normal" if on else "disabled"
        self.btn_mode_write.configure(state=st)
        self.btn_read_all.configure(state=st)
        self.btn_ds.configure(state=st)
        self._on_param()
        self.refresh_link()

    # ── IODD ────────────────────────────────────────────────────────────────
    def _match_iodd(self, p):
        """꽂힌 장치의 VID/DID 와 같은 IODD 를 폴더에서 찾아 물린다.

        **장치가 자기 신원을 말해 주는데 사람에게 파일을 고르라고 할 이유가 없다.**
        AUTOSTART 로 붙으면 VID/DID 가 그대로 오므로 그것으로 찾는다.
        """
        if not p.vendor_id or self.node is None:
            return None
        d = iodd_mod.index(IODD_DIR).get((p.vendor_id, p.device_id))
        if d is None:
            return None
        self.iodds[(self.node.address, p.port)] = d
        self.app._log(f"port {p.port}: matched IODD {Path(d.path).name}")
        return d

    def _no_iodd_hint(self, p) -> str:
        """왜 없는지까지 말한다 — "없다"만으로는 다음에 뭘 할지 알 수 없다."""
        if p is None or not p.vendor_id:
            return "No device on this port"
        return (f"No IODD for VID {p.vendor_id} DID {p.device_id} in the folder"
                f" - use Browse... to add one")

    def _pick_iodd(self) -> None:
        if self.node is None or self.port is None:
            return
        IODD_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.askopenfilename(
            title="IODD file", initialdir=str(IODD_DIR),
            filetypes=[("IODD", "*.xml *.zip"), ("All files", "*.*")])
        if not path:
            return
        try:
            d = iodd_mod.load(path)
        except iodd_mod.IoddError as e:
            messagebox.showerror("IODD error", str(e))
            return

        # **신원을 대조한다.** 다른 디바이스의 IODD 를 물리면 PD 해석이 통째로
        # 틀리는데, 값이 그럴듯해 보여 알아채기 어렵다.
        p = next((x for x in self.node.ports if x.port == self.port), None)
        if p is not None and p.vendor_id and (
                d.vendor_id != p.vendor_id or d.device_id != p.device_id):
            if not messagebox.askyesno(
                    "Identity mismatch",
                    f"IODD    VID {d.vendor_id} DID {d.device_id}\n"
                    f"Device  VID {p.vendor_id} DID {p.device_id}\n\n"
                    "This IODD is for a different device. Use it anyway?"):
                return

        # **폴더에 들여 놓는다.** 그래야 다음부터 VID/DID 로 저절로 물린다 — 한 번
        # 고른 파일을 매번 다시 찾아 오게 하지 않는다.
        src = Path(path)
        if src.parent.resolve() != IODD_DIR.resolve():
            try:
                shutil.copy2(src, IODD_DIR / src.name)
                d = iodd_mod.load_cached(IODD_DIR / src.name)
                self.app._log(f"copied {src.name} into the IODD folder")
            except OSError as e:
                self.app._log(f"could not copy into the IODD folder: {e}")

        self.iodds[(self.node.address, self.port)] = d
        self.var_iodd.set(Path(d.path).name)
        self.lbl_iodd.configure(text=d.summary)

        self._fill_params(d)
        self.app.image_panel.refresh()
        self.auto_read()        # 목록이 생겼으니 바로 채운다

    def _fill_params(self, d) -> None:
        self.tv.delete(*self.tv.get_children())
        self._param_rows.clear()
        if d is None:
            self.lbl_pcount.configure(text="")
            return

        for p in d.params:
            iid = self.tv.insert("", "end", values=(
                f"{p.index}.{p.subindex}" if p.subindex else str(p.index),
                p.access, f"{p.dtype} {p.bit_length}b" if p.dtype else "",
                p.name, p.choices, p.default, ""),
                tags=() if (p.values or p.low is not None) else ("unknown",))
            self._param_rows.append((iid, p))

        w = sum(1 for p in d.params if p.writable)
        self.lbl_pcount.configure(text=f"{len(d.params)} params - {w} writable")

    def _param(self):
        sel = self.tv.selection()
        if not sel:
            return None
        return next((p for iid, p in self._param_rows if iid == sel[0]), None)

    def _on_param(self, _e=None) -> None:
        # **지금 고치고 있는 줄이면 두고 간다.** `selection_set()` 이 큐에 넣은
        # `<<TreeviewSelect>>` 는 편집기를 만든 *뒤에* 도착하기 때문에, 무조건
        # 지우면 더블클릭이 아무 일도 안 하는 것처럼 보인다.
        if self._edit_iid is not None and self._edit_iid not in self.tv.selection():
            self._cancel_edit()
        p = self._param()
        if p is None:
            self.lbl_phint.configure(text="")
            return

        hint = []
        if not p.writable:
            hint.append("read-only")
        elif not self.online:
            hint.append("connect to edit")
        else:
            hint.append("double-click the Device cell to edit")
        if p.choices:
            hint.append(p.choices)
        if p.default:
            hint.append(f"default {p.default}")
        self.lbl_phint.configure(text="     ".join(hint))

    # ── 표 안에서 고치기 ────────────────────────────────────────────────────
    def _cancel_edit(self, _e=None) -> None:
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
            self._edit_iid = None

    def _begin_edit(self, event=None) -> str | None:
        """쓰기 가능한 줄의 Device 칸에 편집기를 띄운다.

        열거형은 **콤보박스**로 준다. `0=MPa · 1=kPa · 2=kgf/cm2 · 3=bar` 를 보고
        `3` 을 외워 치게 할 이유가 없고, 오타로 엉뚱한 값이 나가지도 않는다.
        """
        self._cancel_edit()

        iid = (self.tv.identify_row(event.y)
               if event is not None and getattr(event, "y", None) is not None
               else None)
        if not iid:
            sel = self.tv.selection()
            iid = sel[0] if sel else None
        if not iid:
            return "break"

        self.tv.selection_set(iid)
        p = next((q for i, q in self._param_rows if i == iid), None)
        if p is None or not p.writable or not self.online:
            return "break"

        box = self.tv.bbox(iid, "device")
        if not box:                       # 스크롤 밖이면 자리를 잡을 수 없다
            return "break"
        x, y, w, h = box

        cur = self.tv.set(iid, "device")
        cur = cur.split(" (")[0] if cur and cur != "-" else ""

        if p.values:
            ed = ttk.Combobox(self.tv, state="readonly",
                              values=[f"{v.value} = {v.name}" for v in p.values])
            pick = next((f"{v.value} = {v.name}" for v in p.values
                         if str(v.value) == cur), "")
            ed.set(pick)
            ed.bind("<<ComboboxSelected>>", self._commit_edit)
        else:
            ed = ttk.Entry(self.tv)
            ed.insert(0, cur)
            ed.select_range(0, "end")

        ed.place(x=x, y=y, width=w, height=h)
        ed.bind("<Return>", self._commit_edit)
        ed.bind("<Escape>", self._cancel_edit)
        # **`<FocusOut>` 은 걸지 않는다.** 더블클릭의 ButtonRelease 에서 트리뷰가
        # 포커스를 도로 가져가고, 콤보박스는 목록을 펼칠 때도 포커스를 잃는다 —
        # 둘 다 편집기를 즉시 지워 버린다. 닫는 길은 Enter · Esc · 다른 줄 선택이다.
        self._editor, self._edit_iid = ed, iid
        ed.focus_set()
        return "break"

    def _commit_edit(self, _e=None) -> str:
        if self._editor is None or self._edit_iid is None:
            return "break"
        iid = self._edit_iid
        p = next((q for i, q in self._param_rows if i == iid), None)
        txt = self._editor.get().split(" = ")[0].strip()
        self._cancel_edit()

        if p is None or not txt:
            return "break"
        try:
            data = _encode(p, txt)
        except ValueError as e:
            messagebox.showerror("Value error", str(e))
            return "break"
        # IODD 가 범위를 적어 두었으면 지킨다 — 장치가 거절하기 전에 여기서 막는다
        if p.low is not None and p.high is not None and not p.values:
            try:
                v = float(txt)
            except ValueError:
                v = None
            if v is not None and not (p.low <= v <= p.high):
                messagebox.showerror(
                    "Out of range",
                    f"{p.name} accepts {p.low:g} .. {p.high:g}")
                return "break"

        self._write(p, data, txt)
        return "break"

    def _write(self, p, data: bytes, txt: str) -> None:
        s = self.app.session
        port = self.port
        if s is None or port is None:
            return
        where = f"port {port} idx {p.index}.{p.subindex} ({p.name})"

        def work():
            try:
                s.isdu_write(port, p.index, data, p.subindex)
                self.after(0, lambda: self.app._log(f"{where} = {txt}"))
                self.after(0, self._read_one)     # 되읽어 확인한다
            except SessionError as err:
                # **기본값으로 묶어 넘긴다.** `except ... as e` 의 `e` 는 블록을
                # 벗어나면 지워지므로, 늦게 실행되는 콜백이 그대로 참조하면
                # NameError 가 나고 진짜 오류가 가려진다.
                self.after(0, lambda m=str(err): self._write_failed(where, txt, m))

        threading.Thread(target=work, daemon=True).start()

    def _write_failed(self, where: str, txt: str, msg: str) -> None:
        """실패는 **로그에도 남긴다.** 대화상자는 닫으면 사라져서 무엇을 언제
        시도했는지가 남지 않는다."""
        self.app._log(f"{where} = {txt}  WRITE FAILED - {msg}")
        messagebox.showerror("Write failed", f"{where}\nvalue {txt}\n\n{msg}")

    # ── 온라인 동작 ─────────────────────────────────────────────────────────
    def _on_mode(self, _e=None) -> None:
        """모드를 골라도 장치에는 아무것도 안 나간다 — `Apply port mode` 가 낸다.

        **IODD 는 여기에 묶지 않는다.** 예전에는 IO-Link 모드가 아니면 `Browse...`
        를 잠갔는데, 포트가 꺼져 있는 상태(=모드를 바꾸러 온 상태)에서 IODD 를 못
        무는 셈이라 순서가 뒤집혀 있었다. IODD 는 장치를 설명하는 파일일 뿐이다.
        """

    def _write_mode(self) -> None:
        s = self.app.session
        if s is None or self.port is None:
            return
        try:
            s.write_port_config(self.port, _MODE_VAL[self.var_mode.get()])
        except SessionError as e:
            messagebox.showerror("Write failed", str(e))
            return

        p = next((x for x in (self.node.ports if self.node else [])
                  if x.port == self.port), None)
        if p is not None:
            p.mode = self.var_mode.get()
        self.app._log(f"port {self.port} mode -> {self.var_mode.get()}, restarting")
        # **포트가 재시작된다.** 기동은 깨우기 → 신원 읽기 → 호환 검사 순이라
        # 곧바로 물으면 아직 DEACTIVATED 다. 자리잡을 때까지 몇 번 더 본다.
        self.after(400, lambda: self._poll_mode(self.port, 0))

    def _poll_mode(self, port: int, tries: int) -> None:
        s = self.app.session
        if s is None or port != self.port:
            return
        st = s.port_status(port)
        if st is not None:
            self._apply_status(port, st)
        settled = st is not None and st["status"] not in (0, 1)   # NO_DEVICE·DEACTIVATED
        if not settled and tries < 12:
            self.after(500, lambda: self._poll_mode(port, tries + 1))
            return
        self.app._log(f"port {port}: {PSI_NAME.get(st['status'], '?') if st else 'no answer'}")
        # 신원이 이제야 왔으므로 IODD 물리기부터 다시 — show() 가 그 전부를 한다
        self.show(self.node, port)

    def _apply_status(self, port: int, st: dict) -> None:
        """살아 있는 상태를 노드 사본에 반영한다 — 트리·띠·라벨이 같은 것을 본다."""
        p = next((x for x in (self.node.ports if self.node else []) if x.port == port),
                 None)
        if p is not None:
            p.status = PSI_NAME.get(st["status"], f"?{st['status']}")
            p.pd_in, p.pd_out = st["pd_in"], st["pd_out"]
            p.vendor_id, p.device_id = st["vendor_id"], st["device_id"]
            self.lbl_status.configure(
                text=f"{p.status}"
                     + (f"   VID {p.vendor_id} DID {p.device_id}"
                        if p.vendor_id else ""))
            self.app.refresh_port(self.node.address, port)
            self.app.master_panel.refresh()   # PD 길이·사이클 하한이 바뀐다
        self.refresh_link()

    def _store_ds(self) -> None:
        """현재 장치 설정을 마스터의 Data Storage 에 올린다.

        **아직 없다.** 모듈 펌웨어에 DS 상태 머신이 없어 `Validation & Backup`
        3·4 를 설정 단계에서 거부한다 (`iol_cm.c`). 버튼은 자리를 잡아 두는
        것이고, 되는 것처럼 보이게 만들지 않는다 — 백업됐다고 믿고 장치를 가는
        것이 최악이다.
        """
        messagebox.showinfo(
            "Data Storage",
            "Not available yet.\n\n"
            "The module firmware has no Data Storage state machine, so it "
            "rejects Validation & Backup 3 and 4. Until that lands, note the "
            "values you set and write them back by hand after a device swap.")

    def _read_one(self) -> None:
        p = self._param()
        s = self.app.session
        port = self.port
        if p is None or s is None or port is None:
            return

        def work():
            try:
                data = s.isdu_read(port, p.index, p.subindex)
                self.after(0, lambda d=data: self._set_dev(p, d))
            except SessionError as err:
                self.after(0, lambda m=str(err): self.app._log(
                    f"port {port} idx {p.index}.{p.subindex} read failed - {m}"))

        threading.Thread(target=work, daemon=True).start()

    def cancel_reads(self) -> None:
        """돌고 있는 파라미터 읽기를 버린다.

        **주기 교환이 우선이다.** 링크 잠금은 하나뿐이라, 108개를 훑는 ISDU 읽기가
        붙들고 있으면 주기 프레임이 못 나가고 워치독이 물려 모듈이 FAILSAFE 로
        떨어진다 — 손실 100 % 로 보이지만 원인은 주기가 아니라 이것이다.
        """
        self._read_gen += 1
        self.btn_read_all.configure(
            state="normal" if self.online else "disabled", text="Read all")

    def auto_read(self) -> None:
        """연결돼 있고 볼 포트가 정해졌으면 값을 읽는다.

        IODD 가 없으면 읽을 목록 자체가 없으니 그렇다고 말한다 — 조용히 넘어가면
        사용자는 읽기가 실패한 줄 안다.
        """
        if not self.online or self.port is None:
            return
        if not self._param_rows:
            self.app._log(f"port {self.port}: {self.lbl_iodd.cget('text')}")
            return
        self._read_all()

    def _read_all(self) -> None:
        s = self.app.session
        if s is None or self.port is None or self.node is None:
            return

        # **표가 비는 이유를 말해 준다.** 파라미터 목록은 IODD 에서 오므로,
        # IODD 가 없으면 읽을 것이 하나도 없다 — 아무 말 없이 돌아오면 버튼이
        # 고장 난 것처럼 보인다.
        if self.iodds.get((self.node.address, self.port)) is None:
            info = next((x for x in self.node.ports if x.port == self.port), None)
            if info is not None and info.vendor_id:
                messagebox.showwarning(
                    "IODD required",
                    f"Port {self.port} has a device"
                    f" (VID {info.vendor_id}  DID {info.device_id})"
                    " but no IODD.\n\n"
                    "The parameter list comes from the IODD, so there is"
                    " nothing to read.\n\n"
                    "Use Browse... to pick the file, or put it in"
                    " EdgeConfig/edgeconfig/iodd/ and press Connect again.")
            else:
                messagebox.showinfo("No device", f"Port {self.port} has no device")
            return

        rows = [(iid, p) for iid, p in self._param_rows if p.readable]
        if not rows:
            return

        port = self.port
        self._read_gen += 1
        gen = self._read_gen
        # **수십 번의 ISDU 왕복이라 시간이 걸린다.** 버튼을 잠가 두 번 돌지 않게
        # 하고, 끝난 것을 로그로 알린다 — 아무 표시도 없으면 멈춘 것처럼 보인다.
        self.btn_read_all.configure(state="disabled", text="Reading...")
        self.app._log(f"port {port}: reading {len(rows)} parameters")

        def work():
            ok = 0
            bad: list[str] = []
            for iid, p in rows:
                if gen != self._read_gen:
                    return                      # 다른 포트로 옮겨 갔다
                try:
                    data = s.isdu_read(port, p.index, p.subindex, timeout=3.0)
                    self.after(0, lambda i=iid, pp=p, d=data: self._set_row(i, pp, d))
                    ok += 1
                except SessionError as err:
                    self.after(0, lambda i=iid: self._set_err(i))
                    # **무엇이 실패했는지 남긴다.** 개수만 세면 장치가 거절한 것과
                    # 통신이 흔들린 것을 구분할 수 없다.
                    bad.append(f"{p.index}.{p.subindex} {p.name[:18]} [{err}]")
            self.after(0, lambda: self.app._log(
                f"port {port}: {ok} read" + (f", {len(bad)} failed" if bad else "")))
            for line in bad:
                self.after(0, lambda t=line: self.app._log(f"    failed - {t}"))
            self.after(0, lambda: self.btn_read_all.configure(
                state="normal" if self.online else "disabled", text="Read all"))

        threading.Thread(target=work, daemon=True).start()

    # ── 표 갱신 ─────────────────────────────────────────────────────────────
    def _set_dev(self, p, data: bytes) -> None:
        for iid, q in self._param_rows:
            if q is p:
                self._set_row(iid, p, data)
                return

    def _remember_value(self, p, data: bytes) -> None:
        """숫자형만 담는다. IODD 의 환산 조건이 가리키는 것이 그것뿐이다."""
        if self.node is None or self.port is None or not data:
            return
        if p.dtype not in ("UIntegerT", "IntegerT") or p.subindex:
            return
        v = int.from_bytes(data, "big", signed=(p.dtype == "IntegerT"))
        self.app.dev_values.setdefault((self.node.address, self.port), {})[
            p.index] = v

    def _set_row(self, iid: str, p, data: bytes) -> None:
        self._remember_value(p, data)
        # **읽는 도중에 포트를 옮기면 그 행은 이미 없다.** 읽기는 스레드에서
        # 돌고 표는 그 사이에 갈아엎히므로, 반드시 살아 있는지 보고 쓴다.
        if self.tv.exists(iid):
            self.tv.set(iid, "device", _decode(p, data))

    def _set_err(self, iid: str) -> None:
        if self.tv.exists(iid):
            self.tv.set(iid, "device", "-")


def _decode(p, data: bytes) -> str:
    """장치가 준 바이트를 사람이 읽는 글자로.

    형을 모르면 **16진수 그대로 둔다.** 모르는 것을 숫자로 꾸며 보이면 그럴듯해서
    틀린 줄을 알아채지 못한다.
    """
    if not data:
        return ""
    if p.dtype in ("UIntegerT", "IntegerT"):
        v = int.from_bytes(data, "big",
                           signed=(p.dtype == "IntegerT"))
        for vn in p.values:
            if vn.value == v:
                return f"{v} ({vn.name})"
        return str(v)
    if p.dtype == "BooleanT":
        return "true" if data[0] else "false"
    if p.dtype in ("Float32T", "Float64T"):
        n = 4 if p.dtype == "Float32T" else 8
        if len(data) < n:
            return data.hex(" ")
        v = struct.unpack(">" + ("f" if n == 4 else "d"), data[:n])[0]
        return f"{v:g}"
    if p.dtype == "StringT":
        return data.decode("ascii", "replace").rstrip("\0 ")
    return data.hex(" ")


def _encode(p, text: str) -> bytes:
    if p.dtype in ("UIntegerT", "IntegerT"):
        v = int(text, 0)
        n = p.byte_length
        return v.to_bytes(n, "big", signed=(p.dtype == "IntegerT"))
    if p.dtype == "BooleanT":
        return bytes([1 if text.strip().lower() in ("1", "true", "on") else 0])
    if p.dtype in ("Float32T", "Float64T"):
        return struct.pack(">" + ("f" if p.dtype == "Float32T" else "d"),
                           float(text))
    if p.dtype == "StringT":
        return text.encode("ascii", "replace")
    return bytes.fromhex(text.replace(" ", ""))
