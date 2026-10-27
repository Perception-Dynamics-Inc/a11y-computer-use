"""The CI apt helper survives a stalled mirror (.github/scripts/apt-retry.sh).

A fake ``apt-get`` on PATH records every call; the helper runs with sudo
disabled and a one-second phase timeout so the stall is real but short.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "apt-retry.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("timeout"),
    reason="needs bash and GNU timeout (the Linux CI job)",
)

SHIM = """#!/usr/bin/env bash
echo "$*" >> "$APT_SHIM_LOG"
case "$*" in
  *--download-only*)
    n=$(grep -c -- --download-only "$APT_SHIM_LOG")
    case "$APT_SHIM_MODE" in
      hang_first) [ "$n" -eq 1 ] && exec sleep 30 ;;
      fail) exit 100 ;;
    esac ;;
esac
exit 0
"""


def _run(tmp_path, mode, *args, attempts="3"):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "apt-get"
    shim.write_text(SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    sources = tmp_path / "ubuntu.sources"
    sources.write_text("URIs: http://azure.archive.ubuntu.com/ubuntu/\n")
    log = tmp_path / "apt.log"
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "APT_SHIM_LOG": str(log), "APT_SHIM_MODE": mode,
        "APT_RETRY_SUDO": "", "APT_RETRY_PHASE_TIMEOUT": "1", "APT_RETRY_PAUSE": "0",
        "APT_RETRY_ATTEMPTS": attempts, "APT_RETRY_SOURCES": str(sources),
    }
    done = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return done, calls, sources.read_text()


def test_a_healthy_install_downloads_then_installs_without_touching_the_mirror(tmp_path):
    done, calls, sources = _run(tmp_path, "ok", "install", "xvfb", "openbox")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(calls) == 2 and "--download-only" in calls[0] and "--download-only" not in calls[1]
    assert calls[1].endswith("install -y --no-install-recommends xvfb openbox")
    assert "azure.archive.ubuntu.com" in sources


def test_a_stalled_download_is_killed_retried_on_another_mirror_then_installed(tmp_path):
    done, calls, sources = _run(tmp_path, "hang_first", "install", "evince")
    assert done.returncode == 0, done.stdout + done.stderr
    downloads = [c for c in calls if "--download-only" in c]
    assert len(downloads) == 2
    assert calls[-1].endswith("install -y --no-install-recommends evince") and "--download-only" not in calls[-1]
    assert "azure.archive.ubuntu.com" not in sources and "//archive.ubuntu.com" in sources
    assert "attempt 2/3" in done.stdout


def test_a_mirror_that_never_answers_fails_the_step_without_running_dpkg(tmp_path):
    done, calls, _ = _run(tmp_path, "fail", "install", "geany", attempts="2")
    assert done.returncode == 1
    assert "still failing after 2 attempts" in done.stdout
    assert not [c for c in calls if " install " in c and "--download-only" not in c]


def test_update_is_retried_the_same_way(tmp_path):
    done, calls, _ = _run(tmp_path, "ok", "update")
    assert done.returncode == 0 and len(calls) == 1 and calls[0].endswith("update")
