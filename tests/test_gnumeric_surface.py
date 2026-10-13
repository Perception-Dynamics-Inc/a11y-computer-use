"""Gnumeric's childless drawing area reports the AT-SPI limitation honestly."""

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, build_snapshot
from a11y_computer_use.schema import Display, Scope
from tests.test_audacity_panels import _Node, _Tree


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(_atspi, "_extents", lambda node, **kw: ((10.0, 10.0), node.size))
    monkeypatch.setattr(
        _atspi, "_state_flags",
        lambda node: (True, False, None, False, None, node.focusable, False),
    )
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kw: None)


def _frame(surface, app="gnumeric"):
    frame = _Node("frame", "Book1", children=[surface], size=(1200.0, 700.0))
    _Node("application", app, children=[frame])
    return frame


def test_drawing_area_is_findable_with_an_honest_note():
    surface = _Node("drawing area", size=(900.0, 500.0))
    frame = _frame(surface)
    snap = build_snapshot(
        frame, _Tree(), scope=Scope.WINDOW, app="gnumeric", pid=1,
        geometry=(DisplayGeometry(Display(0, 1280, 800, 1.0, True), (0.0, 0.0)),),
    )
    matches = observe.find_elements(snap, role="opaque_region", text="Gnumeric drawing area")
    assert len(matches) == 1
    region = matches[0]
    assert "not exposed by AT-SPI" in region.note
    assert "only the active cell" in region.note
    assert (region.bounds.x, region.bounds.y, region.bounds.width, region.bounds.height) == (10, 10, 900, 500)
    assert not observe.find_elements(snap, role="cell")
    assert not observe.find_elements(snap, role="table")
    assert not observe.find_elements(snap, text="ROW10")
    for mode in ("full", "interactive"):
        rendered = observe.render_text(snap, mode=mode)
        assert "Gnumeric drawing area" in rendered
        assert "not exposed by AT-SPI" in rendered


@pytest.mark.parametrize("app,role,name,description,children", [
    ("other", "drawing area", "", "", ()),
    ("gnumeric", "panel", "", "", ()),
    ("gnumeric", "drawing area", "Native title", "", ()),
    ("gnumeric", "drawing area", "", "Native description", ()),
    ("gnumeric", "drawing area", "", "", (_Node("table", "Cells"),)),
])
def test_other_surfaces_and_native_information_are_preserved(app, role, name, description, children):
    surface = _Node(role, name, description=description, children=children)
    _frame(surface, app)
    raw = _atspi.ATSPIAccessor().read(surface)
    assert raw.surface_name == ""
    assert raw.note == ""
    assert raw.title == name
    assert raw.description == description
