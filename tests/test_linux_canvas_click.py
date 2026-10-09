"""Hermetic. A Chrome canvas or image is clicked at the center of its box.

Chrome's action click reports offset 0,0. The pointer path uses the AT-SPI
box, and the DOM box when that size is 0. A button still uses the action.
No display and no browser.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode


class _Rect:
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _App:
    def __init__(self, toolkit: str, pid: int = 4242):
        self.toolkit = toolkit
        self.pid = pid

    def get_toolkit_name(self):
        return self.toolkit

    def get_name(self):
        return self.toolkit

    def get_process_id(self):
        return self.pid

    def get_role_name(self):
        return "application"


class _Node:
    def __init__(self, role, name, *, actions=(), x=0, y=0, w=0, h=0,
                 toolkit="Chromium", attrs=None, pid=4242):
        self.role = role
        self.name = name
        self.actions = list(actions)
        self.rect = _Rect(x, y, w, h)
        self.attrs = dict(attrs or {})
        self.app = _App(toolkit, pid)
        self.action_log: list[str] = []

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_application(self):
        return self.app

    def get_process_id(self):
        return self.app.pid

    def get_parent(self):
        return None

    def get_attributes(self):
        return self.attrs

    def get_component_iface(self):
        return self

    def get_extents(self, _coord):
        return self.rect

    def get_action_iface(self):
        return self if self.actions else None

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, index):
        return self.actions[index]

    def do_action(self, index):
        self.action_log.append(self.actions[index])
        return True


class _Atspi:
    class CoordType:
        SCREEN = 0


@pytest.fixture
def atspi(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)


def _element(role: str, bounds: Bounds) -> Element:
    return Element("e9", role, "Drawing board", None, bounds, "snap", clickable=True)


def _press(node, element, monkeypatch, dom=None):
    clicks: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_input.click",
        lambda x, y, *_args, **_kwargs: clicks.append((int(x), int(y))),
    )
    seen: list[dict] = []

    def dom_box(info):
        seen.append(info)
        return dom

    monkeypatch.setattr("a11y_computer_use.drivers.linux._dom_click_box", dom_box)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: node,
    )
    node.pointer = clicks
    return driver.press_element(element), clicks, seen


def test_chrome_image_click_uses_the_center_of_the_atspi_box(atspi, monkeypatch) -> None:
    node = _Node("image", "Drawing board", actions=["click"], x=40, y=80, w=600, h=400)
    element = _element("AXImage", Bounds(0, 40, 80, 600, 400))
    ok, clicks, seen = _press(node, element, monkeypatch, dom={"screenX": 1, "screenY": 1, "width": 9, "height": 9})
    assert ok is True
    assert clicks == [(340, 280)]
    assert node.action_log == []
    assert seen == []


def test_zero_atspi_size_uses_the_dom_size_at_the_atspi_origin(atspi, monkeypatch) -> None:
    node = _Node(
        "canvas", "Drawing board", actions=["click"], x=40, y=80, w=0, h=0,
        attrs={"tag": "canvas", "id": "board"},
    )
    element = _element("AXImage", Bounds(0, 0, 0, 0, 0))
    dom = {"screenX": 9, "screenY": 9, "width": 600, "height": 400}
    ok, clicks, seen = _press(node, element, monkeypatch, dom=dom)
    assert ok is True
    assert clicks == [(340, 280)]
    assert node.action_log == []
    assert seen and seen[0]["element_id"] == "board"
    assert seen[0]["label"] == "Drawing board"
    assert seen[0]["tag"] == "canvas"


def test_zero_origin_uses_the_dom_screen_box(atspi, monkeypatch) -> None:
    node = _Node("image", "Drawing board", actions=["click"], x=0, y=0, w=0, h=0)
    element = _element("AXImage", Bounds(0, 0, 0, 0, 0))
    dom = {"screenX": 100, "screenY": 80, "width": 600, "height": 400}
    ok, clicks, _seen = _press(node, element, monkeypatch, dom=dom)
    assert ok is True
    assert clicks == [(400, 280)]
    assert node.action_log == []


def test_missing_box_is_not_reported_as_a_click(atspi, monkeypatch) -> None:
    node = _Node("image", "Drawing board", actions=["click"], x=0, y=0, w=0, h=0)
    element = _element("AXImage", Bounds(0, 0, 0, 0, 0))
    with pytest.raises(ComputerUseError) as exc:
        _press(node, element, monkeypatch, dom=None)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "click_without_coordinates"
    assert node.action_log == []
    assert node.pointer == []


def test_chrome_button_still_uses_the_action(atspi, monkeypatch) -> None:
    node = _Node("push button", "Save", actions=["click"], x=10, y=10, w=80, h=24)
    element = Element("e2", "AXButton", "Save", None, Bounds(0, 10, 10, 80, 24), "snap", clickable=True)
    ok, clicks, seen = _press(node, element, monkeypatch)
    assert ok is True
    assert clicks == []
    assert seen == []
    assert node.action_log == ["click"]


def test_gtk_image_still_uses_the_action(atspi, monkeypatch) -> None:
    node = _Node("image", "Icon", actions=["click"], x=10, y=10, w=32, h=32, toolkit="gtk")
    element = _element("AXImage", Bounds(0, 10, 10, 32, 32))
    ok, clicks, seen = _press(node, element, monkeypatch)
    assert ok is True
    assert clicks == []
    assert seen == []
    assert node.action_log == ["click"]
