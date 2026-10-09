"""Anthropic Messages API with tool use and image blocks.

POST ``{base}/v1/messages`` (or ``{base}/messages`` when ``base`` already ends
in ``/v1``). System messages are hoisted to the top-level ``system`` field.
Tool results are ``tool_result`` blocks on a user message. Images are base64
``source`` blocks, including images inside a tool result.

``ANTHROPIC_API_KEY`` is required when the base URL host is
``api.anthropic.com``. A custom ``ANTHROPIC_BASE_URL`` may omit the key.
"""

from __future__ import annotations

from collections.abc import Callable

from a11y_computer_use.agent.models.base import (
    Message,
    ModelError,
    ModelTurn,
    ToolCall,
    check_tools,
    classify_content,
    env_value,
    hostname_of,
    request_json,
    require_httpx,
    tool_schema,
    usage_dict,
)

__all__ = ["AnthropicModel"]

_API_VERSION = "2023-06-01"
_DEFAULT_BASE = "https://api.anthropic.com"


class AnthropicModel:
    """Claude through the Messages API. No vendor SDK."""

    name = "anthropic"
    supports_images = True

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        client=None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 3,
        default_timeout: float = 120.0,
    ) -> None:
        require_httpx()
        if not model:
            raise ModelError("anthropic model name is empty")
        self.model = model
        self.key_env = ("ANTHROPIC_API_KEY",)
        self.base_url = (base_url or env_value("ANTHROPIC_BASE_URL") or _DEFAULT_BASE).rstrip("/")
        if api_key is not None:
            self.api_key = api_key or None
        else:
            self.api_key = env_value("ANTHROPIC_API_KEY")
        if self.api_key is None and hostname_of(self.base_url) == "api.anthropic.com":
            raise ModelError(
                "anthropic model needs ANTHROPIC_API_KEY (no key in the environment or constructor)"
            )
        self.max_tokens = max_tokens
        self._client = client
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._default_timeout = default_timeout

    def complete(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        timeout: float | None = None,
    ) -> ModelTurn:
        check_tools(tools)
        system, wire = _wire_messages(messages)
        body: dict = {"model": self.model, "max_tokens": self.max_tokens, "messages": wire}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [_wire_tool(tool) for tool in tools]
        headers = {"anthropic-version": _API_VERSION}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        data = self._post(self._url(), headers, body, timeout)
        return _parse(data)

    def _url(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/messages"
        return f"{self.base_url}/v1/messages"

    def _post(self, url: str, headers: dict[str, str], body: dict, timeout: float | None) -> dict:
        kwargs: dict = {
            "timeout": self._default_timeout if timeout is None else timeout,
            "max_attempts": self._max_attempts,
        }
        if self._sleep is not None:
            kwargs["sleep"] = self._sleep
        if self._client is not None:
            return request_json(self._client, url, headers=headers, body=body, **kwargs)
        httpx = require_httpx()
        with httpx.Client() as client:
            return request_json(client, url, headers=headers, body=body, **kwargs)


def _wire_tool(tool: dict) -> dict:
    name, description, parameters = tool_schema(tool)
    return {"name": name, "description": description, "input_schema": parameters}


def _wire_messages(messages: list[Message]) -> tuple[str, list[dict]]:
    system_chunks: list[str] = []
    out: list[dict] = []
    pending: list[dict] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            out.append({"role": "user", "content": pending})
            pending = None

    def add_user(blocks: list[dict]) -> None:
        nonlocal pending
        if pending is None:
            pending = []
        pending.extend(blocks)

    for message in messages:
        if message.role == "system":
            flush()
            text, images, calls = classify_content(message.content)
            if calls or images:
                raise ModelError("anthropic system messages must be text")
            if text:
                system_chunks.append(text)
            continue
        if message.role == "user":
            add_user(_user_blocks(message))
            continue
        if message.role == "tool":
            add_user([_tool_result(message)])
            continue
        if message.role == "assistant":
            flush()
            _append_assistant(out, message)
            continue
        raise ModelError(f"unknown message role {message.role!r}")
    flush()
    return "\n\n".join(system_chunks), out


def _user_blocks(message: Message) -> list[dict]:
    text, images, calls = classify_content(message.content)
    if calls:
        raise ModelError("user messages cannot contain tool calls")
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(_image_blocks(images))
    return blocks


def _append_assistant(out: list[dict], message: Message) -> None:
    text, images, calls = classify_content(message.content)
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(_image_blocks(images))
    for index, call in enumerate(calls):
        blocks.append(
            {
                "type": "tool_use",
                "id": call["id"] or f"toolu_{index}",
                "name": call["name"],
                "input": call["args"],
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    if out and out[-1]["role"] == "assistant":
        out[-1]["content"].extend(blocks)
        return
    out.append({"role": "assistant", "content": blocks})


def _tool_result(message: Message) -> dict:
    if not message.tool_call_id:
        raise ModelError("tool message is missing tool_call_id")
    text, images, calls = classify_content(message.content)
    if calls:
        raise ModelError("tool messages cannot contain tool calls")
    block: dict = {"type": "tool_result", "tool_use_id": message.tool_call_id}
    if images:
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        content.extend(_image_blocks(images))
        block["content"] = content
    else:
        block["content"] = text
    return block


def _image_blocks(images: list[tuple[str, str]]) -> list[dict]:
    return [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": data},
        }
        for mime, data in images
    ]


def _parse(data: dict) -> ModelTurn:
    content = data.get("content")
    if not isinstance(content, list):
        raise ModelError("anthropic response is missing content blocks")
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(str(block.get("text") or ""))
        elif kind == "tool_use":
            name = block.get("name")
            if not name:
                raise ModelError("tool_use block is missing a name")
            args = block.get("input") or {}
            if isinstance(args, str):
                raise ModelError("tool_use input must be an object")
            if not isinstance(args, dict):
                raise ModelError("tool_use input must be an object")
            call_id = block.get("id")
            calls.append(ToolCall(name=str(name), args=args, id=str(call_id) if call_id else None))
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    parsed = None
    if usage is not None:
        parsed = usage_dict(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
    return ModelTurn(calls=calls, text="\n".join(part for part in texts if part), raw=data, usage=parsed)
