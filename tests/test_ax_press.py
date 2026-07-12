"""AX-activation path: press elements through the accessibility API so the
agent never moves the user's physical cursor (the payoff of being a11y-first).

Covers `observe.build_snapshot` retaining live handles per epoch,
`observe.ax_handle_for` lookup + FIFO eviction, and `observe.press_element`'s
action selection, secure-field refusal, editable-focus fallback, and
missing-handle fallback. The three AX FFI shims are monkeypatched so these run
with no TCC grant.
"""

from __future__ import annotations

import pytest

from computeruse import observe
from computeruse.observe import ax_handle_for, build_snapshot, press_element
from computeruse.schema import Bounds, Element, Scope
from tests.fixtures.trees import GEOMETRY, DictAccessor, ax, button


def _snap(root: dict):
    """Build a WINDOW snapshot over ``root`` (the dict node doubles as handle)."""
    return build_snapshot(
        root, DictAccessor(), scope=Scope.WINDOW, app="com.test", pid=1, geometry=GEOMETRY
    )


def _window(*children: dict) -> dict:
    return ax("AXWindow", title="W", at=(100.0, 50.0), size=(400.0, 300.0), children=list(children))


# --- handle registry ---------------------------------------------------------


def test_build_snapshot_retains_handle_per_ref() -> None:
    alpha = button("Alpha", (110.0, 60.0))
    beta = button("Beta", (210.0, 60.0))
    root = _window(alpha, beta)
    snap = _snap(root)

    # Pre-order refs: e1 window, e2 Alpha, e3 Beta — each maps to its live node.
    assert ax_handle_for(snap.snapshot_id, "e1") is root
    assert ax_handle_for(snap.snapshot_id, "e2") is alpha
    assert ax_handle_for(snap.snapshot_id, "e3") is beta


def test_ax_handle_for_unknown_ref_or_epoch_is_none() -> None:
    snap = _snap(_window(button("Alpha", (110.0, 60.0))))
    assert ax_handle_for(snap.snapshot_id, "e99") is None
    assert ax_handle_for("snap-does-not-exist", "e1") is None


def test_collapsed_wrapper_keeps_the_childs_handle() -> None:
    # A single-child AXGroup wrapper collapses; the surviving ref must carry the
    # inner button's handle, not the discarded wrapper's.
    inner = button("Deep", (120.0, 120.0))
    wrapper = ax("AXGroup", at=(110.0, 110.0), size=(200.0, 100.0), children=[inner])
    snap = _snap(_window(wrapper))
    # e1 window, e2 is the collapsed button.
    el = snap.element("e2")
    assert el.role == "AXButton"
    assert ax_handle_for(snap.snapshot_id, el.ref) is inner


def test_handle_registry_evicts_old_epochs() -> None:
    root = _window(button("Alpha", (110.0, 60.0)))
    first = _snap(root)
    assert ax_handle_for(first.snapshot_id, "e2") is not None
    for _ in range(observe._MAX_EPOCHS + 1):
        _snap(root)
    assert ax_handle_for(first.snapshot_id, "e2") is None


# --- press_element -----------------------------------------------------------


def test_press_element_performs_highest_priority_action(monkeypatch: pytest.MonkeyPatch) -> None:
    alpha = button("Alpha", (110.0, 60.0))  # actions=("AXPress",)
    snap = _snap(_window(alpha))
    el = snap.element("e2")
    performed: list[tuple[object, str]] = []
    # Element exposes both AXConfirm and AXPress; AXPress wins on priority.
    monkeypatch.setattr(observe, "_copy_action_names", lambda h: ("AXConfirm", "AXPress"))
    monkeypatch.setattr(observe, "_perform_action", lambda h, a: performed.append((h, a)) or True)

    assert press_element(el) is True
    assert performed == [(alpha, "AXPress")]


def test_press_element_reports_ax_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _snap(_window(button("Alpha", (110.0, 60.0))))
    el = snap.element("e2")
    monkeypatch.setattr(observe, "_copy_action_names", lambda h: ("AXPress",))
    monkeypatch.setattr(observe, "_perform_action", lambda h, a: False)  # AX returned an error
    assert press_element(el) is False  # caller falls back to a synthetic click


def test_press_element_focuses_editable_without_press_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = ax("AXTextField", title="Name", at=(110.0, 60.0), size=(200.0, 30.0))
    snap = _snap(_window(field))
    el = snap.element("e2")
    assert el.editable and not el.secure
    focused: list[object] = []
    monkeypatch.setattr(observe, "_copy_action_names", lambda h: ())  # no activate action
    monkeypatch.setattr(observe, "_set_focused", lambda h: focused.append(h) or True)

    assert press_element(el) is True
    assert focused == [field]


def test_press_element_refuses_secure_field_without_touching_ax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure = ax(
        "AXSecureTextField",
        title="Password",
        at=(110.0, 60.0),
        size=(200.0, 30.0),
        actions=("AXConfirm",),  # even though it advertises an action
    )
    snap = _snap(_window(secure))
    el = snap.element("e2")
    assert el.secure
    queried: list[object] = []
    monkeypatch.setattr(observe, "_copy_action_names", lambda h: queried.append(h) or ("AXConfirm",))

    assert press_element(el) is False  # human handoff; the click path raises SECURE_FIELD
    assert queried == []  # short-circuits before any AX query


def test_press_element_without_handle_falls_back() -> None:
    orphan = Element(
        ref="e1",
        role="AXButton",
        title="Nowhere",
        value=None,
        bounds=Bounds(1, 0, 0, 10, 10),
        snapshot_id="snap-never-registered",
    )
    assert press_element(orphan) is False


def test_press_element_non_editable_without_action_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plain static-text-ish node with a handle but no activate action and not
    # editable: nothing to do via AX, so fall back.
    node = ax("AXRow", title="Row", at=(110.0, 60.0), size=(200.0, 30.0))
    snap = _snap(_window(node))
    el = snap.element("e2")
    assert not el.editable
    monkeypatch.setattr(observe, "_copy_action_names", lambda h: ())
    assert press_element(el) is False


# --- scroll_into_view (AXScrollToVisible, the cursor-free scroll) -------------


def test_scroll_into_view_performs_ax_action(monkeypatch: pytest.MonkeyPatch) -> None:
    alpha = button("Alpha", (110.0, 60.0))
    snap = _snap(_window(alpha))
    el = snap.element("e2")
    performed: list[tuple[object, str]] = []
    monkeypatch.setattr(observe, "_perform_action", lambda h, a: performed.append((h, a)) or True)

    assert observe.scroll_into_view(el) is True
    assert performed == [(alpha, "AXScrollToVisible")]


def test_scroll_into_view_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _snap(_window(button("Alpha", (110.0, 60.0))))
    el = snap.element("e2")
    monkeypatch.setattr(observe, "_perform_action", lambda h, a: False)
    assert observe.scroll_into_view(el) is False  # caller falls back to a wheel scroll


def test_scroll_into_view_without_handle_falls_back() -> None:
    orphan = Element(
        ref="e1",
        role="AXButton",
        title="x",
        value=None,
        bounds=Bounds(1, 0, 0, 10, 10),
        snapshot_id="snap-never-registered",
    )
    assert observe.scroll_into_view(orphan) is False
