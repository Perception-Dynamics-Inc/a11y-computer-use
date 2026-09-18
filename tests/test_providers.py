"""Planner providers: request/response marshalling against fake transports.

No network and no CLI: the Anthropic and OpenAI providers get a fake ``post``,
the Claude CLI provider a fake ``subprocess.run``. Each test pins the wire
shape both ways (what we send, what we parse) so a real endpoint is only ever
a transport swap.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading

import pytest

from a11y_computer_use import providers
from a11y_computer_use.providers import (
    AnthropicProvider,
    ClaudeCLIProvider,
    OpenAIProvider,
    PlannerTurn,
    ProviderError,
    ScriptedProvider,
    ToolCall,
    Usage,
    done_turn,
    first_json_object,
    get_provider,
    tool_turn,
)

TOOLS = [
    {"name": "click", "description": "Click a ref.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}}},
    {"name": "done", "description": "Finish.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"},
                                                       "success": {"type": "boolean"}},
                      "required": ["summary", "success"]}},
]

HISTORY = [
    {"role": "user", "content": [
        {"type": "text", "text": "Task: click Save"},
        {"type": "text", "text": "[snap-1] e2 button \"Save\" (click)", "observation": True},
    ]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "Clicking."},
        {"type": "tool_use", "id": "call-1", "name": "click", "input": {"ref": "e2"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call-1", "name": "click", "is_error": False,
         "content": [{"type": "text", "text": "clicked e2"},
                     {"type": "image", "media_type": "image/png", "data": "QUJD"}]},
    ]},
]


class FakePost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, body, **kw):
        self.calls.append((url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


# --- transport failures ------------------------------------------------------------


class _RawServer:
    """Accepts connections on 127.0.0.1 and hands each socket to ``handler`` on
    its own thread: the misbehaving upstreams urllib does NOT wrap in URLError
    (they surface raw from ``HTTPConnection.getresponse()`` / ``read()``)."""

    def __init__(self, handler) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.05)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/v1/messages"
        self.connections = 0
        self.stopping = threading.Event()
        self._thread = threading.Thread(target=self._serve, args=(handler,), daemon=True)
        self._thread.start()

    def _serve(self, handler) -> None:
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:  # accept timeout: re-check the stop flag
                continue
            self.connections += 1
            threading.Thread(target=self._handle, args=(handler, conn), daemon=True).start()

    @staticmethod
    def _handle(handler, conn) -> None:
        with conn:
            try:
                handler(conn)
            except OSError:
                pass

    def close(self) -> None:
        self.stopping.set()
        self._thread.join(timeout=2)
        self.sock.close()


def _read_request_head(conn) -> bytes:
    conn.settimeout(1.0)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


@pytest.mark.parametrize("failure", ["timeout", "reset", "truncated"])
def test_post_json_turns_raw_transport_failures_into_provider_errors(monkeypatch, failure) -> None:
    """A read timeout, a peer that closes without a status line, and a body shorter
    than its Content-Length reach urlopen's caller as TimeoutError, RemoteDisconnected
    and IncompleteRead: none is a URLError, so an ``except URLError`` let them escape
    plan() and crash the agent loop. They must be retried and end in ProviderError."""
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)  # no backoff wait

    def handler(conn) -> None:
        if failure == "reset":
            return  # accept, then close without sending a byte
        _read_request_head(conn)
        if failure == "timeout":
            server.stopping.wait(2.0)  # hold the connection open past timeout_s
        else:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: 100\r\n\r\n{\"id\": \"msg")

    server = _RawServer(handler)
    try:
        with pytest.raises(ProviderError) as ei:
            providers._post_json(server.url, {}, {"x": 1}, timeout_s=0.2, retries=2)
        assert server.connections == 2  # every retry reached the server before giving up
    finally:
        server.close()
    assert "connection error" in str(ei.value)


# --- Anthropic ---------------------------------------------------------------------


ANTHROPIC_REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
    "stop_reason": "tool_use",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "I will click Save."},
        {"type": "tool_use", "id": "toolu_1", "name": "click", "input": {"ref": "e2"}},
    ],
    "usage": {"input_tokens": 100, "output_tokens": 20,
              "cache_creation_input_tokens": 400, "cache_read_input_tokens": 1000},
}


def test_anthropic_request_shape_and_parsed_turn(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    post = FakePost(ANTHROPIC_REPLY)
    p = AnthropicProvider(api_key="sk-test", post=post)
    turn = p.plan(HISTORY, TOOLS, system="SYS")

    url, headers, body = post.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-test" and headers["anthropic-version"] == "2023-06-01"
    assert "context-management-2025-06-27" in headers["anthropic-beta"]
    assert body["context_management"] == {"edits": [{"type": "clear_tool_uses_20250919"}]}
    assert body["model"] == "claude-opus-5" and body["system"] == "SYS" and body["max_tokens"] == 4096
    assert body["tools"][0] == {"name": "click", "description": "Click a ref.",
                                "input_schema": TOOLS[0]["input_schema"]}
    assert "thinking" not in body  # Claude Opus 5 runs adaptive thinking by default
    user0, assistant, user1 = body["messages"]
    assert user0["content"][1] == {"type": "text", "text": "[snap-1] e2 button \"Save\" (click)"}
    assert assistant["content"][1] == {"type": "tool_use", "id": "call-1", "name": "click",
                                       "input": {"ref": "e2"}}
    result = user1["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "call-1"
    assert "is_error" not in result and "name" not in result
    assert result["content"][1] == {"type": "image", "source": {"type": "base64",
                                                                "media_type": "image/png", "data": "QUJD"}}

    assert turn.text == "I will click Save."
    assert turn.tool_calls == [ToolCall("toolu_1", "click", {"ref": "e2"})]
    assert turn.usage == Usage(1500, 20)  # cached input counted too
    assert turn.stop_reason == "tool_use"
    assert turn.raw == {"provider": "anthropic", "content": ANTHROPIC_REPLY["content"]}


def test_anthropic_replays_its_own_assistant_content_verbatim() -> None:
    post = FakePost(ANTHROPIC_REPLY, ANTHROPIC_REPLY)
    p = AnthropicProvider(api_key="sk-test", post=post)
    first = p.plan(HISTORY[:1], TOOLS, system="SYS")
    history = HISTORY[:1] + [first.assistant_message(), {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "name": "click",
                                     "content": [{"type": "text", "text": "clicked e2"}], "is_error": True}]}]
    p.plan(history, TOOLS, system="SYS")
    _url, _headers, body = post.calls[1]
    assert body["messages"][1] == {"role": "assistant", "content": ANTHROPIC_REPLY["content"]}  # thinking kept
    assert body["messages"][2]["content"][0]["is_error"] is True
    assert p.history_edits_ok is False


def test_anthropic_falls_back_when_context_editing_is_rejected() -> None:
    post = FakePost(ProviderError("HTTP 400: context_management: Extra inputs are not permitted"),
                    ANTHROPIC_REPLY)
    p = AnthropicProvider(api_key="sk-test", post=post)
    turn = p.plan(HISTORY, TOOLS, system="SYS")
    assert turn.tool_calls[0].name == "click"
    assert p.context_editing is False
    _url, headers, body = post.calls[1]
    assert "context_management" not in body and "anthropic-beta" not in headers


def test_anthropic_refusal_is_a_turn_without_calls() -> None:
    reply = {**ANTHROPIC_REPLY, "stop_reason": "refusal", "content": [],
             "stop_details": {"type": "refusal", "category": "cyber"}}
    turn = AnthropicProvider(api_key="sk-test", post=FakePost(reply)).plan(HISTORY, TOOLS, system="S")
    assert turn.stop_reason == "refusal" and turn.tool_calls == [] and "cyber" in turn.text


def test_anthropic_auth_token_and_base_url(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()
    post = FakePost(ANTHROPIC_REPLY)
    p = AnthropicProvider("claude-sonnet-5", auth_token="oauth-x", base_url="https://proxy.test/",
                          context_editing=False, post=post)
    p.plan(HISTORY[:1], TOOLS, system="S")
    url, headers, body = post.calls[0]
    assert url == "https://proxy.test/v1/messages" and body["model"] == "claude-sonnet-5"
    assert headers["authorization"] == "Bearer oauth-x" and headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "context_management" not in body


def test_anthropic_other_errors_propagate() -> None:
    p = AnthropicProvider(api_key="k", post=FakePost(ProviderError("HTTP 401: invalid x-api-key")))
    with pytest.raises(ProviderError, match="401"):
        p.plan(HISTORY, TOOLS, system="S")


# --- OpenAI-compatible ----------------------------------------------------------------


OPENAI_REPLY = {
    "id": "chatcmpl-1", "model": "gpt-5",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_a", "type": "function",
                        "function": {"name": "click", "arguments": "{\"ref\": \"e2\"}"}}]}}],
    "usage": {"prompt_tokens": 300, "completion_tokens": 12},
}


def test_openai_request_shape_and_parsed_turn(monkeypatch) -> None:
    post = FakePost(OPENAI_REPLY)
    p = OpenAIProvider("gpt-5", api_key="sk-o", post=post)
    turn = p.plan(HISTORY, TOOLS, system="SYS")

    url, headers, body = post.calls[0]
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["authorization"] == "Bearer sk-o"
    assert body["model"] == "gpt-5"
    assert body["tools"][0] == {"type": "function", "function": {
        "name": "click", "description": "Click a ref.", "parameters": TOOLS[0]["input_schema"]}}
    msgs = body["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "Task: click Save\n[snap-1] e2 button \"Save\" (click)"}
    assert msgs[2]["role"] == "assistant" and msgs[2]["content"] == "Clicking."
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "click", "arguments": "{\"ref\": \"e2\"}"}
    assert msgs[3] == {"role": "tool", "tool_call_id": "call-1", "content": "clicked e2"}
    image_msg = msgs[4]  # tool messages are text-only, so the image follows as a user part
    assert image_msg["role"] == "user"
    assert image_msg["content"][1]["image_url"]["url"] == "data:image/png;base64,QUJD"

    assert turn.tool_calls == [ToolCall("call_a", "click", {"ref": "e2"})]
    assert turn.usage == Usage(300, 12) and turn.text == "" and turn.stop_reason == "tool_calls"
    assert turn.raw == {"provider": "openai", "message": OPENAI_REPLY["choices"][0]["message"]}


def test_openai_replays_raw_message_and_tolerates_bad_arguments() -> None:
    bad = {"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "oops",
        "tool_calls": [{"id": "c", "type": "function", "function": {"name": "click", "arguments": "{not json"}}]}}]}
    post = FakePost(OPENAI_REPLY, bad)
    p = OpenAIProvider("llama3.1", base_url="http://localhost:11434/v1", post=post)  # no key: Ollama
    first = p.plan(HISTORY[:1], TOOLS, system="S")
    second = p.plan(HISTORY[:1] + [first.assistant_message()], TOOLS, system="S")
    assert post.calls[0][1]["authorization"] == "Bearer none"
    assert post.calls[1][2]["messages"][2] == OPENAI_REPLY["choices"][0]["message"]
    assert second.tool_calls[0].arguments == {"_unparsed": "{not json"} and second.text == "oops"


def test_openai_needs_model_and_key_for_openai_dot_com(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with pytest.raises(ProviderError, match="model"):
        OpenAIProvider(None)
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAIProvider("gpt-5")
    with pytest.raises(ProviderError, match="no choices"):
        OpenAIProvider("gpt-5", api_key="k", post=FakePost({"choices": []})).plan(HISTORY, TOOLS, system="S")


# --- Claude Code CLI ----------------------------------------------------------------------


CLI_JSON = {
    "type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
    "result": "Sure.\n```json\n{\"tool\": \"click\", \"args\": {\"ref\": \"e2\"}}\n```",
    "usage": {"input_tokens": 2, "output_tokens": 28, "cache_creation_input_tokens": 23481,
              "cache_read_input_tokens": 0},
    "session_id": "abc",
}


class FakeRun:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_claude_cli_command_prompt_and_parsed_turn() -> None:
    run = FakeRun(subprocess.CompletedProcess([], 0, json.dumps(CLI_JSON), ""))
    p = ClaudeCLIProvider(model="claude-opus-5", run=run)
    turn = p.plan(HISTORY, TOOLS, system="SYS")

    cmd = run.calls[0]
    assert cmd[:3] == ["claude", "-p", "--output-format"] and "json" in cmd
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""  # built-in tools off
    system = cmd[cmd.index("--system-prompt") + 1]
    assert system.startswith("SYS") and "exactly one JSON object" in system
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5"
    prompt = cmd[-1]
    assert "TOOLS\n- click(ref?: string): Click a ref." in prompt
    assert "- done(summary: string, success: boolean): Finish." in prompt
    assert "[user] Task: click Save" in prompt
    assert "[assistant] {\"tool\": \"click\", \"args\": {\"ref\": \"e2\"}}" in prompt
    assert "[tool result: click] clicked e2" in prompt and "image omitted" in prompt

    assert turn.tool_calls == [ToolCall("cli-1", "click", {"ref": "e2"})]
    assert turn.usage == Usage(23483, 28) and turn.stop_reason == "end_turn"
    assert turn.text.startswith("Sure.")


def test_claude_cli_errors_are_structured() -> None:
    err = {**CLI_JSON, "is_error": True, "result": "Not logged in"}
    p = ClaudeCLIProvider(run=FakeRun(subprocess.CompletedProcess([], 0, json.dumps(err), "")))
    with pytest.raises(ProviderError, match="Not logged in"):
        p.plan(HISTORY, TOOLS, system="S")
    p = ClaudeCLIProvider(run=FakeRun(subprocess.CompletedProcess([], 1, "garbage", "boom")))
    with pytest.raises(ProviderError, match="non-JSON"):
        p.plan(HISTORY, TOOLS, system="S")
    p = ClaudeCLIProvider(run=FakeRun(FileNotFoundError("claude")))
    with pytest.raises(ProviderError, match="failed to run"):
        p.plan(HISTORY, TOOLS, system="S")


def test_claude_cli_without_model_and_without_json_yields_no_calls() -> None:
    reply = {**CLI_JSON, "result": "I am not sure what to do."}
    run = FakeRun(subprocess.CompletedProcess([], 0, json.dumps(reply), ""))
    turn = ClaudeCLIProvider(run=run).plan(HISTORY, TOOLS, system="S")
    assert "--model" not in run.calls[0]
    assert turn.tool_calls == [] and turn.text == "I am not sure what to do."


@pytest.mark.parametrize("text, expected", [
    ('{"tool": "done", "args": {"summary": "x", "success": true}}', {"tool": "done", "args": {"summary": "x", "success": True}}),
    ('prose first {"a": 1} then {"b": 2}', {"a": 1}),
    ('```json\n{"tool": "click"}\n```', {"tool": "click"}),
    ("{broken then {\"ok\": 1}", {"ok": 1}),
    ("[1, 2] and no object", None),
    ("nothing here", None),
])
def test_first_json_object(text, expected) -> None:
    assert first_json_object(text) == expected


# --- scripted + selection -------------------------------------------------------------------


def test_scripted_provider_replays_then_exhausts() -> None:
    p = ScriptedProvider([tool_turn("click", {"ref": "e1"}), lambda messages: done_turn(f"{len(messages)} msgs")])
    assert p.plan([], [], system="").tool_calls[0].name == "click"
    assert p.plan([{"role": "user", "content": []}], [], system="").tool_calls[0].arguments["summary"] == "1 msgs"
    last = p.plan([], [], system="")
    assert last.tool_calls[0].name == "done" and last.tool_calls[0].arguments["success"] is False
    assert len(p.seen) == 3


def test_planner_turn_assistant_message_carries_text_calls_and_raw() -> None:
    turn = PlannerTurn(text="hi", tool_calls=[ToolCall("1", "click", {"ref": "e1"})], raw={"provider": "x"})
    msg = turn.assistant_message()
    assert msg == {"role": "assistant", "raw": {"provider": "x"}, "content": [
        {"type": "text", "text": "hi"}, {"type": "tool_use", "id": "1", "name": "click", "input": {"ref": "e1"}}]}
    assert "raw" not in PlannerTurn().assistant_message()
    assert (Usage(1, 2) + Usage(3, 4)) == Usage(4, 6) and Usage(4, 6).total == 10


def test_get_provider_selection(monkeypatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                "A11Y_COMPUTER_USE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(providers.shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="no planner available"):
        get_provider()
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert isinstance(get_provider(), ClaudeCLIProvider)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    assert isinstance(get_provider(model="llama3.1"), OpenAIProvider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    assert isinstance(get_provider(), AnthropicProvider)
    monkeypatch.setenv("A11Y_COMPUTER_USE_PROVIDER", "claude-cli")
    assert isinstance(get_provider(), ClaudeCLIProvider)
    assert isinstance(get_provider("scripted"), ScriptedProvider)
    with pytest.raises(ProviderError, match="unknown provider"):
        get_provider("gemini")
