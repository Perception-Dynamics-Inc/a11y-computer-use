"""Model protocol for the computer-use agent.

A model turns the conversation and the tool schemas into one `ModelTurn`.
Providers convert `Message` content into their own wire format. Nothing in this
package calls a live model by itself: pass an ``httpx`` client (tests use
``MockTransport``) or use `ScriptedModel` / `CommandModel`.

Neutral content blocks inside `Message.content` (a string is one text block):

* ``{"type": "text", "text": "..."}``
* ``{"type": "image", "path": "..."}`` or
  ``{"type": "image", "b64": "...", "mime": "image/png"}``
* ``{"type": "tool_call", "name": "...", "args": {...}, "id": "..."}``
  Optional ``thought_signature`` is copied from Gemini function-call parts so
  the next request can replay it.

Tool results are ``Message(role="tool", content=..., tool_call_id=..., name=...)``.
``tools`` are JSON-schema function definitions (``name``, ``description``,
``parameters``). ``input_schema`` and an OpenAI ``{"type": "function",
"function": {...}}`` wrapper are accepted as aliases.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "Message",
    "Model",
    "ModelError",
    "ModelTurn",
    "ToolCall",
    "assistant_message",
    "make_model",
    "message_to_dict",
    "model_turn_from_dict",
]

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_RETRY_STATUSES = frozenset({408, 429})


class ModelError(Exception):
    """The model could not produce a turn.

    ``status`` is the HTTP status when the failure came from a response,
    otherwise ``None`` (missing key, timeout, parse error, local command).
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class ToolCall:
    """One tool the model asked the agent to run."""

    name: str
    args: dict
    id: str | None = None


@dataclass(slots=True)
class ModelTurn:
    """What the model said this turn."""

    calls: list[ToolCall]
    text: str = ""
    raw: dict | None = None
    usage: dict | None = None


