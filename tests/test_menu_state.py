"""Open-menu detection and dismissal: hermetic on every OS.

A menu left open in an app swallows typed text and clicks. The Runtime asks
the driver whether a menu is open before `type` and `click`, closes it, and
says so in the result. `key` does not: the open menu is the key target, so
arrows, Return, and alt+letter reach it. `desktop_snapshot` prints the open
menu in its header; `menu(action="state"|"close")` expose the same to the planner.
"""
from __future__ import annotations

import json
import sys

import pytest

from a11y_computer_use import menus, safety, server
from a11y_computer_use.drivers import browser, linux, windows
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Scope,
    Snapshot,
)
from tests.test_menus import FakeAccessor, Node, bar_item, item, menu

APP = "com.apple.TextEdit"


# --------------------------------------------------------------------------- #
# menus.py: open_path / close_open_menu over the fake AX tree
# --------------------------------------------------------------------------- #
def _app(open_file=False, open_export=False):
    export = menu("Export", item("Add to Render Queue…"), item("Export as PDF…"))
    if open_export:
        export.attrs["AXSelected"] = True
        export.children[0].attrs["AXSize"] = (180, 60)
    file_menu = bar_item("File", item("New"), item("Open…"), export, item("Move to Trash"))
    if open_file:
        file_menu.attrs["AXSelected"] = True
        file_menu.children[0].attrs["AXSize"] = (220, 320)
    view_menu = bar_item("View", item("Show Toolbar"))
    return Node("AXApplication", "TextEdit", [], AXMenuBar=Node("AXMenuBar", "", [file_menu, view_menu]))


def test_closed_menus_are_not_reported_open_even_though_they_list_items() -> None:
    acc = FakeAccessor()
    assert menus.open_path(acc, _app()) == []
    assert menus.close_open_menu(acc, _app()) == []
    assert acc.closed == []


def test_open_path_follows_the_selected_levels() -> None:
    acc = FakeAccessor()
    assert menus.open_path(acc, _app(open_file=True)) == ["File"]
    assert menus.open_path(acc, _app(open_file=True, open_export=True)) == ["File", "Export"]


def test_a_laid_out_menu_counts_as_open_without_axselected() -> None:
    app = _app()
    app.attrs["AXMenuBar"].children[0].children[0].attrs["AXSize"] = (220, 320)
    assert menus.open_path(FakeAccessor(), app) == ["File"]


def test_close_open_menu_closes_the_top_level_item_and_returns_the_path() -> None:
    acc = FakeAccessor()
    assert menus.close_open_menu(acc, _app(open_file=True, open_export=True)) == ["File", "Export"]
    assert acc.closed == ["File"]


def test_no_menu_bar_means_nothing_is_open() -> None:
    assert menus.open_path(FakeAccessor(), Node("AXApplication", "bg")) == []


# --------------------------------------------------------------------------- #
# Runtime: pre-action dismissal, header line, menu state/close
# --------------------------------------------------------------------------- #
class FakeDriver:
    name = "fake"
    resolves_apps = True

    def __init__(self):
        self.calls: list[tuple] = []
        self.open: list[str] = []

    def ensure_trusted(self): pass
    def frontmost_app(self): return APP, 1
    def main_display_id(self): return 1

    def snapshot(self, scope, app):
        els = [Element(ref="e1", role="AXWindow", title="Untitled", value=None,
                       bounds=Bounds(1, 0, 0, 100, 100), snapshot_id="s", path=("AXWindow",)),
               Element(ref="e2", role="AXButton", title="Save", value=None,
                       bounds=Bounds(1, 10, 10, 30, 20), snapshot_id="s", parent="e1",
                       path=("AXWindow", "AXButton"), clickable=True)]
        return Snapshot(snapshot_id="s", scope=scope, app=app, pid=1, created_at=0.0,
                        displays=(Display(1, 100, 100, 1.0, True),), elements=tuple(els))

    def menu_state(self, app):
        self.calls.append(("menu_state", app))
        return {"open": bool(self.open), "path": list(self.open)}

    def menu_close(self, app):
        self.calls.append(("menu_close", app))
        was, self.open = list(self.open), []
        return was

    def resolve_ref(self, snap, ref, *, live=None): return snap.element(ref)
    def menu_items(self, app, path): return []
    def menu_press(self, app, path): return path
    def key_chord(self, chord, **kw): self.calls.append(("key", chord))
    def type_text(self, text, **kw): self.calls.append(("type", text))
    def press_element(self, element): self.calls.append(("press", element.ref)); return True
    def click(self, target, **kw): self.calls.append(("click", target))
    def app_at_point(self, point): return APP
    def windows(self): return []
    def running_apps(self): return [{"app": APP, "name": "TextEdit"}]
    def activate_app(self, identifier): return APP


def make_runtime(tmp_path, tier=safety.Tier.FULL):
    driver = FakeDriver()
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, tier)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    return rt, driver


def _order(calls, *names):
    return [c[0] for c in calls if c[0] in names]


