"""Observe module tests: pruning, budgets, rendering, refs, permissions.

Everything tree-shaped runs against dict-backed fixtures
(tests/fixtures/trees.py) through the injectable `TreeAccessor` seam — no
TCC grant needed. Live-AX paths are covered by one skipif-guarded smoke test
plus dedicated tests for the structured permission/app errors.
"""

from __future__ import annotations

import sys

import pytest

from a11y_computer_use import observe
from a11y_computer_use.observe import (
    MAX_CHILDREN,
    MAX_DEPTH,
    build_snapshot,
    estimate_tokens,
    render_text,
    resolve_ref,
    snapshot,
)
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, Scope, Snapshot
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


def _wrapped(node: dict, levels: int, *, siblings: bool = False) -> dict:
    """Bury ``node`` under ``levels`` untitled AXGroup wrappers inside a window.
    With ``siblings`` every wrapper also holds a button, so none of them can
    collapse and the pruned depth equals the raw depth."""
    for i in range(levels):
        kids = [button(f"sibling {i}", (120.0, 140.0 + i)), node] if siblings else [node]
        node = ax("AXGroup", at=(100.0, 50.0), size=(1000.0, 700.0), children=kids)
    return ax("AXWindow", title="Doc", at=(100.0, 50.0), size=(1000.0, 700.0), children=[node])


def test_depth_counts_kept_ancestors_not_collapsed_wrappers() -> None:
    # Fifteen raw levels of generic single-child wrappers but two kept levels
    # (window > button): the leaf survives and nothing is marked as elided. This
    # is the web case (a link's text under a dozen generic wrappers).
    snap = snap_of(_wrapped(button("Deep", (120.0, 120.0)), levels=MAX_DEPTH + 3))
    assert by_title(snap, "Deep").path == ("AXWindow", "AXButton")
    assert "more" not in render_text(snap)


def test_depth_cap_applies_to_kept_depth_after_collapse() -> None:
    # Wrappers that keep two children never collapse, so the cap bites at the
    # same kept level as before and hides every raw child of the node at the cap.
    snap = snap_of(_wrapped(button("Deep", (120.0, 120.0)), levels=MAX_DEPTH + 3, siblings=True))
    assert max(len(el.path) for el in snap.elements) == MAX_DEPTH + 1
    assert not any(el.title == "Deep" for el in snap.elements)
    assert "… 2 more" in render_text(snap)
    assert render_text(snap).count("more") == 1


class _CountingAccessor(DictAccessor):
    """Counts accessor reads, the cost unit of a live walk (several IPC attribute
    copies per node on AX, D-Bus round-trips on AT-SPI). ``cap`` turns an
    unbounded walk into a failure instead of a hang."""

    def __init__(self, cap: int = 100_000) -> None:
        self.reads, self.cap = 0, cap

    def read(self, node):
        self.reads += 1
        assert self.reads <= self.cap, "the walk is not bounded by MAX_DEPTH"
        return super().read(node)


def _binary_wrapper_tree(depth: int) -> dict:
    """Web div soup: a complete binary tree of untitled AXGroup wrappers with a
    button at every leaf. No wrapper can collapse (each keeps two children)."""
    def node(d: int) -> dict:
        if d == 0:
            return button("Leaf", (120.0, 60.0))
        return ax("AXGroup", at=(100.0, 50.0), size=(500.0, 500.0), children=[node(d - 1), node(d - 1)])

    return ax("AXWindow", title="W", at=(100.0, 50.0), size=(1000.0, 700.0), children=[node(depth)])


def test_depth_cap_bounds_the_walk_not_just_the_output() -> None:
    # 2^17-1 raw nodes, but the cap must fire DURING the walk: only raw levels
    # 0..MAX_DEPTH are read (1 + 2^12 - 1 = 4096 reads, the count at 3b331ba), not
    # the whole tree followed by a post-pass that throws the deep part away.
    acc = _CountingAccessor()
    snap = build_snapshot(_binary_wrapper_tree(16), acc, scope=Scope.WINDOW, app="x", pid=1,
                          geometry=GEOMETRY)
    assert acc.reads == 4096
    assert max(len(el.path) for el in snap.elements) == MAX_DEPTH + 1


