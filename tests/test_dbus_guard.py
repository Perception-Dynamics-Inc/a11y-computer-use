"""The MCP server must survive libatspi dropping a connected D-Bus socket.

LibreOffice Calc's registration makes libatspi unref a private connection
without closing it. libdbus then warns "The last reference on a connection
was dropped" and can abort the process. The hook keeps that last reference.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux dbus guard")

from a11y_computer_use.drivers import _dbus_guard  # noqa: E402
from a11y_computer_use.schema import ComputerUseError, ErrorCode  # noqa: E402
from a11y_computer_use.server import error_text  # noqa: E402

_CHILD = """
import json, os
os.environ["DBUS_FATAL_WARNINGS"] = "1"
from a11y_computer_use.drivers._dbus_guard import _exercise_private_unref
print(json.dumps(_exercise_private_unref(os.environ["DBUS_SESSION_BUS_ADDRESS"])))
"""


def _session_bus():
    """A session bus address, and a killer for a daemon this test started."""
    existing = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if existing:
        return existing, None
    if not shutil_which("dbus-daemon"):
        pytest.skip("dbus-daemon is not installed")
    proc = subprocess.run(
        ["dbus-daemon", "--session", "--fork", "--print-address=1", "--print-pid=1"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        pytest.fail(f"dbus-daemon did not print an address and pid: {proc.stdout!r}")
    return lines[0], int(lines[1])


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _reset_depth() -> None:
    _dbus_guard._TLS.depth = 0


def test_private_unref_does_not_warn_or_abort() -> None:
    """Dropping the last ref on a connected private socket must not kill us."""
    address, pid = _session_bus()
    env = os.environ.copy()
    env["DBUS_SESSION_BUS_ADDRESS"] = address
    env["DBUS_FATAL_WARNINGS"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    assert proc.returncode == 0, proc.stderr
    assert "last reference on a connection was dropped" not in proc.stderr, proc.stderr
    assert "last reference on a connection was dropped" not in proc.stdout, proc.stdout
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["held"] >= 1, payload
    info = payload["status"]
    assert info["patched"] is True, info
    assert info["refcount_ok"] is True, info
    assert "dbus_connection_unref" in info["symbols"]
    assert "dbus_connection_open_private" in info["symbols"]
    assert "dbus_bus_get" in info["symbols"]


def test_bus_exception_is_retried_once(monkeypatch) -> None:
    _reset_depth()
    monkeypatch.setattr(_dbus_guard, "install", lambda: True)
    calls = {"fn": 0, "reconnect": 0}

    def reconnect() -> bool:
        calls["reconnect"] += 1
        return True

    monkeypatch.setattr(_dbus_guard, "reconnect", reconnect)

    def fn():
        calls["fn"] += 1
        if calls["fn"] == 1:
            raise RuntimeError("dbus connection disconnected")
        return "ok"

    assert _dbus_guard.call_with_reconnect(fn) == "ok"
    assert calls == {"fn": 2, "reconnect": 1}


def test_unrelated_error_is_not_retried(monkeypatch) -> None:
    _reset_depth()
    monkeypatch.setattr(_dbus_guard, "install", lambda: True)
    monkeypatch.setattr(_dbus_guard, "reconnect", lambda: pytest.fail("reconnected"))

    def fn():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        _dbus_guard.call_with_reconnect(fn)


def test_second_bus_failure_is_a_retryable_timeout(monkeypatch) -> None:
    _reset_depth()
    monkeypatch.setattr(_dbus_guard, "install", lambda: True)
    monkeypatch.setattr(_dbus_guard, "reconnect", lambda: True)

    def fn():
        raise RuntimeError("org.a11y bus disconnected")

    with pytest.raises(ComputerUseError) as caught:
        _dbus_guard.call_with_reconnect(fn)
    exc = caught.value
    assert exc.code is ErrorCode.TIMEOUT
    assert exc.detail["reason"] == "bus_disconnected"
    assert exc.detail["retryable"] is True
    text = error_text(exc)
    assert text.startswith("timeout: the accessibility bus dropped")
    assert '"retryable": true' in text
    assert "bus_disconnected" in text


def test_failed_reconnect_is_still_retryable(monkeypatch) -> None:
    _reset_depth()
    monkeypatch.setattr(_dbus_guard, "install", lambda: True)
    monkeypatch.setattr(_dbus_guard, "reconnect", lambda: False)

    def fn():
        raise RuntimeError("dbus disconnected")

    with pytest.raises(ComputerUseError) as caught:
        _dbus_guard.call_with_reconnect(fn)
    assert caught.value.detail["reason"] == "bus_disconnected"
    assert caught.value.detail["retryable"] is True


def test_a_drop_retries_a_structured_error_once(monkeypatch) -> None:
    _reset_depth()
    monkeypatch.setattr(_dbus_guard, "install", lambda: True)
    monkeypatch.setattr(_dbus_guard, "reconnect", lambda: True)
    before = _dbus_guard._drops
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            _dbus_guard._drops += 1
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND,
                "no running application matches 'Calc'",
                detail={"app": "Calc"},
            )
        return "snap"

    try:
        assert _dbus_guard.call_with_reconnect(fn) == "snap"
    finally:
        _dbus_guard._drops = before
    assert calls["n"] == 2


def test_nested_bus_error_reconnects_once(monkeypatch) -> None:
    """snapshot() wraps _run(); only the outer call may reconnect."""
    _reset_depth()
    monkeypatch.setattr(_dbus_guard, "install", lambda: True)
    reconnects = {"n": 0}
    inners = {"n": 0}

    def reconnect() -> bool:
        reconnects["n"] += 1
        return True

    monkeypatch.setattr(_dbus_guard, "reconnect", reconnect)

    def inner():
        inners["n"] += 1
        if inners["n"] == 1:
            raise RuntimeError("dbus disconnected")
        return "in"

    def outer():
        return _dbus_guard.call_with_reconnect(inner)

    assert _dbus_guard.call_with_reconnect(outer) == "in"
    assert reconnects["n"] == 1
    assert inners["n"] == 2
