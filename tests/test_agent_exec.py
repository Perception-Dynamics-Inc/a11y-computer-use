"""Hermetic exec permission, audit, denial, timeout, and output-cap tests.

ScriptedModel and FakeRuntime only. ``shell`` and ``python`` run in a
subprocess when ``allow_exec`` is on and the approve hook allows the call.
They are not MCP tools.
"""

from __future__ import annotations

import inspect
import json
import os
import time
from datetime import datetime
from pathlib import Path

from a11y_computer_use.agent import tool_schemas
from a11y_computer_use.agent.exec import EXEC_OUTPUT_CAP
from a11y_computer_use.server import Runtime
from tests.test_agent_core import FakeRuntime, done, el, run, turn, window
from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel

_AUDIT_FIELDS = ("timestamp", "command", "cwd", "exit_code", "output", "approval")


def _shell_print(text: str) -> str:
    """A shell command that prints ``text`` on both cmd.exe and a POSIX shell."""
    if os.name == "nt":
        return f"echo {text}"
    return f"printf %s {text}"


def _audit(result) -> list[dict]:
    path = Path(result.trace_dir) / "exec-audit.jsonl"
    text = path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert text.endswith("\n")
    return rows


def _assert_audit_shape(row: dict) -> None:
    for key in _AUDIT_FIELDS:
        assert key in row, row
    datetime.fromisoformat(row["timestamp"])
    assert isinstance(row["command"], str)
    assert isinstance(row["cwd"], str)
    assert isinstance(row["output"], str)
    assert isinstance(row["approval"], str)


def test_shell_and_python_schemas_appear_only_when_exec_is_allowed():
    hidden = {item["name"] for item in tool_schemas()}
    shown = {item["name"] for item in tool_schemas(allow_exec=True)}
    assert {"shell", "python"}.isdisjoint(hidden)
    assert {"shell", "python"} <= shown
    assert "click" in hidden and "done" in hidden
    source = inspect.getsource(Runtime.call_tool)
    assert '"shell"' not in source
    assert '"python"' not in source


def test_exec_disabled_is_audited_and_does_not_run(tmp_path):
    target = tmp_path / "nope.txt"
    code = f"from pathlib import Path; Path({str(target)!r}).write_text('no', encoding='utf-8')"
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    model = ScriptedModel([
        turn(ToolCall("python", {"code": code}, id="p")),
        turn(done("still closed", [{"element": {"role": "AXButton", "name": "Save"}}])),
    ])
    result, _events, runtime, _agent = run(
        model, elements, trace_dir=tmp_path / "trace", allow_exec=False,
    )
    assert result.status == "success"
    assert not target.exists()
    assert runtime.calls == []
    assert result.step_log[0].action == "python"
    assert result.step_log[0].error == "exec is not allowed"
    assert result.step_log[0].turn_stop == "failure"
    assert "shell" not in {item["name"] for item in model.seen_tools[0]}
    assert "python" not in {item["name"] for item in model.seen_tools[0]}
    rows = _audit(result)
    assert len(rows) == 1
    _assert_audit_shape(rows[0])
    assert rows[0]["approval"] == "exec_disabled"
    assert rows[0]["exit_code"] is None
    assert rows[0]["command"] == code
    assert rows[0]["action"] == "python"


