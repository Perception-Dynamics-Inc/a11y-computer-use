"""Live agent runs against the GTK fixture and the local HTML pages.

The fixture smoke test launches the GTK app and the pages and snapshots them
with the Linux and browser drivers.

The agent tests call ``Agent(...).run(goal)`` with a ``ScriptedModel`` callable.
That callable is scripted, not an LLM: it reads element refs out of the
snapshot text in the messages. They run whenever this process has a Linux
display (the Linux live job). Hermetic jobs have no display and skip them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from a11y_computer_use.observe import render_text
from tests.agent_fixtures.harness import (
    APP_NAME,
    FORM_RESULT,
    NOTE,
    PageSite,
    chrome_binary,
    element,
    launch_chrome,
    launch_gtk,
    message_text,
    parse_snapshot,
    pick,
    stop_process,
    wait_cdp,
    wait_gtk_snapshot,
)

# The Linux live job exports DISPLAY (xvfb-run). Hermetic jobs do not, and
# they skip the display tests the same way the other GTK live tests do.
_LINUX_DISPLAY = sys.platform.startswith("linux") and bool(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
)
requires_display = pytest.mark.skipif(
    not _LINUX_DISPLAY,
    reason="GTK and Chrome fixtures need a Linux display; the linux live job provides Xvfb",
)

# The sentence core._note_screen appends. The stuck script switches only when
# this exact text is the latest message, after the Ping clicks that caused it.
_REPLAN = (
    "The screen is stuck: this snapshot has already repeated. "
    "Choose a different strategy. Do not repeat the last action."
)


def _linux_driver():
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    driver.ensure_trusted()
    return driver


def _focus_app() -> None:
    """Best-effort: ask the window manager to put the fixture in front.

    ``windowactivate --sync`` can wait forever under Xvfb when the active-window
    property never settles. The agent still focuses ``cuagentfix`` itself.
    """
    try:
        subprocess.run(
            ["xdotool", "search", "--name", APP_NAME, "windowactivate"],
            check=False,
            timeout=3,
            capture_output=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return


# ---------------------------------------------------------------------------
# Fixture smoke. No agent run; the parser case is hermetic.
# ---------------------------------------------------------------------------


def test_snapshot_text_parser_matches_rendered_lines() -> None:
    """The scripted planners key off this parse. Hermetic: no display, no agent."""
    sample = "\n".join([
        '[snap-1] cuagentfix (window)',
        '  e7 textarea "Notes" ="M1 live note second line " (edit,focus)',
        '  e10 textfield "Save path" ="/tmp/n.txt" (click,edit)',
        '  e13 combobox "Format" ="markdown" (click,edit)',
        '  e16 button "Ping" (click)',
        '  e17 statictext "Status" ="idle"',
    ])
    elements = parse_snapshot(sample)
    notes = pick(elements, role="textarea", name="Notes")
    assert notes is not None and notes.ref == "e7" and notes.value is not None and "M1 live note" in notes.value
    path = pick(elements, role="textfield", name="Save path")
    assert path is not None and path.value == "/tmp/n.txt"
    combo = pick(elements, role="combobox", name="Format")
    assert combo is not None and combo.value == "markdown"
    ping = pick(elements, role="button", name="Ping")
    assert ping is not None and ping.ref == "e16" and "click" in ping.flags
    status = pick(elements, role="statictext", name="Status")
    assert status is not None and status.value == "idle"


@requires_display
def test_fixtures_snapshot_gtk_app_and_pages(tmp_path: Path) -> None:
    """Launch the fixture app and the pages and snapshot them with the library."""
    from a11y_computer_use.drivers.browser import BrowserDriver
    from a11y_computer_use.schema import Scope

    assert chrome_binary(), "google-chrome or chromium is required to snapshot the pages"
    driver = _linux_driver()
    proc = launch_gtk()
    chrome = None
    try:
        snap = wait_gtk_snapshot(driver)
        assert snap is not None and snap.elements, "AT-SPI snapshot of cuagentfix was empty"
        notes = element(snap, role="AXTextArea", title="Notes")
        path = element(snap, role="AXTextField", title="Save path")
        combo = element(snap, role="AXComboBox", title="Format")
        save = element(snap, role="AXButton", title="Save")
        ping = element(snap, role="AXButton", title="Ping")
        status = element(snap, role="AXStaticText", title="Status")
        window = element(snap, role="AXWindow", title=APP_NAME)
        assert notes is not None and notes.editable, render_text(snap)
        assert path is not None and path.editable, render_text(snap)
        assert combo is not None and combo.value == "markdown", render_text(snap)
        assert save is not None and save.clickable
        assert ping is not None and ping.clickable
        assert status is not None and status.value == "idle", render_text(snap)
        assert window is not None

        menus = driver.menu_items(APP_NAME, "File")
        assert any(row.get("title") == "Save As" for row in menus), menus

        shot = driver.screenshot()
        assert shot.png.startswith(b"\x89PNG")

        dest = Path(f"/tmp/cuagent-smoke-{uuid.uuid4().hex[:8]}.txt")
        dest.unlink(missing_ok=True)
        try:
            assert driver.set_value(driver.resolve_ref(snap, notes.ref), NOTE)
            snap = driver.snapshot(Scope.WINDOW, APP_NAME)
            path = element(snap, role="AXTextField", title="Save path")
            assert path is not None
            assert driver.set_value(driver.resolve_ref(snap, path.ref), str(dest))
            snap = driver.snapshot(Scope.WINDOW, APP_NAME)
            combo = element(snap, role="AXComboBox", title="Format")
            assert combo is not None
            assert driver.set_value(driver.resolve_ref(snap, combo.ref), "plain")
            snap = driver.snapshot(Scope.WINDOW, APP_NAME)
            save = element(snap, role="AXButton", title="Save")
            assert save is not None
            assert driver.press_element(driver.resolve_ref(snap, save.ref))
            snap = wait_status(driver, "saved plain")
            status = element(snap, role="AXStaticText", title="Status")
            window = next(el for el in snap.elements if el.role == "AXWindow")
            assert status is not None and status.value == "saved plain", render_text(snap)
            assert "saved plain" in (window.title or "")
            assert dest.read_text() == NOTE

            assert driver.menu_press(APP_NAME, "File > Save As") == "Save As"
            snap = wait_status(driver, "choose-path")
            status = element(snap, role="AXStaticText", title="Status")
            assert status is not None and status.value == "choose-path"
        finally:
            dest.unlink(missing_ok=True)

        chrome, endpoint = launch_chrome(tmp_path / "chrome-profile")
        wait_cdp(endpoint)
        browser = BrowserDriver(endpoint=endpoint)
        with PageSite() as site:
            _assert_pages(browser, site)
    finally:
        stop_process(proc)
        stop_process(chrome)


def wait_status(driver, expected: str):
    from a11y_computer_use.schema import Scope

    deadline_s = time.monotonic() + 5
    last = None
    while time.monotonic() < deadline_s:
        last = driver.snapshot(Scope.WINDOW, APP_NAME)
        status = element(last, role="AXStaticText", title="Status")
        if status is not None and status.value == expected:
            return last
        time.sleep(0.2)
    return last


def _assert_pages(browser, site: PageSite) -> None:
    from a11y_computer_use.schema import Scope

    browser.navigate(site.url("form.html"))
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    name = element(snap, role="AXTextField", title="Name")
    color = element(snap, role="AXComboBox", title="Color")
    subscribe = element(snap, role="AXCheckBox", title="Subscribe")
    submit = element(snap, role="AXButton", title="Submit")
    assert name is not None and name.editable, render_text(snap)
    assert color is not None and color.value == "Choose", render_text(snap)
    assert subscribe is not None and subscribe.checked is False, render_text(snap)
    assert submit is not None and submit.clickable

    assert browser.set_value(browser.resolve_ref(snap, name.ref), "Ada")
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    color = element(snap, role="AXComboBox", title="Color")
    assert color is not None
    assert browser.set_value(browser.resolve_ref(snap, color.ref), "blue")
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    subscribe = element(snap, role="AXCheckBox", title="Subscribe")
    assert subscribe is not None
    assert browser.press_element(browser.resolve_ref(snap, subscribe.ref))
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    submit = element(snap, role="AXButton", title="Submit")
    assert submit is not None
    assert browser.press_element(browser.resolve_ref(snap, submit.ref))
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    assert any(FORM_RESULT in (el.title or "") for el in snap.elements), render_text(snap)
    dom = _eval(browser, "document.getElementById('result').textContent")
    assert dom == FORM_RESULT

    browser.navigate(site.url("login.html"))
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    password = element(snap, role="AXSecureTextField", title="Password")
    assert password is not None and password.secure, render_text(snap)

    browser.navigate(site.url("otp.html"))
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    otp = element(snap, role="AXTextField", title="One-time code")
    assert otp is not None and otp.editable, render_text(snap)
    assert not any(el.secure for el in snap.elements)
    assert any("2fa" in (el.title or "") for el in snap.elements), render_text(snap)

    browser.navigate(site.url("card.html"))
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    card = element(snap, role="AXTextField", title="Card number")
    assert card is not None and card.editable, render_text(snap)
    assert element(snap, role="AXTextField", title="Security code") is not None

    browser.navigate(site.url("captcha.html"))
    snap = browser.snapshot(Scope.WINDOW, browser._target_id)
    assert any("captcha" in (el.title or "").lower() for el in snap.elements), render_text(snap)
    cross = _eval(
        browser,
        "(() => { const f = document.getElementById('captcha-frame');"
        " return {src: f.src, doc: f.contentDocument === null ? 'null' : 'readable',"
        " origin: location.origin}; })()",
    )
    assert cross["doc"] == "null", cross
    assert cross["origin"] not in cross["src"] or _origin(cross["src"]) != cross["origin"]
    widget_origin = _origin(cross["src"])
    assert widget_origin != cross["origin"], cross


def _origin(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _eval(browser, expression: str):
    reply = browser._connect().call(
        "Runtime.evaluate", {"expression": expression, "returnByValue": True}
    )
    return reply.get("result", {}).get("value")


# ---------------------------------------------------------------------------
# Agent runs. ScriptedModel only. Display-gated, same as the GTK live tests.
# ---------------------------------------------------------------------------


def _agent_api():
    from a11y_computer_use.agent.core import Agent
    from a11y_computer_use.agent.models.base import ModelTurn, ToolCall
    from a11y_computer_use.agent.models.scripted import ScriptedModel

    return Agent, ScriptedModel, ModelTurn, ToolCall


def _scripted(scripted_cls, fn):
    return scripted_cls(fn)


def _grant(*app_ids: str) -> None:
    """FULL tier in the temp-HOME store. The agent does not grant by itself."""
    from a11y_computer_use.safety import PermissionStore, Tier

    store = PermissionStore()
    for app_id in app_ids:
        if app_id and app_id != "unknown":
            store.set_tier(app_id, Tier.FULL)


def _grant_linux_desktop(*extra: str) -> None:
    """Grant the live Linux comm names the runtime will key a snapshot on."""
    names = ["python3", "python", "chrome", "chromium", "google-chrome", "chromium-browser", APP_NAME, *extra]
    try:
        from a11y_computer_use.drivers import _linux_system

        front = _linux_system.frontmost_app_id()
        if front:
            names.append(front)
        for row in _linux_system.running_apps():
            comm = row.get("name") or row.get("bundle_id")
            if comm:
                names.append(str(comm))
    except Exception:
        pass
    _grant(*names)


def _run_agent(agent_cls, model, goal: str, trace: Path, **kwargs):
    agent = agent_cls(
        model,
        display=os.environ.get("DISPLAY"),
        max_steps=kwargs.pop("max_steps", 16),
        max_time_s=kwargs.pop("max_time_s", 180),
        approve=kwargs.pop("approve", lambda _action: True),
        auto_deny=kwargs.pop("auto_deny", False),
        trace_dir=str(trace),
        vision=False,
        **kwargs,
    )
    return agent.run(goal)


def _save_conditions(dest: Path) -> list[dict]:
    return [
        {"file_exists": str(dest), "contains": NOTE},
        {"value": {"name": "Status", "equals": "saved plain"}},
        {"window_title_contains": "saved plain"},
    ]


def _shown_note(value: str | None) -> str:
    return value or ""


def _note_landed(value: str | None) -> bool:
    shown = _shown_note(value)
    flat = NOTE.replace("\n", " ")
    return shown == flat or shown == NOTE or (shown.endswith("…") and flat.startswith(shown[:-1]))


class _SaveScript:
    """Scripted, not an LLM. Reads refs from the snapshot and saves the note."""

    def __init__(self, dest: Path, turn_cls, call_cls) -> None:
        self.dest = dest
        self.Turn = turn_cls
        self.Call = call_cls
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        return _save_turn(message_text(messages), self.dest, self.Turn, self.Call)


def _save_turn(text: str, dest: Path, turn_cls, call_cls):
    elements = parse_snapshot(text)
    notes = pick(elements, role="textarea", name="Notes")
    path = pick(elements, role="textfield", name="Save path")
    combo = pick(elements, role="combobox", name="Format")
    status = pick(elements, role="statictext", name="Status")
    save = pick(elements, role="button", name="Save")
    if notes is None or path is None or combo is None or save is None:
        return turn_cls(calls=[call_cls(name="app", args={"action": "focus", "name": APP_NAME})])
    if not _note_landed(notes.value):
        return turn_cls(calls=[call_cls(name="set_value", args={"ref": notes.ref, "value": NOTE})])
    if (path.value or "") != str(dest):
        return turn_cls(calls=[call_cls(name="set_value", args={"ref": path.ref, "value": str(dest)})])
    if combo.value != "plain":
        return turn_cls(calls=[call_cls(name="select", args={"ref": combo.ref, "value": "plain"})])
    if status is None or status.value != "saved plain":
        return turn_cls(calls=[call_cls(name="click", args={"ref": save.ref})])
    return turn_cls(calls=[call_cls(
        name="done",
        args={"answer": "saved the note", "conditions": _save_conditions(dest)},
    )])


class _ThreeActionSave(_SaveScript):
    """One turn sets the note, the path, and the format. Later turns save."""

    def __init__(self, dest: Path, turn_cls, call_cls) -> None:
        super().__init__(dest, turn_cls, call_cls)
        self.batched = False

    def __call__(self, messages):
        self.calls += 1
        text = message_text(messages)
        elements = parse_snapshot(text)
        notes = pick(elements, role="textarea", name="Notes")
        path = pick(elements, role="textfield", name="Save path")
        combo = pick(elements, role="combobox", name="Format")
        if (
            not self.batched
            and notes is not None
            and path is not None
            and combo is not None
            and (
                not _note_landed(notes.value)
                or (path.value or "") != str(self.dest)
                or combo.value != "plain"
            )
        ):
            self.batched = True
            return self.Turn(calls=[
                self.Call(name="set_value", args={"ref": notes.ref, "value": NOTE}),
                self.Call(name="set_value", args={"ref": path.ref, "value": str(self.dest)}),
                self.Call(name="select", args={"ref": combo.ref, "value": "plain"}),
            ])
        return _save_turn(text, self.dest, self.Turn, self.Call)


class _WrongThenSave(_SaveScript):
    """First turn writes the wrong note and claims success. Later turns recover."""

    def __init__(self, dest: Path, turn_cls, call_cls) -> None:
        super().__init__(dest, turn_cls, call_cls)
        self.injected = False

    def __call__(self, messages):
        self.calls += 1
        text = message_text(messages)
        if not self.injected:
            self.injected = True
            notes = pick(parse_snapshot(text), role="textarea", name="Notes")
            ref = notes.ref if notes is not None else "e999"
            return self.Turn(calls=[
                self.Call(name="set_value", args={"ref": ref, "value": "WRONG"}),
                self.Call(name="done", args={
                    "answer": "saved",
                    "conditions": _save_conditions(self.dest),
                }),
            ])
        return _save_turn(text, self.dest, self.Turn, self.Call)


class _Cues:
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def stuck(self) -> bool:
        for event in self.events:
            kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", "")
            if str(kind) == "stuck":
                return True
        return False


class _StuckThenSave(_SaveScript):
    """Clicks Ping, which changes nothing, until the agent asks for a new strategy.

    The cue is the loop's exact replan sentence. Three identical snapshots
    produce it. The script keeps pinging until that sentence arrives after at
    least three pings, then saves. Switching earlier would hide a miss.
    """

    def __init__(self, dest: Path, turn_cls, call_cls, cues: _Cues) -> None:
        super().__init__(dest, turn_cls, call_cls)
        self.cues = cues
        self.pings = 0
        self.cue = ""

    def __call__(self, messages):
        self.calls += 1
        text = message_text(messages)
        last = message_text(messages[-1:]) if messages else ""
        # The replan sentence is one message. Later turns are ordinary
        # observations again, so the cue has to stick or the script goes
        # back to Ping and the run fails stuck.
        if self.cue or (self.pings >= 3 and _REPLAN in last):
            self.cue = _REPLAN
            return _save_turn(text, self.dest, self.Turn, self.Call)
        self.pings += 1
        ping = pick(parse_snapshot(text), role="button", name="Ping")
        ref = ping.ref if ping is not None else "e999"
        return self.Turn(calls=[self.Call(name="click", args={"ref": ref})])


class _FormScript:
    """Scripted, not an LLM. Fills the fixture form from snapshot refs."""

    def __init__(self, turn_cls, call_cls) -> None:
        self.Turn = turn_cls
        self.Call = call_cls

    def __call__(self, messages):
        text = message_text(messages)
        elements = parse_snapshot(text)
        name = pick(elements, role="textfield", name="Name")
        color = pick(elements, role="combobox", name="Color")
        subscribe = pick(elements, role="checkbox", name="Subscribe")
        submit = pick(elements, role="button", name="Submit")
        if name is None or color is None or subscribe is None or submit is None:
            return self.Turn(calls=[self.Call(name="wait", args={"seconds": 0.2})])
        if (name.value or "") != "Ada":
            return self.Turn(calls=[self.Call(name="set_value", args={"ref": name.ref, "value": "Ada"})])
        if color.value != "blue":
            return self.Turn(calls=[self.Call(name="select", args={"ref": color.ref, "value": "blue"})])
        if "checked" not in set(subscribe.flags.split(",")):
            return self.Turn(calls=[self.Call(name="click", args={"ref": subscribe.ref})])
        if FORM_RESULT not in text:
            return self.Turn(calls=[self.Call(name="click", args={"ref": submit.ref})])
        # The browser snapshot's window title is the CDP target id. The result
        # is the static text and the web area whose document title was set to it.
        return self.Turn(calls=[self.Call(name="done", args={
            "answer": FORM_RESULT,
            "conditions": [
                {"element": {"role": "AXStaticText", "name": FORM_RESULT}},
                {"element": {"role": "AXWebArea", "name": FORM_RESULT}},
            ],
        })])


class _HumanScript:
    """Scripted, not an LLM. Stops with ask_human when the snapshot shows the challenge."""

    def __init__(self, kind: str, marker: str, turn_cls, call_cls) -> None:
        self.kind = kind
        self.marker = marker
        self.Turn = turn_cls
        self.Call = call_cls

    def __call__(self, messages):
        text = message_text(messages)
        if self.marker.lower() not in text.lower():
            return self.Turn(calls=[self.Call(name="wait", args={"seconds": 0.2})])
        return self.Turn(calls=[self.Call(
            name="ask_human",
            args={"kind": self.kind, "message": f"scripted stop: {self.kind}"},
        )])


def _short_dest() -> Path:
    return Path(f"/tmp/cuagent-{uuid.uuid4().hex[:8]}.txt")


def _assert_trace(result, trace: Path) -> None:
    root = Path(getattr(result, "trace_dir", None) or trace)
    candidates = [root, trace]
    traj = None
    pngs: list[Path] = []
    for folder in candidates:
        if not folder.exists():
            continue
        found = list(folder.rglob("trajectory.jsonl"))
        if found:
            traj = found[0]
        pngs.extend(folder.rglob("*.png"))
    assert traj is not None and traj.is_file(), f"trajectory.jsonl missing under {candidates}"
    lines = [line for line in traj.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, traj
    for line in lines:
        json.loads(line)
    assert pngs, f"no PNGs under {candidates}"
    assert any(path.read_bytes().startswith(b"\x89PNG") for path in pngs)


def _as_mapping(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise AssertionError(f"expected a mapping, got {value!r}")


def _assert_conditions(result, fragments: list[str]) -> None:
    assert result.status == "success", result
    conditions = result.conditions
    assert conditions, result
    blobs = []
    for item in conditions:
        mapped = _as_mapping(item)
        assert mapped.get("ok") is True, mapped
        assert "detail" in mapped, mapped
        blobs.append(json.dumps(mapped.get("condition"), sort_keys=True))
    joined = "\n".join(blobs)
    for fragment in fragments:
        assert fragment in joined, (fragment, conditions)


def _step_verified(step) -> bool | None:
    if isinstance(step, dict):
        value = step.get("verified")
    else:
        value = getattr(step, "verified", None)
    if isinstance(value, bool):
        return value
    return None


def _step_action(step) -> str:
    if isinstance(step, dict):
        return str(step.get("action") or step.get("name") or "")
    return str(getattr(step, "action", "") or "")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point Path.home at an empty config so a run cannot touch the real grant store."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("A11Y_COMPUTER_USE_ALLOW_ANY_PATH", "1")
    monkeypatch.delenv("A11Y_COMPUTER_USE_DRIVER", raising=False)
    return home


@requires_display
def test_agent_saves_note_and_records_trace(tmp_path, isolated_home) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    dest = _short_dest()
    dest.unlink(missing_ok=True)
    proc = launch_gtk()
    trace = tmp_path / "trace"
    trace.mkdir()
    try:
        driver = _linux_driver()
        assert wait_gtk_snapshot(driver) is not None
        _focus_app()
        _grant_linux_desktop()
        script = _SaveScript(dest, ModelTurn, ToolCall)
        result = _run_agent(
            Agent,
            _scripted(ScriptedModel, script),
            f"In {APP_NAME}, write the note, set Format to plain, and save it to {dest}.",
            trace,
        )
        assert result.status == "success", result
        assert dest.read_text() == NOTE
        _assert_conditions(result, [str(dest), "saved plain"])
        assert result.steps >= 1
        _assert_trace(result, trace)
        assert script.calls >= 1
    finally:
        stop_process(proc)
        dest.unlink(missing_ok=True)


@requires_display
def test_agent_runs_three_actions_in_one_turn(tmp_path, isolated_home) -> None:
    """A scripted model issues set_value, set_value, and select in one turn."""
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    dest = _short_dest()
    dest.unlink(missing_ok=True)
    proc = launch_gtk()
    trace = tmp_path / "trace"
    trace.mkdir()
    try:
        driver = _linux_driver()
        assert wait_gtk_snapshot(driver) is not None
        _focus_app()
        _grant_linux_desktop()
        script = _ThreeActionSave(dest, ModelTurn, ToolCall)
        result = _run_agent(
            Agent,
            _scripted(ScriptedModel, script),
            f"In {APP_NAME}, write the note, set Format to plain, and save it to {dest}.",
            trace,
        )
        assert script.batched, "the model never issued the 3-action turn"
        assert result.status == "success", result
        assert dest.read_text() == NOTE
        actions = [_step_action(step) for step in result.step_log]
        window_of_three = None
        for index in range(len(actions) - 2):
            if actions[index:index + 3] == ["set_value", "set_value", "select"]:
                window_of_three = index
                break
        assert window_of_three is not None, actions
        batch = list(result.step_log[window_of_three:window_of_three + 3])
        assert [_step_verified(step) for step in batch] == [True, True, True]
        assert all(not _step_skipped(step) for step in batch)
        lines = [
            json.loads(line)
            for line in (Path(result.trace_dir) / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(len(entry.get("response", {}).get("calls") or []) == 3 for entry in lines), lines
    finally:
        stop_process(proc)
        dest.unlink(missing_ok=True)


def _step_skipped(step) -> list:
    if isinstance(step, dict):
        return list(step.get("skipped") or [])
    return list(getattr(step, "skipped", None) or [])


@requires_display
def test_agent_exec_writes_a_file_when_allowed(tmp_path, isolated_home) -> None:
    """Live shell, with allow_exec and an approve hook, writes the file."""
    import shlex

    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    target = tmp_path / "exec-live.txt"
    token = f"m2-live-{uuid.uuid4().hex[:8]}"
    command = f"printf %s {shlex.quote(token)} > {shlex.quote(str(target))}"
    trace = tmp_path / "trace"
    trace.mkdir()

    def script(_messages):
        if target.is_file() and target.read_text(encoding="utf-8") == token:
            return ModelTurn(calls=[ToolCall(
                name="done",
                args={
                    "answer": "wrote the file",
                    "conditions": [{"file_exists": str(target), "contains": token}],
                },
            )])
        return ModelTurn(calls=[
            ToolCall(name="shell", args={"command": command, "timeout_s": 10}),
            ToolCall(name="done", args={
                "answer": "wrote the file",
                "conditions": [{"file_exists": str(target), "contains": token}],
            }),
        ])

    result = _run_agent(
        Agent,
        ScriptedModel(script),
        f"Write {token} to {target} with the shell.",
        trace,
        allow_exec=True,
        max_steps=6,
    )
    assert result.status == "success", result
    assert target.read_text(encoding="utf-8") == token
    assert _step_action(result.step_log[0]) == "shell"
    assert _step_verified(result.step_log[0]) is True
    rows = [
        json.loads(line)
        for line in (Path(result.trace_dir) / "exec-audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["approval"] == "approved"
    assert rows[0]["exit_code"] == 0
    assert rows[0]["command"] == command
    assert rows[0]["cwd"]
    assert "timestamp" in rows[0]


@requires_display
def test_agent_exec_denial_does_not_write(tmp_path, isolated_home) -> None:
    """allow_exec still refuses the command when approve returns false."""
    import shlex

    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    target = tmp_path / "exec-denied.txt"
    marker = tmp_path / "marker.txt"
    marker.write_text("present", encoding="utf-8")
    token = "should-not-land"
    command = f"printf %s {shlex.quote(token)} > {shlex.quote(str(target))}"
    trace = tmp_path / "trace"
    trace.mkdir()
    state = {"phase": "deny"}

    def script(_messages):
        if state["phase"] == "deny":
            state["phase"] = "finish"
            return ModelTurn(calls=[
                ToolCall(name="shell", args={"command": command, "timeout_s": 10}),
                ToolCall(name="done", args={
                    "answer": "wrote it",
                    "conditions": [{"file_exists": str(target), "contains": token}],
                }),
            ])
        return ModelTurn(calls=[ToolCall(
            name="done",
            args={
                "answer": "refused the shell",
                "conditions": [{"file_exists": str(marker), "contains": "present"}],
            },
        )])

    result = _run_agent(
        Agent,
        ScriptedModel(script),
        f"Do not write {target}.",
        trace,
        allow_exec=True,
        approve=lambda _action: False,
        auto_deny=True,
        max_steps=6,
    )
    assert not target.exists()
    assert result.status == "success", result
    assert result.answer == "refused the shell"
    assert _step_action(result.step_log[0]) == "shell"
    assert _step_verified(result.step_log[0]) is False
    assert _step_skipped(result.step_log[0])
    assert _step_skipped(result.step_log[0])[0]["name"] == "done"
    rows = [
        json.loads(line)
        for line in (Path(result.trace_dir) / "exec-audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert rows[0]["approval"] == "denied"
    assert rows[0]["exit_code"] is None
    assert rows[0]["command"] == command
    assert token not in rows[0]["output"]


@requires_display
def test_agent_recovers_when_verification_rejects_a_wrong_step(tmp_path, isolated_home) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    dest = _short_dest()
    dest.unlink(missing_ok=True)
    proc = launch_gtk()
    trace = tmp_path / "trace"
    trace.mkdir()
    try:
        driver = _linux_driver()
        assert wait_gtk_snapshot(driver) is not None
        _focus_app()
        _grant_linux_desktop()
        script = _WrongThenSave(dest, ModelTurn, ToolCall)
        result = _run_agent(
            Agent,
            _scripted(ScriptedModel, script),
            f"In {APP_NAME}, save the exact note to {dest} as plain text.",
            trace,
            max_steps=20,
        )
        assert script.calls >= 2, "the wrong done was accepted; verification did not continue the run"
        flags = [_step_verified(step) for step in result.step_log]
        assert False in flags, f"no step was marked verified false: {result.step_log}"
        assert True in flags, f"no later step verified: {result.step_log}"
        assert flags.index(False) < max(i for i, value in enumerate(flags) if value is True)
        assert result.status == "success", result
        assert dest.read_text() == NOTE
        _assert_conditions(result, [str(dest), NOTE.split("\n", 1)[0]])
        _assert_trace(result, trace)
    finally:
        stop_process(proc)
        dest.unlink(missing_ok=True)


@requires_display
def test_agent_changes_strategy_after_a_stuck_ping(tmp_path, isolated_home) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    dest = _short_dest()
    dest.unlink(missing_ok=True)
    proc = launch_gtk()
    trace = tmp_path / "trace"
    trace.mkdir()
    cues = _Cues()
    try:
        driver = _linux_driver()
        assert wait_gtk_snapshot(driver) is not None
        _focus_app()
        _grant_linux_desktop()
        script = _StuckThenSave(dest, ModelTurn, ToolCall, cues)
        result = _run_agent(
            Agent,
            _scripted(ScriptedModel, script),
            f"In {APP_NAME}, save the exact note to {dest} as plain text.",
            trace,
            max_steps=24,
            on_event=cues.on_event,
        )
        assert script.pings >= 3, script.pings
        assert script.cue == _REPLAN, (
            f"no strategy-change cue after {script.pings} identical Ping clicks; events={cues.events!r}"
        )
        assert cues.stuck(), cues.events
        assert result.status == "success", result
        assert dest.read_text() == NOTE
        _assert_trace(result, trace)
    finally:
        stop_process(proc)
        dest.unlink(missing_ok=True)


@pytest.fixture
def pages(tmp_path, isolated_home, monkeypatch):
    assert isolated_home.is_dir()
    assert chrome_binary(), "google-chrome or chromium is required for the page agent tests"
    profile = tmp_path / "chrome"
    proc = None
    try:
        proc, endpoint = launch_chrome(profile)
        wait_cdp(endpoint)
        monkeypatch.setenv("A11Y_COMPUTER_USE_DRIVER", "browser")
        monkeypatch.setenv("A11Y_COMPUTER_USE_CDP_ENDPOINT", endpoint)
        from a11y_computer_use.drivers.browser import BrowserDriver

        browser = BrowserDriver(endpoint=endpoint)
        with PageSite() as site:
            yield browser, site, endpoint
    finally:
        stop_process(proc)


@requires_display
def test_agent_form_result_text(tmp_path, isolated_home, pages) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    browser, site, _endpoint = pages
    from a11y_computer_use.schema import Scope

    browser.navigate(site.url("form.html"))
    browser.snapshot(Scope.WINDOW, browser._target_id)
    _grant(browser._target_id)
    trace = tmp_path / "trace"
    trace.mkdir()
    result = _run_agent(
        Agent,
        _scripted(ScriptedModel, _FormScript(ModelTurn, ToolCall)),
        "Fill Name with Ada, choose blue, check Subscribe, press Submit, and stop when the result is visible.",
        trace,
    )
    assert result.status == "success", result
    assert _eval(browser, "document.getElementById('result').textContent") == FORM_RESULT
    _assert_conditions(result, ["RESULT name=Ada"])
    _assert_trace(result, trace)


@pytest.mark.parametrize(
    ("page", "kind", "marker", "empty_js"),
    [
        ("login.html", "login", "Password", "document.getElementById('password').value"),
        ("otp.html", "2fa", "One-time code", "document.getElementById('otp').value"),
        ("card.html", "payment", "Card number", "document.getElementById('card').value"),
        ("captcha.html", "captcha", "captcha", None),
    ],
)
@requires_display
def test_agent_needs_human(tmp_path, isolated_home, pages, page, kind, marker, empty_js) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    browser, site, _endpoint = pages
    from a11y_computer_use.schema import Scope

    browser.navigate(site.url(page))
    browser.snapshot(Scope.WINDOW, browser._target_id)
    _grant(browser._target_id)
    trace = tmp_path / "trace" / kind
    trace.mkdir(parents=True)
    result = _run_agent(
        Agent,
        _scripted(ScriptedModel, _HumanScript(kind, marker, ModelTurn, ToolCall)),
        f"Complete the {page} page. Stop if a human has to take over.",
        trace,
        max_steps=6,
    )
    assert result.status == "needs_human", result
    human = _as_mapping(result.needs_human)
    assert human.get("kind") == kind, human
    assert human.get("message"), human
    assert "kind" in human and "message" in human
    for step in result.step_log:
        assert _step_action(step) not in {"type", "set_value"}, step
    if empty_js is not None:
        assert _eval(browser, empty_js) == ""


def _cli() -> list[str]:
    candidate = Path(sys.executable).with_name("a11y-agent")
    if candidate.exists():
        return [str(candidate)]
    import shutil

    found = shutil.which("a11y-agent")
    if found:
        return [found]
    return [sys.executable, "-m", "a11y_computer_use.agent.cli"]


def _cli_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["A11Y_COMPUTER_USE_ALLOW_ANY_PATH"] = "1"
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    return env


def _write_script(path: Path, turns: list[dict]) -> None:
    path.write_text(json.dumps(turns), encoding="utf-8")


def _run_cli(home: Path, args: list[str], trace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*_cli(), "run", *args, "--trace-dir", str(trace)],
        capture_output=True,
        text=True,
        timeout=120,
        env=_cli_env(home),
        check=False,
    )


def _stdout_json(proc: subprocess.CompletedProcess) -> dict:
    text = proc.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        assert start != -1 and end != -1, proc.stdout + proc.stderr
        return json.loads(text[start : end + 1])


@requires_display
def test_cli_json_exit_codes(tmp_path, isolated_home) -> None:
    _agent_api()
    home = isolated_home
    _grant_linux_desktop()
    good = Path(f"/tmp/cuagent-cli-{uuid.uuid4().hex[:8]}.txt")
    good.write_text("cli-ok", encoding="utf-8")
    missing = Path(f"/tmp/cuagent-missing-{uuid.uuid4().hex[:8]}.txt")
    missing.unlink(missing_ok=True)
    try:
        success_script = tmp_path / "success.json"
        _write_script(success_script, [{
            "calls": [{"name": "done", "args": {
                "answer": "the file is there",
                "conditions": [{"file_exists": str(good), "contains": "cli-ok"}],
            }, "id": "s1"}],
            "text": "scripted",
        }])
        success = _run_cli(
            home,
            ["--json", "--model", f"scripted:{success_script}", "--max-steps", "4",
             "Confirm the prepared file and stop."],
            tmp_path / "trace-ok",
        )
        assert success.returncode == 0, success.stderr
        body = _stdout_json(success)
        assert body.get("status") == "success", body

        human_script = tmp_path / "human.json"
        _write_script(human_script, [{
            "calls": [{"name": "ask_human", "args": {"kind": "login", "message": "scripted login stop"}, "id": "h1"}],
            "text": "scripted",
        }])
        human = _run_cli(
            home,
            ["--json", "--model", f"scripted:{human_script}", "--max-steps", "4",
             "Sign in if you can."],
            tmp_path / "trace-human",
        )
        assert human.returncode == 2, human.stderr
        assert _stdout_json(human).get("status") == "needs_human"

        # A finite script that runs out is ModelError and exits 3. Exit 1 is a
        # failed run that still had turns left: evidence keeps failing, then
        # max_steps stops the loop before the script is exhausted.
        failed_script = tmp_path / "failed.json"
        failed_turn = {"calls": [{"name": "done", "args": {
            "answer": "not saved",
            "conditions": [{"file_exists": str(missing), "contains": "nope"}],
        }, "id": "f1"}], "text": "scripted"}
        _write_script(failed_script, [failed_turn, failed_turn, failed_turn])
        failed = _run_cli(
            home,
            ["--json", "--model", f"scripted:{failed_script}", "--max-steps", "2",
             "Save a file that is not there."],
            tmp_path / "trace-failed",
        )
        assert failed.returncode == 1, failed.stderr + failed.stdout
        failed_body = _stdout_json(failed)
        assert failed_body.get("status") == "failed", failed_body
        assert failed_body.get("reason") == "max_steps", failed_body

        broken = _run_cli(
            home,
            ["--json", "--model", "scripted:/tmp/cuagent-no-such-script.json", "--max-steps", "2",
             "This model spec does not resolve."],
            tmp_path / "trace-broken",
        )
        assert broken.returncode == 3, broken.stderr
    finally:
        good.unlink(missing_ok=True)


def _clear_focus() -> str:
    """Drop the active window so the next observation is the empty desktop."""
    import shutil

    if shutil.which("wmctrl"):
        subprocess.run(["wmctrl", "-k", "on"], check=False, timeout=3, capture_output=True)
    try:
        from Xlib import Xatom, display

        opened = display.Display()
        root = opened.screen().root
        atom = opened.intern_atom("_NET_ACTIVE_WINDOW")
        root.change_property(atom, Xatom.WINDOW, 32, [0])
        opened.flush()
        opened.close()
    except Exception:
        pass
    try:
        from a11y_computer_use.drivers import _linux_system

        return _linux_system.frontmost_app_id() or "unknown"
    except Exception:
        return "unknown"


@requires_display
def test_agent_empty_desktop_first_observation_is_usable(tmp_path, isolated_home) -> None:
    """No focused app: the scripted model sees a desktop overview, not unknown."""
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    marker = isolated_home / "desk.txt"
    marker.write_text("ready", encoding="utf-8")
    front = _clear_focus()
    seen: list[str] = []

    def script(messages):
        seen.append(message_text(messages))
        return ModelTurn(calls=[ToolCall(
            name="done",
            args={
                "answer": "desktop",
                "conditions": [{"file_exists": str(marker), "contains": "ready"}],
            },
        )])

    trace = tmp_path / "trace"
    trace.mkdir()
    result = _run_agent(
        Agent,
        ScriptedModel(script),
        "Look at the desktop and stop.",
        trace,
        approve=None,
        auto_deny=True,
        max_steps=4,
    )
    assert seen, result
    first = seen[0]
    assert front in {"", "unknown"}, front
    assert "No application is focused" in first
    assert "launch" in first
    assert "unknown has no permission grant" not in first
    assert "ask the user" not in first
    assert "needs_permission" not in first
    assert result.status == "success", result


@requires_display
def test_agent_files_and_terminal_grants_cover_those_names(tmp_path, isolated_home) -> None:
    """A grant for the real binary covers Files and Terminal."""
    import shutil

    from a11y_computer_use import safety, server
    from a11y_computer_use.safety import Tier

    files = next((
        name for name in ("thunar", "nautilus", "nemo", "dolphin", "pcmanfm")
        if shutil.which(name)
    ), None)
    terminal = next((
        name for name in ("xfce4-terminal", "gnome-terminal", "xterm", "konsole", "kitty")
        if shutil.which(name)
    ), None)
    if files is None and terminal is None:
        pytest.skip("no file manager or terminal binary")
    store = safety.PermissionStore()
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"))
    runtime.APP_LAUNCH_WAIT_S = 12
    try:
        if files is not None:
            store.set_tier(files, Tier.FULL)
            text = runtime.app("launch", "Files")
            assert "needs_permission" not in text, text
            assert "ask the user" not in text, text
        if terminal is not None:
            store.set_tier(terminal, Tier.FULL)
            text = runtime.app("launch", "Terminal")
            assert "needs_permission" not in text, text
            assert "ask the user" not in text, text
    finally:
        for name in (files, terminal):
            if not name:
                continue
            try:
                runtime.app("quit", name)
            except Exception:
                subprocess.run(["pkill", "-x", name], check=False, timeout=3)


@requires_display
def test_agent_submit_button_does_not_need_approval(tmp_path, isolated_home) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    script_path = tmp_path / "submit.py"
    script_path.write_text(
        "import gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "from gi.repository import GLib, Gtk\n"
        "GLib.set_prgname('cuagentsubmit')\n"
        "window = Gtk.Window(title='cuagentsubmit')\n"
        "window.set_default_size(320, 120)\n"
        "box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)\n"
        "status = Gtk.Label(label='idle')\n"
        "button = Gtk.Button(label='Submit')\n"
        "button.connect('clicked', lambda _button: status.set_text('submitted'))\n"
        "box.pack_start(status, False, False, 0)\n"
        "box.pack_start(button, False, False, 0)\n"
        "window.add(box)\n"
        "window.connect('destroy', Gtk.main_quit)\n"
        "window.show_all()\n"
        "Gtk.main()\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    proc = subprocess.Popen([sys.executable, str(script_path)], env=env)
    trace = tmp_path / "trace"
    trace.mkdir()
    try:
        driver = _linux_driver()
        deadline = time.monotonic() + 15
        from a11y_computer_use.schema import Scope

        snap = None
        while time.monotonic() < deadline:
            try:
                snap = driver.snapshot(Scope.WINDOW, "cuagentsubmit")
            except Exception:
                snap = None
            if snap is not None and any(el.title == "Submit" for el in snap.elements):
                break
            time.sleep(0.2)
        assert snap is not None and any(el.title == "Submit" for el in snap.elements)
        _grant("cuagentsubmit", "python3", "python")

        def script(messages):
            text = message_text(messages)
            if "submitted" in text:
                marker = isolated_home / "submitted.txt"
                marker.write_text("submitted", encoding="utf-8")
                return ModelTurn(calls=[ToolCall(
                    name="done",
                    args={
                        "answer": "submitted",
                        "conditions": [{"file_exists": str(marker), "contains": "submitted"}],
                    },
                )])
            elements = parse_snapshot(text)
            button = next((item for item in elements if item.name == "Submit"), None)
            assert button is not None, text
            return ModelTurn(calls=[ToolCall(name="click", args={"ref": button.ref})])

        result = _run_agent(
            Agent,
            ScriptedModel(script),
            "Click Submit.",
            trace,
            approve=None,
            auto_deny=True,
            max_steps=6,
        )
        assert result.status == "success", result
        assert result.status != "needs_human"
        assert result.answer == "submitted"
        assert not any(
            str(getattr(step, "error", "") or "").startswith("approval_denied")
            for step in result.step_log
        )
    finally:
        stop_process(proc)


@requires_display
def test_agent_number_min_only_accepts_a_value(tmp_path, isolated_home) -> None:
    """A Chrome number input with min and no max accepts 3."""
    from a11y_computer_use import observe, safety, server
    from a11y_computer_use.schema import ErrorCode, Scope
    from a11y_computer_use.safety import Tier

    binary = chrome_binary()
    if binary is None:
        pytest.skip("no Chrome/Chromium binary")
    page = tmp_path / "minonly.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>cuaminonly</title>"
        "<label>Qty <input id=qty type=number min=0 aria-label=Qty></label>",
        encoding="utf-8",
    )
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    proc = subprocess.Popen(
        [
            binary, "--force-renderer-accessibility", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--window-size=800,600", page.resolve().as_uri(),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        store = safety.PermissionStore()
        for app_id in ("chrome", "chromium", "google-chrome", "chromium-browser"):
            store.set_tier(app_id, Tier.FULL)
        runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"))
        deadline = time.monotonic() + 45
        snap = None
        while time.monotonic() < deadline:
            for app_id in ("chrome", "chromium", "google-chrome"):
                try:
                    shot = runtime.driver.snapshot(Scope.WINDOW, app_id)
                except Exception as exc:
                    if getattr(exc, "code", None) not in {None, ErrorCode.APP_NOT_FOUND}:
                        raise
                    shot = None
                if shot is not None and any(el.title == "Qty" for el in shot.elements):
                    snap = shot
                    break
            if snap is not None:
                break
            time.sleep(0.4)
        assert snap is not None, "Chrome did not expose the min-only number field"
        field = next(el for el in snap.elements if el.title == "Qty")
        runtime._current = snap
        result = runtime.set_value(field.ref, "3")
        assert "outside" not in result
        assert "0..0" not in result
        again = runtime.driver.snapshot(Scope.WINDOW, snap.app or "chrome")
        live = next(el for el in again.elements if el.title == "Qty")
        assert str(live.value) in {"3", "3.0"}, (result, observe.render_text(again))
    finally:
        stop_process(proc)

