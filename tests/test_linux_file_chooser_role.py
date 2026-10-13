"""ATSPI_ROLE_FILE_CHOOSER is a dialog in the snapshot, not a document window.

Hermetic. The accessible is a fake: no display, no bus, no GTK process.
"""

from __future__ import annotations

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import build_snapshot
from a11y_computer_use.schema import Scope


def _geometry():
    from a11y_computer_use.observe import DisplayGeometry
    from a11y_computer_use.schema import Display

    return (DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),)


def _node(role, title, *, pos=(0.0, 0.0), size=(800.0, 500.0), children=(), actions=()):
    from a11y_computer_use.observe import RawNode

    return (
        RawNode(role=role, title=title, position=pos, size=size, actions=actions),
        list(children),
    )


class _FakeAccessor:
    def read(self, node):
        return node[0]

    def children(self, node):
        return node[1]


class _Chooser:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_role_name(self):
        return "file chooser"

    def get_name(self):
        return self.name

    def get_description(self):
        return ""


def test_file_chooser_role_is_a_dialog_not_a_window() -> None:
    assert _atspi._ROLE["file chooser"] == "AXDialog"
    assert _atspi._ROLE["dialog"] == "AXDialog"
    assert _atspi._ROLE["frame"] == "AXWindow"
    assert _atspi._ROLE["color chooser"] == "AXWindow"


def test_file_chooser_snapshot_says_dialog(monkeypatch) -> None:
    """The Open dialog and its nested file-chooser widget both render as dialog."""
    monkeypatch.setattr(_atspi, "_state_flags", lambda acc: (True, False, None, False, None, False, False))
    monkeypatch.setattr(_atspi, "_extents", lambda acc, keep_zero=False: ((40.0, 30.0), (640.0, 420.0)))
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("probed")))

    outer = _atspi.ATSPIAccessor().read(_Chooser("Open image"))
    inner = _atspi.ATSPIAccessor().read(_Chooser("File Chooser Widget"))
    assert outer.role == "AXDialog" and outer.value is None
    assert inner.role == "AXDialog" and inner.title == "File Chooser Widget"

    cancel = _node("AXButton", "Cancel", pos=(400.0, 400.0), size=(80.0, 30.0), actions=("AXPress",))
    open_button = _node("AXButton", "Open", pos=(500.0, 400.0), size=(80.0, 30.0), actions=("AXPress",))
    tree = _node(
        outer.role, outer.title, size=(640.0, 420.0),
        children=[_node(
            inner.role, inner.title, pos=(8.0, 40.0), size=(624.0, 340.0),
            children=[cancel, open_button],
        )],
    )
    snap = build_snapshot(
        tree, _FakeAccessor(), scope=Scope.WINDOW, app="ristretto", pid=1, geometry=_geometry(),
    )
    rendered = observe.render_text(snap, mode="full")
    assert 'dialog "Open image"' in rendered
    assert 'dialog "File Chooser Widget"' in rendered
    assert 'window "Open image"' not in rendered
    assert 'window "File Chooser Widget"' not in rendered
    assert 'button "Cancel"' in rendered and 'button "Open"' in rendered
    assert {el.role for el in snap.elements if "Open image" in el.title or el.title == "File Chooser Widget"} == {
        "AXDialog",
    }
