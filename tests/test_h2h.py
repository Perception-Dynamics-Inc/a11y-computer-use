"""cu-arena head-to-head (`computeruse.h2h`).

Hermetic tests pin the task suite (every manifest valid, every target a real
id), the scoring math (misclicks, wasted actions, aggregates, cost), the report
rendering, and the pixel loop's mapping onto the Anthropic adapter over a
scripted CDP transport (no browser). Opt-in live tests drive real headless
Chromium with scripted planners in both modes and prove the success predicates
and misclick counting on the real fixtures.
"""

from __future__ import annotations

import io
import json
import os
import re

import pytest

from computeruse import cli, h2h, safety, server
from computeruse.agent import Step
from computeruse.providers import PlannerTurn, ScriptedProvider, Usage, done_turn, tool_turn
from computeruse.schema import Scope

from tests.test_browser import _AX_NODES, _DOM_SNAPSHOT, ScriptedTransport, _driver_on

# --------------------------------------------------------------------------- #
# task suite
# --------------------------------------------------------------------------- #


def test_every_task_manifest_is_valid_and_instrumented() -> None:
    tasks = h2h.load_tasks()
    assert len(tasks) >= 10
    problems = [p for t in tasks for p in h2h.validate_task(t)]
    assert problems == []
    assert len({t.id for t in tasks}) == len(tasks)
    for t in tasks:
        assert t.html_path.exists() and t.instruction and t.allowed_targets
        html = t.html_path.read_text(encoding="utf-8")
        assert "_cu.js" in html  # click/input instrumentation on every page
        assert not re.search(r"window\.(confirm|alert|prompt)\(", html)  # would block CDP


def test_load_tasks_subset_and_unknown_id() -> None:
    subset = h2h.load_tasks(["tabs", "form_fill"])
    assert [t.id for t in subset] == ["tabs", "form_fill"]
    with pytest.raises(KeyError):
        h2h.load_tasks(["nope"])


def test_native_dropdown_is_flagged_not_comparable_for_pixel_modes_only() -> None:
    native, custom = h2h.load_tasks(["dropdown", "dropdown_custom"])
    assert native.comparability("refs") is None
    assert "select popup" in (native.comparability("pixels") or "")
    assert native.comparability("pixels+snap")
    assert custom.not_comparable == () and custom.comparability("pixels") is None
    assert custom.instruction == native.instruction  # same task, DOM-rendered options


def test_validate_task_rejects_bad_not_comparable(tmp_path) -> None:
    (tmp_path / "p.html").write_text('<script src="_cu.js"></script><button id="b"></button>', encoding="utf-8")
    spec = h2h.TaskSpec(id="p", title="p", page="p.html", instruction="do", success="1",
                        allowed_targets=("b",), max_steps=3, directory=tmp_path,
                        not_comparable=(("telepathy", "x"), ("pixels", "  ")))
    joined = " ".join(h2h.validate_task(spec))
    assert "unknown mode 'telepathy'" in joined and "needs a reason" in joined


def test_validate_task_reports_problems(tmp_path) -> None:
    page = tmp_path / "t.html"
    page.write_text("<html><body><button id='ok'>x</button>"
                    "<iframe src='inner.html'></iframe></body></html>", encoding="utf-8")
    (tmp_path / "inner.html").write_text("<html><body><input id='deep'></body></html>", encoding="utf-8")
    spec = h2h.TaskSpec(id="t", title="t", page="t.html", instruction="do", success="(1",
                        allowed_targets=("ok", "deep", "missing"), max_steps=0, directory=tmp_path)
    joined = " ".join(h2h.validate_task(spec))
    assert "not instrumented" in joined and "'missing'" in joined and "'deep'" not in joined
    assert "does not forward clicks" in joined  # the iframe page lacks cu-click
    assert "unbalanced" in joined and "max_steps" in joined
    missing_page = h2h.TaskSpec(id="m", title="m", page="nope.html", instruction="do", success="1",
                                allowed_targets=(), max_steps=1, directory=tmp_path)
    assert h2h.validate_task(missing_page) == ["m: page nope.html does not exist"]