def test_cyclic_wrapper_with_fanout_is_bounded_by_max_depth() -> None:
    # An untitled group that lists itself twice (a fan-out-2 AX cycle) must be
    # capped by MAX_DEPTH during the walk, not by _MAX_RAW_DEPTH after ~2^64 reads.
    g = ax("AXGroup", at=(100.0, 50.0), size=(500.0, 500.0), children=[])
    g["children"] = [button("B", (120.0, 60.0)), g, g]
    root = ax("AXWindow", title="W", at=(100.0, 50.0), size=(1000.0, 700.0), children=[g])
    acc = _CountingAccessor()
    snap = build_snapshot(root, acc, scope=Scope.WINDOW, app="x", pid=1, geometry=GEOMETRY)
    assert acc.reads == 6143  # the 3b331ba count
    assert max(len(el.path) for el in snap.elements) == MAX_DEPTH + 1


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
    from a11y_computer_use import capture

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
    monkeypatch.delenv("A11Y_COMPUTER_USE_NO_WEB_A11Y", raising=False)
    monkeypatch.setattr(observe.time, "sleep", lambda _s: None)
    ax = _FakeAx()
    observe._maybe_enable_web_a11y(ax, object(), _FakeAcc("AXWebArea"), pid=1234)
    assert ("AXManualAccessibility", True) in ax.calls
    assert ("AXEnhancedUserInterface", True) in ax.calls
    assert 1234 in observe._WEB_A11Y_ENABLED  # cached: won't re-set next snapshot


def test_web_a11y_skipped_on_native_app_but_marked_handled(monkeypatch) -> None:
    observe._WEB_A11Y_ENABLED.clear()
    monkeypatch.delenv("A11Y_COMPUTER_USE_NO_WEB_A11Y", raising=False)
    ax = _FakeAx()
    observe._maybe_enable_web_a11y(ax, object(), _FakeAcc("AXWindow"), pid=999)
    assert ax.calls == []  # no web area -> nothing set
    assert 999 in observe._WEB_A11Y_ENABLED  # but never probed again


def test_web_a11y_opt_out(monkeypatch) -> None:
    observe._WEB_A11Y_ENABLED.clear()
    monkeypatch.setenv("A11Y_COMPUTER_USE_NO_WEB_A11Y", "1")
    ax = _FakeAx()
    observe._maybe_enable_web_a11y(ax, object(), _FakeAcc("AXWebArea"), pid=1)
    assert ax.calls == []


# ---------------------------------------------------------------------------
# Stable-id anchors (AXIdentifier / AutomationId / accessible-id)
# ---------------------------------------------------------------------------


def test_stable_id_survives_title_and_bounds_drift() -> None:
    """A button whose label AND position both change still re-resolves via its
    stable_id — the case title/path/bounds anchoring fails on (dynamic UIs)."""
    before = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        ax("AXButton", title="Submit", at=(10.0, 10.0), size=(80.0, 30.0),
           actions=("AXPress",), stable_id="submit-btn"),
    ])
    after = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        ax("AXButton", title="Sending…", at=(250.0, 200.0), size=(80.0, 30.0),
           actions=("AXPress",), stable_id="submit-btn"),
    ])
    snap_before, snap_after = snap_of(before), snap_of(after)
    anchor = by_title(snap_before, "Submit")
    match, reason = observe._match_anchor(anchor, snap_after)
    assert match is not None and reason == ""
    assert match.title == "Sending…" and match.stable_id == "submit-btn"


