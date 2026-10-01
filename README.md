# edgelib - EdgeX Slice I/O master library

A library for driving EdgeX slice I/O from the CM4. It finds the modules on the
RS-485 backplane, configures them, and exchanges process data cyclically. C and
Python, one API.

## Install

```sh
sudo bash ./install.sh
```

This installs the library, the Python bindings, the commissioning tool
(`edgeconfig`) and the packages all three need. It also adds the backplane port
(`dtoverlay=uart3`) and the power enable (`gpio=25=op,dh`) to `config.txt`, so
**reboot once after installing**.

> The power enable is a `config.txt` line rather than a service on purpose. If a
> process held GPIO25, the pin would drop to the pull-down the moment that
> process died or stopped, and **backplane 5V would go with it.** The firmware
> applies `config.txt` at boot and nothing requests the line afterwards.

To remove it: `sudo bash ./uninstall.sh`. It takes back what the installer put
there, the two `config.txt` lines and the group membership included, and leaves
anything that was on the board before it alone. `config.txt` is read at boot, so
**reboot once after removing** as well.

## Getting started

```sh
edgeconfig gui
```

What to do, in what order, in front of a powered-up system is in **section 3.3,
"Walk-through", of the manual**, screen by screen.

| | |
|---|---|
| `doc/index.html` | **API manual.** Open it in a browser |
| `include/` · `lib/` | C library |
| `python/` | Python bindings |
| `EdgeConfig/` | Commissioning tool |
| `examples/` | **What the commissioning tool generates lands here** |

## An empty examples/ is normal

Examples differ per installation. What is plugged into which port, and the shape
of that device's data, decides the code - so it cannot be written in advance.

`Generate example code` in the commissioning tool writes an example **for your
own system** into this folder. It lands here no matter which directory you
started `edgeconfig` from.

```
examples/<date_time>/
├── c/        edgex.json · edgex.h · edgex_example.c · Makefile
└── python/   edgex.json · edgex_pd.py · edgex_example.py
```

Every press creates a new folder, so earlier output is never overwritten. To
collect it somewhere else, set `EDGECONFIG_EXAMPLES`.

```sh
EDGECONFIG_EXAMPLES=/mnt/usb/edgex  edgeconfig gui
```

The last step of section 3.3 has the details.

## Connecting

The LAN port ships with a fixed IP. Put your PC on the same subnet (for example
`192.168.0.11`) and connect.

| | |
|---|---|
| IP address | `192.168.0.10` |
| Subnet mask | `255.255.255.0` |

```sh
ssh admin@192.168.0.10
```

The initial account is `admin` / `1234`. **Change it before first use.**

---

Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