def test_fixture_server_serves_pages_and_assets() -> None:
    import urllib.request

    with h2h.FixtureServer() as srv:
        assert srv.base_url.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(srv.url_for("form_fill.html"), timeout=5) as resp:
            body = resp.read().decode()
        assert "<title>Contact</title>" in body
        with urllib.request.urlopen(srv.url_for("_cu.js"), timeout=5) as resp:
            assert b"__cuState" in resp.read()


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def test_count_misclicks_counts_unknown_targets_and_bare_tags() -> None:
    clicks = [{"target": "send"}, {"target": "reset"}, {"target": "body"}, {"target": "name"}]
    assert h2h.count_misclicks(clicks, ("name", "email", "send")) == 2
    assert h2h.count_misclicks([], ("x",)) == 0


def test_estimate_cost_needs_both_prices() -> None:
    assert h2h.estimate_cost(1_000_000, 1_000_000, 3.0, 15.0) == 18.0
    assert h2h.estimate_cost(10, 10, None, 15.0) is None


def _rec(task, mode, success, turns, misclicks=0, wasted=0, tin=100, tout=10, cost=0.0,
         comparable=True, note=""):
    return h2h.RunRecord(task_id=task, mode=mode, round=1, success=success, stopped="done",
                         turns=turns, actions=turns - 1, misclicks=misclicks, wasted=wasted,
                         input_tokens=tin, output_tokens=tout, cost_reported_usd=cost,
                         wall_s=2.0, summary="s", comparable=comparable, note=note)


def test_aggregate_reports_completion_with_and_without_non_comparable_tasks() -> None:
    records = [
        _rec("a", "refs", True, 2), _rec("d", "refs", True, 3),
        _rec("a", "pixels", True, 4), _rec("d", "pixels", False, 8, comparable=False, note="popup not painted"),
    ]
    agg = h2h.aggregate(records)
    # raw rates keep every run; the comparable rate drops task "d" for BOTH modes
    assert agg["refs"]["completion_rate"] == 1.0 and agg["pixels"]["completion_rate"] == 0.5
    assert agg["refs"]["comparable_runs"] == 1 and agg["pixels"]["comparable_runs"] == 1
    assert agg["refs"]["comparable_rate"] == 1.0 and agg["pixels"]["comparable_rate"] == 1.0
    assert agg["refs"]["excluded_tasks"] == ["d"]
    text = h2h.format_report(h2h.H2HReport(records=records))
    assert "rate (comparable tasks)" in text and "leaves out d" in text
    assert "| d | pixels | 1 | no | done | 8 | 7 | 0 | 0 | 100/10 | 2s | not comparable: popup not painted |" in text
    assert "- d / pixels (round 1) [not comparable]:" in text


def test_report_round_trips_through_json() -> None:
    records = [_rec("a", "refs", True, 2, cost=0.1), _rec("a", "pixels", False, 5, comparable=False, note="n")]
    report = h2h.H2HReport(records=records, meta={"planner": "x", "date": "2026-09-02"})
    again = h2h.H2HReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert again.meta == report.meta
    assert [r.to_dict() for r in again.records] == [r.to_dict() for r in records]
    assert h2h.format_report(again) == h2h.format_report(report)
    # unknown keys in a saved record are ignored, not fatal
    loose = {**records[0].to_dict(), "future_field": 1}
    assert h2h.RunRecord.from_dict(loose).task_id == "a"


def test_aggregate_and_report_render_both_modes() -> None:
    records = [
        _rec("a", "refs", True, 3, cost=0.1), _rec("b", "refs", False, 8, misclicks=1, wasted=2, cost=0.2),
        _rec("a", "pixels", True, 5, misclicks=2, cost=0.3), _rec("b", "pixels", True, 6, wasted=1, cost=0.4),
    ]
    agg = h2h.aggregate(records, price_in=1.0, price_out=10.0)
    assert list(agg) == ["refs", "pixels"]  # MODES order, not alphabetical
    assert agg["refs"]["completed"] == 1 and agg["refs"]["completion_rate"] == 0.5
    assert agg["refs"]["misclicks"] == 1 and agg["refs"]["wasted"] == 2
    assert agg["pixels"]["completed"] == 2 and agg["pixels"]["mean_turns"] == 5.5
    assert agg["refs"]["cost_reported_usd"] == pytest.approx(0.3)
    assert agg["refs"]["cost_estimated_usd"] == pytest.approx((200 * 1.0 + 20 * 10.0) / 1e6)

    report = h2h.H2HReport(records=records, meta={"planner": "scripted", "date": "2026-09-02",
                                                  "price_in": 1.0, "price_out": 10.0})
    text = h2h.format_report(report)
    assert "| refs | 1/2 | 50% |" in text and "| pixels | 2/2 | 100% |" in text
    assert "cost (reported)" in text and "cost (est.)" in text
    assert "| b | refs | 1 | no | done |" in text
    assert "Failed runs:" in text and "b / refs" in text
    assert "—" not in text and "–" not in text
    as_dict = report.to_dict()
    assert set(as_dict) == {"meta", "aggregate", "records"} and len(as_dict["records"]) == 4
    json.dumps(as_dict)  # serialisable