def test_exec_auto_deny_and_hook_denial_write_the_audit_and_skip_the_rest(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    target = tmp_path / "denied.txt"
    code = f"from pathlib import Path; Path({str(target)!r}).write_text('x', encoding='utf-8')"

    auto, _events, runtime, _agent = run(
        ScriptedModel([
            turn(
                ToolCall("python", {"code": code}, id="s"),
                done("wrote", [{"element": {"role": "AXButton", "name": "Save"}}], call_id="d"),
            ),
            turn(done("blocked", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path / "auto",
        allow_exec=True,
        auto_deny=True,
    )
    assert not target.exists()
    assert runtime.calls == []
    assert auto.step_log[0].action == "python"
    assert auto.step_log[0].turn_stop == "refusal"
    assert auto.step_log[0].error.startswith("approval_denied")
    assert [item["name"] for item in auto.step_log[0].skipped] == ["done"]
    assert auto.answer == "blocked"
    auto_rows = _audit(auto)
    assert auto_rows[0]["approval"] == "auto_denied"
    assert auto_rows[0]["exit_code"] is None
    assert auto_rows[0]["command"] == code
    assert auto_rows[0]["action"] == "python"
    assert auto.status == "success"

    seen: list[str] = []

    def approve(action):
        seen.append(action.name)
        return False

    hooked, _events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("python", {"code": code})),
            turn(done("blocked", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path / "hook",
        allow_exec=True,
        approve=approve,
        auto_deny=True,
    )
    assert seen == ["python"]
    assert not target.exists()
    assert hooked.step_log[0].turn_stop == "refusal"
    hook_rows = _audit(hooked)
    _assert_audit_shape(hook_rows[0])
    assert hook_rows[0]["approval"] == "denied"
    assert hook_rows[0]["cwd"]
    assert hooked.answer == "blocked"


def test_approved_exec_appends_audit_and_stops_on_timeout_or_bad_exit(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    written = tmp_path / "out.txt"
    first = (
        "from pathlib import Path; "
        f"Path({str(written)!r}).write_text('hello', encoding='utf-8')"
    )
    second = _shell_print("again")
    model = ScriptedModel([turn(
        ToolCall("python", {"code": first, "cwd": str(tmp_path)}, id="1"),
        ToolCall("shell", {"command": second, "cwd": str(tmp_path)}, id="2"),
        done("wrote", [{"element": {"role": "AXButton", "name": "Save"}}]),
    )])
    result, _events, runtime, _agent = run(
        model,
        elements,
        trace_dir=tmp_path / "trace",
        allow_exec=True,
        approve=lambda _action: True,
    )
    assert result.status == "success"
    assert written.read_text(encoding="utf-8") == "hello"
    assert "again" in result.step_log[1].result
    assert runtime.calls == []
    assert [step.action for step in result.step_log[:2]] == ["python", "shell"]
    assert result.step_log[0].verified is True
    assert result.step_log[0].skipped == []
    rows = _audit(result)
    assert len(rows) == 2
    assert rows[0]["command"] == first
    assert rows[1]["command"] == second
    assert "again" in rows[1]["output"]
    for row in rows:
        _assert_audit_shape(row)
        assert row["approval"] == "approved"
        assert row["exit_code"] == 0
        assert Path(row["cwd"]) == tmp_path
        assert row["truncated"] is False
    shown = {item["name"] for item in model.seen_tools[0]}
    assert {"shell", "python", "done"} <= shown

    boom = tmp_path / "boom.txt"
    code = f"from pathlib import Path; Path({str(boom)!r}).write_text('no', encoding='utf-8'); raise SystemExit(2)"
    failed, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(
                ToolCall("python", {"code": code}),
                ToolCall("python", {"code": "print('later')"}),
            ),
            turn(done("stopped", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path / "bad",
        allow_exec=True,
        approve=lambda _action: True,
    )
    assert boom.read_text(encoding="utf-8") == "no"
    assert failed.step_log[0].turn_stop == "failure"
    assert failed.step_log[0].error == "exit 2"
    assert [item["name"] for item in failed.step_log[0].skipped] == ["python"]
    assert _audit(failed)[0]["exit_code"] == 2
    assert _audit(failed)[0]["approval"] == "approved"
    assert failed.answer == "stopped"

    started = time.perf_counter()
    timed, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(
                ToolCall("python", {"code": "import time; time.sleep(30)", "timeout_s": 0.4}),
                done("too slow", [{"element": {"role": "AXButton", "name": "Save"}}]),
            ),
            turn(done("continued", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path / "slow",
        allow_exec=True,
        approve=lambda _action: True,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 8, elapsed
    assert timed.step_log[0].error == "timeout"
    assert timed.step_log[0].turn_stop == "failure"
    assert timed.step_log[0].verified is False
    assert [item["name"] for item in timed.step_log[0].skipped] == ["done"]
    assert timed.answer == "continued"
    timeout_row = _audit(timed)[0]
    _assert_audit_shape(timeout_row)
    assert timeout_row["approval"] == "approved"
    assert timeout_row["exit_code"] is None
    assert timeout_row["error"] == "timeout"
    assert "import time" in timeout_row["command"]


def test_exec_output_is_capped_and_invalid_exec_is_rejected(tmp_path):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    blob = "y" * (EXEC_OUTPUT_CAP + 5_000)
    capped, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("python", {"code": f"print({blob!r})"})),
            turn(done("capped", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path / "cap",
        allow_exec=True,
        approve=lambda _action: True,
    )
    row = _audit(capped)[0]
    _assert_audit_shape(row)
    assert row["truncated"] is True
    assert row["approval"] == "approved"
    assert row["exit_code"] == 0
    assert "truncated" in row["output"]
    assert row["output"].startswith("y" * 100)
    assert len(row["output"]) < len(blob)
    assert capped.step_log[0].result.startswith("y" * 100)
    assert blob not in capped.step_log[0].result

    rejected, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(
                ToolCall("shell", {"command": "   "}),
                ToolCall("python", {"code": "print('no')"}),
            ),
            turn(done("rejected", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path / "bad-args",
        allow_exec=True,
        approve=lambda _action: True,
    )
    assert rejected.step_log[0].error == "shell requires command"
    assert rejected.step_log[0].turn_stop == "failure"
    assert [item["name"] for item in rejected.step_log[0].skipped] == ["python"]
    assert _audit(rejected)[0]["approval"] == "rejected"
    assert _audit(rejected)[0]["exit_code"] is None
    assert rejected.answer == "rejected"

    missing = tmp_path / "not-a-dir"
    cwd_denied, _events, _runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("python", {"code": "print('no')", "cwd": str(missing)})),
            turn(done("no cwd", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        trace_dir=tmp_path / "cwd",
        allow_exec=True,
        approve=lambda _action: True,
    )
    assert cwd_denied.step_log[0].error == "cwd is not a directory"
    assert _audit(cwd_denied)[0]["approval"] == "rejected"
    assert cwd_denied.answer == "no cwd"
