"""Hermetic coverage for gucharmap Character Table surface hints."""

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


def _frame(surface, *, app="gucharmap", tab="Character Table", role="frame", parent_role="scroll pane"):
    frame = _Node(role, "Character Map", children=[
        _Node("page tab", tab, children=[_Node(parent_role, children=[surface, _Node("scroll bar")])]),
        _Node("label", "U+0041 LATIN CAPITAL LETTER A"),
        _Node("table", "Scripts", children=[_Node("table cell", "Arabic")]),
    ], size=(1200.0, 700.0))
    _Node("application", app, children=[frame])
    return frame


def test_table_is_labelled_without_fabricated_character_targets():
    surface = _Node("drawing area", size=(600.0, 500.0), focusable=True)
    snap = build_snapshot(
        _frame(surface), _Tree(), scope=Scope.WINDOW, app="gucharmap", pid=1,
        geometry=(DisplayGeometry(Display(0, 1280, 800, 1.0, True), (0.0, 0.0)),),
    )
    regions = observe.find_elements(snap, role="opaque_region", text="gucharmap Character Table")
    assert len(regions) == 1
    region = regions[0]
    assert (region.bounds.width, region.bounds.height) == (600.0, 500.0)
    assert region.value is None
    for text in ("not exposed by AT-SPI", "whole painted table", "scroll_to_find",
                 "clicking it cannot confirm", "Scrolling may change", "Character Details"):
        assert text in region.note
        assert text in observe.render_text(snap)
    for text in ("U+0041", "LATIN CAPITAL LETTER A", "U+2665", "HEART"):
        assert not observe.find_elements(snap, role="opaque_region", text=text)
        assert not observe.find_elements(snap, role="cell", text=text)
    matches = observe.find_elements(snap, text="LATIN CAPITAL LETTER A")
    assert len(matches) == 1 and matches[0].role != "opaque_region"
    assert len(observe.find_elements(snap, role="cell", text="Arabic")) == 1
    assert _atspi.opaque_click_verdict(surface)[0] == "unverifiable"


@pytest.mark.parametrize("context", [
    {"app": "other"}, {"tab": "Character Details"}, {"role": "dialog"},
    {"parent_role": "panel"},
])
def test_hint_does_not_leak_outside_character_table(context):
    surface = _Node("drawing area")
    _frame(surface, **context)
    raw = _atspi.ATSPIAccessor().read(surface)
    assert raw.surface_name == ""
    assert raw.note == ""


@pytest.mark.parametrize("name,description,children", [
    ("Native grid", "", ()), ("", "Native description", ()),
    ("", "", (_Node("table cell", "LATIN CAPITAL LETTER A"),)),
])
def test_native_content_takes_precedence(name, description, children):
    surface = _Node("drawing area", name, description=description, children=children)
    _frame(surface)
    raw = _atspi.ATSPIAccessor().read(surface)
    assert raw.surface_name == ""
    assert raw.note == ""
    assert raw.title == name
    assert raw.description == description
