#!/usr/bin/env bash
#
# edgelib uninstaller
#
# Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
#
#     sudo bash ./uninstall.sh
#
# Undoes install.sh: the C library, the Python packages, the build products,
# the board setup in config.txt and the group membership. Only what install.sh
# put there is taken back - anything that was already on the board stays.

set -uo pipefail

PREFIX="${PREFIX:-/usr/local}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

for arg in "$@"; do
    case "$arg" in
        -h|--help)
            cat <<'EOF'
edgelib uninstaller

    sudo bash ./uninstall.sh

Removes the C library from /usr/local, the Python packages (edgelib,
edgeconfig), the build products, the config.txt lines and the group
membership that install.sh added.
EOF
            exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m-- %s\033[0m\n' "$*"; }
warn() { printf '\033[33m   ! %s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root: sudo bash ./uninstall.sh" >&2
    exit 1
fi

PY=python3
NEED_REBOOT=0
STATE_DIR=/var/lib/edgelib

# -- 1. Python packages ------------------------------------------------------
say "Python packages"
PIP_FLAGS="--break-system-packages --root-user-action=ignore"

# Ask where the package sits before removing it - afterwards it cannot be
# imported any more.
pkg_dir() {
    $PY - <<'PY' 2>/dev/null
try:
    import pathlib, edgeconfig
    print(pathlib.Path(edgeconfig.__file__).resolve().parent)
except Exception:
    pass
PY
}
EDGECONFIG_DIR="$(pkg_dir)"

# shellcheck disable=SC2086
$PY -m pip uninstall -y $PIP_FLAGS edgeconfig edgelib \
    || $PY -m pip uninstall -y edgeconfig edgelib \
    || warn "pip uninstall failed - remove edgeconfig and edgelib by hand"

# IODD files added in the field are not in pip's file list, so pip leaves the
# directory behind.
if [ -n "$EDGECONFIG_DIR" ] && [ -d "$EDGECONFIG_DIR" ]; then
    case "$EDGECONFIG_DIR" in
        */site-packages/edgeconfig|*/dist-packages/edgeconfig)
            rm -rf "$EDGECONFIG_DIR"
            echo "   leftover IODD files removed" ;;
        *)
            warn "left alone, unexpected path: $EDGECONFIG_DIR" ;;
    esac
fi

# -- 2. C library ------------------------------------------------------------
say "C library <- $PREFIX"
make -s uninstall PREFIX="$PREFIX" || warn "make uninstall failed"

# -- 3. Build products -------------------------------------------------------
say "Build products"
make -s clean || warn "make clean failed"

# -- 4. Board setup ----------------------------------------------------------
say "Board setup (config.txt)"

CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt

has_cfg() {   # has_cfg <line>
    grep -qE "^[[:space:]]*$(echo "$1" | sed 's/[]\/$*.^[]/\\&/g')([[:space:]]|$)" "$CFG"
}

report_cfg() {   # report_cfg <line> <was there before> <is there now>
    if [ "$2" = 0 ]; then
        echo "   $1: not in $CFG"
    elif [ "$3" = 0 ]; then
        echo "   $1: removed"
        NEED_REBOOT=1
    else
        warn "$1: left alone - install.sh did not put it there"
    fi
}

if [ ! -f "$CFG" ]; then
    warn "config.txt not found - nothing to undo"
else
    B_UART=0; has_cfg "dtoverlay=uart3" && B_UART=1
    B_GPIO=0; has_cfg "gpio=25=op,dh"   && B_GPIO=1

    # A line goes only if it sits under the "# EdgeX ..." comment install.sh
    # wrote above it. A line that was on the board before has no such comment
    # and is left where it is, together with the blank line of the block.
    TMP="$(mktemp)"
    awk '
        { line[NR] = $0 }
        END {
            for (i = 1; i <= NR; i++) {
                if ((line[i] ~ /^[ \t]*dtoverlay=uart3[ \t\r]*$/ ||
                     line[i] ~ /^[ \t]*gpio=25=op,dh[ \t\r]*$/) &&
                    i > 1 && line[i-1] ~ /^#[ \t]*EdgeX/) {
                    drop[i] = 1
                    drop[i-1] = 1
                    if (i > 2 && line[i-2] ~ /^[ \t\r]*$/) drop[i-2] = 1
                }
            }
            for (i = 1; i <= NR; i++) if (!(i in drop)) print line[i]
        }
    ' "$CFG" > "$TMP"

    if cmp -s "$TMP" "$CFG"; then
        rm -f "$TMP"
    else
        cp -a "$CFG" "$CFG.edgelib.bak"
        # Written in place: the boot partition is FAT, and a moved-in file
        # would not keep the owner and mode of the original.
        cat "$TMP" > "$CFG"
        rm -f "$TMP"
        echo "   the old file is kept as $CFG.edgelib.bak"
    fi

    A_UART=0; has_cfg "dtoverlay=uart3" && A_UART=1
    A_GPIO=0; has_cfg "gpio=25=op,dh"   && A_GPIO=1
    report_cfg "dtoverlay=uart3" "$B_UART" "$A_UART"
    report_cfg "gpio=25=op,dh"   "$B_GPIO" "$A_GPIO"
    sync
fi

UNIT=/etc/systemd/system/edgelib-gpio25.service
if [ -e "$UNIT" ] || [ -L "$UNIT" ]; then
    say "Old GPIO25 service"
    systemctl unmask edgelib-gpio25.service >/dev/null 2>&1 || true
    systemctl disable edgelib-gpio25.service >/dev/null 2>&1 || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "   removed - the running one is left alone, so power stays up"
    NEED_REBOOT=1
fi

# -- 5. Groups ---------------------------------------------------------------
say "Groups"
ADDED="$STATE_DIR/added-groups"
if [ -f "$ADDED" ]; then
    while read -r u g; do
        [ -n "$u" ] && [ -n "$g" ] || continue
        if gpasswd -d "$u" "$g" >/dev/null 2>&1; then
            echo "   $u taken out of group $g"
        else
            warn "could not take $u out of group $g"
        fi
    done < "$ADDED"
    rm -f "$ADDED"
    rmdir "$STATE_DIR" 2>/dev/null || true
else
    echo "   nothing to undo - install.sh added nobody to a group"
fi

sync

cat <<'EOF'

-- left alone

  packages     what apt installed (build-essential, python3-tk, gpiod, ...)
  examples/    the code the commissioning tool generated

-- done

  edgelib removed.
EOF

if [ "$NEED_REBOOT" = 1 ]; then
    cat <<'EOF'

***************************************************************
  Reboot to finish.

      sudo reboot

  /dev/ttyAMA3 and backplane 5V are still up: config.txt is
  only read at boot, so they go away at the next one.
***************************************************************
EOF
fi
