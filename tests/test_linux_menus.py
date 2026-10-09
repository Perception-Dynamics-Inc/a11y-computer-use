"""Linux menu bars over a fake AT-SPI tree. No gi, no bus, no display.

The shapes are the ones GTK publishes: a bar entry is role ``menu``, and its
entries are either direct children or the children of one popup. ``file_dialog``
stays an explicit unsupported result.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _linux_menus
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope


class Acc:
    def __init__(self, role, name="", children=(), states=None, actions=("click",), key=""):
        self.role = role
        self.name = name
        self.children = list(children)
        self.states = None if states is None else set(states)
        self.actions = list(actions)
        self.key = key
        self.pressed: list[str] = []

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_action_iface(self):
        return _Action(self)


class _Action:
    def __init__(self, node: Acc) -> None:
        self.node = node

    def get_n_actions(self):
        return len(self.node.actions)

    def get_action_name(self, index):
        return self.node.actions[index]

    def get_key_binding(self, index):
        return self.node.key

    def do_action(self, index):
        self.node.pressed.append(self.node.actions[index])
        return True


def _item(name, *, states=("sensitive", "enabled"), key="", role="menu item"):
    return Acc(role, name, states=states, key=key)


def _menu(name, *children, states=("sensitive", "enabled")):
    return Acc("menu", name, children, states=states)


def mousepad():
    """Direct children, the flattened GTK shape, plus one nested popup."""
    recent = _menu(
        "Recent",
        Acc("menu", "", [_item("notes.txt")], states=("sensitive", "enabled")),
    )
    file_menu = _menu(
        "File",
        _item("New", key="n;<Alt>f:n;<Primary>n"),
        _item("Open…", key="<Primary><Shift>O"),
        Acc("separator", "separator"),
        _item("Save", states={"enabled"}, key="<Control>s"),
        recent,
    )
    view = _menu(
        "View",
        _item("Toolbar", role="check menu item", states=("sensitive", "enabled", "checked")),
    )
    bar = Acc("menu bar", "", [file_menu, view])
    frame = Acc("frame", "Mousepad", [bar], states={"active"})
    return Acc("application", "mousepad", [frame]), file_menu


def named_popup_app():
    """Items live in one child menu that reuses the parent's name."""
    popup = Acc(
        "menu", "File",
        [_item("New", key="<Control>n"), _item("Save", key="<Control>s")],
        states={"sensitive", "enabled"},
    )
    file_menu = _menu("File", popup)
    bar = Acc("menu bar", "", [file_menu])
    return Acc("application", "gedit", [Acc("frame", "gedit", [bar], states={"active"})])


def test_shortcut_binding_renders_gtk_tags() -> None:
    assert _linux_menus.shortcut_from_binding("<Control>n") == "ctrl+n"
    assert _linux_menus.shortcut_from_binding("<Primary><Shift>O") == "ctrl+shift+o"
    assert _linux_menus.shortcut_from_binding("<Alt>F4") == "alt+f4"
    assert _linux_menus.shortcut_from_binding("<Shift><Control>s") == "ctrl+shift+s"
    assert _linux_menus.shortcut_from_binding("n;37;4") == "n"
    assert _linux_menus.shortcut_from_binding("") is None
    assert _linux_menus.shortcut_from_binding("0;0;0") is None
    # Mousepad New: mnemonic path, then the Primary accelerator. Not alt+f:n.
    assert _linux_menus.shortcut_from_binding("n;<Alt>f:n;<Primary>n") == "ctrl+n"
    assert _linux_menus.shortcut_from_binding("s;<Control>s;s") == "ctrl+s"
    # Bare named accelerators beat the leading mnemonic letter. Not f, c, or d.
    assert _linux_menus.shortcut_from_binding("f;<Alt>v:f;F11") == "f11"
    assert _linux_menus.shortcut_from_binding("c;<Alt>h:c;F1") == "f1"
    assert _linux_menus.shortcut_from_binding("d;<Alt>e:d;Delete") == "delete"


