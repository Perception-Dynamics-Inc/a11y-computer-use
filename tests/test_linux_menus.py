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
        # These fixtures model available entries, including closed popup rows.
        self.states = None if states is None else set(states) | {"visible"}
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


def test_shortcut_binding_keeps_digit_accelerators() -> None:
    """#1177: GTK3 View > Small reports ``s;<Alt>v:s;<Primary>1``.

    A digit key with a modifier is a real chord (Ctrl+1, Ctrl+0, Alt+1).
    Only a bare number, as in keycode-style bindings, is not a key.
    """
    assert _linux_menus.shortcut_from_binding("s;<Alt>v:s;<Primary>1") == "ctrl+1"
    assert _linux_menus.shortcut_from_binding("<Primary>0") == "ctrl+0"
    assert _linux_menus.shortcut_from_binding("<Alt>1") == "alt+1"
    assert _linux_menus.shortcut_from_binding("<Primary><Shift>2") == "ctrl+shift+2"
    assert _linux_menus.shortcut_from_binding("n;37;4") == "n"
    assert _linux_menus.shortcut_from_binding("0;0;0") is None

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
    assert _linux_menus.shortcut_from_binding("s;<Alt>e:s;<Control>s") == "ctrl+s"
    # The accelerator field is empty or only the mnemonic letter. Not alt+f, not f, not s.
    assert _linux_menus.shortcut_from_binding("f;<Alt>f;") is None
    assert _linux_menus.shortcut_from_binding("_F;;") is None
    assert _linux_menus.shortcut_from_binding("a;<Alt>v:a;") is None
    assert _linux_menus.shortcut_from_binding("<Alt>f;<Alt>f;") is None
    assert _linux_menus.shortcut_from_binding("s;<Control>s;s") is None
    assert _linux_menus.mnemonic_from_binding("f;<Alt>f;") == "f"
    assert _linux_menus.mnemonic_from_binding("_F;;") == "f"
    assert _linux_menus.mnemonic_from_binding("a;<Alt>v:a;") == "a"
    assert _linux_menus.mnemonic_from_binding("<Alt>f;<Alt>f;") == "f"
    assert _linux_menus.mnemonic_from_binding("n;<Alt>f:n;<Primary>n") == "n"
    assert _linux_menus.mnemonic_from_binding("<Primary>n") is None
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
    status = _item("Statusbar", role="check menu item", key="a;<Alt>v:a;")
    view.children.append(status)
    rows = _linux_menus.menu_items(app, "View")
    bar_entry = next(row for row in rows if row["title"] == "Statusbar")
    assert bar_entry["shortcut"] is None
    assert bar_entry["mnemonic"] == "a"


def test_mnemonic_binding_is_not_listed_as_a_shortcut() -> None:
    """``f;<Alt>f;`` and ``_F;;`` are the underlined letter, not a shortcut."""
    file_menu = _menu("File", _item("New", key="n;<Alt>f:n;<Primary>n"))
    file_menu.key = "f;<Alt>f;"
    label = _menu("Format", _item("Bold", key="_B;;"))
    label.key = "_F;;"
    bar = Acc("menu bar", "", [file_menu, label])
    app = Acc("application", "mousepad", [Acc("frame", "Mousepad", [bar], states={"active"})])
    rows = {row["title"]: row for row in _linux_menus.menu_items(app, None)}
    assert rows["File"]["shortcut"] is None and rows["File"]["mnemonic"] == "f"
    assert rows["Format"]["shortcut"] is None and rows["Format"]["mnemonic"] == "f"
    new = _linux_menus.menu_items(app, "File")[0]
    assert new["shortcut"] == "ctrl+n" and new["mnemonic"] == "n"
    bold = _linux_menus.menu_items(app, "Format")[0]
    assert bold["shortcut"] is None and bold["mnemonic"] == "b"


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


def test_menu_state_drops_the_event_cache_before_the_read(monkeypatch) -> None:
    """A check bit served from the event cache is read again.

    Qt's checkable item has no ``.states`` set. ``get_state_set`` would
    otherwise keep CHECKED from before the action. The drop is one node,
    and only while the listener is up.
    """
    from a11y_computer_use.drivers import _atspi_events

    class Item:
        def __init__(self) -> None:
            self.drops = 0
            self.on = False

        def get_role_name(self) -> str:
            return "menu item"

        def clear_cache_single(self) -> None:
            self.drops += 1
            self.on = True

        def get_state_set(self):
            on = self.on

            class States:
                def contains(self, _member: object) -> bool:
                    return on

            return States()

    item = Item()
    monkeypatch.setattr(_atspi_events, "listening", lambda: False)
    assert _linux_menus._checked(item) is None
    assert item.drops == 0
    monkeypatch.setattr(_atspi_events, "listening", lambda: True)
    assert _linux_menus._checked(item) is True
    assert item.drops == 2


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


def libreoffice():
    """Impress and Writer bar entries: role menu, one action named ''.

    Leaves publish ``click``. Nested menus (Writer's Text under Format) use
    the same unnamed action. Fake tree, not a live soffice process.
    """
    outline = _item("Outline", role="radio menu item")
    normal = _item("Normal", role="radio menu item")
    view = Acc("menu", "View", [normal, outline], states=("sensitive", "enabled"), actions=("",))
    new_slide = _item("New Slide")
    slide = Acc("menu", "Slide", [new_slide], states=("sensitive", "enabled"), actions=("",))
    uppercase = _item("UPPERCASE")
    text = Acc("menu", "Text", [uppercase], states=("sensitive", "enabled"), actions=("",))
    # A sibling item keeps Text from being read as Format's sole popup.
    bold = _item("Bold")
    fmt = Acc("menu", "Format", [bold, text], states=("sensitive", "enabled"), actions=("",))
    bar = Acc("menu bar", "", [view, slide, fmt])
    frame = Acc("frame", "Untitled 1 - LibreOffice Impress", [bar], states={"active"})
    app = Acc("application", "soffice", [frame])
    return app, view, slide, text, fmt, outline, new_slide, uppercase


