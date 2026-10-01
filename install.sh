#!/usr/bin/env bash
#
# edgelib installer
#
# Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
#
#     sudo bash ./install.sh
#
# Installs the library, the Python bindings and the EdgeConfig commissioning
# tool, sets up the board (UART3, backplane power) and builds the examples.

set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'EOF'
edgelib installer

    sudo bash ./install.sh

Installs the library, the Python bindings and the EdgeConfig commissioning
tool, sets up the board (UART3, backplane power) and builds the examples.
EOF
            exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m-- %s\033[0m\n' "$*"; }
warn() { printf '\033[33m   ! %s\033[0m\n' "$*"; }

NEED_REBOOT=0

has_target() { make -n "$1" >/dev/null 2>&1; }

if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root: sudo bash ./install.sh" >&2
    exit 1
fi

# -- 1. Environment ----------------------------------------------------------
say "Environment"
command -v cc >/dev/null || { echo "cc not found: apt install build-essential"; exit 1; }
echo "   compiler  $(cc --version | head -1)"
echo "   kernel    $(uname -r)"

PY=python3
command -v $PY >/dev/null || { echo "python3 not found"; exit 1; }

[ -e /usr/include/linux/gpio.h ] || warn "linux/gpio.h not found (linux-libc-dev)"

for f in install.sh uninstall.sh EdgeConfig/run_gui.sh test/run_all.sh; do
    [ -f "$f" ] && chmod +x "$f"
done

say "Packages"
need_apt=""
for p in build-essential python3-pip python3-tk python3-serial python3-libgpiod gpiod; do
    if dpkg -s "$p" > /dev/null 2>&1; then
        echo "   $p: OK"
    else
        echo "   $p: missing"
        need_apt="$need_apt $p"
    fi
done
if [ -n "$need_apt" ]; then
    echo "   installing:$need_apt"
    apt-get update
    # shellcheck disable=SC2086
    apt-get install -y $need_apt
fi

# -- 2. Build and test -------------------------------------------------------
say "Build"
has_target clean && make -s clean
make -s

if has_target test; then
    say "Self test (no hardware needed)"
    make -s test
fi

# -- 3. Install --------------------------------------------------------------
say "Install -> $PREFIX"
make -s install PREFIX="$PREFIX"

say "Python packages"
PIP_FLAGS="--break-system-packages --root-user-action=ignore"

# Where the installed package keeps its IODD files.
iodd_dir() {
    $PY - <<'PY' 2>/dev/null
try:
    import pathlib, edgeconfig
    print(pathlib.Path(edgeconfig.__file__).resolve().parent / "iodd")
except Exception:
    pass
PY
}

# IODD files added in the field are kept: pip clears that directory.
IODD_SAVE=""
OLD_IODD="$(iodd_dir)"
if [ -n "$OLD_IODD" ] && [ -d "$OLD_IODD" ]; then
    IODD_SAVE="$(mktemp -d)"
    cp -a "$OLD_IODD/." "$IODD_SAVE/" 2>/dev/null || true
fi

# shellcheck disable=SC2086
$PY -m pip install --quiet $PIP_FLAGS ./python || warn "python package install failed"
if [ -d EdgeConfig ]; then
    # shellcheck disable=SC2086
    $PY -m pip install --quiet --no-deps $PIP_FLAGS ./EdgeConfig \
        || warn "EdgeConfig install failed"
fi

NEW_IODD="$(iodd_dir)"
if [ -n "$NEW_IODD" ] && [ -d "$NEW_IODD" ]; then
    if [ -n "$IODD_SAVE" ]; then
        for f in "$IODD_SAVE"/*.xml "$IODD_SAVE"/*.zip; do
            [ -f "$f" ] || continue
            b="$(basename "$f")"
            [ -e "$NEW_IODD/$b" ] || cp -a "$f" "$NEW_IODD/$b"
        done
    fi
    [ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER" "$NEW_IODD"
    chmod -R u+rwX "$NEW_IODD"
fi
[ -n "$IODD_SAVE" ] && rm -rf "$IODD_SAVE"

# -- 4. Board setup ----------------------------------------------------------
say "Board setup (config.txt)"

CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt

add_cfg() {   # add_cfg <line> <comment>
    if [ ! -f "$CFG" ]; then
        warn "config.txt not found - add '$1' by hand"
        return
    fi
    if grep -qE "^[[:space:]]*$(echo "$1" | sed 's/[]\/$*.^[]/\\&/g')([[:space:]]|$)" "$CFG"; then
        echo "   $1: already there"
        return
    fi
    printf '\n# %s\n%s\n' "$2" "$1" >> "$CFG"
    echo "   $1: added to $CFG"
    NEED_REBOOT=1
}

add_cfg "dtoverlay=uart3" "EdgeX backplane - UART3 RS-485 master (/dev/ttyAMA3)"
add_cfg "gpio=25=op,dh" "EdgeX backplane power enable - high from boot"

if [ -e /dev/ttyAMA3 ]; then
    echo "   /dev/ttyAMA3: present"
else
    echo "   /dev/ttyAMA3: not yet - it appears after a reboot"
    NEED_REBOOT=1
fi

UNIT=/etc/systemd/system/edgelib-gpio25.service
if [ -e "$UNIT" ] || [ -L "$UNIT" ]; then
    say "Removing the old GPIO25 service"
    systemctl unmask edgelib-gpio25.service >/dev/null 2>&1 || true
    systemctl disable edgelib-gpio25.service >/dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "   removed - the running one is left alone, so power stays up"
    echo "   after a reboot config.txt sets GPIO25 instead"
    NEED_REBOOT=1
fi

# -- 5. Examples -------------------------------------------------------------
if has_target examples; then
    say "Examples"
    make -s examples || warn "the examples do not link. Check the headers"
fi

# -- 6. Permissions ----------------------------------------------------------
say "Permissions"
USER_NAME="${SUDO_USER:-${USER:-root}}"
STATE_DIR=/var/lib/edgelib
for grp in dialout gpio; do
    if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$grp"; then
        echo "   $USER_NAME is in group $grp"
    elif usermod -aG "$grp" "$USER_NAME" 2>/dev/null; then
        echo "   $USER_NAME added to group $grp (log in again to apply)"
        # Noted so uninstall.sh takes back what was given here, and only that.
        mkdir -p "$STATE_DIR"
        echo "$USER_NAME $grp" >> "$STATE_DIR/added-groups"
    else
        warn "$USER_NAME is not in group $grp - sudo usermod -aG $grp $USER_NAME"
    fi
done

sync

cat <<'EOF'

-- done

  C        #include <edgelib.h>   ->  cc ... -ledgelib
  Python   from edgelib import EdgeBus

  Commissioning (reads the bus, writes the config and the code):
      edgeconfig gui
      python3 -m edgeconfig.commission line_a --cycle-us 10000
      -> line_a.json, line_a.h, line_a_pd.py
EOF

if [ "$NEED_REBOOT" = 1 ]; then
    cat <<'EOF'

***************************************************************
  A reboot is needed.

      sudo reboot

  /dev/ttyAMA3 appears once the UART3 overlay is up, and the
  backplane 5V comes on then too, from GPIO25 in config.txt.
***************************************************************
EOF
fi
