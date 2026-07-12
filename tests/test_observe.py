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
