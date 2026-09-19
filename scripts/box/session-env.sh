#!/usr/bin/env bash
# Source this on a Box (or any Linux desktop) to import the graphical session's
# environment into a non-interactive shell: DISPLAY, XAUTHORITY, the session
# D-Bus address, XDG_RUNTIME_DIR, WAYLAND_DISPLAY. Reads them from a running
# session process, so it works over SSH where none of them are set.
for name in budgie-wm gnome-shell gnome-session-binary xfwm4 mutter sway openbox; do
  pid=$(pgrep -u "$(id -u)" -x "$name" | head -1)
  [ -n "$pid" ] && break
done
if [ -n "${pid:-}" ]; then
  for var in DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR WAYLAND_DISPLAY XDG_SESSION_TYPE XDG_CURRENT_DESKTOP; do
    val=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$var=//p" | head -1)
    [ -n "$val" ] && export "$var=$val"
  done
fi
export GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0
