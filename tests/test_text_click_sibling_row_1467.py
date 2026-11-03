"""A label's actionable ancestor must not be pressed at another row (#1467).

TodoMVC lists ``<li><label>Buy milk</label></li>`` inside a ``<ul>`` that is
actionable only through a delegated dblclick listener. Promoting the label to
the list pressed the list's center, which is the "Walk dog" row: the wrong
item entered edit mode and the click reported ``confirmed``.
"""
from __future__ import annotations

from a11y_computer_use.schema import Bounds, Element
from a11y_computer_use.server import text_click_control


def _el(ref, role, bounds, parent=None, clickable=False, title=""):
    return Element(ref=ref, role=role, title=title, value=None,
                   bounds=Bounds(0, *bounds), snapshot_id="s1", parent=parent,
                   clickable=clickable)


def _todo_list():
    lst = _el("e33", "AXList", (300, 260, 680, 214), clickable=True)
    rows = [
        _el("e34", "AXListItem", (300, 260, 680, 50), parent="e33"),
        _el("e40", "AXListItem", (300, 310, 680, 50), parent="e33"),
        _el("e46", "AXListItem", (300, 360, 680, 50), parent="e33"),
    ]
    label = _el("e38", "AXStaticText", (424, 286, 84, 25), parent="e34", title="Buy milk")
    other = _el("e44", "AXStaticText", (424, 336, 84, 25), parent="e40", title="Walk dog")
    return [lst, *rows, label, other], label


def test_label_is_not_promoted_to_a_list_whose_center_is_another_row():
    elements, label = _todo_list()
    # The list's center (640,367) is inside the "Walk dog" row, not the label.
    assert text_click_control(elements, label) is None


def test_a_button_whose_center_is_on_its_label_still_stands_for_it():
    button = _el("e2", "AXButton", (100, 100, 120, 30), clickable=True, title="Save")
    span = _el("e3", "AXStaticText", (110, 105, 100, 20), parent="e2", title="Save")
    assert text_click_control([button, span], span) is button


# --- confirmation guard: a change in another row is not the target's ------

from types import SimpleNamespace

import pytest

from a11y_computer_use import outcome
from a11y_computer_use.schema import ComputerUseError


def _rows(editing: str | None):
    """A TodoMVC list; ``editing`` names the row that opened its editor."""
    els = [_el("e33", "AXList", (300, 260, 680, 150), clickable=True)]
    for i, name in enumerate(("Buy milk", "Walk dog", "Pay rent")):
        row, y = f"r{i}", 260 + 50 * i
        els.append(_el(row, "AXListItem", (300, y, 680, 50), parent="e33"))
        els.append(_el(f"t{i}", "AXStaticText", (424, y + 26, 84, 25), parent=row, title=name))
        if editing == name:
            els.append(_el(f"x{i}", "AXTextField", (300, y, 680, 50), parent=row, title=""))
    return SimpleNamespace(elements=tuple(els))


def test_right_target_but_the_change_lands_in_another_row_is_wrong_target():
    before, after = _rows(None), _rows("Walk dog")
    label = next(el for el in before.elements if el.title == "Buy milk")
    evidence = outcome.wrong_row_effect(before, after, label)
    assert evidence and evidence.startswith("wrong_target")
    assert "'Walk dog'" in evidence and "'Buy milk'" in evidence
    assert not any(ch.isdigit() for ch in evidence)  # no coordinates


def test_a_change_in_the_targets_own_row_still_confirms():
    before, after = _rows(None), _rows("Buy milk")
    label = next(el for el in before.elements if el.title == "Buy milk")
    assert outcome.wrong_row_effect(before, after, label) is None


def test_double_click_whose_settled_hit_is_another_row_refuses_before_input(monkeypatch):
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    driver._run = lambda fn: fn()  # type: ignore[method-assign]
    monkeypatch.setattr(driver, "_refuse_chromium_occluded", lambda *a, **k: None)
    # Chromium's first answer is the box-only guess (the label); the cached
    # real hit, which both clicks would land on, is the "Walk dog" row.
    answers = iter([True, False])
    monkeypatch.setattr(_atspi, "chromium_pointer_target", lambda h, x, y: next(answers))
    _, label = _todo_list()
    driver._verify_chromium_pointer_target(label, object(), 466, 298)
    with pytest.raises(ComputerUseError) as raised:
        driver._verify_double_click_target(label, object(), 466, 298)
    detail = raised.value.detail
    assert detail["reason"] == "wrong_target" and detail["outcome"] == "refused"
    assert "No input was sent" in raised.value.message
    assert "466" not in raised.value.message and "coordinates" in detail["hint"]


