"""wait_until stays inside timeout_s.

A url_status probe used to keep a fixed 10 second request timeout, and DNS
ignored it. These tests use a resolver that sleeps and a server that accepts
and never answers. A later failure gave each socket operation its own copy of
the time remaining: a TLS handshake plus a read, or a server that trickles
one header byte at a time, ran past timeout_s and the trickle came back as
success. file_exists, file_stable, and settle only wait in the poll sleep; a
poll_s much larger than timeout_s must not extend them.
"""

from __future__ import annotations

import socket
import ssl
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


_CERT = Path(__file__).parent / "fixtures" / "localhost.crt"
_KEY = Path(__file__).parent / "fixtures" / "localhost.key"


def _listen() -> socket.socket:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock


def _server_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(_CERT, _KEY)
    return ctx


def _unverified_context() -> ssl.SSLContext:
    """The product verifies certificates. This test trusts only its own cert."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _accept_loop(sock: socket.socket, stop: threading.Event, handle) -> None:
    sock.settimeout(0.2)
    try:
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    finally:
        sock.close()


def _assert_url_timeout(url: str, timeout_s: float = 2.0) -> None:
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as info:
        conditions.Checker().wait(
            {"url_status": url, "status": 200},
            timeout_s=timeout_s, poll_s=0.05,
        )
    _assert_bounded(started, timeout_s)
    assert info.value.code is ErrorCode.TIMEOUT
    assert "last_status" not in info.value.detail
    error = info.value.detail["last_error"]
    assert "timed out" in error.lower() or "Timeout" in error


def test_tls_handshake_delay_stays_inside_timeout_s(monkeypatch) -> None:
    """TCP accepts and then never starts TLS. The handshake shares the deadline."""
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS", "1")
    monkeypatch.setattr(conditions, "_https_context", _unverified_context)
    sock = _listen()
    stop = threading.Event()

    def hold(conn: socket.socket) -> None:
        try:
            stop.wait(30)
        finally:
            conn.close()

    threading.Thread(target=_accept_loop, args=(sock, stop, hold), daemon=True).start()
    try:
        _assert_url_timeout(f"https://127.0.0.1:{sock.getsockname()[1]}/handshake")
    finally:
        stop.set()


def test_tls_response_delay_stays_inside_timeout_s(monkeypatch) -> None:
    """The handshake finishes. The HTTP response does not. The read shares the deadline."""
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS", "1")
    monkeypatch.setattr(conditions, "_https_context", _unverified_context)
    ctx = _server_context()
    sock = _listen()
    stop = threading.Event()

    def delay(conn: socket.socket) -> None:
        try:
            tls = ctx.wrap_socket(conn, server_side=True)
            tls.settimeout(0.5)
            try:
                tls.recv(4096)
            except OSError:
                return
            stop.wait(30)
        except OSError:
            return
        finally:
            conn.close()

    threading.Thread(target=_accept_loop, args=(sock, stop, delay), daemon=True).start()
    try:
        _assert_url_timeout(f"https://127.0.0.1:{sock.getsockname()[1]}/slow")
    finally:
        stop.set()


def test_trickle_headers_time_out_instead_of_succeeding(monkeypatch) -> None:
    """One header byte every 0.5s used to complete as HTTP 200 after ~29s."""
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS", "1")
    body = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"OK"
    )
    sock = _listen()
    stop = threading.Event()

    def trickle(conn: socket.socket) -> None:
        try:
            conn.settimeout(0.2)
            try:
                conn.recv(4096)
            except OSError:
                pass
            for byte in body:
                if stop.is_set():
                    return
                try:
                    conn.send(bytes([byte]))
                except OSError:
                    return
                if stop.wait(0.5):
                    return
        finally:
            conn.close()

    threading.Thread(target=_accept_loop, args=(sock, stop, trickle), daemon=True).start()
    try:
        _assert_url_timeout(f"http://127.0.0.1:{sock.getsockname()[1]}/trickle")
    finally:
        stop.set()


def test_local_tls_200_still_matches(monkeypatch) -> None:
    """A prompt HTTPS response is still a success. The deadline is not a blanket failure."""
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS", "1")
    monkeypatch.setattr(conditions, "_https_context", _unverified_context)
    ctx = _server_context()
    sock = _listen()
    stop = threading.Event()
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

    def answer(conn: socket.socket) -> None:
        try:
            tls = ctx.wrap_socket(conn, server_side=True)
            tls.settimeout(2)
            tls.recv(4096)
            tls.sendall(reply)
        except OSError:
            return
        finally:
            conn.close()

    threading.Thread(target=_accept_loop, args=(sock, stop, answer), daemon=True).start()
    try:
        started = time.monotonic()
        out = conditions.Checker().wait(
            {"url_status": f"https://127.0.0.1:{sock.getsockname()[1]}/ok"},
            timeout_s=5, poll_s=0.05,
        )
        assert out["matched"].endswith("returned 200")
        assert time.monotonic() - started < 4
    finally:
        stop.set()
