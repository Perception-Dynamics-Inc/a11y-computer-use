"""Menu bar, open/save panels, and app lifecycle: hermetic on every OS.

The menu and panel logic in `a11y_computer_use.menus` runs over a small
accessor seam, so a fake tree exercises path parsing, title matching, the
level-by-level press, panel detection, and the go-to-folder drive. The
Runtime tests use a recording fake driver, so tiers, confirmation, audit, and
the launch/focus/quit waits are checked without pyobjc.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from a11y_computer_use import menus, safety, server
from a11y_computer_use.drivers import browser, linux, windows
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    FileDialogVerb,
    Scope,
    Snapshot,
)

APP = "com.apple.TextEdit"


# --------------------------------------------------------------------------- #
# A fake AX tree
# --------------------------------------------------------------------------- #
class Node:
    def __init__(self, role, title="", children=(), **attrs):
        self.attrs = {"AXRole": role, "AXTitle": title, **attrs}
        self.children = list(children)


class FakeAccessor:
    def __init__(self, *, press_ok=None):
        self.pressed: list[str] = []
        self.set: list[tuple[str, str]] = []
        self.cancels = 0
        self.press_ok = press_ok or (lambda node: True)

    def attr(self, node, name):
        return node.attrs.get(name)

    def children(self, node):
        return tuple(node.children)

    def press(self, node):
        self.pressed.append(node.attrs.get("AXTitle", ""))
        return self.press_ok(node)

    def set_value(self, node, value):
        self.set.append((node.attrs.get("AXTitle", ""), value))
        return True

    def cancel(self):
        self.cancels += 1


def item(title, **attrs):
    return Node("AXMenuItem", title, **attrs)


def menu(title, *entries):
    return Node("AXMenuItem", title, [Node("AXMenu", "", entries)])


def bar_item(title, *entries):
    return Node("AXMenuBarItem", title, [Node("AXMenu", "", entries)])


def textedit_app():
    export = menu("Export", item("Add to Render Queue…", AXMenuItemCmdChar="M",
                                 AXMenuItemCmdModifiers=1 | 4),
                  item("Export as PDF…"))
    file_menu = bar_item(
        "File",
        item("New", AXMenuItemCmdChar="N", AXMenuItemCmdModifiers=0),
        item("Open…", AXMenuItemCmdChar="O", AXMenuItemCmdModifiers=0),
        Node("AXMenuItem", ""),  # separator
        item("Save", AXEnabled=False, AXMenuItemCmdChar="S", AXMenuItemCmdModifiers=0),
        item("Save As…", AXMenuItemCmdChar="S", AXMenuItemCmdModifiers=1 | 2),
        item("Save a Copy…"),
        export,
        item("Move to Trash"),
    )
    view_menu = bar_item("View", item("Show Toolbar", AXMenuItemMarkChar="✓"))
    return Node("AXApplication", "TextEdit", [], AXMenuBar=Node("AXMenuBar", "", [file_menu, view_menu]))


# --------------------------------------------------------------------------- #
# paths and matching
# --------------------------------------------------------------------------- #
def test_parse_path_splits_on_arrows_and_trims() -> None:
    assert menus.parse_path("File > Export > Add to Render Queue") == ("File", "Export", "Add to Render Queue")
    assert menus.parse_path("File → Save") == ("File", "Save")
    with pytest.raises(ValueError):
        menus.parse_path("File > > Save")
    with pytest.raises(ValueError):
        menus.parse_path("")


def test_match_title_is_case_insensitive_and_ignores_the_ellipsis() -> None:
    titles = ["New", "Open…", "Save", "Save As…", "Save a Copy…"]
    assert menus.match_title("open", titles) == 1
    assert menus.match_title("Save As", titles) == 3
    assert menus.match_title("save", titles) == 2  # exact beats prefix
    assert menus.match_title("save a c", titles) == 4  # unique prefix
    with pytest.raises(LookupError, match="ambiguous"):
        menus.match_title("sav", ["Save", "Save As…", "Savings"])
    with pytest.raises(LookupError, match="available"):
        menus.match_title("Print", titles)


def test_shortcut_text_renders_modifier_bits() -> None:
    assert menus.shortcut_text("S", 0) == "cmd+s"
    assert menus.shortcut_text("S", 1 | 2) == "alt+shift+cmd+s"
    assert menus.shortcut_text("M", 1 | 4) == "ctrl+shift+cmd+m"
    assert menus.shortcut_text("K", 8) == "k"  # no-command bit
    assert menus.shortcut_text("", 0) is None


# --------------------------------------------------------------------------- #
# listing and pressing over the fake tree
# --------------------------------------------------------------------------- #
def test_list_top_level_and_nested_menus() -> None:
    acc, app = FakeAccessor(), textedit_app()
    assert [i.title for i in menus.list_items(acc, app, None)] == ["File", "View"]
    rows = [i.to_dict() for i in menus.list_items(acc, app, "File")]
    assert rows[0] == {"title": "New", "enabled": True, "shortcut": "cmd+n", "submenu": False, "checked": None}
    save = next(r for r in rows if r["title"] == "Save")
    assert save["enabled"] is False
    assert next(r for r in rows if r["title"] == "Export")["submenu"] is True
    assert [i.title for i in menus.list_items(acc, app, "file > export")] == [
        "Add to Render Queue…", "Export as PDF…"]
    checked = menus.list_items(acc, app, "View")[0]
    assert checked.checked is True


def test_list_an_item_that_is_not_a_menu_is_a_structured_error() -> None:
    acc, app = FakeAccessor(), textedit_app()
    with pytest.raises(ComputerUseError) as exc:
        menus.list_items(acc, app, "File > New")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND


def test_press_opens_each_level_then_the_leaf() -> None:
    acc, app = FakeAccessor(), textedit_app()
    title = menus.press_path(acc, app, "file > export > add to render queue", settle=lambda s: None)
    assert title == "Add to Render Queue…"
    assert acc.pressed == ["File", "Export", "Add to Render Queue…"]
    assert acc.cancels == 0


def test_press_of_a_disabled_item_is_refused_and_closes_open_menus() -> None:
    acc, app = FakeAccessor(), textedit_app()
    with pytest.raises(ComputerUseError) as exc:
        menus.press_path(acc, app, "File > Save", settle=lambda s: None)
    assert exc.value.code is ErrorCode.UNSUPPORTED and exc.value.detail["reason"] == "disabled"
    assert acc.pressed == ["File"] and acc.cancels == 1


def test_press_of_an_unknown_item_names_the_available_ones() -> None:
    acc, app = FakeAccessor(), textedit_app()
    with pytest.raises(ComputerUseError) as exc:
        menus.press_path(acc, app, "File > Print", settle=lambda s: None)
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "Save As…" in exc.value.detail["available"]
    assert acc.cancels == 1  # File was opened, so it is closed again


def test_press_refused_by_the_app_is_structured() -> None:
    acc = FakeAccessor(press_ok=lambda node: node.attrs["AXTitle"] != "Export")
    with pytest.raises(ComputerUseError) as exc:
        menus.press_path(acc, textedit_app(), "File > Export > Export as PDF", settle=lambda s: None)
    assert exc.value.detail["reason"] == "press_failed" and exc.value.detail["level"] == 1
    assert acc.cancels == 1


def test_no_menu_bar_is_unsupported() -> None:
    with pytest.raises(ComputerUseError) as exc:
        menus.list_items(FakeAccessor(), Node("AXApplication", "bg"), None)
    assert exc.value.code is ErrorCode.UNSUPPORTED


# --------------------------------------------------------------------------- #
# open / save panels
# --------------------------------------------------------------------------- #
def panel_app(kind: str):
    if kind == "save":
        sheet = Node("AXSheet", "Save", [
            Node("AXTextField", "", AXDescription="Save As:", AXValue="Untitled"),
            Node("AXButton", "Cancel"), Node("AXButton", "Save"),
        ])
    elif kind == "open":
        sheet = Node("AXSheet", "Open", [Node("AXButton", "Cancel"), Node("AXButton", "Open")])
    else:
        sheet = None
    window = Node("AXWindow", "Untitled", [sheet] if sheet else [Node("AXButton", "Bold")])
    return Node("AXApplication", "TextEdit", [], AXFocusedWindow=window, AXWindows=[window])


def test_find_panel_classifies_save_and_open_and_none() -> None:
    acc = FakeAccessor()
    save = menus.find_panel(acc, panel_app("save"))
    assert save is not None and save.kind is FileDialogVerb.SAVE and save.filename_field is not None
    open_ = menus.find_panel(acc, panel_app("open"))
    assert open_ is not None and open_.kind is FileDialogVerb.OPEN
    assert menus.find_panel(acc, panel_app("none")) is None


def test_drive_open_panel_uses_go_to_folder() -> None:
    acc = FakeAccessor()
    panel = menus.find_panel(acc, panel_app("open"))
    keys, typed = [], []
    result = menus.drive_panel(panel, FileDialogVerb.OPEN, "/tmp/evidence.txt", accessor=acc,
                               key=keys.append, type_text=typed.append, settle=lambda s: None)
    assert keys == ["cmd+shift+g", "return", "return"] and typed == ["/tmp/evidence.txt"]
    assert result["action"] == "open"


def test_drive_save_panel_sets_the_filename_via_ax_then_returns() -> None:
    acc = FakeAccessor()
    panel = menus.find_panel(acc, panel_app("save"))
    keys, typed = [], []
    result = menus.drive_panel(panel, FileDialogVerb.SAVE, "/Users/me/out/final.mp4", accessor=acc,
                               key=keys.append, type_text=typed.append, settle=lambda s: None)
    assert typed == ["/Users/me/out"] and acc.set == [("", "final.mp4")]
    assert keys == ["cmd+shift+g", "return", "return"]
    assert "set filename via AX" in result["steps"]


def test_drive_panel_refuses_the_wrong_kind_and_relative_paths() -> None:
    acc = FakeAccessor()
    panel = menus.find_panel(acc, panel_app("open"))
    with pytest.raises(ComputerUseError) as exc:
        menus.drive_panel(panel, FileDialogVerb.SAVE, "/x", accessor=acc, key=lambda c: None,
                          type_text=lambda t: None, settle=lambda s: None)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    with pytest.raises(ValueError):
        menus.drive_panel(panel, FileDialogVerb.OPEN, "relative.txt", accessor=acc, key=lambda c: None,
                          type_text=lambda t: None, settle=lambda s: None)


# --------------------------------------------------------------------------- #
# other drivers answer a structured unsupported
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("driver", [windows.WindowsDriver(), linux.LinuxDriver(),
                                    browser.BrowserDriver(endpoint="http://127.0.0.1:1")])
def test_other_drivers_report_unsupported_menus(driver) -> None:
    for call in (lambda: driver.menu_items("x", None), lambda: driver.menu_press("x", "File > Save"),
                 lambda: driver.file_dialog("open", "/x", "x")):
        with pytest.raises(ComputerUseError) as exc:
            call()
        assert exc.value.code is ErrorCode.UNSUPPORTED


# --------------------------------------------------------------------------- #
# Runtime: gating, confirmation, audit, app lifecycle
# --------------------------------------------------------------------------- #
class FakeDriver:
    name = "fake"
    resolves_apps = True

    def __init__(self):
        self.calls: list[tuple] = []
        self.front = APP
        self.apps = [{"app": APP, "name": "TextEdit"}]
        self.window_rows: list[dict] = []
        self.sheet = False

    def ensure_trusted(self): pass
    def frontmost_app(self): return self.front, 1
    def main_display_id(self): return 1

    def snapshot(self, scope, app):
        els = [Element(ref="e1", role="AXWindow", title="Untitled", value=None,
                       bounds=Bounds(1, 0, 0, 100, 100), snapshot_id="s", path=("AXWindow",))]
        if self.sheet:
            els.append(Element(ref="e2", role="AXSheet", title="Save?", value=None,
                               bounds=Bounds(1, 0, 0, 50, 50), snapshot_id="s", parent="e1",
                               path=("AXWindow", "AXSheet")))
        return Snapshot(snapshot_id="s", scope=scope, app=app, pid=1, created_at=0.0,
                        displays=(Display(1, 100, 100, 1.0, True),), elements=tuple(els))

    def menu_items(self, app, path):
        self.calls.append(("menu_items", app, path))
        return [{"title": "New", "enabled": True, "shortcut": "cmd+n", "submenu": False, "checked": None}]

    def menu_press(self, app, path):
        self.calls.append(("menu_press", app, path))
        return path.split(">")[-1].strip()

    def file_dialog(self, verb, path, app):
        self.calls.append(("file_dialog", verb, path, app))
        return {"action": verb, "path": path, "steps": ["go-to-folder"]}

    def running_apps(self): return list(self.apps)
    def launch_app(self, identifier): self.calls.append(("launch", identifier))
    def activate_app(self, identifier): self.calls.append(("activate", identifier)); return APP
    def windows(self): return list(self.window_rows)
    def key_chord(self, chord, **kw): self.calls.append(("key", chord))


def make_runtime(tmp_path: Path, tier=safety.Tier.FULL):
    driver = FakeDriver()
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, tier)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    return rt, driver, store


def audit_rows(tmp_path: Path):
    rows = []
    for f in sorted((tmp_path / "audit").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return rows


def test_menu_list_is_read_tier_and_press_is_click_tier(tmp_path) -> None:
    rt, driver, store = make_runtime(tmp_path, safety.Tier.READ)
    assert json.loads(rt.menu(APP, "File", action="list"))[0]["title"] == "New"
    with pytest.raises(server.ActionRefused):
        rt.menu(APP, "File > New")
    store.set_tier(APP, safety.Tier.CLICK)
    assert rt.menu(APP, "File > New") == f"pressed menu item 'New' in {APP}"
    assert ("menu_press", APP, "File > New") in driver.calls
    kinds = [(r["action"], r["result"]) for r in audit_rows(tmp_path)]
    assert ("menuop", "ok") in kinds and ("menuop", "deny") in kinds  # click needs > read


def test_menu_press_on_a_destructive_label_needs_confirmation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "CONFIRMATION_GATE", True)
    rt, driver, _ = make_runtime(tmp_path)
    with pytest.raises(ComputerUseError) as exc:
        rt.menu(APP, "File > Move to Trash")
    assert exc.value.code is ErrorCode.CONFIRMATION_DECLINED
    assert not any(c[0] == "menu_press" for c in driver.calls)
    assert rt.menu(APP, "File > Move to Trash", confirm=lambda prompt: True).startswith("pressed")


def test_menu_press_validates_the_path_before_gating(tmp_path) -> None:
    rt, _, _ = make_runtime(tmp_path)
    with pytest.raises(ValueError):
        rt.menu(APP, "File > > New")
    with pytest.raises(ValueError):
        rt.menu(APP, None)


def test_file_dialog_is_full_tier_and_returns_the_steps(tmp_path) -> None:
    rt, driver, store = make_runtime(tmp_path, safety.Tier.CLICK)
    with pytest.raises(server.ActionRefused):
        rt.file_dialog("open", "/tmp/x.txt")
    store.set_tier(APP, safety.Tier.FULL)
    result = json.loads(rt.file_dialog("save", "/tmp/out.mp4"))
    assert result["action"] == "save" and driver.calls[-1] == ("file_dialog", "save", "/tmp/out.mp4", APP)
    with pytest.raises(ValueError):
        rt.file_dialog("browse", "/tmp/x")


def test_app_quit_is_full_tier_and_reports_a_lingering_dialog(tmp_path) -> None:
    rt, driver, store = make_runtime(tmp_path, safety.Tier.CLICK)
    rt.QUIT_SETTLE_S = 0.0
    with pytest.raises(server.ActionRefused):
        rt.app("quit", APP)
    store.set_tier(APP, safety.Tier.FULL)
    driver.apps = []  # gone right after cmd+q
    assert rt.app("quit", APP) == f"quit {APP}"
    assert ("key", "cmd+q") in driver.calls
    driver.apps = [{"app": APP, "name": "TextEdit"}]
    driver.sheet = True
    assert "dialog" in rt.app("quit", APP)


def test_app_launch_and_focus_on_a_tab_driver_do_not_wait(tmp_path) -> None:
    rt, driver, _ = make_runtime(tmp_path, safety.Tier.CLICK)
    assert rt.app("launch", APP) == f"launched {APP}"
    assert rt.app("focus", APP) == f"focused {APP}"


def test_app_launch_waits_for_the_first_window_on_an_os_driver(tmp_path, monkeypatch) -> None:
    rt, driver, _ = make_runtime(tmp_path, safety.Tier.CLICK)
    driver.resolves_apps = False
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: APP)
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, APP))
    rt.APP_LAUNCH_WAIT_S = 0.6
    driver.window_rows = [{"window_id": 1, "app": "TextEdit", "title": "Untitled", "bounds": None}]
    assert rt.app("launch", "TextEdit") == "launched TextEdit; first window: 'Untitled'"
    driver.window_rows = []
    assert rt.app("launch", "TextEdit").endswith("no window appeared within 1s")


def test_app_focus_reports_when_the_app_never_comes_to_the_front(tmp_path, monkeypatch) -> None:
    rt, driver, _ = make_runtime(tmp_path, safety.Tier.CLICK)
    driver.resolves_apps = False
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.other")
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, APP))
    rt.APP_FOCUS_WAIT_S = 0.3
    assert "not frontmost yet" in rt.app("focus", APP)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: APP)
    assert rt.app("focus", APP) == f"focused {APP}"


def test_menu_and_file_dialog_are_dispatchable_by_name(tmp_path) -> None:
    rt, _, _ = make_runtime(tmp_path)
    assert rt.call_tool("menu", {"app": APP, "path": "File > New"}).startswith("pressed")
    assert json.loads(rt.dispatch("file_dialog", {"action": "open", "path": "/tmp/a"}))["action"] == "open"
