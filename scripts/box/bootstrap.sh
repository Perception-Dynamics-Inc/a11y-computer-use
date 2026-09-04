#!/usr/bin/env bash
# Prepare a Box (box.ascii.dev) Ubuntu desktop VM as a computerUse Linux test bed.
#
# Runs ON the box, as the desktop user. Idempotent: re-running only updates.
# Assumes the repo tree was synced to $REPO (default ~/computerUse) first; see
# scripts/box/README.md for the sync command.
#
# What this installs mirrors the `linux` job in .github/workflows/ci.yml, plus
# x11-utils/xdotool for the coordinate-input checks that only a real window
# manager can prove.
set -euo pipefail

REPO="${REPO:-$HOME/computerUse}"
export DEBIAN_FRONTEND=noninteractive

echo "== system deps (AT-SPI2 bus + GI typelibs + apt PyGObject + X11 helpers)"
sudo apt-get update -qq
sudo apt-get install -y -qq --no-install-recommends \
  at-spi2-core gir1.2-atspi-2.0 gir1.2-gtk-3.0 python3-gi python3-venv \
  xclip xdotool x11-utils dbus-x11
# dbus-x11 provides dbus-launch: without it, Gio.bus_get_sync(SESSION) cannot
# autolaunch a session bus if the inherited DBUS_SESSION_BUS_ADDRESS is stale
# (which happens after a box resume), and test_linux_forces_a11y_status errors.
# The CI Linux job installs it for the same reason.

if [[ "${INSTALL_VSCODE:-0}" == "1" ]] && ! command -v code >/dev/null; then
  echo "== VS Code (Electron probe target)"
  curl -sSL -o /tmp/code.deb "https://update.code.visualstudio.com/latest/linux-deb-x64/stable"
  sudo apt-get install -y -qq /tmp/code.deb
fi

echo "== venv with system gi (apt's python3-gi is built for the distro interpreter)"
cd "$REPO"
[[ -d .venv ]] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install --upgrade pip -q
# The [linux] extra is skipped on purpose so pip never builds PyGObject from source.
.venv/bin/pip install -e ".[dev,browser]" python-xlib -q

echo "== smoke"
.venv/bin/python -c "import computeruse; from computeruse.drivers import get_driver, current_platform; d = get_driver(); print('platform:', current_platform(), '| driver:', d.name); assert d.name == 'linux', d.name"
.venv/bin/python -c "import computeruse.server as s; srv = s.build_server(); assert srv.name == 'computeruse'; print('MCP server built:', srv.name)"

echo "== desktop session"
# Budgie on this image ships toolkit-accessibility off; GTK apps only publish
# their trees on the a11y bus when it is on (the driver also forces
# org.a11y.Status.IsEnabled at runtime, this makes it stick for the session).
if command -v gsettings >/dev/null; then
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus" \
    gsettings set org.gnome.desktop.interface toolkit-accessibility true || true
fi
echo "bootstrap done: $(date -u +%FT%TZ)"
