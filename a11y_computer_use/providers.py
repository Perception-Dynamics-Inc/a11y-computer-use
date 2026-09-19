"""Planner providers for the reference agent loop (`a11y_computer_use.agent`).

A *provider* turns the conversation so far plus the tool surface into one
planner turn: zero or more tool calls, optional assistant text, and the token
usage the API reported. The loop in `a11y_computer_use.agent` owns everything else
(observation, execution through the gated `server.Runtime`, history bounds).

Message format (provider-neutral, converted on the wire by each provider):

    {"role": "user", "content": [block, ...]}
    {"role": "assistant", "content": [block, ...], "raw": {...}}

    text block         {"type": "text", "text": "..."}
    image block        {"type": "image", "media_type": "image/png", "data": "<base64>"}
    tool_use block     {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    tool_result block  {"type": "tool_result", "tool_use_id": "...", "name": "...",
                        "content": [text/image blocks], "is_error": bool}

``raw`` is the provider's verbatim assistant message. A provider replays its
own ``raw`` untouched (Anthropic thinking blocks, OpenAI tool_calls) and
converts the neutral blocks for anything produced elsewhere.

Providers here use only the standard library: ``urllib`` for HTTP and
``subprocess`` for the Claude Code CLI. No SDK is required, so the package
keeps its dependency list.

Providers:
    AnthropicProvider   Messages API with native tool use (ANTHROPIC_API_KEY).
    OpenAIProvider      Chat completions with tools; OPENAI_BASE_URL points it
                        at Ollama, vLLM, OpenRouter, or any compatible server.
    ClaudeCLIProvider   Shells out to the local ``claude -p`` command, so a
                        Claude Code subscription is enough to run the loop.
    ScriptedProvider    A fixed list of turns, for tests and demos.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """The planner could not produce a turn (auth, HTTP, parse, or CLI failure)."""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool the planner asked the loop to run."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage the provider reported for one turn (0 when unknown).

    ``input_tokens`` counts every input token the model saw, cached or not,
    so cost comparisons between providers stay apples to apples.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    #: Cost the provider itself reported for the turn, in USD (0.0 when the
    #: API reports none). The Claude Code CLI reports ``total_cost_usd`` at
    #: list price; the HTTP APIs report tokens only.
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens,
                     self.output_tokens + other.output_tokens,
                     self.cost_usd + other.cost_usd)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class PlannerTurn:
    """What the planner said this turn."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    #: Provider-specific verbatim assistant message for replay (see module doc).
    raw: dict | None = None

    def assistant_message(self) -> dict:
        """The neutral assistant message to append to the history."""
        content: list[dict] = []
        if self.text:
            content.append({"type": "text", "text": self.text})
        for call in self.tool_calls:
            content.append({"type": "tool_use", "id": call.id, "name": call.name,
                            "input": dict(call.arguments)})
        message: dict = {"role": "assistant", "content": content}
        if self.raw is not None:
            message["raw"] = self.raw
        return message


@runtime_checkable
class Provider(Protocol):
    """One planner backend."""

    #: Stable id: "anthropic" | "openai" | "claude-cli" | "scripted".
    name: str
    #: Whether the loop may rewrite earlier turns (elide old observations).
    #: False for backends whose API binds later turns to the exact prefix.
    history_edits_ok: bool

    def plan(self, messages: list[dict], tools: list[dict], *, system: str) -> PlannerTurn:
        ...


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only)
# ---------------------------------------------------------------------------

_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


