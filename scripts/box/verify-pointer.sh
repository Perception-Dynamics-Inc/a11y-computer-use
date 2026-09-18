#!/usr/bin/env bash
# Verify the Linux coordinate-input path on a REAL desktop (runs ON the box).
#
# What Xvfb CI cannot prove: with the pointer parked away from (0, 0), a
# coordinate click must (1) put the pointer exactly at the requested screen
# coordinates and (2) land on the widget there; and `Runtime.click(x, y)` with
# no display_id must resolve the display through the driver, not Quartz.
#
# Steps:
#   1. discover the desktop session environment (same block as run-live.sh)
#   2. park the pointer off-origin, run the probe (driver click + Runtime click)
#   3. pytest: synthetic XTEST tests + the live AT-SPI2 suite (incl. the two new
#      coordinate tests) under the real window manager
#   4. optional (XVFB=1): the same live suite under Xvfb + a fresh a11y bus, the
#      CI shape, to show the new tests behave there too (the Runtime test
#      self-skips without a window manager)
set -euo pipefail

REPO="${REPO:-$HOME/a11y-computer-use}"
cd "$REPO"

echo "== 1. desktop session environment"
PID=$(pgrep -f gsd-a11y-settings | head -1 || true)
[[ -z "$PID" ]] && PID=$(pgrep -x budgie-wm | head -1 || true)
if [[ -n "$PID" ]]; then
  while IFS= read -r kv; do export "$kv"; done < <(tr '\0' '\n' < "/proc/$PID/environ" \
    | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XDG_SESSION_TYPE|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY|XDG_CURRENT_DESKTOP)=')
fi
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0
echo "session: type=${XDG_SESSION_TYPE:-?} desktop=${XDG_CURRENT_DESKTOP:-?} DISPLAY=$DISPLAY"
echo "screen: $(xdpyinfo 2>/dev/null | awk '/dimensions/{print $2}')"
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true

echo "== 2. probe: park the pointer off-origin, then coordinate click + Runtime click"
xdotool mousemove 800 400
echo "parked: $(xdotool getmouselocation)"
.venv/bin/python scripts/box/pointer_probe.py
echo "after probe: $(xdotool getmouselocation)"

echo "== 3. pytest under the real window manager"
.venv/bin/pytest tests/test_linux_synthetic.py tests/test_linux_system_synthetic.py -q -p no:cacheprovider 2>&1 | tail -1
.venv/bin/pytest tests/test_linux_live.py -q -rs --tb=short -p no:cacheprovider 2>&1 \
  | grep -v "ATK_IS_VALUE\|Deprecation\|_safe(lambda\|^$" | tail -25

if [[ "${XVFB:-0}" == "1" ]]; then
  echo "== 4. the CI shape: Xvfb + fresh session bus + a11y bus (no window manager)"
  command -v xvfb-run >/dev/null || sudo apt-get install -y -qq xvfb dbus-x11 >/dev/null
  env -u DISPLAY -u XAUTHORITY -u DBUS_SESSION_BUS_ADDRESS \
    xvfb-run -a -s "-screen 0 1280x800x24" dbus-run-session -- bash -euo pipefail -c '
      ( /usr/libexec/at-spi-bus-launcher --launch-immediately & ) 2>/dev/null
      sleep 2
      timeout -k 5 180 .venv/bin/pytest tests/test_linux_live.py -q -rs --tb=short -p no:cacheprovider 2>&1 \
        | grep -v "ATK_IS_VALUE\|Deprecation\|_safe(lambda\|^$" | tail -25
    '
fi
echo "verify-pointer done: $(date -u +%FT%TZ)"