def test_list_uses_a_bare_named_accelerator_not_the_mnemonic_letter() -> None:
    file_menu = _menu("File", _item("New", key="n;<Alt>f:n;<Primary>n"))
    edit = _menu("Edit", _item("Delete Selection", key="d;<Alt>e:d;Delete"))
    view = _menu("View", _item("Fullscreen", key="f;<Alt>v:f;F11"))
    help_menu = _menu("Help", _item("Contents", key="c;<Alt>h:c;F1"))
    bar = Acc("menu bar", "", [file_menu, edit, view, help_menu])
    app = Acc("application", "mousepad", [Acc("frame", "Mousepad", [bar], states={"active"})])

    def listed(menu: str, title: str) -> str | None:
        rows = _linux_menus.menu_items(app, menu)
        return next(row["shortcut"] for row in rows if row["title"] == title)

    assert listed("File", "New") == "ctrl+n"
    assert listed("View", "Fullscreen") == "f11"
    assert listed("Help", "Contents") == "f1"
    assert listed("Edit", "Delete Selection") == "delete"


def test_list_reads_a_gtk_menu_without_pressing_it() -> None:
    app, file_menu = mousepad()
    assert [row["title"] for row in _linux_menus.menu_items(app, None)] == ["File", "View"]
    rows = _linux_menus.menu_items(app, "File")
    assert [row["title"] for row in rows] == ["New", "Open…", "Save", "Recent"]
    assert rows[0]["shortcut"] == "ctrl+n" and rows[0]["enabled"] is True
    assert rows[1]["shortcut"] == "ctrl+shift+o"
    save = next(row for row in rows if row["title"] == "Save")
    assert save["enabled"] is False and save["shortcut"] == "ctrl+s"
    assert next(row for row in rows if row["title"] == "Recent")["submenu"] is True
    assert file_menu.pressed == []
    recent = _linux_menus.menu_items(app, "file > recent")
    assert [row["title"] for row in recent] == ["notes.txt"]
    checked = _linux_menus.menu_items(app, "View")[0]
    assert checked["title"] == "Toolbar" and checked["checked"] is True


def test_qt_checkable_menu_item_reports_checked_true_or_false() -> None:
    """A Qt QAction stays role ``menu item`` and adds the CHECKABLE state.

    Off is false, on is true. Qt 6.4 sets CHECKED and not CHECKABLE, so an
    on item is still true. A plain item in the same menu stays None, and a
    GTK check menu item is unchanged.
    """
    plain = _item("Do Thing")
    off = _item("Option X", states=("sensitive", "enabled", "checkable"))
    on = _item("Option X", states=("sensitive", "enabled", "checkable", "checked"))
    qt64 = _item("Option X", states=("sensitive", "enabled", "checked"))
    gtk = _item("Toolbar", role="check menu item", states=("sensitive", "enabled", "checked"))

    def listed(item):
        bar = Acc("menu bar", "", [_menu("Tools", plain, item)])
        app = Acc("application", "qtx", [Acc("frame", "qtx", [bar], states={"active"})])
        rows = _linux_menus.menu_items(app, "Tools")
        return next(row for row in rows if row["title"] == item.name)

    assert listed(off)["checked"] is False
    assert listed(on)["checked"] is True
    assert listed(qt64)["checked"] is True
    assert listed(plain)["checked"] is None
    assert listed(gtk)["checked"] is True
    assert _linux_menus._checked(plain) is None


def test_named_popup_child_is_the_menu_not_an_extra_item() -> None:
    app = named_popup_app()
    assert [row["title"] for row in _linux_menus.menu_items(app, "File")] == ["New", "Save"]


def test_ellipsis_and_prefix_use_the_shared_matcher() -> None:
    app, _file_menu = mousepad()
    assert _linux_menus.menu_press(app, "file > open", settle=lambda _s: None) == "Open…"


def test_press_opens_each_level_then_the_leaf() -> None:
    app, file_menu = mousepad()
    title = _linux_menus.menu_press(app, "File > Recent > notes.txt", settle=lambda _s: None)
    assert title == "notes.txt"
    assert file_menu.pressed == ["click"]


def test_press_of_a_disabled_item_does_not_activate_it() -> None:
    """Escape closes the menu that was opened. A second click does not."""
    app, file_menu = mousepad()
    sent: list[str] = []

    class _Open(_Action):
        def do_action(self, index):
            self.node.pressed.append(self.node.actions[index])
            self.node.states.add("selected")
            return True

    file_menu.get_action_iface = lambda: _Open(file_menu)  # type: ignore[method-assign]

    def dismiss() -> None:
        sent.append("escape")
        file_menu.states.discard("selected")

    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(app, "File > Save", settle=lambda _s: None, dismiss=dismiss)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "disabled"
    assert "menu_still_open" not in exc.value.detail.get("reason", "")
    save = next(child for child in file_menu.children if child.name == "Save")
    assert save.pressed == []
    assert file_menu.pressed == ["click"]
    assert sent == ["escape"]
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}