def test_stable_id_duplicates_disambiguate_by_proximity() -> None:
    tree = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 400.0), children=[
        ax("AXButton", title="Row", at=(10.0, 10.0), size=(80.0, 30.0),
           actions=("AXPress",), stable_id="row"),
        ax("AXButton", title="Row", at=(10.0, 300.0), size=(80.0, 30.0),
           actions=("AXPress",), stable_id="row"),
    ])
    snap = snap_of(tree)
    top = [el for el in snap.elements if el.bounds.y < 100][0]
    match, _ = observe._match_anchor(top, snap)
    assert match is not None and match.bounds.y == top.bounds.y  # nearest wins


def test_no_stable_id_falls_back_to_positional_ladder() -> None:
    # unchanged behaviour when the app assigns no ids
    snap1 = snap_of(save_window())
    snap2 = snap_of(save_window(save_at=(205.0, 105.0)))
    anchor = by_title(snap1, "Save")
    match, reason = observe._match_anchor(anchor, snap2)
    assert match is not None and match.title == "Save" and reason == ""


# ---------------------------------------------------------------------------
# Snapshot diffing (the non-accumulation token win)
# ---------------------------------------------------------------------------


def _btn(title, at, sid=None):
    node = ax("AXButton", title=title, at=at, size=(80.0, 30.0), actions=("AXPress",))
    if sid:
        node["stable_id"] = sid
    return node


def test_diff_add_remove_change() -> None:
    old = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        _btn("Save", (10.0, 10.0)),
        ax("AXTextField", title="Name", value="", at=(10.0, 50.0), size=(200.0, 30.0)),
        _btn("Cancel", (10.0, 90.0)),
    ])
    new = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        _btn("Save", (10.0, 10.0)),
        ax("AXTextField", title="Name", value="Alice", at=(10.0, 50.0), size=(200.0, 30.0)),
        _btn("Submit", (10.0, 90.0)),
    ])
    d = observe.diff_snapshots(snap_of(old), snap_of(new))
    assert {e.title for e in d.added} == {"Submit"}
    assert {e.title for e in d.removed} == {"Cancel"}
    assert len(d.changed) == 1
    _oel, nel, ch = d.changed[0]
    assert nel.title == "Name" and ch["value"] == ("", "Alice")
    assert not d.empty


def test_diff_no_change_is_empty() -> None:
    d = observe.diff_snapshots(snap_of(save_window()), snap_of(save_window()))
    assert d.empty
    assert "(no change)" in observe.render_diff(d)


def test_diff_stable_id_move_is_change_not_add_remove() -> None:
    old = ax("AXWindow", title="W", at=(0.0, 0.0), size=(500.0, 500.0),
             children=[_btn("Go", (10.0, 10.0), sid="go")])
    new = ax("AXWindow", title="W", at=(0.0, 0.0), size=(500.0, 500.0),
             children=[_btn("Going…", (300.0, 400.0), sid="go")])
    d = observe.diff_snapshots(snap_of(old), snap_of(new))
    assert not d.added and not d.removed  # same stable_id -> matched
    assert len(d.changed) == 1
    _o, _n, ch = d.changed[0]
    assert "title" in ch and "bounds" in ch


def test_render_diff_shows_all_three_sections() -> None:
    old = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        _btn("Keep", (10.0, 10.0)),
        _btn("Old", (10.0, 50.0)),
        ax("AXTextField", title="F", value="a", at=(10.0, 90.0), size=(200.0, 30.0)),
    ])
    new = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        _btn("Keep", (10.0, 10.0)),
        _btn("New", (10.0, 50.0)),
        ax("AXTextField", title="F", value="b", at=(10.0, 90.0), size=(200.0, 30.0)),
    ])
    text = observe.render_diff(observe.diff_snapshots(snap_of(old), snap_of(new)))
    assert "+1 -1 ~1" in text
    assert "New" in text and "gone" in text and "→" in text


# ---------------------------------------------------------------------------
# Self-correcting stale-ref candidates
# ---------------------------------------------------------------------------


