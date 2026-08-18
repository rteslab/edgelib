"""EdgeConfig — 커미셔닝 툴 (단일 창, 좌우 분할)

```
┌──────────────────────────────────────────────────────────────┐
│ 툴바 — Scan · 자동 ID · 연결 · PREOP · RUN                    │
├──────────────────────────────────────────────────────────────┤
│ 슬라이스 띠 — 실물 배치 그대로                                │
├───────────────┬──────────────────────────────────────────────┤
│ 모듈 · 포트   │  선택한 것의 설정 · 파라미터 · PD             │
│ (트리)        │                                              │
├───────────────┴──────────────────────────────────────────────┤
│ 이벤트 · 로그                                                 │
└──────────────────────────────────────────────────────────────┘
```

**한 창에 좌우로 둔 이유**는 셋이다. 설정하는 동안 어느 슬롯인지 계속 보이고,
모듈 전환이 한 번 클릭이고, **RUN 의 PD 는 계속 갱신되는 값이라 모달 창과 맞지
않는다.**

**온라인 전용이다.** 하는 일은 장치의 값을 읽고 원하는 값으로 쓰는 것뿐이고,
그 밖의 것은 두지 않는다 — 연결 없이 값을 칠 수 있게 두면 그 값이 어디에 있는
것인지 모호해진다.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from ..catalog import Node, PortInfo

# 정수 코드를 이름으로. edgelib 은 숫자로 주고 화면은 이름으로 읽는다
_MODE_NAME = {0: "DEACTIVATED", 1: "IOL_MANUAL", 2: "IOL_AUTOSTART",
              3: "DI_CQ", 4: "DO_CQ"}
_PSI_NAME = {0: "NO_DEVICE", 1: "DEACTIVATED", 2: "PORT_DIAG", 3: "PREOPERATE",
             4: "OPERATE", 5: "DI_CQ", 6: "DO_CQ", 254: "PORT_POWER_OFF",
             255: "NOT_AVAILABLE"}
from ..discover import scan
from ..link import Link, LinkError
from ..session import STATE_NAME, Session, SessionError

# **주기 통신은 C 가 돌리는 편이 낫다.** 파이썬 스레드로도 100 ms 는 돌지만 6 ms 는
# 못 버티고, 워치독이 물리는 순간 손실 100 % 가 된다. edgelib 이 깔려 있으면 그쪽을
# 쓰고, 없으면 예전대로 파이썬으로 돈다 — 이 툴은 혼자서도 서야 한다.
try:
    from ..live import LiveSession
except Exception:                                    # noqa: BLE001
    LiveSession = None
from .portpanel import PortPanel
from .masterpanel import MasterPanel
from .nodepanel import NodePanel

from pathlib import Path

TITLE = "EdgeConfig - EdgeX Slice I/O Commissioning"
LOGO = Path(__file__).resolve().parent / "rtes_logo.png"

# 주소는 늘 1번부터 준다 (코어 §3.5.2)
FIRST_ADDRESS = 1

# 트리 폭. 가장 긴 줄이 "ID 1   IO-Link Master 4-port" 이다.
TREE_W = 300

CARD_W, CARD_H = 118, 92
# 모듈 카테고리별 머리 색 (DI · DO · AI · IO-Link · AO)
_COLOR = {0x10: "#3B7DD8", 0x20: "#2E9E5B", 0x30: "#C98A22",
          0x40: "#7A4FBF", 0x50: "#C0522E"}
_GREY = "#7A7A7A"

# 회사 로고에서 그대로 가져온 색. 마스터 카드만 이 색을 쓴다 — 모듈과 다른
# 종류라는 것이 색 하나로 드러난다.
BRAND, BRAND_DARK = "#00477C", "#003257"

# 카드 테두리. 전보다 진하게 — 옅으면 카드가 배경에 녹아 경계가 안 보인다.
CARD_EDGE = "#A8A8A8"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(TITLE)
        self.geometry("1240x820")
        self.minsize(1040, 680)

        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass

        self.nodes: list[Node] = []
        self.link: Link | None = None
        self.session: Session | None = None
        self.sel_node: int | None = None
        self.sel_port: int | None = None

        # 장치에서 읽은 파라미터 값. (노드, 포트) → {인덱스: 값}
        #
        # **환산 계수를 고르는 데 쓴다.** IODD 는 온도에 ℃·℉ 두 계수를 두고
        # `V_Unit_T` 값으로 어느 쪽인지 정하게 해 놓았다 — 계산이 아니라 조회다.
        self.dev_values: dict[tuple[int, int], dict[int, int]] = {}

        self._q: queue.Queue = queue.Queue()
        self._busy = False

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain)
        self.after(200, self._tick)

    # ── 화면 ────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self._toolbar()

        self.strip = tk.Frame(self, bg="#EFEFEF", height=CARD_H + 20)
        self.strip.pack(fill="x", padx=8)
        self.strip.pack_propagate(False)
        self._strip_hint()

        # **아래 칸을 먼저 잡는다.** 나중에 pack 하면 위쪽이 세로를 다 먹어
        # 이벤트·로그가 창 밖으로 밀려난다.
        self._bottom()

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8, pady=6)

        left = ttk.Frame(pane, width=TREE_W)
        pane.add(left, weight=0)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        # **글자가 잘리지 않을 만큼 준다.** 계층이 생기면서 들여쓰기가 자리를
        # 먹으므로, 기본 200 px 로는 "Port 1  DEACTIVATED" 가 끝에서 잘린다.
        self.tree.column("#0", width=TREE_W - 20, minwidth=180, stretch=True)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree)

        # **탭이 아니라 트리가 고른다.** 고른 것에 해당하는 화면 하나만 보인다 —
        # 늘 떠 있는 탭은 지금 무엇을 설정하는 중인지를 흐린다.
        self.right = ttk.Frame(pane)
        pane.add(self.right, weight=1)

        self.port_panel = PortPanel(self.right, self)
        self.node_panel = NodePanel(self.right, self)
        self.master_panel = MasterPanel(self.right, self)
        self.image_panel = self.master_panel.image
        self._shown = None
        self._show(self.master_panel)

    def _show(self, panel) -> None:
        if self._shown is panel:
            return
        if self._shown is not None:
            self._shown.pack_forget()
        panel.pack(fill="both", expand=True)
        self._shown = panel

    def _toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.pack(fill="x")

        # 포트 장치·DIR GPIO·주소 배정 방식은 모두 **정해진 것**이라 화면에 두지
        # 않는다. 고칠 수 없는 것을 고칠 수 있는 것처럼 보이면 그것부터 의심하게
        # 된다. 주소는 언제나 1번부터 자동으로 준다 — 이미 주소가 있는 노드는
        # 자기 주소로 답하므로 건드리지 않는다 (코어 §3.5.2).
        # **버튼 하나다.** 훑는 것과 붙는 것을 나눌 이유가 없다 — 훑어 놓고
        # 붙지 않는 상태에 할 수 있는 일이 없고, 나눠 두면 버스를 두 번 훑게
        # 된다(파이썬으로 한 번, edgelib 안에서 C 로 또 한 번).
        self.btn_conn = ttk.Button(bar, text="Connect", command=self._on_connect)
        self.btn_conn.pack(side="left", padx=(0, 2))

        self.btn_run = ttk.Button(bar, text="RUN", command=self._on_run,
                                  state="disabled")
        self.btn_run.pack(side="left", padx=2)

        self.lbl_state = ttk.Label(bar, text="Offline", foreground="#777")
        self.lbl_state.pack(side="left", padx=12)

        # 회사 로고. **참조를 붙들어 둔다** — Tk 의 PhotoImage 는 파이썬 쪽 참조가
        # 사라지면 회수되어 빈 칸만 남는다. 없으면 그냥 넘어간다.
        try:
            self._logo = tk.PhotoImage(file=str(LOGO))
            ttk.Label(bar, image=self._logo).pack(side="right")
        except tk.TclError:
            pass


    def _bottom(self) -> None:
        box = ttk.Frame(self, padding=(8, 0, 8, 8))
        box.pack(side="bottom", fill="x")

        evf = ttk.LabelFrame(box, text="Events", padding=4)
        evf.pack(side="left", fill="both", expand=True)

        self.ev = ttk.Treeview(evf, columns=("where", "type", "code", "name"),
                               show="headings", height=5)
        for c, t, w in (("where", "Where", 110), ("type", "Type", 70),
                        ("code", "Code", 80), ("name", "Detail", 260)):
            self.ev.heading(c, text=t)
            self.ev.column(c, width=w, anchor="w")
        self.ev.pack(side="left", fill="both", expand=True)

        b = ttk.Frame(evf)
        b.pack(side="right", fill="y", padx=4)
        ttk.Button(b, text="Read", command=self._ev_read).pack(fill="x", pady=1)
        ttk.Button(b, text="Clear errors", command=self._ev_clear).pack(fill="x", pady=1)

        lf = ttk.LabelFrame(box, text="Log", padding=4)
        lf.pack(side="right", fill="both", expand=True, padx=(8, 0))
        self.txt = tk.Text(lf, height=7, width=52, wrap="none", font=("monospace", 8))
        self.txt.pack(fill="both", expand=True)
        self.txt.configure(state="disabled")

    # ── 슬라이스 띠 ─────────────────────────────────────────────────────────
    def _strip_hint(self) -> None:
        for w in self.strip.winfo_children():
            w.destroy()
        tk.Label(self.strip, bg="#EFEFEF", fg="#888",
                 text="Press Connect to find the modules on the bus").place(
            relx=0.5, rely=0.5, anchor="center")

    def _card(self, color: str, title: str, sub: str,
              border: str = CARD_EDGE) -> tk.Frame:
        """띠 한 장. 마스터든 모듈이든 같은 틀을 쓴다."""
        f = tk.Frame(self.strip, width=CARD_W, height=CARD_H, bg="white",
                     highlightbackground=border, highlightthickness=1)
        f.pack_propagate(False)

        hd = tk.Frame(f, bg=color, height=22)
        hd.pack(fill="x")
        hd.pack_propagate(False)
        tk.Label(hd, text=title, bg=color, fg="white",
                 font=("sans", 9, "bold")).pack()

        tk.Label(f, text=sub, bg="white", fg="#555", wraplength=CARD_W - 10,
                 font=("sans", 8), justify="center").pack(pady=3)
        return f

    def _render_strip(self) -> None:
        for w in self.strip.winfo_children():
            w.destroy()
        if not self.nodes:
            self._strip_hint()
            return

        # 마스터도 **모듈과 같은 구조**다 — 색 머리띠 + 흰 본문. 실물 배치에서
        # 맨 왼쪽에 오는 한 자리이니 모양이 달라야 할 이유가 없다. 브랜드 남색과
        # 진한 테두리로만 구분한다.
        cpl = self._card(BRAND, "Controller", "EdgeX", border=BRAND_DARK)
        cpl.pack(side="left", padx=(4, 10), pady=10)
        for w in [cpl] + _descend(cpl):
            w.bind("<Button-1>", lambda _e: self._select_edgex())

        for n in self.nodes:
            color = _COLOR.get(n.category, _GREY)
            f = self._card(color, f"ID {n.address}", n.type_name)
            f.pack(side="left", padx=3, pady=10)

            if n.is_iolink and n.ports:
                pf = tk.Frame(f, bg="white")
                pf.pack()
                for p in n.ports:
                    live = p.status in ("OPERATE", "PREOPERATE")
                    tk.Label(pf, text=str(p.port), width=2, font=("sans", 7, "bold"),
                             bg="#2E9E5B" if live else "#DDD",
                             fg="white" if live else "#999").pack(side="left", padx=1)

            for w in [f] + _descend(f):
                w.bind("<Button-1>", lambda _e, a=n.address: self._select_node(a))

    # ── 트리 ────────────────────────────────────────────────────────────────
    def _render_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        # 버스 전체에 걸린 것(사이클 시간·프로세스 이미지·내보내기)은 어느 한
        # 노드의 것이 아니다. 그 자리를 트리에 만들어 준다.
        root = self.tree.insert("", "end", iid="edgex",
                                text="Controller   EdgeX", open=True)
        for n in self.nodes:
            nid = self.tree.insert(root, "end", iid=f"n{n.address}",
                                   text=f"ID {n.address}   {n.type_name}",
                                   open=True)
            for p in n.ports:
                self.tree.insert(nid, "end", iid=f"n{n.address}p{p.port}",
                                 text=self._port_label(p))

    @staticmethod
    def _port_label(p) -> str:
        """앞 공백을 두지 않는다 — 트리가 이미 계층만큼 들여쓰기 때문에
        직접 넣은 공백은 글자만 밀어내 끝을 자른다."""
        mark = "●" if p.status in ("OPERATE", "PREOPERATE") else "○"
        return f"{mark} Port {p.port}   {p.status}"

    def refresh_port(self, node: int, port: int) -> None:
        """포트 한 칸의 표시만 다시 그린다.

        **트리 전체를 다시 만들지 않는다.** 다시 만들면 선택이 풀리고, 그 순간
        `<<TreeviewSelect>>` 가 포트를 놓아 버려 보던 화면이 사라진다.
        """
        n = self._node(node)
        if n is None:
            return
        p = next((x for x in n.ports if x.port == port), None)
        iid = f"n{node}p{port}"
        if p is not None and self.tree.exists(iid):
            self.tree.item(iid, text=self._port_label(p))
        self._render_strip()

    def _on_tree(self, _e=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == "edgex":
            self.sel_node = self.sel_port = None
            self.master_panel.refresh()
            self._show(self.master_panel)
            return
        if "p" in iid[1:]:
            n, p = iid[1:].split("p")
            self.sel_node, self.sel_port = int(n), int(p)
        else:
            self.sel_node, self.sel_port = int(iid[1:]), None
        node = self._node(self.sel_node)
        if self.sel_port is not None:
            self.port_panel.show(node, self.sel_port)
            self._show(self.port_panel)
        else:
            self.node_panel.show(node)
            self._show(self.node_panel)

    def _select_edgex(self) -> None:
        if self.tree.exists("edgex"):
            self.tree.selection_set("edgex")

    def _select_node(self, addr: int) -> None:
        self.tree.selection_set(f"n{addr}")
        self.tree.see(f"n{addr}")

    def _node(self, addr: int | None) -> Node | None:
        return next((n for n in self.nodes if n.address == addr), None)

    # ── IODD 가 없으면 아무것도 하지 않는다 ──────────────────────────────
    def no_iodd_ports(self) -> list:
        """IO-Link 로 장치가 붙었는데 IODD 가 없는 포트들 — `(노드, 포트)`.

        **RUN 도 산출물도 이것 하나로 막는다.** 판정이 두 곳에 있으면 언젠가
        어긋나고, 어긋나면 한쪽만 막히는 상태가 생긴다.

        IODD 가 없으면 그 장치의 프로세스 데이터가 어느 비트가 무엇이고 무엇을
        곱해야 물리량이 되는지를 알 수 없다. ISDU 목록도 IODD 에서 오므로
        파라미터를 볼 수도 고칠 수도 없다 — 커미셔닝이 성립하지 않는다.

        SIO(`DI_CQ`·`DO_CQ`)와 빈 포트는 해당 없다. 비트 하나가 전부라 설명할
        것이 없다.
        """
        out = []
        for n in self.nodes:
            for p in n.ports:
                if not str(p.mode).startswith("IOL") or not p.vendor_id:
                    continue
                if self.port_panel.iodds.get((n.address, p.port)) is None:
                    out.append((n, p))
        return out

    def warn_no_iodd(self, what: str) -> bool:
        """막아야 하면 알리고 True. 문구는 한 곳에서만 쓴다."""
        missing = self.no_iodd_ports()
        if not missing:
            return False
        lines = "\n".join(
            f"    node {n.address} port {p.port}"
            f"    VID {p.vendor_id}  DID {p.device_id}"
            + (f"  {p.product}" if p.product else "")
            for n, p in missing)
        messagebox.showerror(
            "IODD required",
            f"{what} needs an IODD for every IO-Link device.\n\n"
            "These ports have a device but no IODD:\n\n" + lines
            + "\n\nWithout it the process data has no names, types or units,"
              " and the ISDU parameter list stays empty.\n\n"
              "Select the port and use Browse..., or put the IODD file in"
              " EdgeConfig/edgeconfig/iodd/ and press Connect again.")
        self._log(f"{what} blocked: no IODD on "
                  + ", ".join(f"node {n.address} port {p.port}"
                              for n, p in missing))
        return True

    # ── 연결 = 훑기 + PREOP ─────────────────────────────────────────────
    def _nodes_from(self, sess) -> list:
        """edgelib 이 이미 알아낸 것을 툴의 자료형으로 옮긴다.

        **다시 훑지 않는다.** `EdgeBus()` 를 여는 것이 곧 STARTUP 탐색이고, 포트
        크기까지 그때 확인된다. 여기서 `discover.scan()` 을 또 부르면 같은 일을
        두 번 하면서 상태만 흔든다.
        """
        out = []
        for info in sess.bus.nodes():
            node = Node(address=info.address, category=info.category,
                        model=info.model, variant=info.variant,
                        hw_rev=info.hw_rev,
                        serial=bytes.fromhex(info.serial or ""))
            for p in info.ports:
                st = sess.port_status(p.port)
                node.ports.append(PortInfo(
                    port=p.port, mode=_MODE_NAME.get(p.mode, "DEACTIVATED"),
                    pd_in=p.pd_in, pd_out=p.pd_out,
                    vendor_id=p.vendor_id, device_id=p.device_id,
                    status=_PSI_NAME.get(st["status"], "?") if st else "?"))
            out.append(node)
        return out

    def _connect_worker(self) -> None:
        say = lambda m: self._q.put(("log", m))       # noqa: E731
        try:
            if LiveSession is not None:
                # 여는 것이 곧 탐색이다 — C 안에서 STARTUP 을 밟고 포트까지 본다
                sess = LiveSession.open(log=say)
                nodes = self._nodes_from(sess)
            else:
                # edgelib 이 없으면 예전 길로 간다: 훑고, 첫 노드에 붙는다
                self.link = Link()
                nodes = scan(self.link, assign_from=FIRST_ADDRESS, log=say)
                if not nodes:
                    raise SessionError("no module answered on the bus")
                sess = Session(self.link, nodes[0].address, log=say)

            sess.to_preop()
            sess.events_resync()
            self._q.put(("connected", (sess, nodes)))
        except Exception as e:                       # noqa: BLE001
            self._q.put(("error", f"{type(e).__name__}: {e}"))

    # ── 연결 ────────────────────────────────────────────────────────────────
    def _on_connect(self) -> None:
        if self.session is not None:
            self._disconnect()
            return
        if self._busy:
            return
        self._busy = True
        self.btn_conn.configure(state="disabled", text="Connecting...")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connected(self, sess, nodes) -> None:
        """워커가 끝난 뒤 **주 스레드에서만** 화면을 만진다."""
        self.session = sess
        self.nodes = nodes
        self._busy = False

        self._render_strip()
        self._render_tree()
        self.master_panel.refresh()
        self.tree.selection_set("edgex")

        self.btn_conn.configure(state="normal", text="Disconnect")
        self.btn_run.configure(state="normal")
        self.port_panel.set_online(True)
        self.node_panel.set_online(True)

        # 붙자마자 지금 상태를 다 보여준다 — 이벤트는 RESYNC 로 받아 왔고,
        # 파라미터는 여기서 읽는다. 붙어 놓고 버튼을 더 눌러야 할 이유가 없다.
        self._refresh_events()
        self.port_panel.auto_read()

    def _disconnect(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:                        # noqa: BLE001
                pass
            self.session = None
        if self.link is not None:
            self.link.close()
            self.link = None
        self.btn_conn.configure(state="normal", text="Connect")
        self.btn_run.configure(state="disabled", text="RUN")
        self.lbl_state.configure(text="Offline", foreground="#777")
        self.port_panel.set_online(False)
        self.node_panel.set_online(False)

    def _on_run(self) -> None:
        s = self.session
        if s is None:
            return
        try:
            if s.state == 2:                          # RUN → PREOP
                s.stop_run()
                self.btn_run.configure(text="RUN")
                self.master_panel.clear_values()
                return
            # **세션이 붙은 노드에서 가져온다.** 트리 선택(`sel_node`)에서 가져오면
            # `Controller` 를 고른 상태에서는 None 이라 포트 목록이 비고, 출력
            # 페이로드가 OE 1 B 만 나간다. 슬레이브는 자기가 아는 길이보다 짧은
            # 요청을 **일부만 적용하지 않고 통째로 거절**하므로(`bp_cyclic.c`
            # "반쯤 적용된 출력이 가장 나쁘다") 모든 사이클이 BAD_PARAM 이 된다.
            # IODD 가 없으면 올라가지 않는다. 값이 바이트로만 보이고 ISDU 도
            # 비어 있어, 그 상태의 RUN 으로는 커미셔닝이 한 걸음도 나가지 않는다.
            if self.warn_no_iodd("RUN"):
                return
            node = self._node(s.node)
            if node is None:
                raise SessionError(f"node {s.node} is not in the scan")
            ports = {p.port: (p.pd_in, p.pd_out) for p in node.ports}
            cycle_s = self.master_panel.confirm_cycle()
            if cycle_s is None:
                return
            # 읽는 중에 올라가면 그 스레드가 링크를 붙들어 주기 프레임이 밀린다
            self.port_panel.cancel_reads()
            s.to_run(ports, cycle_s)
            self.btn_run.configure(text="STOP")
        except SessionError as e:
            self._log(f"RUN failed: {e}")
            messagebox.showerror("RUN failed", str(e))

    # ── 이벤트 ──────────────────────────────────────────────────────────────
    def _ev_read(self) -> None:
        if self.session is None:
            messagebox.showinfo("Offline", "Connect first")
            return
        self.session.events_resync()
        self._refresh_events()

    def _ev_clear(self) -> None:
        if self.session is None:
            return
        ok = self.session.clear_errors()
        self.session.events_resync()
        self._refresh_events()
        if not ok:
            messagebox.showwarning(
                "Not cleared",
                "A fault is still active.\n"
                "Only resolved errors are cleared - fix the cause first.")

    def _refresh_events(self) -> None:
        self.ev.delete(*self.ev.get_children())
        if self.session is None:
            return
        for ev in self.session.events_active.values():
            self.ev.insert("", "end", values=(
                "Module" if ev["channel"] == 0 else f"Port {ev['channel']}",
                {1: "notify", 2: "warning", 3: "error"}.get(ev["type"], "?"),
                f"0x{ev['code']:04X}", _event_name(ev["code"])))
        for ev in self.session.events_recent[:4]:
            self.ev.insert("", "end", values=(
                f"Port {ev['channel']}" if ev["channel"] else "Module",
                "past", f"0x{ev['code']:04X}", _event_name(ev["code"])))

    # ── 주기 갱신 ───────────────────────────────────────────────────────────
    def _tick(self) -> None:
        s = self.session
        if s is not None:
            # edgelib 쪽은 스냅샷을 여기서 뜬다. 버스를 타지 않으므로 공짜다
            if hasattr(s, "poll") and s.state == 2:
                s.poll()

            if s.state != 2:
                detail = ""
            elif getattr(s, "engine", "python") == "edgelib":
                # **셀 수 없는 것을 적지 않는다.** edgelib 은 프레임 통계를 주지
                # 않고(§6.5), 손실은 문턱을 넘을 때 이벤트로 올라온다
                detail = f"   loss {s.snap.loss_pct:.0f}%"
            else:
                detail = (f"   {s.snap.cycles} cycles"
                          f"   lost {s.snap.errors} ({s.snap.loss_pct:.1f}%)"
                          f"   late {s.snap.late}"
                          f"   RTT {s.snap.rtt_ms:.1f}/{s.snap.rtt_max:.1f} ms")
            self.lbl_state.configure(
                text=f"{STATE_NAME.get(s.state, '?')}{detail}",
                foreground="#1B7F3B" if s.state == 2 else "#1A73E8")
            if s.state == 2:
                self.master_panel.update_pd(s.snap, s.node)
                if s.has_events():
                    s.events_poll()
                    self._refresh_events()
        self.after(200, self._tick)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "connected":
                    self._connected(*payload)
                elif kind == "error":
                    self._busy = False
                    self.btn_conn.configure(state="normal", text="Connect")
                    self._disconnect()
                    self._log(f"failed: {payload}")
                    messagebox.showerror("Failed", str(payload))
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _log(self, msg: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _on_close(self) -> None:
        # **연결을 끊고 나간다.** 그냥 죽으면 모듈이 워치독으로 안전 상태에 들어가고,
        # 다음 접속에서 에러 소거가 필요해진다.
        self._disconnect()
        self.destroy()


def _descend(w) -> list:
    out = []
    for c in w.winfo_children():
        out.append(c)
        out += _descend(c)
    return out


_EVENT_NAMES = {
    0x0101: "Communication loss", 0x0102: "Cycle missed",
    0x1800: "No Device", 0x1802: "VendorID mismatch", 0x1803: "DeviceID mismatch",
    0x1804: "Short circuit at C/Q", 0x1805: "Overtemperature", 0x1807: "Overcurrent L+",
    0x6000: "Invalid cycle time", 0x6001: "Revision fault",
    0xFF26: "Port status changed",
}


def _event_name(code: int) -> str:
    return _EVENT_NAMES.get(code, "")


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
