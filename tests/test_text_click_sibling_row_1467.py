"""A label's actionable ancestor must not be pressed at another row (#1467).

TodoMVC lists ``<li><label>Buy milk</label></li>`` inside a ``<ul>`` that is
actionable only through a delegated dblclick listener. Promoting the label to
the list pressed the list's center, which is the "Walk dog" row: the wrong
item entered edit mode and the click reported ``confirmed``.
"""
from __future__ import annotations

from a11y_computer_use.schema import Bounds, Element
from a11y_computer_use.server import text_click_control


def _el(ref, role, bounds, parent=None, clickable=False, title=""):
    return Element(ref=ref, role=role, title=title, value=None,
                   bounds=Bounds(0, *bounds), snapshot_id="s1", parent=parent,
                   clickable=clickable)


def _todo_list():
    lst = _el("e33", "AXList", (300, 260, 680, 214), clickable=True)
    rows = [
        _el("e34", "AXListItem", (300, 260, 680, 50), parent="e33"),
        _el("e40", "AXListItem", (300, 310, 680, 50), parent="e33"),
        _el("e46", "AXListItem", (300, 360, 680, 50), parent="e33"),
    ]
    label = _el("e38", "AXStaticText", (424, 286, 84, 25), parent="e34", title="Buy milk")
    other = _el("e44", "AXStaticText", (424, 336, 84, 25), parent="e40", title="Walk dog")
    return [lst, *rows, label, other], label


def test_label_is_not_promoted_to_a_list_whose_center_is_another_row():
    elements, label = _todo_list()
    # The list's center (640,367) is inside the "Walk dog" row, not the label.
    assert text_click_control(elements, label) is None


def test_a_button_whose_center_is_on_its_label_still_stands_for_it():
    button = _el("e2", "AXButton", (100, 100, 120, 30), clickable=True, title="Save")
    span = _el("e3", "AXStaticText", (110, 105, 100, 20), parent="e2", title="Save")
    assert text_click_control([button, span], span) is button
