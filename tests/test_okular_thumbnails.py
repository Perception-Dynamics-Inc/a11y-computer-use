"""Hermetic coverage for Okular thumbnail surface hints."""

from __future__ import annotations

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, build_snapshot
from a11y_computer_use.schema import Display, Scope


class _Node:
    def __init__(self, role, name="", *, description="", children=(),
                 size=(52.0, 52.0), focusable=False):
        self.role = role
        self.name = name
        self.description = description
        self.children = list(children)
        self.size = size
        self.focusable = focusable
        self.parent = None
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return self.description

    def get_parent(self):
        return self.parent

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]


class _Tree:
    def read(self, node):
        return _atspi.ATSPIAccessor().read(node)

    def children(self, node):
        return node.children


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(_atspi, "_extents", lambda node, **kw: ((10.0, 10.0), node.size))
    monkeypatch.setattr(
        _atspi, "_state_flags",
        lambda node: (True, False, None, False, None, node.focusable, False),
    )
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kw: None)


def _frame(*children, app="okular", role="frame"):
    frame = _Node(role, "Sidebar", children=children, size=(1200.0, 700.0))
    _Node("application", app, children=[frame])
    return frame


def _snapshot(frame):
    return build_snapshot(
        frame, _Tree(), scope=Scope.WINDOW, app="okular", pid=1,
        geometry=(DisplayGeometry(Display(0, 1280, 800, 1.0, True), (0.0, 0.0)),),
    )


def _sidebar(strip, *, app="okular", role="frame", label="Thumbnails"):
    return _frame(_Node("filler", children=[
        _Node("label", label),
        _Node("entry", "Enter at least 3 letters to filter pages"),
        _Node("filler", children=[strip, _Node("scroll bar")], size=(180.0, 600.0)),
    ], size=(200.0, 650.0)), app=app, role=role)


def test_empty_thumbnail_strip_is_labelled_and_remains_opaque():
    strip = _Node("filler", size=(166.0, 580.0))
    snap = _snapshot(_sidebar(strip))
    regions = observe.find_elements(snap, role="opaque_region", text="Okular thumbnails")
    assert len(regions) == 1
    assert regions[0].bounds.width == 166.0
    assert regions[0].bounds.height == 580.0
    for text in ("not exposed by AT-SPI", "page-number field", "Go to Page…", "Find bar"):
        assert text in regions[0].note
    for page in ("1", "2", "3", "4"):
        assert not observe.find_elements(snap, role="image", text=page)
    assert len(observe.find_elements(snap, text="Enter at least 3 letters")) == 1


@pytest.mark.parametrize("app,role,label", [
    ("other", "frame", "Thumbnails"),
    ("okular", "dialog", "Thumbnails"),
    ("okular", "frame", "Bookmarks"),
])
def test_fallback_does_not_leak_to_other_apps_or_panes(app, role, label):
    strip = _Node("filler", size=(166.0, 580.0))
    _sidebar(strip, app=app, role=role, label=label)
    raw = _atspi.ATSPIAccessor().read(strip)
    assert raw.surface_name == ""
    assert raw.note == ""


@pytest.mark.parametrize("name,description,children", [
    ("Native thumbnails", "", ()),
    ("", "Native description", ()),
    ("", "", (_Node("icon", "3"),)),
])
def test_native_thumbnail_content_is_preserved(name, description, children):
    strip = _Node("filler", name, description=description, children=children,
                  size=(166.0, 580.0))
    snap = _snapshot(_sidebar(strip))
    raw = _atspi.ATSPIAccessor().read(strip)
    assert raw.surface_name == ""
    assert raw.note == ""
    if children:
        assert len(observe.find_elements(snap, role="image", text="3")) == 1
    else:
        assert raw.title == name
        assert raw.description == description


def test_unrelated_empty_sidebar_filler_is_not_labelled():
    strip = _Node("filler", size=(166.0, 580.0))
    frame = _sidebar(strip)
    unrelated = _Node("filler", size=(40.0, 40.0))
    frame.children.append(unrelated)
    unrelated.parent = frame
    assert _atspi.ATSPIAccessor().read(unrelated).surface_name == ""
