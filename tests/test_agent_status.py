"""Additive per-call statuses use only evidence already collected by the loop."""

import json
import re
from unittest.mock import Mock

import pytest

from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.agent.result import StepRecord, step_status
from a11y_computer_use.outcome import ActionResult
from tests.test_agent_core import FakeRuntime, el, run, turn, window


@pytest.mark.parametrize("verified", [False, True])
@pytest.mark.parametrize("marker,expected", [
    ("confirmed", "confirmed"), ("unverifiable", "unverifiable"),
    ("suspected_noop", "no_op"), ("partial", "failed"), ("refused", "failed"),
    (None, None),
])
def test_status_mapping_and_precedence(verified, marker, expected):
    assert step_status(verified, None, marker) == (
        expected or ("confirmed" if verified else "no_op")
    )
    assert step_status(verified, "error", marker) == "failed"
    assert step_status(verified, "error", marker, skipped=True) == "skipped"
    assert step_status(verified, None, marker, skipped=True) == "skipped"


@pytest.mark.parametrize("marker,status", [
    ("confirmed", "confirmed"), ("unverifiable", "unverifiable"),
    ("suspected_noop", "no_op"), ("partial", "failed"), ("refused", "failed"),
])
def test_marker_status_reaches_records_events_and_trace(tmp_path, marker, status):
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    runtime = FakeRuntime(elements)
    runtime.call_tool = Mock(return_value=ActionResult("existing sentence", outcome=marker))
    runtime.desktop_snapshot = Mock(wraps=runtime.desktop_snapshot)
    result, events, _, _ = run(
        ScriptedModel([turn(ToolCall("click", {"ref": "e2"}))]), elements,
        runtime=runtime, trace_dir=tmp_path, max_steps=1, max_retries=0,
    )
    assert result.step_log[0].status == status
    assert re.fullmatch(r"<untrusted nonce=([a-f0-9]+)>existing sentence</untrusted nonce=\1>",
                        result.step_log[0].result)
    assert result.step_log[0].verified is (marker == "confirmed")
    assert result.step_log[0].error is None
    assert result.to_dict()["step_log"][0]["status"] == status
    assert next(e.data for e in events if e.type == "step_finished")["status"] == status
    for filename in ("steps.jsonl", "trajectory.jsonl"):
        rows = [json.loads(line) for line in (tmp_path / filename).read_text().splitlines()]
        assert next(row for row in rows if row.get("action") == "click")["status"] == status
    assert runtime.call_tool.call_count == 1
    assert runtime.desktop_snapshot.call_count == 2


def test_three_call_failure_records_skipped_without_extra_runtime_calls(tmp_path):
    counts = []
    for with_tail in (False, True):
        elements = window(el("e2", "AXTextField", "Note", parent="e1", editable=True))
        runtime = FakeRuntime(elements)
        runtime.call_tool = Mock(side_effect=[
            ActionResult("set value", outcome="confirmed"), ValueError("bad key"),
        ])
        runtime.desktop_snapshot = Mock(wraps=runtime.desktop_snapshot)
        runtime.screenshot = Mock(wraps=runtime.screenshot)
        calls = [ToolCall("set_value", {"ref": "e2", "value": "hello"}),
                 ToolCall("key", {"chord": "Return"})]
        if with_tail:
            calls.append(ToolCall("type", {"text": "private tail"}, id="tail"))
        directory = tmp_path / str(with_tail)
        result, events, _, agent = run(
            ScriptedModel([turn(*calls)]), elements, runtime=runtime,
            trace_dir=directory, max_steps=2, max_retries=0,
        )
        counts.append((runtime.call_tool.call_count, runtime.desktop_snapshot.call_count,
                       runtime.screenshot.call_count))
        assert [step.status for step in result.step_log] == ["confirmed", "failed"]
        assert result.steps == 2
        if with_tail:
            skipped = result.skipped_steps
            assert len(skipped) == 1 and isinstance(skipped[0], StepRecord)
            assert skipped[0].status == "skipped"
            assert skipped[0].args["text"] == "[REDACTED]"
            assert skipped[0].duration_s == 0 and skipped[0].error is None
            assert not skipped[0].verified
            assert result.to_dict()["skipped_steps"][0]["status"] == "skipped"
            assert [e.data["status"] for e in events if e.type == "step_finished"] == [
                "confirmed", "failed", "skipped",
            ]
            assert any(m.content == "statuses: set_value=confirmed, key=failed, type=skipped"
                       for m in agent._messages)
            assert any(isinstance(m.content, str) and
                       "Stopped this turn (failure). Did not run: type." in m.content
                       for m in agent._messages)
            for filename in ("steps.jsonl", "trajectory.jsonl"):
                rows = [json.loads(line) for line in (directory / filename).read_text().splitlines()]
                assert [row["status"] for row in rows if "status" in row] == [
                    "confirmed", "failed", "skipped",
                ]
                assert rows[-1]["call_id"] == "tail"
                assert rows[-1]["after_index"] == 2
    assert counts[0] == counts[1]
    assert counts[0][:2] == (2, 3)


@pytest.mark.parametrize("error,status", [(None, "confirmed"), ("exit code 1", "failed")])
def test_exec_status_uses_existing_result(tmp_path, monkeypatch, error, status):
    from a11y_computer_use.agent.exec import ExecResult

    execute = Mock(return_value=ExecResult(0 if error is None else 1, "output", False, error))
    monkeypatch.setattr("a11y_computer_use.agent.core.run_command", execute)
    result, events, runtime, _ = run(
        ScriptedModel([turn(ToolCall("python", {"code": "print('output')"}))]),
        window(), trace_dir=tmp_path, max_steps=1, allow_exec=True,
        approve=lambda _action: True,
    )
    assert result.step_log[0].status == status
    assert next(e.data for e in events if e.type == "step_finished")["status"] == status
    assert execute.call_count == 1
    assert runtime.calls == []


def test_successful_done_preserves_skipped_tail_in_final_result(tmp_path):
    from tests.test_agent_core import done

    result, events, runtime, _ = run(
        ScriptedModel([turn(
            done("finished", [{"window_title_contains": "Demo"}]),
            ToolCall("key", {"chord": "Return"}, id="tail"),
        )]), window(), trace_dir=tmp_path,
    )
    assert result.status == "success"
    assert [step.status for step in result.step_log] == ["confirmed"]
    assert [step.status for step in result.skipped_steps] == ["skipped"]
    assert runtime.calls == []
    assert [e.data["status"] for e in events if e.type == "step_finished"] == [
        "confirmed", "skipped",
    ]