def test_press_of_a_disabled_item_reports_a_menu_that_stays_open() -> None:
    app, file_menu = mousepad()
    file_menu.states.add("selected")
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(
            app, "File > Save", settle=lambda _s: None, dismiss=lambda: None,
        )
    assert exc.value.detail["reason"] == "menu_still_open"
    assert exc.value.detail["open"] == ["File"]
    save = next(child for child in file_menu.children if child.name == "Save")
    assert save.pressed == []


def test_unknown_item_names_what_is_available() -> None:
    app, file_menu = mousepad()
    sent: list[str] = []
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(
            app, "File > Print", settle=lambda _s: None, dismiss=lambda: sent.append("escape"),
        )
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "Save" in exc.value.detail["available"]
    assert file_menu.pressed == ["click"]
    assert sent == ["escape"]


def test_no_menu_bar_is_unsupported() -> None:
    app = Acc("application", "busybox", [Acc("frame", "busybox", [Acc("push button", "Go")])])
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_items(app, None)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert "menu bar" in exc.value.message


def test_active_frame_menu_bar_wins() -> None:
    idle = Acc("frame", "other", [Acc("menu bar", "", [_menu("Help")])], states=set())
    current = Acc("frame", "Mousepad", [Acc("menu bar", "", [_menu("File", _item("New"))])], states={"active"})
    app = Acc("application", "mousepad", [idle, current])
    assert [row["title"] for row in _linux_menus.menu_items(app, None)] == ["File"]


def test_listing_a_leaf_does_not_activate_it() -> None:
    app, file_menu = mousepad()
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_items(app, "File > New")
    assert "not a menu" in exc.value.message
    new = next(child for child in file_menu.children if child.name == "New")
    assert new.pressed == [] and file_menu.pressed == []


def test_empty_menu_is_opened_to_list_and_then_closed() -> None:
    file_menu = _menu("File")

    class _Fill(_Action):
        def do_action(self, index):
            self.node.pressed.append("click")
            if not self.node.children:
                self.node.children = [_item("New"), _item("Open"), _item("Save")]
                self.node.states.add("selected")
            else:
                self.node.states.discard("selected")
            return True

    file_menu.get_action_iface = lambda: _Fill(file_menu)  # type: ignore[method-assign]
    bar = Acc("menu bar", "", [file_menu])
    app = Acc("application", "mousepad", [Acc("frame", "Mousepad", [bar], states={"active"})])
    rows = _linux_menus.menu_items(app, "File", settle=lambda _s: None)
    assert [row["title"] for row in rows] == ["New", "Open", "Save"]
    assert file_menu.pressed == ["click", "click"]
    assert "selected" not in file_menu.states


def test_a_visible_menu_label_is_not_an_open_menu() -> None:
    app, file_menu = mousepad()
    file_menu.states.add("showing")
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}


def test_open_menu_state_and_close() -> None:
    app, file_menu = mousepad()
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}
    file_menu.states.add("selected")
    popup = Acc("popup menu", "", [], states={"showing"})
    file_menu.children.append(popup)
    assert _linux_menus.menu_state(app) == {"open": True, "path": ["File"]}
    sent: list[str] = []

    def escape() -> None:
        sent.append("escape")
        file_menu.states.discard("selected")
        popup.states.discard("showing")

    assert _linux_menus.menu_close(app, dismiss=escape, settle=lambda _s: None) == ["File"]
    assert sent == ["escape"]
    assert file_menu.pressed == []
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}


def test_close_fails_when_the_menu_stays_open() -> None:
    app, file_menu = mousepad()
    file_menu.states.add("selected")

    def repress() -> None:
        # The old close path: click the bar entry and leave it selected.
        file_menu.pressed.append("click")

    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_close(app, dismiss=repress, settle=lambda _s: None)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "menu_still_open"
    assert _linux_menus.menu_state(app) == {"open": True, "path": ["File"]}


def test_close_when_nothing_is_open_does_not_dismiss() -> None:
    app, _file_menu = mousepad()
    sent: list[str] = []
    assert _linux_menus.menu_close(app, dismiss=lambda: sent.append("escape"), settle=lambda _s: None) == []
    assert sent == []


