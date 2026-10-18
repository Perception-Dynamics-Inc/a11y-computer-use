"""A LibreOffice walk asks each ancestor question once per snapshot.

Hermetic. The accessibles are fakes that count their calls: no display,
no bus, no LibreOffice process. On a Calc sheet every cell and widget used
to climb its parents to the application from each toolkit check, climb
again to its frame, and read that frame's rectangle twice. Those reads
were most of the live Calc snapshot (#775).
"""

from __future__ import annotations

from a11y_computer_use.drivers import _atspi


class _Rect:
    def __init__(self, x, y, w, h) -> None:
        self.x, self.y, self.width, self.height = x, y, w, h


class _Node:
    def __init__(self, role: str, parent=None, calls=None) -> None:
        self.role = role
        self.parent = parent
        self.kids: list = []
        self.calls = calls if calls is not None else (parent.calls if parent is not None else {})
        if parent is not None:
            parent.kids.append(self)

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def get_role_name(self):
        return self.role

    def get_parent(self):
        self._count("get_parent")
        return self.parent

    def get_application(self):
        self._count("get_application")
        node = self
        while node.parent is not None and node.parent.role != "desktop frame":
            node = node.parent
        return node

    def get_child_count(self):
        return len(self.kids)

    def get_child_at_index(self, index):
        return self.kids[index]

    def get_component_iface(self):
        return self

    def get_extents(self, coord):
        self._count("get_extents")
        return _Rect(0, 0, 1280, 800)


def _calc_tree():
    calls: dict = {}
    app = _Node("application", calls=calls)
    frame = _Node("frame", app)
    pane = _Node("panel", frame)
    table = _Node("table", pane)
    cells = [_Node("table cell", table) for _ in range(5)]
    return calls, app, frame, pane, table, cells


def test_children_carry_the_parent_application_inside_one_read() -> None:
    calls, app, _frame, _pane, table, cells = _calc_tree()
    with _atspi.read_cache():
        _atspi._share_parent_application(table, cells)
        calls.clear()
        owners = [_atspi._call_first(cell, ("get_application",)) for cell in cells]
    assert owners == [app] * len(cells)
    assert calls.get("get_application", 0) == 0


def test_a_child_that_names_another_parent_climbs_for_itself() -> None:
    calls, app, frame, _pane, table, cells = _calc_tree()
    stray = cells[0]
    stray.parent = frame
    with _atspi.read_cache():
        _atspi._share_parent_application(table, cells)
        calls.clear()
        assert _atspi._call_first(stray, ("get_application",)) is app
        assert calls.get("get_application", 0) == 1
        calls.clear()
        assert _atspi._call_first(cells[1], ("get_application",)) is app
        assert calls.get("get_application", 0) == 0


def test_nothing_is_shared_outside_a_read_cache_block() -> None:
    calls, app, _frame, _pane, table, cells = _calc_tree()
    _atspi._share_parent_application(table, cells)
    calls.clear()
    assert _atspi._call_first(cells[0], ("get_application",)) is app
    assert calls.get("get_application", 0) == 1


def test_frame_lookup_climbs_each_ancestor_once_per_walk() -> None:
    calls, _app, frame, _pane, _table, cells = _calc_tree()
    accessor = _atspi.ATSPIAccessor()
    calls.clear()
    found = [accessor._frame_ancestor_of(cell) for cell in cells]
    # The first cell climbs cell, table, pane. Each later cell takes one
    # step to the table this walk already placed.
    assert calls["get_parent"] == 3 + (len(cells) - 1)
    assert found == [frame] * len(cells)
    assert found == [_atspi._frame_ancestor(cell) for cell in cells]


def test_frame_outside_any_frame_is_none_for_the_walk_too() -> None:
    calls: dict = {}
    app = _Node("application", calls=calls)
    loose = _Node("panel", app)
    accessor = _atspi.ATSPIAccessor()
    assert accessor._frame_ancestor_of(loose) is None
    assert _atspi._frame_ancestor(loose) is None


class _FakeAtspi:
    class CoordType:
        SCREEN = 0
        WINDOW = 1


def test_frame_rectangle_is_read_once_per_walk(monkeypatch) -> None:
    # No PyGObject on macOS and Windows; the coord enum is all that is read.
    monkeypatch.setattr(_atspi, "_atspi", lambda: _FakeAtspi)
    calls, _app, frame, _pane, _table, _cells = _calc_tree()
    accessor = _atspi.ATSPIAccessor()
    calls.clear()
    first = accessor._frame_screen(frame)
    second = accessor._frame_screen(frame)
    assert first == second == (0.0, 0.0, 1280.0, 800.0)
    assert calls["get_extents"] == 1


def test_a_non_chromium_walk_does_not_ask_each_child_about_chromium(monkeypatch) -> None:
    _calls, _app, _frame, pane, _table, _cells = _calc_tree()
    asked: list = []
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: asked.append(acc) or False)
    monkeypatch.setattr(
        _atspi, "chromium_clipped_target",
        lambda acc: (_ for _ in ()).throw(AssertionError("clip probe on a GTK tree")),
    )
    monkeypatch.setattr(_atspi, "_gtk_table_rows_for_snapshot", lambda *a, **k: [])
    accessor = _atspi.ATSPIAccessor()
    kids = accessor.children(pane)
    assert len(kids) == 1
    assert len(asked) <= 1


def test_a_chromium_walk_still_drops_clipped_leaves(monkeypatch) -> None:
    _calls, _app, _frame, _pane, table, cells = _calc_tree()
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_in_view_named_rows", lambda node: [])
    monkeypatch.setattr(_atspi, "_gtk_table_rows_for_snapshot", lambda *a, **k: [])
    monkeypatch.setattr(_atspi, "chromium_clipped_target", lambda acc: acc is cells[0])
    accessor = _atspi.ATSPIAccessor()
    kids = accessor.children(table)
    assert cells[0] not in kids
    assert cells[1] in kids