def test_libreoffice_menu_bar_presses_its_single_unnamed_action() -> None:
    """do_action(0) opens a menu whose only AT-SPI action is unnamed.

    Listing still does not press. A path through those menus presses the
    empty action on each menu role and ``click`` on the leaf.
    """
    app, view, slide, text, fmt, outline, new_slide, uppercase = libreoffice()
    assert [row["title"] for row in _linux_menus.menu_items(app, None)] == ["View", "Slide", "Format"]
    assert [row["title"] for row in _linux_menus.menu_items(app, "View")] == ["Normal", "Outline"]
    assert view.pressed == [] and slide.pressed == [] and fmt.pressed == []

    assert _linux_menus.menu_press(app, "View", settle=lambda _s: None) == "View"
    assert view.pressed == [""]

    assert _linux_menus.menu_press(app, "View > Outline", settle=lambda _s: None) == "Outline"
    assert view.pressed == ["", ""]
    assert outline.pressed == ["click"]

    assert _linux_menus.menu_press(app, "Slide > New Slide", settle=lambda _s: None) == "New Slide"
    assert slide.pressed == [""]
    assert new_slide.pressed == ["click"]

    assert _linux_menus.menu_press(app, "Format > Text > UPPERCASE", settle=lambda _s: None) == "UPPERCASE"
    assert fmt.pressed == [""]
    assert text.pressed == [""]
    assert uppercase.pressed == ["click"]


def test_unnamed_action_is_not_pressed_outside_a_single_menu() -> None:
    """The empty-action fallback is role menu and exactly one action."""
    leaf = Acc("menu item", "Outline", states=("sensitive", "enabled"), actions=("",))
    view = _menu("View", leaf)
    bar = Acc("menu bar", "", [view])
    leaf_app = Acc("application", "soffice", [Acc("frame", "Impress", [bar], states={"active"})])
    with pytest.raises(ComputerUseError) as leaf_exc:
        _linux_menus.menu_press(
            leaf_app, "View > Outline", settle=lambda _s: None, dismiss=lambda: None,
        )
    assert leaf_exc.value.detail["reason"] == "press_failed"
    assert leaf_exc.value.detail["level"] == 1
    assert leaf.pressed == []
    assert view.pressed == ["click"]

    ambiguous = Acc(
        "menu", "View", [_item("Outline")],
        states=("sensitive", "enabled"), actions=("", "expand"),
    )
    ambiguous_bar = Acc("menu bar", "", [ambiguous])
    ambiguous_app = Acc(
        "application", "soffice",
        [Acc("frame", "Impress", [ambiguous_bar], states={"active"})],
    )
    with pytest.raises(ComputerUseError) as menu_exc:
        _linux_menus.menu_press(ambiguous_app, "View", settle=lambda _s: None)
    assert menu_exc.value.detail["reason"] == "press_failed"
    assert menu_exc.value.detail["level"] == 0
    assert ambiguous.pressed == []

    named = Acc(
        "menu", "Format", [_item("Bold")],
        states=("sensitive", "enabled"), actions=("", "click"),
    )
    named_bar = Acc("menu bar", "", [named])
    named_app = Acc(
        "application", "soffice",
        [Acc("frame", "Writer", [named_bar], states={"active"})],
    )
    assert _linux_menus.menu_press(named_app, "Format", settle=lambda _s: None) == "Format"
    assert named.pressed == ["click"]


def test_classify_leaf_press_treats_a_timeout_as_dispatched() -> None:
    assert _linux_menus.classify_leaf_press(True, failed=False, elapsed=0.05) == "ok"
    assert _linux_menus.classify_leaf_press(False, failed=True, elapsed=0.40) == "dispatched"
    assert _linux_menus.classify_leaf_press(False, failed=False, elapsed=0.40) == "dispatched"
    assert _linux_menus.classify_leaf_press(False, failed=False, elapsed=0.05) == "refused"
    assert _linux_menus.classify_leaf_press(False, failed=True, elapsed=0.05) == "refused"


def test_leaf_timeout_waits_for_the_dialog_and_a_quick_refusal_still_fails() -> None:
    """A slow leaf is not 'the app refused'. A quick false still is."""
    app, file_menu = mousepad()
    seen: dict[str, object] = {}

    def dispatch(node):
        seen["node"] = node.name
        return "dispatched"

    def await_dialog(root, *, dispatched: bool) -> None:
        seen["dispatched"] = dispatched
        seen["root"] = root

    title = _linux_menus.menu_press(
        app, "File > New", settle=lambda _s: None,
        dispatch=dispatch, await_dialog=await_dialog,
    )
    assert title == "New"
    assert seen == {"node": "New", "dispatched": True, "root": app}
    assert file_menu.pressed == ["click"]
    new = next(child for child in file_menu.children if child.name == "New")
    assert new.pressed == []

    def refuse(_node):
        return "refused"

    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(
            app, "File > Open…", settle=lambda _s: None, dismiss=lambda: None, dispatch=refuse,
        )
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "press_failed"


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