@dataclass(slots=True)
class Message:
    """One conversation item in the provider-neutral format."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict]
    tool_call_id: str | None = None
    name: str | None = None


@runtime_checkable
class Model(Protocol):
    """One completion backend."""

    name: str
    supports_images: bool

    def complete(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        timeout: float | None = None,
    ) -> ModelTurn:
        ...


def as_blocks(content: str | list[dict] | None) -> list[dict]:
    """Normalize message content to a list of blocks."""
    if content is None or content == "":
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return list(content)
    raise ModelError(f"message content must be a string or a list of blocks, got {type(content).__name__}")


def load_image(block: Mapping[str, object]) -> tuple[str, str]:
    """Return ``(mime, base64)`` for an image block.

    ``b64`` wins when it is present. ``path`` is read from disk. ``mime``
    overrides the suffix guess (default ``image/png``).
    """
    raw_b64 = block.get("b64")
    if isinstance(raw_b64, str) and raw_b64:
        mime = block.get("mime")
        return (str(mime) if mime else "image/png"), raw_b64
    path = block.get("path")
    if not isinstance(path, str) or not path:
        raise ModelError("image block needs 'path' or 'b64'")
    file = Path(path)
    try:
        data = file.read_bytes()
    except OSError as exc:
        raise ModelError(f"image file not found: {path}") from exc
    mime = block.get("mime") or _IMAGE_MIME.get(file.suffix.lower(), "image/png")
    return str(mime), base64.b64encode(data).decode("ascii")


def classify_content(content: str | list[dict] | None) -> tuple[str, list[tuple[str, str]], list[dict]]:
    """Split content into text, images ``(mime, b64)``, and tool-call dicts.

    Each tool-call dict has ``name``, ``args``, ``id``, and ``thought_signature``.
    """
    texts: list[str] = []
    images: list[tuple[str, str]] = []
    calls: list[dict] = []
    for block in as_blocks(content):
        if not isinstance(block, Mapping):
            raise ModelError(f"content block must be an object, got {type(block).__name__}")
        kind = block.get("type")
        if kind == "text" or (kind is None and "text" in block):
            texts.append(str(block.get("text") or ""))
        elif kind == "image":
            images.append(load_image(block))
        elif kind in {"tool_call", "tool_use"}:
            calls.append(_normalize_call(block))
        else:
            raise ModelError(f"unknown content block type {kind!r}")
    text = "\n".join(part for part in texts if part)
    return text, images, calls


def _normalize_call(block: Mapping[str, object]) -> dict:
    args = block.get("args")
    if args is None and "input" in block:
        args = block.get("input")
    if args is None and "arguments" in block:
        args = block.get("arguments")
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else {}
        except json.JSONDecodeError as exc:
            raise ModelError(f"tool call arguments are not JSON: {args[:200]}") from exc
    if not isinstance(args, dict):
        raise ModelError(f"tool call args must be an object, got {type(args).__name__}")
    name = block.get("name")
    if not isinstance(name, str) or not name:
        raise ModelError("tool call is missing a name")
    signature = block.get("thought_signature") or block.get("thoughtSignature")
    return {
        "name": name,
        "args": dict(args),
        "id": block.get("id") if isinstance(block.get("id"), str) else None,
        "thought_signature": signature if isinstance(signature, str) and signature else None,
    }


def tool_schema(tool: Mapping[str, object]) -> tuple[str, str, dict]:
    """Return ``(name, description, parameters)`` from a function schema."""
    body: Mapping[str, object] = tool
    if tool.get("type") == "function" and isinstance(tool.get("function"), Mapping):
        body = tool["function"]  # type: ignore[assignment]
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise ModelError("tool schema is missing a name")
    description = str(body.get("description") or "")
    params = body.get("parameters")
    if params is None:
        params = body.get("input_schema")
    if params is None:
        params = {"type": "object", "properties": {}}
    if not isinstance(params, dict):
        raise ModelError(f"tool {name!r} parameters must be a JSON schema object")
    return name, description, params


def message_to_dict(message: Message) -> dict:
    """JSON-ready copy of a `Message` (``None`` fields omitted)."""
    payload: dict = {"role": message.role, "content": message.content}
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        payload["name"] = message.name
    return payload


def model_turn_from_dict(data: Mapping[str, object]) -> ModelTurn:
    """Parse a `ModelTurn` from the scripted / command JSON shape."""
    if not isinstance(data, Mapping):
        raise ModelError("model turn JSON must be an object")
    raw_calls = data.get("calls") or []
    if not isinstance(raw_calls, list):
        raise ModelError("model turn 'calls' must be a list")
    calls: list[ToolCall] = []
    for item in raw_calls:
        if not isinstance(item, Mapping):
            raise ModelError("each tool call must be an object")
        normalized = _normalize_call(item)
        calls.append(ToolCall(name=normalized["name"], args=normalized["args"], id=normalized["id"]))
    text = data.get("text") or ""
    if not isinstance(text, str):
        raise ModelError("model turn 'text' must be a string")
    raw = data.get("raw")
    if raw is not None and not isinstance(raw, dict):
        raise ModelError("model turn 'raw' must be an object or null")
    usage = data.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ModelError("model turn 'usage' must be an object or null")
    return ModelTurn(calls=calls, text=text, raw=raw, usage=usage)


def assistant_message(turn: ModelTurn) -> Message:
    """The assistant `Message` to append so the next call replays tool calls.

    Gemini ``thoughtSignature`` values on function-call parts are copied onto
    the matching ``tool_call`` block as ``thought_signature``. The Gemini
    provider sends that field back; dropping it makes the next request fail
    validation on current Gemini models.
    """
    blocks: list[dict] = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    signatures = _thought_signatures(turn.raw)
    for index, call in enumerate(turn.calls):
        block: dict = {"type": "tool_call", "name": call.name, "args": dict(call.args)}
        if call.id is not None:
            block["id"] = call.id
        signature = signatures[index] if index < len(signatures) else None
        if signature:
            block["thought_signature"] = signature
        blocks.append(block)
    if not blocks:
        return Message(role="assistant", content="")
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return Message(role="assistant", content=turn.text)
    return Message(role="assistant", content=blocks)


def _thought_signatures(raw: dict | None) -> list[str | None]:
    if not isinstance(raw, dict):
        return []
    candidates = raw.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return []
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return []
    signatures: list[str | None] = []
    for part in parts:
        if isinstance(part, dict) and "functionCall" in part:
            signature = part.get("thoughtSignature")
            signatures.append(signature if isinstance(signature, str) and signature else None)
    return signatures


def usage_dict(*, input_tokens: int, output_tokens: int) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def require_httpx():
    """Import httpx or raise `ModelError` naming the ``[agent]`` extra."""
    try:
        import httpx
    except ImportError as exc:
        raise ModelError(
            "HTTP models need httpx. Install it with: pip install 'a11y-computer-use[agent]'"
        ) from exc
    return httpx


def request_json(
    client,
    url: str,
    *,
    headers: dict[str, str],
    body: dict,
    timeout: float,
    max_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    backoff_s: float = 0.25,
) -> dict:
    """POST JSON and return the decoded object.

    Retries HTTP 408, 429, and 5xx, and timeouts, with exponential backoff
    (or ``Retry-After`` when it is a number of seconds, capped at 8). Other
    HTTP statuses and non-JSON bodies raise `ModelError` immediately.
    """
    httpx = require_httpx()
    if max_attempts < 1:
        raise ModelError("max_attempts must be at least 1")
    delay_base = backoff_s
    last: ModelError | None = None
    for attempt in range(max_attempts):
        response = None
        try:
            response = client.post(url, headers=headers, json=body, timeout=timeout)
        except httpx.TimeoutException as exc:
            last = ModelError(f"request to {url} timed out after {timeout}s")
            if attempt >= max_attempts - 1:
                raise last from exc
            sleep(min(delay_base * (2**attempt), 8.0))
            continue
        except httpx.HTTPError as exc:
            raise ModelError(f"request to {url} failed: {exc}") from exc
        status = int(response.status_code)
        if status in _RETRY_STATUSES or 500 <= status <= 599:
            detail = _body_snippet(response)
            last = ModelError(f"HTTP {status} from {url} after {attempt + 1} attempts: {detail}", status=status)
            if attempt >= max_attempts - 1:
                raise last
            sleep(_retry_delay(response, attempt, delay_base))
            continue
        if status >= 400:
            raise ModelError(
                f"HTTP {status} from {url}: {_body_snippet(response)}",
                status=status,
            )
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ModelError(f"non-JSON response from {url}: {_body_snippet(response)}") from exc
        if not isinstance(data, dict):
            raise ModelError(f"response from {url} must be a JSON object")
        return data
    assert last is not None
    raise last


def _body_snippet(response) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - snippet is best-effort
        text = ""
    return text[:2000]


def _retry_delay(response, attempt: int, backoff_s: float) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 8.0)
        except ValueError:
            pass
    return min(backoff_s * (2**attempt), 8.0)


def env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def hostname_of(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def make_model(spec: str | Model) -> Model:
    """Build a `Model` from a spec string, or return a `Model` unchanged.

    Specs (provider prefix is case-insensitive; the model id is not)::

        openai:gpt-4.1
        anthropic:claude-sonnet-4-5
        gemini:gemini-2.5-flash
        xai:grok-4
        ollama:llama3.1
        ollama:llama3.1:8b
        vllm:meta-llama/Llama-3.1-8B-Instruct
        command:/path/to/script
        scripted:/path/to/script.json

    ``command:`` and ``scripted:`` take the remainder of the string, so paths
    and extra colons (Ollama tags) are preserved. See ``docs/agent-models.md``.
    """
    if isinstance(spec, Model):
        return spec
    if not isinstance(spec, str):
        raise ModelError(f"make_model expected a spec string or a Model, got {type(spec).__name__}")
    text = spec.strip()
    if not text:
        raise ModelError("model spec is empty")
    provider, sep, rest = text.partition(":")
    if not sep:
        raise ModelError(
            f"model spec {spec!r} must look like 'provider:model', 'command:/path', or 'scripted:/path.json'"
        )
    key = provider.strip().lower()
    rest = rest.strip()
    if key == "command":
        if not rest:
            raise ModelError("command spec is missing a path")
        from a11y_computer_use.agent.models.command import CommandModel

        return CommandModel(rest)
    if key == "scripted":
        if not rest:
            raise ModelError("scripted spec is missing a path")
        from a11y_computer_use.agent.models.scripted import ScriptedModel

        return ScriptedModel(path=rest)
    if key in {"openai", "xai", "ollama", "vllm"}:
        if not rest:
            raise ModelError(f"{key} spec is missing a model name")
        from a11y_computer_use.agent.models.openai_compat import open_ai_compatible

        return open_ai_compatible(key, rest)
    if key == "anthropic":
        if not rest:
            raise ModelError("anthropic spec is missing a model name")
        from a11y_computer_use.agent.models.anthropic import AnthropicModel

        return AnthropicModel(rest)
    if key == "gemini":
        if not rest:
            raise ModelError("gemini spec is missing a model name")
        from a11y_computer_use.agent.models.gemini import GeminiModel

        return GeminiModel(rest)
    raise ModelError(
        f"unknown model spec {spec!r}; expected openai, xai, ollama, vllm, "
        "anthropic, gemini, command, or scripted"
    )


def check_tools(tools: Sequence[Mapping[str, object]]) -> None:
    for tool in tools:
        tool_schema(tool)
