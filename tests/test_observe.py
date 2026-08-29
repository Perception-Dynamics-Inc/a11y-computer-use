"""Observe module tests: pruning, budgets, rendering, refs, permissions.

Everything tree-shaped runs against dict-backed fixtures
(tests/fixtures/trees.py) through the injectable `TreeAccessor` seam — no
TCC grant needed. Live-AX paths are covered by one skipif-guarded smoke test
plus dedicated tests for the structured permission/app errors.
"""

from __future__ import annotations

import sys

import pytest

from computeruse import observe
from computeruse.observe import (
    MAX_CHILDREN,
    MAX_DEPTH,
    build_snapshot,
    estimate_tokens,
    render_text,
    resolve_ref,
    snapshot,
)
from computeruse.schema import Bounds, ComputerUseError, Element, ErrorCode, Scope, Snapshot
from tests.conftest import HAS_AX, HAS_DISPLAYS
from tests.fixtures.trees import (
    GEOMETRY,
    DictAccessor,
    ax,
    button,
    deep_chain,
    typical_app_window,
)

FIXTURE_APP = "com.example.fixture"


def snap_of(tree: dict) -> Snapshot:
    return build_snapshot(
        tree,
        DictAccessor(),
        scope=Scope.WINDOW,
        app=FIXTURE_APP,
        pid=101,
        geometry=GEOMETRY,
    )


def by_title(snap: Snapshot, title: str) -> Element:
    return next(el for el in snap.elements if el.title == title)


def save_window(save_at: tuple[float, float] = (200.0, 100.0), extra: tuple = ()) -> dict:
    return ax(
        "AXWindow",
        title="Doc",
        at=(100.0, 50.0),
        size=(1000.0, 700.0),
        children=[button("Save", save_at), *extra],
    )


# ---------------------------------------------------------------------------
# Pruning engine
# ---------------------------------------------------------------------------


def test_bounds_projected_to_physical_pixels() -> None:
    snap = snap_of(typical_app_window())
    window = snap.element("e1")
    # window authored at (100, 50) points, 1200x800, on a 2x display
    assert window.bounds == Bounds(display_id=1, x=200, y=100, width=2400, height=1600)


def test_zero_size_and_offscreen_nodes_dropped() -> None:
    snap = snap_of(typical_app_window())
    titles = {el.title for el in snap.elements}
    assert "Ghost" not in titles, "fully offscreen nodes must be dropped"
    assert "Half off" in titles, "partially visible nodes must be kept"
    assert not any(
        el.bounds.width == 0 or el.bounds.height == 0 for el in snap.elements
    ), "zero-size nodes must be dropped"


def test_decorative_nodes_dropped_but_described_image_kept() -> None:
    snap = snap_of(typical_app_window())
    images = [el for el in snap.elements if el.role == "AXImage"]
    assert [el.title for el in images] == ["Sync status"], (
        "unlabelled images and empty static text are decorative; described "
        "images survive with the description as title"
    )
    assert not any(el.role == "AXStaticText" and not el.value for el in snap.elements)


def test_single_child_wrappers_collapse() -> None:
    snap = snap_of(typical_app_window())
    editor = by_title(snap, "Document body")
    assert editor.parent == "e1", "group>scrollarea chain must collapse onto the window"
    assert editor.path == ("AXWindow", "AXTextArea"), "path reflects the pruned ancestry"
    assert not any(el.role in ("AXGroup", "AXScrollArea") for el in snap.elements)


def test_children_capped_with_interactive_priority() -> None:
    snap = snap_of(typical_app_window())
    sidebar = next(el for el in snap.elements if el.role == "AXList")
    kept = [el for el in snap.elements if el.parent == sidebar.ref]
    assert len(kept) == MAX_CHILDREN
    kept_titles = {el.title for el in kept}
    assert {"Pin note", "Load more"} <= kept_titles, "interactive children must survive the cap"
    elided = 40 - MAX_CHILDREN
    assert f"… {elided} more" in render_text(snap)


def test_depth_capped_with_marker() -> None:
    snap = snap_of(deep_chain(levels=MAX_DEPTH + 3))
    deepest = max(len(el.path) for el in snap.elements)
    assert deepest == MAX_DEPTH + 1, "nodes beyond the depth cap are elided"
    assert by_title(snap, f"level {MAX_DEPTH}") is not None
    assert not any(el.title == "Bottom" for el in snap.elements)
    assert "… 1 more" in render_text(snap)


