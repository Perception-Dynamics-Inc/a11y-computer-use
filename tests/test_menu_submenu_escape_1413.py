"""#1413: a click on an item inside the open menu chain must not send Escape.

Escape before the click closed the ARIA menubar submenu that held the target,
and the call then said ``target_vanished ... nothing was sent`` although
Escape had been delivered.
"""
from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _linux_menus
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.test_menu_state import APP, make_runtime


def test_click_inside_open_menu_does_not_send_escape(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["File", "Recent"]
    driver.menu_escape_safe = lambda _app: True
    driver.target_vanished = lambda _element: False
    driver.menu_contains_target = lambda _app, _element: True
    rt.desktop_snapshot(APP)
    out = rt.click(ref="e2")
    assert str(out).startswith("clicked e2")
    assert "closed open menu" not in str(out)
    assert ("menu_close", APP) not in driver.calls
    assert ("press", "e2") in driver.calls


def test_click_outside_open_menu_still_closes_it(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["File"]
    driver.menu_escape_safe = lambda _app: True
    driver.target_vanished = lambda _element: False
    driver.menu_contains_target = lambda _app, _element: False
    rt.desktop_snapshot(APP)
    out = rt.click(ref="e2")
    assert "(closed open menu File first)" in str(out)
    assert ("menu_close", APP) in driver.calls


def test_unknown_containment_refuses_before_any_key(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["File", "Recent"]
    driver.menu_escape_safe = lambda _app: True
    driver.menu_contains_target = lambda _app, _element: None
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as exc:
        rt.click(ref="e2")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "menu_dismiss_blocked"
    assert "Escape was not sent" in exc.value.message
    assert ("menu_close", APP) not in driver.calls
    assert not any(call[0] in {"press", "click"} for call in driver.calls)


def test_vanished_after_escape_reports_the_key_was_sent(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["File"]
    driver.menu_escape_safe = lambda _app: True
    driver.target_vanished = lambda _element: True
    driver.menu_contains_target = lambda _app, _element: False
    rt.desktop_snapshot(APP)
    with pytest.raises(ComputerUseError) as exc:
        rt.click(ref="e2")
    assert exc.value.detail["reason"] == "target_vanished"
    assert "nothing was sent" not in exc.value.message
    assert "nothing was sent" not in exc.value.detail["evidence"]
    assert exc.value.detail["sent"] == ["Escape"]
    assert not any(call[0] in {"press", "click"} for call in driver.calls)


class _Acc:
    def __init__(self, role, parent=None):
        self.role = role
        self.parent = parent

    def get_parent(self):
        return self.parent


def test_target_in_menu_bar_follows_ancestors(monkeypatch) -> None:
    page = _Acc("document web")
    bar = _Acc("menu bar", page)
    opener_li = _Acc("list item", bar)
    submenu = _Acc("menu", opener_li)
    recent_item = _Acc("menu item", submenu)
    outside = _Acc("push button", page)
    monkeypatch.setattr(_linux_menus, "find_menu_bar", lambda _app: bar)
    assert _linux_menus.target_in_menu_bar(object(), recent_item) is True
    assert _linux_menus.target_in_menu_bar(object(), outside) is False
    monkeypatch.setattr(_linux_menus, "find_menu_bar", lambda _app: None)
    assert _linux_menus.target_in_menu_bar(object(), recent_item) is False
