#!/usr/bin/env bash
# Run computerUse's Linux + browser verification INSIDE the box's real desktop
# session (Budgie on Xorg on the current Box image). Runs ON the box.
#
# Steps:
#   1. discover the desktop session environment from a live session process
#   2. hermetic tests (no display needed)
#   3. live AT-SPI2 tests against a real GTK3 window under a real window manager
#   4. non-headless Chrome with a CDP port: live browser tests + cu-arena numbers
#
# Every step prints its pytest summary line; nothing here edits the repo.
set -uo pipefail

REPO="${REPO:-$HOME/computerUse}"
CDP_PORT="${CDP_PORT:-9222}"
cd "$REPO"

echo "== 1. desktop session environment"
# Any process inside the graphical session carries the variables we need.
# gsd-a11y-settings and budgie-wm are always present on this image; fall back
# to the first process that has DISPLAY set.
PID=$(pgrep -f gsd-a11y-settings | head -1 || true)
[[ -z "$PID" ]] && PID=$(pgrep -x budgie-wm | head -1 || true)
if [[ -z "$PID" ]]; then
  for p in $(pgrep -u "$(id -u)"); do
    if tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | grep -q '^DISPLAY='; then PID=$p; break; fi
  done
fi
if [[ -n "$PID" ]]; then
  while IFS= read -r kv; do export "$kv"; done < <(tr '\0' '\n' < "/proc/$PID/environ" \
    | grep -E '^(DISPLAY|WAYLAND_DISPLAY|XDG_SESSION_TYPE|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY|XDG_CURRENT_DESKTOP)=')
fi
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0
echo "session: type=${XDG_SESSION_TYPE:-?} desktop=${XDG_CURRENT_DESKTOP:-?} DISPLAY=$DISPLAY WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset}"
echo "wm: $(xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | awk '{print $NF}' | xargs -I{} xprop -id {} _NET_WM_NAME 2>/dev/null | cut -d= -f2-)"
echo "screen: $(xdpyinfo 2>/dev/null | awk '/dimensions/{print $2}')"
echo "a11y bus: $(dbus-send --session --print-reply --dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress 2>/dev/null | awk -F'"' '/string/{print $2}')"
gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null || true

echo "== 2. hermetic (no display)"
.venv/bin/pytest tests/test_drivers.py tests/test_observe.py tests/test_linux_synthetic.py tests/test_browser.py -q -p no:cacheprovider 2>&1 | tail -1

echo "== 3. live AT-SPI2 backend under the real window manager"
.venv/bin/pytest tests/test_linux_live.py -q -rs -p no:cacheprovider 2>&1 | grep -v ATK_IS_VALUE | tail -1
echo "== 3b. coordinate/vision input, chords, Unicode typing, scroll, drag, apps/windows/clipboard, gated Runtime"
.venv/bin/pytest tests/test_linux_desktop_live.py -q -rs -p no:cacheprovider 2>&1 | grep -v ATK_IS_VALUE | tail -1
echo "== 3c. the whole suite, as it runs on this desktop"
.venv/bin/pytest -q -p no:cacheprovider 2>&1 | grep -v ATK_IS_VALUE | tail -1
.venv/bin/computeruse doctor 2>&1 | tail -1

echo "== 4. browser backend against a real (non-headless) Chrome"
CHROME=$(command -v google-chrome || command -v google-chrome-stable || command -v chromium || true)
if [[ -n "$CHROME" ]]; then
  pkill -f "remote-debugging-port=$CDP_PORT" 2>/dev/null || true; sleep 1
  nohup "$CHROME" --remote-debugging-port="$CDP_PORT" --no-first-run --no-default-browser-check \
    --password-store=basic --user-data-dir="$HOME/.cache/cu-chrome" about:blank >/tmp/cu-chrome.log 2>&1 &
  for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1 && break; sleep 0.5; done
  export COMPUTERUSE_CDP_ENDPOINT="http://127.0.0.1:$CDP_PORT"
  .venv/bin/pytest tests/test_browser.py -k live -q -rs -p no:cacheprovider 2>&1 | tail -1
  .venv/bin/pytest tests/test_arena.py -k live -q -rs -s -p no:cacheprovider 2>&1 | grep -E 'cu-arena|tok|cheaper|passed|failed'
  .venv/bin/computeruse bench web https://example.com --rounds 3 2>&1 | tail -6
  if [[ "${KEEP_CHROME:-0}" != "1" ]]; then pkill -f "remote-debugging-port=$CDP_PORT" 2>/dev/null || true; fi
else
  echo "no chrome binary found; skipping browser step"
fi
echo "run-live done: $(date -u +%FT%TZ)"