def _post_json(url: str, headers: dict[str, str], body: dict, *, timeout_s: float = 300.0,
               retries: int = 3) -> dict:
    """POST ``body`` as JSON and return the decoded JSON response.

    Retries connection errors and retryable statuses with exponential backoff.
    Raises `ProviderError` carrying the status and the response text otherwise.
    """
    data = json.dumps(body).encode("utf-8")
    delay = 1.0
    last: str = "no attempts"
    for attempt in range(retries):
        request = urllib.request.Request(url, data=data, method="POST",
                                         headers={"content-type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            last = f"HTTP {exc.code}: {text[:2000]}"
            if exc.code not in _RETRY_STATUSES:
                raise ProviderError(last) from exc
        except (OSError, http.client.HTTPException) as exc:
            # URLError is an OSError, but a read timeout (TimeoutError), a reset
            # (ConnectionResetError / RemoteDisconnected) and a truncated body
            # (IncompleteRead, an HTTPException) come raw from http.client, not
            # wrapped in URLError; all of them are retryable transport failures.
            last = f"connection error: {getattr(exc, 'reason', exc)}"
        except json.JSONDecodeError as exc:
            raise ProviderError(f"non-JSON response from {url}: {exc}") from exc
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 2
    raise ProviderError(last)


def _text_of(blocks: Sequence[dict]) -> str:
    return "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")


# ---------------------------------------------------------------------------
# Anthropic Messages API
# ---------------------------------------------------------------------------


class AnthropicProvider:
    """Claude through the Messages API with native tool use.

    The history is append-only on the wire: this provider replays its own
    assistant content verbatim (thinking blocks included) and never lets the
    loop elide earlier tool results, because current Claude models bind later
    thinking blocks to the exact conversation prefix. Context is bounded
    server-side instead, with the ``clear_tool_uses`` context-editing strategy
    (beta header ``context-management-2025-06-27``); if the API rejects that
    parameter the provider retries once without it and stays plain.
    """

    name = "anthropic"
    history_edits_ok = False
    DEFAULT_MODEL = "claude-opus-5"
    API_VERSION = "2023-06-01"

    def __init__(self, model: str | None = None, *, api_key: str | None = None,
                 auth_token: str | None = None, base_url: str | None = None,
                 max_tokens: int = 4096, context_editing: bool = True,
                 post: Callable[..., dict] = _post_json) -> None:
        self.model = model or self.DEFAULT_MODEL
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self.auth_token = (auth_token if auth_token is not None
                           else os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        if not self.api_key and not self.auth_token:
            raise ProviderError("AnthropicProvider needs ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN)")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL")
                         or "https://api.anthropic.com").rstrip("/")
        self.max_tokens = max_tokens
        self.context_editing = context_editing
        self._post = post

    # -- wire conversion ----------------------------------------------------

    @staticmethod
    def _block(block: dict) -> dict:
        kind = block.get("type")
        if kind == "image":
            return {"type": "image", "source": {"type": "base64",
                                                "media_type": block.get("media_type", "image/png"),
                                                "data": block["data"]}}
        if kind == "tool_result":
            content = [AnthropicProvider._block(b) for b in block.get("content", [])]
            out = {"type": "tool_result", "tool_use_id": block["tool_use_id"], "content": content}
            if block.get("is_error"):
                out["is_error"] = True
            return out
        if kind == "tool_use":
            return {"type": "tool_use", "id": block["id"], "name": block["name"],
                    "input": block.get("input", {})}
        return {"type": "text", "text": str(block.get("text", ""))}

    def _to_wire(self, message: dict) -> dict:
        raw = message.get("raw")
        if message["role"] == "assistant" and raw and raw.get("provider") == self.name:
            return {"role": "assistant", "content": raw["content"]}
        return {"role": message["role"], "content": [self._block(b) for b in message["content"]]}

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": self.API_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        else:
            headers["authorization"] = f"Bearer {self.auth_token}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
        return headers

    def plan(self, messages: list[dict], tools: list[dict], *, system: str) -> PlannerTurn:
        body: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": [{"name": t["name"], "description": t.get("description", ""),
                       "input_schema": t["input_schema"]} for t in tools],
            "messages": [self._to_wire(m) for m in messages],
        }
        headers = self._headers()
        if self.context_editing:
            beta = headers.get("anthropic-beta")
            headers["anthropic-beta"] = ",".join(filter(None, [beta, "context-management-2025-06-27"]))
            body["context_management"] = {"edits": [{"type": "clear_tool_uses_20250919"}]}
        url = f"{self.base_url}/v1/messages"
        try:
            data = self._post(url, headers, body)
        except ProviderError as exc:
            if self.context_editing and "context_management" in str(exc):
                self.context_editing = False  # the API rejected the beta: stay plain from now on
                return self.plan(messages, tools, system=system)
            raise
        content = list(data.get("content") or [])
        usage = data.get("usage") or {}
        turn_usage = Usage(
            input_tokens=int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
        )
        stop = data.get("stop_reason")
        if stop == "refusal":
            details = data.get("stop_details") or {}
            text = f"refused by the model's safety classifiers ({details.get('category') or 'unspecified'})"
            return PlannerTurn(text=text, usage=turn_usage, stop_reason=stop,
                               raw={"provider": self.name, "content": content})
        calls = [ToolCall(id=str(b.get("id")), name=str(b.get("name")),
                          arguments=dict(b.get("input") or {}))
                 for b in content if b.get("type") == "tool_use"]
        return PlannerTurn(text=_text_of(content), tool_calls=calls, usage=turn_usage,
                           stop_reason=stop, raw={"provider": self.name, "content": content})


# ---------------------------------------------------------------------------
# OpenAI-compatible chat completions (OpenAI, Ollama, vLLM, OpenRouter, ...)
# ---------------------------------------------------------------------------


