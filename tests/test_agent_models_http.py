"""HTTP model providers against recorded fixtures.

httpx.MockTransport serves the JSON in tests/fixtures/agent_models/. No test
opens a socket and no test reads a real API key. Env vars that would point at
a live endpoint are cleared for every test.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from a11y_computer_use.agent.models import (
    Message,
    ModelError,
    ToolCall,
    assistant_message,
    make_model,
)
from a11y_computer_use.agent.models.anthropic import AnthropicModel
from a11y_computer_use.agent.models.gemini import GeminiModel
from a11y_computer_use.agent.models.openai_compat import OpenAICompatibleModel

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_models"
_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
_KEY_VARS = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "XAI_API_KEY", "XAI_BASE_URL",
    "OLLAMA_API_KEY", "OLLAMA_BASE_URL", "OLLAMA_HOST",
    "VLLM_API_KEY", "VLLM_BASE_URL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL",
)

_TOOLS = [
    {"name": "click", "description": "Click a ref.",
     "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]}},
    {"name": "type", "description": "Type text.",
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]
_SHOT = [{"name": "screenshot", "description": "Capture the screen.",
          "parameters": {"type": "object", "properties": {}}}]


def _load(provider: str) -> dict:
    return json.loads((_FIXTURES / f"{provider}.json").read_text(encoding="utf-8"))


def _history() -> list[Message]:
    return [
        Message("system", "You drive the computer."),
        Message("user", "Click Save."),
        Message("assistant", [
            {"type": "text", "text": "Clicking Save."},
            {"type": "tool_call", "id": "call_1", "name": "click", "args": {"ref": "e2"}},
        ]),
        Message("tool", "clicked e2", "call_1", "click"),
    ]


def _image_message(path: Path) -> Message:
    return Message("user", [
        {"type": "text", "text": "What is this?"},
        {"type": "image", "b64": _PNG, "mime": "image/png"},
        {"type": "image", "path": str(path)},
    ])


def _tool_image_messages() -> list[Message]:
    return [
        Message("user", "Look"),
        Message("assistant", [
            {"type": "tool_call", "id": "call_s", "name": "screenshot", "args": {}},
        ]),
        Message("tool", [
            {"type": "text", "text": "screen"},
            {"type": "image", "b64": _PNG, "mime": "image/png"},
        ], "call_s", "screenshot"),
    ]


@pytest.fixture(autouse=True)
def _no_live_keys(monkeypatch: pytest.MonkeyPatch):
    for name in _KEY_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def png_file(tmp_path: Path) -> Path:
    path = tmp_path / "pixel.png"
    path.write_bytes(base64.b64decode(_PNG))
    return path


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _record(response):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        captured["headers"] = {key.lower(): value for key, value in request.headers.items()}
        captured["calls"] = captured.get("calls", 0) + 1
        return httpx.Response(200, json=response)

    return handler, captured


def _openai(client, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return OpenAICompatibleModel("gpt-4.1", api_key="test-openai-key", client=client, **kwargs)


def _anthropic(client, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return AnthropicModel("claude-sonnet-4-5", api_key="test-anthropic-key", client=client, **kwargs)


def _gemini(client, **kwargs):
    kwargs.setdefault("sleep", lambda _s: None)
    return GeminiModel("gemini-2.5-flash", api_key="test-gemini-key", client=client, **kwargs)


_BUILDERS = {"openai": _openai, "anthropic": _anthropic, "gemini": _gemini}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_tool_call_roundtrip_matches_fixture(provider: str):
    fixture = _load(provider)
    handler, captured = _record(fixture["roundtrip_response"])
    with _client(handler) as client:
        turn = _BUILDERS[provider](client).complete(_history(), _TOOLS)
    assert captured["body"] == fixture["roundtrip_request"]
    assert turn.text == "Typing."
    assert turn.calls == [ToolCall("type", {"text": "hi"}, "call_2")]
    assert turn.usage == {"input_tokens": 20, "output_tokens": 8}
    assert turn.raw == fixture["roundtrip_response"]
    if provider == "openai":
        assert captured["headers"]["authorization"] == "Bearer test-openai-key"
        assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    elif provider == "anthropic":
        assert captured["headers"]["x-api-key"] == "test-anthropic-key"
        assert captured["headers"]["anthropic-version"] == "2023-06-01"
        assert captured["url"] == "https://api.anthropic.com/v1/messages"
    else:
        assert captured["headers"]["x-goog-api-key"] == "test-gemini-key"
        assert "test-gemini-key" not in captured["url"]
        assert captured["url"].endswith("/models/gemini-2.5-flash:generateContent")


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_multiple_tool_calls_match_fixture(provider: str):
    fixture = _load(provider)
    handler, captured = _record(fixture["multi_response"])
    with _client(handler) as client:
        turn = _BUILDERS[provider](client).complete([Message("user", "Do both.")], _TOOLS)
    assert captured["body"] == fixture["multi_request"]
    assert turn.text == ""
    assert turn.calls == [
        ToolCall("click", {"ref": "e2"}, "c1"),
        ToolCall("type", {"text": "hi"}, "c2"),
    ]
    assert turn.usage == {"input_tokens": 3, "output_tokens": 4}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_image_message_matches_fixture(provider: str, png_file: Path):
    fixture = _load(provider)
    handler, captured = _record(fixture["text_response"])
    with _client(handler) as client:
        turn = _BUILDERS[provider](client).complete([_image_message(png_file)], _TOOLS)
    assert captured["body"] == fixture["image_request"]
    assert turn.text == "ok"
    assert turn.calls == []


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_tool_result_image_matches_fixture(provider: str):
    fixture = _load(provider)
    handler, captured = _record(fixture["text_response"])
    with _client(handler) as client:
        _BUILDERS[provider](client).complete(_tool_image_messages(), _SHOT)
    assert captured["body"] == fixture["tool_image_request"]


def test_missing_image_file_does_not_call_http(tmp_path: Path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_load("openai")["text_response"])

    message = Message("user", [{"type": "image", "path": str(tmp_path / "missing.png")}])
    with _client(handler) as client:
        with pytest.raises(ModelError, match="not found"):
            _openai(client).complete([message], [])
    assert calls["n"] == 0


def test_gemini_replays_thought_signature():
    fixture = _load("gemini")
    responses = [fixture["roundtrip_response"], fixture["text_response"]]
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=responses.pop(0))

    with _client(handler) as client:
        model = _gemini(client)
        first = model.complete([Message("user", "Click Save.")], _TOOLS)
        replay = assistant_message(first)
        model.complete(
            [replay, Message("tool", "typed", "call_2", "type")],
            _TOOLS,
        )
    signature = bodies[1]["contents"][0]["parts"][1]
    assert signature["thoughtSignature"] == "sig-test"
    assert signature["functionCall"]["id"] == "call_2"
    assert signature["functionCall"]["name"] == "type"

    multi_handler, _captured = _record(fixture["multi_response"])
    with _client(multi_handler) as client:
        multi = _gemini(client).complete([Message("user", "Do both.")], _TOOLS)
    message = assistant_message(multi)
    assert message.content[0]["thought_signature"] == "sig-1"
    assert "thought_signature" not in message.content[1]


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_retries_429_then_succeeds(provider: str):
    fixture = _load(provider)
    plan = [429, 429, 200]
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = plan.pop(0)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=fixture["text_response"])

    with _client(handler) as client:
        turn = _BUILDERS[provider](client, sleep=delays.append).complete([Message("user", "hi")], [])
    assert turn.text == "ok"
    assert delays == [0.25, 0.5]
    assert plan == []


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_retries_5xx_then_raises(provider: str):
    delays: list[float] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, json={"error": {"message": "unavailable"}})

    with _client(handler) as client:
        with pytest.raises(ModelError, match="HTTP 502") as raised:
            _BUILDERS[provider](client, sleep=delays.append).complete([Message("user", "hi")], [])
    assert raised.value.status == 502
    assert "unavailable" in str(raised.value)
    assert "3 attempts" in str(raised.value)
    assert calls["n"] == 3
    assert delays == [0.25, 0.5]


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize("status", [400, 401])
def test_client_errors_are_not_retried(provider: str, status: int):
    calls = {"n": 0}
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"error": {"message": "bad request"}})

    with _client(handler) as client:
        with pytest.raises(ModelError, match=f"HTTP {status}") as raised:
            _BUILDERS[provider](client, sleep=delays.append).complete([Message("user", "hi")], [])
    assert raised.value.status == status
    assert "bad request" in str(raised.value)
    assert calls["n"] == 1
    assert delays == []


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_timeouts_are_retried(provider: str):
    calls = {"n": 0}
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    with _client(handler) as client:
        with pytest.raises(ModelError, match="timed out") as raised:
            _BUILDERS[provider](client, sleep=delays.append).complete(
                [Message("user", "hi")], [], timeout=2.5
            )
    assert raised.value.status is None
    assert "2.5s" in str(raised.value)
    assert calls["n"] == 3
    assert delays == [0.25, 0.5]


def test_retry_after_header_sets_the_delay():
    fixture = _load("openai")
    delays: list[float] = []
    first = {"value": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if first["value"]:
            first["value"] = False
            return httpx.Response(429, headers={"retry-after": "1.5"}, json={"error": {"message": "slow"}})
        return httpx.Response(200, json=fixture["text_response"])

    with _client(handler) as client:
        turn = _openai(client, sleep=delays.append).complete([Message("user", "hi")], [])
    assert turn.text == "ok"
    assert delays == [1.5]


def test_openai_unparsed_arguments_and_empty_choices():
    def bad_args(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "click", "arguments": "not-json"}}
            ]}}]
        })

    with _client(bad_args) as client:
        with pytest.raises(ModelError, match="not JSON"):
            _openai(client).complete([Message("user", "hi")], _TOOLS)

    def no_choices(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with _client(no_choices) as client:
        with pytest.raises(ModelError, match="no choices"):
            _openai(client).complete([Message("user", "hi")], [])

    def not_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"nope")

    with _client(not_json) as client:
        with pytest.raises(ModelError, match="non-JSON"):
            _openai(client).complete([Message("user", "hi")], [])


def test_gemini_block_and_anthropic_missing_content():
    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with _client(blocked) as client:
        with pytest.raises(ModelError, match="SAFETY"):
            _gemini(client).complete([Message("user", "hi")], [])

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    with _client(empty) as client:
        with pytest.raises(ModelError, match="no candidates"):
            _gemini(client).complete([Message("user", "hi")], [])

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "msg"})

    with _client(missing) as client:
        with pytest.raises(ModelError, match="content blocks"):
            _anthropic(client).complete([Message("user", "hi")], [])


def test_input_schema_alias_and_openai_wrapper():
    alias = [{
        "name": "click",
        "description": "Click a ref.",
        "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
    }]
    wrapped = [{
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click a ref.",
            "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]},
        },
    }]
    bodies: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "max_tokens" in body:
            payload = _load("anthropic")["text_response"]
        else:
            payload = _load("openai")["text_response"]
        return httpx.Response(200, json=payload)

    with _client(capture) as wired:
        _openai(wired).complete([Message("user", "hi")], alias)
        _openai(wired).complete([Message("user", "hi")], wrapped)
        _anthropic(wired).complete([Message("user", "hi")], alias)
    assert bodies[0]["tools"] == bodies[1]["tools"]
    assert bodies[0]["tools"][0]["function"]["parameters"]["required"] == ["ref"]
    assert bodies[2]["tools"][0]["input_schema"]["required"] == ["ref"]


def test_make_model_posts_through_mock_transport(monkeypatch: pytest.MonkeyPatch):
    """make_model's own client is still hermetic: MockTransport replaces httpx.Client."""
    openai_fix = _load("openai")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["x-api-key"] = request.headers.get("x-api-key")
        captured["goog"] = request.headers.get("x-goog-api-key")
        captured["body"] = body
        if "contents" in body:
            payload = _load("gemini")["text_response"]
        elif "max_tokens" in body:
            payload = _load("anthropic")["text_response"]
        else:
            payload = openai_fix["text_response"]
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)

    monkeypatch.setenv("XAI_API_KEY", "test-xai-key")
    turn = make_model("xai:grok-4").complete([Message("user", "hi")], [])
    assert turn.text == "ok"
    assert captured["url"] == "https://api.x.ai/v1/chat/completions"
    assert captured["auth"] == "Bearer test-xai-key"
    assert captured["body"]["model"] == "grok-4"

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    turn = make_model("ollama:llama3.1").complete([Message("user", "hi")], [])
    assert turn.text == "ok"
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert captured["auth"] is None
    assert captured["body"]["model"] == "llama3.1"

    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("VLLM_API_KEY", "test-vllm-key")
    make_model("vllm:local").complete([Message("user", "hi")], [])
    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["auth"] == "Bearer test-vllm-key"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    make_model("anthropic:claude-sonnet-4-5").complete([Message("user", "hi")], [])
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["x-api-key"] == "test-anthropic-key"
    assert captured["body"]["model"] == "claude-sonnet-4-5"

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    make_model("gemini:gemini-2.5-flash").complete([Message("user", "hi")], [])
    assert captured["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert captured["goog"] == "test-gemini-key"
    assert "test-gemini-key" not in captured["url"]

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.test/v1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    make_model("anthropic:claude-sonnet-4-5").complete([Message("user", "hi")], [])
    assert captured["url"] == "https://example.test/v1/messages"
    assert captured["x-api-key"] is None
