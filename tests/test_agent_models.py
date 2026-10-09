"""Scripted and command models, and make_model, with no network.

HTTP providers are covered in test_agent_models_http.py against recorded
fixtures. No API key is read or required here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from a11y_computer_use.agent.models import (
    CommandModel,
    Message,
    Model,
    ModelError,
    ModelTurn,
    ScriptedModel,
    ToolCall,
    assistant_message,
    make_model,
)
from a11y_computer_use.agent.models.command import command_argv

_SCRIPT = """\
import json, sys
req = json.load(sys.stdin)
messages = req["messages"]
tools = req["tools"]
saw = False
for message in messages:
    content = message.get("content")
    if isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "image" for block in content
    ):
        saw = True
json.dump({
    "text": "saw-image" if saw else "acting",
    "calls": [
        {"name": "click", "args": {"ref": "e1"}, "id": "1"},
        {"name": "type", "args": {"text": "hi"}, "id": "2"},
    ],
    "usage": {"input_tokens": 4, "output_tokens": 2},
    "raw": {"tool_names": [tool.get("name") for tool in tools], "n_messages": len(messages)},
}, sys.stdout)
"""

_TOOLS = [
    {"name": "click", "description": "Click a ref.",
     "parameters": {"type": "object", "properties": {"ref": {"type": "string"}}}},
]


def test_legacy_agent_module_still_imports():
    """The model package sits beside agent.py; the reference loop stays importable."""
    from a11y_computer_use.agent import AgentResult, Step, run_task

    assert AgentResult.__name__ == "AgentResult"
    assert Step.__name__ == "Step"
    assert callable(run_task)


def test_scripted_list_and_protocol():
    turns = [
        ModelTurn(calls=[ToolCall("click", {"ref": "e1"}, "c1")], text="go"),
        ModelTurn(calls=[ToolCall("done", {"answer": "ok"}, "d1")], text="done",
                  usage={"input_tokens": 2, "output_tokens": 1}),
    ]
    model = ScriptedModel(turns)
    assert isinstance(model, Model)
    assert model.name == "scripted" and model.supports_images is True
    messages = [Message("user", "start")]
    first = model.complete(messages, _TOOLS)
    assert first.calls == [ToolCall("click", {"ref": "e1"}, "c1")]
    assert first.text == "go"
    second = model.complete(messages, [])
    assert second.calls[0].name == "done"
    assert second.usage == {"input_tokens": 2, "output_tokens": 1}
    assert model.seen == [messages, messages]
    assert model.seen_tools == [_TOOLS, []]
    with pytest.raises(ModelError, match="no turns left"):
        model.complete(messages, [])


def test_scripted_dict_turns_and_callable():
    model = ScriptedModel([{"calls": [{"name": "click", "args": {"ref": "e9"}}], "text": "go"}])
    assert model.complete([], []).calls == [ToolCall("click", {"ref": "e9"}, None)]

    def react(messages: list[Message]) -> ModelTurn:
        if any(message.role == "tool" for message in messages):
            return ModelTurn(calls=[ToolCall("done", {"answer": "ok"}, "d")], text="done")
        return ModelTurn(calls=[ToolCall("click", {"ref": "e1"}, "c")])

    reactive = ScriptedModel(react)
    assert reactive.complete([Message("user", "go")], []).calls[0].name == "click"
    assert reactive.complete([Message("tool", "clicked", "c", "click")], []).text == "done"

    def image_script(messages: list[Message]) -> ModelTurn:
        block = messages[0].content[1]
        assert block == {"type": "image", "path": "no-such.png"}
        return ModelTurn(calls=[], text="saw-image")

    seen = ScriptedModel(image_script)
    content = [{"type": "text", "text": "look"}, {"type": "image", "path": "no-such.png"}]
    assert seen.complete([Message("user", content)], []).text == "saw-image"


def test_scripted_callable_must_return_a_turn():
    model = ScriptedModel(lambda messages: {"calls": []})
    with pytest.raises(ModelError, match="ModelTurn"):
        model.complete([], [])


def test_scripted_json_file_shapes(tmp_path: Path):
    turn = {"calls": [{"name": "click", "args": {"ref": "e2"}, "id": "c1"}], "text": "go",
            "usage": {"input_tokens": 1, "output_tokens": 1}}
    listed = tmp_path / "list.json"
    listed.write_text(json.dumps([turn, {"calls": [], "text": "stop"}]), encoding="utf-8")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"turns": [turn]}), encoding="utf-8")
    single = tmp_path / "one.json"
    single.write_text(json.dumps(turn), encoding="utf-8")

    from_list = make_model(f"scripted:{listed}")
    assert isinstance(from_list, ScriptedModel)
    assert from_list.complete([], []).calls[0].id == "c1"
    assert from_list.complete([], []).text == "stop"
    assert make_model(f"scripted:{wrapped}").complete([], []).text == "go"
    assert make_model(f"scripted:{single}").complete([], []).calls[0].name == "click"


def test_scripted_file_errors(tmp_path: Path):
    missing = tmp_path / "missing.json"
    with pytest.raises(ModelError, match="not found"):
        ScriptedModel(path=str(missing))
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ModelError, match="not JSON"):
        ScriptedModel(path=str(bad))
    with pytest.raises(ModelError, match="not more than one"):
        ScriptedModel([ModelTurn(calls=[])], path=str(bad))


def test_make_model_passthrough_and_specs(monkeypatch: pytest.MonkeyPatch):
    model = ScriptedModel([ModelTurn(calls=[], text="ok")])
    assert make_model(model) is model
    with pytest.raises(ModelError, match="spec string or a Model"):
        make_model(object())  # type: ignore[arg-type]
    with pytest.raises(ModelError, match="empty"):
        make_model("   ")
    with pytest.raises(ModelError, match="provider:model"):
        make_model("gpt-4.1")
    with pytest.raises(ModelError, match="unknown model spec"):
        make_model("claude:opus")
    for name in (
        "OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "GEMINI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ModelError, match="OPENAI_API_KEY"):
        make_model("OpenAI:gpt-4.1")
    with pytest.raises(ModelError, match="XAI_API_KEY"):
        make_model("xai:grok-4")
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        make_model("anthropic:claude-sonnet-4-5")
    with pytest.raises(ModelError, match="GEMINI_API_KEY or GOOGLE_API_KEY"):
        make_model("gemini:gemini-2.5-flash")
    with pytest.raises(ModelError, match="missing a model name"):
        make_model("openai:")

    ollama = make_model("ollama:llama3.1:8b")
    assert ollama.name == "ollama"
    assert ollama.model == "llama3.1:8b"
    assert ollama.base_url == "http://127.0.0.1:11434/v1"
    assert ollama.api_key is None

    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    hosted = make_model("ollama:llama3.1")
    assert hosted.base_url == "http://127.0.0.1:11434/v1"

    monkeypatch.setenv("VLLM_BASE_URL", "http://127.0.0.1:9000/v1")
    vllm = make_model("vllm:meta-llama/Llama-3.1-8B-Instruct")
    assert vllm.name == "vllm"
    assert vllm.base_url == "http://127.0.0.1:9000/v1"
    assert vllm.model == "meta-llama/Llama-3.1-8B-Instruct"
    assert vllm.api_key is None

    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    local = make_model("openai:local-model")
    assert local.base_url == "http://127.0.0.1:8000/v1"
    assert local.api_key is None

    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    gemini = make_model("gemini:gemini-2.5-flash")
    assert gemini.api_key == "test-google-key"
    assert gemini.name == "gemini"


def test_command_argv_and_fake_script(tmp_path: Path):
    assert command_argv("model-bin --flag") == ["model-bin", "--flag"]
    script = tmp_path / "model.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    assert command_argv(str(script))[0] == sys.executable

    model = make_model(f"command:{script}")
    assert isinstance(model, CommandModel)
    assert model.supports_images is True
    messages = [
        Message("user", [{"type": "text", "text": "look"}, {"type": "image", "path": "shot.png"}]),
    ]
    turn = model.complete(messages, _TOOLS, timeout=5)
    assert turn.text == "saw-image"
    assert turn.calls == [
        ToolCall("click", {"ref": "e1"}, "1"),
        ToolCall("type", {"text": "hi"}, "2"),
    ]
    assert turn.usage == {"input_tokens": 4, "output_tokens": 2}
    assert turn.raw == {"tool_names": ["click"], "n_messages": 1}

    plain = CommandModel([sys.executable, str(script)])
    quiet = plain.complete([Message("user", "go")], _TOOLS)
    assert quiet.text == "acting"
    assert [call.name for call in quiet.calls] == ["click", "type"]


def test_command_errors(tmp_path: Path):
    missing = CommandModel("/no/such/a11y-model-bin")
    with pytest.raises(ModelError, match="failed to start"):
        missing.complete([], [])

    broken = tmp_path / "bad.py"
    broken.write_text("import sys\nsys.stderr.write('boom-stderr')\nsys.exit(3)\n", encoding="utf-8")
    with pytest.raises(ModelError, match="exited 3") as exited:
        CommandModel(str(broken)).complete([], [])
    assert "boom-stderr" in str(exited.value)

    garbage = tmp_path / "garbage.py"
    garbage.write_text("print('not-json')\n", encoding="utf-8")
    with pytest.raises(ModelError, match="non-JSON"):
        CommandModel(str(garbage)).complete([], [])

    sleeper = tmp_path / "sleep.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    slow = CommandModel(str(sleeper), timeout=30)
    with pytest.raises(ModelError, match="timed out after 0.2s"):
        slow.complete([], [], timeout=0.2)


def test_assistant_message_copies_gemini_thought_signature():
    turn = ModelTurn(
        calls=[
            ToolCall("click", {"ref": "e2"}, "c1"),
            ToolCall("type", {"text": "hi"}, "c2"),
        ],
        text="both",
        raw={
            "candidates": [{
                "content": {
                    "parts": [
                        {"functionCall": {"name": "click", "id": "c1"}, "thoughtSignature": "sig-1"},
                        {"functionCall": {"name": "type", "id": "c2"}},
                    ]
                }
            }]
        },
    )
    message = assistant_message(turn)
    assert message.role == "assistant"
    assert message.content[0] == {"type": "text", "text": "both"}
    assert message.content[1]["thought_signature"] == "sig-1"
    assert "thought_signature" not in message.content[2]
