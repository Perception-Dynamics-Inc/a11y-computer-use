"""Gemini ``generateContent`` with function calling and inline images.

POST ``{base}/models/{model}:generateContent``. The default base is
``https://generativelanguage.googleapis.com/v1beta``. The key is sent as
``x-goog-api-key`` (``GEMINI_API_KEY``, else ``GOOGLE_API_KEY``) and is
required for ``generativelanguage.googleapis.com``.

Function calls keep the ``id`` Gemini returns. ``thoughtSignature`` on a
function-call part is replayed when the assistant message carries
``thought_signature`` (see `assistant_message`). Tool results are
``functionResponse`` parts; images travel as sibling ``inlineData`` parts.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

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

__all__ = ["GeminiModel"]

_DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"


class GeminiModel:
    """Gemini through the generateContent REST API. No vendor SDK."""

    name = "gemini"
    supports_images = True

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client=None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = 3,
        default_timeout: float = 120.0,
    ) -> None:
        require_httpx()
        if not model:
            raise ModelError("gemini model name is empty")
        self.model = model.removeprefix("models/")
        self.key_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        self.base_url = (base_url or env_value("GEMINI_BASE_URL") or _DEFAULT_BASE).rstrip("/")
        if api_key is not None:
            self.api_key = api_key or None
        else:
            self.api_key = env_value("GEMINI_API_KEY", "GOOGLE_API_KEY")
        if self.api_key is None and hostname_of(self.base_url) == "generativelanguage.googleapis.com":
            raise ModelError(
                "gemini model needs GEMINI_API_KEY or GOOGLE_API_KEY "
                "(no key in the environment or constructor)"
            )
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
        system, contents = _wire_messages(messages)
        body: dict = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [_wire_tool(tool) for tool in tools]}]
        headers: dict[str, str] = {}
        if self.api_key:
            headers["x-goog-api-key"] = self.api_key
        data = self._post(self._url(), headers, body, timeout)
        return _parse(data)

    def _url(self) -> str:
        model = quote(self.model, safe="-_.")
        return f"{self.base_url}/models/{model}:generateContent"

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
    return {"name": name, "description": description, "parameters": parameters}


def _wire_messages(messages: list[Message]) -> tuple[str, list[dict]]:
    system_chunks: list[str] = []
    contents: list[dict] = []
    for message in messages:
        if message.role == "system":
            text, images, calls = classify_content(message.content)
            if calls or images:
                raise ModelError("gemini system messages must be text")
            if text:
                system_chunks.append(text)
            continue
        if message.role == "user":
            _add_parts(contents, "user", _user_parts(message))
            continue
        if message.role == "tool":
            _add_parts(contents, "user", _tool_parts(message))
            continue
        if message.role == "assistant":
            _add_parts(contents, "model", _assistant_parts(message))
            continue
        raise ModelError(f"unknown message role {message.role!r}")
    return "\n\n".join(system_chunks), contents


def _add_parts(contents: list[dict], role: str, parts: list[dict]) -> None:
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
        return
    contents.append({"role": role, "parts": parts})


def _user_parts(message: Message) -> list[dict]:
    text, images, calls = classify_content(message.content)
    if calls:
        raise ModelError("user messages cannot contain tool calls")
    return _text_and_images(text, images)


def _assistant_parts(message: Message) -> list[dict]:
    text, images, calls = classify_content(message.content)
    parts = _text_and_images(text, images)
    for index, call in enumerate(calls):
        fn: dict = {"name": call["name"], "args": call["args"], "id": call["id"] or f"call_{index}"}
        part: dict = {"functionCall": fn}
        if call["thought_signature"]:
            part["thoughtSignature"] = call["thought_signature"]
        parts.append(part)
    return parts


def _tool_parts(message: Message) -> list[dict]:
    if not message.name:
        raise ModelError("gemini tool message is missing name (the function that was called)")
    if not message.tool_call_id:
        raise ModelError("tool message is missing tool_call_id")
    text, images, calls = classify_content(message.content)
    if calls:
        raise ModelError("tool messages cannot contain tool calls")
    response: dict = {"result": text} if text else {"result": ""}
    fn_response: dict = {"name": message.name, "id": message.tool_call_id, "response": response}
    parts: list[dict] = [{"functionResponse": fn_response}]
    parts.extend({"inlineData": {"mimeType": mime, "data": data}} for mime, data in images)
    return parts


def _text_and_images(text: str, images: list[tuple[str, str]]) -> list[dict]:
    parts: list[dict] = []
    if text:
        parts.append({"text": text})
    parts.extend({"inlineData": {"mimeType": mime, "data": data}} for mime, data in images)
    return parts


def _parse(data: dict) -> ModelTurn:
    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise ModelError(f"gemini blocked the prompt: {feedback.get('blockReason')}")
    candidates = data.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise ModelError("gemini response has no candidates")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ModelError("gemini candidate is missing content parts")
    texts: list[str] = []
    calls: list[ToolCall] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "text" in part and "functionCall" not in part:
            texts.append(str(part.get("text") or ""))
        fn = part.get("functionCall")
        if isinstance(fn, dict):
            name = fn.get("name")
            if not name:
                raise ModelError("functionCall is missing a name")
            args = fn.get("args") or {}
            if not isinstance(args, dict):
                raise ModelError("functionCall args must be an object")
            call_id = fn.get("id")
            calls.append(ToolCall(name=str(name), args=args, id=str(call_id) if call_id else None))
    usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else None
    parsed = None
    if usage is not None:
        parsed = usage_dict(
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
        )
    return ModelTurn(calls=calls, text="\n".join(part for part in texts if part), raw=data, usage=parsed)