def test_double_click_on_a_settled_hit_proceeds(monkeypatch):
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    driver._run = lambda fn: fn()  # type: ignore[method-assign]
    monkeypatch.setattr(_atspi, "chromium_pointer_target", lambda h, x, y: True)
    _, label = _todo_list()
    driver._verify_double_click_target(label, object(), 466, 298)


def test_click_runs_the_double_click_check_before_input():
    import inspect
    from a11y_computer_use.drivers.linux import LinuxDriver

    src = inspect.getsource(LinuxDriver.click)
    assert src.index("_verify_double_click_target") < src.index("_linux_input.click(")


def _judge_click(monkeypatch, before, after, label):
    from a11y_computer_use.server import Runtime

    rt = Runtime.__new__(Runtime)
    rt.driver = SimpleNamespace(name="fake")
    monkeypatch.setattr(rt, "_reread", lambda *_a: after, raising=False)
    monkeypatch.setattr(rt, "_process_died", lambda _b: False, raising=False)
    monkeypatch.setattr(rt, "_remember_baseline", lambda _s: None, raising=False)
    return rt._judge_mutation("chrome", {"snap": before, "scope": None}, label, None)


def test_a_click_that_edits_another_row_is_never_confirmed(monkeypatch):
    before, after = _rows(None), _rows("Walk dog")
    label = next(el for el in before.elements if el.title == "Buy milk")
    verdict, evidence = _judge_click(monkeypatch, before, after, label)
    assert verdict == "unverifiable" and evidence.startswith("wrong_target")


def test_a_click_that_edits_its_own_row_is_confirmed(monkeypatch):
    before, after = _rows(None), _rows("Buy milk")
    label = next(el for el in before.elements if el.title == "Buy milk")
    assert _judge_click(monkeypatch, before, after, label)[0] == "confirmed"


def _live_rows(snap, editing):
    from dataclasses import replace as _replace
    return _replace(snap, elements=tuple(
        _replace(el, snapshot_id=snap.snapshot_id) for el in _rows(editing).elements
    ))


@pytest.mark.parametrize("selection_after", [None, ()])
def test_double_click_on_text_whose_change_lands_in_another_row_says_wrong_target(
    tmp_path, monkeypatch, selection_after,
):
    """Live #1467: a double-click on page text is judged by its selection, which
    bypassed the row guard; the wrong-row edit came back as a bare unverifiable."""
    from a11y_computer_use import safety, server
    from tests.conftest import build_synthetic_snapshot
    from tests.test_server import _HoverDriver, _hover_runtime

    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "editor")
    monkeypatch.setattr(server, "_app_at_point", lambda p: None)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    d = _HoverDriver()
    d.selection_details = lambda el: () if selection_after == () else None
    rt = _hover_runtime(tmp_path, d)
    base = build_synthetic_snapshot(app="editor")
    before = _live_rows(base, None)
    rt._current = before
    monkeypatch.setattr(rt, "_reread", lambda *_a, **_k: _live_rows(base, "Walk dog"))
    label = next(el for el in before.elements if el.title == "Buy milk")
    result = rt.click(ref=label.ref, count=2)
    assert result.outcome == "unverifiable"
    assert result.evidence.startswith("wrong_target")
    assert "Walk dog" in result.evidence and "Buy milk" in result.evidence
    assert not result.next


def _renumbered(snap, prefix):
    """The same page read again: every ref (and parent ref) is new."""
    from dataclasses import is_dataclass, replace as _replace
    elements = tuple(
        _replace(el, ref=prefix + el.ref, snapshot_id=prefix,
                 parent=(prefix + el.parent) if el.parent else None)
        for el in snap.elements
    )
    if is_dataclass(snap):
        return _replace(snap, snapshot_id=prefix, elements=elements)
    return SimpleNamespace(snapshot_id=prefix, elements=elements)


