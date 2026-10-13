"""ATSPI_ROLE_STATUS_BAR is a statusbar in the snapshot, not a layout group.

Hermetic. The accessible is a fake: no display, no bus, no GTK process.
"""

from __future__ import annotations
import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import RawNode, build_snapshot
from a11y_computer_use.schema import Display, Scope


def _geometry():
    from a11y_computer_use.observe import DisplayGeometry

    return (DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),)


class _FakeAccessor:
    def read(self, node):
        return node[0]

    def children(self, node):
        return node[1]


class _Bar:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_role_name(self):
        return "status bar"

    def get_name(self):
        return self.name

    def get_description(self):
        return ""


def test_status_bar_role_is_not_a_group() -> None:
    assert _atspi._ROLE["status bar"] == "AXStatusBar"
    assert "AXStatusBar" in _atspi._NO_VALUE_ROLES
    assert _atspi._ROLE.get("panel") == "AXGroup"


def test_status_bar_snapshot_says_statusbar(monkeypatch) -> None:
    """The image metadata bar keeps its text and renders as statusbar."""
    monkeypatch.setattr(_atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, False))
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((2.0, 760.0), (1276.0, 19.0)))
    monkeypatch.setattr(
        _atspi, "_value_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("probed")),
    )

    title = "img003-blue.png\t400 x 400\t2.8 kB\t100.0%"
    raw = _atspi.ATSPIAccessor().read(_Bar(title))
    assert raw.role == "AXStatusBar"
    assert raw.title == title
    assert raw.value is None

    window = (
        RawNode(role="AXWindow", title="ristretto", position=(0.0, 0.0), size=(1280.0, 800.0)),
        [(raw, [])],
    )
    snap = build_snapshot(
        window, _FakeAccessor(), scope=Scope.WINDOW, app="ristretto", pid=1, geometry=_geometry(),
    )
    rendered = observe.render_text(snap, mode="full", include_bounds=True)
    assert f'statusbar "{title}"' in rendered
    assert f'group "{title}"' not in rendered
    bar = next(el for el in snap.elements if el.title == title)
    assert bar.role == "AXStatusBar"
    assert bar.bounds.width == 1276 and bar.bounds.height == 19


def test_unnamed_status_bar_keeps_text_interface_label(monkeypatch) -> None:
    """Geany's caret status survives without probing numeric Value."""
    from types import SimpleNamespace

    text = "line: 301 col: 1"
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(Text=SimpleNamespace(
        get_character_count=lambda node: len(text),
        get_text=lambda node, start, end: text[start:end],
    )))
    monkeypatch.setattr(_atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, False))
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((2.0, 760.0), (1276.0, 19.0)))
    monkeypatch.setattr(_atspi, "_value_text", lambda *args: pytest.fail("probed Value"))
    raw = _atspi.ATSPIAccessor().read(_Bar(""))
    assert raw.role == "AXStatusBar"
    assert raw.title == text
    assert raw.value is None