def test_refs_are_sequential_and_preorder() -> None:
    snap = snap_of(typical_app_window())
    assert [el.ref for el in snap.elements] == [f"e{i}" for i in range(1, len(snap.elements) + 1)]
    seen: dict[str, Element] = {}
    for el in snap.elements:
        if el.parent is not None:
            assert el.parent in seen, "parents must precede children (pre-order)"
            assert el.path == seen[el.parent].path + (el.role,)
            assert el.snapshot_id == snap.snapshot_id
        seen[el.ref] = el


def test_secure_field_flagged_and_value_never_leaks() -> None:
    snap = snap_of(typical_app_window())
    field = by_title(snap, "Password")
    assert field.secure and field.editable
    assert field.value is None, "secure fields must never carry the secret"
    text = render_text(snap)
    assert "hunter2" not in text
    assert "secure" in text


# ---------------------------------------------------------------------------
# Rendering & token budget
# ---------------------------------------------------------------------------


def test_render_text_is_compact_and_within_budget() -> None:
    snap = snap_of(typical_app_window())
    text = render_text(snap)
    assert text.splitlines()[0] == f"[{snap.snapshot_id}] {FIXTURE_APP} (window)"
    editor = by_title(snap, "Document body")
    assert f'{editor.ref} textarea "Document body" ="hello world" (edit,focus)' in text
    publish = by_title(snap, "Publish")
    assert f'{publish.ref} button "Publish" (click,disabled)' in text
    assert estimate_tokens(snap) == (len(text) + 3) // 4
    assert estimate_tokens(snap) <= 1000, "typical window must fit the ~1k token budget"


def test_estimate_tokens_works_on_foreign_snapshots(synthetic_snapshot: Snapshot) -> None:
    # conftest's snapshot was not built by observe: no epoch metadata, no
    # markers — rendering and the estimate must still work.
    text = render_text(synthetic_snapshot)
    assert "e1 window" in text
    assert "…" not in text
    assert estimate_tokens(synthetic_snapshot) > 0


def test_epoch_registry_is_bounded() -> None:
    for _ in range(observe._MAX_EPOCHS + 4):
        snap_of(save_window())
    assert len(observe._EPOCHS) == observe._MAX_EPOCHS


# ---------------------------------------------------------------------------
# Ref re-resolution (snapshot-scoped refs)
# ---------------------------------------------------------------------------


def test_resolve_ref_same_epoch_returns_element() -> None:
    snap = snap_of(save_window())
    save = by_title(snap, "Save")
    assert resolve_ref(snap, save.ref, live=snap) == save


def test_resolve_ref_follows_moved_element_across_epochs() -> None:
    old = snap_of(save_window())
    live = snap_of(save_window(save_at=(240.0, 120.0)))
    resolved = resolve_ref(old, by_title(old, "Save").ref, live=live)
    assert resolved.snapshot_id == live.snapshot_id
    assert resolved.title == "Save"
    assert resolved.bounds == by_title(live, "Save").bounds


def test_resolve_ref_raises_structured_stale_ref() -> None:
    old = snap_of(save_window())
    save_ref = by_title(old, "Save").ref
    live = snap_of(
        ax("AXWindow", title="Doc", at=(100.0, 50.0), size=(1000.0, 700.0), children=[])
    )
    with pytest.raises(ComputerUseError) as exc:
        resolve_ref(old, save_ref, live=live)
    err = exc.value
    assert err.code is ErrorCode.STALE_REF
    assert err.to_dict()["error"] == "stale_ref"
    assert err.detail["ref"] == save_ref
    assert err.detail["snapshot_id"] == old.snapshot_id
    assert err.detail["reason"] == "not_found"
    assert err.detail["anchor"] == {"role": "AXButton", "title": "Save", "path": ["AXWindow", "AXButton"]}