class OpenAIProvider:
    """Chat completions with function tools.

    ``base_url`` (or ``OPENAI_BASE_URL``) selects the server, so the same
    code drives OpenAI, a local Ollama (``http://localhost:11434/v1``), vLLM,
    or OpenRouter. ``model`` is required because compatible servers share no
    default. A missing API key is tolerated for non-OpenAI hosts.
    """

    name = "openai"
    history_edits_ok = True

    def __init__(self, model: str | None, *, api_key: str | None = None,
                 base_url: str | None = None, post: Callable[..., dict] = _post_json) -> None:
        if not model:
            raise ProviderError("OpenAIProvider needs a model (--model, e.g. gpt-5 or llama3.1)")
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not self.api_key and "api.openai.com" in self.base_url:
            raise ProviderError("OpenAIProvider needs OPENAI_API_KEY for api.openai.com")
        self._post = post

    @staticmethod
    def _content_parts(blocks: Sequence[dict]) -> list[dict]:
        parts: list[dict] = []
        for block in blocks:
            if block.get("type") == "image":
                media = block.get("media_type", "image/png")
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{media};base64,{block['data']}"}})
            elif block.get("type") == "text":
                parts.append({"type": "text", "text": str(block.get("text", ""))})
        return parts

    def _to_wire(self, message: dict) -> list[dict]:
        raw = message.get("raw")
        if message["role"] == "assistant":
            if raw and raw.get("provider") == self.name:
                return [raw["message"]]
            text = _text_of(message["content"])
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                     for b in message["content"] if b.get("type") == "tool_use"]
            out: dict = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
            return [out]
        wire: list[dict] = []
        plain: list[dict] = []  # text/image blocks that are not tool results
        images_after: list[dict] = []
        for block in message["content"]:
            if block.get("type") == "tool_result":
                inner = block.get("content", [])
                text = _text_of(inner) or ("error" if block.get("is_error") else "ok")
                wire.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text})
                imgs = [b for b in inner if b.get("type") == "image"]
                if imgs:  # tool messages are text-only: attach the image as a user part
                    images_after.append({"type": "text",
                                         "text": f"Image returned by {block.get('name', 'the tool')}:"})
                    images_after.extend(self._content_parts(imgs))
            else:
                plain.append(block)
        parts = self._content_parts(plain) + images_after
        if parts:
            if all(p["type"] == "text" for p in parts):
                wire.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
            else:
                wire.append({"role": "user", "content": parts})
        return wire

    def plan(self, messages: list[dict], tools: list[dict], *, system: str) -> PlannerTurn:
        wire: list[dict] = [{"role": "system", "content": system}]
        for message in messages:
            wire.extend(self._to_wire(message))
        body = {
            "model": self.model,
            "messages": wire,
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t["input_schema"]}} for t in tools],
        }
        headers = {"authorization": f"Bearer {self.api_key or 'none'}"}
        data = self._post(f"{self.base_url}/chat/completions", headers, body)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"no choices in response: {json.dumps(data)[:500]}")
        message = choices[0].get("message") or {}
        calls: list[ToolCall] = []
        for i, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {"_unparsed": raw_args}
            calls.append(ToolCall(id=str(call.get("id") or f"call-{i}"), name=str(fn.get("name")),
                                  arguments=args if isinstance(args, dict) else {}))
        usage = data.get("usage") or {}
        text = message.get("content")
        if isinstance(text, list):  # some servers return content parts
            text = _text_of(text)
        return PlannerTurn(
            text=str(text or ""), tool_calls=calls,
            usage=Usage(int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)),
            stop_reason=choices[0].get("finish_reason"),
            raw={"provider": self.name, "message": message},
        )


# ---------------------------------------------------------------------------
# Claude Code CLI (`claude -p`)
# ---------------------------------------------------------------------------

_CLI_PROTOCOL = (
    "You are the planner of a computer-use agent. Each reply must be exactly one JSON "
    "object and nothing else, of the form {\"tool\": <tool name>, \"args\": {...}}. "
    "Pick one tool from the list, with arguments matching its schema. Call the "
    "\"done\" tool when the task is complete or impossible."
)


