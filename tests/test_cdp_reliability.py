"""Hermetic CDP fault, concurrency, target-isolation, and retention regressions."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest

from computeruse.drivers import _cdp, browser
from computeruse.schema import ComputerUseError, ErrorCode
from tests.test_browser import ScriptedTransport, _CDPError, _driver_on, _fixture_responder


class FaultTransport:
    def __init__(self, outcome: str | Exception) -> None:
        self.outcome = outcome
        self.sent: list[dict] = []
        self.close_count = 0

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self, timeout: float | None = None) -> str:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def close(self) -> None:
        self.close_count += 1


@pytest.mark.parametrize("outcome", ["", "not json", "[]", OSError("disconnected"),
                                     '{"id":1,"result":[]}', '{"id":1,"error":false}'])
def test_broken_cdp_is_structured_and_closed_once(outcome: str | Exception) -> None:
    transport = FaultTransport(outcome)
    session = _cdp.CDPSession(transport)
    with pytest.raises(ComputerUseError) as error:
        session.call("Page.navigate")
    assert error.value.code is ErrorCode.APP_NOT_FOUND
    assert error.value.detail["outcome_unknown"] is True
    assert session.closed
    session.close()
    with pytest.raises(ComputerUseError):
        session.call("Page.navigate")
    assert len(transport.sent) == 1 and transport.close_count == 1


def test_socket_timeout_is_not_retried_and_late_reply_is_skipped() -> None:
    class LateTransport(FaultTransport):
        def send(self, payload: str) -> None:
            super().send(payload)
            self.replies = deque([
                json.dumps({"id": 1, "result": {"old": True}}),
                json.dumps({"id": 2, "result": {"new": True}}),
            ])

        def recv(self, timeout: float | None = None) -> str:
            if len(self.sent) == 1:
                raise TimeoutError("timed out")
            return self.replies.popleft()

    transport = LateTransport("")
    session = _cdp.CDPSession(transport)
    with pytest.raises(ComputerUseError) as error:
        session.call("Input.insertText", {"text": "once"})
    assert error.value.code is ErrorCode.TIMEOUT
    assert error.value.detail["outcome_unknown"] is True
    assert len(transport.sent) == 1
    assert session.call("Runtime.evaluate") == {"new": True}


def test_concurrent_cdp_commands_cannot_consume_another_callers_reply() -> None:
    class InFlightTransport:
        pending: dict | None = None

        def send(self, payload: str) -> None:
            assert self.pending is None, "commands overlapped on one socket"
            self.pending = json.loads(payload)

        def recv(self, timeout: float | None = None) -> str:
            time.sleep(0.001)  # release the GIL while other workers try to send
            pending, self.pending = self.pending, None
            return json.dumps({"id": pending["id"], "result": pending["params"]})

        def close(self) -> None:
            pass

    session = _cdp.CDPSession(InFlightTransport())
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda i: session.call("Echo", {"i": i}), range(128)))
    assert results == [{"i": i} for i in range(128)]


def test_queued_command_times_out_without_sending() -> None:
    receiving = threading.Event()
    release = threading.Event()

    class BlockingTransport(FaultTransport):
        def recv(self, timeout: float | None = None) -> str:
            receiving.set()
            assert release.wait(2)
            return '{"id":1,"result":{}}'

    transport = BlockingTransport("")
    session = _cdp.CDPSession(transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(session.call, "First")
        try:
            assert receiving.wait(1)
            with pytest.raises(ComputerUseError) as error:
                session.call("MustNotSend", timeout=0.01)
            assert error.value.code is ErrorCode.TIMEOUT
            assert error.value.detail["sent"] is False
            assert error.value.detail["outcome_unknown"] is False
            assert len(transport.sent) == 1
        finally:
            release.set()
        active.result(timeout=1)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_cdp_invalid_timeout_never_sends(timeout: float) -> None:
    transport = FaultTransport("")
    with pytest.raises(ValueError):
        _cdp.CDPSession(transport).call("Input.insertText", timeout=timeout)
    assert not transport.sent


def test_event_retention_is_bounded_by_count_and_serialized_size() -> None:
    class EventsTransport(ScriptedTransport):
        def send(self, payload: str) -> None:
            super().send(payload)
            events = [json.dumps({"method": "Log.entryAdded", "params": {"text": "x" * 100, "i": i}})
                      for i in range(20)]
            self._inbox[:0] = events

    session = _cdp.CDPSession(EventsTransport(lambda m, p: {}), event_buffer=3, event_bytes=400)
    session.call("Tick")
    assert len(session._events) <= 3 and session._event_bytes <= 400
    events = session.drain_events()
    assert events[-1]["params"]["i"] == 19
    assert session.dropped_events == 20 - len(events)
    assert session._event_bytes == 0 and not session._event_sizes


def test_oversized_cdp_message_closes_connection_before_json_parse() -> None:
    transport = FaultTransport("x" * 21)
    with pytest.raises(ComputerUseError, match="max_message_size"):
        _cdp.CDPSession(transport, max_message_size=20).call("Huge")
    assert transport.close_count == 1


def test_missing_explicit_tab_never_falls_back_to_another(monkeypatch) -> None:
    monkeypatch.setattr(_cdp, "page_targets", lambda endpoint: [
        {"id": "OTHER", "webSocketDebuggerUrl": "ws://other"}])
    monkeypatch.setattr(_cdp, "connect", lambda url: pytest.fail("connected to the wrong tab"))
    driver = browser.BrowserDriver(target_id="MISSING")
    with pytest.raises(ComputerUseError) as error:
        driver.ensure_trusted()
    assert error.value.code is ErrorCode.APP_NOT_FOUND
    assert driver._target_id == "MISSING" and driver._session is None


def test_failed_domain_initialization_closes_and_does_not_cache_session(monkeypatch) -> None:
    transport = FaultTransport(TimeoutError("hung"))
    monkeypatch.setattr(_cdp, "page_targets", lambda endpoint: [
        {"id": "TAB", "webSocketDebuggerUrl": "ws://tab"}])
    monkeypatch.setattr(_cdp, "connect", lambda url: transport)
    driver = browser.BrowserDriver()
    with pytest.raises(ComputerUseError):
        driver.ensure_trusted()
    assert transport.close_count == 1 and driver._session is None


def test_close_discards_previous_tabs_console_and_network(monkeypatch) -> None:
    driver, _ = _driver_on(lambda m, p: {})
    driver._console.append({"text": "secret from TAB1"})
    driver._network.append({"url": "/TAB1"})
    driver._net_pending["pending"] = {"url": "/TAB1"}
    monkeypatch.setattr(_cdp, "page_targets", lambda endpoint: [
        {"id": "TAB2", "webSocketDebuggerUrl": "ws://tab2"}])
    monkeypatch.setattr(_cdp, "connect", lambda url: ScriptedTransport(lambda m, p: {}))
    driver.activate_app("TAB2")
    assert driver.console_messages() == []
    assert driver.network_requests() == []
    assert driver._net_pending == {}


def test_feeds_remain_bounded_when_only_one_feed_is_read() -> None:
    driver, _ = _driver_on(lambda m, p: {})
    for batch in range(4):
        driver._session._events.extend([
            {"method": "Runtime.consoleAPICalled", "params": {"args": [{"value": "x" * 6000}]}},
            {"method": "Network.responseReceived", "params": {"response": {"url": "/x", "status": 200}}},
        ] * browser._FEED_LIMIT)
        driver.console_messages(clear=False)
    assert len(driver._console) == len(driver._network) == browser._FEED_LIMIT
    assert len(driver._console[0]["text"]) == browser._FEED_TEXT_LIMIT
    driver.console_messages()
    driver.network_requests()
    assert driver._console.maxlen == driver._network.maxlen == browser._FEED_LIMIT


@pytest.mark.parametrize("failure", [None, "protocol", "javascript"])
def test_dom_action_releases_remote_object_even_on_failure(failure: str | None) -> None:
    def responder(method: str, params: dict) -> dict:
        if method == "Runtime.callFunctionOn" and failure == "protocol":
            raise _CDPError("detached")
        if method == "Runtime.callFunctionOn" and failure == "javascript":
            return {"exceptionDetails": {"text": "this.click is not a function"}}
        return _fixture_responder(method, params)

    driver, transport = _driver_on(responder)
    if failure:
        with pytest.raises(ComputerUseError):
            driver._call_on(101, "function(){this.click()}")
    else:
        assert driver._call_on(101, "function(){this.click()}")
    assert transport.sent[-1] == ("Runtime.releaseObject", {"objectId": "obj-101"})


def test_navigation_timeout_is_an_error_and_respects_cdp_budget() -> None:
    driver, _ = _driver_on(lambda m, p: {"result": {"value": "loading"}})
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as error:
        driver.navigate("https://slow.example", timeout_s=0.02)
    assert error.value.code is ErrorCode.TIMEOUT
    assert time.monotonic() - started < 0.5


def test_navigation_does_not_accept_old_complete_document() -> None:
    loaders = iter(["OLD", "NEW"])

    def responder(method: str, params: dict) -> dict:
        if method == "Page.navigate":
            return {"frameId": "F", "loaderId": "NEW"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "F", "loaderId": next(loaders)}}}
        return {"result": {"value": "complete"}}

    driver, transport = _driver_on(responder)
    driver.navigate("https://new.example")
    assert transport.methods() == ["Page.navigate", "Page.getFrameTree", "Page.getFrameTree", "Runtime.evaluate"]


def test_inaccessible_frames_consume_the_frame_budget() -> None:
    def responder(method: str, params: dict) -> dict:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "ROOT"}, "childFrames": [
                {"frame": {"id": str(i)}} for i in range(1000)]}}
        if method == "DOM.getFrameOwner":
            raise _CDPError("out-of-process frame")
        return {"nodes": []}

    driver, transport = _driver_on(responder)
    assert len(driver._collect_frames(driver._session)) == 1
    assert transport.methods().count("DOM.getFrameOwner") == driver._MAX_FRAMES - 1


def test_frame_timeout_stops_snapshot_instead_of_repeating_for_every_frame() -> None:
    def responder(method: str, params: dict) -> dict:
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "ROOT"}, "childFrames": [
                {"frame": {"id": str(i)}} for i in range(10)]}}
        if method == "DOM.getFrameOwner":
            raise TimeoutError("browser stopped responding")
        return {"nodes": []}

    driver, transport = _driver_on(responder)
    with pytest.raises(ComputerUseError) as error:
        driver._collect_frames(driver._session)
    assert error.value.code is ErrorCode.TIMEOUT
    assert transport.methods().count("DOM.getFrameOwner") == 1


def test_feed_timeout_is_not_reported_as_an_empty_success() -> None:
    driver, _ = _driver_on(lambda m, p: (_ for _ in ()).throw(TimeoutError("stalled")))
    with pytest.raises(ComputerUseError) as error:
        driver.console_messages()
    assert error.value.code is ErrorCode.TIMEOUT


@pytest.mark.parametrize("reply", [{}, {"result": {"value": None}},
                                    {"result": {"value": False}, "exceptionDetails": {"text": "failed"}}])
def test_unreadable_password_probe_never_inserts_text(reply: dict) -> None:
    driver, transport = _driver_on(lambda m, p: reply)
    with pytest.raises(ComputerUseError):
        driver.type_text("private")
    assert "Input.insertText" not in transport.methods()
