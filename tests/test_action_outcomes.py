"""Typed action outcomes. Hermetic: a fake driver and a scripted agent.

The sentence each tool already returned stays the string value. ``outcome``,
``next``, and ``evidence`` are attributes, and the MCP tool result puts them
in structured content. A live process is used only for the crash case, where
the pid has to really exit.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import time

import pytest

from a11y_computer_use import outcome, safety, server
from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Scope,
    Snapshot,
)
from tests.test_agent_core import FakeRuntime, done, el, run, turn, window

APP = "demo"


def _element(ref: str, role: str, title: str, **kwargs) -> Element:
    bounds = kwargs.pop("bounds", Bounds(0, 10, 20, 80, 24))
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=kwargs.pop("value", None),
        bounds=bounds,
        snapshot_id="snap",
        **kwargs,
    )


def _tree() -> list[Element]:
    return [
        _element("e1", "AXWindow", "Demo", path=("AXWindow",)),
        _element(
            "e2", "AXButton", "Save", parent="e1", path=("AXWindow", "AXButton"),
            clickable=True,
        ),
        _element(
            "e3", "AXTextField", "Name", parent="e1", path=("AXWindow", "AXTextField"),
            value="", editable=True, focused=True, clickable=True,
        ),
        _element(
            "e4", "AXStaticText", "Idle label", parent="e1",
            path=("AXWindow", "AXStaticText"), clickable=True,
        ),
        _element(
            "e5", "AXStaticText", "ready", parent="e1",
            path=("AXWindow", "AXStaticText"),
        ),
        _element(
            "e6", "AXPopUpButton", "Color", parent="e1",
            path=("AXWindow", "AXPopUpButton"), value="Red", clickable=True,
        ),
        _element(
            "e7", "AXList", "Rows", parent="e1", path=("AXWindow", "AXList"),
            bounds=Bounds(0, 10, 80, 200, 120),
        ),
        _element(
            "e8", "AXButton", "Publish", parent="e1", path=("AXWindow", "AXButton"),
            clickable=True, enabled=False,
        ),
    ]


class TreeDriver:
    """Mutable accessibility tree. ``resolves_apps`` keeps the test off the OS."""

    name = "fake"
    resolves_apps = True

    def __init__(self) -> None:
        self.elements = _tree()
        self.pid = 4242
        self.front = APP
        self.menu = {"open": False, "path": []}
        self.rows = [{
            "window_id": 7,
            "app": APP,
            "title": "Demo",
            "on_screen": True,
            "bounds": {"display_id": 0, "x": 1, "y": 2, "width": 300, "height": 200},
        }]
        self.apps = [{"app": APP, "name": "Demo", "pid": 4242}]
        self.dead = False
        self.apply_type = False
        self.move_lands = True
        self.calls: list[tuple] = []

    def ensure_trusted(self) -> None:
        return None

    def frontmost_app(self):
        return self.front, self.pid

    def main_display_id(self) -> int:
        return 0

    def snapshot(self, scope, app) -> Snapshot:
        if self.dead:
            raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "gone", detail={"app": app})
        return Snapshot(
            snapshot_id="snap",
            scope=scope,
            app=app,
            pid=self.pid,
            created_at=0.0,
            displays=(Display(0, 800, 600, 1.0, True),),
            elements=tuple(self.elements),
        )

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        del live
        return snap.element(ref)

    def _replace(self, ref: str, **changes) -> None:
        self.elements = [
            dataclasses.replace(item, **changes) if item.ref == ref else item
            for item in self.elements
        ]

    def press_element(self, element: Element) -> bool:
        self.calls.append(("press", element.ref))
        if element.ref == "e2":
            self._replace("e5", title="saved")
            return True
        if element.ref == "e4":
            return True
        return False

    def click(self, target, **kwargs) -> None:
        self.calls.append(("click", getattr(target, "ref", None), kwargs))

    def type_text(self, text: str, **kwargs) -> int:
        del kwargs
        self.calls.append(("type", text))
        if self.apply_type:
            current = next(item for item in self.elements if item.ref == "e3")
            self._replace("e3", value=f"{current.value or ''}{text}")
        return len(text)

    def key_chord(self, chord: str, **kwargs) -> None:
        del kwargs
        self.calls.append(("key", chord))
        if chord == "ctrl+s":
            self._replace("e5", title="keyed")

    def scroll(self, target, **kwargs) -> None:
        self.calls.append(("scroll", getattr(target, "ref", None), kwargs))
        if getattr(self, "scroll_moves", False):
            row = next(item for item in self.elements if item.ref == "e7")
            bounds = row.bounds
            self._replace("e7", bounds=Bounds(
                bounds.display_id, bounds.x, bounds.y + 40, bounds.width, bounds.height,
            ))

    def set_value(self, element: Element, value: str):
        self.calls.append(("set_value", element.ref, value))
        shown = getattr(self, "landed", value)
        if shown is False:
            return False
        self._replace(element.ref, value=shown)
        return shown

    def menu_items(self, app, path):
        del app, path
        return [{"title": "New", "enabled": True, "shortcut": "", "submenu": False, "checked": None}]

    def menu_state(self, app):
        del app
        return dict(self.menu)

    def menu_press(self, app, path):
        del app
        self.menu = {"open": True, "path": [part.strip() for part in path.split(">")]}
        self._replace("e5", title="menu")
        return path.split(">")[-1].strip()

    def menu_close(self, app):
        del app
        path = list(self.menu.get("path") or [])
        self.menu = {"open": False, "path": []}
        return path

    def running_apps(self):
        return list(self.apps)

    def launch_app(self, identifier, activate=None):
        del activate
        self.calls.append(("launch", identifier))
        return None

    def activate_app(self, identifier):
        self.calls.append(("activate", identifier))
        if getattr(self, "focus_lands", True):
            self.front = identifier
        return identifier

    def windows(self):
        return list(self.rows)

    def window_owner(self, window_id: int) -> str:
        for row in self.rows:
            if int(row["window_id"]) == int(window_id):
                return str(row["app"])
        raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "no window", detail={"window_id": window_id})

    def close_window(self, window_id: int) -> None:
        self.calls.append(("close", window_id))
        if getattr(self, "close_removes", True):
            self.rows = [row for row in self.rows if int(row["window_id"]) != int(window_id)]

    def move_window(self, window_id: int, x: int, y: int) -> None:
        self.calls.append(("move", window_id, x, y))
        if not self.move_lands:
            return
        for row in self.rows:
            if int(row["window_id"]) == int(window_id):
                row["bounds"] = {**row["bounds"], "x": x, "y": y}

    def minimize_window(self, window_id: int) -> None:
        self.calls.append(("minimize", window_id))


def _runtime(tmp_path, driver: TreeDriver):
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    runtime.desktop_snapshot(APP)
    return runtime


def test_action_result_equals_its_sentence() -> None:
    result = outcome.ActionResult(
        "typed 5 characters", outcome="confirmed", next=(), evidence="read back 'hello'",
    )
    assert result == "typed 5 characters"
    assert result.startswith("typed 5")
    assert isinstance(result, str)
    assert result.as_dict() == {
        "outcome": "confirmed",
        "next": [],
        "evidence": "read back 'hello'",
    }
    with pytest.raises(ValueError):
        outcome.ActionResult("x", outcome="maybe")


def test_judge_uses_readback_state_and_process_death() -> None:
    assert outcome.judge(changed=True) == ("confirmed", "the accessibility state changed")
    assert outcome.judge(changed=False) == ("suspected_noop", "the accessibility state did not change")
    assert outcome.judge(changed=None)[0] == "unverifiable"
    assert outcome.judge(
        changed=True, requested="hello", readback="hello",
    ) == ("confirmed", "read back 'hello'")
    assert outcome.judge(
        changed=True, requested="a", readback="hella", before_value="hell",
    )[0] == "confirmed"
    assert outcome.judge(
        changed=True, requested="hello", readback="help", before_value="",
    )[0] == "partial"
    assert outcome.judge(
        changed=False, requested="hello", readback="", before_value="",
    )[0] == "suspected_noop"
    died = outcome.judge(changed=True, process_died=True)
    assert died == ("partial", "the target process exited after the action")
    assert outcome.suggest_next("click", "confirmed") == ()
    assert outcome.suggest_next("click", "partial", process_died=True) == ("ref", "foreground")
    assert outcome.suggest_next("set_value", "suspected_noop")[0] == "ref"
    assert "cdp" in outcome.suggest_next("select", "partial", browser=True)
    assert outcome.next_for_error("stale_ref", {}) == ("ref", "coordinates", "keyboard")
    hidden = outcome.next_for_error("unsupported", {"reason": "not_showing"})
    assert hidden[0] == "foreground"
    assert outcome.next_for_error("secure_field", {}) == ()
    refused = outcome.refused_result(
        text="unsupported: e2 is not showing",
        code="unsupported",
        message="e2 is not showing",
        detail={"reason": "not_showing", "outcome": "refused", "next": ["foreground", "ref"]},
    )
    assert refused.outcome == "refused"
    assert refused.next == ("foreground", "ref")
    assert "re-observe" not in refused


def test_click_that_changes_the_tree_is_confirmed(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("e2")
    assert result == "clicked e2 (AXButton 'Save')"
    assert result.outcome == "confirmed"
    assert result.next == ()
    assert "changed" in result.evidence


def test_click_on_an_inert_label_is_suspected_noop(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("e4")
    assert result == "clicked e4 (AXStaticText 'Idle label')"
    assert result.outcome == "suspected_noop"
    assert "did not change" in result.evidence
    assert "ref" in result.next
    assert "coordinates" in result.next


def test_second_inert_click_without_a_snapshot_is_suspected_noop(tmp_path) -> None:
    """The second click is judged against the tree just before it.

    The first click changes the tree. A second click with no snapshot in
    between used to compare against that older tree and report confirmed.
    """
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    first = runtime.click("e2")
    assert first.outcome == "confirmed"
    second = runtime.click("e2")
    assert second == "clicked e2 (AXButton 'Save')"
    assert second.outcome == "suspected_noop"
    assert "did not change" in second.evidence


def test_set_value_in_a_background_window_is_confirmed_from_app_scope(tmp_path) -> None:
    """A field that window scope omits is confirmed from the app-scope read-back.

    The ref comes from an app snapshot. A window-scope reread would not
    contain the field and would report unverifiable. The write is confirmed
    only when that wider read-back equals the request. A different read-back
    is not confirmed. A cover is a separate refusal and does not reach this.
    """
    driver = TreeDriver()
    driver.elements = [
        *driver.elements,
        _element(
            "e9", "AXTextField", "Field A",
            path=("Alpha", "AXTextField"),
            value="", editable=True,
        ),
    ]
    original = driver.snapshot

    def snapshot(scope, app):
        shot = original(scope, app)
        kind = scope.value if isinstance(scope, Scope) else str(scope)
        if kind == "window":
            return dataclasses.replace(
                shot, elements=tuple(el for el in shot.elements if el.ref != "e9"),
            )
        return shot

    driver.snapshot = snapshot  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    runtime.desktop_snapshot(APP, scope="app")
    written = runtime.set_value("e9", "alpha-ok")
    assert written == "set e9 = 'alpha-ok'"
    assert written.outcome == "confirmed", (written.outcome, written.evidence)
    assert "alpha-ok" in written.evidence
    field = next(item for item in driver.elements if item.ref == "e9")
    assert field.value == "alpha-ok"
    window = driver.snapshot(Scope.WINDOW, APP)
    assert all(el.ref != "e9" for el in window.elements)

    runtime.desktop_snapshot(APP, scope="app")
    driver.landed = "nope"
    missed = runtime.set_value("e9", "alpha-ok")
    assert missed.outcome != "confirmed"
    assert missed.outcome in {"partial", "suspected_noop"}
    assert "alpha-ok" not in missed.evidence or "nope" in missed.evidence


def test_set_value_on_a_covered_element_is_refused(tmp_path) -> None:
    driver = TreeDriver()
    driver.occlusion = lambda _element, _app: "other"  # type: ignore[attr-defined]
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.set_value("e3", "Ann")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "covered"
    assert exc.value.detail["outcome"] == "refused"
    assert exc.value.detail["next"][0] == "foreground"
    field = next(item for item in driver.elements if item.ref == "e3")
    assert field.value in (None, "")


def test_click_that_kills_the_process_is_partial(tmp_path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    driver = TreeDriver()
    driver.name = "linux"
    driver.pid = proc.pid

    def click(target, **_kwargs) -> None:
        # An on-screen Linux button is a pointer click, not DoAction.
        driver.calls.append(("click", getattr(target, "ref", None)))
        driver.dead = True
        proc.kill()
        proc.wait(timeout=2)

    driver.click = click  # type: ignore[method-assign]
    try:
        runtime = _runtime(tmp_path, driver)
        runtime._PROCESS_SETTLE_S = 0.5
        result = runtime.click("e2")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    assert result == "clicked e2 (AXButton 'Save')"
    assert result.outcome == "partial"
    assert "exited after the action" in result.evidence
    assert result.next == ("ref", "foreground")


def test_a_zombie_process_counts_as_exited() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert outcome.pid_alive(proc.pid)
    proc.kill()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and outcome.pid_alive(proc.pid):
        time.sleep(0.02)
    try:
        assert outcome.pid_alive(proc.pid) is False
    finally:
        proc.wait(timeout=2)


def test_a_pid_that_was_never_alive_is_not_a_crash(tmp_path) -> None:
    driver = TreeDriver()
    driver.name = "linux"
    driver.pid = 2**30 + 17
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("e4")
    assert result.outcome == "suspected_noop"


def test_disabled_click_stays_a_raised_refusal(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.click("e8")
    assert exc.value.code is ErrorCode.ELEMENT_DISABLED
    marker = outcome.refused_result(
        text="disabled", code=exc.value.code.value, message=exc.value.message, detail=exc.value.detail,
    )
    assert marker.outcome == "refused"
    assert marker.next == ("ref", "keyboard")


def test_type_readback_confirms_and_a_miss_does_not(tmp_path) -> None:
    driver = TreeDriver()
    driver.apply_type = True
    runtime = _runtime(tmp_path, driver)
    result = runtime.type_text("hello")
    assert result == "typed 5 characters"
    assert result.outcome == "confirmed"
    assert "hello" in result.evidence

    driver.apply_type = False
    runtime.desktop_snapshot(APP)
    missed = runtime.type_text("nope")
    assert missed == "typed 4 characters"
    assert missed.outcome == "suspected_noop"
    assert "keyboard" in missed.next


def test_key_confirms_when_the_tree_changes(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.key("ctrl+s")
    assert result == "pressed ctrl+s"
    assert result.outcome == "confirmed"
    runtime.desktop_snapshot(APP)
    quiet = runtime.key("ctrl+a")
    assert quiet == "pressed ctrl+a"
    assert quiet.outcome == "suspected_noop"


def test_scroll_uses_bounds_and_set_value_uses_readback(tmp_path) -> None:
    driver = TreeDriver()
    driver.scroll_moves = True
    runtime = _runtime(tmp_path, driver)
    moved = runtime.scroll("e7", dy=3)
    assert moved.startswith("scrolled e7")
    assert moved.outcome == "confirmed"

    driver.scroll_moves = False
    runtime.desktop_snapshot(APP)
    still = runtime.scroll("e7", dy=3)
    assert still.outcome == "suspected_noop"

    written = runtime.set_value("e3", "Ann")
    assert written == "set e3 = 'Ann'"
    assert written.outcome == "confirmed"
    assert "Ann" in written.evidence

    runtime.desktop_snapshot(APP)
    driver.landed = "An"
    partial = runtime.set_value("e3", "Ann")
    assert partial.outcome == "partial"
    assert "Ann" in partial.evidence


def test_select_is_judged_like_set_value(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.call_tool("select", {"ref": "e6", "value": "Blue"})
    assert result == "set e6 = 'Blue'"
    assert result.outcome == "confirmed"
    assert result.next == ()


def test_menu_app_and_window_carry_outcomes(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    listed = runtime.menu(APP, "File", action="list")
    assert listed.startswith("[")
    assert listed.outcome == "confirmed"
    assert listed.next == ()

    pressed = runtime.menu(APP, "File > New")
    assert pressed == f"pressed menu item 'New' in {APP}"
    assert pressed.outcome == "confirmed"

    closed = runtime.menu(APP, action="close")
    assert closed.outcome == "confirmed"
    none_open = runtime.menu(APP, action="close")
    assert none_open.outcome == "suspected_noop"
    assert "no menu was open" in none_open.evidence

    apps = runtime.app("list")
    assert apps.outcome == "confirmed"
    launched = runtime.app("launch", APP)
    assert launched == f"launched {APP}"
    assert launched.outcome == "confirmed"

    focused = runtime.app("focus", APP)
    assert focused == f"focused {APP}"
    assert focused.outcome == "confirmed"

    runtime.QUIT_SETTLE_S = 0
    driver.apps = []
    quit_result = runtime.app("quit", APP)
    assert quit_result == f"quit {APP}"
    assert quit_result.outcome == "confirmed"

    moved = runtime.window("move", window_id=7, x=8, y=9)
    assert moved == "moved window 7 to (8, 9) (demo)"
    assert moved.outcome == "confirmed"
    driver.move_lands = False
    missed_move = runtime.window("move", window_id=7, x=40, y=50)
    assert missed_move.outcome == "partial"
    driver.move_lands = True
    driver.close_removes = False
    stayed = runtime.window("close", window_id=7)
    assert stayed == "closed window 7 (demo)"
    assert stayed.outcome == "suspected_noop"
    driver.close_removes = True
    gone = runtime.window("close", window_id=7)
    assert gone.outcome == "confirmed"
    driver.rows = [{
        "window_id": 7, "app": APP, "title": "Demo", "on_screen": True,
        "bounds": {"display_id": 0, "x": 8, "y": 9, "width": 300, "height": 200},
    }]
    showing = runtime.window("minimize", window_id=7)
    assert showing.outcome == "suspected_noop"


@pytest.mark.anyio
async def test_mcp_click_keeps_the_sentence_and_publishes_structured_outcome(tmp_path) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session as client_session

    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    mcp = server.build_server(runtime=runtime)
    async with client_session(mcp) as client:
        result = await client.call_tool("click", {"ref": "e2"})
    assert result.isError is False
    assert result.content[0].text == "clicked e2 (AXButton 'Save')"
    structured = result.structuredContent
    assert structured["outcome"] == "confirmed"
    assert structured["next"] == []
    assert structured["evidence"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_agent_follows_next_after_a_hidden_ref(tmp_path) -> None:
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    runtime = FakeRuntime(elements)

    def call_tool(name, params, confirm=None):
        del confirm
        runtime.calls.append((name, dict(params)))
        if name == "click":
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "e2 (AXButton 'Save') is not showing",
                detail={
                    "reason": "not_showing",
                    "outcome": "refused",
                    "next": ["foreground", "ref"],
                    "evidence": "e2 is still in the tree but its document is not showing",
                },
            )
        if name == "app":
            return outcome.ActionResult(
                "focused demo", outcome="confirmed", next=(), evidence="demo is frontmost",
            )
        return outcome.ActionResult(f"{name} ok", outcome="confirmed", next=(), evidence="ok")

    runtime.call_tool = call_tool  # type: ignore[method-assign]
    result, events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert ("app", {"action": "focus", "name": "demo"}) in runtime.calls
    finished = next(
        event for event in events
        if event.type == "step_finished" and event.data.get("index") == 1
    )
    assert finished.data["verified"] is True
    assert finished.data["error"] is None
    assert result.status == "success"


def test_agent_escalates_a_noop_and_counts_it_as_stuck(tmp_path) -> None:
    save = el("e2", "AXButton", "Save", parent="e1", clickable=True, bounds=Bounds(0, 20, 30, 80, 24))
    clock = el("clock", "AXStaticText", "t0", parent="e1")
    elements = window(save, clock)
    runtime = FakeRuntime(elements)
    feedback: list[str] = []

    def call_tool(name, params, confirm=None):
        del confirm
        runtime.calls.append((name, dict(params)))
        return outcome.ActionResult(
            "clicked e2",
            outcome="suspected_noop",
            next=("coordinates", "keyboard", "foreground"),
            evidence="the accessibility state did not change",
        )

    def snapshot(app, **kwargs):
        del app, kwargs
        runtime.tick = getattr(runtime, "tick", 0) + 1
        runtime.elements = [
            dataclasses.replace(item, title=f"t{runtime.tick}") if item.ref == "clock" else item
            for item in runtime.elements
        ]
        runtime._current = Snapshot(
            "snap", Scope.WINDOW, "demo", 1, 0.0, (), tuple(runtime.elements),
        )
        return f"screen {runtime.tick}"

    runtime.call_tool = call_tool  # type: ignore[method-assign]
    runtime.desktop_snapshot = snapshot  # type: ignore[method-assign]

    def script(messages):
        feedback.extend(
            message.content for message in messages if isinstance(message.content, str)
        )
        return turn(ToolCall("click", {"ref": "e2"}))

    result, events, runtime, _agent = run(
        ScriptedModel(script),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
        max_steps=6,
        max_replans=0,
    )
    assert result.reason == "stuck"
    assert runtime.calls[0] == ("click", {"ref": "e2"})
    assert runtime.calls[1][0] == "click"
    assert "x" in runtime.calls[1][1]
    assert runtime.calls[2] == ("key", {"chord": "Return"})
    assert any("outcome: suspected_noop" in text for text in feedback)
    assert any(event.type == "stuck" and event.data["terminal"] is True for event in events)


def test_confirmed_outcome_verifies_without_a_digest_change(tmp_path) -> None:
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    runtime = FakeRuntime(elements)

    def call_tool(name, params, confirm=None):
        del confirm
        runtime.calls.append((name, dict(params)))
        return outcome.ActionResult(
            "clicked e2", outcome="confirmed", next=(), evidence="the accessibility state changed",
        )

    runtime.call_tool = call_tool  # type: ignore[method-assign]
    result, events, _runtime, agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    click_done = next(event for event in events if event.type == "step_finished" and event.data["index"] == 1)
    assert click_done.data["verified"] is True
    assert agent._levels.get(("click", "e2"), 0) == 0
    assert agent._hints == {}
    assert result.status == "success"
    assert result.step_log[0].verified is True
