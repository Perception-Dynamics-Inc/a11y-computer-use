#!/usr/bin/env bash
# CI helper: apt-get update/install that survives a stalled mirror.
#
# The Linux job's install steps have a 15-minute timeout (#1185) and a stalled
# azure.archive.ubuntu.com connection has used all of it more than once (main,
# #1193, #1196). Healthy installs take 30-60 s. This keeps the step timeout as
# it is and bounds each network phase instead:
#   * the download (and `update`) phase runs under `timeout` and is retried,
#     with apt's own connect/read timeouts and Acquire::Retries;
#   * from the second attempt on it falls back from azure.archive.ubuntu.com to
#     archive.ubuntu.com;
#   * only after the packages are cached locally does dpkg run, so killing a
#     stalled download never leaves a half-installed package.
#
# usage: apt-retry.sh update
#        apt-retry.sh install PACKAGE...
set -uo pipefail

SUDO="${APT_RETRY_SUDO-sudo}"
ATTEMPTS="${APT_RETRY_ATTEMPTS:-3}"
PHASE_TIMEOUT="${APT_RETRY_PHASE_TIMEOUT:-120}"
PAUSE="${APT_RETRY_PAUSE:-5}"
SOURCES="${APT_RETRY_SOURCES:-/etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list}"
OPTS=(-o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20
      -o Dpkg::Lock::Timeout=120)

mode="${1:-}"
[ -n "$mode" ] && shift
case "$mode" in
  update) ;;
  install) [ "$#" -gt 0 ] || { echo "::error::apt-retry.sh install needs packages"; exit 2; } ;;
  *) echo "usage: apt-retry.sh update | install PACKAGE..." >&2; exit 2 ;;
esac

use_fallback_mirror() {
  # Returns 0 when it changed a source, so the caller refreshes the index (apt
  # keys its lists by host, so a new host has none until `update` runs).
  local f changed=1
  for f in $SOURCES; do
    [ -f "$f" ] || continue
    if grep -q 'azure\.archive\.ubuntu\.com' "$f"; then
      echo "apt-retry: switching $f from azure.archive.ubuntu.com to archive.ubuntu.com"
      $SUDO sed -i 's#//azure\.archive\.ubuntu\.com#//archive.ubuntu.com#g' "$f"
      changed=0
    fi
  done
  return $changed
}

network_phase() {
  # $@: the apt-get arguments of the network phase. Returns apt's status, or
  # 124 when `timeout` killed it.
  $SUDO timeout --kill-after=10 "$PHASE_TIMEOUT" apt-get "${OPTS[@]}" "$@"
}

attempt=1
while :; do
  if [ "$attempt" -gt 1 ] && use_fallback_mirror && [ "$mode" = install ]; then
    network_phase update || echo "apt-retry: index refresh on the fallback mirror failed"
  fi
  echo "apt-retry: attempt $attempt/$ATTEMPTS ($mode, ${PHASE_TIMEOUT}s per network phase)"
  if [ "$mode" = update ]; then
    network_phase update
    rc=$?
  else
    network_phase install -y --no-install-recommends --download-only "$@"
    rc=$?
    # A fast failure (a 404 on a superseded package) means a stale index: refresh
    # it. A timeout (124/137) means a stalled mirror: the next attempt switches.
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && [ "$rc" -ne 137 ]; then
      network_phase update >/dev/null 2>&1 || true
    fi
  fi
  [ "$rc" -eq 0 ] && break
  echo "apt-retry: attempt $attempt failed (status $rc)"
  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    echo "::error::apt-get $mode still failing after $ATTEMPTS attempts"
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep "$PAUSE"
done

if [ "$mode" = install ]; then
  # Everything is in /var/cache/apt/archives now: no network, so no stall.
  $SUDO apt-get "${OPTS[@]}" install -y --no-install-recommends "$@"
fi