@pytest.mark.parametrize("opened", [False, True])
@pytest.mark.parametrize("operation", ["list", "list_path", "press"])
def test_header_bar_menu_refusal_explains_snapshot_route(opened, operation) -> None:
    button = Acc("push button", "Menu", states={"checked"} if opened else set())
    leaf = Acc("push button", "Preferences")
    checkbox = Acc("check box", "Slideshow")
    children = [Acc("panel", "", [button])]
    if opened:
        children.append(Acc("panel", "", [leaf, checkbox], states={"showing"}))
    app = Acc("application", "eog", [Acc("frame", "image.png", children, states={"active"})])
    dismissed = []
    with pytest.raises(ComputerUseError) as exc:
        if operation == "press":
            _linux_menus.menu_press(app, "Preferences", dismiss=lambda: dismissed.append(True))
        else:
            _linux_menus.menu_items(app, "Menu" if operation == "list_path" else None)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    hint = exc.value.detail["hint"]
    assert "Menu or Main Menu button" in hint
    assert "desktop_snapshot" in hint
    assert "click its snapshot ref" in hint
    assert "button/checkbox refs" in hint
    assert "already open" in hint
    assert "menu paths do not support these popovers" in hint
    assert "Escape can quit" in hint
    assert button.pressed == leaf.pressed == checkbox.pressed == []
    assert dismissed == []


def test_active_frame_menu_bar_wins() -> None:
    idle = Acc("frame", "other", [Acc("menu bar", "", [_menu("Help")])], states=set())
    current = Acc("frame", "Mousepad", [Acc("menu bar", "", [_menu("File", _item("New"))])], states={"active"})
    app = Acc("application", "mousepad", [idle, current])
    assert [row["title"] for row in _linux_menus.menu_items(app, None)] == ["File"]


class _Fresh:
    """A new proxy on every ``get_child_at_index``, as PyGObject does.

    The previous wrapper is collected when the walk drops it. On CPython the
    next proxy can be allocated at that same ``id()``.
    """

    __slots__ = ("role", "name", "states", "spec", "actions", "key", "pressed", "log")

    def __init__(self, role, name, spec=(), states=(), log=None):
        self.role = role
        self.name = name
        self.spec = tuple(spec)
        self.states = set(states) | {"visible"}
        self.actions = ["click"]
        self.key = ""
        self.pressed = []
        self.log = [] if log is None else log

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.spec)

    def get_child_at_index(self, index):
        role, name, spec, states = self.spec[index]
        return _Fresh(role, name, spec, states, self.log)

    def get_action_iface(self):
        return self

    def get_n_actions(self):
        return 1

    def get_action_name(self, index):
        return "click"

    def get_key_binding(self, index):
        return ""

    def do_action(self, index):
        self.log.append(self.name)
        return True


def _spec(role, name, children=(), states=()):
    return (role, name, tuple(children), tuple(states))


def _writer_app():
    """Writer's bar sits under panels, beside a toolbar that allocates leaves.

    An idle frame publishes a different bar first. The search has to keep
    going and then prefer the ACTIVE frame.
    """
    upper = _spec("menu item", "UPPERCASE", states=("sensitive", "enabled"))
    text = _spec("menu", "Text", (upper,), ("sensitive", "enabled"))
    bold = _spec("menu item", "Bold", states=("sensitive", "enabled"))
    fmt = _spec("menu", "Format", (bold, text), ("sensitive", "enabled"))
    new = _spec("menu item", "New", states=("sensitive", "enabled"))
    file_menu = _spec("menu", "File", (new,), ("sensitive", "enabled"))
    bar = _spec("menu bar", "", (file_menu, fmt))
    root_pane = _spec("root pane", "", (bar,))
    panel_b = _spec("panel", "", (root_pane,))
    panel_a = _spec("panel", "", (panel_b,))
    leaves = tuple(_spec("push button", f"b{i}", states=("sensitive",)) for i in range(40))
    toolbar = _spec("panel", "toolbar", leaves)
    writer = _spec("frame", "Untitled 1 - LibreOffice Writer", (toolbar, panel_a), ("active",))
    about = _spec("menu item", "About", states=("sensitive", "enabled"))
    help_menu = _spec("menu", "Help", (about,), ("sensitive", "enabled"))
    idle_bar = _spec("menu bar", "", (help_menu,))
    idle = _spec("frame", "other", (idle_bar,))
    log: list[str] = []
    return _Fresh("application", "soffice", (idle, writer), log=log), log


def test_recycled_proxy_ids_still_operate_the_writer_menu_bar() -> None:
    """LibreOffice Writer's menu bar is below a toolbar of leaf buttons.

    Each ``get_child_at_index`` builds a new proxy. After a leaf is
    processed nothing else references it, so it is collected and its
    ``id()`` is reused for the root pane that owns the menu bar. A seen-set
    of those ids skips the root pane and reports that Writer has no menu
    bar, even though an idle frame's bar was found. ``menu`` has to list
    File and Format and press Format > Text > UPPERCASE.
    """
    for _ in range(20):
        app, log = _writer_app()
        assert [row["title"] for row in _linux_menus.menu_items(app, None)] == ["File", "Format"]
        assert _linux_menus.menu_press(app, "Format > Text > UPPERCASE", settle=lambda _s: None) == "UPPERCASE"
        assert log == ["Format", "Text", "UPPERCASE"]


