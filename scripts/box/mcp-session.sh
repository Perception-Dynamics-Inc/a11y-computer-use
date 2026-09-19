#!/usr/bin/env bash
# Start the a11y-computer-use MCP server inside the desktop session env, for a
# remote planner: a11y-computer-use agent --mcp-command "box ssh <id> bash ~/a11y-computer-use/scripts/box/mcp-session.sh"
# Nothing may be printed to stdout before the server starts (MCP framing).
set -e
ROOT="${A11Y_REPO:-$HOME/a11y-computer-use}"
# shellcheck disable=SC1091
source "$ROOT/scripts/box/session-env.sh"
# Qt apps (Krita) only expose AT-SPI when asked; GTK apps need the bridge module.
export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 QT_ACCESSIBILITY=1
exec "$ROOT/.venv/bin/a11y-computer-use" mcp
