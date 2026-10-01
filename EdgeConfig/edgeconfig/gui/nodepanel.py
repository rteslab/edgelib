"""노드 하나 — 신원과 주소

**주소는 이름이 아니라 자리다.** 탐색이 준 번호는 발견 순서에 따라 달라지므로,
응용이 "3번 노드의 2번 포트"라고 적어 두면 다음 기동에 다른 장치를 가리킬 수
있다. 그래서 노드마다 **고정할지 말지**를 여기서 정한다.

고정을 골라도 모듈은 그것을 기억하지 못한다 — 전원을 넣으면 주소 없이 뜬다
(`iol_cm_init`, NV 미구현). 그래서 이 선택은 **설정 파일에 남고**, 기동할 때
라이브러리가 UID 를 보고 그 번호를 다시 준다. 지금 버스에 쓰는 것은 즉시
확인해 보라는 뜻이고, 파일에 적히는 것이 실제 근거다.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..catalog import Node
from ..discover import PSI_NAME, set_address
from ..link import Link, LinkError


class NodePanel(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.node: Node | None = None

        idf = ttk.LabelFrame(self, text="Module", padding=8)
        idf.pack(fill="x")
        idf.columnconfigure(1, weight=1)

        ttk.Label(idf, text="Type").grid(row=0, column=0, sticky="w")
        self.lbl_type = ttk.Label(idf, text="-", foreground="#555")
        self.lbl_type.grid(row=0, column=1, sticky="w", padx=10)

        ttk.Label(idf, text="UID").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.lbl_uid = ttk.Label(idf, text="-", foreground="#555",
                                 font=("monospace", 9))
        self.lbl_uid.grid(row=1, column=1, sticky="w", padx=10, pady=(4, 0))

        # ── 주소 ────────────────────────────────────────────────────────────
        af = ttk.LabelFrame(self, text="Address", padding=8)
        af.pack(fill="x", pady=(8, 0))

        self.var_mode = tk.StringVar(value="auto")
        ttk.Radiobutton(af, text="Automatic - assigned in discovery order",
                        variable=self.var_mode, value="auto",
                        command=self._on_mode).grid(row=0, column=0, columnspan=3,
                                                    sticky="w")
        ttk.Radiobutton(af, text="Fixed", variable=self.var_mode, value="fixed",
                        command=self._on_mode).grid(row=1, column=0, sticky="w",
                                                    pady=(4, 0))
        self.var_addr = tk.StringVar(value="1")
        self.ent_addr = ttk.Entry(af, textvariable=self.var_addr, width=5,
                                  state="disabled")
        self.ent_addr.grid(row=1, column=1, sticky="w", padx=8, pady=(4, 0))
        self.btn_apply = ttk.Button(af, text="Apply now", state="disabled",
                                    command=self._apply)
        self.btn_apply.grid(row=1, column=2, sticky="w", pady=(4, 0))

        self.lbl_addr = ttk.Label(af, text="", foreground="#555")
        self.lbl_addr.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(af, foreground="#777", justify="left", wraplength=720,
                  text=("The module does not keep its address across power cycles, "
                        "so the choice is written to the configuration file and "
                        "restored from the UID at start-up. Applying now only "
                        "proves it works.")).grid(row=3, column=0, columnspan=3,
                                                  sticky="w", pady=(6, 0))

        # ── 포트 ────────────────────────────────────────────────────────────
        pf = ttk.LabelFrame(self, text="Ports", padding=6)
        pf.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("port", "mode", "status", "pdin", "pdout", "device")
        self.tv = ttk.Treeview(pf, columns=cols, show="headings", height=5)
        for c, t, w in (("port", "Port", 55), ("mode", "Mode", 130),
                        ("status", "Status", 120), ("pdin", "PD in", 65),
                        ("pdout", "PD out", 65), ("device", "Device", 220)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, anchor="w")
        self.tv.pack(fill="both", expand=True)

    # ── 화면 ────────────────────────────────────────────────────────────────
    def show(self, node: Node | None) -> None:
        self.node = node
        if node is None:
            self.lbl_type.configure(text="-")
            self.lbl_uid.configure(text="-")
            self.tv.delete(*self.tv.get_children())
            return

        self.lbl_type.configure(text=node.type_name)
        self.lbl_uid.configure(text=node.uid_hex)
        self.var_mode.set(getattr(node, "addr_mode", "auto"))
        self.var_addr.set(str(node.address))
        self.lbl_addr.configure(text=f"Currently ID {node.address}")
        self._on_mode()

        self.tv.delete(*self.tv.get_children())
        for p in node.ports:
            dev = (f"VID {p.vendor_id} DID {p.device_id}" if p.vendor_id else "")
            d = self.app.port_panel.iodds.get((node.address, p.port))
            if d is not None:
                dev = f"{d.product}   {dev}"
            self.tv.insert("", "end", values=(
                p.port, p.mode, p.status, p.pd_in, p.pd_out, dev))

    def _on_mode(self) -> None:
        fixed = self.var_mode.get() == "fixed"
        st = "normal" if fixed else "disabled"
        self.ent_addr.configure(state=st)
        self.btn_apply.configure(state=st if self.app.session is not None
                                 else "disabled")
        if self.node is not None:
            self.node.addr_mode = self.var_mode.get()

    def set_online(self, on: bool) -> None:
        self._on_mode()

    # ── 적용 ────────────────────────────────────────────────────────────────
    def _apply(self) -> None:
        if self.node is None:
            return
        try:
            addr = int(self.var_addr.get())
        except ValueError:
            messagebox.showerror("Input error", "Address must be a number")
            return
        if not (1 <= addr <= 126):
            messagebox.showerror("Out of range", "Address must be 1..126")
            return
        if any(n.address == addr and n is not self.node for n in self.app.nodes):
            messagebox.showerror("Already used", f"ID {addr} belongs to another module")
            return
        if addr == self.node.address:
            return

        uid = bytes([self.node.category, self.node.model,
                     self.node.variant, self.node.hw_rev]) + self.node.serial
        old = self.node.address

        # **주소를 바꾸면 세션이 가리키던 상대가 달라진다.** 끊고 다시 붙는다.
        # 끊고 나면 포트가 비므로, 주소 배정만 임시 링크로 처리한다 —
        # `SET_ADDRESS` 는 STARTUP 전용이고 edgelib 은 그것을 열지 않는다.
        self.app._disconnect()

        def work():
            try:
                with Link() as link:
                    ok = set_address(link, uid, addr)
                err = ""
            except LinkError as e:
                ok, err = False, str(e)
            self.after(0, lambda: self._done(ok, old, addr, err))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok: bool, old: int, addr: int, err: str = "") -> None:
        if not ok:
            self.app._log(f"ID {old} -> {addr} failed" + (f": {err}" if err else ""))
            messagebox.showerror(
                "Failed", err or "The module did not take the address")
            return
        self.node.address = addr
        self.node.addr_mode = "fixed"
        self.app.sel_node = addr
        self.app._log(f"ID {old} -> {addr}")
        self.app._render_strip()
        self.app._render_tree()
        self.app.master_panel.refresh()
        self.show(self.node)
