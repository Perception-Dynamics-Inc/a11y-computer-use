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
