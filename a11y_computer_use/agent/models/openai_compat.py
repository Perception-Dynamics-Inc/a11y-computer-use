"""OpenAI-compatible chat completions: OpenAI, xAI, Ollama, and vLLM.

One POST ``{base_url}/chat/completions`` with function tools. Images are
``image_url`` data URLs. Tool results are ``role: tool`` messages; a tool
result that also carries an image is followed by a user message, because the
chat-completions tool role is text-only.

Keys and base URLs come from the constructor or the environment. A missing key
is an error only for the vendor host (``api.openai.com``, ``api.x.ai``).
Ollama and vLLM, and any OpenAI/xAI spec pointed at another ``base_url``, run
without a key. No vendor SDK.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

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

__all__ = ["OpenAICompatibleModel", "open_ai_compatible"]

_FAMILIES: dict[str, dict[str, Any]] = {
    "openai": {
        "key_env": ("OPENAI_API_KEY",),
        "base_url_env": ("OPENAI_BASE_URL",),
        "default_base_url": "https://api.openai.com/v1",
        "official_hosts": ("api.openai.com",),
    },
    "xai": {
        "key_env": ("XAI_API_KEY",),
        "base_url_env": ("XAI_BASE_URL",),
        "default_base_url": "https://api.x.ai/v1",
        "official_hosts": ("api.x.ai",),
    },
    "ollama": {
        "key_env": ("OLLAMA_API_KEY",),
        "base_url_env": ("OLLAMA_BASE_URL",),
        "default_base_url": "http://127.0.0.1:11434/v1",
        "official_hosts": (),
    },
    "vllm": {
        "key_env": ("VLLM_API_KEY",),
        "base_url_env": ("VLLM_BASE_URL",),
        "default_base_url": "http://127.0.0.1:8000/v1",
        "official_hosts": (),
    },
}


def _ollama_base_from_env() -> str | None:
    explicit = os.environ.get("OLLAMA_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    host = os.environ.get("OLLAMA_HOST")
    if not host:
        return None
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    if not host.endswith("/v1"):
        host += "/v1"
    return host


def open_ai_compatible(provider: str, model: str, **kwargs: Any) -> OpenAICompatibleModel:
    """Build the OpenAI-compatible client selected by a `make_model` prefix."""
    try:
        family = _FAMILIES[provider]
    except KeyError as exc:
        raise ModelError(f"unknown OpenAI-compatible provider {provider!r}") from exc
    return OpenAICompatibleModel(model, name=provider, **family, **kwargs)


class OpenAICompatibleModel:
    """Chat completions with function tools against an OpenAI-compatible server."""

    supports_images = True

    def __init__(
        self,
        model: str,
        *,
        name: str = "openai",
        api_key: str | None = None,
        base_url: str | None = None,
        key_env: tuple[str, ...] = ("OPENAI_API_KEY",),
        base_url_env: tuple[str, ...] = ("OPENAI_BASE_URL",),
        default_base_url: str = "https://api.openai.com/v1",
        official_hosts: tuple[str, ...] = ("api.openai.com",),
        client=None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 3,
        default_timeout: float = 120.0,
    ) -> None:
        require_httpx()
        if not model:
            raise ModelError(f"{name} model name is empty")
        self.name = name
        self.model = model
        self.key_env = key_env
        self.official_hosts = official_hosts
        if name == "ollama" and base_url is None and not env_value(*base_url_env):
            resolved = _ollama_base_from_env() or default_base_url
        else:
            resolved = (base_url or env_value(*base_url_env) or default_base_url).rstrip("/")
        self.base_url = resolved.rstrip("/")
        if api_key is not None:
            self.api_key = api_key or None
        else:
            self.api_key = env_value(*key_env)
        if self.api_key is None and hostname_of(self.base_url) in official_hosts:
            names = " or ".join(key_env)
            raise ModelError(f"{name} model needs {names} (no key in the environment or constructor)")
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
        url = f"{self.base_url}/chat/completions"
        headers: dict[str, str] = {}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body: dict = {"model": self.model, "messages": _wire_messages(messages)}
        if tools:
            body["tools"] = [_wire_tool(tool) for tool in tools]
        data = self._post(url, headers, body, timeout)
        return _parse(data)

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
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _wire_messages(messages: list[Message]) -> list[dict]:
    wire: list[dict] = []
    for message in messages:
        wire.extend(_wire_message(message))
    return wire


def _wire_message(message: Message) -> list[dict]:
    role = message.role
    if role == "system":
        text, images, calls = classify_content(message.content)
        if calls:
            raise ModelError("system messages cannot contain tool calls")
        if images:
            return [{"role": "system", "content": _parts(text, images)}]
        return [{"role": "system", "content": text}]
    if role == "user":
        text, images, calls = classify_content(message.content)
        if calls:
            raise ModelError("user messages cannot contain tool calls")
        if images:
            return [{"role": "user", "content": _parts(text, images)}]
        if isinstance(message.content, str):
            return [{"role": "user", "content": message.content}]
        return [{"role": "user", "content": text}]
    if role == "assistant":
        text, images, calls = classify_content(message.content)
        payload: dict = {"role": "assistant", "content": _assistant_content(text, images)}
        if calls:
            payload["tool_calls"] = [_wire_call(call, index) for index, call in enumerate(calls)]
        return [payload]
    if role == "tool":
        text, images, calls = classify_content(message.content)
        if calls:
            raise ModelError("tool messages cannot contain tool calls")
        if not message.tool_call_id:
            raise ModelError("tool message is missing tool_call_id")
        tool_msg: dict = {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": text if text else ("Image attached." if images else ""),
        }
        if message.name:
            tool_msg["name"] = message.name
        out = [tool_msg]
        if images:
            label = f"Image returned by {message.name}:" if message.name else "Image returned by the tool:"
            out.append({"role": "user", "content": _parts(label, images)})
        return out
    raise ModelError(f"unknown message role {role!r}")


def _parts(text: str, images: list[tuple[str, str]]) -> list[dict]:
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    for mime, data in images:
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    return parts


def _assistant_content(text: str, images: list[tuple[str, str]]) -> str | list[dict] | None:
    if images:
        return _parts(text, images)
    return text or None


def _wire_call(call: dict, index: int) -> dict:
    call_id = call["id"] or f"call_{index}"
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": call["name"],
            "arguments": json.dumps(call["args"], separators=(",", ":"), ensure_ascii=False),
        },
    }


def _parse(data: dict) -> ModelTurn:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices, list):
        raise ModelError(f"no choices in response: {json.dumps(data)[:500]}")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise ModelError("response choice message must be an object")
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            raise ModelError("tool call must be an object")
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") if isinstance(fn, dict) else "{}"
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as exc:
                raise ModelError(f"tool call arguments are not JSON: {raw_args[:200]}") from exc
        elif isinstance(raw_args, dict):
            args = raw_args
        else:
            args = {}
        if not isinstance(args, dict):
            raise ModelError("tool call arguments must be a JSON object")
        name = fn.get("name") if isinstance(fn, dict) else None
        if not name:
            raise ModelError("tool call is missing a name")
        call_id = call.get("id")
        calls.append(ToolCall(name=str(name), args=args, id=str(call_id) if call_id else None))
    text = message.get("content")
    if isinstance(text, list):
        text = "\n".join(
            str(part.get("text") or "")
            for part in text
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    parsed_usage = None
    if usage is not None:
        parsed_usage = usage_dict(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )
    return ModelTurn(calls=calls, text=str(text or ""), raw=data, usage=parsed_usage)
