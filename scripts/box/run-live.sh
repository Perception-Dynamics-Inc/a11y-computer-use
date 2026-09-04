#!/usr/bin/env bash
# Run on an ASCII Box: fail on errors AND on missing live coverage.
set -euo pipefail

REPO="${REPO:-$HOME/computerUse}"
CDP_PORT="${CDP_PORT:-9222}"
cd "$REPO"
source scripts/box/session.sh
REPORT_DIR="${REPORT_DIR:-$REPO/artifacts/box-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$REPORT_DIR"
export COMPUTERUSE_CDP_ENDPOINT="http://127.0.0.1:$CDP_PORT"

chrome_pid=""
chrome_profile=""
cleanup() {
  if [[ -n "$chrome_pid" && "${KEEP_CHROME:-0}" != "1" ]]; then
    kill "$chrome_pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "$chrome_pid" 2>/dev/null; then break; fi
      sleep 0.1
    done
    kill -KILL "$chrome_pid" 2>/dev/null || true
    wait "$chrome_pid" 2>/dev/null || true
    rm -rf -- "$chrome_profile"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Desktop: ${XDG_CURRENT_DESKTOP:-unknown}, DISPLAY=$DISPLAY"
xdpyinfo >/dev/null
gsettings set org.gnome.desktop.interface toolkit-accessibility true

# Reuse the local CDP port; otherwise launch and clean up only our own Chrome.
if ! curl -fsS --connect-timeout 1 --max-time 2 "$COMPUTERUSE_CDP_ENDPOINT/json/version" >/dev/null 2>&1; then
  chrome_bin=$(command -v google-chrome || command -v google-chrome-stable || command -v chromium)
  chrome_profile=$(mktemp -d /tmp/computeruse-chrome.XXXXXX)
  "$chrome_bin" --remote-debugging-address=127.0.0.1 --remote-debugging-port="$CDP_PORT" \
    --no-first-run --no-default-browser-check --password-store=basic \
    --user-data-dir="$chrome_profile" about:blank >"$REPORT_DIR/chrome.log" 2>&1 &
  chrome_pid=$!
  for _ in $(seq 1 60); do
    if curl -fsS --connect-timeout 1 --max-time 2 "$COMPUTERUSE_CDP_ENDPOINT/json/version" >/dev/null 2>&1; then break; fi
    kill -0 "$chrome_pid" || { cat "$REPORT_DIR/chrome.log"; exit 1; }
    sleep 0.5
  done
  curl -fsS --connect-timeout 1 --max-time 2 "$COMPUTERUSE_CDP_ENDPOINT/json/version" >/dev/null
fi

# Preserve all diagnostics and exit statuses, including errors before the summary.
timeout -k 5 600 .venv/bin/pytest -q -rs -p no:cacheprovider \
  --junitxml="$REPORT_DIR/full.xml" 2>&1 | tee "$REPORT_DIR/full.log"
timeout -k 5 300 .venv/bin/pytest tests/test_linux_live.py tests/test_linux_desktop_live.py \
  -q -rs -p no:cacheprovider --junitxml="$REPORT_DIR/linux.xml" 2>&1 | tee "$REPORT_DIR/linux.log"
.venv/bin/python scripts/box/check_results.py "$REPORT_DIR/linux.xml"
timeout -k 5 300 .venv/bin/pytest tests/test_browser.py tests/test_adapters.py \
  tests/test_agent.py tests/test_h2h.py tests/test_arena.py \
  -k 'live and not finder' -q -rs -p no:cacheprovider \
  --junitxml="$REPORT_DIR/browser.xml" 2>&1 | tee "$REPORT_DIR/browser.log"
.venv/bin/python scripts/box/check_results.py "$REPORT_DIR/browser.xml"
.venv/bin/computeruse doctor | tee "$REPORT_DIR/doctor.log"
if [[ "${LOAD_ITERATIONS:-0}" != "0" ]]; then
  timeout -k 5 600 .venv/bin/python scripts/box/load_browser.py \
    --workers "${LOAD_WORKERS:-4}" --iterations "$LOAD_ITERATIONS" \
    --endpoint "$COMPUTERUSE_CDP_ENDPOINT" --output "$REPORT_DIR/load.json"
fi
echo "Verified. Reports: $REPORT_DIR"
