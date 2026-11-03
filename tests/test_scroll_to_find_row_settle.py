"""scroll_to_find waits for the matched row's cells before returning.

Chrome exposes a newly scrolled table row before its later cells. The
first cell names the target while the button in the same row is not in
the tree yet, so a ``find`` for that button right after the return saw
``no elements match`` (test_linux_chrome_scroll_to_find_large_table_row).
"""
from a11y_computer_use import server
from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot


def _el(ref, role, title, y, parent, h=24, **kw):
    return Element(ref=ref, role=role, title=title, value=None,
                   bounds=Bounds(0, 20, y, 400, h), snapshot_id="s",
                   parent=parent, **kw)


def _snap(scrolls: int, read: int, button_from: int) -> Snapshot:
    """Row ROW-0300 sits below the table until the second wheel.

    From then on its first two cells are in the tree at once; its button
    cell only from read ``button_from`` after that wheel. Main returned
    after its third read (index 2), before the button arrived at index 3.
    """
    els = [
        Element(ref="e0", role="AXWindow", title="T", value=None,
                bounds=Bounds(0, 0, 0, 1280, 800), snapshot_id="s"),
        Element(ref="e1", role="AXWebArea", title="T", value=None,
                bounds=Bounds(0, 0, 0, 1280, 800), snapshot_id="s", parent="e0"),
        _el("e2", "AXTable", "", 100, "e1", h=500),
        _el("e8", "AXRow", "", 110 - 40 * scrolls, "e2"),
        _el("e9", "AXCell", f"ROW-{scrolls:04d}", 110 - 40 * scrolls, "e8"),
        _el("e10", "AXCell", "val", 110 - 40 * scrolls, "e8"),
        _el("e11", "AXCell", "", 110 - 40 * scrolls, "e8"),
    ]
    if scrolls >= 2:
        els += [
            _el("e3", "AXRow", "", 300, "e2"),
            _el("e4", "AXCell", "", 300, "e3"),
            _el("e5", "AXStaticText", "ROW-0300", 300, "e4"),
            _el("e12", "AXCell", "val 300", 300, "e3"),
        ]
        if read >= button_from:
            els += [_el("e6", "AXCell", "", 300, "e3"),
                    _el("e7", "AXButton", "edit 300", 300, "e6", clickable=True)]
    return Snapshot(snapshot_id=f"s{scrolls}-{read}", scope=Scope.WINDOW, app="chrome",
                    pid=1, created_at=0.0, displays=(Display(0, 1280, 1000, 1.0, True),),
                    elements=tuple(els))


def _runtime(monkeypatch, button_from: int):
    state = {"scrolls": 0, "reads": 0}

    class _D:
        name = "linux"

        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            shot = _snap(state["scrolls"], state["reads"], button_from)
            if state["scrolls"] >= 2:
                state["reads"] += 1
            return shot

        def scroll(self, target, **kw):
            state["scrolls"] += 1

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))
    monkeypatch.setattr(server, "_SCROLL_VERIFY_PAUSE_S", 0)
    return rt, state


def _has_button(rt) -> bool:
    return any(el.role == "AXButton" and el.title == "edit 300"
               for el in rt._current.elements)


def test_row_whose_button_arrives_late_is_populated_on_return(monkeypatch) -> None:
    rt, state = _runtime(monkeypatch, button_from=3)
    out = rt.scroll_to_find("chrome", text="ROW-0300")
    assert out.startswith("found after"), out
    assert "ROW-0300" in out
    assert _has_button(rt), state


def test_populated_row_returns_after_one_extra_read(monkeypatch) -> None:
    rt, state = _runtime(monkeypatch, button_from=0)
    out = rt.scroll_to_find("chrome", text="ROW-0300")
    assert out.startswith("found after"), out
    assert _has_button(rt)
    # main reads 3 times after the row arrives; settling adds one read.
    assert state["reads"] <= 4, state


def test_descendant_count_marks_a_row_short_of_its_siblings() -> None:
    snap = _snap(2, 9, 0)
    assert server._row_descendant_count(snap, [snap.element("e5")]) == (5, True)
    early = _snap(2, 0, 9)
    assert server._row_descendant_count(early, [early.element("e5")]) == (3, False)
    assert server._row_descendant_count(snap, [snap.element("e2")]) is None
