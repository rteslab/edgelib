"""프로세스 데이터 — 어느 바이트가 무엇이고, 지금 얼마인가

**노드 하나의 것만 본다.** 버스 전체를 실시간으로 띄우는 것은 설정 툴의 일이
아니다 — 여기서 하는 것은 한 모듈이 제대로 도는지 확인하는 일이고, 그 확인은
그 모듈 화면에 있어야 한다.

배치와 값을 한 표에 둔다. 갈라 두면 "3번 바이트가 무엇이고 지금 얼마인가"를 두
화면에서 맞춰 봐야 하고, 맞춰 보는 순간 틀린다.

**한정자는 표에 넣지 않는다.** `MST`(포트별 PQ·ISDU 준비)와 `OE`(출력 허용)는
전선 위에서 byte 0 이지만, 데이터가 아니라 그 데이터를 어떻게 볼지를 정하는
값이다. 바이트 목록에 끼워 넣으면 다른 항목과 같은 것으로 읽힌다. 그래서 위쪽에
포트별 칸으로 따로 둔다.

출력은 **여기서 바꾼다.** OE 를 켜야 나가고, 끄면 장치가 자기 안전값으로 간다.
"""

from __future__ import annotations

import threading
from pathlib import Path
from tkinter import messagebox, ttk

from .. import image as img_mod
from .. import iodd as iodd_mod
from ..paths import IODD_DIR
from ..session import SessionError

