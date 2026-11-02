"""#1461: scroll_to_find must return the matched control, never a grid whose
aggregated value text merely contains the row that is still clipped."""
from a11y_computer_use import server
from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot


def _snap(step: int) -> Snapshot:
    # Viewport y 166..469. The pooled overscan holds "Edit order 120" below it
    # until the third scroll brings the button inside.
    button_y = {0: None, 1: 520.0, 2: 480.0}.get(step, 300.0)
    rows = "Order 113\n$113.00\nEdit order 113\n" + ("Edit order 120" if button_y else "")
    els = [
        Element(ref="e0", role="AXWindow", title="V: ready", value=None,
                bounds=Bounds(0, 0, 0, 1280, 800), snapshot_id="s"),
        Element(ref="e1", role="AXWebArea", title="V: ready", value=None,
                bounds=Bounds(0, 0, 0, 1280, 800), snapshot_id="s", parent="e0"),
        Element(ref="e28", role="AXScrollArea", title="", value=None,
                bounds=Bounds(0, 14, 166, 562, 303), snapshot_id="s", parent="e1"),
        Element(ref="e29", role="AXTable", title="Orders", value=rows,
                bounds=Bounds(0, 14, 166, 562, 303), snapshot_id="s", parent="e28"),
    ]
    if button_y is not None:
        els.append(Element(ref="e40", role="AXButton", title="Edit order 120", value=None,
                           bounds=Bounds(0, 400, button_y, 120, 24), snapshot_id="s",
                           clickable=True, parent="e29"))
    return Snapshot(snapshot_id=f"s{step}", scope=Scope.WINDOW, app="chrome", pid=1,
                    created_at=0.0, displays=(Display(0, 1280, 1000, 1.0, True),),
                    elements=tuple(els))


def test_scroll_to_find_returns_the_button_not_the_grid_1461(monkeypatch) -> None:
    scrolls: list = []

    class _D:
        name = "linux"
        i = 0

        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return _snap(_D.i)

        def scroll(self, target, **kw):
            scrolls.append(kw.get("dy"))
            _D.i += 1

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))
    monkeypatch.setattr(server, "_SCROLL_VERIFY_PAUSE_S", 0)

    out = rt.scroll_to_find("chrome", text="Edit order 120")
    assert out.startswith("found after")
    assert "e40" in out or "Edit order 120" in out.split(":", 1)[1]
    assert "Orders" not in out
    assert len(scrolls) >= 3


def test_grid_with_matching_title_still_counts() -> None:
    grid = Element(ref="e1", role="AXTable", title="Edit order 120 history", value=None,
                   bounds=None, snapshot_id="s")
    cell = Element(ref="e2", role="AXTable", title="Orders", value="Edit order 120",
                   bounds=None, snapshot_id="s")
    assert server._drop_aggregate_containers((grid, cell), "edit order 120") == (grid,)
