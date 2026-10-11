"""Hermetic tests for ``a11y-agent mcp``.

The in-memory MCP client talks to ``build_agent_mcp``. The desktop MCP tool
list in ``tests/test_server.py`` is not modified.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as client_session

from a11y_computer_use import __version__
from a11y_computer_use.agent.mcp_server import build_agent_mcp
from a11y_computer_use.agent.models.base import ModelTurn, ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.agent.service import RunStore
from tests.test_agent_core import FakeRuntime, el, window
from tests.test_server import EXPECTED_TOOLS

pytestmark = pytest.mark.anyio

AGENT_TOOLS = {"run_goal", "get_run", "cancel_run", "approve"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _restore_display():
    """Agent(display=...) writes $DISPLAY. Put the suite's display back."""
    saved = os.environ.get("DISPLAY")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = saved


def _done() -> ModelTurn:
    return ModelTurn(calls=[ToolCall(
        "done",
        {"answer": "saved", "conditions": [{"element": {"role": "AXButton", "name": "Save"}}]},
    )])


def _store(tmp_path: Path, model, *, approval_timeout_s: float = 5.0) -> RunStore:
    runtime = FakeRuntime(window(el("e2", "AXButton", "Save", parent="e1", clickable=True)))
    return RunStore(
        approval_timeout_s=approval_timeout_s,
        runtime_factory=lambda: runtime,
        model_factory=lambda _spec: model,
        trace_root=tmp_path / "traces",
    )


def _plain(text: str) -> str:
    from a11y_computer_use.untrusted import unwrap

    inner = unwrap(text)
    return text if inner is None else inner


def _payload(result) -> dict:
    assert not result.isError, result
    text = result.content[0].text
    return json.loads(text)


def _error_text(result) -> str:
    assert result.isError, result
    return " ".join(getattr(block, "text", "") for block in result.content)


async def test_agent_mcp_tools_are_separate_from_the_desktop_server(tmp_path: Path) -> None:
    server = build_agent_mcp(_store(tmp_path, ScriptedModel([_done()])))
    options = server._mcp_server.create_initialization_options()
    assert options.server_name == "a11y-agent"
    assert options.server_version == __version__
    async with client_session(server) as client:
        listed = (await client.list_tools()).tools
    names = {tool.name for tool in listed}
    assert names == AGENT_TOOLS
    assert names.isdisjoint(EXPECTED_TOOLS)
    for tool in listed:
        assert tool.description


async def test_run_goal_and_get_run(tmp_path: Path) -> None:
    server = build_agent_mcp(_store(tmp_path, ScriptedModel([_done()])))
    async with client_session(server) as client:
        started = _payload(await client.call_tool("run_goal", {"goal": "save", "model": "scripted:unused"}))
        result = {}
        for _ in range(50):
            result = _payload(await client.call_tool("get_run", {"run_id": started["id"]}))
            if result.get("status") != "running":
                break
            await asyncio.sleep(0.02)
        assert result["status"] == "success"
        assert _plain(result["answer"]) == "saved"
        missing = await client.call_tool("get_run", {"run_id": "missing"})
        assert missing.isError
        assert "404" in _error_text(missing)


async def test_approve_and_display_conflict(tmp_path: Path) -> None:
    turns = [
        ModelTurn(calls=[ToolCall("app", {"action": "quit", "name": "Demo"})]),
        _done(),
    ]
    server = build_agent_mcp(_store(tmp_path, ScriptedModel(turns), approval_timeout_s=8))
    async with client_session(server) as client:
        first = _payload(await client.call_tool("run_goal", {
            "goal": "quit",
            "model": "scripted:unused",
            "display": ":1",
        }))
        view = {}
        for _ in range(50):
            view = _payload(await client.call_tool("get_run", {"run_id": first["id"]}))
            if view.get("pending_approvals"):
                break
            await asyncio.sleep(0.02)
        assert view.get("pending_approvals"), view
        busy = await client.call_tool("run_goal", {
            "goal": "again",
            "model": "scripted:unused",
            "display": ":1",
        })
        assert busy.isError
        assert "409" in _error_text(busy)
        approval_id = view["pending_approvals"][0]["approval_id"]
        answered = _payload(await client.call_tool("approve", {
            "run_id": first["id"],
            "approval_id": approval_id,
            "approve": False,
        }))
        assert answered["approve"] is False
        result = {}
        for _ in range(50):
            result = _payload(await client.call_tool("get_run", {"run_id": first["id"]}))
            if result.get("status") != "running":
                break
            await asyncio.sleep(0.02)
        assert result["status"] == "success"
        assert result["step_log"][0]["turn_stop"] == "refusal"
        assert view["pending_approvals"][0]["target"]["reason"] == "quit"
        assert "Demo" in _plain(view["pending_approvals"][0]["target"]["name"])
        assert "<untrusted nonce=" in view["pending_approvals"][0]["target"]["name"]
        cancelled = _payload(await client.call_tool("cancel_run", {"run_id": first["id"]}))
        assert cancelled["cancel"] is True


