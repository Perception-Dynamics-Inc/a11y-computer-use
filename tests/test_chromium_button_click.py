"""Chromium transform-only moves must not authorize pointer clicks on page buttons."""
from types import SimpleNamespace

import pytest

from a11y_computer_use import observe, server
from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, MouseButton, Scope, Snapshot
from tests.test_linux_synthetic import _chrome_collapsed_panel


@pytest.fixture
def button_case(monkeypatch):
    frame, panel, node, _text = _chrome_collapsed_panel()
    node.states.add("SHOWING")
    element = Element("e1", "AXButton", "Item A", None, Bounds(0, 22, 115, 280, 40),
                      "button-action", clickable=True)
    observe._register_epoch(element.snapshot_id, {}, {element.ref: node})
    driver = LinuxDriver(visible_actions=False)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_raise_handle_window", lambda *a: None)
    monkeypatch.setattr(driver, "_visible_action", lambda *a: None)
    # The hit test lies exactly as live Chromium does; using it is a failure.
    monkeypatch.setattr(_atspi, "chromium_pointer_target",
                        lambda *a: pytest.fail("AT-SPI hit test cannot establish freshness"))
    monkeypatch.setattr(_linux_input, "click", lambda *a, **kw: pytest.fail("pointer sent"))
    calls = []
    names = ["click"]
    action = SimpleNamespace(get_n_actions=lambda: len(names), get_action_name=lambda i: names[i],
                             do_action=lambda i: calls.append(names[i]) or True)
    node.get_action_iface = lambda: action
    return driver, element, node, frame, action, names, calls


@pytest.mark.parametrize("name", ["click", "press"])
@pytest.mark.parametrize("role", ["button", "push button"])
def test_page_button_uses_exact_node_action(button_case, name, role):
    driver, element, node, frame, action, names, calls = button_case
    node.role = role
    driver._focused_editable = object()
    names[:] = ["showmenu", name, "toggle"]
    result = driver.click_button_action(element, button=MouseButton.LEFT, count=1, modifiers=())
    assert result == f"AT-SPI doAction({name})"
    assert calls == [name]
    assert driver._focused_editable is None


@pytest.mark.parametrize("names", [[], ["toggle"], ["activate"], ["showmenu"], ["grabfocus"]])
def test_missing_click_action_refuses_without_substitution(button_case, names):
    driver, element, node, frame, action, advertised, calls = button_case
    advertised[:] = names
    with pytest.raises(ComputerUseError) as exc:
        driver.click_button_action(element, button=MouseButton.LEFT, count=1, modifiers=())
    assert exc.value.detail["reason"] == "chromium_button_action_unavailable"
    assert exc.value.detail["outcome"] == "refused"
    assert calls == []


def test_failed_action_is_not_retried_or_sent_to_pointer(button_case):
    driver, element, node, frame, action, names, calls = button_case
    names[:] = ["click", "press"]
    action.do_action = lambda i: calls.append(names[i]) or False
    with pytest.raises(ComputerUseError):
        driver.click_button_action(element, button=MouseButton.LEFT, count=1, modifiers=())
    assert calls == ["click"]


@pytest.mark.parametrize("kwargs", [
    {"button": MouseButton.RIGHT}, {"button": MouseButton.MIDDLE}, {"count": 2},
    {"count": 3}, {"modifiers": ("shift",)}, {"allow_action": False},
])
def test_pointer_specific_requests_are_not_rewritten(button_case, kwargs):
    driver, element, node, frame, action, names, calls = button_case
    params = dict(button=MouseButton.LEFT, count=1, modifiers=())
    params.update(kwargs)
    with pytest.raises(ComputerUseError) as exc:
        driver.click_button_action(element, **params)
    assert exc.value.detail["outcome"] == "refused"
    assert not calls


@pytest.mark.parametrize("kind", ["toolbar", "gtk", "missing", "link"])
def test_other_targets_keep_existing_route(button_case, kind):
    driver, element, node, frame, action, names, calls = button_case
    if kind == "toolbar":
        node.parent = frame
    elif kind == "gtk":
        node.application.toolkit = "GTK"
        node.application.name = "Editor"
    elif kind == "missing":
        observe._register_epoch(element.snapshot_id, {}, {})
    else:
        node.role = "link"
    assert driver.click_button_action(element, button=MouseButton.LEFT, count=1, modifiers=()) is None
    assert not calls


@pytest.mark.parametrize("verdict", ["confirmed", "suspected_noop"])
def test_runtime_reports_mechanism_but_still_checks_effect(button_case, monkeypatch, verdict):
    driver, element, node, frame, action, names, calls = button_case
    snap = Snapshot(element.snapshot_id, Scope.WINDOW, "chrome", 0, 0.0, (), (element,))
    rt = server.Runtime.__new__(server.Runtime)
    rt._current = snap
    rt.driver = driver
    rt._target = lambda *a, **kw: (element, "chrome")
    rt._capture = lambda: {}
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._resolves_apps = lambda: False
    rt._dismiss_open_menu = lambda app: ""
    for name in ("_reject_coordinate", "_refuse_disabled", "_refuse_secure", "_recheck_target",
                 "_guard_user", "_popover_cover", "_refuse_vanished_target", "_cover_owner",
                 "_paragraph_click_verdict", "_opaque_click_verdict", "_navigating_row_location"):
        setattr(rt, name, lambda *a, **kw: None)
    rt._canvas_fallback_target = lambda element: False
    rt._effect_after = lambda pre: ""
    monkeypatch.setattr(driver, "same_app_window_cover", lambda *a: None)
    rt._judge_mutation = lambda *a, **kw: (verdict, "observed effect" if verdict == "confirmed" else "unchanged")
    monkeypatch.setattr(server, "PREFER_AX_ACTIONS", True)
    result = rt.click(ref="e1")
    assert "via AT-SPI doAction(click)" in result
    assert result.outcome == verdict
    assert calls == ["click"]


