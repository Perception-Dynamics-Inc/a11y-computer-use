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
        cancelled = _payload(await client.call_tool("cancel_run", {"run_id": first["id"]}))
        assert cancelled["cancel"] is True