def test_driver_lists_and_presses_the_fake_application(monkeypatch) -> None:
    app, file_menu = mousepad()

    def find_root(name, scope):
        assert scope is Scope.APP
        return app if name == "mousepad" else None

    monkeypatch.setattr("a11y_computer_use.drivers._atspi.find_root", find_root)
    monkeypatch.setattr(_linux_menus, "_settle", lambda _seconds: None)
    driver = LinuxDriver()
    assert [row["title"] for row in driver.menu_items("mousepad", "File")][0] == "New"
    assert driver.menu_press("mousepad", "File > New") == "New"
    new = next(child for child in file_menu.children if child.name == "New")
    assert new.pressed == ["click"]
    with pytest.raises(ComputerUseError) as exc:
        driver.menu_items("no-such-app", None)
    assert exc.value.code is ErrorCode.APP_NOT_FOUND


def test_driver_close_sends_escape_and_fails_if_the_menu_stays_open(monkeypatch) -> None:
    app, file_menu = mousepad()
    file_menu.states.add("selected")
    sent: list[str] = []

    def find_root(name, scope):
        assert scope is Scope.APP
        return app if name == "mousepad" else None

    def press_chord(chord: str) -> None:
        sent.append(chord)

    monkeypatch.setattr("a11y_computer_use.drivers._atspi.find_root", find_root)
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.press_chord", press_chord)
    monkeypatch.setattr(_linux_menus, "_settle", lambda _seconds: None)
    driver = LinuxDriver()
    with pytest.raises(ComputerUseError) as exc:
        driver.menu_close("mousepad")
    assert sent == ["escape"]
    assert exc.value.detail["reason"] == "menu_still_open"
    assert driver.menu_state("mousepad") == {"open": True, "path": ["File"]}
    assert file_menu.pressed == []

    def press_and_clear(chord: str) -> None:
        sent.append(chord)
        file_menu.states.discard("selected")

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.press_chord", press_and_clear)
    assert driver.menu_close("mousepad") == ["File"]
    assert sent[-1] == "escape"
    assert driver.menu_state("mousepad") == {"open": False, "path": []}


def test_close_on_wayland_does_not_report_success(monkeypatch) -> None:
    app, file_menu = mousepad()
    file_menu.states.add("selected")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(_linux_menus, "_settle", lambda _seconds: None)
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_close(app)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert _linux_menus.menu_state(app) == {"open": True, "path": ["File"]}
    assert file_menu.pressed == []


def test_missing_app_is_app_not_found_for_snapshot_and_menus(monkeypatch) -> None:
    """No AT-SPI application is the same answer menu list already gives.

    An empty tree of a running app is not this case. A missing bus is not
    this case either: that stays closed, covered by the next test.
    Synthetic find_root, not a live desktop.
    """

    def find_root(name, scope):
        assert name == "xfce4-terminal"
        return None

    monkeypatch.setattr("a11y_computer_use.drivers._atspi.find_root", find_root)
    driver = LinuxDriver()
    for call in (
        lambda: driver.snapshot(Scope.WINDOW, "xfce4-terminal"),
        lambda: driver.menu_state("xfce4-terminal"),
        lambda: driver.menu_close("xfce4-terminal"),
        lambda: driver.menu_items("xfce4-terminal", None),
    ):
        with pytest.raises(ComputerUseError) as exc:
            call()
        assert exc.value.code is ErrorCode.APP_NOT_FOUND
        assert exc.value.detail["app"] == "xfce4-terminal"
        assert "activated" not in exc.value.message


def test_menu_state_without_a_bus_does_not_raise(monkeypatch) -> None:
    def boom(name, scope):
        raise ImportError("no gi")

    monkeypatch.setattr("a11y_computer_use.drivers._atspi.find_root", boom)
    driver = LinuxDriver()
    assert driver.menu_state("mousepad") == {"open": False, "path": []}
    assert driver.menu_close("mousepad") == []
    with pytest.raises(ComputerUseError) as exc:
        driver.menu_items("mousepad", None)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "missing_dependency"
    assert "System Settings" not in exc.value.detail["hint"]
    assert "python3-gi" in exc.value.detail["hint"]


def test_file_dialog_names_the_linux_limit() -> None:
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().file_dialog("open", "/tmp/notes.txt", "mousepad")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert "not supported on Linux" in exc.value.message
    assert "GTK" in exc.value.message and "portal" in exc.value.message
    hint = exc.value.detail["hint"]
    assert "Ctrl+L" in hint and "set_value" in hint
    assert "press the item by ref" not in hint
    assert exc.value.detail["reason"] == "no_file_dialog"