def test_stale_ref_candidates_rank_partial_title_near_misses() -> None:
    live = snap_of(ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        _btn("Submit form", (10.0, 10.0)),
        _btn("Cancel", (10.0, 50.0)),
    ]))
    anchor = Element(ref="e9", role="AXButton", title="Submit", value=None,
                     bounds=Bounds(0, 10, 10, 80, 30), snapshot_id="old",
                     path=("AXWindow", "AXButton"))
    cands = observe.stale_ref_candidates(anchor, live)
    assert cands and cands[0]["title"] == "Submit form"  # partial overlap ranked first
    assert all(set(c) == {"ref", "role", "title", "score"} for c in cands)


def test_stale_ref_error_carries_candidates() -> None:
    old = snap_of(ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0),
                     children=[_btn("Submit", (10.0, 10.0))]))
    # renamed + reparented under a toolbar → genuinely stale (path & title differ)
    new = snap_of(ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        ax("AXToolbar", title="Bar", at=(0.0, 0.0), size=(400.0, 40.0),
           children=[_btn("Submit form", (10.0, 10.0))]),
    ]))
    anchor = by_title(old, "Submit")
    with pytest.raises(ComputerUseError) as ei:
        observe.resolve_ref(old, anchor.ref, live=new)
    assert ei.value.code is ErrorCode.STALE_REF
    cands = ei.value.detail["candidates"]
    assert any(c["title"] == "Submit form" for c in cands)


# ---------------------------------------------------------------------------
# Interactive view (render mode) and budget truncation
# ---------------------------------------------------------------------------


def _interactive_window() -> dict:
    """A window mixing actionable controls, static text, an untitled wrapper, a
    titled group, a plain (selectable) row and a layout row holding a button."""
    return ax(
        "AXWindow", title="Editor", at=(100.0, 50.0), size=(1000.0, 700.0),
        children=[
            ax("AXToolbar", at=(100.0, 50.0), size=(1000.0, 40.0), children=[
                button("Save", (110.0, 55.0)),
                ax("AXStaticText", value="Draft", at=(200.0, 55.0), size=(60.0, 20.0)),
            ]),
            ax("AXGroup", at=(100.0, 100.0), size=(600.0, 500.0), children=[  # untitled: skipped
                ax("AXStaticText", value="Welcome back, Alice", at=(110.0, 110.0), size=(300.0, 20.0)),
                ax("AXStaticText", value="3 unread", at=(110.0, 140.0), size=(300.0, 20.0)),
                ax("AXTextField", title="Name", value="Alice", at=(110.0, 170.0), size=(300.0, 30.0)),
                ax("AXCheckBox", title="Remember", at=(110.0, 210.0), size=(120.0, 20.0),
                   actions=("AXPress",), checked=False),
            ]),
            ax("AXGroup", title="Sidebar", at=(720.0, 100.0), size=(250.0, 500.0), children=[
                ax("AXStaticText", value="Recent", at=(725.0, 105.0), size=(100.0, 20.0)),
                ax("AXRow", at=(725.0, 130.0), size=(240.0, 20.0), children=[  # plain row: a target
                    ax("AXCell", value="Report Q3", at=(725.0, 130.0), size=(240.0, 20.0)),
                ]),
                ax("AXRow", at=(725.0, 160.0), size=(240.0, 20.0), children=[  # layout row: folded
                    button("Open", (730.0, 160.0)),
                ]),
            ]),
            ax("AXStaticText", value="Status: saved", at=(110.0, 650.0), size=(300.0, 20.0)),
        ],
    )


