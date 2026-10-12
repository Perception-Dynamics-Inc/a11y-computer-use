"""Chromium controls buried past the kept-depth cap.

Synthetic trees. ``atspi_web`` marks a document the way the Linux AT-SPI
reader does. A tree with that field empty keeps the old cap: a buried
button stays elided.
"""

from __future__ import annotations

from a11y_computer_use.observe import (
    MAX_DEPTH,
    DisplayGeometry,
    RawNode,
    build_snapshot,
    render_text,
)
from a11y_computer_use.schema import Display, Scope

_GEOMETRY = (
    DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),
)


class _WebAccessor:
    def __init__(self) -> None:
        self.reads = 0

    def read(self, node: object) -> RawNode:
        assert isinstance(node, dict)
        self.reads += 1
        assert self.reads < 500, "the buried-control scan is not bounded"
        return RawNode(
            role=str(node["role"]),
            title=str(node.get("title", "")),
            value=node.get("value"),
            description=str(node.get("description", "")),
            position=node.get("at"),
            size=node.get("size"),
            actions=tuple(node.get("actions", ())),
            atspi_web=str(node.get("atspi_web", "")),
            editable=bool(node.get("editable", False)),
        )

    def children(self, node: object):
        assert isinstance(node, dict)
        return list(node.get("children", ()))


def _ax(role: str, title: str = "", *, children=(), web: str = "", **extra) -> dict:
    node = {
        "role": role,
        "title": title,
        "at": (20.0, 40.0),
        "size": (900.0, 600.0),
        "children": list(children),
        "atspi_web": web,
    }
    node.update(extra)
    return node


def _phone_page(*, web: str) -> dict:
    """Phone group at the kept-depth cap, under a document web when ``web`` is set."""
    settings = _ax(
        "AXGroup",
        "Settings",
        children=[
            _ax("AXButton", "General", actions=("AXPress",), size=(80.0, 24.0)),
            _ax("AXTextArea", "Composer", editable=True, size=(400.0, 80.0)),
            _ax("AXStaticText", "BuriedStatic", size=(80.0, 14.0)),
        ],
    )
    phone = _ax(
        "AXGroup",
        "Phone",
        children=[
            _ax("AXComboBox", "Country", actions=("AXPress",), size=(120.0, 28.0)),
            _ax("AXTextField", "", editable=True, size=(160.0, 28.0)),
            _ax("AXStaticText", "HintLabel", size=(80.0, 14.0)),
            settings,
        ],
    )
    node = phone
    # page is kept-depth 1. ``MAX_DEPTH - 2`` wrappers put Phone at the cap.
    for i in range(MAX_DEPTH - 2):
        node = _ax(
            "AXGroup",
            "",
            children=[
                node,
                _ax("AXStaticText", f"pad {i}", size=(40.0, 12.0)),
            ],
        )
    page = _ax("AXGroup", "Form", children=[node], web=web)
    return _ax("AXWindow", "Chrome", children=[page])


def _titles(tree: dict) -> set[str]:
    accessor = _WebAccessor()
    snap = build_snapshot(
        tree, accessor, scope=Scope.WINDOW, app="chrome", pid=1, geometry=_GEOMETRY,
    )
    return {el.title for el in snap.elements}, accessor.reads, render_text(snap)


def test_web_depth_cap_keeps_phone_and_nested_controls() -> None:
    titles, reads, text = _titles(_phone_page(web="page"))
    assert "Phone" in titles
    assert "Country" in titles
    assert "General" in titles
    assert "Composer" in titles
    assert "HintLabel" not in titles
    assert "BuriedStatic" not in titles
    assert "Settings" not in titles
    assert reads < 500
    assert "Country" in text
    assert "Composer" in text


def test_unmarked_tree_still_hides_the_buried_controls() -> None:
    titles, reads, _text = _titles(_phone_page(web=""))
    assert "Phone" in titles or "Form" in titles
    assert "Country" not in titles
    assert "General" not in titles
    assert "Composer" not in titles
    assert reads < 500


def test_web_scan_does_not_read_past_the_existing_budget() -> None:
    """The control past ``_MAX_WEB_FRAME_SCAN`` stays elided."""
    early = _ax("AXButton", "Early", actions=("AXPress",), size=(80.0, 24.0))
    padding = [
        _ax("AXStaticText", f"pad-row {i}", size=(40.0, 12.0)) for i in range(70)
    ]
    late = _ax("AXButton", "Late", actions=("AXPress",), size=(80.0, 24.0))
    phone = _ax("AXGroup", "Phone", children=[early, *padding, late])
    node = phone
    for i in range(MAX_DEPTH - 2):
        node = _ax(
            "AXGroup",
            "",
            children=[node, _ax("AXStaticText", f"wrap {i}", size=(20.0, 10.0))],
        )
    page = _ax("AXGroup", "Form", children=[node], web="page")
    window = _ax("AXWindow", "Chrome", children=[page])
    titles, reads, _text = _titles(window)
    assert "Early" in titles
    assert "Late" not in titles
    assert reads < 500