def test_format_report_without_runs() -> None:
    assert "no runs" in h2h.format_report(h2h.H2HReport())


def test_counting_provider_counts_turns_and_sums_usage_and_cost() -> None:
    inner = ScriptedProvider([
        tool_turn("click", {"ref": "e1"}, usage=Usage(10, 1, 0.5)),
        done_turn("ok", usage=Usage(20, 2, 0.25)),
    ])
    p = h2h.CountingProvider(inner)
    p.plan([], [], system="s")
    p.plan([], [], system="s")
    assert p.turns == 2
    assert p.usage == Usage(30, 3, 0.75) and p.usage.cost_usd == pytest.approx(0.75)
    assert p.name == "scripted" and p.history_edits_ok is True


def test_usage_cost_is_additive_and_defaults_to_zero() -> None:
    assert Usage(1, 2) + Usage(3, 4) == Usage(4, 6, 0.0)
    assert (Usage(1, 2, 0.1) + Usage(1, 1, 0.2)).cost_usd == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# pixel loop over a scripted CDP transport (no browser)
# --------------------------------------------------------------------------- #


def _real_png(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _pixel_responder(method: str, params: dict):
    """The tests.test_browser fixture page, with a real 800x600 PNG for screenshots."""
    import base64

    if method == "Page.captureScreenshot":
        return {"data": base64.b64encode(_real_png(800, 600)).decode()}
    if method == "Accessibility.getFullAXTree":
        return {"nodes": _AX_NODES}
    if method == "Page.getFrameTree":
        return {"frameTree": {"frame": {"id": "MAIN"}, "childFrames": []}}
    if method == "DOMSnapshot.captureSnapshot":
        return _DOM_SNAPSHOT
    if method == "DOM.resolveNode":
        return {"object": {"objectId": f"obj-{params.get('backendNodeId')}"}}
    if method == "Runtime.callFunctionOn":
        return {"result": {"type": "undefined"}}
    if method == "Page.getLayoutMetrics":
        return {"cssVisualViewport": {"pageX": 0, "pageY": 0},
                "cssContentSize": {"width": 800, "height": 600}}
    if method == "Runtime.evaluate":
        if "activeElement" in params.get("expression", ""):
            return {"result": {"type": "boolean", "value": False}}
        return {"result": {"type": "string", "value": "state"}}
    if method in ("Input.insertText", "Input.dispatchKeyEvent", "Input.dispatchMouseEvent",
                  "DOM.enable", "Page.enable", "Runtime.enable", "Log.enable", "Network.enable",
                  "Runtime.releaseObject"):
        return {}
    raise AssertionError(f"unexpected CDP method {method}")


def _pixel_runtime(tmp_path):
    d, transport = _driver_on(_pixel_responder)
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("TAB1", safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)
    return rt, transport


def test_pixel_loop_pure_coordinates_dispatches_mouse_events(tmp_path) -> None:
    rt, transport = _pixel_runtime(tmp_path)
    seen: list[Step] = []
    provider = ScriptedProvider([
        tool_turn("left_click", {"x": 20, "y": 20}, usage=Usage(100, 5, 0.01)),  # inside "Save"
        tool_turn("type", {"text": "hi"}),
        tool_turn("key", {"text": "Return"}),
        tool_turn("scroll", {"x": 400, "y": 300, "direction": "down", "amount": 2}),
        done_turn("clicked save"),
    ])
    result = h2h.run_pixel_task("press Save", rt, provider, app="TAB1", snap_to_refs=False,
                                max_steps=10, on_step=seen.append)
    assert result.success and result.stopped == "done"
    assert [s.tool for s in result.steps] == ["left_click", "type", "key", "scroll", "done"]
    assert all(s.ok for s in result.steps), [s.result for s in result.steps]
    methods = transport.methods()
    assert "Input.dispatchMouseEvent" in methods  # the incumbent: a raw coordinate click
    assert "Input.insertText" in methods and "Input.dispatchKeyEvent" in methods
    assert not any(m == "Runtime.callFunctionOn" for m in methods)  # never went through a ref
    assert result.usage.input_tokens == 100 and result.usage.cost_usd == pytest.approx(0.01)
    # the planner saw the initial screenshot and a fresh one after every action
    first = provider.seen[0]
    assert any(b.get("type") == "image" for b in first[0]["content"])
    after_click = provider.seen[1][-1]["content"][0]
    assert after_click["type"] == "tool_result" and after_click["observation"] is True
    assert any(b.get("type") == "image" for b in after_click["content"])
    assert seen[0].tool == "left_click" and "image point (20, 20)" in seen[0].result


def test_pixel_loop_with_snap_executes_the_click_as_a_ref(tmp_path) -> None:
    rt, transport = _pixel_runtime(tmp_path)
    provider = ScriptedProvider([tool_turn("left_click", {"x": 20, "y": 20}), done_turn("ok")])
    result = h2h.run_pixel_task("press Save", rt, provider, app="TAB1", snap_to_refs=True, max_steps=5)
    assert result.success
    assert "snapped" in result.steps[0].result and "Save" in result.steps[0].result
    methods = transport.methods()
    assert "Runtime.callFunctionOn" in methods  # this.click() on the resolved node
    assert "Input.dispatchMouseEvent" not in methods


def test_pixel_loop_unknown_tool_and_max_steps(tmp_path) -> None:
    rt, _ = _pixel_runtime(tmp_path)
    provider = ScriptedProvider([tool_turn("teleport", {}), tool_turn("screenshot", {})])
    result = h2h.run_pixel_task("x", rt, provider, app="TAB1", snap_to_refs=False, max_steps=2)
    assert not result.success and result.stopped == "max_steps"
    assert result.steps[0].error_code == "unknown_tool" and result.steps[1].tool == "screenshot"


def test_pixel_loop_planner_without_tool_calls_is_nudged_then_stopped(tmp_path) -> None:
    rt, _ = _pixel_runtime(tmp_path)
    provider = ScriptedProvider([PlannerTurn(text="hmm"), PlannerTurn(text="still thinking")])
    result = h2h.run_pixel_task("x", rt, provider, app="TAB1", snap_to_refs=False, max_steps=5)
    assert result.stopped == "no_action" and not result.success


def test_pixel_tools_are_the_incumbent_surface_only() -> None:
    names = [t["name"] for t in h2h.PIXEL_TOOLS]
    assert names == ["screenshot", "left_click", "double_click", "type", "key", "scroll", "done"]
    assert "desktop_snapshot" not in names and "find" not in names  # never the tree
    assert "800x600" in h2h.pixel_system_prompt(800, 600)


# --------------------------------------------------------------------------- #
# harness over the scripted transport: wasted-action and misclick accounting
# --------------------------------------------------------------------------- #


def test_harness_scores_a_scripted_refs_run_without_a_browser(tmp_path, monkeypatch) -> None:
    """A Harness on the scripted transport: navigate/evaluate are answered by the
    responder, so the accounting path (state digest, clicks, success) is exercised
    end to end with no Chrome."""
    state = {"digest": "A", "clicks": '[{"target":"reset"},{"target":"send"}]', "title": True}

    def responder(method, params):
        if method == "Page.navigate":
            return {"frameId": "MAIN"}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "readyState" in expr:
                return {"result": {"value": "complete"}}
            if "__cuState" in expr:
                return {"result": {"value": state["digest"]}}
            if "__cu.clicks" in expr:
                return {"result": {"value": state["clicks"]}}
            return {"result": {"value": state["title"]}}  # the success predicate
        return _pixel_responder(method, params)

    d, transport = _driver_on(responder)
    spec = h2h.load_tasks(["form_fill"])[0]

    def factory(mode):
        assert mode == "refs"
        # click the Save button by ref (state digest never changes -> counted as wasted)
        return ScriptedProvider([
            lambda messages: tool_turn("click", {"ref": _ref_for(messages, 'button "Save"')}),
            done_turn("pressed"),
        ])

    events: list[str] = []
    harness = h2h.Harness(d, factory, workdir=tmp_path, on_event=lambda k, p: events.append(k))
    record = harness.run_one(spec, "refs", "http://fixture/form_fill.html")
    assert record.success is True and record.stopped == "done"
    assert record.turns == 2 and record.actions == 1
    assert record.wasted == 1  # digest "A" before and after the click
    assert record.misclicks == 1  # "reset" is not an allowed target, "send" is
    assert record.clicks and record.steps[0]["tool"] == "click"
    assert events == ["step", "step", "run"]
    assert ("Page.navigate", {"url": "http://fixture/form_fill.html"}) in transport.sent
    # the benchmark's own permission store, never the user's
    assert (tmp_path / "permissions.json").exists()


def _ref_for(messages, needle: str) -> str:
    blocks = [b for m in messages if m["role"] == "user" for b in m["content"] if b.get("observation")]
    block = blocks[-1]
    text = block.get("text") or "\n".join(b.get("text", "") for b in block.get("content", []))
    return re.search(r"(e\d+) " + re.escape(needle), text).group(1)


def test_harness_auto_approves_the_confirmation_gate_in_both_loops(tmp_path, monkeypatch) -> None:
    """A ref click on a "Delete" button trips the Runtime's irreversible-action
    classifier; a coordinate click carries no title and does not. The harness
    hands the same approving callback to both loops so the gate cannot decide
    the comparison."""
    captured: dict[str, object] = {}

    def fake_run_task(task, runtime, provider, **kw):
        captured["refs"] = kw.get("confirm")
        return h2h.AgentResult(task=task, app="TAB1", provider="scripted", model=None, success=True,
                               summary="", stopped="done")

    def fake_pixel(task, runtime, provider, **kw):
        captured["pixels"] = kw.get("confirm")
        return h2h.AgentResult(task=task, app="TAB1", provider="scripted", model=None, success=True,
                               summary="", stopped="done")

    real_pixel = h2h.run_pixel_task
    monkeypatch.setattr(h2h.agent, "run_task", fake_run_task)
    monkeypatch.setattr(h2h, "run_pixel_task", fake_pixel)

    def responder(method, params):
        if method == "Page.navigate":
            return {"frameId": "MAIN"}
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            if "readyState" in expr:
                return {"result": {"value": "complete"}}
            if "__cu.clicks" in expr:
                return {"result": {"value": "[]"}}
            return {"result": {"value": "x"}}
        return _pixel_responder(method, params)

    d, _ = _driver_on(responder)
    spec = h2h.load_tasks(["modal_confirm"])[0]
    harness = h2h.Harness(d, lambda mode: ScriptedProvider([]), workdir=tmp_path)
    harness.run_one(spec, "refs", "http://fixture/modal_confirm.html")
    harness.run_one(spec, "pixels", "http://fixture/modal_confirm.html")
    assert captured["refs"] is h2h.auto_confirm and captured["pixels"] is h2h.auto_confirm
    assert h2h.auto_confirm('Confirm a potentially irreversible action: click "Delete"') is True
    # and the pixel loop threads it into the adapter
    rt, _ = _pixel_runtime(tmp_path)
    provider = ScriptedProvider([done_turn("ok")])
    sentinel = lambda prompt: False  # noqa: E731
    real_adapter = h2h.AnthropicComputerAdapter
    seen: dict[str, object] = {}

    class Spy(real_adapter):
        def __init__(self, runtime, **kw):
            seen["confirm"] = kw.get("confirm")
            super().__init__(runtime, **kw)

    monkeypatch.setattr(h2h, "AnthropicComputerAdapter", Spy)
    real_pixel("x", rt, provider, app="TAB1", snap_to_refs=False, confirm=sentinel)
    assert seen["confirm"] is sentinel


def test_harness_rejects_unknown_mode(tmp_path) -> None:
    d, _ = _driver_on(_pixel_responder)
    harness = h2h.Harness(d, lambda mode: ScriptedProvider([]), workdir=tmp_path)
    with pytest.raises(ValueError, match="unknown mode"):
        harness.run_one(h2h.load_tasks(["tabs"])[0], "telepathy", "http://x")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_bench_h2h_render_merges_saved_json_and_applies_current_notes(tmp_path, capsys) -> None:
    a = h2h.H2HReport(records=[_rec("dropdown", "refs", True, 2), _rec("dropdown", "pixels", False, 8)],
                      meta={"planner": "claude-cli", "date": "2026-09-02"})
    b = h2h.H2HReport(records=[_rec("tabs", "refs", True, 2), _rec("tabs", "pixels", True, 3)],
                      meta={"planner": "claude-cli", "date": "2026-09-02"})
    (tmp_path / "a.json").write_text(json.dumps(a.to_dict()), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(b.to_dict()), encoding="utf-8")
    assert cli.main(["bench", "h2h", "--render", str(tmp_path / "a.json"), str(tmp_path / "b.json"),
                     "--out", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    # the saved pixels run had no note; the current dropdown manifest flags it
    assert "not comparable: headless Chrome does not paint the native select popup" in out
    assert "| refs | 2/2 | 100% | 1/1 (100%) |" in out and "| pixels | 1/2 | 50% | 1/1 (100%) |" in out
    merged = json.loads((tmp_path / "out" / "h2h.json").read_text(encoding="utf-8"))
    assert len(merged["records"]) == 4 and merged["meta"]["merged_from"]
    assert cli.main(["bench", "h2h", "--render", str(tmp_path / "missing.json")]) == 2


def test_cli_bench_h2h_list_and_bad_arguments(capsys) -> None:
    assert cli.main(["bench", "h2h", "--list"]) == 0
    out = capsys.readouterr().out
    assert "form_fill" in out and "similar_buttons" in out
    assert "dropdown         Select from a native dropdown" in out and "[pixels: not comparable]" in out
    assert cli.main(["bench", "h2h", "--tasks", "nope", "--provider", "claude-cli"]) == 2
    assert "unknown task" in capsys.readouterr().err
    assert cli.main(["bench", "h2h", "--modes", "refs,telepathy", "--provider", "claude-cli"]) == 2
    assert "unknown mode" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# live: headless Chromium (opt-in)
# --------------------------------------------------------------------------- #


def _live_endpoint() -> str | None:
    from computeruse.drivers import _cdp

    endpoint = os.environ.get("COMPUTERUSE_CDP_ENDPOINT", "http://127.0.0.1:9222")
    try:
        _cdp.page_targets(endpoint)
        return endpoint
    except Exception:
        return None


def _center(sess, selector: str) -> tuple[int, int]:
    """CSS-pixel centre of an element via CDP (screenshot space at scale 1, no downscale)."""
    expr = (f"(function(){{var r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
            "return [Math.round(r.left+r.width/2+window.scrollX), Math.round(r.top+r.height/2+window.scrollY)];})()")
    value = sess.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})["result"]["value"]
    return int(value[0]), int(value[1])


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set COMPUTERUSE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_h2h_scripted_planners_in_both_modes(tmp_path) -> None:
    """Scripted planners solve real fixtures through both loops; a deliberate
    wrong click proves misclick counting on the real instrumentation."""
    from computeruse.drivers.browser import BrowserDriver

    d = BrowserDriver(endpoint=_live_endpoint())
    sess = d._connect()
    tasks = {t.id: t for t in h2h.load_tasks(["similar_buttons", "form_fill", "tabs"])}
    plans: dict[str, list] = {}

    def factory(mode):
        return ScriptedProvider(plans.pop("next"))

    harness = h2h.Harness(d, factory, workdir=tmp_path)
    try:
        with h2h.FixtureServer() as srv:
            # refs: similar_buttons by ref, then form_fill via act
            plans["next"] = [lambda m: tool_turn("click", {"ref": _ref_for(m, 'button "Archive"')}),
                             done_turn("archived")]
            rec = harness.run_one(tasks["similar_buttons"], "refs", srv.url_for("similar_buttons.html"))
            assert rec.success and rec.misclicks == 0 and rec.actions == 1 and rec.wasted == 0, rec

            plans["next"] = [
                lambda m: tool_turn("set_value", {"ref": _ref_for(m, 'textfield "Full name"'),
                                                  "value": "Ada Lovelace"}),
                lambda m: tool_turn("set_value", {"ref": _ref_for(m, 'textfield "Email address"'),
                                                  "value": "ada@example.com"}),
                lambda m: tool_turn("click", {"ref": _ref_for(m, 'button "Send"')}),
                done_turn("sent"),
            ]
            rec = harness.run_one(tasks["form_fill"], "refs", srv.url_for("form_fill.html"))
            assert rec.success and rec.misclicks == 0 and rec.actions == 3 and rec.wasted == 0, rec

            # pixels: click by real coordinates (page loaded first so we can measure)
            d.navigate(srv.url_for("tabs.html"))
            billing, download = _center(sess, "#tab-billing"), None
            plans["next"] = [
                tool_turn("left_click", {"x": billing[0], "y": billing[1]}),
                lambda m: tool_turn("left_click", dict(zip(("x", "y"), _center(sess, "#download-invoice")))),
                done_turn("downloaded"),
            ]
            rec = harness.run_one(tasks["tabs"], "pixels", srv.url_for("tabs.html"))
            assert rec.success and rec.misclicks == 0 and rec.actions == 2, rec

            # pixels, deliberately wrong: Delete instead of Archive -> failure + 1 misclick
            d.navigate(srv.url_for("similar_buttons.html"))
            wrong = _center(sess, "#btn-delete")
            plans["next"] = [tool_turn("left_click", {"x": wrong[0], "y": wrong[1]}), done_turn("oops", success=False)]
            rec = harness.run_one(tasks["similar_buttons"], "pixels", srv.url_for("similar_buttons.html"))
            assert not rec.success and rec.misclicks == 1 and rec.wasted == 0, rec

            # pixels+snap: the same right click lands as a ref click
            d.navigate(srv.url_for("similar_buttons.html"))
            right = _center(sess, "#btn-archive")
            plans["next"] = [tool_turn("left_click", {"x": right[0], "y": right[1]}), done_turn("ok")]
            rec = harness.run_one(tasks["similar_buttons"], "pixels+snap", srv.url_for("similar_buttons.html"))
            assert rec.success and rec.misclicks == 0, rec
            assert "snapped" in rec.steps[0]["result"]
    finally:
        d.close()


@pytest.mark.skipif(_live_endpoint() is None, reason="no live CDP endpoint")
def test_live_scroll_to_find_reaches_an_item_deep_in_an_overflow_list(tmp_path) -> None:
    """The long_list fixture: Reykjavik is 3,000 px down inside a 380 px scroll box,
    so it is pruned from the first snapshot; scroll_to_find must scroll the LIST
    (wheel over it, positive dy = down) until the item is observable, then a ref
    click selects it."""
    from computeruse.drivers.browser import BrowserDriver

    d = BrowserDriver(endpoint=_live_endpoint())
    sess = d._connect()
    try:
        with h2h.FixtureServer() as srv:
            d.navigate(srv.url_for("long_list.html"))
            tab = d.frontmost_app()[0]
            store = safety.PermissionStore(tmp_path / "perm.json")
            store.set_tier(tab, safety.Tier.FULL)
            rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)
            assert "Reykjavik" not in rt.desktop_snapshot(tab)
            # Reykjavik sits about 3,200 px down; each scroll step is 5 wheel lines (200 px)
            found = rt.scroll_to_find(tab, text="Reykjavik", role="button", max_scrolls=20)
            assert found.startswith("found after"), found
            ref = re.search(r"(e\d+) button \"Reykjavik\"", found).group(1)
            top = sess.call("Runtime.evaluate", {"expression": "document.getElementById('cities').scrollTop",
                                                 "returnByValue": True})["result"]["value"]
            assert top > 0
            rt.click(ref=ref)
            title = sess.call("Runtime.evaluate", {"expression": "document.title",
                                                   "returnByValue": True})["result"]["value"]
            assert title == "CITY:Reykjavik"
    finally:
        d.close()


@pytest.mark.skipif(_live_endpoint() is None, reason="no live CDP endpoint")
def test_live_every_fixture_loads_and_success_predicate_is_false_initially() -> None:
    from computeruse.drivers.browser import BrowserDriver

    d = BrowserDriver(endpoint=_live_endpoint())
    sess = d._connect()
    try:
        with h2h.FixtureServer() as srv:
            for spec in h2h.load_tasks():
                d.navigate(srv.url_for(spec.page))
                value = sess.call("Runtime.evaluate", {"expression": f"!!({spec.success})",
                                                       "returnByValue": True})["result"]["value"]
                assert value is False, spec.id
                cu = sess.call("Runtime.evaluate", {"expression": "typeof window.__cu.clicks",
                                                    "returnByValue": True})["result"]["value"]
                assert cu == "object", spec.id
                snap = d.snapshot(Scope.WINDOW, d.frontmost_app()[0])
                assert any(el.actionable for el in snap.elements), spec.id
    finally:
        d.close()
