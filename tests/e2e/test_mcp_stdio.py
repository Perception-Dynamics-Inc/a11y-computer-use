"""End-to-end MCP transport tests: spawn ``computeruse mcp`` as a real
subprocess and drive initialize / tools/list / tools/call over stdio with the
official ``mcp`` client SDK.

Unlike tests/test_server.py (in-memory transport, mocked drivers), nothing is
mocked here: these assert that the shipped entry point speaks MCP end-to-end,
exposes the full PLAN.md §8 v1 tool surface with documentation, and converts
driver failures into structured tool errors instead of crashing the server.
HOME points at a pytest temp dir so the subprocess can never read or write
the real ``~/.computeruse`` state.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, InitializeResult, ListToolsResult

from tests.conftest import HAS_AX

HANDSHAKE_TIMEOUT_S = 30.0

#: PLAN.md §8: the ~12-tool front door, plus the `find` query tool.
EXPECTED_TOOLS = {
    "desktop_snapshot",
    "find",
    "screenshot",
    "zoom",
    "click",
    "type",
    "key",
    "scroll",
    "drag",
    "wait_for",
    "act",
    "set_value",
    "app",
    "window",
    "clipboard",
}


def _server_params(home: Path) -> StdioServerParameters:
    """The real entry point (``python -m computeruse mcp``), HOME-isolated."""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "computeruse", "mcp"],
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
    )


def _drive(
    home: Path, tool: str | None = None, params: dict | None = None
) -> tuple[InitializeResult, ListToolsResult, CallToolResult | None]:
    """One full client session: initialize + tools/list (+ one tools/call)."""

    async def session_body():
        async with stdio_client(_server_params(home)) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                call = await session.call_tool(tool, params or {}) if tool else None
                return init, tools, call

    async def bounded():
        return await asyncio.wait_for(session_body(), HANDSHAKE_TIMEOUT_S)

    return asyncio.run(bounded())


def test_initialize_identifies_server(tmp_path: Path) -> None:
    init, _tools, _ = _drive(tmp_path)
    assert init.serverInfo.name == "computeruse"
    # The server ships usage instructions (ref lifecycle, doctor pointer).
    assert init.instructions and "desktop_snapshot" in init.instructions


def test_tools_list_exposes_full_v1_surface(tmp_path: Path) -> None:
    _init, tools, _ = _drive(tmp_path)
    by_name = {t.name: t for t in tools.tools}
    assert set(by_name) == EXPECTED_TOOLS
    undocumented = sorted(n for n, t in by_name.items() if not (t.description or "").strip())
    assert undocumented == [], f"tools missing descriptions: {undocumented}"
    # Hosts render parameter UIs from the input schema; every tool needs one.
    assert all(t.inputSchema.get("type") == "object" for t in tools.tools)


@pytest.mark.skipif(HAS_AX, reason="AX grant present; this asserts the ungranted path")
def test_desktop_snapshot_permission_error_is_a_tool_result(tmp_path: Path) -> None:
    """Missing TCC grant surfaces as a structured tool error, not a crash."""
    _init, _tools, call = _drive(tmp_path, tool="desktop_snapshot", params={"app": "TextEdit"})
    assert call is not None and call.isError is True
    text = call.content[0].text
    assert "permission_denied_accessibility:" in text
    assert "doctor" in text, "remediation hint must point at `computeruse doctor`"


@pytest.mark.skipif(not HAS_AX, reason="requires the Accessibility TCC grant")
def test_desktop_snapshot_succeeds_when_granted(tmp_path: Path) -> None:
    """Read-only snapshot of Finder (always running; no input injected).

    HOME is isolated, so the subprocess starts with an empty grant store —
    seed a 'read' tier for Finder, otherwise the tier gate (correctly) returns
    needs_permission before AX is ever consulted.
    """
    store = tmp_path / ".computeruse" / "permissions.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"apps": {"com.apple.finder": {"tier": "read"}}}))
    _init, _tools, call = _drive(
        tmp_path, tool="desktop_snapshot", params={"app": "com.apple.finder"}
    )
    assert call is not None and call.isError is False
