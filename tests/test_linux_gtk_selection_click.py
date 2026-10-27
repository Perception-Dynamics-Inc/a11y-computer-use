"""#1238/#1240: a GTK ListBox row or FlowBox tile click is judged by its selection."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from a11y_computer_use.drivers import _atspi


def _node(role, parent=None, names=("SHOWING",), index=0, selection=None, toolkit="gtk"):
    states = NS(names=set(names))
    return NS(role=role, parent=parent, toolkit=toolkit, index=index,
              get_state_set=lambda: states, get_index_in_parent=lambda: index,
              selection=selection)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    monkeypatch.setattr(_atspi, "_parent_of", lambda n: n.parent)
    monkeypatch.setattr(_atspi, "_toolkit_name", lambda n: n.toolkit)
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda n: None)
    monkeypatch.setattr(_atspi, "_selection_iface", lambda n: n.selection)


def _selection(*children):
    return NS(get_n_selected_children=lambda: len(children),
              get_selected_child=lambda i: children[i])


def test_listbox_row_reads_its_own_selected_state():
    box = _node("list box")
    row = _node("list item", box, index=2)
    label = _node("label", row)
    assert _atspi.gtk_selection_click_state(label) == (False, False)
    picked = _node("list item", box, names=("SHOWING", "SELECTED"), index=2)
    assert _atspi.gtk_selection_click_state(picked) == (True, False)


def test_flowbox_tile_reads_the_container_selection():
    table = _node("table")
    tile = _node("table cell", table, index=4)
    text = _node("label", tile)
    table.selection = _selection()
    assert _atspi.gtk_selection_click_state(text) == (False, False)
    table.selection = _selection(_node("table cell", table, index=4))
    assert _atspi.gtk_selection_click_state(text) == (False, True)
    table.selection = _selection(_node("table cell", table, index=1))
    assert _atspi.gtk_selection_click_state(text) == (False, False)


@pytest.mark.parametrize("make", [
    lambda: _node("push button", _node("list box")),                       # not an item
    lambda: _node("list item", _node("list box"), toolkit="Chromium"),     # another toolkit
    lambda: _node("list item", _node("panel")),                            # no selection container
    lambda: _node("list item", _node("list box"), names=()),               # not showing
    lambda: _node("list item", _node("list box"), names=("SHOWING", "DEFUNCT")),
])
def test_other_nodes_are_not_judged_by_selection(make):
    assert _atspi.gtk_selection_click_state(make()) is None
