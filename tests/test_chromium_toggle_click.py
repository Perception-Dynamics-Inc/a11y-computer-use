"""Scoped toggle read-back must prove a change without another window walk."""
from types import SimpleNamespace

import pytest

from a11y_computer_use import observe, outcome, server
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, Element, MouseButton, Point, Scope, Snapshot
from tests.test_linux_synthetic import _chrome_collapsed_panel


@pytest.fixture
def toggle_case(monkeypatch):
    frame, panel, node, text = _chrome_collapsed_panel()
    node.role = "check box"
    node.states.add("SHOWING")
    element = Element("e1", "AXCheckBox", node.name, None, Bounds(0, 22, 115, 28, 28),
                      "toggle-probe", clickable=True)
    observe._register_epoch(element.snapshot_id, {}, {element.ref: node})
    driver = LinuxDriver(visible_actions=False)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "_accessible_at_point", lambda x, y: node)
    return driver, element, node, frame


@pytest.mark.parametrize("role,bit", [("check box", "CHECKED"), ("radio button", "CHECKED"),
                                      ("toggle button", "PRESSED")])
@pytest.mark.parametrize("point", [False, True])
@pytest.mark.parametrize("initial", [False, True])
def test_toggle_probe_reads_both_directions(toggle_case, role, bit, point, initial):
    driver, element, node, frame = toggle_case
    node.role = role
    if initial:
        node.states.add(bit)
    target = Point(0, 36, 129) if point else element
    probe = driver.toggle_click_probe(target)
    assert probe is not None
    node.states.symmetric_difference_update({bit})
    assert driver.toggle_click_evidence(probe) == "the clicked toggle's checked/pressed state changed"


@pytest.mark.parametrize("case", ["unchanged", "missing", "error", "defunct", "other_node"])
def test_unreadable_or_unrelated_changes_do_not_confirm(toggle_case, case):
    driver, element, node, frame = toggle_case
    node.states.add("CHECKED")
    probe = driver.toggle_click_probe(element)
    if case == "missing":
        node.get_state_set = lambda: None
    elif case == "error":
        def fail():
            raise RuntimeError("bus disconnected")
        node.get_state_set = fail
    elif case == "defunct":
        node.states.update({"DEFUNCT", "CHECKED"})
    elif case == "other_node":
        frame.states.add("CHECKED")
    assert driver.toggle_click_evidence(probe) is None


@pytest.mark.parametrize("case", ["toolbar", "gtk", "button", "unreadable"])
def test_probe_rejects_non_page_toggles(toggle_case, case):
    driver, element, node, frame = toggle_case
    if case == "toolbar":
        node.parent = frame
    elif case == "gtk":
        node.application.toolkit = "GTK"
        node.application.name = "Editor"
    elif case == "button":
        node.role = "push button"
    else:
        node.get_state_set = lambda: None
    assert driver.toggle_click_probe(element) is None


def test_native_states_are_refreshed_and_failed_contains_is_unknown(toggle_case, monkeypatch):
    driver, element, node, frame = toggle_case
    flags = SimpleNamespace(CHECKED=1, PRESSED=2, DEFUNCT=3)
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(StateType=flags))
    reads = []
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda acc: reads.append(acc))
    node.get_state_set = lambda: SimpleNamespace(contains=lambda flag: flag == 1)
    probe = driver.toggle_click_probe(element)
    assert probe[1] == (True, False)
    def fail(flag):
        raise RuntimeError("unreadable")
    node.get_state_set = lambda: SimpleNamespace(contains=fail)
    assert driver.toggle_click_evidence(probe) is None
    assert reads[-1] is node
    assert reads.count(node) >= 2


@pytest.mark.parametrize("route", ["point", "pointer_ref", "native_ref"])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("dead", [False, True])
def test_runtime_skips_full_read_and_grace_only_after_toggle_change(
    toggle_case, monkeypatch, route, changed, dead,
):
    point = route == "point"
    driver, element, node, frame = toggle_case
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
            node.states.add("CHECKED")
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
    assert result.outcome == ("partial" if dead else "confirmed" if changed else "suspected_noop")
    if changed:
        assert rt._action_baseline["capture"] is None
    # A later click gets a fresh baseline, never the previous flip.
    assert driver.toggle_click_evidence(driver.toggle_click_probe(target)) is None


def test_point_hit_descends_into_cross_origin_document(toggle_case, monkeypatch):
    from tests.test_linux_synthetic import _GeckoNode

    driver, element, node, frame = toggle_case
    document = _GeckoNode("document web", "Cross-origin frame", ("SHOWING",), [node])
    document.application = node.application
    document.parent = frame.children[0]
    text = _GeckoNode("static", "Label")
    text.parent = node
    monkeypatch.setattr(_atspi, "_accessible_at_point", lambda x, y: text)
    probe = driver.toggle_click_probe(Point(0, 36, 129))
    assert probe[0] is node
    node.states.add("CHECKED")
    assert driver.toggle_click_evidence(probe) is not None


def test_state_settles_between_native_operations(toggle_case, monkeypatch):
    from a11y_computer_use.drivers import linux

    driver, element, node, frame = toggle_case
    probe = driver.toggle_click_probe(element)
    waits = []
    def settle(seconds):
        waits.append(seconds)
        node.states.add("CHECKED")
    monkeypatch.setattr(linux.time, "sleep", settle)
    assert driver.toggle_click_evidence(probe) is not None
    assert waits == [0.01]


@pytest.mark.parametrize("missing", [True, False])
def test_coordinate_hit_failure_keeps_normal_verification(toggle_case, monkeypatch, missing):
    driver, element, node, frame = toggle_case
    def hit(x, y):
        if missing:
            return None
        raise RuntimeError("hit testing unavailable")
    monkeypatch.setattr(_atspi, "_accessible_at_point", hit)
    assert driver.toggle_click_probe(Point(0, 36, 129)) is None