@pytest.mark.parametrize("selection_after", [None, ()])
def test_wrong_row_is_found_when_the_click_resolved_against_a_fresh_tree(
    tmp_path, monkeypatch, selection_after,
):
    """Tester's #1475 gate: the before-state is the full tree that resolved
    the ``find`` ref, whose refs differ from the issuing snapshot's. The row
    guard located the target by its issuing ref there, found no row, and the
    live click came back as a bare unverifiable."""
    from a11y_computer_use import safety, server
    from tests.conftest import build_synthetic_snapshot
    from tests.test_server import _HoverDriver, _hover_runtime

    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "editor")
    monkeypatch.setattr(server, "_app_at_point", lambda p: None)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    d = _HoverDriver()
    d.selection_details = lambda el: () if selection_after == () else None
    rt = _hover_runtime(tmp_path, d)
    base = build_synthetic_snapshot(app="editor")
    issuing = _live_rows(base, None)
    resolved = _renumbered(issuing, "z")
    d.take_click_snapshot = lambda el: resolved
    rt._current = issuing
    monkeypatch.setattr(rt, "_reread", lambda *_a, **_k: _renumbered(_live_rows(base, "Walk dog"), "y"))
    label = next(el for el in issuing.elements if el.title == "Buy milk")
    result = rt.click(ref=label.ref, count=2)
    assert result.outcome == "unverifiable"
    assert result.evidence.startswith("wrong_target")
    assert "Walk dog" in result.evidence and "Buy milk" in result.evidence


def test_row_guard_anchors_the_target_in_a_renumbered_before_tree():
    before = _renumbered(SimpleNamespace(snapshot_id="s1", elements=_rows(None).elements), "z")
    after = _renumbered(SimpleNamespace(snapshot_id="s1", elements=_rows("Walk dog").elements), "y")
    label = next(el for el in _rows(None).elements if el.title == "Buy milk")
    evidence = outcome.wrong_row_effect(before, after, label)
    assert evidence and "'Walk dog'" in evidence
    own = _renumbered(SimpleNamespace(snapshot_id="s1", elements=_rows("Buy milk").elements), "y")
    assert outcome.wrong_row_effect(before, own, label) is None


def _debug_click(tmp_path, monkeypatch, editing_after, flag):
    from a11y_computer_use import safety, server
    from tests.conftest import build_synthetic_snapshot
    from tests.test_server import _HoverDriver, _hover_runtime

    if flag:
        monkeypatch.setenv("A11Y_DEBUG_ROW_GUARD", "1")
    else:
        monkeypatch.delenv("A11Y_DEBUG_ROW_GUARD", raising=False)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "editor")
    monkeypatch.setattr(server, "_app_at_point", lambda p: None)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    d = _HoverDriver()
    d.selection_details = lambda el: ()
    rt = _hover_runtime(tmp_path, d)
    base = build_synthetic_snapshot(app="editor")
    issuing = _live_rows(base, None)
    d.take_click_snapshot = lambda el: _renumbered(issuing, "z")
    rt._current = issuing
    monkeypatch.setattr(
        rt, "_reread", lambda *_a, **_k: _renumbered(_live_rows(base, editing_after), "y"))
    label = next(el for el in issuing.elements if el.title == "Buy milk")
    return rt.click(ref=label.ref, count=2)


def _debug_payload(evidence):
    import json
    assert "row_guard_debug=" in evidence
    return json.JSONDecoder().raw_decode(evidence.split("row_guard_debug=", 1)[1])[0]


def test_row_guard_debug_trace_records_the_path_when_flagged(tmp_path, monkeypatch, capsys):
    result = _debug_click(tmp_path, monkeypatch, "Walk dog", flag=True)
    assert result.evidence.startswith("wrong_target")
    data = _debug_payload(result.evidence)
    assert data["guard_ran"] and data["wrong_row_effect_called"] and data["reread"]
    assert data["selection_branch"] == "suspected_noop"
    trace = data["trace"]
    assert trace["exit"] == "wrong_target"
    assert "object" in trace["before_lookup"] and "control_match" in trace["before_lookup"]
    assert len(trace["before_rows"]) == len(trace["after_rows"]) == 3
    assert "row_guard_debug:" in capsys.readouterr().err


def test_row_guard_debug_trace_names_the_early_exit(tmp_path, monkeypatch):
    result = _debug_click(tmp_path, monkeypatch, None, flag=True)
    assert _debug_payload(result.evidence)["trace"]["exit"] == "no_other_row_changed"


def test_row_guard_debug_trace_is_off_by_default(tmp_path, monkeypatch, capsys):
    result = _debug_click(tmp_path, monkeypatch, "Walk dog", flag=False)
    assert "row_guard_debug" not in result.evidence
    assert "row_guard_debug" not in capsys.readouterr().err