def test_same_bus_path_is_not_a_new_node(monkeypatch) -> None:
    """Two live wrappers of one accessible share a bus name and object path.

    Their ``id()`` values differ, because both wrappers are still alive.
    A panel that reports eighty copies of itself would otherwise fill the
    400-node cap before the menu bar further down the frame is reached.
    """
    from a11y_computer_use.drivers import _atspi

    monkeypatch.setattr(_atspi, "_atspi_wrapper_key", lambda node: node.atspi_key)

    class Node:
        __slots__ = ("kind", "role", "name", "states", "atspi_key", "actions", "key")

        def __init__(self, kind, role, name, atspi_key, states=()):
            self.kind = kind
            self.role = role
            self.name = name
            self.atspi_key = atspi_key
            self.states = set(states) | {"visible"}
            self.actions = ["click"]
            self.key = ""

        def get_role_name(self):
            return self.role

        def get_name(self):
            return self.name

        def get_child_count(self):
            if self.kind == "cycle":
                return 80
            if self.kind in {"app", "down", "bar", "menu"}:
                return 1
            if self.kind == "frame":
                return 2
            return 0

        def get_child_at_index(self, index):
            if self.kind == "cycle":
                return Node("cycle", "panel", "cycle", (":1.9", "/cycle"))
            if self.kind == "app":
                return Node("frame", "frame", "Writer", (":1.9", "/frame"), ("active",))
            if self.kind == "frame":
                if index == 0:
                    return Node("cycle", "panel", "cycle", (":1.9", "/cycle"))
                return Node("down", "panel", "down", (":1.9", "/d0"))
            if self.kind == "down":
                depth = int(self.atspi_key[1][2:])
                if depth >= 8:
                    return Node("bar", "menu bar", "", (":1.9", "/bar"))
                return Node("down", "panel", "down", (":1.9", f"/d{depth + 1}"))
            if self.kind == "bar":
                return Node("menu", "menu", "File", (":1.9", "/file"), ("sensitive", "enabled"))
            if self.kind == "menu":
                return Node("item", "menu item", "New", (":1.9", "/new"), ("sensitive", "enabled"))
            raise IndexError(index)

        def get_action_iface(self):
            return self

        def get_n_actions(self):
            return 1

        def get_action_name(self, _index):
            return "click"

        def get_key_binding(self, _index):
            return ""

        def do_action(self, _index):
            return True

    app = Node("app", "application", "soffice", (":1.9", "/app"))
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

    monkeypatch.setattr("a11y_computer_use.drivers._atspi.app_listed", lambda _name: False)
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


class _ENode(Acc):
    """Chromium menu node. ``get_parent`` is the menu bar for a bar button."""

    def __init__(self, role, name="", children=(), states=None, log=None, on_press=None):
        super().__init__(role, name, children, states)
        self.parent = None
        self.log = [] if log is None else log
        self.on_press = on_press
        for child in self.children:
            if hasattr(child, "parent"):
                child.parent = self

    def get_parent(self):
        return self.parent

    def get_action_iface(self):
        return self

    def get_n_actions(self):
        return 1

    def get_action_name(self, _index):
        return "click"

    def get_key_binding(self, _index):
        return self.key

    def do_action(self, _index):
        self.log.append(self.name)
        self.pressed.append("click")
        if self.on_press is not None:
            self.on_press(self)
        return True


def _electron(menus: dict[str, list[str]]) -> tuple[Acc, list[str]]:
    """Application menu bar of push buttons. Each press opens a separate window.

    The popup is ``window > menu bar > menu > menu item``, which is the
    Chromium shape. Item names keep their accelerator text.
    """
    log: list[str] = []
    app = _ENode("application", "electron", log=log)
    buttons = []
    for title, items in menus.items():
        def open_menu(_node, title=title, items=items):
            if any(child.name == title for child in app.children):
                return
            rows = [
                _ENode("menu item", name, states={"sensitive", "enabled", "showing"}, log=log)
                for name in items
            ]
            menu = _ENode("menu", "", rows, states={"showing"})
            popup_bar = _ENode("menu bar", "", [menu], states={"showing"})
            window = _ENode("window", title, [popup_bar], states={"showing"})
            app.children.append(window)
            window.parent = app

        buttons.append(_ENode(
            "push button", title,
            states={"sensitive", "enabled", "showing"},
            log=log, on_press=open_menu,
        ))
    bar = _ENode("menu bar", "", buttons, states={"showing"})
    frame = _ENode("frame", "Notes", [bar], states={"active", "showing"})
    app.children = [frame]
    frame.parent = app
    return app, log


def test_electron_menu_bar_buttons_open_a_separate_popup() -> None:
    """Synthetic Chromium menu. Not a live Electron.

    Top-level entries are push buttons, so they used to list ``submenu``
    false and ``File > New Note`` failed with ``'File' has no submenu``.
    The item's accessible name includes the accelerator. ``New Note`` matches
    ``New Note Ctrl+N``, and the full string still matches too.
    """
    menus = {
        "File": ["New Note Ctrl+N", "Show Dialog Ctrl+D", "Quit"],
        "Edit": ["Select All Ctrl+A"],
        "View": ["Toggle Panel", "Fullscreen F11"],
    }
    app, log = _electron(menus)
    listed = _linux_menus.menu_items(app, None)
    assert [(row["title"], row["submenu"]) for row in listed] == [
        ("File", True), ("Edit", True), ("View", True),
    ]
    assert log == []
    rows = _linux_menus.menu_items(app, "File", settle=lambda _s: None)
    assert [row["title"] for row in rows] == ["New Note Ctrl+N", "Show Dialog Ctrl+D", "Quit"]
    assert all(row["submenu"] is False for row in rows)

    app, log = _electron(menus)
    assert _linux_menus.menu_press(app, "File > New Note", settle=lambda _s: None) == "New Note Ctrl+N"
    assert log == ["File", "New Note Ctrl+N"]
    app, log = _electron(menus)
    assert _linux_menus.menu_press(app, "File > New Note Ctrl+N", settle=lambda _s: None) == "New Note Ctrl+N"
    app, log = _electron(menus)
    assert _linux_menus.menu_press(app, "Edit > Select All", settle=lambda _s: None) == "Select All Ctrl+A"
    app, log = _electron(menus)
    assert _linux_menus.menu_press(app, "View > Fullscreen", settle=lambda _s: None) == "Fullscreen F11"
    app, log = _electron(menus)
    assert _linux_menus.menu_press(app, "View > Toggle Panel", settle=lambda _s: None) == "Toggle Panel"
    app, log = _electron(menus)
    assert _linux_menus.menu_press(app, "File > show", settle=lambda _s: None) == "Show Dialog Ctrl+D"
    app, _log = _electron(menus)
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(
            app, "File > Missing", settle=lambda _s: None, dismiss=lambda: None,
        )
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "New Note Ctrl+N" in exc.value.detail["available"]


