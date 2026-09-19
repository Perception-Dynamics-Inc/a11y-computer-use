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
        # macOS resolves the app before the gate (structured app_not_found);
        # Linux and Windows gate the ungranted app first (a refusal, as text).
        # Both keep their meaning across the wire.
        try:
            out = rt.call_tool("desktop_snapshot", {"app": "com.example.nothing"})
        except ComputerUseError as exc:
            assert exc.code in (ErrorCode.APP_NOT_FOUND, ErrorCode.PERMISSION_DENIED_ACCESSIBILITY)
        else:
            assert isinstance(out, str) and out.startswith("needs_permission"), out
        out = rt.call_tool("app", {"action": "list"})  # gated: an ungranted app is a refusal, as text
        assert isinstance(out, str) and out.startswith("needs_permission")
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


def test_wire_params_asks_for_jpeg_only_when_the_server_advertises_format() -> None:
    new = [{"name": "screenshot", "input_schema": {"properties": {"format": {}, "quality": {}}}}]
    old = [{"name": "screenshot", "input_schema": {"properties": {"max_long_edge": {}}}}]
    assert remote.wire_params("screenshot", {"max_long_edge": 1280}, new) == {"max_long_edge": 1280, "format": "jpeg"}
    assert remote.wire_params("screenshot", {"format": "png"}, new) == {"format": "png"}  # explicit choice wins
    assert remote.wire_params("screenshot", {}, old) == {}  # an older server never sees the argument
    assert remote.wire_params("click", {"ref": "e1"}, new) == {"ref": "e1"}


def test_as_png_returns_png_unchanged_and_transcodes_jpeg() -> None:
    import io

    from PIL import Image

    im = Image.new("RGB", (8, 6), (40, 120, 200))
    png, jpg = io.BytesIO(), io.BytesIO()
    im.save(png, format="PNG"); im.save(jpg, format="JPEG")
    assert remote.as_png(png.getvalue(), "image/png") == png.getvalue()
    out = remote.as_png(jpg.getvalue(), "image/jpeg")
    assert out[:8] == b"\x89PNG\r\n\x1a\n" and Image.open(io.BytesIO(out)).size == (8, 6)
