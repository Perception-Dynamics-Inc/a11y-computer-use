"""RemoteRuntime: the agent loop's tools served by a remote MCP server."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from a11y_computer_use import remote
from a11y_computer_use.schema import ComputerUseError, ErrorCode


def test_parse_error_text_maps_codes_and_strips_the_mcp_prefix() -> None:
    code, msg, detail = remote.parse_error_text(
        'Error executing tool click: stale_ref: ref e7 no longer resolves | detail: {"reason": "title_changed"} | hint: re-observe')
    assert code is ErrorCode.STALE_REF and msg.startswith("ref e7") and detail == {"reason": "title_changed"}
    code, msg, _ = remote.parse_error_text("Error executing tool type: needs_permission: X has no grant")
    assert code is None and msg.startswith("needs_permission:")
    assert remote.parse_error_text("plain text")[0] is None


def test_remote_runtime_lists_tools_and_returns_refusals_as_text(tmp_path, monkeypatch) -> None:
    """A local `a11y-computer-use mcp` stands in for the remote machine."""
    monkeypatch.setenv("HOME", str(tmp_path))  # isolated grants: everything is refused
    rt = remote.RemoteRuntime([sys.executable, "-m", "a11y_computer_use", "mcp"], start_timeout_s=60)
    try:
        names = {t["name"] for t in rt.remote_tool_specs()}
        assert {"desktop_snapshot", "click", "type", "notes", "wait_until"} <= names
        assert all("input_schema" in t for t in rt.remote_tool_specs())
        out = rt.call_tool("desktop_snapshot", {"app": "com.example.nothing"})
        assert isinstance(out, str) and ("needs_permission" in out or "app_not_found" in out)
        assert rt._frontmost() == "unknown" or isinstance(rt._frontmost(), str)
        assert rt._resolve_app("x") == (None, "x")
        assert rt.driver.name.startswith("remote:")
    finally:
        rt.close()


def test_remote_runtime_reports_a_server_that_never_starts() -> None:
    with pytest.raises(ComputerUseError) as info:
        remote.RemoteRuntime([sys.executable, "-c", "import sys; sys.exit(3)"], start_timeout_s=10)
    assert info.value.code is ErrorCode.UNSUPPORTED


def test_agent_tool_specs_prefers_the_remote_list() -> None:
    from a11y_computer_use import agent

    class _RT:
        def remote_tool_specs(self):
            return [{"name": "click", "description": "", "input_schema": {"type": "object"}}]

    names = [t["name"] for t in agent.tool_specs(_RT())]
    assert names == ["click", "done"]
