"""The reference agent loop, end to end against the gated Runtime with a fake
driver and a scripted planner (no TCC, no model, any OS), plus one opt-in live
run on headless Chromium.

The fake driver sets ``resolves_apps`` so app identity resolves through the
driver (like the browser backend) instead of the OS system-ops; everything
above the driver seam is the real thing: gates, recheck, Effect Receipts,
audit, and the MCP tool schemas the planner is offered.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from a11y_computer_use import agent, cli, providers, safety, server
from a11y_computer_use.providers import PlannerTurn, ProviderError, ScriptedProvider, ToolCall, Usage, done_turn, tool_turn
from a11y_computer_use.schema import ComputerUseError, Display, ErrorCode
from tests.conftest import build_synthetic_snapshot

APP = "com.test.app"

EXPECTED_TOOLS = {
    "desktop_snapshot", "find", "screenshot", "zoom", "screen_text", "click", "type", "key",
    "scroll", "drag",
    "wait_for", "act", "set_value", "scroll_to_find", "app", "window", "clipboard",
}


def tiny_png(width: int = 8, height: int = 4) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (40, 40, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeDriver:
    """A complete `Driver` over the synthetic snapshot; records what it was asked."""

    name = "fake"
    resolves_apps = True

    def __init__(self) -> None:
        self.pressed: list = []
        self.typed: list = []
        self.values: list = []
        self.snapshots = 0
        self.stale_once = False

    def ensure_trusted(self) -> None:
        pass

    def snapshot(self, scope, app):
        self.snapshots += 1
        return build_synthetic_snapshot(snapshot_id=f"snap-{self.snapshots}", app=app)

    def resolve_ref(self, snap, ref, *, live=None):
        if self.stale_once:
            self.stale_once = False
            raise ComputerUseError(ErrorCode.STALE_REF, "the tree changed", detail={"ref": ref})
        return snap.element(ref)

    def press_element(self, element) -> bool:
        self.pressed.append(element.ref)
        return True

    def scroll_into_view(self, element) -> bool:
        return True

    def set_value(self, element, value) -> bool:
        self.values.append((element.ref, value))
        return True

    def click(self, target, **kw):
        self.pressed.append(("mouse", target))

    def drag(self, start, end, **kw):
        pass

    def scroll(self, target, **kw):
        pass

    def type_text(self, text, **kw):
        self.typed.append(text)

    def key_chord(self, chord, **kw):
        self.typed.append(("key", chord))

    def wait_for(self, target, *, condition, timeout_s, checker=None):
        return target

    def screenshot(self, display_id=None):
        return SimpleNamespace(png=tiny_png(), display=Display(1, 8, 4, 1.0, True))

    def zoom_region(self, region) -> bytes:
        return tiny_png(2, 2)

    def frontmost_app(self):
        return APP, 1

    def app_at_point(self, point):
        return APP

    def running_apps(self):
        return [{"id": APP, "frontmost": True}]

    def launch_app(self, identifier) -> None:
        pass

    def activate_app(self, identifier) -> str:
        return APP

    def windows(self):
        return []

    def read_clipboard(self):
        return "clip"

    def write_clipboard(self, text) -> None:
        pass


def make_runtime(tmp_path, *, tier=safety.Tier.FULL, driver=None):
    store = safety.PermissionStore(tmp_path / "perm.json")
    if tier is not None:
        store.set_tier(APP, tier)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"),
                          driver=driver or FakeDriver())


def audit_rows(tmp_path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def observation_blocks(messages: list[dict]) -> list[dict]:
    return [b for m in messages if m["role"] == "user" for b in m["content"] if b.get("observation")]


def tool_results(messages: list[dict]) -> list[dict]:
    return [b for m in messages if m["role"] == "user" for b in m["content"]
            if b.get("type") == "tool_result"]


# --- tool surface ------------------------------------------------------------


def test_tool_specs_are_the_mcp_surface_plus_done(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    specs = agent.tool_specs(rt)
    assert {s["name"] for s in specs} == EXPECTED_TOOLS | {"done"}
    assert specs[-1]["name"] == "done"
    click = next(s for s in specs if s["name"] == "click")
    assert click["description"].startswith("Click an element ref")
    assert click["input_schema"]["type"] == "object"
    assert "ref" in click["input_schema"]["properties"]
    assert "title" not in click["input_schema"]  # pydantic decoration stripped
    assert "title" not in click["input_schema"]["properties"]["ref"]


def test_tool_specs_include_browser_feeds_only_when_the_driver_has_them(tmp_path) -> None:
    class Browserish(FakeDriver):
        def console_messages(self):
            return []

    names = {s["name"] for s in agent.tool_specs(make_runtime(tmp_path, driver=Browserish()))}
    assert "console" in names and "network" not in names


def test_call_tool_covers_the_whole_surface_and_run_once_stays_narrow(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    assert "[snap-1]" in rt.call_tool("desktop_snapshot", {"app": APP})
    assert rt.call_tool("wait_for", {"ref": "e2"}) == "e2 exists: satisfied"
    with pytest.raises(ValueError, match="unknown tool"):
        rt.dispatch("wait_for", {"ref": "e2"})  # run-once never offers refs
    with pytest.raises(ValueError, match="unknown tool"):
        rt.call_tool("frobnicate", {})


# --- the loop ------------------------------------------------------------------


def test_loop_observes_clicks_by_ref_and_finishes(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([
        tool_turn("click", {"ref": "e2"}, usage=Usage(100, 10)),
        done_turn("Save was clicked", usage=Usage(120, 8)),
    ])
    result = agent.run_task("Click Save", rt, provider, app=APP)

    assert result.success and result.stopped == "done"
    assert result.summary == "Save was clicked"
    assert [s.tool for s in result.steps] == ["click", "done"]
    assert rt.driver.pressed == ["e2"]  # a11y press, no coordinates
    step = result.steps[0]
    assert step.ok and step.error_code is None
    assert "clicked e2" in step.result and "effect:" in step.result  # Effect Receipt on by default
    assert step.params == {"ref": "e2", "verify": True}
    assert result.usage == Usage(220, 18)
    assert result.audit_dir == str(tmp_path / "audit")
    assert result.provider == "scripted" and result.app == APP

    first = provider.seen[0][0]
    assert first["role"] == "user"
    assert first["content"][0]["text"] == "Task: Click Save"
    assert "[snap-1]" in first["content"][1]["text"] and first["content"][1]["observation"]
    second = provider.seen[1]
    assert second[1]["role"] == "assistant" and second[1]["content"][0]["type"] == "tool_use"
    (res,) = tool_results(second)
    assert res["name"] == "click" and res["is_error"] is False
    assert "clicked e2" in res["content"][0]["text"]

    rows = audit_rows(tmp_path)
    kinds = [r["action"] for r in rows]
    assert "observeop" in kinds and "click" in kinds  # the gate audited the real actions
    steps = [r for r in rows if r["action"] == "agent_step"]
    assert [r["params"]["tool"] for r in steps] == ["click", "done"]
    assert sum(r["metrics"]["planner_input_tokens"] for r in steps) == 220
    run = next(r for r in rows if r["action"] == "agent_run")
    assert run["result"] == "ok" and run["params"]["steps"] == 2
    assert run["metrics"]["planner_output_tokens"] == 18


def test_stale_ref_attaches_a_fresh_observation_and_the_loop_recovers(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    rt.driver.stale_once = True
    provider = ScriptedProvider([
        tool_turn("click", {"ref": "e2"}),
        tool_turn("click", {"ref": "e2"}),
        done_turn("done after re-observe"),
    ])
    result = agent.run_task("Click Save", rt, provider, app=APP)
    assert result.success
    first, second, _done = result.steps
    assert not first.ok and first.error_code == "stale_ref"
    assert "re-observed" in first.result and "[snap-" in first.result
    assert second.ok and rt.driver.pressed == ["e2"]
    # the stale result became the newest observation; the initial one was elided
    seen = provider.seen[1]
    assert seen[0]["content"][1]["text"].startswith("[earlier observation elided")
    (stale,) = tool_results(seen)
    assert stale["observation"] and stale["is_error"]


def test_history_keeps_only_the_newest_observation(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([
        tool_turn("desktop_snapshot", {"app": APP}),
        tool_turn("desktop_snapshot", {"app": APP}),
        tool_turn("desktop_snapshot", {"app": APP}),
        done_turn("looked three times"),
    ])
    agent.run_task("Look around", rt, provider, app=APP)
    seen = provider.seen[3]  # what the planner saw before its 4th turn
    blocks = observation_blocks(seen)
    assert len(blocks) == 4
    assert all(b.get("elided") for b in blocks[:-1])
    assert "elided" in blocks[0]["text"]
    assert "elided" in blocks[1]["content"][0]["text"] and "desktop_snapshot" in blocks[1]["content"][0]["text"]
    assert "[snap-4]" in blocks[-1]["content"][0]["text"]  # the newest stays in full


def test_history_is_left_alone_when_the_provider_forbids_edits(tmp_path) -> None:
    class AppendOnly(ScriptedProvider):
        history_edits_ok = False

    rt = make_runtime(tmp_path)
    provider = AppendOnly([tool_turn("desktop_snapshot", {"app": APP}),
                           tool_turn("desktop_snapshot", {"app": APP}), done_turn("ok")])
    agent.run_task("Look", rt, provider, app=APP)
    blocks = observation_blocks(provider.seen[2])
    assert len(blocks) == 3 and not any(b.get("elided") for b in blocks)


def test_max_steps_bounds_planner_turns(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([tool_turn("desktop_snapshot", {"app": APP})] * 6)
    result = agent.run_task("Loop forever", rt, provider, app=APP, max_steps=3)
    assert result.stopped == "max_steps" and not result.success
    assert len(result.steps) == 3 and "3 planner turns" in result.summary


def test_refusals_and_bad_calls_reach_the_planner_as_results(tmp_path) -> None:
    rt = make_runtime(tmp_path, tier=safety.Tier.READ)  # observe allowed, click refused
    provider = ScriptedProvider([
        tool_turn("click", {"ref": "e2"}),
        tool_turn("frobnicate", {}),
        tool_turn("click", {}),
        tool_turn("set_value", {"ref": "e4", "value": "hunter2"}),
        done_turn("could not act", success=False),
    ])
    result = agent.run_task("Try things", rt, provider, app=APP)
    assert not result.success and result.stopped == "done"
    codes = [s.error_code for s in result.steps]
    # READ is a granted tier, so a click is a hard deny (an ungranted app would be needs_permission)
    assert codes == ["deny", "unknown_tool", "invalid_arguments", "secure_field", None]
    assert rt.driver.pressed == [] and rt.driver.values == []
    results = tool_results(provider.seen[4])
    assert all(r["is_error"] for r in results)
    assert results[0]["content"][0]["text"].startswith("deny")
    assert "frobnicate" in results[1]["content"][0]["text"]
    assert "secure_field" in results[3]["content"][0]["text"]


def test_ungranted_app_observation_error_is_the_first_observation(tmp_path) -> None:
    rt = make_runtime(tmp_path, tier=None)
    provider = ScriptedProvider([done_turn("no access", success=False)])
    result = agent.run_task("Anything", rt, provider, app=APP)
    assert not result.success
    obs = provider.seen[0][0]["content"][1]["text"]
    assert "needs_permission" in obs and APP in obs


def test_screenshot_result_carries_an_image_block(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([tool_turn("screenshot", {}), done_turn("saw it")])
    result = agent.run_task("Look", rt, provider, app=APP)
    step = result.steps[0]
    assert step.ok and step.result.startswith("display 1:")
    (res,) = tool_results(provider.seen[1])
    text, image = res["content"]
    assert image["type"] == "image" and image["media_type"] == "image/png"
    assert base64.b64decode(image["data"]).startswith(b"\x89PNG")
    assert res["observation"]


def test_older_images_are_dropped_from_history(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([tool_turn("screenshot", {}), tool_turn("screenshot", {}), done_turn("ok")])
    agent.run_task("Look twice", rt, provider, app=APP)
    first, second = tool_results(provider.seen[2])
    assert not any(b["type"] == "image" for b in first["content"])
    assert any(b["type"] == "image" for b in second["content"])


def test_several_tool_calls_in_one_turn_count_tokens_once(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    turn = PlannerTurn(tool_calls=[ToolCall("a", "click", {"ref": "e2"}),
                                   ToolCall("b", "type", {"text": "hi"})], usage=Usage(50, 5))
    provider = ScriptedProvider([turn, done_turn("both ran", usage=Usage(10, 1))])
    result = agent.run_task("Two things", rt, provider, app=APP)
    assert [s.tool for s in result.steps] == ["click", "type", "done"]
    assert result.steps[0].usage == Usage(50, 5) and result.steps[1].usage == Usage()
    assert result.usage == Usage(60, 6)
    assert rt.driver.typed == ["hi"]
    assert len(tool_results(provider.seen[1])) == 2


def test_done_alongside_other_calls_is_deferred_until_their_results_are_seen(tmp_path) -> None:
    # [click, done(success=True)] in one turn against a READ-only app: the click is
    # denied, so the planner's claimed success (made before it could see that) must
    # not end the run. The click runs, both results go back, and done is asked again.
    rt = make_runtime(tmp_path, tier=safety.Tier.READ)
    turn = PlannerTurn(tool_calls=[ToolCall("a", "click", {"ref": "e2"}),
                                   ToolCall("b", "done", {"summary": "clicked", "success": True})],
                       usage=Usage(50, 5))
    provider = ScriptedProvider([turn, done_turn("could not click", success=False)])
    result = agent.run_task("Click Save", rt, provider, app=APP)
    assert result.success is False and result.stopped == "done"
    assert [s.tool for s in result.steps] == ["click", "done"]
    assert result.steps[0].error_code == "deny" and result.summary == "could not click"
    click_res, done_res = tool_results(provider.seen[1])
    assert click_res["name"] == "click" and click_res["is_error"]
    assert done_res["tool_use_id"] == "b" and done_res["name"] == "done" and done_res["is_error"]
    assert done_res["content"][0]["text"].startswith("done_not_sole")
    assert result.usage == Usage(50, 5)  # the turn's tokens are still counted exactly once
    rows = [r for r in audit_rows(tmp_path) if r["action"] == "agent_step"]
    assert [r["result"] for r in rows] == ["deny", "done_not_sole", "ok"]


def test_calls_listed_after_done_in_the_same_turn_still_run(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    turn = PlannerTurn(tool_calls=[ToolCall("a", "done", {"summary": "early", "success": True}),
                                   ToolCall("b", "click", {"ref": "e2"}),
                                   ToolCall("c", "type", {"text": "hi"})], usage=Usage(30, 3))
    provider = ScriptedProvider([turn, done_turn("ok")])
    result = agent.run_task("Do both", rt, provider, app=APP)
    assert result.success and [s.tool for s in result.steps] == ["click", "type", "done"]
    assert rt.driver.pressed == ["e2"] and rt.driver.typed == ["hi"]  # trailing calls ran
    assert [r["name"] for r in tool_results(provider.seen[1])] == ["done", "click", "type"]
    assert result.usage == Usage(30, 3)


@pytest.mark.parametrize("raw, expected", [
    ("false", False), ("False", False), ("true", True), (0, False), (1, False), (None, False),
    ("yes", False),
])
def test_done_success_is_not_truthiness(tmp_path, raw, expected) -> None:
    # OpenAI-compatible local models and the CLI's free-text JSON emit string
    # booleans: "false" must not be recorded as a successful run.
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([tool_turn("done", {"summary": "x", "success": raw})])
    result = agent.run_task("t", rt, provider, app=APP, max_steps=2)
    assert result.success is expected and result.stopped == "done"
    done = result.steps[-1]
    assert done.error_code == "invalid_done_arguments" and not done.ok and done.params["success"] == raw
    rows = [r for r in audit_rows(tmp_path) if r["action"] == "agent_step"]
    assert rows[-1]["result"] == "invalid_done_arguments"
    run = next(r for r in audit_rows(tmp_path) if r["action"] == "agent_run")
    assert run["result"] == ("ok" if expected else "done")


def test_verify_false_skips_effect_receipts(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([tool_turn("click", {"ref": "e2"}), done_turn("ok")])
    result = agent.run_task("Click", rt, provider, app=APP, verify=False)
    assert "effect" not in result.steps[0].result and result.steps[0].params == {"ref": "e2"}


def test_planner_without_tool_calls_is_nudged_then_stopped(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    provider = ScriptedProvider([PlannerTurn(text="Let me think."), PlannerTurn(text="Still thinking.")])
    result = agent.run_task("Do it", rt, provider, app=APP)
    assert result.stopped == "no_action" and result.summary == "Still thinking."
    nudge = provider.seen[1][-1]
    assert nudge["role"] == "user" and "Reply with a tool call" in nudge["content"][0]["text"]


def test_provider_refusal_and_errors_stop_the_loop(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    refused = agent.run_task("x", rt, ScriptedProvider([PlannerTurn(text="refused", stop_reason="refusal")]), app=APP)
    assert refused.stopped == "provider_error" and refused.summary == "refused"

    class Broken:
        name = "broken"
        history_edits_ok = True

        def plan(self, messages, tools, *, system):
            raise ProviderError("HTTP 401: bad key")

    failed = agent.run_task("x", rt, Broken(), app=APP)
    assert failed.stopped == "provider_error" and "HTTP 401" in failed.summary
    assert audit_rows(tmp_path)[-1]["action"] == "agent_run"


def test_non_provider_exceptions_from_plan_still_end_the_run_with_an_audit_row(tmp_path) -> None:
    # A raw transport error (a read timeout is a builtins.TimeoutError, not a
    # ProviderError) must stop the run as provider_error, not crash out of run_task
    # before the agent_run audit row is written.
    class Exploding:
        name = "exploding"
        history_edits_ok = True

        def plan(self, messages, tools, *, system):
            raise TimeoutError("timed out")

    rt = make_runtime(tmp_path)
    result = agent.run_task("x", rt, Exploding(), app=APP, max_steps=2)
    assert result.stopped == "provider_error" and not result.success
    assert result.summary == "planner error: TimeoutError: timed out"
    run = next(r for r in audit_rows(tmp_path) if r["action"] == "agent_run")
    assert run["result"] == "provider_error" and run["params"]["steps"] == 0


def test_default_app_is_the_frontmost_and_on_step_sees_every_step(tmp_path) -> None:
    rt = make_runtime(tmp_path)
    seen: list[str] = []
    result = agent.run_task("Click", rt, ScriptedProvider([tool_turn("click", {"ref": "e2"}), done_turn("ok")]),
                            on_step=lambda s: seen.append(s.tool))
    assert result.app == APP and seen == ["click", "done"]
    assert result.to_dict()["steps"][0]["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_system_prompt_names_app_backend_and_the_ref_contract(tmp_path) -> None:
    text = agent.system_prompt(make_runtime(tmp_path), APP)
    assert APP in text and "fake" in text and "stale_ref" in text and "done" in text


# --- CLI ---------------------------------------------------------------------------


def test_cli_agent_runs_a_task_and_prints_json(tmp_path, monkeypatch, capsys) -> None:
    rt = make_runtime(tmp_path, tier=None)
    monkeypatch.setattr(server, "Runtime", lambda: rt)
    provider = ScriptedProvider([tool_turn("click", {"ref": "e2"}), done_turn("clicked")])
    monkeypatch.setattr(providers, "get_provider", lambda name, model=None: provider)
    code = cli.main(["agent", "--task", "Click Save", "--app", APP, "--grant", "full", "--json"])
    out, err = capsys.readouterr()
    assert code == 0
    assert f"granted {APP} tier full" in err and "[step 1] click" in err
    payload = json.loads(out)
    assert payload["success"] is True and payload["steps"][1]["tool"] == "done"
    assert rt.store.get_tier(APP) is safety.Tier.FULL


def test_cli_agent_reports_failure_with_exit_1_and_provider_errors_with_exit_2(tmp_path, monkeypatch, capsys) -> None:
    rt = make_runtime(tmp_path)
    monkeypatch.setattr(server, "Runtime", lambda: rt)
    monkeypatch.setattr(providers, "get_provider",
                        lambda name, model=None: ScriptedProvider([done_turn("gave up", success=False)]))
    assert cli.main(["agent", "--task", "x", "--app", APP]) == 1
    assert "not completed" in capsys.readouterr().out

    def no_provider(name, model=None):
        raise ProviderError("no planner available")

    monkeypatch.setattr(providers, "get_provider", no_provider)
    assert cli.main(["agent", "--task", "x"]) == 2
    assert "no planner available" in capsys.readouterr().err


# --- live: headless Chromium (opt-in) -----------------------------------------------


def _live_endpoint() -> str | None:
    from a11y_computer_use.drivers import _cdp

    endpoint = os.environ.get("A11Y_COMPUTER_USE_CDP_ENDPOINT", "http://127.0.0.1:9222")
    try:
        _cdp.page_targets(endpoint)
        return endpoint
    except Exception:
        return None


def _latest_observation(messages: list[dict]) -> str:
    block = observation_blocks(messages)[-1]
    if block["type"] == "tool_result":
        return "\n".join(b.get("text", "") for b in block["content"] if b.get("type") == "text")
    return block["text"]


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_browser_agent_loop_fills_and_clicks_by_ref(tmp_path) -> None:
    """A scripted planner drives the real browser backend through the loop:
    set_value on the input, click the button, done; the page proves both."""
    import urllib.parse

    from a11y_computer_use.drivers.browser import BrowserDriver

    d = BrowserDriver(endpoint=_live_endpoint())
    sess = d._connect()
    tab = d.frontmost_app()[0]
    html = ("<h1>form</h1><label>Name <input id=t></label>"
            "<button id=b onclick=\"document.title='CLICKED'\">Go</button>")
    d.navigate("data:text/html," + urllib.parse.quote(html))
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(tab, safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)

    def fill(messages):
        ref = re.search(r"(e\d+) textfield", _latest_observation(messages)).group(1)
        return tool_turn("set_value", {"ref": ref, "value": "hello"})

    def click_go(messages):
        ref = re.search(r'(e\d+) button "Go"', _latest_observation(messages)).group(1)
        return tool_turn("click", {"ref": ref})

    provider = ScriptedProvider([fill, click_go, done_turn("filled and clicked")])
    try:
        result = agent.run_task("Type hello into Name and press Go", rt, provider, app=tab)
        assert result.success, result.summary
        assert [s.tool for s in result.steps] == ["set_value", "click", "done"]
        assert all(s.ok for s in result.steps)
        title = sess.call("Runtime.evaluate", {"expression": "document.title"})["result"]["value"]
        value = sess.call("Runtime.evaluate",
                          {"expression": "document.getElementById('t').value"})["result"]["value"]
        assert title == "CLICKED" and value == "hello"
        assert "effect:" in result.steps[1].result  # Effect Receipt from the real page
    finally:
        d._reset()
