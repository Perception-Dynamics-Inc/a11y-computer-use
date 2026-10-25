"""GTK dialog clicks use fresh target state, preserving full fallback checks."""
from types import SimpleNamespace

import pytest

from a11y_computer_use import observe, outcome, server
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, Element, MouseButton, Point, Scope, Snapshot
from tests.test_linux_synthetic import _GeckoNode


@pytest.fixture(params=["page tab", "radio button", "check box"])
def gtk_toggle_case(monkeypatch, request):
    node = _GeckoNode(request.param, "Page Setup", ("SHOWING",))
    frame = _GeckoNode("dialog", "Print", ("SHOWING",), [node])
    app = _GeckoNode("application", "Mousepad")
    app.toolkit = "gtk"
    node.application = frame.application = app
    node.parent = frame
    frame.parent = app
    element = Element("e1", "AXButton", node.name, None, Bounds(0, 22, 115, 28, 28),
                      "gtk-toggle-probe", clickable=True)
    observe._register_epoch(element.snapshot_id, {}, {element.ref: node})
    driver = LinuxDriver(visible_actions=False)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "_accessible_at_point", lambda x, y: node)
    return driver, element, node, frame


@pytest.mark.parametrize("failure", ["missing", "error", "defunct", "hidden", "unchanged", "other"])
def test_failed_or_unrelated_read_does_not_confirm(gtk_toggle_case, failure):
    driver, element, node, frame = gtk_toggle_case
    node.states.update({"CHECKED", "SELECTED"})
    probe = driver.toggle_click_probe(element)
    assert probe is not None
    if failure == "missing":
        node.get_state_set = lambda: None
    elif failure == "error":
        def fail():
            raise RuntimeError("disconnected")
        node.get_state_set = fail
    elif failure == "defunct":
        node.states.add("DEFUNCT")
    elif failure == "hidden":
        node.states.discard("SHOWING")
        node.states.discard("CHECKED")
        node.states.discard("SELECTED")
    elif failure == "other":
        frame.states.add("SELECTED")
    assert driver.toggle_click_evidence(probe) is None


@pytest.mark.parametrize("case", ["frame", "web", "button", "qt"])
def test_probe_requires_gtk_dialog_control(gtk_toggle_case, case):
    driver, element, node, frame = gtk_toggle_case
    if case == "frame":
        frame.role = "frame"
    elif case == "web":
        frame.role = "document web"
    elif case == "button":
        node.role = "push button"
    else:
        node.application.toolkit = "Qt"
    assert driver.toggle_click_probe(element) is None


def test_native_selected_state_refresh_and_uncheck(gtk_toggle_case, monkeypatch):
    driver, element, node, frame = gtk_toggle_case
    flags = SimpleNamespace(CHECKED=1, PRESSED=2, SELECTED=3, SHOWING=4, DEFUNCT=5)
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(StateType=flags))
    reads = []
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda acc: reads.append(acc))
    node.get_state_set = lambda: SimpleNamespace(contains=lambda flag: flag in {1, 3, 4})
    probe = driver.toggle_click_probe(element)
    assert probe is not None
    node.get_state_set = lambda: SimpleNamespace(contains=lambda flag: flag == 4)
    assert driver.toggle_click_evidence(probe) is not None
    assert reads.count(node) >= 2

@pytest.mark.parametrize("route", ["point", "pointer_ref", "native_ref"])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("dead", [False, True])
def test_runtime_skips_full_read_and_grace_only_after_toggle_change(
    gtk_toggle_case, monkeypatch, route, changed, dead,
):
    bit = "SELECTED" if gtk_toggle_case[2].role == "page tab" else "CHECKED"
    point = route == "point"
    driver, element, node, frame = gtk_toggle_case
    snap = Snapshot(element.snapshot_id, Scope.WINDOW, "chrome", 123, 0.0, (), (element,))
    reads, clicks, grace = [], [], []
    def snapshot(scope, app):
        reads.append((scope, app))
        return snap
    monkeypatch.setattr(driver, "snapshot", snapshot)
    target = Point(0, 36, 129) if point else element
    def click(sent, **kwargs):
        clicks.append((sent, kwargs))
        if changed:
            node.states.add(bit)
    monkeypatch.setattr(driver, "click", click)
    monkeypatch.setattr(driver, "press_element", lambda target: click(
        target, button=MouseButton.LEFT, count=1, modifiers=(),
    ) or True)
    rt = server.Runtime.__new__(server.Runtime)
    rt._current = snap
    rt.driver = driver
    rt._target = lambda *a, **kw: (target if point else driver.resolve_ref(snap, "e1"), "chrome")
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._resolves_apps = lambda: False
    rt._dismiss_open_menu = lambda app: ""
    rt._capture = lambda: rt._freeze(snap)
    for name in ("_reject_coordinate", "_refuse_disabled", "_refuse_secure", "_recheck_target",
                 "_guard_user", "_popover_cover", "_refuse_vanished_target", "_cover_owner",
                 "_paragraph_click_verdict", "_opaque_click_verdict", "_navigating_row_location"):
        setattr(rt, name, lambda *a, **kw: None)
    rt._canvas_fallback_target = lambda element: False
    rt._effect_after = lambda pre: ""
    monkeypatch.setattr(driver, "same_app_window_cover", lambda *a: None)
    monkeypatch.setattr(server, "PREFER_AX_ACTIONS", route == "native_ref")
    liveness = iter([True, not dead])
    monkeypatch.setattr(outcome, "pid_alive", lambda pid: next(liveness))
    monkeypatch.setattr(outcome, "wait_until_dead", lambda *a: grace.append(a) or dead)
    result = rt.click(x=36, y=129) if point else rt.click(ref="e1")
    assert len(reads) == (0 if point else 1) + (0 if changed else 1)
    assert len(grace) == (0 if changed else 1)
    assert len(clicks) == 1
    assert clicks[0][1] == dict(button=MouseButton.LEFT, count=1, modifiers=())
    assert result.outcome == ("partial" if dead else "confirmed" if changed else "unverifiable")
    if not changed and not dead:
        assert result.next == ()
        assert "re-observe first" in result.evidence
    if changed:
        assert rt._action_baseline["capture"] is None
    # A later click gets a fresh baseline, never the previous flip.
    assert driver.toggle_click_evidence(driver.toggle_click_probe(target)) is None

