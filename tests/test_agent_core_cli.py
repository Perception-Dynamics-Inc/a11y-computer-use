"""Hermetic CLI tests: one JSON object and the 0/1/2/3 exit codes."""

from __future__ import annotations

import json

import pytest

from a11y_computer_use.agent import cli
from a11y_computer_use.agent.core import Agent
from a11y_computer_use.agent.models.base import ModelTurn, ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from tests.test_agent_core import FakeRuntime, done, el, turn, window


def _agent(args, model, elements, **kwargs):
    options = {
        "max_steps": args.max_steps,
        "max_time_s": args.max_time_s,
        "display": args.display,
        "trace_dir": args.trace_dir,
    }
    options.update(kwargs)
    return Agent(model, runtime=FakeRuntime(elements), **options)


def _loads(capsys) -> tuple[dict, str]:
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("\n") == 1
    return json.loads(out), out


def test_json_success(monkeypatch, capsys, tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    model = ScriptedModel([turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}]))])

    def build(args):
        return _agent(args, model, elements, trace_dir=tmp_path)

    monkeypatch.setattr(cli, "build_agent", build)
    code = cli.main(["run", "confirm save", "--model", "scripted:ignored.json", "--json"])
    payload, _raw = _loads(capsys)
    assert code == 0
    assert payload["status"] == "success"
    assert payload["reason"] == "done"
    assert payload["answer"] == "saved"
    assert isinstance(payload["steps"], int)
    assert payload["steps"] == 1
    assert isinstance(payload["step_log"], list)
    assert payload["step_log"][0]["action"] == "done"
    assert set(payload) >= {
        "status", "answer", "steps", "elapsed_s", "reason",
        "conditions", "needs_human", "trace_dir", "step_log",
    }


def test_json_needs_human_exits_2(monkeypatch, capsys):
    elements = window(el("e2", "AXTextField", "Password", parent="e1", secure=True, editable=True))
    model = ScriptedModel(lambda _messages: turn(ToolCall("type", {"ref": "e2", "text": "hunter2"})))

    def build(args):
        return _agent(args, model, elements)

    monkeypatch.setattr(cli, "build_agent", build)
    code = cli.main(["run", "sign in", "--model", "scripted:ignored.json", "--json"])
    payload, _raw = _loads(capsys)
    assert code == 2
    assert payload["status"] == "needs_human"
    assert payload["needs_human"]["kind"] == "login"
    assert payload["steps"] == 0


def test_json_cancel_exits_3(monkeypatch, capsys):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    holder: dict = {}

    def script(_messages):
        holder["agent"].cancel()
        return ModelTurn(calls=[ToolCall("click", {"ref": "e2"})])

    def build(args):
        agent = _agent(args, ScriptedModel(script), elements)
        holder["agent"] = agent
        return agent

    monkeypatch.setattr(cli, "build_agent", build)
    code = cli.main(["run", "click", "--model", "scripted:ignored.json", "--json"])
    payload, _raw = _loads(capsys)
    assert code == 3
    assert payload["reason"] == "cancelled"
    assert payload["status"] == "failed"


def test_json_max_steps_exits_1(monkeypatch, capsys):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    model = ScriptedModel(lambda _messages: turn(ToolCall("click", {"ref": "e2"})))

    def build(args):
        return _agent(args, model, elements)

    monkeypatch.setattr(cli, "build_agent", build)
    code = cli.main([
        "run", "poke", "--model", "scripted:ignored.json", "--json", "--max-steps", "1",
    ])
    payload, _raw = _loads(capsys)
    assert code == 1
    assert payload["reason"] == "max_steps"
    assert payload["steps"] == 1


def test_approve_and_auto_deny_together_exit_3(capsys):
    code = cli.main([
        "run", "quit the app", "--model", "scripted:ignored.json",
        "--json", "--approve", "--auto-deny",
    ])
    payload, _raw = _loads(capsys)
    assert code == 3
    assert payload["status"] == "failed"
    assert payload["steps"] == 0
    assert "approve" in payload["reason"]
    assert isinstance(payload["step_log"], list)


def test_missing_model_exits_3():
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "a goal"])
    assert exc.value.code == 3
