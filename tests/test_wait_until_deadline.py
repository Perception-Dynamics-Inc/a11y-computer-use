"""wait_until stays inside timeout_s.

A url_status probe used to keep a fixed 10 second request timeout, and DNS
ignored it. These tests use a resolver that sleeps and a server that accepts
and never answers. file_exists, file_stable, and settle only wait in the poll
sleep; a poll_s much larger than timeout_s must not extend them.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from a11y_computer_use import conditions
from a11y_computer_use.schema import ComputerUseError, ErrorCode

#: Scheduling slack. Far below the old 10 second probe, and under a second.
_MARGIN_S = 0.75


def _assert_bounded(started: float, timeout_s: float) -> float:
    elapsed = time.monotonic() - started
    assert elapsed <= timeout_s + _MARGIN_S, elapsed
    # A probe that returns immediately did not apply the budget.
    assert elapsed >= timeout_s * 0.5, elapsed
    return elapsed


def _hanging_server() -> tuple[int, threading.Event]:
    """Accept connections and send nothing. The client has to time out."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        sock.settimeout(0.2)
        held: list[socket.socket] = []
        try:
            while not stop.is_set():
                try:
                    conn, _addr = sock.accept()
                except socket.timeout:
                    continue
                held.append(conn)
        finally:
            for conn in held:
                conn.close()
            sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return port, stop


def test_slow_resolver_stays_inside_timeout_s(monkeypatch) -> None:
    def slow(_host: str, _port: int):
        time.sleep(30)
        return []

    monkeypatch.setattr(conditions, "_getaddrinfo", slow)
    timeout_s = 0.4
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as info:
        conditions.Checker().wait(
            {"url_status": "http://nonexistent.invalid/"},
            timeout_s=timeout_s, poll_s=0.05,
        )
    _assert_bounded(started, timeout_s)
    assert info.value.code is ErrorCode.TIMEOUT
    assert "last_status" not in info.value.detail
    assert "TimeoutError" in info.value.detail["last_error"]
    assert "nonexistent.invalid" in info.value.detail["last_error"]
    assert info.value.detail["polls"] >= 1


def test_slow_server_stays_inside_timeout_s(monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS", "1")
    port, stop = _hanging_server()
    timeout_s = 0.4
    try:
        started = time.monotonic()
        with pytest.raises(ComputerUseError) as info:
            conditions.Checker().wait(
                {"url_status": f"http://127.0.0.1:{port}/slow", "status": 200},
                timeout_s=timeout_s, poll_s=0.05,
            )
        _assert_bounded(started, timeout_s)
    finally:
        stop.set()
    assert info.value.code is ErrorCode.TIMEOUT
    assert "last_status" not in info.value.detail
    error = info.value.detail["last_error"]
    assert "timed out" in error.lower() or "Timeout" in error


def test_resolved_private_address_is_still_refused(monkeypatch) -> None:
    """The refusal looks at the address the lookup returned, and does not connect."""

    def private(_host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.1.2.3", port))]

    monkeypatch.setattr(conditions, "_getaddrinfo", private)
    monkeypatch.delenv("A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS", raising=False)
    started = time.monotonic()
    with pytest.raises(ValueError, match="non-public address 10.1.2.3"):
        conditions._url_status("http://example.test/", timeout_s=2)
    assert time.monotonic() - started < 0.5

    def loopback(_host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    monkeypatch.setattr(conditions, "_getaddrinfo", loopback)
    with pytest.raises(ValueError, match="non-public address 127.0.0.1"):
        conditions._url_status("http://example.test/secret", timeout_s=2)


def test_file_and_settle_polls_stay_inside_timeout_s(tmp_path: Path, monkeypatch) -> None:
    """A poll_s of 30 must not stretch a 0.3 second wait. These conditions do
    not have a request timeout; the poll sleep is the only wait."""
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    timeout_s = 0.3
    missing = tmp_path / "missing.bin"

    started = time.monotonic()
    with pytest.raises(ComputerUseError) as missing_file:
        conditions.Checker().wait(
            {"file_exists": str(missing), "min_bytes": 5},
            timeout_s=timeout_s, poll_s=30,
        )
    _assert_bounded(started, timeout_s)
    assert missing_file.value.detail["exists"] is False
    assert missing_file.value.detail["last_size"] is None
    assert missing_file.value.detail["min_bytes"] == 5

    target = tmp_path / "render.bin"
    target.write_bytes(b"abcd")
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as stable:
        conditions.Checker().wait(
            {"file_stable": str(target), "seconds": 60, "min_bytes": 1},
            timeout_s=timeout_s, poll_s=30,
        )
    _assert_bounded(started, timeout_s)
    assert stable.value.detail["exists"] is True
    assert stable.value.detail["last_size"] == 4
    assert stable.value.detail["min_bytes"] == 1
    assert "stable_for_s" in stable.value.detail

    started = time.monotonic()
    with pytest.raises(ComputerUseError) as settle:
        conditions.Checker().wait({"settle": 60}, timeout_s=timeout_s, poll_s=30)
    _assert_bounded(started, timeout_s)
    assert settle.value.detail["settle_s"] == 60
    assert settle.value.detail["elapsed_s"] <= timeout_s + _MARGIN_S