@pytest.mark.parametrize("kwargs", [
    {}, {"button": MouseButton.RIGHT}, {"count": 2},
    {"modifiers": ("shift",)}, {"allow_action": False},
])
def test_native_toggle_keeps_existing_click_route(button_case, kwargs):
    driver, element, node, frame, action, names, calls = button_case
    node.role = "toggle button"
    names[:] = ["toggle"]
    assert _atspi._ROLE[node.role] == element.role == "AXButton"
    params = dict(button=MouseButton.LEFT, count=1, modifiers=())
    params.update(kwargs)
    assert driver.click_button_action(element, **params) is None
    assert calls == []  # Do not substitute the advertised toggle for a click.


@pytest.mark.parametrize("changed", [True, False])
def test_ref_click_counts_full_window_reads(button_case, monkeypatch, changed):
    """Real resolve/click/judge paths; only native transport and guards are fake."""
    from dataclasses import replace

    driver, element, node, frame, action, names, calls = button_case
    snap = Snapshot(element.snapshot_id, Scope.WINDOW, "chrome", 0, 0.0, (), (element,))
    reads = []
    node.name = element.title

    def snapshot(scope, app):
        reads.append((scope, app))
        return replace(snap, elements=(replace(element, title=node.name),))

    def send(index):
        calls.append(names[index])
        if changed:
            node.name = "Count 1"
        return True

    action.do_action = send
    monkeypatch.setattr(driver, "snapshot", snapshot)
    rt = server.Runtime.__new__(server.Runtime)
    rt._current = snap
    rt.driver = driver
    rt._target = lambda *a, **kw: (driver.resolve_ref(snap, "e1"), "chrome")
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._resolves_apps = lambda: False
    rt._dismiss_open_menu = lambda app: ""
    guards = []
    for name in ("_reject_coordinate", "_refuse_disabled", "_refuse_secure", "_recheck_target",
                 "_guard_user", "_popover_cover", "_refuse_vanished_target", "_cover_owner",
                 "_paragraph_click_verdict", "_opaque_click_verdict", "_navigating_row_location"):
        setattr(rt, name, lambda *a, _name=name, **kw: guards.append(_name))
    rt._canvas_fallback_target = lambda element: False
    rt._effect_after = lambda pre: ""
    monkeypatch.setattr(driver, "same_app_window_cover", lambda *a: None)
    monkeypatch.setattr(server, "PREFER_AX_ACTIONS", True)
    result = rt.click(ref="e1")
    assert len(reads) == (1 if changed else 2)
    assert result.outcome == ("confirmed" if changed else "suspected_noop")
    assert calls == ["click"]
    assert "_recheck_target" in guards
    assert "_popover_cover" in guards
    assert "_refuse_vanished_target" in guards
    if changed:
        assert result.evidence == "the clicked button's accessible name changed"
        assert rt._action_baseline["capture"] is None
        assert driver.button_click_evidence(element) is None


@pytest.mark.parametrize("case", ["unchanged", "missing_before", "missing_after", "defunct", "foreign", "changed"])
def test_button_evidence_requires_fresh_bound_name_change(button_case, monkeypatch, case):
    from dataclasses import replace

    driver, element, node, frame, action, names, calls = button_case
    node.name = "Count 0"
    if case == "missing_before":
        node.get_name = lambda: None
    driver.click_button_action(element, button=MouseButton.LEFT, count=1, modifiers=())
    node.get_name = lambda: "Count 0" if case == "unchanged" else "Count 1"
    if case == "missing_after":
        node.get_name = lambda: None
    if case == "defunct":
        node.states.add("DEFUNCT")
    target = replace(element) if case == "foreign" else element
    evidence = driver.button_click_evidence(target)
    assert (evidence is not None) == (case == "changed")
    assert driver.button_click_evidence(element) is None


def test_resolve_snapshot_is_one_use_and_target_bound(button_case, monkeypatch):
    from dataclasses import replace

    driver, element, node, frame, action, names, calls = button_case
    snap = Snapshot(element.snapshot_id, Scope.WINDOW, "chrome", 0, 0.0, (), (element,))
    monkeypatch.setattr(driver, "snapshot", lambda *a: snap)
    resolved = driver.resolve_ref(snap, element.ref)
    assert driver.take_click_snapshot(replace(resolved)) is None
    assert driver.take_click_snapshot(resolved) is None
    resolved = driver.resolve_ref(snap, element.ref)
    assert driver.take_click_snapshot(resolved) is snap
    assert driver.take_click_snapshot(resolved) is None


def test_scoped_click_evidence_preserves_process_exit(button_case, monkeypatch):
    driver, element, node, frame, action, names, calls = button_case
    node.name = "Count 0"
    driver.click_button_action(element, button=MouseButton.LEFT, count=1, modifiers=())
    node.name = "Count 1"
    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = driver
    rt._current = None
    monkeypatch.setattr(rt, "_process_died", lambda before: True)
    monkeypatch.setattr(rt, "_reread", lambda *a: pytest.fail("unnecessary full read"))
    verdict, evidence = rt._judge_mutation("chrome", None, element, None, cover_click=True)
    assert verdict == "partial"
    assert "process exited" in evidence