def test_resolve_ref_ambiguous_twins_are_stale() -> None:
    old = snap_of(save_window(save_at=(200.0, 100.0)))
    # two identical Save buttons equidistant from the anchor's old center
    live = snap_of(
        ax(
            "AXWindow",
            title="Doc",
            at=(100.0, 50.0),
            size=(1000.0, 700.0),
            children=[button("Save", (150.0, 100.0)), button("Save", (250.0, 100.0))],
        )
    )
    with pytest.raises(ComputerUseError) as exc:
        resolve_ref(old, by_title(old, "Save").ref, live=live)
    assert exc.value.code is ErrorCode.STALE_REF
    assert exc.value.detail["reason"] == "ambiguous"


def test_resolve_ref_unknown_ref_is_a_key_error() -> None:
    snap = snap_of(save_window())
    with pytest.raises(KeyError):
        resolve_ref(snap, "e999", live=snap)


def test_resolve_ref_defaults_to_fresh_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    old = snap_of(save_window())
    live = snap_of(save_window(save_at=(240.0, 120.0)))
    calls: list[tuple[Scope, str | None]] = []

    def fake_snapshot(scope: Scope = Scope.WINDOW, *, app: str | None = None) -> Snapshot:
        calls.append((scope, app))
        return live

    monkeypatch.setattr(observe, "snapshot", fake_snapshot)
    resolved = resolve_ref(old, by_title(old, "Save").ref)
    assert calls == [(Scope.WINDOW, FIXTURE_APP)], "default live path re-observes same scope/app"
    assert resolved.snapshot_id == live.snapshot_id


# ---------------------------------------------------------------------------
# Permission / live-path errors (no TCC grant required)
# ---------------------------------------------------------------------------


def test_snapshot_without_ax_grant_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observe, "_is_trusted", lambda: False)
    with pytest.raises(ComputerUseError) as exc:
        snapshot(Scope.WINDOW, app="com.apple.TextEdit")
    err = exc.value
    assert err.code is ErrorCode.PERMISSION_DENIED_ACCESSIBILITY
    assert err.to_dict()["error"] == "permission_denied_accessibility"
    assert "doctor" in str(err.detail["hint"]), "error must carry a doctor hint"


def test_snapshot_validates_scope_and_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observe, "_is_trusted", lambda: True)
    with pytest.raises(NotImplementedError):
        snapshot(Scope.DISPLAY)
    with pytest.raises(ValueError):
        snapshot(Scope.WINDOW)


@pytest.mark.skipif(sys.platform != "darwin", reason="NSWorkspace is macOS-only")
def test_snapshot_unknown_app_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observe, "_is_trusted", lambda: True)
    with pytest.raises(ComputerUseError) as exc:
        snapshot(Scope.APP, app="com.example.no-such-app-xyz")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert exc.value.detail == {"app": "com.example.no-such-app-xyz"}


@pytest.mark.skipif(
    not HAS_DISPLAYS,
    reason="needs an unlocked window-server session reporting a display",
)
def test_display_geometry_enumerates_real_displays() -> None:
    # Display enumeration needs no TCC grant, so this can run live.
    geometry = observe._display_geometry()
    assert len(geometry) >= 1
    assert sum(g.display.is_main for g in geometry) == 1
    assert all(g.display.scale > 0 for g in geometry)


@pytest.mark.skipif(
    not HAS_DISPLAYS,
    reason="needs an unlocked window-server session reporting a display",
)
def test_display_geometry_matches_capture_metadata() -> None:
    """Snapshot bounds and screenshot metadata must agree on what "physical
    pixels" means: observe delegates to capture.displays, so a Retina main
    display reports scale 2.0 (not the CGDisplayPixelsWide points bug) in
    both modules."""
    from computeruse import capture

    observed = {g.display.display_id: g.display for g in observe._display_geometry()}
    captured = {d.display_id: d for d in capture.displays()}
    assert observed == captured


@pytest.mark.skipif(not HAS_AX, reason="Accessibility TCC grant missing")
def test_snapshot_live_smoke() -> None:
    # Read-only: walks Finder's AX tree, injects nothing.
    snap = snapshot(Scope.APP, app="com.apple.finder")
    assert snap.elements, "Finder should expose at least its menu bar"
    assert estimate_tokens(snap) <= 2000


# --- per-widget pruning + interactive coverage (COM-12) ----------------------


