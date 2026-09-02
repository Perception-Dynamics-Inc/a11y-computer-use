"""cu-meter — audit → benchmark report aggregation (pure, any OS)."""

from __future__ import annotations

import json

from computeruse import bench


def _write(d, rows):
    (d / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_report_aggregates_latency_and_tokens(tmp_path) -> None:
    d = tmp_path / "audit"; d.mkdir()
    _write(d, [
        {"action": "observeop", "result": "ok", "metrics": {"duration_ms": 30.0, "tokens_est": 300}},
        {"action": "observeop", "result": "ok", "metrics": {"duration_ms": 10.0, "tokens_est": 50}},
        {"action": "click", "result": "ok", "metrics": {"duration_ms": 5.0}},
        {"action": "click", "result": "needs_permission"},  # no metrics — still counted
    ])
    rep = bench.report(d)
    assert rep["total_actions"] == 4
    assert rep["total_tokens_est"] == 350
    assert rep["observe_tokens_est"] == 350  # observation is the token sink
    assert rep["by_action"]["observeop"]["count"] == 2
    assert rep["by_action"]["observeop"]["p50_ms"] == 20.0  # median(30,10)
    assert rep["by_action"]["click"]["count"] == 2 and rep["by_action"]["click"]["p50_ms"] == 5.0
    assert "observeop" in bench.format_report(rep)


def test_report_sums_planner_tokens_from_agent_rows(tmp_path) -> None:
    d = tmp_path / "audit"; d.mkdir()
    _write(d, [
        {"action": "observeop", "result": "ok", "metrics": {"duration_ms": 30.0, "tokens_est": 300}},
        {"action": "agent_step", "result": "ok",
         "metrics": {"duration_ms": 900.0, "planner_input_tokens": 1200, "planner_output_tokens": 40}},
        {"action": "agent_step", "result": "stale_ref",
         "metrics": {"duration_ms": 400.0, "planner_input_tokens": 800, "planner_output_tokens": 30}},
        {"action": "agent_run", "result": "ok",
         "metrics": {"duration_ms": 2000.0, "planner_input_tokens": 2000, "planner_output_tokens": 70}},
    ])
    rep = bench.report(d)
    assert rep["agent_runs"] == 1
    assert rep["planner_input_tokens"] == 2000 and rep["planner_output_tokens"] == 70
    assert rep["observe_tokens_est"] == 300  # observation cost stays separate from planner cost
    assert rep["by_action"]["agent_step"]["count"] == 2
    text = bench.format_report(rep)
    assert "agent runs=1" in text and "planner tokens in=2000 out=70" in text


def test_report_without_agent_rows_omits_planner_line(tmp_path) -> None:
    d = tmp_path / "audit"; d.mkdir()
    _write(d, [{"action": "click", "result": "ok", "metrics": {"duration_ms": 5.0}}])
    rep = bench.report(d)
    assert rep["agent_runs"] == 0 and rep["planner_input_tokens"] == 0
    assert "planner" not in bench.format_report(rep)


def test_report_empty_dir(tmp_path) -> None:
    d = tmp_path / "audit"; d.mkdir()
    rep = bench.report(d)
    assert rep["total_actions"] == 0 and rep["p50_ms"] is None