def test_interactive_view_keeps_actionables_with_identical_refs() -> None:
    snap = snap_of(_interactive_window())
    view = observe.interactive_view(snap)
    kept = {el.title or el.value: el for el in view}
    # every actionable element survives, as the SAME object the full snapshot issued
    for title in ("Save", "Name", "Remember", "Open"):
        assert title in kept, title
        assert snap.element(kept[title].ref) is kept[title]
    # a plain row is a selectable target; a row that merely holds a button is layout
    assert len([el for el in view if el.role == "AXRow"]) == 1
    assert not any(el.role == "AXCell" for el in view)
    # structure: the root, the toolbar (structural role) and the titled group stay;
    # the untitled wrapper group and every static text go
    assert {el.role for el in view if el.title == "Sidebar"} == {"AXGroup"}
    assert any(el.role == "AXToolbar" for el in view)
    assert not any(el.role == "AXStaticText" for el in view)
    assert not any(el.role == "AXGroup" and not el.title for el in view)
    assert [el.ref for el in view] == sorted((el.ref for el in view), key=lambda r: int(r[1:]))


def test_interactive_render_folds_static_text_per_container() -> None:
    snap = snap_of(_interactive_window())
    text = render_text(snap, mode="interactive")
    lines = text.splitlines()
    assert lines[0].endswith("(window) interactive")
    # one folded text line per kept container, in tree order
    assert '    text: "Welcome back, Alice | 3 unread | Status: saved"' in lines
    assert '      text: "Draft"' in lines  # under the toolbar
    assert '        text: "Report Q3"' in lines  # under the plain row
    assert '      text: "Recent"' in lines  # under Sidebar
    assert not any('"Draft"' in ln and "statictext" in ln for ln in lines)
    # the click flag is implied for buttons/checkboxes; other flags remain
    save = next(ln for ln in lines if '"Save"' in ln)
    assert "(click" not in save
    assert any('"Remember" (unchecked)' in ln for ln in lines)
    assert any('"Name" ="Alice" (edit)' in ln for ln in lines)
    # the footer says what was folded, so the agent knows how to see it
    hidden = len(snap.elements) - len(observe.interactive_view(snap))
    assert lines[-1] == f"… {hidden} static elements folded; mode='full' or find lists them"
    # the full view still prints the flag and the static lines
    full = render_text(snap)
    assert 'button "Save" (click)' in full and 'statictext ="Draft"' in full


def test_interactive_layout_row_children_reparent_under_nearest_kept() -> None:
    snap = snap_of(_interactive_window())
    lines = render_text(snap, mode="interactive").splitlines()
    sidebar = next(i for i, ln in enumerate(lines) if '"Sidebar"' in ln)
    open_btn = next(i for i, ln in enumerate(lines) if '"Open"' in ln)
    assert open_btn > sidebar

    def indent(ln: str) -> int:
        return len(ln) - len(ln.lstrip(" "))

    # "Open" sits exactly one level below Sidebar: its layout row was folded away
    assert indent(lines[open_btn]) == indent(lines[sidebar]) + 2
    # while the plain row keeps its own line, one level below Sidebar as well
    row = next(i for i, ln in enumerate(lines) if ln.strip().split(" ")[1:2] == ["row"])
    assert indent(lines[row]) == indent(lines[sidebar]) + 2


def test_interactive_omits_bounds_unless_requested() -> None:
    snap = snap_of(_interactive_window())
    assert " @1:" not in render_text(snap, mode="interactive")  # no geometry, not even the root
    with_bounds = render_text(snap, mode="interactive", include_bounds=True)
    element_lines = [ln for ln in with_bounds.splitlines()[1:]
                     if ln.strip().startswith("e") and "text:" not in ln]
    assert element_lines and all(" @1:" in ln for ln in element_lines)
    # full mode keeps geometry on roots only by default, everywhere on request
    assert render_text(snap).count(" @1:") == 1
    assert render_text(snap, include_bounds=True).count(" @1:") == len(snap.elements)


def test_interactive_elisions_roll_up_to_the_kept_container() -> None:
    # typical_app_window's untitled sidebar list is capped (16 rows elided); the
    # list itself is folded, so its marker surfaces on the window it folds into.
    snap = snap_of(typical_app_window())
    text = render_text(snap, mode="interactive")
    assert f"… {40 - MAX_CHILDREN} more" in text
    assert not any(ln.strip().startswith("e") and " list" in ln for ln in text.splitlines())