def test_dense_container_capped_tighter_than_lists() -> None:
    # A grid (dense container) with many interactive cells is capped harder
    # than the plain MAX_CHILDREN, to keep dense widgets (calendar month grid,
    # spreadsheets) within the snapshot token budget.
    cells = [button(f"cell {i}", (110.0, 60.0 + 25.0 * i)) for i in range(30)]
    grid = ax("AXGrid", at=(100.0, 50.0), size=(400.0, 900.0), children=cells)
    root = ax("AXWindow", title="Grid", at=(100.0, 50.0), size=(500.0, 1000.0), children=[grid])
    snap = build_snapshot(root, DictAccessor(), scope=Scope.WINDOW, app="x", pid=1, geometry=GEOMETRY)

    grid_el = next(el for el in snap.elements if el.role == "AXGrid")
    kept = [el for el in snap.elements if el.parent == grid_el.ref]
    assert len(kept) == observe.DENSE_MAX_CHILDREN
    assert observe.DENSE_MAX_CHILDREN < MAX_CHILDREN
    assert f"… {30 - observe.DENSE_MAX_CHILDREN} more" in render_text(snap)


def test_interactive_count_distinguishes_hostile_apps() -> None:
    rich = build_snapshot(
        typical_app_window(), DictAccessor(), scope=Scope.WINDOW, app="x", pid=1, geometry=GEOMETRY
    )
    assert observe.interactive_count(rich) > 0  # buttons + text area

    # a window of only static text (a custom-drawn app's shell) has none
    plain = ax(
        "AXWindow", title="W", at=(100.0, 50.0), size=(300.0, 200.0),
        children=[ax("AXStaticText", value="hi", at=(110.0, 60.0), size=(100.0, 20.0))],
    )
    bare = build_snapshot(plain, DictAccessor(), scope=Scope.WINDOW, app="x", pid=1, geometry=GEOMETRY)
    assert observe.interactive_count(bare) == 0


# ---------------------------------------------------------------------------
# find_elements / render_matches (the `find` tool query, platform-free)
# ---------------------------------------------------------------------------


def _find_window() -> dict:
    return ax(
        "AXWindow", title="Form", at=(100.0, 50.0), size=(1000.0, 700.0),
        children=[
            button("Save", (120.0, 70.0)),
            button("Cancel", (220.0, 70.0)),
            ax("AXTextField", title="Name", value="Alice", at=(120.0, 120.0), size=(300.0, 30.0)),
            ax("AXStaticText", value="Welcome back", at=(120.0, 170.0), size=(300.0, 20.0)),
        ],
    )


def test_find_by_text_matches_title_or_value() -> None:
    snap = snap_of(_find_window())
    assert {e.title for e in observe.find_elements(snap, text="save")} == {"Save"}
    # value is searched too: "Alice" lives in the text field's value
    assert [e.role for e in observe.find_elements(snap, text="alice")] == ["AXTextField"]


def test_find_by_role_ignores_ax_prefix_and_is_substring() -> None:
    snap = snap_of(_find_window())
    assert {e.title for e in observe.find_elements(snap, role="button")} == {"Save", "Cancel"}
    assert {e.title for e in observe.find_elements(snap, role="AXButton")} == {"Save", "Cancel"}
    assert [e.title for e in observe.find_elements(snap, role="textfield")] == ["Name"]


def test_find_by_capability_flags() -> None:
    snap = snap_of(_find_window())
    assert [e.title for e in observe.find_elements(snap, editable=True)] == ["Name"]
    assert {e.title for e in observe.find_elements(snap, clickable=True)} == {"Save", "Cancel"}


def test_find_combines_filters() -> None:
    snap = snap_of(_find_window())
    # role + text narrows to one
    assert {e.title for e in observe.find_elements(snap, role="button", text="cancel")} == {"Cancel"}


def test_render_matches_shows_refs_and_bounds_or_no_match() -> None:
    snap = snap_of(_find_window())
    matches = observe.find_elements(snap, role="button")
    out = observe.render_matches(snap, matches)
    assert "2 match(es)" in out
    assert "Save" in out and "Cancel" in out
    assert "e" in out and "@" in out  # refs + bounds
    empty = observe.render_matches(snap, observe.find_elements(snap, text="nonesuch"))
    assert "no elements match" in empty


