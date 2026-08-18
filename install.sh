#!/bin/sh
# edgelib installer — EdgeX Slice I/O master library
#
# Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
#
#     sudo sh ./install.sh
#
# What it does
#   1. installs the packages the library and the commissioning tool need
#   2. builds and installs the C library into /usr/local
#   3. installs the Python binding and the `edgeconfig` command
#   4. sets up config.txt: UART3 (/dev/ttyAMA3)
#   5. sets GPIO25 high on every boot (systemd)
#   6. puts the calling user into the dialout / gpio groups

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (sudo sh ./install.sh)"
    exit 1
fi

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

PIP_FLAGS="--break-system-packages --root-user-action=ignore"

# The release is built on Windows, which has no execute bit, so the shell
# scripts arrive without one. Put it back before anything tries to run them.
echo "Fixing script permissions..."
for f in install.sh uninstall.sh EdgeConfig/run_gui.sh; do
    if [ -f "$f" ]; then
        chmod +x "$f"
        echo "  $f: +x"
    fi
done

echo "Checking packages..."
need_apt=""
for p in build-essential python3-pip python3-tk python3-serial python3-libgpiod gpiod; do
    if dpkg -s "$p" > /dev/null 2>&1; then
        echo "  $p: OK"
    else
        echo "  $p: missing"
        need_apt="$need_apt $p"
    fi
done
if [ -n "$need_apt" ]; then
    echo "  Installing:$need_apt"
    apt-get update
    # shellcheck disable=SC2086
    apt-get install -y $need_apt
fi

echo "Setting up the backplane (config.txt)..."
# dtoverlay=uart3   RS-485 on UART3 (GPIO4/GPIO5) -> /dev/ttyAMA3.
#                   Without it every scan fails with "cannot open /dev/ttyAMA3".
#
# GPIO25 (backplane power enable) is NOT set here - it gets a systemd unit
# further down, the same shape prismlib uses for its USB power rail.
#
# Bookworm moved config.txt under /boot/firmware.
NEED_REBOOT=0
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt

# Only the exact line is checked. dtoverlay is append-only and a stock config.txt
# already carries others (vc4-kms-v3d), so "this key is already set to something
# else" is not an error here - it is the normal state, and treating it as one
# silently skips the overlay the backplane depends on.
add_cfg() {   # add_cfg <line> <comment>
    if [ ! -f "$CFG" ]; then
        echo "  ! config.txt not found - add '$1' by hand"
        return
    fi
    if grep -qE "^[[:space:]]*$(echo "$1" | sed 's/[]\/$*.^[]/\\&/g')([[:space:]]|$)" "$CFG"; then
        echo "  $1: already in $CFG"
        return
    fi
    printf '\n# %s\n%s\n' "$2" "$1" >> "$CFG"
    echo "  $1: added to $CFG"
    NEED_REBOOT=1
}

add_cfg "dtoverlay=uart3" "EdgeX backplane - RS-485 master on UART3 (/dev/ttyAMA3)"

if [ -e /dev/ttyAMA3 ]; then
    echo "  /dev/ttyAMA3: present"
else
    echo "  /dev/ttyAMA3: not there yet - it appears after a reboot"
    NEED_REBOOT=1
fi

echo "Building edgelib C library..."
make

echo "Installing edgelib C library..."
make install

echo "Installing edgelib Python binding..."
# shellcheck disable=SC2086
pip install ./python $PIP_FLAGS

echo "Installing EdgeConfig commissioning tool..."
# --no-deps: pyserial and gpiod came from apt above. Without it pip goes to
# PyPI, and a machine on a plant network has nowhere to go.
# shellcheck disable=SC2086
pip install --no-deps ./EdgeConfig $PIP_FLAGS

echo "Cleaning build artifacts..."
rm -rf ./python/build ./python/*.egg-info ./EdgeConfig/build ./EdgeConfig/*.egg-info

echo "Installing GPIO25 boot service (systemd)..."
# gpioset holds the line only while it runs, so the unit must stay alive.
cat > /etc/systemd/system/edgelib-gpio25.service << 'EOF'
[Unit]
Description=EdgeX GPIO25 HIGH
After=sysinit.target

[Service]
Type=simple
ExecStart=/usr/bin/gpioset -c 0 25=1
Restart=on-failure
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable edgelib-gpio25.service
systemctl restart edgelib-gpio25.service || \
    echo "  ! GPIO25 is held by another process - check: pgrep -a gpioset"

echo "Adding $SUDO_USER to the dialout and gpio groups..."
if [ -n "$SUDO_USER" ]; then
    for g in dialout gpio; do
        if getent group "$g" > /dev/null 2>&1; then
            usermod -aG "$g" "$SUDO_USER" && echo "  $g: OK"
        fi
    done
    echo "  (log out and back in for the groups to take effect)"
fi

echo ""
echo "edgelib installed successfully."
echo "  C header       : /usr/local/include/edgelib.h"
echo "  C library      : /usr/local/lib/libedgelib.so"
echo "  Python         : pip show edgelib"
echo "  Commissioning  : edgeconfig gui"
echo "  Documentation  : doc/index.html"
echo "  Backplane      : /dev/ttyAMA3 (dtoverlay=uart3)"
echo "  Power enable   : GPIO25 high from boot (edgelib-gpio25.service)"
echo ""

if [ "$NEED_REBOOT" = "1" ]; then
    echo "***************************************************************"
    echo "  REBOOT REQUIRED before commissioning."
    echo ""
    echo "  /dev/ttyAMA3 only appears after the UART3 overlay is loaded,"
    echo "  and the bus cannot be scanned without it."
    echo ""
    echo "      sudo reboot"
    echo ""
    echo "  After the reboot, start the commissioning tool:"
    echo ""
    echo "      edgeconfig gui"
    echo ""
    echo "  and follow section 3.3 of the documentation (doc/index.html)."
    echo "***************************************************************"
else
    echo "Next: run 'edgeconfig gui' and follow section 3.3 of the documentation."
fi