def test_budget_truncates_deterministically_and_reports_omissions() -> None:
    snap = snap_of(_interactive_window())
    full = render_text(snap)
    total_lines = len(full.splitlines())
    cut = render_text(snap, budget=30)
    assert cut == render_text(snap, budget=30), "same snapshot, same budget, same text"
    lines = cut.splitlines()
    assert lines[0] == full.splitlines()[0], "the header always survives"
    assert lines[-1].startswith("… truncated at ~30 tokens: ")
    omitted = int(lines[-1].split(": ")[1].split(" ")[0])
    assert omitted == total_lines - len(lines) + 1  # every dropped line here is an element line
    assert len("\n".join(lines[:-1])) <= 30 * 4
    assert render_text(snap, budget=10_000) == full, "a roomy budget changes nothing"
    with pytest.raises(ValueError):
        render_text(snap, budget=0)
    assert estimate_tokens(snap, mode="interactive") < estimate_tokens(snap)


def test_render_text_rejects_unknown_mode() -> None:
    snap = snap_of(save_window())
    with pytest.raises(ValueError):
        render_text(snap, mode="compact")
    with pytest.raises(ValueError):
        observe.render_diff(observe.diff_snapshots(snap, snap), mode="compact")


def test_interactive_diff_folds_static_changes_into_one_text_line() -> None:
    old = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        _btn("Save", (10.0, 10.0)),
        ax("AXStaticText", title="status", value="Status: saving", at=(10.0, 50.0), size=(200.0, 20.0)),
        ax("AXStaticText", title="tip", value="Tip of the day", at=(10.0, 80.0), size=(200.0, 20.0)),
    ])
    new = ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 300.0), children=[
        _btn("Save", (10.0, 10.0)),
        _btn("Undo", (100.0, 10.0)),
        ax("AXStaticText", title="status", value="Status: saved", at=(10.0, 50.0), size=(200.0, 20.0)),
        ax("AXStaticText", title="banner", value="New!", at=(10.0, 80.0), size=(200.0, 20.0)),
    ])
    diff = observe.diff_snapshots(snap_of(old), snap_of(new))
    assert len(diff.added) == 2 and len(diff.removed) == 1 and len(diff.changed) == 1
    text = observe.render_diff(diff, mode="interactive")
    lines = text.splitlines()
    assert lines[0].endswith("+2 -1 ~1")  # header counts describe the whole diff
    assert any(ln.startswith("  + ") and '"Undo"' in ln for ln in lines)
    # the static label change is kept, folded into a single text line
    assert sum(1 for ln in lines if ln.startswith("  ~ text: ")) == 1
    assert "Status: saving→Status: saved" in text
    # static elements that appeared or vanished are only counted
    assert "(1 added, 1 removed static elements folded)" in text
    assert "Tip of the day" not in text and "New!" not in text
    # the full diff still lists everything (removed elements print title, not value)
    full = observe.render_diff(diff)
    assert '"tip" (gone)' in full and "New!" in full and '"Undo" (click)' in full


def test_interactive_diff_empty_and_budget() -> None:
    snap = snap_of(save_window())
    d = observe.diff_snapshots(snap, snap_of(save_window()))
    assert "(no change)" in observe.render_diff(d, mode="interactive")
    big_old = snap_of(ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 400.0), children=[]))
    big_new = snap_of(ax("AXWindow", title="W", at=(0.0, 0.0), size=(400.0, 400.0),
                         children=[_btn(f"B{i}", (10.0, 10.0 + 30.0 * i)) for i in range(12)]))
    cut = observe.render_diff(observe.diff_snapshots(big_old, big_new), budget=20)
    assert cut.splitlines()[-1].startswith("… truncated at ~20 tokens")