# ---------------------------------------------------------------------------
# Rich element states (checked / selected / expanded / placeholder)
# ---------------------------------------------------------------------------


def test_checked_state_helper() -> None:
    assert observe._checked_state("AXCheckBox", "", 1) is True
    assert observe._checked_state("AXCheckBox", "", 0) is False
    assert observe._checked_state("AXCheckBox", "", 2) is True   # mixed reads as on
    assert observe._checked_state("AXRadioButton", "", "1") is True
    assert observe._checked_state("AXButton", "AXToggle", 1) is True
    assert observe._checked_state("AXButton", "", 1) is None      # not checkable
    assert observe._checked_state("AXCheckBox", "", None) is None  # no value


def test_states_flow_to_element_and_render() -> None:
    tree = ax(
        "AXWindow", title="Prefs", at=(0.0, 0.0), size=(800.0, 600.0),
        children=[
            ax("AXCheckBox", title="Wifi", at=(10.0, 10.0), size=(120.0, 20.0),
               actions=("AXPress",), checked=True),
            ax("AXCheckBox", title="Bluetooth", at=(10.0, 40.0), size=(120.0, 20.0),
               actions=("AXPress",), checked=False),
            ax("AXRow", title="Row A", at=(10.0, 70.0), size=(300.0, 20.0), selected=True),
            ax("AXDisclosureTriangle", title="More", at=(10.0, 100.0), size=(20.0, 20.0),
               actions=("AXPress",), expanded=False),
            ax("AXTextField", title="", placeholder="Search", at=(10.0, 130.0), size=(300.0, 24.0)),
        ],
    )
    snap = snap_of(tree)
    els = {el.title: el for el in snap.elements}
    assert els["Wifi"].checked is True
    assert els["Bluetooth"].checked is False
    assert els["Row A"].selected is True
    assert els["More"].expanded is False
    text = observe.render_text(snap)
    assert "(click,checked)" in text or "checked" in text
    assert "unchecked" in text
    assert "selected" in text
    assert "collapsed" in text
    assert '~"Search"' in text  # empty field shows its placeholder for identity


# ---------------------------------------------------------------------------
# Chromium/Electron a11y force-enable (AXEnhancedUserInterface)
# ---------------------------------------------------------------------------


class _FakeAx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def AXUIElementSetAttributeValue(self, el, attr, val):  # noqa: N802
        self.calls.append((attr, val))
        return 0


class _FakeAcc:
    def __init__(self, root_role: str) -> None:
        self._role = root_role

    def _attr(self, node, name):
        if name == "AXRole":
            return self._role
        if name == "AXChildren":
            return ()
        return None


def test_web_a11y_enabled_on_chromium_like_app(monkeypatch) -> None:
    observe._WEB_A11Y_ENABLED.clear()
    monkeypatch.delenv("COMPUTERUSE_NO_WEB_A11Y", raising=False)
    monkeypatch.setattr(observe.time, "sleep", lambda _s: None)
    ax = _FakeAx()
    observe._maybe_enable_web_a11y(ax, object(), _FakeAcc("AXWebArea"), pid=1234)
    assert ("AXManualAccessibility", True) in ax.calls
    assert ("AXEnhancedUserInterface", True) in ax.calls
    assert 1234 in observe._WEB_A11Y_ENABLED  # cached: won't re-set next snapshot


def test_web_a11y_skipped_on_native_app_but_marked_handled(monkeypatch) -> None:
    observe._WEB_A11Y_ENABLED.clear()
    monkeypatch.delenv("COMPUTERUSE_NO_WEB_A11Y", raising=False)
    ax = _FakeAx()
    observe._maybe_enable_web_a11y(ax, object(), _FakeAcc("AXWindow"), pid=999)
    assert ax.calls == []  # no web area -> nothing set
    assert 999 in observe._WEB_A11Y_ENABLED  # but never probed again


def test_web_a11y_opt_out(monkeypatch) -> None:
    observe._WEB_A11Y_ENABLED.clear()
    monkeypatch.setenv("COMPUTERUSE_NO_WEB_A11Y", "1")
    ax = _FakeAx()
    observe._maybe_enable_web_a11y(ax, object(), _FakeAcc("AXWebArea"), pid=1)
    assert ax.calls == []