async def test_mcp_approve_shows_the_same_payment_target(tmp_path: Path) -> None:
    elements = window(
        el("e2", "AXButton", "Pay now", parent="e1", clickable=True),
        title="Checkout - Google Chrome",
    )
    runtime = FakeRuntime(elements)
    runtime.current_document_url = lambda: "http://127.0.0.1:9/checkout"  # type: ignore[attr-defined]
    turns = [
        ModelTurn(calls=[ToolCall("click", {"ref": "e2"})]),
        _done(),
    ]
    stopped = build_agent_mcp(RunStore(
        approval_timeout_s=8,
        runtime_factory=lambda: runtime,
        model_factory=lambda _spec: ScriptedModel(turns),
        trace_root=tmp_path / "stopped",
    ))
    async with client_session(stopped) as client:
        started = _payload(await client.call_tool("run_goal", {
            "goal": "buy",
            "model": "scripted:unused",
            "display": ":3",
        }))
        view = {}
        for _ in range(50):
            view = _payload(await client.call_tool("get_run", {"run_id": started["id"]}))
            if view.get("status") != "running":
                break
            await asyncio.sleep(0.02)
        assert view["status"] == "needs_human"
        assert view["needs_human"]["kind"] == "payment"
        assert "Pay now" in _plain(view["needs_human"]["message"])
        assert "http://127.0.0.1:9/checkout" in _plain(view["needs_human"]["message"])
        assert "reason=payment" in _plain(view["needs_human"]["message"])
        assert runtime.calls == []

    opted_runtime = FakeRuntime(elements)
    opted_runtime.current_document_url = lambda: "http://127.0.0.1:9/checkout"  # type: ignore[attr-defined]
    server = build_agent_mcp(RunStore(
        approval_timeout_s=8,
        runtime_factory=lambda: opted_runtime,
        model_factory=lambda _spec: ScriptedModel([
            ModelTurn(calls=[ToolCall("click", {"ref": "e2"})]),
            ModelTurn(calls=[ToolCall(
                "done",
                {"answer": "held", "conditions": [{"element": {"role": "AXButton", "name": "Pay now"}}]},
            )]),
        ]),
        trace_root=tmp_path / "opted",
    ))
    async with client_session(server) as client:
        started = _payload(await client.call_tool("run_goal", {
            "goal": "buy",
            "model": "scripted:unused",
            "display": ":4",
            "allow_payments": True,
        }))
        view = {}
        for _ in range(50):
            view = _payload(await client.call_tool("get_run", {"run_id": started["id"]}))
            if view.get("pending_approvals"):
                break
            await asyncio.sleep(0.02)
        pending = view["pending_approvals"][0]
        target = pending["target"]
        assert pending["reason"] == "payment"
        assert target["role"] == "AXButton"
        assert target["reason"] == "payment"
        assert _plain(target["name"]) == "Pay now"
        assert "Checkout" in _plain(target["window"])
        assert _plain(target["url"]) == "http://127.0.0.1:9/checkout"
        assert "<untrusted nonce=" in target["name"]
        answered = _payload(await client.call_tool("approve", {
            "run_id": started["id"],
            "approval_id": pending["approval_id"],
            "approve": False,
        }))
        assert answered["approve"] is False
        assert opted_runtime.calls == []


def test_run_store_rejects_an_unknown_body_field(tmp_path: Path) -> None:
    """The body ``run_goal`` hands to ``RunStore.start`` rejects unknown keys.

    The model factory is not called and no run is stored. Unknown keys on the
    MCP tool arguments themselves are sealed when the desktop argument check
    lands; this is the shared service body.
    """
    built: list[str] = []
    store = RunStore(
        model_factory=lambda spec: built.append(spec) or ScriptedModel([_done()]),
        trace_root=tmp_path / "traces",
    )
    with pytest.raises(ValueError, match=r"invalid_arguments: unknown field 'ref'") as caught:
        store.start({"goal": "save", "model": "scripted:unused", "ref": "e2"})
    assert "expected" in str(caught.value)
    assert "goal" in str(caught.value)
    assert built == []
    assert store._runs == {}