def first_json_object(text: str) -> dict | None:
    """The first JSON object embedded in ``text`` (fences and prose tolerated)."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    return None


class ClaudeCLIProvider:
    """Plan through the local Claude Code CLI (``claude -p``), no API key needed.

    Each turn is one stateless ``claude -p`` call: the conversation is flattened
    into the prompt, built-in tools are disabled, and the model answers with
    one JSON action object. MCP servers from the user's Claude Code settings
    are never loaded (``--strict-mcp-config``), which keeps each call's system
    prompt small.

    Images: by default screenshot results are described, not viewed. With
    ``view_images=True`` the newest image in the history is written to a
    temporary PNG and the call enables only the CLI's ``Read`` tool so the
    model can look at it (this is how the pixel mode of ``bench h2h`` shows
    the planner its screenshots). The CLI reports ``total_cost_usd`` per call,
    which lands in `Usage.cost_usd`.
    """

    name = "claude-cli"
    history_edits_ok = True

    def __init__(self, model: str | None = None, *, binary: str = "claude", timeout_s: float = 600.0,
                 view_images: bool = False,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.model = model
        self.binary = binary
        self.timeout_s = timeout_s
        self.view_images = view_images
        #: Model id the CLI reported for the last call (``modelUsage`` key).
        self.reported_model: str | None = None
        self._run = run
        self._counter = 0
        self._image_dir: str | None = None

    @staticmethod
    def _tool_lines(tools: Sequence[dict]) -> str:
        lines = []
        for tool in tools:
            props = (tool.get("input_schema") or {}).get("properties") or {}
            required = set((tool.get("input_schema") or {}).get("required") or [])
            params = ", ".join(
                f"{k}{'' if k in required else '?'}: {v.get('type') or 'any'}"
                for k, v in props.items()
                if isinstance(v, dict)
            )
            desc = " ".join(str(tool.get("description", "")).split())
            lines.append(f"- {tool['name']}({params}): {desc}")
        return "\n".join(lines)

    def _image_path(self, block: dict) -> str | None:
        """Write one image block to a PNG the CLI's Read tool can open; None
        when images are not being viewed."""
        if not self.view_images:
            return None
        import base64
        import tempfile

        if self._image_dir is None:
            self._image_dir = tempfile.mkdtemp(prefix="a11y_computer_use-cli-")
        self._counter += 1
        path = os.path.join(self._image_dir, f"screenshot-{self._counter}.png")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(block["data"]))
        return path

    def _transcript(self, messages: Sequence[dict]) -> tuple[str, list[str]]:
        """Flatten the history into prompt text. Returns the text and the paths
        of the images written for viewing (empty unless ``view_images``).

        Only the newest image is written: the loop already elides older
        observations, and one screenshot per turn is what the model needs.
        """
        parts: list[str] = []
        images: list[str] = []
        newest: dict | None = None
        for message in messages:
            for block in message["content"]:
                if block.get("type") == "image":
                    newest = block
                elif block.get("type") == "tool_result":
                    for inner in block.get("content", []):
                        if inner.get("type") == "image":
                            newest = inner

        def describe(block: dict, role: str) -> str:
            if block is newest and self.view_images:
                path = self._image_path(block)
                if path:
                    images.append(path)
                    return (f"[{role}] (screenshot saved at {path}; view it with the Read tool "
                            "before deciding)")
            if self.view_images:
                return f"[{role}] (an earlier screenshot, superseded)"
            return (f"[{role}] (an image was returned here; this planner cannot view images, "
                    "use accessibility refs instead)")

        for message in messages:
            for block in message["content"]:
                kind = block.get("type")
                if kind == "text":
                    parts.append(f"[{message['role']}] {block.get('text', '')}")
                elif kind == "image":
                    parts.append(describe(block, message["role"]))
                elif kind == "tool_use":
                    parts.append(f"[assistant] {json.dumps({'tool': block['name'], 'args': block.get('input', {})})}")
                elif kind == "tool_result":
                    text = _text_of(block.get("content", []))
                    imgs = [b for b in block.get("content", []) if b.get("type") == "image"]
                    if imgs:
                        if self.view_images:
                            text += "\n" + describe(imgs[-1], "tool")
                        else:
                            text += "\n(image omitted: this planner cannot view images)"
                    status = "error" if block.get("is_error") else "result"
                    parts.append(f"[tool {status}: {block.get('name', '?')}] {text}")
        return "\n\n".join(parts), images

    def plan(self, messages: list[dict], tools: list[dict], *, system: str) -> PlannerTurn:
        transcript, images = self._transcript(messages)
        prompt = (f"TOOLS\n{self._tool_lines(tools)}\n\nCONVERSATION\n{transcript}\n\n"
                  "Reply with the next single JSON action object.")
        cmd = [self.binary, "-p", "--output-format", "json", "--no-session-persistence",
               "--strict-mcp-config"]
        if images:
            cmd += ["--tools", "Read", "--allowedTools", "Read"]
        else:
            cmd += ["--tools", ""]
        cmd += ["--system-prompt", f"{system}\n\n{_CLI_PROTOCOL}"]
        if self.model:
            cmd += ["--model", self.model]
        cmd.append(prompt)
        try:
            proc = self._run(cmd, capture_output=True, text=True, timeout=self.timeout_s)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"claude CLI failed to run: {exc}") from exc
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"claude CLI returned non-JSON (exit {proc.returncode}): "
                                f"{(proc.stdout or proc.stderr)[:500]}") from exc
        if proc.returncode != 0 and not data:
            raise ProviderError(f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}")
        if data.get("is_error"):
            raise ProviderError(f"claude CLI error: {data.get('result')}")
        text = str(data.get("result") or "")
        usage = data.get("usage") or {}
        turn_usage = Usage(
            input_tokens=int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
        )
        models = data.get("modelUsage")
        if isinstance(models, dict) and models:
            self.reported_model = str(next(iter(models)))
        calls: list[ToolCall] = []
        obj = first_json_object(text)
        if obj and isinstance(obj.get("tool"), str):
            self._counter += 1
            args = obj.get("args")
            if args is None:
                args = obj.get("arguments", {})
            calls.append(ToolCall(id=f"cli-{self._counter}", name=obj["tool"],
                                  arguments=dict(args) if isinstance(args, dict) else {}))
        return PlannerTurn(text=text, tool_calls=calls, usage=turn_usage,
                           stop_reason=data.get("stop_reason"))


# ---------------------------------------------------------------------------
# Scripted (tests, demos)
# ---------------------------------------------------------------------------

TurnSource = PlannerTurn | Callable[[list[dict]], PlannerTurn]


def tool_turn(name: str, arguments: dict | None = None, *, text: str = "",
              usage: Usage | None = None, call_id: str | None = None) -> PlannerTurn:
    """A one-tool-call `PlannerTurn`, the common scripted shape."""
    return PlannerTurn(
        text=text,
        tool_calls=[ToolCall(id=call_id or f"scripted-{name}", name=name, arguments=dict(arguments or {}))],
        usage=usage or Usage(),
    )


def done_turn(summary: str, success: bool = True, **kw) -> PlannerTurn:
    return tool_turn("done", {"summary": summary, "success": success}, **kw)


class ScriptedProvider:
    """Replays a fixed sequence of turns. Each entry is a `PlannerTurn` or a
    callable receiving the current messages (so a script can read refs out of
    the latest observation). When the script runs out, the provider calls
    ``done`` with ``success=False`` so the loop ends deterministically."""

    name = "scripted"
    history_edits_ok = True

    def __init__(self, turns: Sequence[TurnSource]) -> None:
        self._turns = list(turns)
        self.seen: list[list[dict]] = []  # a copy of the messages for every plan() call

    def plan(self, messages: list[dict], tools: list[dict], *, system: str) -> PlannerTurn:
        self.seen.append(json.loads(json.dumps(messages)))
        if not self._turns:
            return done_turn("script exhausted", success=False)
        turn = self._turns.pop(0)
        return turn(messages) if callable(turn) else turn


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

PROVIDERS = ("anthropic", "openai", "claude-cli", "scripted")


def get_provider(name: str | None = None, *, model: str | None = None,
                 view_images: bool = False) -> Provider:
    """Build a provider by name (or ``$A11Y_COMPUTER_USE_PROVIDER``, or the first
    one the environment can support: Anthropic key, OpenAI key, ``claude`` CLI).

    ``view_images`` matters only for ``claude-cli``, whose calls are text
    unless the newest screenshot is written to disk for the CLI to Read; the
    API providers send images inline regardless."""
    target = name or os.environ.get("A11Y_COMPUTER_USE_PROVIDER")
    if not target:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            target = "anthropic"
        elif os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
            target = "openai"
        elif shutil.which("claude"):
            target = "claude-cli"
        else:
            raise ProviderError(
                "no planner available: set ANTHROPIC_API_KEY, OPENAI_API_KEY (+ --model), "
                "or install the claude CLI; or pass --provider")
    if target == "anthropic":
        return AnthropicProvider(model)
    if target == "openai":
        return OpenAIProvider(model)
    if target == "claude-cli":
        return ClaudeCLIProvider(model, view_images=view_images)
    if target == "scripted":
        return ScriptedProvider([])
    raise ProviderError(f"unknown provider {target!r}; expected one of {PROVIDERS}")


__all__ = [
    "AnthropicProvider", "ClaudeCLIProvider", "OpenAIProvider", "PlannerTurn", "Provider",
    "ProviderError", "ScriptedProvider", "ToolCall", "Usage", "PROVIDERS", "done_turn",
    "first_json_object", "get_provider", "tool_turn",
]
