"""Long-task support: the notes tool, wait_until, history compaction, and the
mission runner. Hermetic on every OS: fake driver, scripted planner, temp
files, a local HTTP server; no TCC, no model, no browser."""

from __future__ import annotations

import http.server
import json
import os
import threading
import time
from pathlib import Path

import pytest

from a11y_computer_use import agent, cli, conditions, mission, safety, server
from a11y_computer_use.providers import ScriptedProvider, done_turn, tool_turn
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.test_agent import APP, FakeDriver, audit_rows, make_runtime

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "missions" / "agency-demo.toml"


# --- notes ---------------------------------------------------------------------


def test_notes_round_trip_persist_and_render(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    assert rt.call_tool("notes", {"action": "list"}) == "(no notes)"
    assert rt.call_tool("notes", {"action": "add", "text": "video: ~/Downloads/car.mp4"}).startswith("noted #1")
    rt.call_tool("notes", {"action": "add", "text": "site: https://example.com"})
    listed = rt.call_tool("notes", {"action": "list"})
    assert listed.splitlines() == ["1. video: ~/Downloads/car.mp4", "2. site: https://example.com"]
    saved = json.loads((tmp_path / "audit" / "notes.json").read_text())
    assert [n["text"] for n in saved] == ["video: ~/Downloads/car.mp4", "site: https://example.com"]
    # a new Runtime over the same audit dir sees the same notes (process restarts)
    rt2 = server.Runtime(store=rt.store, audit=safety.AuditLog(tmp_path / "audit"), driver=FakeDriver())
    assert rt2.notes_store.render() == listed
    assert rt.call_tool("notes", {"action": "clear"}) == "cleared 2 notes"
    assert rt.notes_store.all() == []
    with pytest.raises(ValueError):
        rt.call_tool("notes", {"action": "add", "text": "   "})
    with pytest.raises(ValueError):
        rt.call_tool("notes", {"action": "shout"})


def test_notes_are_gated_and_audited_at_read_tier(tmp_path) -> None:
    rt = make_runtime(tmp_path, tier=None)  # no grant at all
    with pytest.raises(server.ActionRefused):
        rt.call_tool("notes", {"action": "add", "text": "x"})
    rt.store.set_tier(APP, safety.Tier.READ)
    rt.call_tool("notes", {"action": "add", "text": "x"})
    rows = [r for r in audit_rows(tmp_path) if r["action"] == "observeop" and r["params"].get("verb") == "notes"]
    assert rows and rows[-1]["result"] == "ok"


def test_notes_are_injected_into_every_planner_turn(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([
        tool_turn("notes", {"action": "add", "text": "video: /tmp/car.mp4"}),
        done_turn("noted"),
    ])
    systems: list[str] = []
    original = provider.plan

    def plan(messages, tools, *, system):
        systems.append(system)
        return original(messages, tools, system=system)

    provider.plan = plan  # type: ignore[method-assign]
    result = agent.run_task("remember the video path", rt, provider, app=APP, max_steps=3)
    assert result.success
    assert "Your notes so far" not in systems[0]
    assert "1. video: /tmp/car.mp4" in systems[1]


# --- wait_until ------------------------------------------------------------------


def test_wait_until_file_exists_with_glob_and_min_bytes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    rt = make_runtime(tmp_path)
    target = tmp_path / "downloads" / "car.mp4"

    def writer() -> None:
        time.sleep(0.3)
        target.parent.mkdir()
        target.write_bytes(b"x" * 2048)

    threading.Thread(target=writer).start()
    out = json.loads(rt.call_tool("wait_until", {
        "condition": {"file_exists": str(tmp_path / "downloads" / "*.mp4"), "min_bytes": 1024},
        "timeout_s": 5, "poll_s": 0.05}))
    assert "car.mp4" in out["matched"] and out["waited_s"] >= 0.2


def test_wait_until_file_stable_waits_for_growth_to_stop(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    rt = make_runtime(tmp_path)
    target = tmp_path / "render.mp4"
    target.write_bytes(b"a")

    def grower() -> None:
        for _ in range(3):
            time.sleep(0.1)
            with target.open("ab") as fh:
                fh.write(b"more")

    threading.Thread(target=grower).start()
    out = json.loads(rt.call_tool("wait_until", {
        "condition": {"file_stable": str(target), "seconds": 0.4}, "timeout_s": 5, "poll_s": 0.05}))
    assert "stable at 13 bytes" in out["matched"]


def test_wait_until_url_status_and_timeout(tmp_path, monkeypatch) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self):  # noqa: N802
            Handler.hits += 1
            self.send_response(503 if Handler.hits < 3 else 200)
            self.end_headers()

        def log_message(self, *_a):  # quiet
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/"
    rt = make_runtime(tmp_path)
    try:
        out = json.loads(rt.call_tool("wait_until", {"condition": {"url_status": url}, "timeout_s": 5, "poll_s": 0.05}))
        assert out["matched"].endswith("returned 200") and out["polls"] == 3
        with pytest.raises(ComputerUseError) as info:
            rt.call_tool("wait_until", {"condition": {"url_status": url, "status": 418}, "timeout_s": 0.2, "poll_s": 0.05})
        assert info.value.code is ErrorCode.TIMEOUT and info.value.detail["polls"] >= 2
    finally:
        httpd.shutdown()
    with pytest.raises(ValueError):
        rt.call_tool("wait_until", {"condition": {"url_status": "ftp://x"}, "timeout_s": 1})


def test_wait_until_snapshot_text_and_unsupported_screen_text(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    out = json.loads(rt.call_tool("wait_until", {
        "condition": {"snapshot_text": "Save", "app": APP}, "timeout_s": 2, "poll_s": 0.05}))
    assert "'Save'" in out["matched"]
    rt._ocr_engine = None  # no OCR engine (any OS off macOS): screen_text is unsupported
    with pytest.raises(ComputerUseError) as info:
        rt.call_tool("wait_until", {"condition": {"screen_text": "hi"}, "timeout_s": 1})
    assert info.value.code is ErrorCode.UNSUPPORTED


def test_wait_until_rejects_paths_outside_home_and_bad_conditions(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", raising=False)
    rt = make_runtime(tmp_path)
    outside = "/etc/hosts" if os.name != "nt" else "C:\\Windows\\win.ini"
    with pytest.raises(ValueError, match="outside the home directory"):
        rt.call_tool("wait_until", {"condition": {"file_exists": outside}, "timeout_s": 1})
    with pytest.raises(ValueError, match="exactly one"):
        rt.call_tool("wait_until", {"condition": {"file_exists": "a", "url_status": "b"}, "timeout_s": 1})
    with pytest.raises(ValueError):
        rt.call_tool("wait_until", {"condition": {"file_exists": "~/x"}, "timeout_s": -1})
    assert conditions.MAX_WAIT_UNTIL_S == 1800.0


def test_wait_until_is_a_read_tier_tool_on_the_mcp_surface(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    names = {t["name"] for t in agent.tool_specs(rt)}
    assert {"notes", "wait_until", "done"} <= names
    spec = next(t for t in agent.tool_specs(rt) if t["name"] == "wait_until")
    assert "condition" in spec["input_schema"]["properties"]
    assert "1800" in spec["description"]


# --- compaction ------------------------------------------------------------------


def _long_result_turns(n: int):
    return [tool_turn("notes", {"action": "add", "text": f"fact {i}: " + "x" * 400}) for i in range(n)]


def test_history_is_compacted_deterministically_and_keeps_the_latest_observation(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    turns = _long_result_turns(6) + [tool_turn("desktop_snapshot", {"app": APP}), done_turn("ok")]
    provider = ScriptedProvider(turns)
    result = agent.run_task("t", rt, provider, app=APP, max_steps=20, context_budget=900)
    assert result.success and result.compactions >= 1
    assert result.to_dict()["compactions"] == result.compactions
    # the final planner turn saw a compacted history: task first, then the summary
    final = provider.seen[-1]
    first = final[0]["content"]
    assert first[0]["text"].startswith("Task: t")
    assert "compacted history" in first[1]["text"]
    assert "- notes {" in first[1]["text"]
    # the newest observation (the snapshot) is still in full somewhere in the history
    texts = [b.get("text", "") for m in final for b in m["content"] if b.get("type") in ("text",)]
    results = [c.get("text", "") for m in final for b in m["content"] if b.get("type") == "tool_result" for c in b.get("content", [])]
    assert any("snap-" in t for t in texts + results)
    # and the notes survived, via the system prompt
    assert len(rt.notes_store.all()) == 6


def test_compaction_is_off_when_the_provider_forbids_history_edits(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider(_long_result_turns(4) + [done_turn("ok")])
    provider.history_edits_ok = False
    result = agent.run_task("t", rt, provider, app=APP, max_steps=10, context_budget=300)
    assert result.success and result.compactions == 0
    assert len(provider.seen[-1]) > 4


def test_compact_history_pairs_tool_use_ids_with_their_results() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Task: t"},
                                     {"type": "text", "text": "obs one", "observation": True}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "click", "input": {"ref": "e1"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "name": "click",
                                      "content": [{"type": "text", "text": "clicked e1\nmore"}]}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c2", "name": "type", "input": {"text": "hi"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c2", "name": "type",
                                      "content": [{"type": "text", "text": "typed"}]}]},
    ]
    compacted = agent.compact_history(messages, "t")
    assert len(compacted) == 3
    assert compacted[1]["content"][0]["id"] == "c2" and compacted[2]["content"][0]["tool_use_id"] == "c2"
    summary = compacted[0]["content"][1]["text"]
    assert '- click {"ref": "e1"} -> ok: clicked e1' in summary
    assert compacted[0]["content"][2]["text"].endswith("obs one")
    assert agent.compact_history(compacted, "t") == compacted or len(agent.compact_history(compacted, "t")) <= 3


def test_deadline_stops_the_loop_between_turns(tmp_path) -> None:
    rt = make_runtime(tmp_path)

    def slow(_messages):
        time.sleep(0.15)
        return tool_turn("notes", {"action": "list"})

    provider = ScriptedProvider([slow, slow, slow, done_turn("ok")])
    result = agent.run_task("t", rt, provider, app=APP, max_steps=10, deadline_s=0.2)
    assert result.stopped == "deadline" and not result.success
    assert len(result.steps) < 4


# --- missions ----------------------------------------------------------------------


def test_example_mission_validates(capsys) -> None:
    m = mission.load(EXAMPLE)
    assert m.name == "agency-demo" and len(m.phases) == 6
    assert [p.name for p in m.phases] == ["brief", "design", "video", "edit", "site", "reply"]
    assert cli.main(["mission", "validate", str(EXAMPLE)]) == 0
    assert "6 phases" in capsys.readouterr().out
    assert cli.main(["mission", "run", str(EXAMPLE), "--dry-run"]) == 0


def test_mission_validation_reports_every_problem(tmp_path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[mission]\nname = "x y"\n[[phase]]\nname = "a"\ntask = ""\ntier = "root"\n'
                   'max_steps = 0\nchecks = [{ nope = 1 }]\n[[phase]]\nname = "a"\ntask = "t"\n')
    with pytest.raises(ValueError) as info:
        mission.load(bad)
    text = str(info.value)
    for needle in ("short identifier", "task is required", "tier must be", "max_steps", "exactly one", "unique"):
        assert needle in text
    unknown = tmp_path / "unknown.toml"
    unknown.write_text('[mission]\nname = "m"\n[[phase]]\nname = "a"\ntask = "t"\nbogus = 1\n')
    with pytest.raises(ValueError, match="unknown keys"):
        mission.load(unknown)


def _two_phase_mission(tmp_path, marker: Path) -> mission.Mission:
    text = f'''
[mission]
name = "demo"
deadline_s = 60
[mission.record]
start = "echo rec-start"
stop = "echo rec-stop"

[[phase]]
name = "first"
task = "click save"
apps = ["{APP}"]
tier = "click"
max_steps = 5
checks = [{{ notes_contain = "saved" }}]

[[phase]]
name = "second"
task = "produce the file"
apps = ["{APP}"]
tier = "full"
max_steps = 5
retries = 1
check_timeout_s = 0.3
on_fail_notes = "try writing the file again"
checks = [{{ file_exists = "{marker.as_posix()}" }}]
'''
    path = tmp_path / "demo.toml"
    path.write_text(text)
    return mission.load(path)


def test_mission_runs_phases_grants_apps_retries_and_writes_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    marker = tmp_path / "out" / "done.txt"
    m = _two_phase_mission(tmp_path, marker)
    store = safety.PermissionStore(tmp_path / "perm.json")  # no grants: the runner grants per phase
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=FakeDriver())

    def second_attempt(_messages):
        # attempt 1 claims done without producing the file; attempt 2 produces it
        if any("attempt 1 failed" in n["text"] for n in rt.notes_store.all()):
            marker.parent.mkdir(exist_ok=True)
            marker.write_text("x")
        return done_turn("produced")

    provider = ScriptedProvider([
        tool_turn("click", {"ref": "e2"}),
        tool_turn("notes", {"action": "add", "text": "saved the document"}),
        done_turn("saved"),
        second_attempt,
        second_attempt,
    ])
    seen_steps: list[tuple[str, str]] = []
    result = mission.run(m, rt, provider, runs_dir=tmp_path / "runs",
                         on_step=lambda phase, step: seen_steps.append((phase, step.tool)))
    assert result.passed and result.stopped == "completed"
    assert [p.name for p in result.phases] == ["first", "second"]
    assert result.phases[0].attempts == 1 and result.phases[0].passed
    assert result.phases[1].attempts == 2 and result.phases[1].passed
    # the runner wrote the failure into the notes before the retry
    assert any("phase second attempt 1 failed" in n["text"] and "try writing the file again" in n["text"]
               for n in rt.notes_store.all())
    # grants were made for the phase and restored afterwards (no prior grant -> revoked)
    assert store.get_tier(APP) is None
    # artifacts
    run_dir = result.run_dir
    assert (run_dir / "phase-01-first.json").exists() and (run_dir / "phase-02-second.json").exists()
    assert (run_dir / "notes.json").exists() and (run_dir / "result.json").exists()
    timeline = [json.loads(l) for l in (run_dir / "timeline.jsonl").read_text().splitlines()]
    kinds = [row["kind"] for row in timeline]
    assert kinds[0] == "mission_start" and kinds[1] == "record_start" and kinds[-1] == "mission_end"
    assert kinds.count("phase_start") == 2 and kinds.count("phase_attempt") == 3
    assert any(r["kind"] == "check" and r["passed"] is False for r in timeline)
    assert all("t" in row and "ts" in row for row in timeline)
    assert ("first", "click") in seen_steps
    summary = json.loads((run_dir / "result.json").read_text())
    assert summary["passed"] is True and summary["phases"][1]["attempts"] == 2


def test_mission_stops_at_a_failing_phase_and_restores_prior_grants(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    marker = tmp_path / "never.txt"
    m = _two_phase_mission(tmp_path, marker)
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.READ)  # a pre-existing grant must come back
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=FakeDriver())
    provider = ScriptedProvider([done_turn("nothing", success=False)])
    result = mission.run(m, rt, provider, runs_dir=tmp_path / "runs")
    assert not result.passed and result.stopped == "phase_failed"
    assert len(result.phases) == 1 and not result.phases[0].passed
    assert store.get_tier(APP) is safety.Tier.READ


def test_mission_from_phase_skips_earlier_phases(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    marker = tmp_path / "x.txt"
    marker.write_text("x")
    m = _two_phase_mission(tmp_path, marker)
    rt = make_runtime(tmp_path)
    result = mission.run(m, rt, ScriptedProvider([done_turn("ok")]), runs_dir=tmp_path / "runs", from_phase=2)
    assert result.passed and [p.name for p in result.phases] == ["second"]


def test_mission_cli_run_with_scripted_provider(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    marker = tmp_path / "x.txt"
    marker.write_text("x")
    m_path = tmp_path / "demo.toml"
    _two_phase_mission(tmp_path, marker)
    from a11y_computer_use import providers

    monkeypatch.setattr(providers, "get_provider",
                        lambda name=None, model=None: ScriptedProvider([
                            tool_turn("notes", {"action": "add", "text": "saved"}), done_turn("a"), done_turn("b")]))
    rt = make_runtime(tmp_path)
    monkeypatch.setattr(server, "Runtime", lambda: rt)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    code = cli.main(["mission", "run", str(m_path), "--runs-dir", str(tmp_path / "runs"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["passed"] is True and len(out["phases"]) == 2
