"""#1375: a parent's children past the fan-out caps are reachable by find.

Display-free: dict-backed trees through `build_snapshot`.
"""

from __future__ import annotations

from collections.abc import Sequence

from a11y_computer_use import observe
from a11y_computer_use.observe import MAX_CHILDREN, build_snapshot, find_elements, render_text
from a11y_computer_use.schema import Scope, Snapshot
from tests.fixtures.trees import GEOMETRY, DictAccessor, ax, button


class SeekAccessor(DictAccessor):
    def __init__(self, seek: str | None = None) -> None:
        self.child_seek = seek


def _snap(tree: dict, seek: str | None = None) -> Snapshot:
    return build_snapshot(
        tree, SeekAccessor(seek), scope=Scope.WINDOW, app="com.example.fixture",
        pid=101, geometry=GEOMETRY,
    )


def _window(*children: dict) -> dict:
    return ax("AXWindow", title="Doc", at=(0.0, 0.0), size=(1440.0, 900.0), children=children)


def _button_group(count: int) -> dict:
    kids = [button(f"Btn {i}", (10.0 + (i % 12) * 100.0, 60.0 + (i // 12) * 40.0)) for i in range(count)]
    return ax("AXGroup", title="Buttons", at=(0.0, 50.0), size=(1400.0, 800.0), children=kids)


def _point_chart(count: int) -> dict:
    kids = [
        ax(
            "AXButton", title=f"Outlier {i}" if i == 777 else f"pt {i}",
            at=(10.0 + (i % 40) * 30.0, 60.0 + (i // 40) * 20.0), size=(6.0, 6.0),
            actions=("AXPress",), children=[ax("AXStaticText", title="x", at=(0.0, 0.0), size=(1.0, 1.0))],
        )
        for i in range(count)
    ]
    return ax("AXGroup", title="Chart", at=(0.0, 50.0), size=(1400.0, 850.0), children=kids)


def test_uniform_button_group_lists_all_sixty() -> None:
    snap = _snap(_window(_button_group(60)))
    titles = {el.title for el in snap.elements}
    assert {f"Btn {i}" for i in range(60)} <= titles
    assert "more" not in render_text(snap)


def test_find_reaches_button_past_cap_in_large_group() -> None:
    # 120 buttons: over the 96 sheet allowance, so the plain view folds some.
    tree = _window(_button_group(120))
    plain = _snap(tree)
    assert not find_elements(plain, text="Btn 119")
    armed = _snap(tree, seek="Btn 119")
    hits = find_elements(armed, text="Btn 119")
    assert [el.title for el in hits] == ["Btn 119"]


def test_find_reaches_child_past_walk_window() -> None:
    # Index 777 is past the 200-child walk window and the 24 keep cap.
    tree = _window(_point_chart(1500))
    plain = _snap(tree)
    assert not find_elements(plain, text="Outlier 777")
    armed = _snap(tree, seek="outlier 777")
    hits = find_elements(armed, text="Outlier 777")
    assert [el.title for el in hits] == ["Outlier 777"]
    # The rest stays folded: the armed view is not a full listing.
    assert len([el for el in armed.elements if el.title.startswith("pt ")]) < MAX_CHILDREN


def test_plain_snapshot_unchanged_by_seek_code() -> None:
    tree = _window(_point_chart(300))
    a = render_text(_snap(tree)).split("\n", 1)[1]
    b = render_text(build_snapshot(
        tree, DictAccessor(), scope=Scope.WINDOW, app="com.example.fixture",
        pid=101, geometry=GEOMETRY,
    )).split("\n", 1)[1]
    assert a == b
    assert f"… {300 - MAX_CHILDREN} more" in a


def test_mixed_group_still_capped_at_24() -> None:
    kids: list[dict] = []
    for i in range(40):
        if i % 2:
            kids.append(button(f"B{i}", (10.0 + (i % 12) * 100.0, 60.0 + (i // 12) * 40.0)))
        else:
            kids.append(ax("AXCheckBox", title=f"C{i}", at=(10.0 + (i % 12) * 100.0, 60.0 + (i // 12) * 40.0),
                           size=(80.0, 30.0), actions=("AXPress",)))
    snap = _snap(_window(ax("AXGroup", title="Mixed", at=(0.0, 50.0), size=(1400.0, 800.0), children=kids)))
    group = next(el for el in snap.elements if el.title == "Mixed")
    assert len([el for el in snap.elements if el.parent == group.ref]) == MAX_CHILDREN


def test_dense_grid_still_capped() -> None:
    cells = [ax("AXCell", title=str(i + 1), at=(10.0 + (i % 7) * 40.0, 60.0 + (i // 7) * 30.0),
                size=(36.0, 26.0), actions=("AXPress",)) for i in range(30)]
    snap = _snap(_window(ax("AXGrid", title="Cal", at=(0.0, 50.0), size=(400.0, 300.0), children=cells)))
    grid = next(el for el in snap.elements if el.title == "Cal")
    assert len([el for el in snap.elements if el.parent == grid.ref]) == observe.DENSE_MAX_CHILDREN


def test_server_arms_and_restores_child_seek() -> None:
    from a11y_computer_use.server import Runtime

    class D:
        _child_seek = "old"

    srv = Runtime.__new__(Runtime)
    srv.driver = D()
    with srv._child_seek("Btn 59"):
        assert srv.driver._child_seek == "Btn 59"
    assert srv.driver._child_seek == "old"


def test_atspi_seek_reads_past_fetch_window(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    names = [f"pt {i}" for i in range(1500)]
    names[777] = "Outlier 777"
    monkeypatch.setattr(_atspi, "_child_at", lambda node, i: i)
    monkeypatch.setattr(_atspi, "_node_name", lambda child: names[child])
    start = _atspi._MAX_CHILDREN_FETCH
    assert _atspi._seek_children_past_fetch(object(), start, 1500, "outlier 777") == [777]
    assert _atspi._seek_children_past_fetch(object(), start, 1500, None) == []
    assert _atspi._seek_children_past_fetch(object(), start, 1500, "pt 9999") == []


def test_two_far_children_get_distinct_refs_and_resolve_back() -> None:
    """Refs found past the caps stay distinct and re-resolve to that child.

    The driver keeps earlier find texts armed (``remember_child_seek``), so
    the live tree an action re-resolves against lists the same children.
    """
    tree = _window(_point_chart(1500))
    found = _snap(tree, seek=("pt 1499", "outlier 777"))
    far = {el.title: el.ref for el in found.elements if el.title in {"pt 1499", "Outlier 777"}}
    assert set(far) == {"pt 1499", "Outlier 777"}
    assert len(set(far.values())) == 2
    live = _snap(tree, seek=("outlier 777", "pt 1499"))
    for title, ref in far.items():
        resolved = observe.rematch_ref(found, ref, live)
        assert resolved.title == title


def test_unarmed_live_tree_does_not_rebind_far_ref_to_a_sibling() -> None:
    tree = _window(_point_chart(1500))
    found = _snap(tree, seek="Outlier 777")
    ref = next(el.ref for el in found.elements if el.title == "Outlier 777")
    live = _snap(tree)
    try:
        resolved = observe.rematch_ref(found, ref, live)
    except Exception:
        return
    assert resolved.title == "Outlier 777"


class _Kid:
    def __init__(self, name: str) -> None:
        self.name = name


def _far_list(monkeypatch, names):
    from a11y_computer_use.drivers import _atspi

    kids = [_Kid(n) for n in names]
    monkeypatch.setattr(observe, "_PINNED_REF_HOOK", None)
    monkeypatch.setattr(_atspi, "_FAR_PINS", type(_atspi._FAR_PINS)())
    monkeypatch.setattr(_atspi, "_FAR_REFS", {})
    monkeypatch.setattr(_atspi, "_FAR_REF_PINS", {})
    monkeypatch.setattr(_atspi, "_child_at", lambda node, i: node[i] if i < len(node) else None)
    monkeypatch.setattr(_atspi, "_node_name", lambda child: child.name)
    monkeypatch.setattr(_atspi, "_extents", lambda child, **k: ((1.0, 2.0), (6.0, 6.0)))
    return _atspi, kids


def test_far_child_is_keyed_to_parent_and_index(monkeypatch) -> None:
    names = [f"pt {i}" for i in range(1500)]
    names[777] = "Outlier 777"
    _atspi, kids = _far_list(monkeypatch, names)
    hits = _atspi._seek_children_past_fetch(kids, 250, 1500, ("outlier 777", "pt 1499"))
    assert hits == [kids[777], kids[1499]]
    assert _atspi.far_pin(kids[777])[1:3] == (777, "Outlier 777")
    assert _atspi.far_pin(kids[1499])[1:3] == (1499, "pt 1499")
    assert _atspi.far_child_check(kids[777]) is None
    # A sibling now sits at index 777: refuse, never click the slot.
    kids[777] = _Kid("pt 77")
    assert _atspi.far_child_check(hits[0]) == "title_changed"
    del kids[1000:]
    assert _atspi.far_child_check(hits[1]) == "gone"


def test_linux_resolve_ref_checks_far_child_name(monkeypatch) -> None:
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    names = [f"pt {i}" for i in range(1500)]
    names[777] = "Outlier 777"
    _atspi, kids = _far_list(monkeypatch, names)
    _atspi._seek_children_past_fetch(kids, 250, 1500, "outlier 777")
    snap = _snap(_window(_point_chart(1500)), seek="Outlier 777")
    target = next(el for el in snap.elements if el.title == "Outlier 777")
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: kids[777] if ref == target.ref else None)
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda fn: fn()
    # The live tree an action reads does not list the far child at all.
    unarmed = _snap(_window(_point_chart(1500)))
    assert not any(el.title == "Outlier 777" for el in unarmed.elements)
    driver.effect_snapshot = lambda *a: unarmed
    driver.snapshot = driver.effect_snapshot
    resolved = driver.resolve_ref(snap, target.ref)
    assert resolved.title == "Outlier 777" and resolved.bounds == target.bounds
    assert driver.take_resolved_popover_cover(resolved) == (True, None)
    pinned = kids[777]
    kids[777] = _Kid("pt 77")
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: pinned)
    try:
        driver.resolve_ref(snap, target.ref)
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.STALE_REF
        assert exc.detail["reason"] == "title_changed"
    else:
        raise AssertionError("a renamed slot must be refused")



def test_find_scans_unread_children_by_name_only_after_a_miss(monkeypatch) -> None:
    from a11y_computer_use.drivers.linux import LinuxDriver

    names = [f"pt {i}" for i in range(1500)]
    names[777] = "Outlier 777"
    _atspi, kids = _far_list(monkeypatch, names)
    reads: list[int] = []
    monkeypatch.setattr(_atspi, "_child_at", lambda node, i: reads.append(i) or node[i])
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda fn: fn()
    driver._last_accessor = None
    assert driver.hidden_child_named("Outlier 777") is False and reads == []

    class Acc:
        hidden_parents = [(kids, 250, 1500)]

    driver._last_accessor = Acc()
    assert driver.hidden_child_named("outlier 777") is True
    assert reads[-1] == 777  # stops at the first hit
    reads.clear()
    assert driver.hidden_child_named("zzz nothing") is False
    assert len(reads) == 1250


def _pinned_chart(monkeypatch):
    """A 1500-point chart whose children 777 and 1499 were found past 250."""
    from a11y_computer_use.drivers import _atspi

    monkeypatch.setattr(observe, "_PINNED_REF_HOOK", None)
    monkeypatch.setattr(_atspi, "_FAR_PINS", type(_atspi._FAR_PINS)())
    monkeypatch.setattr(_atspi, "_FAR_REFS", {})
    monkeypatch.setattr(_atspi, "_FAR_REF_PINS", {})
    monkeypatch.setattr(_atspi, "_extents", lambda node, **k: (tuple(node["at"]), tuple(node["size"])))
    monkeypatch.setattr(_atspi, "_child_at", lambda node, i: node["children"][i] if i < len(node["children"]) else None)
    monkeypatch.setattr(_atspi, "_node_name", lambda node: node.get("title", ""))
    chart = _point_chart(1500)
    for index in (777, 1499):
        _atspi.pin_far_child(chart["children"][index], chart, index, chart["children"][index]["title"])
    return chart, _window(chart)


def _ref_of(snap: Snapshot, title: str) -> str:
    return next(el.ref for el in snap.elements if el.title == title)


def test_far_children_have_distinct_refs_stable_across_finds(monkeypatch) -> None:
    _chart, tree = _pinned_chart(monkeypatch)
    ref_a = _ref_of(_snap(tree, seek="Outlier 777"), "Outlier 777")
    ref_b = _ref_of(_snap(tree, seek="pt 1499"), "pt 1499")
    assert ref_a != ref_b
    assert observe.is_pinned_ref(ref_a) and observe.is_pinned_ref(ref_b)
    assert _ref_of(_snap(tree, seek="Outlier 777"), "Outlier 777") == ref_a
    both = _snap(tree, seek=("Outlier 777", "pt 1499"))
    assert {_ref_of(both, "Outlier 777"), _ref_of(both, "pt 1499")} == {ref_a, ref_b}
    # Pre-order refs never take a pinned number.
    assert not any(observe.is_pinned_ref(el.ref) for el in _snap(tree).elements)


def test_find_a_find_b_click_a_lands_on_a_or_refuses(monkeypatch) -> None:
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ComputerUseError, ErrorCode
    from a11y_computer_use.server import Runtime

    chart, tree = _pinned_chart(monkeypatch)
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda fn: fn()
    driver.effect_snapshot = lambda *a: _snap(tree)
    driver.snapshot = driver.effect_snapshot
    runtime = Runtime.__new__(Runtime)
    runtime.driver = driver
    first = _snap(tree, seek="Outlier 777")
    ref_a = _ref_of(first, "Outlier 777")
    runtime._current = first
    runtime._note_issued(first, {ref_a})
    second = _snap(tree, seek="pt 1499")
    ref_b = _ref_of(second, "pt 1499")
    runtime._current = second
    runtime._note_issued(second, {ref_b})
    assert ref_a != ref_b

    snap, anchor = runtime._anchor(ref_a)
    assert anchor.title == "Outlier 777"
    assert driver.resolve_ref(snap, ref_a).title == "Outlier 777"
    snap_b, _ = runtime._anchor(ref_b)
    assert driver.resolve_ref(snap_b, ref_b).title == "pt 1499"

    # A changed: refused, never redirected to B.
    chart["children"][777]["title"] = "pt 77"
    try:
        driver.resolve_ref(snap, ref_a)
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.STALE_REF
        assert exc.detail["reason"] == "title_changed"
    else:
        raise AssertionError("a changed far child must be refused")


def test_pinned_ref_resolves_after_its_snapshot_handles_are_released(monkeypatch) -> None:
    from a11y_computer_use.drivers.linux import LinuxDriver

    chart, tree = _pinned_chart(monkeypatch)
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda fn: fn()
    driver.effect_snapshot = lambda *a: _snap(tree)
    driver.snapshot = driver.effect_snapshot
    first = _snap(tree, seek="Outlier 777")
    ref_a = _ref_of(first, "Outlier 777")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *a: None)
    assert driver.resolve_ref(first, ref_a).title == "Outlier 777"
    chart["children"][777]["title"] = "pt 95"
    try:
        driver.resolve_ref(first, ref_a)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "detail", {}).get("reason") == "title_changed", exc
    else:
        raise AssertionError("must refuse, never rematch the slot")


def test_only_find_after_a_miss_arms_the_far_child_scan() -> None:
    """scroll_to_find keeps the 24-cap seek but never reads unfetched
    children by name: its scrolls bring rows in, and a 300-row table scanned
    on every scroll snapshot lost ROW-0300 under load."""
    from a11y_computer_use.server import Runtime

    class _Driver:
        _child_seek = None
        _far_seek = False

    runtime = Runtime.__new__(Runtime)
    runtime.driver = _Driver()
    with runtime._child_seek("ROW-0300"):
        assert runtime.driver._child_seek == "ROW-0300"
        assert runtime.driver._far_seek is False
    with runtime._child_seek("Outlier 777", far=True):
        assert runtime.driver._far_seek is True
    assert runtime.driver._child_seek is None and runtime.driver._far_seek is False