def test_exact_menu_title_wins_over_a_shared_accelerator_stem() -> None:
    """``New Note`` is the item with that title, not ``New Note Ctrl+Shift+N``.

    ``Save`` matches ``Save Ctrl+S`` and does not also match
    ``Save As Ctrl+Shift+S``. Two items that both strip to the same title
    stay ambiguous. Synthetic menu, not a live Electron.
    """
    app, log = _electron({"File": ["New Note", "New Note Ctrl+Shift+N"]})
    assert _linux_menus.menu_press(app, "File > New Note", settle=lambda _s: None) == "New Note"
    assert log == ["File", "New Note"]
    app, log = _electron({"File": ["New Note", "New Note Ctrl+Shift+N"]})
    assert _linux_menus.menu_press(
        app, "File > New Note Ctrl+Shift+N", settle=lambda _s: None,
    ) == "New Note Ctrl+Shift+N"

    app, log = _electron({"File": ["Save Ctrl+S", "Save As Ctrl+Shift+S"]})
    assert _linux_menus.menu_press(app, "File > Save", settle=lambda _s: None) == "Save Ctrl+S"
    app, log = _electron({"File": ["Save Ctrl+S", "Save As Ctrl+Shift+S"]})
    assert _linux_menus.menu_press(app, "File > Save As", settle=lambda _s: None) == "Save As Ctrl+Shift+S"

    app, _log = _electron({"File": ["New Note Ctrl+N", "New Note Ctrl+Shift+N"]})
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(app, "File > New Note", settle=lambda _s: None, dismiss=lambda: None)
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "New Note Ctrl+N" in str(exc.value)


def test_electron_menu_item_submenu_opens_the_next_popup() -> None:
    """A menu item with HAS_POPUP opens another window. Not a live Electron."""
    log: list[str] = []
    app = _ENode("application", "electron", log=log)
    notes = _ENode("menu item", "notes.txt", states={"sensitive", "enabled", "showing"}, log=log)

    def open_recent(_node):
        if any(child.name == "recent-popup" for child in app.children):
            return
        menu = _ENode("menu", "", [notes], states={"showing"})
        popup_bar = _ENode("menu bar", "", [menu], states={"showing"})
        window = _ENode("window", "recent-popup", [popup_bar], states={"showing"})
        app.children.append(window)
        window.parent = app

    recent = _ENode(
        "menu item", "Recent",
        states={"sensitive", "enabled", "showing", "has_popup"},
        log=log, on_press=open_recent,
    )

    def open_file(_node):
        if any(child.name == "file-popup" for child in app.children):
            return
        menu = _ENode("menu", "", [recent], states={"showing"})
        popup_bar = _ENode("menu bar", "", [menu], states={"showing"})
        window = _ENode("window", "file-popup", [popup_bar], states={"showing"})
        app.children.append(window)
        window.parent = app

    file_btn = _ENode(
        "push button", "File",
        states={"sensitive", "enabled", "showing"},
        log=log, on_press=open_file,
    )
    bar = _ENode("menu bar", "", [file_btn], states={"showing"})
    frame = _ENode("frame", "Notes", [bar], states={"active", "showing"})
    app.children = [frame]
    frame.parent = app
    assert _linux_menus.menu_press(app, "File > Recent > notes.txt", settle=lambda _s: None) == "notes.txt"
    assert log == ["File", "Recent", "notes.txt"]


def _bind_java(app, *nodes):
    """Mark ``app`` as the Java ATK wrapper and point ``nodes`` at it."""
    app.get_toolkit_name = lambda: "JAVA"

    def application():
        return app

    app.get_application = application
    for node in nodes:
        node.get_application = application
    return app


def _swing_edit(*, item_showing: bool):
    """Edit > Clear Notes. The bar entry stays SELECTED, as Java ATK does."""
    item_states = {"sensitive", "enabled"}
    if item_showing:
        item_states.add("showing")
    clear = _item("Clear Notes", states=item_states)
    edit = _menu("Edit", clear, states={"sensitive", "enabled", "showing", "selected"})
    bar = Acc("menu bar", "", [edit])
    frame = Acc("frame", "Probe", [bar], states={"active", "showing"})
    app = Acc("application", "java", [frame])
    _bind_java(app, clear, edit, bar, frame)
    return app, edit, clear, frame


def test_java_selected_without_a_showing_item_is_not_an_open_menu() -> None:
    """A leftover SELECTED on a Java menu-bar entry is not an open menu.

    The entry counts only when a popup or an item is SHOWING. GTK still
    treats SELECTED alone as open; that case is the disabled-Save test.
    """
    app, edit, clear, _frame = _swing_edit(item_showing=False)
    assert "selected" in edit.states
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}
    clear.states.add("showing")
    assert _linux_menus.menu_state(app) == {"open": True, "path": ["Edit"]}


def test_java_menu_press_closes_the_popup_and_ignores_leftover_selected() -> None:
    """The leaf runs, then Escape closes the popup. SELECTED may remain."""
    app, edit, clear, _frame = _swing_edit(item_showing=False)
    edit.states.discard("selected")

    class _Open(_Action):
        def do_action(self, index):
            self.node.pressed.append(self.node.actions[index])
            edit.states.add("selected")
            clear.states.add("showing")
            return True

    edit.get_action_iface = lambda: _Open(edit)  # type: ignore[method-assign]
    sent: list[str] = []

    def dismiss() -> None:
        sent.append("escape")
        clear.states.discard("showing")

    title = _linux_menus.menu_press(
        app, "Edit > Clear Notes", settle=lambda _s: None, dismiss=dismiss,
    )
    assert title == "Clear Notes"
    assert clear.pressed == ["click"]
    assert sent == ["escape"]
    assert "selected" in edit.states
    assert "showing" not in clear.states
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}