class ImagePanel(ttk.Frame):
    def __init__(self, master, app, title=None):
        super().__init__(master, padding=0)
        self.app = app
        self._title = title            # 한정자를 여기 제목으로 적는다
        self.image: img_mod.ProcessImage | None = None
        self.only: int | None = None
        self.only_port: int | None = None
        self._rows: list[tuple[str, img_mod.Entry]] = []
        #: (노드, 포트, 방향) → 그 포트 구간이 이미지에서 시작하는 바이트
        self._base: dict[tuple[int, int, str], int] = {}
        self._editor = None
        self._edit_iid = None
        self._reading = False          # 환산 계수를 읽는 중인가

        # ── 바이트 표 ───────────────────────────────────────────────────────
        # ISDU 표와 같은 결로 읽히게 한다 — 어디·무엇·형·넣을 수 있는 값·지금 값.
        cols = ("dir", "span", "where", "name", "type", "range", "value")
        self.tv = ttk.Treeview(self, columns=cols, show="headings", height=12)
        for c, t, w in (("dir", "Dir", 45), ("span", "Byte", 70),
                        ("where", "Where", 120), ("name", "Item", 210),
                        ("type", "Type", 100), ("range", "Range", 150),
                        ("value", "Value", 130)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tv.tag_configure("stale", foreground="#999")
        self.tv.bind("<Double-1>", self._begin_edit)
        self.tv.bind("<Return>", self._begin_edit)

    # ── 배치 ────────────────────────────────────────────────────────────────
    def refresh(self, only: int | None = None,
                only_port: int | None = None) -> None:
        self.only, self.only_port = only, only_port
        nodes = []
        for n in self.app.nodes:
            if only is not None and n.address != only:
                continue
            spec = img_mod.NodeSpec(
                node=n.address, type_name=n.type_name,
                category=n.category, model=n.model, variant=n.variant,
                hw_rev=n.hw_rev, serial=n.serial.hex().upper(),
                addr_mode=n.addr_mode)
            for p in n.ports:
                d = self.app.port_panel.iodds.get((n.address, p.port))
                if d is None and p.vendor_id:
                    d = iodd_mod.index(IODD_DIR).get((p.vendor_id, p.device_id))
                    if d is not None:
                        self.app.port_panel.iodds[(n.address, p.port)] = d
                spec.ports.append(img_mod.PortSpec(
                    port=p.port, mode=p.mode, pd_in=p.pd_in, pd_out=p.pd_out,
                    iodd=d, product=d.product if d else p.product,
                    vendor_id=p.vendor_id, device_id=p.device_id))
            nodes.append(spec)

        self.image = img_mod.build(nodes)
        self.tv.delete(*self.tv.get_children())
        self._rows.clear()

        # 스냅샷은 **포트 구간**을 준다(`live.py`). 엔트리의 `byte` 는 이미지
        # 전체 기준이라, 그 포트가 어디서 시작하는지를 알아야 자기 몫을 잘라
        # 낼 수 있다. 구간의 첫 엔트리가 곧 시작점이다.
        self._base = {}
        for e in self.image.entries:
            if e.port == 0:
                continue
            k = (e.node, e.port, e.direction)
            if k not in self._base or e.byte < self._base[k]:
                self._base[k] = e.byte
        for e in self.image.entries:
            if e.port == 0:
                continue                       # 한정자는 위쪽 칸이 맡는다
            if only_port is not None and e.port != only_port:
                continue
            bits = (f"{e.dtype} {e.bit_length}b"
                    if e.bit_length and e.dtype else e.dtype)
            iid = self.tv.insert("", "end", values=(
                "IN" if e.direction == img_mod.DIR_IN else "OUT",
                e.span, e.where, e.name, bits, self._range_of(e), ""))
            self._rows.append((iid, e))

        self._show_qual(None)
        self._need_settings()

    # ── 값 ──────────────────────────────────────────────────────────────────
    def _show_qual(self, snap) -> None:
        """머리줄 — **지금 보고 있는 포트의 한정자.**

        `PDQ` 는 그 포트의 입력을 써도 되는지, `OE` 는 출력이 장치까지 가는지를
        말한다. 바이트 수는 위 Nodes 표가 이미 보여주므로 여기 두지 않는다.
        """
        if self._title is None:
            return
        ports = sorted({e.port for _i, e in self._rows})
        if not ports:
            self._title.configure(text="Process data (RUN)")
            return
        s = self.app.session
        bits = []
        for p in ports:
            pq = "valid" if (snap is not None and snap.pq(p)) else "-"
            oe = "valid" if (s is not None and (s.out_qual >> (p - 1)) & 1) else "-"
            tag = "" if len(ports) == 1 else f"P{p} "
            bits.append(f"{tag}PDQ ({pq}), OE ({oe})")
        self._title.configure(text="Process data - " + "   ".join(bits))

    def update_pd(self, snap, node: int) -> None:
        """RUN 중 200 ms 마다. 세션은 노드 하나를 보므로 그 노드 줄만 채운다."""
        self._show_qual(snap)
        for iid, e in self._rows:
            if e.node != node or not self.tv.exists(iid):
                continue
            self.tv.set(iid, "value", self._value_of(e, snap))
            self.tv.item(iid, tags=()
                         if (e.direction == img_mod.DIR_OUT or snap.pq(e.port))
                         else ("stale",))

    def _raw_text(self, e, data) -> str:
        """IODD 가 없거나 SIO·DI 라 `item` 이 없는 자리.

        **자기 몫만 보여 준다.** 구간 전체를 찍으면 한 줄이 옆줄의 바이트까지
        말하게 되어, IODD 가 없는 포트의 화면이 "이도 저도 아닌" 것이 된다.
        """
        base = self._base.get((e.node, e.port, e.direction))
        if base is None:
            return ""
        chunk = bytes(data)[e.byte - base:e.byte - base + e.length]
        if not chunk:
            return ""
        # SIO 의 C/Q 와 핀 2 는 비트 하나다 — 16진수로 두면 읽는 사람이 다시 센다
        if e.bit_length == 1:
            return "ON" if (chunk[0] >> e.bit_offset) & 1 else "off"
        return chunk.hex(" ")

    def _value_of(self, e, snap) -> str:
        if e.direction == img_mod.DIR_OUT:
            s = self.app.session
            data = s.out_data.get(e.port) if s else None
            if not data:
                return ""
            if e.item is None:
                return self._raw_text(e, data)
            v = e.item.extract(bytes(data), e.total_bits)
            return ("ON" if v else "off") if e.item.is_bool else str(v)

        data = snap.ports.get(e.port)
        if not data:
            return ""
        if e.item is None:
            return self._raw_text(e, data)
        v = e.item.extract(data, e.total_bits)
        if e.item.is_bool:
            return "ON" if v else "off"
        # **IODD 가 적어 둔 계수로 물리량을 만든다.** 어느 계수인지는 장치의 단위
        # 설정이 정하므로, 읽어 둔 값이 없으면 원시값 그대로 둔다 — 지어내지 않는다.
        return e.item.value_text(v, self._settings(e))

    def _settings(self, e) -> dict:
        """그 포트에서 읽어 둔 파라미터 값. 환산 계수를 고르는 데 쓴다."""
        return self.app.dev_values.get((e.node, e.port), {})

    def _need_settings(self) -> None:
        """환산 계수를 고르는 데 필요한 파라미터를 **장치에서 읽어 온다.**

        IODD 는 온도에 ℃·℉ 두 계수를 두고 `V_Unit_T` 값으로 어느 쪽인지 정하게
        해 놓았다. 그 값을 모르면 원시값밖에 못 보여주므로, 필요한 것만 (보통
        서너 개) 골라 한 번 읽는다 — 파라미터 전체를 훑을 이유는 없다.
        """
        s = self.app.session
        if s is None or self._reading:
            return

        want: dict[int, set] = {}
        for _iid, e in self._rows:
            if e.item is None or e.node != s.node:
                continue
            have = self.app.dev_values.get((e.node, e.port), {})
            for sc in e.item.scales:
                for idx, _v in sc.conds:
                    if idx not in have:
                        want.setdefault(e.port, set()).add(idx)
        if not want:
            return

        node = s.node
        self._reading = True

        def work():
            try:
                for port, idxs in want.items():
                    for idx in sorted(idxs):
                        try:
                            d = s.isdu_read(port, idx, 0, timeout=3.0)
                        except SessionError as err:
                            self.after(0, lambda p=port, i=idx, m=str(err):
                                       self.app._log(
                                           f"port {p}: scale factor idx {i}"
                                           f" unreadable - {m}"))
                            continue
                        if not d:
                            continue
                        v = int.from_bytes(d, "big")
                        self.after(0, lambda p=port, i=idx, val=v:
                                   self.app.dev_values.setdefault(
                                       (node, p), {}).update({i: val}))
                # 계수가 정해졌으니 범위 열을 다시 쓴다
                self.after(0, self._redraw_ranges)
            finally:
                self._reading = False

        threading.Thread(target=work, daemon=True).start()

    def _redraw_ranges(self) -> None:
        for iid, e in self._rows:
            if self.tv.exists(iid):
                self.tv.set(iid, "range", self._range_of(e))

    def _range_of(self, e) -> str:
        """넣을 수 있는 값 — **환산까지 적용한다.** 값은 28.3 인데 범위가
        -100 .. 600 이면 둘이 다른 단위로 읽혀 오히려 헷갈린다."""
        it = e.item
        if it is None:
            return ""
        if it.is_bool:
            return "off / ON"
        return it.range_text(self._settings(e))

    def clear_values(self) -> None:
        self._cancel_edit()
        self._show_qual(None)
        for iid, _e in self._rows:
            if self.tv.exists(iid):
                self.tv.set(iid, "value", "")
                self.tv.item(iid, tags=())


    # ── 출력 바꾸기 ─────────────────────────────────────────────────────────
    def _cancel_edit(self, _e=None) -> None:
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
            self._edit_iid = None

    def _begin_edit(self, event=None) -> str:
        self._cancel_edit()
        s = self.app.session
        if s is None or s.state != 2:
            return "break"

        iid = (self.tv.identify_row(event.y)
               if event is not None and getattr(event, "y", None) is not None
               else (self.tv.selection() or [None])[0])
        if not iid:
            return "break"
        e = next((x for i, x in self._rows if i == iid), None)
        if e is None or e.direction != img_mod.DIR_OUT or e.item is None:
            return "break"                     # 입력과 원시 덩어리는 못 고친다

        box = self.tv.bbox(iid, "value")
        if not box:
            return "break"
        x, y, w, h = box
        cur = self.tv.set(iid, "value")

        if e.item.is_bool:
            ed = ttk.Combobox(self.tv, state="readonly", values=["off", "ON"])
            ed.set(cur if cur in ("off", "ON") else "off")
            ed.bind("<<ComboboxSelected>>", self._commit_edit)
        else:
            ed = ttk.Entry(self.tv)
            ed.insert(0, cur)
            ed.select_range(0, "end")
        ed.place(x=x, y=y, width=w, height=h)
        ed.bind("<Return>", self._commit_edit)
        ed.bind("<Escape>", self._cancel_edit)
        self._editor, self._edit_iid = ed, iid
        ed.focus_set()
        return "break"

    def _commit_edit(self, _e=None) -> str:
        if self._editor is None or self._edit_iid is None:
            return "break"
        iid, txt = self._edit_iid, self._editor.get().strip()
        e = next((x for i, x in self._rows if i == iid), None)
        self._cancel_edit()
        s = self.app.session
        if e is None or s is None or e.item is None:
            return "break"

        try:
            if e.item.is_bool:
                val = 1 if txt in ("ON", "on", "1", "true") else 0
            else:
                val = int(txt, 0)
        except ValueError:
            messagebox.showerror("Value error", f"{e.name} expects a number")
            return "break"

        buf = bytearray(s.out_data.get(e.port) or bytes(s.pd_out_len.get(e.port, 0)))
        if not buf:
            return "break"
        e.item.insert(buf, e.total_bits, val)
        s.set_output(e.port, bytes(buf))
        if self.tv.exists(iid):
            self.tv.set(iid, "value", self._value_of(e, s.snap))
        self.app._log(f"port {e.port} out {e.name} = {txt}")
        return "break"
