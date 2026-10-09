"""Live M1 agent runs against the GTK fixture and the local HTML pages.

The fixture smoke test launches the GTK app and the pages and snapshots them
with the Linux and browser drivers. It does not import the agent package.

The agent tests call ``Agent(...).run(goal)`` with a ``ScriptedModel`` callable.
That callable is scripted, not an LLM: it reads element refs out of the
snapshot text in the messages. They are skipped with
``pytest.importorskip("a11y_computer_use.agent.core")`` until that module
lands. Nothing else skips them.
"""

from __future__ import annotations

import json
import os
import re
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

# Collection-time gate for the smoke test only. The Linux live job exports
# DISPLAY (xvfb-run). Hermetic jobs do not, and they skip this one test the
# same way the other GTK live tests do. It is not an agent-package skip.
_LINUX_DISPLAY = sys.platform.startswith("linux") and bool(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
)

_STRATEGY = re.compile(
    r"change strategy|strategy change|new strategy|you are stuck|seem stuck|"
    r"try a different|different approach|different action|stop repeating|"
    r"same action|no progress|not making progress|repeating the same",
    re.I,
)


def _linux_driver():
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    driver.ensure_trusted()
    return driver


def _focus_app() -> None:
    """Best-effort: ask the window manager to put the fixture in front."""
    subprocess.run(
        ["xdotool", "search", "--name", APP_NAME, "windowactivate", "--sync"],
        check=False,
        timeout=5,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Fixture smoke (no agent import, no importorskip)
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


@pytest.mark.skipif(
    not _LINUX_DISPLAY,
    reason="GTK and Chrome fixtures need a Linux display; the linux live job provides Xvfb",
)
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
    otp = element(snap, role="AXTextField", title="Authentication code")
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
# Agent runs. importorskip until a11y_computer_use.agent.core exists.
# ---------------------------------------------------------------------------


def _agent_api():
    pytest.importorskip("a11y_computer_use.agent.core")
    from a11y_computer_use.agent.core import Agent
    from a11y_computer_use.agent.models.base import ModelTurn, ToolCall

    try:
        from a11y_computer_use.agent.models.scripted import ScriptedModel
    except ImportError:
        from a11y_computer_use.agent.models import ScriptedModel  # type: ignore
    return Agent, ScriptedModel, ModelTurn, ToolCall


def _scripted(scripted_cls, fn):
    try:
        return scripted_cls(fn)
    except TypeError:
        return scripted_cls(script=fn)


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

    def hit(self, since: int = 0) -> str:
        for event in self.events[since:]:
            kind = ""
            if isinstance(event, dict):
                kind = str(event.get("type") or event.get("kind") or event.get("name") or "")
            else:
                for attr in ("type", "kind", "name"):
                    if hasattr(event, attr):
                        kind = str(getattr(event, attr))
                        break
            blob = f"{kind} {event!r}"
            if kind.lower() in {"strategy", "strategy_change", "stuck"} or "strategy" in kind.lower():
                return blob
            match = _STRATEGY.search(blob)
            if match:
                return match.group(0)
        return ""


class _StuckThenSave(_SaveScript):
    """Clicks Ping, which changes nothing, until the agent asks for a new strategy."""

    def __init__(self, dest: Path, turn_cls, call_cls, cues: _Cues) -> None:
        super().__init__(dest, turn_cls, call_cls)
        self.cues = cues
        self.pings = 0
        self.cue = ""
        self.baseline = None
        self.event_mark: int | None = None

    def __call__(self, messages):
        self.calls += 1
        text = message_text(messages)
        last = message_text(messages[-1:]) if messages else ""
        if self.baseline is None:
            self.baseline = last
        found = ""
        if self.event_mark is not None:
            found = self.cues.hit(self.event_mark)
        match = _STRATEGY.search(last)
        # A sentence that was already in the first observation is the prompt,
        # not a strategy change after the repeated Ping clicks.
        if (
            match
            and self.pings >= 3
            and match.group(0).lower() not in (self.baseline or "").lower()
        ):
            found = found or match.group(0)
        if self.pings >= 3 and found:
            self.cue = found
            return _save_turn(text, self.dest, self.Turn, self.Call)
        self.pings += 1
        if self.pings == 3:
            # Events and text that arrive after this click are the stuck signal.
            self.event_mark = len(self.cues.events)
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
        return self.Turn(calls=[self.Call(name="done", args={
            "answer": FORM_RESULT,
            "conditions": [
                {"window_title_contains": FORM_RESULT},
                {"element": {"role": "AXStaticText", "name": FORM_RESULT}},
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
        assert script.cue, f"no strategy-change cue after {script.pings} identical Ping clicks; events={cues.events!r}"
        assert result.status == "success", result
        assert dest.read_text() == NOTE
        _assert_trace(result, trace)
    finally:
        stop_process(proc)
        dest.unlink(missing_ok=True)


@pytest.fixture
def pages(tmp_path, isolated_home):
    pytest.importorskip("a11y_computer_use.agent.core")
    assert isolated_home.is_dir()
    assert chrome_binary()
    profile = tmp_path / "chrome"
    proc = None
    try:
        proc, endpoint = launch_chrome(profile)
        wait_cdp(endpoint)
        os.environ["A11Y_COMPUTER_USE_DRIVER"] = "browser"
        os.environ["A11Y_COMPUTER_USE_CDP_ENDPOINT"] = endpoint
        from a11y_computer_use.drivers.browser import BrowserDriver

        browser = BrowserDriver(endpoint=endpoint)
        with PageSite() as site:
            yield browser, site, endpoint
    finally:
        os.environ.pop("A11Y_COMPUTER_USE_DRIVER", None)
        os.environ.pop("A11Y_COMPUTER_USE_CDP_ENDPOINT", None)
        stop_process(proc)


def test_agent_form_result_text(tmp_path, isolated_home, pages) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    browser, site, _endpoint = pages
    from a11y_computer_use.schema import Scope

    browser.navigate(site.url("form.html"))
    browser.snapshot(Scope.WINDOW, browser._target_id)
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
        ("otp.html", "2fa", "Authentication code", "document.getElementById('otp').value"),
        ("card.html", "payment", "Card number", "document.getElementById('card').value"),
        ("captcha.html", "captcha", "captcha", None),
    ],
)
def test_agent_needs_human(tmp_path, isolated_home, pages, page, kind, marker, empty_js) -> None:
    Agent, ScriptedModel, ModelTurn, ToolCall = _agent_api()
    browser, site, _endpoint = pages
    from a11y_computer_use.schema import Scope

    browser.navigate(site.url(page))
    browser.snapshot(Scope.WINDOW, browser._target_id)
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


def test_cli_json_exit_codes(tmp_path, isolated_home) -> None:
    _agent_api()
    home = isolated_home
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

        failed_script = tmp_path / "failed.json"
        failed_turn = {"calls": [{"name": "done", "args": {
            "answer": "not saved",
            "conditions": [{"file_exists": str(missing), "contains": "nope"}],
        }, "id": "f1"}], "text": "scripted"}
        _write_script(failed_script, [failed_turn, failed_turn])
        failed = _run_cli(
            home,
            ["--json", "--model", f"scripted:{failed_script}", "--max-steps", "4",
             "Save a file that is not there."],
            tmp_path / "trace-failed",
        )
        assert failed.returncode == 1, failed.stderr
        assert _stdout_json(failed).get("status") == "failed"

        broken = _run_cli(
            home,
            ["--json", "--model", "scripted:/tmp/cuagent-no-such-script.json", "--max-steps", "2",
             "This model spec does not resolve."],
            tmp_path / "trace-broken",
        )
        assert broken.returncode == 3, broken.stderr
    finally:
        good.unlink(missing_ok=True)

