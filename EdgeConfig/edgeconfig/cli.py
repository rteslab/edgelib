"""명령줄 진입점 — GUI 와 같은 코드를 쓴다."""

from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk

from .link import DEFAULT_DIR_GPIO, DEFAULT_PORT, Link, LinkError
from .discover import scan


def cmd_discover(args) -> int:
    try:
        with Link(port=args.port, dir_gpio=args.dir_gpio) as link:
            nodes = scan(link, assign_from=args.assign_from, log=print)
    except LinkError as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1

    print()
    for n in nodes:
        print(f"  ID {n.address:3d}  {n.type_name}")
        print(f"          UID {n.uid_hex}")
        for p in n.ports:
            if p.status not in ("DEACTIVATED", "?"):
                print(f"          port {p.port}  {p.status}"
                      f"  PD {p.pd_in}/{p.pd_out}"
                      f"  VID {p.vendor_id:#06x} DID {p.device_id:#08x}")
    return 0


def cmd_gui(_args) -> int:
    """GUI 를 띄운다.

    **SSH 로 들어오면 `DISPLAY` 가 없다.** 그대로 두면 Tk 가 화면을 못 잡고
    죽는데, 같은 일을 하는 `run_gui.sh` 는 `:0` 을 채워 주므로 한쪽만 되는
    상태가 된다 — 문서가 `edgeconfig gui` 라고 적어 두었으니 이쪽을 맞춘다.

    `:0` 은 Pi 에 붙은 화면이다. 창은 거기 뜨고, 원격에서 보려면 `ssh -X` 로
    들어와야 한다 (그때는 `DISPLAY` 가 이미 있으므로 건드리지 않는다).
    """
    os.environ.setdefault("DISPLAY", ":0")
    try:
        from .gui.app import main
    except ImportError as e:
        print(f"cannot start the GUI: {e}\n"
              "  Tk bindings are missing:  sudo apt install python3-tk",
              file=sys.stderr)
        return 1
    try:
        return main()
    except tk.TclError as e:
        print(f"cannot open a window on DISPLAY={os.environ['DISPLAY']}: {e}\n"
              "  - on the Pi's own screen it just works\n"
              "  - over SSH:  ssh -X admin@<ip>   then  edgeconfig gui\n"
              "  - or send it to the Pi's screen: DISPLAY=:0 edgeconfig gui",
              file=sys.stderr)
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="edgeconfig",
                                 description="EdgeX slice I/O commissioning tool")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--dir-gpio", type=int, default=DEFAULT_DIR_GPIO)

    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("discover", help="scan the bus for modules")
    d.add_argument("--assign-from", type=int, default=None,
                   help="assign addresses to unassigned nodes starting here")
    d.set_defaults(func=cmd_discover)

    g = sub.add_parser("gui", help="open the GUI")
    g.set_defaults(func=cmd_gui)

    args = ap.parse_args(argv)
    if not hasattr(args, "func"):
        return cmd_gui(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
