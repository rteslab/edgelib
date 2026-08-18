#!/bin/sh
# edgelib uninstaller
#
# Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
#
#     sudo sh ./uninstall.sh
#
# Group membership is left alone - it is the machine's, not ours.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (sudo sh ./uninstall.sh)"
    exit 1
fi

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"

PIP_FLAGS="--break-system-packages --root-user-action=ignore"

echo "Removing the GPIO25 boot service..."
systemctl disable --now edgelib-gpio25.service 2>/dev/null || true
rm -f /etc/systemd/system/edgelib-gpio25.service
systemctl daemon-reload
echo "  config.txt is left alone - remove this by hand if you want it gone:"
echo "      dtoverlay=uart3"

echo "Removing Python packages..."
# shellcheck disable=SC2086
pip uninstall -y edgeconfig edgelib $PIP_FLAGS 2>/dev/null || true

echo "Removing C library..."
make uninstall 2>/dev/null || true

echo ""
echo "edgelib removed."
