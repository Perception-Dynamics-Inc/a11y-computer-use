"""Runtime ownership, bounded MCP admission and deterministic cleanup.

These tests use blocking events to force the races without a native desktop.
The MCP load test uses the actual in-memory protocol transport.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from computeruse import safety, server
from computeruse.schema import ComputerUseError, ErrorCode, Scope, Snapshot
from tests.conftest import build_synthetic_snapshot


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class BlockingDriver:
    resolves_apps = True

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.calls: list[str] = []
        self.close_count = 0
        self.snapshot_count = 0

    def frontmost_app(self) -> tuple[str, int]:
        return "test-app", 1

    def ensure_trusted(self) -> None:
        pass

    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        self.snapshot_count += 1
        return build_synthetic_snapshot()

    def type_text(self, text: str) -> None:
        self.calls.append(text)
        if text == "block":
            self.started.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("test did not release blocked driver")

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture
def runtime(tmp_path: Path):
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("test-app", safety.Tier.FULL)
    driver = BlockingDriver()
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    try:
        yield rt
    finally:
        driver.release.set()
        rt.close()


async def wait_for_driver(driver: BlockingDriver) -> None:
    with anyio.fail_after(3):
        while not driver.started.is_set():
            await anyio.sleep(0.001)


def test_batch_owns_snapshot_until_all_steps_complete(runtime) -> None:
    driver = runtime.driver
    original = build_synthetic_snapshot()
    runtime._current = original
    with ThreadPoolExecutor(max_workers=1) as pool:
        batch = pool.submit(runtime.act_batch, [
            {"do": "type", "text": "block"},
            {"do": "type", "text": "second"},
        ])
        try:
            assert driver.started.wait(timeout=3)
            with pytest.raises(ComputerUseError) as blocked:
                runtime.desktop_snapshot("test-app")
            assert blocked.value.code is ErrorCode.BUSY
            assert blocked.value.detail["retryable"] is True
            assert driver.snapshot_count == 0
            assert runtime._current is original
        finally:
            driver.release.set()
        assert all(step["ok"] for step in json.loads(batch.result(timeout=3)))
    assert driver.calls == ["block", "second"]
    runtime.desktop_snapshot("test-app")
    assert driver.snapshot_count == 1


def test_close_waits_for_active_input_and_is_idempotent(runtime) -> None:
    driver = runtime.driver
    closing = Event()

    def close() -> None:
        closing.set()
        runtime.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(runtime.type_text, "block")
        try:
            assert driver.started.wait(timeout=3)
            cleanup = pool.submit(close)
            assert closing.wait(timeout=3)
            assert not cleanup.done()
            assert driver.close_count == 0
        finally:
            driver.release.set()
        action.result(timeout=3)
        cleanup.result(timeout=3)
    runtime.close()
    assert driver.close_count == 1
    assert runtime._current is None
    with pytest.raises(ComputerUseError) as closed:
        runtime.type_text("never")
    assert closed.value.code is ErrorCode.CLOSED
    assert driver.calls == ["block"]


def test_operation_failure_releases_runtime(runtime, monkeypatch) -> None:
    def fail(_text: str) -> None:
        raise ComputerUseError(ErrorCode.TIMEOUT, "driver timed out")

    with monkeypatch.context() as patch:
        patch.setattr(runtime.driver, "type_text", fail)
        with pytest.raises(ComputerUseError):
            runtime.type_text("failed")
    assert runtime.type_text("after") == "typed 5 characters"


@pytest.mark.anyio
async def test_mcp_load_sheds_excess_calls_and_keeps_protocol_responsive(runtime) -> None:
    driver = runtime.driver
    mcp = server.build_server(runtime=runtime, max_pending_calls=4)
    rejected = anyio.Event()
    results = []
    async with create_connected_server_and_client_session(mcp) as client:
        async def call(text: str) -> None:
            result = await client.call_tool("type", {"text": text})
            results.append(result)
            if sum(item.isError for item in results) == 20:
                rejected.set()

        async with anyio.create_task_group() as group:
            group.start_soon(call, "block")
            try:
                await wait_for_driver(driver)
                for i in range(23):
                    group.start_soon(call, f"queued-{i}")
                with anyio.fail_after(3):
                    await rejected.wait()
                    assert (await client.list_tools()).tools
                assert driver.calls == ["block"]
            finally:
                driver.release.set()
    assert len(results) == 24
    assert sum(not item.isError for item in results) == 4
    assert all("busy:" in item.content[0].text for item in results if item.isError)
    assert len(driver.calls) == 4
    assert driver.close_count == 0  # externally supplied Runtime stays caller-owned


@pytest.mark.anyio
async def test_expired_queue_call_never_reaches_driver(runtime) -> None:
    driver = runtime.driver
    mcp = server.build_server(runtime=runtime, queue_timeout_s=0.02)
    async with anyio.create_task_group() as group:
        group.start_soon(mcp.call_tool, "type", {"text": "block"})
        try:
            await wait_for_driver(driver)
            with pytest.raises(ToolError, match="busy: timed out waiting"):
                await mcp.call_tool("type", {"text": "expired"})
        finally:
            driver.release.set()
    await mcp.call_tool("type", {"text": "after"})
    assert driver.calls == ["block", "after"]


@pytest.mark.anyio
async def test_cancelled_queued_call_releases_admission_without_input(runtime) -> None:
    driver = runtime.driver
    mcp = server.build_server(runtime=runtime, max_pending_calls=2)
    async with anyio.create_task_group() as group:
        group.start_soon(mcp.call_tool, "type", {"text": "block"})
        try:
            await wait_for_driver(driver)
            with anyio.move_on_after(0.02) as cancelled:
                await mcp.call_tool("type", {"text": "cancelled"})
            assert cancelled.cancel_called
        finally:
            driver.release.set()
    await mcp.call_tool("type", {"text": "after"})
    assert driver.calls == ["block", "after"]


@pytest.mark.anyio
async def test_active_cancellation_keeps_input_ownership_until_worker_finishes(runtime) -> None:
    driver = runtime.driver
    mcp = server.build_server(runtime=runtime, max_pending_calls=1)
    scope = anyio.CancelScope()
    finished = anyio.Event()

    async def active() -> None:
        with scope:
            await mcp.call_tool("type", {"text": "block"})
        finished.set()

    async with anyio.create_task_group() as group:
        group.start_soon(active)
        try:
            await wait_for_driver(driver)
            scope.cancel()
            await anyio.sleep(0)
            assert not finished.is_set()
            with pytest.raises(ToolError, match="busy:.*queue is full"):
                await mcp.call_tool("type", {"text": "overlap"})
        finally:
            driver.release.set()
    await mcp.call_tool("type", {"text": "after"})
    assert driver.calls == ["block", "after"]


@pytest.mark.anyio
async def test_server_closes_only_runtime_it_created(tmp_path, monkeypatch) -> None:
    driver = BlockingDriver()
    monkeypatch.setattr(server.drivers, "get_driver", lambda: driver)
    mcp = server.build_server(
        store=safety.PermissionStore(tmp_path / "permissions.json"),
        audit=safety.AuditLog(tmp_path / "audit"),
    )
    async with create_connected_server_and_client_session(mcp) as client:
        assert (await client.list_tools()).tools
    assert driver.close_count == 1


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("-inf"), float("nan")])
def test_invalid_wait_timeout_fails_before_driver(runtime, timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        runtime.wait_for("e1", timeout_s=timeout)
    assert runtime.driver.calls == []


@pytest.mark.parametrize("count", [-1, 101, 1.5, True])
def test_scroll_count_is_bounded_before_observation(runtime, count) -> None:
    with pytest.raises(ValueError, match="max_scrolls"):
        runtime.scroll_to_find("test-app", text="target", max_scrolls=count)
    assert runtime.driver.snapshot_count == 0


def test_oversized_batch_fails_before_executing_first_step(runtime) -> None:
    with pytest.raises(ValueError, match="at most 100"):
        runtime.act_batch([{"do": "type", "text": "never"}] * 101)
    assert runtime.driver.calls == []


def test_malformed_batch_step_retains_receipts_for_completed_steps(runtime) -> None:
    result = json.loads(runtime.act_batch([
        {"do": "type", "text": "first"},
        {"do": "click", "modifiers": 42},
        {"do": "type", "text": "never"},
    ]))
    assert [step["ok"] for step in result] == [True, False]
    assert runtime.driver.calls == ["first"]


def test_batch_stops_at_deadline_without_executing_more_input(runtime, monkeypatch) -> None:
    ticks = iter([0.0, 0.0, server.MAX_BATCH_DURATION_S + 1])
    monkeypatch.setattr(server, "time", SimpleNamespace(
        monotonic=lambda: next(ticks), perf_counter=server.time.perf_counter,
    ))
    result = json.loads(runtime.act_batch([
        {"do": "type", "text": "first"},
        {"do": "type", "text": "never"},
    ]))
    assert [step["ok"] for step in result] == [True, False]
    assert "batch time budget exhausted" in result[-1]["error"]
    assert runtime.driver.calls == ["first"]


def test_batch_wait_uses_remaining_time_budget(runtime, monkeypatch) -> None:
    ticks = iter([0.0, server.MAX_BATCH_DURATION_S - 2])
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    timeouts = []
    monkeypatch.setattr(runtime, "wait_for", lambda ref, condition, timeout: timeouts.append(timeout) or "ok")
    result = json.loads(runtime.act_batch([{"do": "wait_for", "ref": "e1", "timeout_s": 50}]))
    assert result[0]["ok"] is True
    assert timeouts == [2.0]


@pytest.mark.parametrize("kwargs", [
    {"max_pending_calls": 0}, {"max_pending_calls": True}, {"max_pending_calls": 1.5},
    {"queue_timeout_s": 0}, {"queue_timeout_s": float("inf")}, {"queue_timeout_s": float("nan")},
])
def test_invalid_queue_limits_are_rejected(runtime, kwargs) -> None:
    with pytest.raises(ValueError):
        server.build_server(runtime=runtime, **kwargs)
