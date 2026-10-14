"""Hermetic coverage for Okular's textless PDF viewport."""
from __future__ import annotations

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from tests.test_audacity_panels import _Node, _patch, _snapshot


def _document(surface, *, app="okular", window_role="frame", pane_name="", scrollbar=True):
    children = [surface]
    if scrollbar:
        children.append(_Node("scroll bar"))
    pane = _Node("panel", pane_name, children=children)
    layered = _Node("layered pane", children=[pane])
    split = _Node("split pane", children=[layered])
    frame = _Node(window_role, "sample.pdf — Okular", children=[split], size=(1200, 700))
    _Node("application", app, children=[frame])
    return frame


@pytest.mark.parametrize("role", ["filler", "panel"])
def test_textless_document_is_named_but_remains_opaque(role):
    surface = _Node(role, size=(633, 460), focusable=True)
    snap = _snapshot(_document(surface))
    matches = observe.find_elements(snap, role="opaque_region", text="Okular document")
    assert len(matches) == 1
    region = matches[0]
    assert region.title == "Okular document / page view"
    assert "find and scroll_to_find cannot read it" in region.note
    assert "Ctrl+F" in region.note
    assert region.value is None
    assert region.bounds.width == 633
    assert region.bounds.height == 460
    assert not observe.find_elements(snap, text="OKULAR_PAGE1_TOKEN_ALPHA_0840")
    assert "Page text is not exposed" in observe.render_text(snap)


@pytest.mark.parametrize("kwargs", [
    {"app": "other"}, {"window_role": "dialog"}, {"pane_name": "Thumbnails"},
    {"scrollbar": False},
])
def test_unrelated_surfaces_are_not_labelled(kwargs):
    surface = _Node("filler", size=(633, 460))
    _document(surface, **kwargs)
    raw = _atspi.ATSPIAccessor().read(surface)
    assert raw.surface_name == ""
    assert raw.note == ""


@pytest.mark.parametrize("kwargs", [
    {"name": "Real page view"}, {"description": "Real description"},
    {"children": [_Node("label", "Actual page text")]},
])
def test_application_supplied_content_is_preserved(kwargs):
    surface = _Node("panel", size=(633, 460), **kwargs)
    _document(surface)
    raw = _atspi.ATSPIAccessor().read(surface)
    assert raw.surface_name == ""
    assert raw.note == ""
    assert raw.title == kwargs.get("name", "")
    assert raw.description == kwargs.get("description", "")


def test_text_interface_is_not_described_as_unavailable(monkeypatch):
    surface = _Node("filler", size=(633, 460))
    _document(surface)
    monkeypatch.setattr(_atspi, "_has_text_interface", lambda node: True)
    assert _atspi.ATSPIAccessor().read(surface).surface_name == ""
