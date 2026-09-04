"""Safety hardening: the gaps the 2026-09 fact-check found in the safety layer,
each closed and pinned here.

1. Refusals that happen before an `Action` exists (a ref that no longer
   resolves, a secure field handed to `set_value`) leave an audit row.
2. Coordinate `click`/`drag`/`scroll` refuse a secure field on EVERY driver:
   a resolved secure element directly, a raw point by hit-testing the latest
   snapshot (the macOS executor did this on its own; browser and Linux did not).
3. `type` probes for a focused password field on the browser (CDP), Linux
   (AT-SPI) and Windows (UIA `IsPassword`) backends, not only macOS; the UIA
   accessor marks password edits secure so the snapshot withholds their value.
4. The browser Runtime recheck (tabs as apps) yields `focus_changed`.
5. `window raise` runs through the driver seam: macOS activates the owner,
   Linux sends `_NET_ACTIVE_WINDOW`, browser and Windows answer `unsupported`.
6. Browser coordinate input emits the exact CDP mouse payloads.
7. Every wire-stable error code and refusal verdict renders exactly once.

Everything here is hermetic: fake drivers, the scripted CDP transport, fake
Xlib / AT-SPI / UIA modules. Nothing needs a grant, a bus, or a browser.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from computeruse import safety, server
from computeruse.drivers import _atspi, _linux_system, _uia, linux, windows
from computeruse.observe import DisplayGeometry, build_snapshot
from computeruse.schema import (
    ComputerUseError,
    Display,
    ErrorCode,
    MouseButton,
    Point,
    Scope,
    ScrollUnit,
)
from tests.conftest import build_synthetic_snapshot
from tests.test_browser import ScriptedTransport, _CDPError, _driver_on, _fixture_responder

APP = "com.apple.TextEdit"


def _audit_rows(audit_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    return rows


# --------------------------------------------------------------------------- #
# A recording fake driver over the synthetic snapshot (display 1, 2x)
# --------------------------------------------------------------------------- #
class _FakeDriver:
    """Resolves app identity itself (like the browser driver) so the Runtime
    gates raw points against `frontmost_app`, and records every input call."""

    name = "fake"
    resolves_apps = True

    def __init__(self, snap=None, *, press_ok: bool = False) -> None:
        self.snap = snap or build_synthetic_snapshot()
        self.press_ok = press_ok
        self.calls: dict[str, list] = {
            "click": [], "drag": [], "scroll": [], "type": [], "press": [],
            "into_view": [], "raise": [], "set": [],
        }

    def ensure_trusted(self) -> None:
        pass

    def frontmost_app(self):
        return APP, 4242

    def snapshot(self, scope, app):
        return self.snap

    def resolve_ref(self, snap, ref, *, live=None):
        try:
            return snap.element(ref)
        except KeyError:
            raise ComputerUseError(
                ErrorCode.STALE_REF, f"{ref} no longer resolves",
                detail={"ref": ref, "reason": "not_found", "candidates": []},
            ) from None

    def press_element(self, element) -> bool:
        self.calls["press"].append(element.ref)
        return self.press_ok

    def scroll_into_view(self, element) -> bool:
        self.calls["into_view"].append(element.ref)
        return True

    def set_value(self, element, value) -> bool:
        self.calls["set"].append((element.ref, value))
        return True

    def click(self, target, **kw):
        self.calls["click"].append((target, kw))

    def drag(self, start, end, **kw):
        self.calls["drag"].append((start, end))

    def scroll(self, target, **kw):
        self.calls["scroll"].append((target, kw))

    def type_text(self, text, **kw):
        self.calls["type"].append(text)

    def main_display_id(self) -> int:
        return 1

    def window_owner(self, window_id: int) -> str:
        if window_id == 404:
            raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "no window 404",
                                   detail={"window_id": window_id})
        return "com.owner"

    def raise_window(self, window_id: int) -> None:
        self.calls["raise"].append(window_id)


def _runtime(tmp_path: Path, *, tier: safety.Tier = safety.Tier.FULL, **kw):
    driver = _FakeDriver(**kw)
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, tier)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    return rt, driver, tmp_path / "audit"


# --------------------------------------------------------------------------- #
# 1. pre-gate refusals are audited
# --------------------------------------------------------------------------- #
def test_stale_ref_from_an_unknown_ref_is_audited(tmp_path) -> None:
    rt, driver, audit_dir = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as ei:
        rt.click(ref="e99")
    assert ei.value.code is ErrorCode.STALE_REF
    row = _audit_rows(audit_dir)[-1]
    assert row["action"] == "click" and row["result"] == "stale_ref"
    assert row["decision"] is None  # the gate never ran: no verdict to report
    assert row["params"] == {"ref": "e99", "snapshot_id": "snap-test-1", "reason": "not_found"}
    assert row["app"] == APP
    assert driver.calls["click"] == []


def test_stale_ref_before_any_snapshot_is_audited_against_unknown(tmp_path) -> None:
    rt, _driver, audit_dir = _runtime(tmp_path)
    with pytest.raises(ComputerUseError):
        rt.scroll(ref="e2", dy=1)
    row, = _audit_rows(audit_dir)
    assert row["action"] == "scroll" and row["result"] == "stale_ref"
    assert row["app"] == "unknown" and row["params"]["snapshot_id"] is None


def test_stale_ref_from_the_live_rewalk_is_audited_with_its_reason(tmp_path) -> None:
    rt, driver, audit_dir = _runtime(tmp_path)
    rt.desktop_snapshot(APP)

    def gone(snap, ref, *, live=None):
        raise ComputerUseError(ErrorCode.STALE_REF, "moved", detail={"ref": ref, "reason": "ambiguous"})

    driver.resolve_ref = gone
    with pytest.raises(ComputerUseError):
        rt.drag(start_ref="e2", end_ref="e3")
    row = _audit_rows(audit_dir)[-1]
    assert row["action"] == "drag" and row["params"]["reason"] == "ambiguous"


def test_wait_for_on_a_stale_ref_is_audited(tmp_path) -> None:
    rt, _driver, audit_dir = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError):
        rt.wait_for("e77")
    assert _audit_rows(audit_dir)[-1]["action"] == "waitfor"


def test_set_value_secure_refusal_is_audited_without_the_value(tmp_path) -> None:
    rt, driver, audit_dir = _runtime(tmp_path, tier=safety.Tier.READ)  # tier is irrelevant
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as ei:
        rt.set_value("e4", "hunter2")  # e4 is the synthetic password field
    assert ei.value.code is ErrorCode.SECURE_FIELD  # secure wins over the tier gate
    row = _audit_rows(audit_dir)[-1]
    assert row["action"] == "typetext" and row["result"] == "secure_field"
    assert row["params"] == {"ref": "e4", "role": "AXTextField"}
    assert "hunter2" not in json.dumps(row)
    assert driver.calls["set"] == [] and driver.calls["type"] == []


# --------------------------------------------------------------------------- #
# 2. coordinate actions refuse secure fields on every driver (shared core)
# --------------------------------------------------------------------------- #
INSIDE_PASSWORD = dict(x=300, y=1160, display_id=1)  # inside e4 Bounds(1, 240, 1140, 400, 56)
INSIDE_SAVE = dict(x=300, y=168, display_id=1)  # inside e2 Bounds(1, 240, 140, 120, 56)


def test_coordinate_click_over_a_secure_field_is_refused_and_audited(tmp_path) -> None:
    rt, driver, audit_dir = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as ei:
        rt.click(**INSIDE_PASSWORD)
    assert ei.value.code is ErrorCode.SECURE_FIELD
    assert ei.value.detail == {"ref": "e4", "role": "AXTextField"}
    assert driver.calls["click"] == []
    row = _audit_rows(audit_dir)[-1]
    assert row["action"] == "click" and row["result"] == "secure_field"
    assert row["decision"]["verdict"] == "allow"  # the tier allowed it; the field refused


def test_coordinate_click_elsewhere_proceeds(tmp_path) -> None:
    rt, driver, _ = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    assert rt.click(**INSIDE_SAVE).startswith("clicked")
    (target, _kw), = driver.calls["click"]
    assert (target.x, target.y) == (300, 168)


def test_coordinate_click_with_no_snapshot_has_nothing_to_check(tmp_path) -> None:
    rt, driver, _ = _runtime(tmp_path)
    rt.click(**INSIDE_PASSWORD)  # no snapshot: the point cannot be attributed
    assert len(driver.calls["click"]) == 1


def test_ref_click_on_a_secure_element_never_falls_back_to_the_mouse(tmp_path) -> None:
    # press_element returns False for secure elements on every driver; without
    # this check the fallback synthetic click would land on the password field.
    rt, driver, audit_dir = _runtime(tmp_path, press_ok=False)
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as ei:
        rt.click(ref="e4")
    assert ei.value.code is ErrorCode.SECURE_FIELD
    assert driver.calls["press"] == [] and driver.calls["click"] == []
    assert _audit_rows(audit_dir)[-1]["result"] == "secure_field"


def test_drag_refuses_when_either_endpoint_is_secure(tmp_path) -> None:
    rt, driver, _ = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as ei:
        rt.drag(start_ref="e2", end_x=300, end_y=1160, display_id=1)
    assert ei.value.code is ErrorCode.SECURE_FIELD and ei.value.detail["ref"] == "e4"
    with pytest.raises(ComputerUseError):
        rt.drag(start_ref="e4", end_ref="e2")
    assert driver.calls["drag"] == []
    rt.drag(start_ref="e2", end_ref="e3")
    assert len(driver.calls["drag"]) == 1


def test_wheel_scroll_over_a_secure_field_is_refused_but_into_view_is_not(tmp_path) -> None:
    rt, driver, _ = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as ei:
        rt.scroll(dy=3, **INSIDE_PASSWORD)
    assert ei.value.code is ErrorCode.SECURE_FIELD
    assert driver.calls["scroll"] == []
    # revealing a field through the accessibility API injects nothing
    assert "into view" in rt.scroll(ref="e4", into_view=True)
    assert driver.calls["into_view"] == ["e4"]


def test_secure_hit_test_picks_the_smallest_containing_element(tmp_path) -> None:
    rt, _driver, _ = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    assert rt._secure_element_under(Point(1, 300, 1160)).ref == "e4"
    assert rt._secure_element_under(Point(1, 300, 168)) is None  # the Save button
    assert rt._secure_element_under(Point(2, 300, 1160)) is None  # another display
    assert rt._secure_element_under(Point(1, 5, 5)) is None  # outside every element


def test_ref_click_on_secure_field_goes_through_the_gate_first(tmp_path) -> None:
    # The pointer refusal runs inside the gate, so a tier too low for clicking is
    # still reported as the refusal it is (the macOS executor has behaved this
    # way since the MVP; the check only adds the same result on other drivers).
    rt, _driver, audit_dir = _runtime(tmp_path, tier=safety.Tier.READ)
    rt.desktop_snapshot(APP)
    with pytest.raises(server.ActionRefused):
        rt.click(**INSIDE_PASSWORD)
    assert _audit_rows(audit_dir)[-1]["result"] == "deny"


# --------------------------------------------------------------------------- #
# 3. `type` refuses a focused password field: browser, Linux, Windows
# --------------------------------------------------------------------------- #
def test_browser_type_refuses_when_the_focused_element_is_a_password() -> None:
    def responder(method, params):
        if method == "Runtime.evaluate":
            assert "activeElement" in params["expression"] and params["returnByValue"] is True
            return {"result": {"type": "boolean", "value": True}}
        return _fixture_responder(method, params)

    d, t = _driver_on(responder)
    with pytest.raises(ComputerUseError) as ei:
        d.type_text("hunter2")
    assert ei.value.code is ErrorCode.SECURE_FIELD
    assert ei.value.detail["api"] == "document.activeElement"
    assert "Input.insertText" not in t.methods()


def test_browser_type_proceeds_only_when_focus_is_verified_as_nonsecure() -> None:
    d, t = _driver_on()  # fixture: activeElement is not a password
    d.type_text("hello")
    assert ("Input.insertText", {"text": "hello"}) in t.sent

    def failing(method, params):
        if method == "Runtime.evaluate":
            raise _CDPError("Runtime domain disabled")
        return _fixture_responder(method, params)

    d2, t2 = _driver_on(failing)
    with pytest.raises(ComputerUseError) as ei:
        d2.type_text("hello")
    assert ei.value.code is ErrorCode.UNSUPPORTED
    assert "Input.insertText" not in t2.methods()


def test_browser_password_typing_refused_through_the_runtime_and_audited(tmp_path) -> None:
    def responder(method, params):
        if method == "Runtime.evaluate":
            return {"result": {"type": "boolean", "value": True}}
        return _fixture_responder(method, params)

    d, _t = _driver_on(responder)
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("TAB1", safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)
    with pytest.raises(ComputerUseError) as ei:
        rt.type_text("hunter2")
    assert ei.value.code is ErrorCode.SECURE_FIELD
    row, = _audit_rows(tmp_path / "audit")
    assert row["result"] == "secure_field" and row["params"]["text"] == safety.REDACTED


class _FakeAcc:
    """A minimal AT-SPI accessible: role name, focus state, children."""

    def __init__(self, role: str, *, focused: bool = False, children=()):
        self._role, self._focused, self._children = role, focused, list(children)

    def get_role_name(self):
        return self._role

    def get_state_set(self):
        focused = self._focused

        class _Set:
            def contains(self, state):
                return state == "FOCUSED" and focused

        return _Set()

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, i):
        return self._children[i]


@pytest.fixture
def fake_atspi(monkeypatch):
    fake = types.SimpleNamespace(StateType=types.SimpleNamespace(FOCUSED="FOCUSED"))
    monkeypatch.setattr(_atspi, "_atspi", lambda: fake)
    return fake


def test_atspi_focused_secure_finds_the_focused_password_field(fake_atspi, monkeypatch) -> None:
    password = _FakeAcc("password text", focused=True)
    root = _FakeAcc("frame", children=[_FakeAcc("entry"), _FakeAcc("panel", children=[password])])
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: root)
    assert _atspi.focused_secure("app") is True
    assert _atspi.is_secure(password) and not _atspi.is_secure(_FakeAcc("entry"))


def test_atspi_focused_secure_is_false_for_a_focused_entry_or_no_focus(fake_atspi, monkeypatch) -> None:
    root = _FakeAcc("frame", children=[_FakeAcc("entry", focused=True), _FakeAcc("password text")])
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: root)
    assert _atspi.focused_secure("app") is False
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: _FakeAcc("frame", children=[_FakeAcc("entry")]))
    assert _atspi.focused_secure("app") is False
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: None)
    assert _atspi.focused_secure("missing") is False


def test_atspi_focused_secure_respects_the_node_bound(fake_atspi, monkeypatch) -> None:
    # the focused password sits deeper than the walk is allowed to go
    deep = _FakeAcc("password text", focused=True)
    root = _FakeAcc("frame", children=[_FakeAcc("panel", children=[_FakeAcc("panel", children=[deep])])])
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: root)
    # bound exhausted without meeting the focused node: UNKNOWN, never "not secure"
    assert _atspi.focused_secure("app", max_nodes=2) is None
    assert _atspi.focused_secure("app", max_nodes=10) is True
    # a node with more children than the walk fetches leaves part of the frame unseen
    wide = _FakeAcc("frame", children=[_FakeAcc("panel")] * (_atspi._MAX_CHILDREN_FETCH + 1))
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: wide)
    assert _atspi.focused_secure("app", max_nodes=10_000) is None


def test_atspi_focused_secure_asks_the_collection_interface_first(fake_atspi, monkeypatch) -> None:
    """GTK3/Chromium/Firefox/Electron frames expose org.a11y.atspi.Collection: one
    round-trip finds the FOCUSED node wherever it sits, so a login form behind a
    400-node header is still refused, and the walk (400 x D-Bus) is skipped."""
    fake_atspi.StateSet = types.SimpleNamespace(new=lambda states: ("states", tuple(states)))
    fake_atspi.CollectionMatchType = types.SimpleNamespace(ALL="ALL", NONE="NONE")
    fake_atspi.MatchRule = types.SimpleNamespace(new=lambda *args: ("rule", args))
    fake_atspi.CollectionSortOrder = types.SimpleNamespace(CANONICAL="CANONICAL")
    asked: list = []

    class _Frame(_FakeAcc):
        def __init__(self, hits):
            super().__init__("frame", children=[_FakeAcc("panel")] * 600)  # far past max_nodes
            self.hits, self.walked = hits, 0

        def get_collection_iface(self):
            frame = self

            class _Coll:
                def get_matches(self, rule, order, count, traverse):
                    asked.append((rule, order, count, traverse))
                    return list(frame.hits)

            return _Coll()

        def get_child_count(self):
            self.walked += 1
            return super().get_child_count()

    password = _FakeAcc("password text", focused=True)
    frame = _Frame([password])
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: frame)
    assert _atspi.focused_secure("app") is True and frame.walked == 0
    assert asked == [(("rule", (("states", ("FOCUSED",)), "ALL", {}, "NONE", [], "NONE", [], "NONE", False)),
                      "CANONICAL", 1, True)]
    frame = _Frame([_FakeAcc("entry", focused=True)])
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: frame)
    assert _atspi.focused_secure("app") is False and frame.walked == 0
    frame = _Frame([])  # Collection answered: nothing is focused, a definite False
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: frame)
    assert _atspi.focused_secure("app") is False and frame.walked == 0


@pytest.fixture
def linux_x11(monkeypatch):
    """A LinuxDriver on X11 (not Wayland) with the XTEST typing module faked."""
    from computeruse import drivers as pkg

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    typed: list[str] = []
    fake_input = types.ModuleType("computeruse.drivers._linux_input")
    fake_input.type_string = typed.append
    monkeypatch.setitem(sys.modules, "computeruse.drivers._linux_input", fake_input)
    monkeypatch.setattr(pkg, "_linux_input", fake_input, raising=False)
    d = linux.LinuxDriver()
    monkeypatch.setattr(d, "frontmost_app", lambda: ("gedit", 42))
    return d, typed


def test_linux_type_refuses_when_the_focused_node_is_a_password(linux_x11, monkeypatch) -> None:
    d, typed = linux_x11
    monkeypatch.setattr(_atspi, "focused_secure", lambda app, **kw: app == "gedit")
    with pytest.raises(ComputerUseError) as ei:
        d.type_text("hunter2")
    assert ei.value.code is ErrorCode.SECURE_FIELD and "STATE_FOCUSED" in ei.value.detail["api"]
    assert typed == []
    monkeypatch.setattr(_atspi, "focused_secure", lambda app, **kw: False)
    d.type_text("hello")
    assert typed == ["hello"]


def test_linux_type_refuses_when_the_focus_probe_cannot_decide(linux_x11, monkeypatch) -> None:
    # None = the walk ran out of its bound without meeting the focused node; typing
    # blind could land the secret in a password field, so it is a refusal too.
    d, typed = linux_x11
    monkeypatch.setattr(_atspi, "focused_secure", lambda app, **kw: None)
    with pytest.raises(ComputerUseError) as ei:
        d.type_text("hunter2")
    assert ei.value.code is ErrorCode.SECURE_FIELD and "exhausted" in ei.value.detail["api"]
    assert ei.value.detail["app"] == "gedit" and typed == []


def test_linux_type_refuses_a_driver_focused_password_editable(linux_x11, monkeypatch) -> None:
    d, typed = linux_x11
    d._focused_editable = object()  # press_element focused something the snapshot called editable
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: True)
    with pytest.raises(ComputerUseError) as ei:
        d.type_text("hunter2")
    assert ei.value.code is ErrorCode.SECURE_FIELD and "password text" in ei.value.detail["api"]
    assert typed == []


class _FakeUIANode:
    def __init__(self, control: str, *, password: bool = False, value: str | None = None,
                 name: str = "", rect=(10, 10, 210, 40)):
        self.ControlTypeName, self.IsPassword, self.Name = control, password, name
        self._value = value
        left, top, right, bottom = rect
        self.BoundingRectangle = types.SimpleNamespace(left=left, top=top, right=right, bottom=bottom)
        self.IsEnabled, self.HasKeyboardFocus, self.AutomationId = True, False, ""

    def GetValuePattern(self):
        if self._value is None:
            return None
        return types.SimpleNamespace(Value=self._value)

    def GetInvokePattern(self):
        return None

    GetTogglePattern = GetExpandCollapsePattern = GetSelectionItemPattern = GetInvokePattern

    def GetChildren(self):
        return []


def test_uia_password_edit_maps_to_secure_and_its_value_is_never_read() -> None:
    acc = _uia.UIAAccessor()
    pw = acc.read(_FakeUIANode("EditControl", password=True, value="hunter2", name="Password"))
    assert pw.role == "AXSecureTextField" and pw.value is None
    plain = acc.read(_FakeUIANode("EditControl", value="alice", name="Name"))
    assert plain.role == "AXTextField" and plain.value == "alice"
    # through the shared engine the element is `secure`, so press/set_value refuse it
    root = _FakeUIANode("WindowControl", name="Login", rect=(0, 0, 800, 600))
    root.GetChildren = lambda: [_FakeUIANode("EditControl", password=True, value="hunter2")]
    geometry = (DisplayGeometry(display=Display(0, 800, 600, 1.0, True), origin=(0.0, 0.0)),)
    snap = build_snapshot(root, acc, scope=Scope.WINDOW, app="app.exe", pid=1, geometry=geometry)
    secure = [el for el in snap.elements if el.secure]
    assert secure and all(el.value is None for el in secure)
    assert windows.WindowsDriver().set_value(secure[0], "x") is False


@pytest.fixture
def fake_uiautomation(monkeypatch):
    from computeruse import drivers as pkg

    state = {"focused": None}
    auto = types.ModuleType("uiautomation")
    auto.GetFocusedControl = lambda: state["focused"]
    monkeypatch.setitem(sys.modules, "uiautomation", auto)
    typed: list[str] = []
    fake_input = types.ModuleType("computeruse.drivers._win_input")
    fake_input.type_unicode = typed.append
    monkeypatch.setitem(sys.modules, "computeruse.drivers._win_input", fake_input)
    monkeypatch.setattr(pkg, "_win_input", fake_input, raising=False)
    return state, typed


def test_windows_type_refuses_a_focused_password_control(fake_uiautomation) -> None:
    state, typed = fake_uiautomation
    state["focused"] = types.SimpleNamespace(IsPassword=True)
    with pytest.raises(ComputerUseError) as ei:
        windows.WindowsDriver().type_text("hunter2")
    assert ei.value.code is ErrorCode.SECURE_FIELD and "IsPassword" in ei.value.detail["api"]
    assert typed == []
    state["focused"] = types.SimpleNamespace(IsPassword=False)
    windows.WindowsDriver().type_text("hello")
    assert typed == ["hello"]
    state["focused"] = None  # no focused control: no signal, typing proceeds
    windows.WindowsDriver().type_text("more")
    assert typed == ["hello", "more"]


# --------------------------------------------------------------------------- #
# 4. browser recheck: a tab switch between decision and injection
# --------------------------------------------------------------------------- #
def test_browser_recheck_aborts_a_click_after_a_tab_switch(tmp_path, monkeypatch) -> None:
    d, t = _driver_on()
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("TAB1", safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)
    rt.desktop_snapshot("TAB1")
    save = next(e for e in rt._current.elements if e.title == "Save")
    before = len(t.sent)
    # the bound tab changes underneath the gated action (another client switched it)
    monkeypatch.setattr(d, "frontmost_app", lambda: ("TAB2", None))
    with pytest.raises(ComputerUseError) as ei:
        rt.click(ref=save.ref)
    assert ei.value.code is ErrorCode.FOCUS_CHANGED
    assert ei.value.detail == {"gated_app": "TAB1", "frontmost_app": "TAB2"}
    injected = [m for m, _ in t.sent[before:] if m.startswith("Input.") or m == "Runtime.callFunctionOn"]
    assert injected == []  # the re-walk read the tree; nothing was injected
    assert _audit_rows(tmp_path / "audit")[-1]["result"] == "focus_changed"


# --------------------------------------------------------------------------- #
# 5. `window raise` through the driver seam
# --------------------------------------------------------------------------- #
def test_window_raise_gates_against_the_owner_and_raises_through_the_driver(tmp_path) -> None:
    rt, driver, audit_dir = _runtime(tmp_path)
    rt.store.set_tier("com.owner", safety.Tier.CLICK)
    assert rt.window("raise", 7) == "raised window 7 (com.owner)"
    assert driver.calls["raise"] == [7]
    row = _audit_rows(audit_dir)[-1]
    assert row["action"] == "windowop" and row["app"] == "com.owner" and row["result"] == "ok"


def test_window_raise_needs_a_grant_for_the_owner_not_the_frontmost_app(tmp_path) -> None:
    rt, driver, audit_dir = _runtime(tmp_path)  # APP holds FULL, com.owner holds nothing
    with pytest.raises(server.ActionRefused):
        rt.window("raise", 7)
    assert driver.calls["raise"] == []
    assert _audit_rows(audit_dir)[-1]["result"] == "needs_permission"
    with pytest.raises(ComputerUseError) as ei:
        rt.window("raise", 404)
    assert ei.value.code is ErrorCode.APP_NOT_FOUND


def test_browser_window_raise_is_a_structured_unsupported(tmp_path) -> None:
    d, _t = _driver_on()
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("TAB1", safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)
    with pytest.raises(ComputerUseError) as ei:
        rt.window("raise", 1)
    assert ei.value.code is ErrorCode.UNSUPPORTED and "app focus" in ei.value.detail["hint"]


def test_windows_window_verbs_are_structured_unsupported_not_crashes() -> None:
    d = windows.WindowsDriver()
    for call in (lambda: d.window_owner(1), lambda: d.raise_window(1)):
        with pytest.raises(ComputerUseError) as ei:
            call()
        assert ei.value.code is ErrorCode.UNSUPPORTED


class _FakeXWin:
    def __init__(self, wid: int):
        self.id = wid


@pytest.fixture
def fake_x11(monkeypatch):
    """A fake X display + EWMH modules for _linux_system window ops."""
    sent: list[tuple[object, int]] = []
    root = types.SimpleNamespace(send_event=lambda event, event_mask: sent.append((event, event_mask)))
    display = types.SimpleNamespace(screen=lambda: types.SimpleNamespace(root=root),
                                    intern_atom=lambda name: name, flush=lambda: None)
    monkeypatch.setattr(_linux_system, "_display", lambda: display)
    monkeypatch.setattr(_linux_system, "_managed_windows", lambda d: [_FakeXWin(7), _FakeXWin(9)])
    monkeypatch.setattr(_linux_system, "_pid_of", lambda win, d: {7: 42, 9: 0}[win.id])
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "gedit" if pid == 42 else None)

    messages: list[dict] = []

    class ClientMessage:
        def __init__(self, **kw):
            messages.append(kw)

    X = types.ModuleType("Xlib.X")
    X.CurrentTime, X.SubstructureRedirectMask, X.SubstructureNotifyMask = 0, 1 << 20, 1 << 19
    event = types.ModuleType("Xlib.protocol.event")
    event.ClientMessage = ClientMessage
    protocol = types.ModuleType("Xlib.protocol")
    protocol.event = event
    xlib = types.ModuleType("Xlib")
    xlib.X, xlib.protocol = X, protocol
    for name, mod in {"Xlib": xlib, "Xlib.X": X, "Xlib.protocol": protocol,
                      "Xlib.protocol.event": event}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    return sent, messages


def test_linux_window_owner_and_raise_by_x_window_id(fake_x11) -> None:
    sent, messages = fake_x11
    assert _linux_system.window_owner(7) == "gedit"
    assert _linux_system.window_owner(9) == ""  # a window whose pid is unreadable
    assert _linux_system.window_owner(8) is None
    assert _linux_system.raise_window(7) is True
    (event, mask), = sent
    assert messages[0]["client_type"] == "_NET_ACTIVE_WINDOW" and messages[0]["window"].id == 7
    assert messages[0]["data"] == (32, [1, 0, 0, 0, 0]) and mask == (1 << 20) | (1 << 19)
    assert _linux_system.raise_window(8) is False

    d = linux.LinuxDriver()
    assert d.window_owner(7) == "gedit"
    d.raise_window(7)
    assert len(sent) == 2
    with pytest.raises(ComputerUseError) as ei:
        d.window_owner(8)
    assert ei.value.code is ErrorCode.APP_NOT_FOUND
    with pytest.raises(ComputerUseError):
        d.raise_window(8)


def test_linux_window_verbs_are_unsupported_on_native_wayland(monkeypatch) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    d = linux.LinuxDriver()
    for call in (lambda: d.window_owner(7), lambda: d.raise_window(7)):
        with pytest.raises(ComputerUseError) as ei:
            call()
        assert ei.value.code is ErrorCode.UNSUPPORTED


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS driver imports pyobjc")
def test_macos_window_raise_activates_the_owning_app(monkeypatch) -> None:
    from computeruse.drivers.macos import MacOSDriver

    running = object()
    activated: list[object] = []
    monkeypatch.setattr(server, "_window_running", lambda wid: (running, "com.owner"))
    monkeypatch.setattr(server, "_activate", activated.append)
    d = MacOSDriver()
    assert d.window_owner(7) == "com.owner"
    d.raise_window(7)
    assert activated == [running]


# --------------------------------------------------------------------------- #
# 6. browser coordinate input: exact CDP payloads
# --------------------------------------------------------------------------- #
def _mouse_events(t: ScriptedTransport) -> list[dict]:
    return [p for m, p in t.sent if m == "Input.dispatchMouseEvent"]


def test_browser_click_payloads_button_count_and_modifiers() -> None:
    d, t = _driver_on()
    d.click(Point(0, 100, 200), button=MouseButton.RIGHT, count=2, modifiers=("shift",))
    events = _mouse_events(t)
    assert [e["type"] for e in events] == ["mousePressed", "mouseReleased"] * 2
    assert all(e["x"] == 100 and e["y"] == 200 and e["button"] == "right" for e in events)
    assert all(e["clickCount"] == 1 and e["modifiers"] == 8 for e in events)  # Shift = 8

    t.sent.clear()
    d.click(Point(0, 1, 2), modifiers=("cmd", "alt"))
    assert _mouse_events(t)[0]["modifiers"] == 4 | 1  # Meta | Alt
    assert _mouse_events(t)[0]["button"] == "left"


def test_browser_click_subtracts_the_scroll_offset() -> None:
    def responder(method, params):
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"pageX": 5, "pageY": 7},
                    "cssContentSize": {"width": 800, "height": 600}}
        return _fixture_responder(method, params)

    d, t = _driver_on(responder)
    d.click(Point(0, 105, 207))
    assert (_mouse_events(t)[0]["x"], _mouse_events(t)[0]["y"]) == (100, 200)


def test_browser_drag_payloads_press_move_release() -> None:
    d, t = _driver_on()
    d.drag(Point(0, 10, 20), Point(0, 300, 400), button=MouseButton.MIDDLE)
    events = _mouse_events(t)
    assert [(e["type"], e["x"], e["y"], e["button"]) for e in events] == [
        ("mousePressed", 10, 20, "middle"),
        ("mouseMoved", 300, 400, "middle"),
        ("mouseReleased", 300, 400, "middle"),
    ]
    assert events[0]["clickCount"] == 1 and events[2]["clickCount"] == 1


def test_browser_scroll_payloads_lines_and_pixels() -> None:
    d, t = _driver_on()
    d.scroll(Point(0, 10, 20), dx=1, dy=2, unit=ScrollUnit.LINES)
    wheel, = _mouse_events(t)
    # CDP wheel deltas share the tool's sign: positive dy scrolls the content
    # down (the earlier negation made every scroll-down at the top of a list a
    # no-op; cu-arena's long_list task caught it).
    assert wheel == {"type": "mouseWheel", "x": 10, "y": 20, "deltaX": 40, "deltaY": 80}
    t.sent.clear()
    d.scroll(Point(0, 10, 20), dx=1, dy=2, unit=ScrollUnit.PIXELS)
    assert _mouse_events(t)[0]["deltaX"] == 1 and _mouse_events(t)[0]["deltaY"] == 2


def test_browser_dry_run_and_pre_check_for_mouse_input() -> None:
    d, t = _driver_on()
    d.click(Point(0, 1, 1), dry_run=True)
    d.drag(Point(0, 1, 1), Point(0, 2, 2), dry_run=True)
    d.scroll(Point(0, 1, 1), dy=1, dry_run=True)
    assert _mouse_events(t) == []
    seen: list[str] = []
    d.click(Point(0, 1, 1), pre_check=lambda: seen.append("click"))
    assert seen == ["click"] and len(_mouse_events(t)) == 2


# --------------------------------------------------------------------------- #
# 7. wire-stable codes render exactly once, everywhere
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_code_renders_exactly_once(code: ErrorCode) -> None:
    text = server.error_text(ComputerUseError(code, "boom", detail={"k": 1}))
    assert text.startswith(f"{code.value}: boom")
    assert text.count(code.value) == 1
    assert "detail:" in text
    if code in server._PERMISSION_CODES:
        assert "doctor" in text  # the hint never repeats the code
    # a hint supplied by the driver replaces the doctor hint, still one code
    hinted = server.error_text(ComputerUseError(code, "boom", detail={"hint": "do X"}))
    assert hinted.count(code.value) == 1 and hinted.endswith("hint: do X")


@pytest.mark.parametrize("verdict", [safety.Verdict.NEEDS_PERMISSION, safety.Verdict.DENY])
def test_refusal_verdicts_render_exactly_once(verdict: safety.Verdict) -> None:
    decision = safety.Decision(verdict=verdict, app=APP, required=safety.Tier.CLICK,
                               granted=None, reason="grant tier 'click' for this app")
    text = server.refusal_text(decision)
    assert text.startswith(f"{verdict.value}: ") and text.count(verdict.value) == 1


@pytest.mark.parametrize("code", list(ErrorCode))
def test_driver_errors_reach_the_runtime_caller_with_one_code(tmp_path, code: ErrorCode) -> None:
    """Through the Runtime on a fake driver: the structured error keeps its code
    (no re-wrapping) and the audit row carries the same value."""
    rt, driver, audit_dir = _runtime(tmp_path)
    rt.desktop_snapshot(APP)

    def boom(target, **kw):
        raise ComputerUseError(code, "driver says no", detail={"k": 1})

    driver.click = boom
    with pytest.raises(ComputerUseError) as ei:
        rt.click(**INSIDE_SAVE)
    assert ei.value.code is code
    assert server.error_text(ei.value).count(code.value) == 1
    assert _audit_rows(audit_dir)[-1]["result"] == code.value


def test_act_batch_reports_each_code_once(tmp_path) -> None:
    rt, driver, _ = _runtime(tmp_path)
    rt.desktop_snapshot(APP)
    out = json.loads(rt.act_batch([{"do": "click", "ref": "e99"}]))
    assert out[0]["ok"] is False and out[0]["error"].count("stale_ref") == 1
    out = json.loads(rt.act_batch([{"do": "click", **INSIDE_PASSWORD}]))
    assert out[0]["error"].count("secure_field") == 1
    rt.store.revoke(APP)
    out = json.loads(rt.act_batch([{"do": "click", **INSIDE_SAVE}]))
    assert out[0]["error"].count("needs_permission") == 1
