"""#1230: links inside GTK3 label text become clickable link nodes."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from a11y_computer_use.drivers import _atspi


class _Link:
    def __init__(self, start, end):
        self._start, self._end = start, end

    def get_start_index(self):
        return self._start

    def get_end_index(self):
        return self._end

    def get_uri(self, index):
        return "lab://alpha"


class _Node:
    def __init__(self, role="link", parent=None, link=None):
        self.role, self.parent, self.link = role, parent, link

    def get_hyperlink(self):
        return self.link


def _install(monkeypatch, text, boxes):
    """``boxes[offset]`` is ``(x, y, w, h)`` or None for one character of ``text``."""
    def character(acc, offset, coord):
        box = boxes.get(offset)
        return None if box is None else NS(x=box[0], y=box[1], width=box[2], height=box[3])

    monkeypatch.setattr(_atspi, "_atspi", lambda: NS(
        CoordType=NS(SCREEN=0),
        Text=NS(get_text=lambda acc, start, end: text[start:end], get_character_extents=character),
    ))
    monkeypatch.setattr(_atspi, "_role_name", lambda node: node.role)
    monkeypatch.setattr(_atspi, "_parent_of", lambda node: node.parent)


def _label_link(start, end):
    label = _Node("label")
    return _Node("link", label, _Link(start, end))


def test_a_link_is_boxed_by_the_characters_it_spans(monkeypatch) -> None:
    text = "Read the alpha guide or"
    boxes = {i: (100 + 8 * i, 43, 8, 19) for i in range(len(text))}
    _install(monkeypatch, text, boxes)
    got = _atspi._inline_link_box(_label_link(9, 20))
    assert got == ("alpha guide", (100 + 8 * 9, 43.0), (8 * 11, 19.0))


def test_a_wrapped_link_is_boxed_by_its_first_drawn_line(monkeypatch) -> None:
    text = "see gamma link that wraps"
    boxes = {i: (10 + 8 * i, 74, 8, 19) for i in range(0, 14)}
    boxes.update({i: (10 + 8 * (i - 14), 93, 8, 19) for i in range(14, len(text))})
    _install(monkeypatch, text, boxes)
    title, position, size = _atspi._inline_link_box(_label_link(4, len(text)))
    assert title == "gamma link that wraps"
    assert position == (10 + 8 * 4, 74.0)
    assert size == (8 * 10, 19.0)  # characters 4..13 only; the second line is not the link's box


def test_unplaceable_links_are_not_invented(monkeypatch) -> None:
    text = "alpha"
    _install(monkeypatch, text, {i: (-1, -1, -1, -1) for i in range(5)})
    assert _atspi._inline_link_box(_label_link(0, 5)) is None  # GTK's no-allocation sentinel
    _install(monkeypatch, text, {})
    assert _atspi._inline_link_box(_label_link(0, 5)) is None  # no extents at all
    _install(monkeypatch, "   ", {i: (0, 0, 8, 19) for i in range(3)})
    assert _atspi._inline_link_box(_label_link(0, 3)) is None  # no words to name it by


@pytest.mark.parametrize("node", [
    _Node("push button", _Node("label"), _Link(0, 3)),
    _Node("link", None, _Link(0, 3)),
    _Node("link", _Node("label"), None),
    _Node("link", _Node("label"), _Link(5, 5)),
    _Node("link", _Node("label"), _Link(None, 3)),
])
def test_only_a_link_with_a_parent_and_a_range_is_placed(monkeypatch, node) -> None:
    _install(monkeypatch, "alpha beta", {i: (0, 0, 8, 19) for i in range(10)})
    assert _atspi._inline_link_box(node) is None


def test_a_very_long_link_reads_a_bounded_number_of_characters(monkeypatch) -> None:
    text = "x" * 5000
    seen = []

    def character(acc, offset, coord):
        seen.append(offset)
        return NS(x=offset, y=0, width=1, height=10)

    monkeypatch.setattr(_atspi, "_atspi", lambda: NS(
        CoordType=NS(SCREEN=0),
        Text=NS(get_text=lambda acc, s, e: text[s:e], get_character_extents=character),
    ))
    monkeypatch.setattr(_atspi, "_role_name", lambda node: node.role)
    monkeypatch.setattr(_atspi, "_parent_of", lambda node: node.parent)
    assert _atspi._inline_link_box(_label_link(0, 5000)) is not None
    assert len(seen) <= _atspi._INLINE_LINK_CHARS


def _visited_node(role, parent_role, toolkit, names):
    parent = NS(role=parent_role)
    states = NS(names=set(names))
    return NS(role=role, parent=parent, toolkit=toolkit, get_state_set=lambda: states)


@pytest.fixture
def visited_env(monkeypatch):
    monkeypatch.setattr(_atspi, "_role_name", lambda node: node.role)
    monkeypatch.setattr(_atspi, "_parent_of", lambda node: node.parent)
    monkeypatch.setattr(_atspi, "_toolkit_name", lambda node: node.toolkit)
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda node: None)


def test_inline_link_state_reads_visited_of_a_gtk_label_link(visited_env) -> None:
    fresh = _visited_node("link", "label", "gtk", {"SHOWING"})
    seen = _visited_node("link", "label", "gtk", {"SHOWING", "VISITED"})
    assert _atspi.gtk_inline_link_state(fresh) == (False, True)
    assert _atspi.gtk_inline_link_state(seen) == (True, True)


@pytest.mark.parametrize("role,parent,toolkit,names", [
    ("link", "document web", "gtk", {"SHOWING"}),   # a page link, not a label link
    ("link", "label", "Chromium", {"SHOWING"}),     # another toolkit
    ("push button", "label", "gtk", {"SHOWING"}),   # not a link
    ("link", "label", "gtk", set()),                # not showing
    ("link", "label", "gtk", {"SHOWING", "DEFUNCT"}),
])
def test_inline_link_state_is_none_off_a_live_gtk_label_link(visited_env, role, parent, toolkit, names) -> None:
    assert _atspi.gtk_inline_link_state(_visited_node(role, parent, toolkit, names)) is None
