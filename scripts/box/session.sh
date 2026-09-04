#!/usr/bin/env bash
# Source this file on the box to enter the existing desktop session.
session_pid=$(pgrep -x budgie-wm | head -1 || true)
if [[ -z "$session_pid" ]]; then
  session_pid=$(pgrep -f '/gsd-a11y-settings' | head -1 || true)
fi
if [[ -n "$session_pid" ]]; then
  while IFS= read -r session_var; do export "$session_var"; done < <(
    tr '\0' '\n' < "/proc/$session_pid/environ" |
      grep -E '^(DISPLAY|WAYLAND_DISPLAY|XDG_SESSION_TYPE|XDG_RUNTIME_DIR|DBUS_SESSION_BUS_ADDRESS|XAUTHORITY|XDG_CURRENT_DESKTOP)='
  )
fi
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
export GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0
unset session_pid session_var