def test_java_menu_press_does_not_escape_into_a_showing_dialog() -> None:
    """A dialog opened by the leaf is not cancelled to close the popup."""
    app, edit, clear, frame = _swing_edit(item_showing=False)
    dialog = Acc("dialog", "Confirm", states={"showing", "visible", "active"})
    frame.children.append(dialog)
    _bind_java(app, dialog)

    class _Keep(_Action):
        def do_action(self, index):
            self.node.pressed.append(self.node.actions[index])
            edit.states.add("selected")
            clear.states.add("showing")
            return True

    edit.get_action_iface = lambda: _Keep(edit)  # type: ignore[method-assign]
    clear.get_action_iface = lambda: _Keep(clear)  # type: ignore[method-assign]
    sent: list[str] = []
    title = _linux_menus.menu_press(
        app, "Edit > Clear Notes", settle=lambda _s: None,
        dismiss=lambda: sent.append("escape"),
    )
    assert title == "Clear Notes"
    assert clear.pressed == ["click"]
    assert sent == []
    assert _linux_menus.menu_state(app)["open"] is True
    assert _linux_menus.escape_reaches_menu(app) is False


def test_java_press_of_the_menu_itself_leaves_the_popup_open() -> None:
    """``menu(path='Edit')`` opens the menu. It is not closed again."""
    app, edit, clear, _frame = _swing_edit(item_showing=False)
    edit.states.discard("selected")

    class _Open(_Action):
        def do_action(self, index):
            self.node.pressed.append(self.node.actions[index])
            edit.states.add("selected")
            clear.states.add("showing")
            return True

    edit.get_action_iface = lambda: _Open(edit)  # type: ignore[method-assign]
    sent: list[str] = []
    assert _linux_menus.menu_press(
        app, "Edit", settle=lambda _s: None, dismiss=lambda: sent.append("escape"),
    ) == "Edit"
    assert sent == []
    assert _linux_menus.menu_state(app) == {"open": True, "path": ["Edit"]}


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


def _quick_app(monkeypatch, *, opens=True, bounds=((10, 20), (80, 30))):
    from a11y_computer_use.drivers import _atspi, _linux_input

    leaf = _ENode("menu item", "New Item", states={"sensitive", "showing"})
    popup = _ENode("menu", "", [leaf], states={"showing"})
    entry = _ENode("menu bar", "File", states={"sensitive", "showing"})
    entry.get_n_actions = lambda: 0
    bar = _ENode("menu bar", "", [entry])
    frame = _ENode("frame", "QML Probe", [bar], states={"active", "showing"})
    app = _ENode("application", "qml", [frame])
    clicks = []

    def click(x, y):
        clicks.append((x, y))
        if opens:
            if popup in frame.children:
                frame.children.remove(popup)
            else:
                frame.children.append(popup)
                popup.parent = frame

    leaf.on_press = lambda _: frame.children.remove(popup)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_extents", lambda _: bounds)
    monkeypatch.setattr(_linux_input, "click", click)
    monkeypatch.setattr(_linux_menus, "_settle", lambda _: None)
    return app, entry, leaf, clicks


def test_qt_quick_menu_lists_and_activates_same_window_popup(monkeypatch):
    app, entry, leaf, clicks = _quick_app(monkeypatch)
    assert _linux_menus.menu_items(app, None)[0]["submenu"] is True
    assert clicks == []
    assert [row["title"] for row in _linux_menus.menu_items(app, "File")] == ["New Item"]
    assert clicks == [(50, 35), (50, 35)]
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}
    assert _linux_menus.menu_press(app, "File > New Item") == "New Item"
    assert leaf.pressed == ["click"]
    assert entry.pressed == []
    assert _linux_menus.menu_state(app) == {"open": False, "path": []}


@pytest.mark.parametrize("bounds", [(None, None), ((10, 20), (0, 30))])
def test_qt_quick_menu_refuses_without_native_bounds(monkeypatch, bounds):
    app, _, leaf, clicks = _quick_app(monkeypatch, bounds=bounds)
    with pytest.raises(ComputerUseError, match="refused to press"):
        _linux_menus.menu_press(app, "File > New Item")
    assert clicks == []
    assert leaf.pressed == []


def test_qt_quick_menu_requires_popup_effect_in_own_window(monkeypatch):
    app, _, leaf, clicks = _quick_app(monkeypatch, opens=False)
    other_popup = _ENode("menu", "", [_item("New Item")], states={"showing"})
    app.children.append(_ENode("frame", "Other", [other_popup], states={"showing"}))
    with pytest.raises(ComputerUseError, match="refused to press"):
        _linux_menus.menu_press(app, "File > New Item")
    assert clicks == [(50, 35)]
    assert leaf.pressed == []


def test_qt_quick_menu_state_and_close_verify_popup(monkeypatch):
    app, entry, _, _ = _quick_app(monkeypatch)
    assert _linux_menus._press(entry)
    assert _linux_menus.menu_state(app) == {"open": True, "path": ["File"]}
    assert _linux_menus.menu_close(
        app, dismiss=lambda: _linux_menus._press(entry),
    ) == ["File"]


def test_qt_quick_menu_refuses_without_x11(monkeypatch):
    from a11y_computer_use.drivers import _atspi

    app, _, _, clicks = _quick_app(monkeypatch)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: False)
    with pytest.raises(ComputerUseError, match="refused to press"):
        _linux_menus.menu_press(app, "File > New Item")
    assert clicks == []


