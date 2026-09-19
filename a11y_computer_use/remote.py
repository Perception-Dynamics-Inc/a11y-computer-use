"""Drive a remote ``a11y-computer-use mcp`` server as the tool backend.

The reference agent loop (`agent.run_task`) only needs a handful of Runtime
methods: ``call_tool``, ``_frontmost``, ``_resolve_app``, ``audit``, ``driver``
and the tool specs. ``RemoteRuntime`` provides them by speaking MCP over stdio
to any command that starts the server, for example an SSH channel into a Linux
desktop VM::

    a11y-computer-use agent --mcp-command "box ssh bx_123 -- ~/computerUse/.venv/bin/a11y-computer-use mcp" \
        --provider claude-cli --app krita --task "..."

The planner runs here; every observation and action happens on the remote
machine under its own grants and audit log (grants cannot be set remotely: the
MCP surface has no grant tool by design). Results keep the wire format the
local Runtime uses, so the loop's stale_ref handling and Effect Receipts work
unchanged. Screenshots arrive as MCP image content and are handed back as
``(text, image)`` like the local ``screenshot`` tool.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import threading
from dataclasses import dataclass
from types import SimpleNamespace

from a11y_computer_use import safety
from a11y_computer_use.schema import ComputerUseError, ErrorCode

_CODES = {code.value: code for code in ErrorCode}
_MCP_PREFIX = re.compile(r"^Error executing tool \S+: ")


def parse_error_text(text: str) -> tuple[ErrorCode | None, str, dict]:
    """Split the wire form ``<code>: <message> | detail: {...} | hint: ...``
    (optionally wrapped by FastMCP's ``Error executing tool X: ``) into its
    parts; ``code`` is None for refusals and plain errors."""
    body = _MCP_PREFIX.sub("", text.strip(), count=1)
    head, sep, rest = body.partition(":")
    code = _CODES.get(head.strip()) if sep else None
    if code is None:
        return None, body, {}
    message, _, tail = rest.strip().partition(" | detail: ")
    return code, message.strip() or body, _parse_detail(tail)


@dataclass
class _Image:
    png: bytes


WIRE_IMAGE_FORMAT = os.environ.get("A11Y_COMPUTER_USE_REMOTE_IMAGE_FORMAT", "jpeg")


def wire_params(tool: str, params: dict[str, object], tools: list[dict]) -> dict[str, object]:
    """Ask a remote ``screenshot`` for JPEG when the server's schema advertises
    ``format`` and the caller did not choose: a 1080p PNG is about 800 KB on
    the wire and took 6 to 70 s over the Box link; the JPEG is a fifth of that.
    Older servers, other tools, and explicit choices pass through unchanged."""
    if tool != "screenshot" or "format" in params or WIRE_IMAGE_FORMAT == "png":
        return dict(params)
    spec = next((t for t in tools if t.get("name") == tool), None)
    props = ((spec or {}).get("input_schema") or {}).get("properties") or {}
    if "format" not in props:
        return dict(params)
    return {**params, "format": WIRE_IMAGE_FORMAT}


def as_png(data: bytes, mime: str) -> bytes:
    """Image bytes from the wire as PNG, so the planner side sees one format."""
    if mime == "image/png" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return data
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


class RemoteRuntime:
    """A Runtime look-alike whose tools run on a remote MCP server."""

    def __init__(self, command: str | list[str], *, audit: safety.AuditLog | None = None,
                 env: dict[str, str] | None = None, call_timeout_s: float = 600.0,
                 start_timeout_s: float = 90.0) -> None:
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        if not self.command:
            raise ValueError("--mcp-command needs a command that starts an MCP server")
        self.audit = audit if audit is not None else safety.AuditLog()
        self.driver = SimpleNamespace(name=f"remote:{self.command[0]}")
        self.notes_store = None  # notes live on the remote server (its `notes` tool)
        self.store = None  # grants live on the remote machine
        self._env = {**os.environ, **(env or {})}
        self._call_timeout_s = call_timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="remote-mcp", daemon=True)
        self._thread.start()
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._session = None
        self._tools: list[dict] = []
        self._error: BaseException | None = None
        self._serve_future = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        asyncio.run_coroutine_threadsafe(self._wait_ready(start_timeout_s), self._loop).result(
            start_timeout_s + 5)
        if self._error is not None:
            raise ComputerUseError(ErrorCode.UNSUPPORTED,
                                   f"remote MCP server did not start: {self._error}",
                                   detail={"command": self.command})

    # -- lifecycle ----------------------------------------------------------
    async def _serve(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(command=self.command[0], args=self.command[1:], env=self._env)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._tools = [
                        {"name": t.name, "description": t.description or "",
                         "input_schema": t.inputSchema or {"type": "object", "properties": {}}}
                        for t in listed.tools
                    ]
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            self._error = exc
            self._ready.set()
            raise

    async def _wait_ready(self, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout_s)
        except asyncio.TimeoutError:
            self._error = TimeoutError(f"no MCP handshake within {timeout_s:.0f}s")

    def close(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._stop.set)
            self._serve_future.result(10)
        except Exception:  # noqa: BLE001 - best effort
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    def __enter__(self) -> "RemoteRuntime":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the surface the agent loop uses ------------------------------------
    def remote_tool_specs(self) -> list[dict]:
        return list(self._tools)

    def call_tool(self, tool: str, params: dict[str, object], *, confirm=None):
        """Run ``tool`` remotely. Structured errors come back as the same
        ``ComputerUseError`` codes the local Runtime raises; refusals and plain
        results are returned as text; screenshots as ``(text, image)``."""
        if self._session is None:
            raise ComputerUseError(ErrorCode.UNSUPPORTED, "remote MCP session is not connected")

        params = wire_params(tool, params, self._tools)

        async def _call():
            return await asyncio.wait_for(self._session.call_tool(tool, dict(params)), self._call_timeout_s)

        try:
            result = asyncio.run_coroutine_threadsafe(_call(), self._loop).result(self._call_timeout_s + 5)
        except asyncio.TimeoutError as exc:
            raise ComputerUseError(ErrorCode.TIMEOUT, f"remote {tool} timed out after {self._call_timeout_s:.0f}s") from exc
        texts: list[str] = []
        image: _Image | None = None
        for block in result.content:
            kind = getattr(block, "type", "")
            if kind == "text":
                texts.append(block.text)
            elif kind == "image":
                image = _Image(as_png(base64.b64decode(block.data), getattr(block, "mimeType", "")))
        text = "\n".join(texts)
        if getattr(result, "isError", False):
            code, message, detail = parse_error_text(text)
            if code is not None:
                raise ComputerUseError(code, message, detail=detail)
            return _MCP_PREFIX.sub("", text, count=1)  # refusals read as tool text
        if image is not None:
            return text, image
        return text

    def _frontmost(self) -> str:
        try:
            rows = json.loads(str(self.call_tool("app", {"action": "list"})))
        except Exception:  # noqa: BLE001 - ungranted or unparsable: unknown
            return "unknown"
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("frontmost"):
                return str(row.get("bundle_id") or row.get("name") or "unknown")
        return "unknown"

    def _resolve_app(self, identifier: str) -> tuple[object, str]:
        return None, identifier


def _parse_detail(tail: str) -> dict:
    tail = tail.split(" | hint:")[0].strip()
    if not tail:
        return {}
    try:
        value = json.loads(tail)
        return value if isinstance(value, dict) else {"detail": value}
    except ValueError:
        return {"detail": tail}