def test_key_reaches_an_open_menu_without_closing_it(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["Edit"]
    assert rt.key("down") == "pressed down"
    assert rt.key("return") == "pressed return"
    assert rt.key("alt+e") == "pressed alt+e"
    assert ("menu_close", APP) not in driver.calls
    assert driver.open == ["Edit"]
    assert [c for c in driver.calls if c[0] == "key"] == [("key", "down"), ("key", "return"), ("key", "alt+e")]


def test_key_still_refuses_a_different_frontmost_app(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["Edit"]
    seen = {"n": 0}
    owned = driver.frontmost_app

    def frontmost():
        # The gate sees the app that owns the menu. By the recheck a different
        # named app is in front, and the chord must not be sent.
        seen["n"] += 1
        if seen["n"] == 1:
            return owned()
        return ("firefox", 2)

    driver.frontmost_app = frontmost
    with pytest.raises(ComputerUseError) as exc:
        rt.key("down")
    assert exc.value.code is ErrorCode.FOCUS_CHANGED
    assert ("key", "down") not in driver.calls
    assert driver.open == ["Edit"]


def test_key_accepts_an_unnamed_frontmost_while_the_menu_is_open(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["Edit"]
    driver.frontmost_app = lambda: ("", 0)
    assert rt.key("down") == "pressed down"
    assert driver.open == ["Edit"]
    driver.frontmost_app = lambda: ("unknown", 0)
    assert rt.key("return") == "pressed return"


def test_type_and_click_close_an_open_menu_first(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["File"]
    assert rt.type_text("hi") == "typed 2 characters (closed open menu File first)"
    driver.open = ["View"]
    rt.desktop_snapshot(APP)
    out = rt.click(ref="e2")
    assert out.startswith("clicked e2") and "(closed open menu View first)" in out
    assert _order(driver.calls, "menu_close", "press") == ["menu_close", "menu_close", "press"]


def test_nothing_open_means_no_note_and_no_close_call(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    assert rt.key("cmd+s") == "pressed cmd+s"
    assert ("menu_close", APP) not in driver.calls


def test_snapshot_header_names_the_open_menu(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path, safety.Tier.READ)
    driver.open = ["File", "Export"]
    text = rt.desktop_snapshot(APP)
    first, second = text.splitlines()[:2]
    assert first.startswith("[s]")
    assert second.startswith("open menu: File > Export")
    driver.open = []
    assert "open menu:" not in rt.desktop_snapshot(APP)


def test_menu_state_is_read_tier_and_close_is_click_tier(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path, safety.Tier.READ)
    driver.open = ["File"]
    assert json.loads(rt.menu(APP, action="state")) == {"open": True, "path": ["File"]}
    with pytest.raises(server.ActionRefused):
        rt.menu(APP, action="close")
    rt.store.set_tier(APP, safety.Tier.CLICK)
    assert rt.menu(APP, action="close") == f"closed menu File in {APP}"
    assert rt.menu(APP, action="close") == f"no menu was open in {APP}"


def test_menu_state_and_close_are_dispatchable_by_name(tmp_path) -> None:
    rt, driver = make_runtime(tmp_path)
    driver.open = ["Edit"]
    assert json.loads(rt.call_tool("menu", {"app": APP, "action": "state"}))["path"] == ["Edit"]
    assert rt.call_tool("menu", {"app": APP, "action": "close"}).startswith("closed menu Edit")


def test_a_driver_without_menu_support_is_left_alone(tmp_path) -> None:
    class Bare(FakeDriver):
        menu_state = None  # type: ignore[assignment]
        menu_close = None  # type: ignore[assignment]

    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=Bare())
    assert rt.key("cmd+n") == "pressed cmd+n"


@pytest.mark.parametrize("driver", [
    linux.LinuxDriver(), windows.WindowsDriver(), browser.BrowserDriver(endpoint="http://127.0.0.1:1"),
], ids=["linux", "windows", "browser"])
def test_other_drivers_report_no_open_menu(driver) -> None:
    """A name with no running app.

    On Linux the Linux driver asks AT-SPI and answers ``app_not_found``,
    the same as menu list. The name is not ``x``: that string is inside
    XFCE application names, so on an XFCE session it is a running app.
    Off Linux the same driver has no AT-SPI bus, so the menu is not known
    and the call stays closed. Windows and the browser report that no
    menu is open.
    """
    if getattr(driver, "name", None) == "linux" and sys.platform.startswith("linux"):
        missing = "no-such-app-a11y"
        for call in (
            lambda: driver.menu_state(missing),
            lambda: driver.menu_close(missing),
        ):
            with pytest.raises(ComputerUseError) as exc:
                call()
            assert exc.value.code is ErrorCode.APP_NOT_FOUND
            assert exc.value.detail["app"] == missing
            assert "no running application matches" in exc.value.message
        return
    assert driver.menu_state("x") == {"open": False, "path": []}
    assert driver.menu_close("x") == []
