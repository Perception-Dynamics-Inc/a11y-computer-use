#!/bin/bash
# Own every PID explicitly; never kill another desktop's browser or bus.
set -eu
session_home=$(mktemp -d /tmp/a11y-latency-session.XXXXXX)
trap 'rm -rf "$session_home"' EXIT
export NO_AT_BRIDGE=0 GTK_MODULES=gail:atk-bridge A11Y_VISIBLE_ACTIONS=0
xvfb-run -a -s '-screen 0 1280x800x24 -dpi 96' dbus-run-session -- env HOME="$session_home" XDG_CACHE_HOME="$session_home/cache" bash -c '
set -eu
xfwm4 --compositor=off >"$XDG_CACHE_HOME-wm.log" 2>&1 & wm_pid=$!
/usr/libexec/at-spi-bus-launcher --launch-immediately >"$XDG_CACHE_HOME-atspi.log" 2>&1 & bus_pid=$!
trap '\''kill "$wm_pid" "$bus_pid" 2>/dev/null || true'\'' EXIT
sleep 2
"$@"
' bash "$@"