def test_menu_bar_container_is_not_a_quick_menu_entry():
    bar = _ENode("menu bar", "Application")
    _ENode("frame", "Window", [bar])
    assert not _linux_menus._quick_menu_entry(bar)
    assert not _linux_menus._has_submenu(bar)


def test_qt_quick_top_menu_uses_pointer_even_with_leaf_dispatch(monkeypatch):
    app, _, _, clicks = _quick_app(monkeypatch)

    def dispatch(_):
        pytest.fail("actionless menu entry must not use native leaf dispatch")

    assert _linux_menus.menu_press(app, "File", dispatch=dispatch) == "File"
    assert clicks == [(50, 35)]
    assert _linux_menus.menu_state(app) == {"open": True, "path": ["File"]}


@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("focus_on_bar", [False, True])
@pytest.mark.parametrize("releases_focus", [False, True])
def test_menu_press_clears_keyboard_tracking_without_popup(
    disabled, focus_on_bar, releases_focus,
):
    leaf = _item("Fit Width", states=() if disabled else ("enabled", "sensitive"))
    view = _menu("View", leaf)
    bar = Acc("menu bar", children=[view], states=set())
    app = Acc("application", children=[Acc("frame", children=[bar])])
    focused = bar if focus_on_bar else view

    class Open(_Action):
        def do_action(self, index):
            focused.states.add("focused")
            return super().do_action(index)

    view.get_action_iface = lambda: Open(view)
    sent = []

    def dismiss():
        sent.append("escape")
        if releases_focus:
            focused.states.discard("focused")

    assert _linux_menus.menu_state(app) == {"open": False, "path": []}
    if disabled or not releases_focus:
        with pytest.raises(ComputerUseError) as exc:
            _linux_menus.menu_press(
                app, "View > Fit Width", settle=lambda _: None, dismiss=dismiss,
            )
        assert exc.value.detail["reason"] == (
            "disabled" if releases_focus else "menu_still_open"
        )
        if not releases_focus:
            assert "menubar still has focus" in exc.value.message
    else:
        assert _linux_menus.menu_press(
            app, "View > Fit Width", settle=lambda _: None, dismiss=dismiss,
        ) == "Fit Width"
    assert sent == ["escape"]
    assert leaf.pressed == ([] if disabled else ["click"])
    assert ("focused" in focused.states) is not releases_focus


def test_menu_press_keeps_dialog_when_menubar_retains_focus():
    leaf = _item("Open")
    file_menu = _menu("File", leaf, states={"focused", "enabled"})
    bar = Acc("menu bar", children=[file_menu])
    frame = Acc("frame", children=[bar])
    app = Acc("application", children=[frame])

    def dispatch(node):
        frame.children.append(Acc("dialog", "Open Document", states={"showing"}))
        return "ok"

    sent = []
    assert _linux_menus.menu_press(
        app, "File > Open", dispatch=dispatch, settle=lambda _: None,
        dismiss=lambda: sent.append("escape"),
    ) == "Open"
    assert sent == []


@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("clears_entry", [False, True])
def test_qt_popup_cleanup_ends_active_action_but_allows_idle_widget_focus(
    disabled, clears_entry,
):
    leaf = _item("Option X", states=() if disabled else ("enabled", "sensitive"))
    popup = _menu("Tools", leaf)
    entry = Acc("menu item", "Tools", [popup], states={"enabled", "sensitive"})
    bar = Acc("menu bar", children=[entry], states={"showing"})
    app = Acc("application", children=[Acc("frame", children=[bar])])
    app.get_toolkit_name = lambda: "Qt"

    class Open(_Action):
        def do_action(self, index):
            popup.states.add("showing")
            bar.states.add("focused")
            entry.states.add("focused")
            return super().do_action(index)

    entry.get_action_iface = lambda: Open(entry)
    sent = []

    def dismiss():
        sent.append("escape")
        if "showing" in popup.states:
            popup.states.remove("showing")
        elif clears_entry:
            entry.states.discard("focused")

    if disabled or not clears_entry:
        with pytest.raises(ComputerUseError) as exc:
            _linux_menus.menu_press(app, "Tools > Option X", settle=lambda _: None, dismiss=dismiss)
        assert exc.value.detail["reason"] == ("disabled" if clears_entry else "menu_still_open")
    else:
        assert _linux_menus.menu_press(
            app, "Tools > Option X", settle=lambda _: None, dismiss=dismiss,
        ) == "Option X"
    assert sent == ["escape", "escape"]
    assert "focused" in bar.states
    assert leaf.pressed == ([] if disabled else ["click"])
    assert _linux_menus._menubar_has_focus(app) is not clears_entry


def test_popup_cleanup_does_not_send_second_escape_to_new_dialog():
    leaf = _item("Open")
    entry = _menu("File", leaf, states={"selected", "focused", "sensitive"})
    bar = Acc("menu bar", children=[entry])
    frame = Acc("frame", children=[bar])
    app = Acc("application", children=[frame])
    sent = []

    def dismiss():
        sent.append("escape")
        entry.states.discard("selected")
        frame.children.append(Acc("dialog", "Open Document", states={"showing"}))

    assert _linux_menus.menu_press(
        app, "File > Open", settle=lambda _: None, dismiss=dismiss,
    ) == "Open"
    assert sent == ["escape"]


