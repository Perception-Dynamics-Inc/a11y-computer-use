"""#1444: a date-picker day cell whose own click closes the dialog.

The cell's click action selects the date and the page removes the grid. The
pointer fallback then finds the row identity changed. That refusal must not
claim "no input was sent" after the click action already ran, and no second
click may be sent. A recycled row before any input is still refused.
No gi import: macOS and Windows collect this file.
"""

from __future__ import annotations

import dataclasses

import pytest

from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.test_value_selection_and_chrome_state import _Node, _driver, _element

_IDENTITY = ("table cell", "15", "table", "November 2026")


class _DayCell(_Node):
    def do_action(self, index):
        self.action_log.append(self.actions[index])
        # The page sets the date and closes the dialog: the grid is gone.
        self.parent.children.remove(self)
        self.parent = None
        return True


def _picker():
    cell = _DayCell("table cell", "15", states={"ENABLED", "SELECTABLE"}, actions=["click"])
    grid = _Node("table", "November 2026", children=[cell])
    for node in (cell, grid):
        node.toolkit = "Chromium"
    return grid, cell


def _cell_element():
    element = _element("e15", "AXCell", "15", clickable=True)
    return dataclasses.replace(element, row_identity=_IDENTITY)


def _setup(monkeypatch, cell):
    driver = _driver(monkeypatch, cell)
    monkeypatch.setattr(
        _atspi, "chromium_row_identity",
        lambda handle, fresh=False: _IDENTITY if handle.parent is not None else (),
    )
    pointer: list[tuple] = []
    for name in ("click", "click_at", "mouse_click"):
        if hasattr(_linux_input, name):
            monkeypatch.setattr(_linux_input, name, lambda *a, **k: pointer.append(a))
    monkeypatch.setattr(_atspi, "_click_center", lambda *a, **k: pointer.append(a))
    return driver, pointer


def test_day_cell_click_that_closes_the_picker_is_not_reported_as_no_input(monkeypatch) -> None:
    _grid, cell = _picker()
    driver, pointer = _setup(monkeypatch, cell)
    # The cell's own action is the input; the outcome judge reads the page.
    assert driver.press_element(_cell_element()) is True
    assert cell.action_log == ["click"]
    assert pointer == []


def test_recycled_day_cell_is_still_refused_before_any_input(monkeypatch) -> None:
    _grid, cell = _picker()
    driver, pointer = _setup(monkeypatch, cell)
    monkeypatch.setattr(_atspi, "chromium_row_identity", lambda handle, fresh=False: ("table cell", "16"))
    with pytest.raises(ComputerUseError) as raised:
        driver.press_element(_cell_element())
    assert raised.value.code is ErrorCode.STALE_REF
    assert "no input was sent" in raised.value.message
    assert raised.value.detail["outcome"] == "refused"
    assert cell.action_log == []
    assert pointer == []