@pytest.mark.parametrize("reply", ["empty", "hit", "unavailable", "failed"])
def test_menu_bar_collection_absence_avoids_walk(monkeypatch, reply):
    from types import SimpleNamespace
    from a11y_computer_use.drivers import _atspi

    root, _ = mousepad()
    bar = root.children[0].children[0]
    rules = []
    queries = []
    api = SimpleNamespace(
        CollectionMatchType=SimpleNamespace(NONE=0, ANY=1),
        CollectionSortOrder=SimpleNamespace(CANONICAL=2),
        Role=SimpleNamespace(MENU_BAR=3),
        StateSet=SimpleNamespace(new=lambda states: states),
        MatchRule=SimpleNamespace(new=lambda *args: rules.append(args) or "rule"),
    )
    monkeypatch.setattr(_atspi, "_atspi", lambda: api)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda root: True)

    def matches(*args):
        queries.append(args)
        if reply == "failed":
            raise RuntimeError("unsupported Collection")
        return {"empty": [], "hit": [bar], "unavailable": None}[reply]

    root.get_collection_iface = lambda: SimpleNamespace(get_matches=matches)
    if reply == "empty":
        root.get_child_count = lambda: pytest.fail("empty native query must avoid traversal")
    assert _linux_menus.find_menu_bar(root) is (None if reply == "empty" else bar)
    assert rules == [([], 0, {}, 0, [3], 1, [], 0, False)]
    assert queries == [("rule", 2, 1, True)]


def test_nonchromium_menu_discovery_retains_walk(monkeypatch):
    from types import SimpleNamespace
    from a11y_computer_use.drivers import _atspi

    root, _ = mousepad()
    root.get_collection_iface = lambda: SimpleNamespace(
        get_matches=lambda *args: pytest.fail("non-Chromium Collection queried"),
    )
    monkeypatch.setattr(_atspi, "_chromium_app", lambda root: False)
    assert _linux_menus.find_menu_bar(root) is root.children[0].children[0]


@pytest.mark.parametrize("operation", ["list", "press"])
@pytest.mark.parametrize("path", ["DVD", "DVD > DVD Menu"])
def test_hidden_menu_parent_refuses_before_any_input(operation, path):
    leaf = _item("DVD Menu")
    dvd = _menu("DVD", leaf)
    dvd.states.discard("visible")
    dvd.actions = []
    media = _menu("Media", _item("Open"))
    app = Acc("application", "parole", [Acc("frame", "Parole", [
        Acc("menu bar", children=[media, dvd]),
    ])])
    assert [row["title"] for row in _linux_menus.menu_items(app, None)] == ["Media"]
    with pytest.raises(ComputerUseError) as exc:
        if operation == "list":
            _linux_menus.menu_items(app, path)
        else:
            _linux_menus.menu_press(app, path)
    assert exc.value.detail["reason"] == "hidden"
    assert exc.value.detail["level"] == 0
    assert not dvd.pressed and not leaf.pressed and not media.pressed


def test_hidden_leaf_is_omitted_but_closed_visible_rows_remain_listable():
    hidden = _item("Hidden")
    hidden.states.discard("visible")
    visible = _item("Open")  # VISIBLE without SHOWING: the popup is closed.
    file_menu = _menu("File", hidden, visible)
    app = Acc("application", children=[Acc("frame", children=[
        Acc("menu bar", children=[file_menu]),
    ])])
    assert [row["title"] for row in _linux_menus.menu_items(app, "File")] == ["Open"]
    assert not file_menu.pressed and not visible.pressed
    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(app, "File > Hidden", settle=lambda _: None,
                                dismiss=lambda: None)
    assert exc.value.detail["reason"] == "hidden"
    assert exc.value.detail["level"] == 1
    assert not hidden.pressed


def test_menu_with_only_hidden_rows_lists_empty_without_opening():
    hidden = _item("Hidden")
    hidden.states.discard("visible")
    file_menu = _menu("File", hidden)
    app = Acc("application", children=[Acc("frame", children=[
        Acc("menu bar", children=[file_menu]),
    ])])
    assert _linux_menus.menu_items(app, "File") == []
    assert not file_menu.pressed and not hidden.pressed


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("operation", ["list", "press"])
def test_qt_closed_popup_visibility_is_rechecked_after_open(nested, operation):
    """Qt hides closed popup rows; opening reveals only available actions."""
    visible = _item("Option X")
    hidden = _item("Hidden")
    visible.states.discard("visible")
    hidden.states.discard("visible")
    popup = _menu("Tools", visible, hidden)
    entry = Acc(
        "menu item", "Tools", [popup], states={"enabled", "sensitive"},
        actions=("Show Menu",),
    )
    app = Acc("application", children=[Acc("menu bar", children=[entry])])
    app.get_toolkit_name = lambda: "Qt"

    class Open(_Action):
        def do_action(self, index):
            if "showing" in popup.states:
                popup.states.discard("showing")
                visible.states.discard("visible")
            else:
                popup.states.add("showing")
                visible.states.add("visible")
            return super().do_action(index)

    entry.get_action_iface = lambda: Open(entry)
    path = "Tools"
    if nested:
        child = _item("Child")
        sub = _menu("Option X", child)
        visible.children = [sub]
        path += " > Option X"

    if operation == "list":
        rows = _linux_menus.menu_items(app, path, settle=lambda _: None)
        assert [row["title"] for row in rows] == (["Child"] if nested else ["Option X"])
    else:
        path += " > Child" if nested else " > Option X"
        assert _linux_menus.menu_press(
            app, path, settle=lambda _: None,
            dismiss=lambda: popup.states.discard("showing"),
        ) == ("Child" if nested else "Option X")
    assert entry.pressed
    assert not hidden.pressed

    with pytest.raises(ComputerUseError) as exc:
        _linux_menus.menu_press(
            app, "Tools > Hidden", settle=lambda _: None,
            dismiss=lambda: popup.states.discard("showing"),
        )
    assert exc.value.detail["reason"] == "hidden"
    assert not hidden.pressed
