"""Combo, spin, slider, tree-row, and Chrome form-state fixes.

The accessibles here are fakes. They record selection, value writes, and
actions so a success that typed into a different field, clamped a spin
button, or activated a tree cell without selecting it fails the test.
No gi import: macOS and Windows collect this file.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers._atspi import ATSPIAccessor
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode


class _States:
    def __init__(self, names):
        self.names = set(names)

    def contains(self, member):
        return str(member) in self.names


class _Rect:
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _Node:
    def __init__(self, role, name="", text="", states=(), children=(), actions=(), *,
                 value=None, minimum=None, maximum=None, attrs=None, x=10, y=10, w=120, h=24):
        self.role = role
        self.name = name
        self.text = text
        self.states = set(states)
        self.children = list(children)
        self.actions = list(actions)
        self.parent = None
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
        self.attrs = dict(attrs or {})
        self.rect = _Rect(x, y, w, h)
        self.action_log: list[str] = []
        self.value_sets: list[float] = []
        self.hold_value = False
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_toolkit_name(self):
        return getattr(self, "toolkit", "")

    def get_description(self):
        return ""

    def get_state_set(self):
        return _States(self.states)

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_parent(self):
        return self.parent

    def get_attributes(self):
        return dict(self.attrs)

    def get_action_iface(self):
        return self if self.actions else None

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, index):
        return self.actions[index]

    def do_action(self, index):
        name = self.actions[index]
        self.action_log.append(name)
        if name == "collapse":
            self.states.discard("EXPANDED")
        if name == "click" and self.role == "list item":
            self.states.add("SELECTED")
        return True

    def get_selection_iface(self):
        if self.role in {"combo box", "list box", "table", "tree table", "tree"}:
            return self
        return None

    def select_child(self, index):
        if getattr(self, "select_fails", False):
            return False
        for child in self.children:
            child.states.discard("SELECTED")
        self.children[index].states.add("SELECTED")
        if "EXPANDABLE" in self.states:
            self.states.add("EXPANDED")
        return True

    def get_component_iface(self):
        return self

    def get_extents(self, _coord):
        return self.rect

    def clear_cache(self):
        return None


class _Entry(_Node):
    def __init__(self, text, name=""):
        super().__init__("text", name, text=text)

    def get_editable_text_iface(self):
        return self

    def set_text_contents(self, text):
        self.text = text
        return True

    def delete_text(self, start, end):
        self.text = self.text[:start] + self.text[end:]
        return True

    def insert_text(self, pos, text, length):
        self.text = self.text[:pos] + text[:length] + self.text[pos:]
        return True


class _Atspi:
    class StateType:
        PRESSED = "PRESSED"
        CHECKED = "CHECKED"
        SELECTED = "SELECTED"
        EXPANDED = "EXPANDED"
        EXPANDABLE = "EXPANDABLE"
        ENABLED = "ENABLED"
        SENSITIVE = "SENSITIVE"
        FOCUSED = "FOCUSED"
        FOCUSABLE = "FOCUSABLE"
        CHECKABLE = "CHECKABLE"
        SINGLE_LINE = "SINGLE_LINE"
        MULTI_LINE = "MULTI_LINE"

    class CoordType:
        SCREEN = 1

    class Text:
        @staticmethod
        def get_character_count(acc):
            if acc.text is None:
                raise RuntimeError("no text")
            return len(acc.text)

        @staticmethod
        def get_text(acc, start, end):
            if acc.text is None:
                raise RuntimeError("no text")
            if end is not None and int(end) < 0:
                return acc.text
            return acc.text[int(start):int(end)]

    class Value:
        @staticmethod
        def get_current_value(acc):
            if acc.value is None:
                raise RuntimeError("no value")
            return acc.value

        @staticmethod
        def get_minimum_value(acc):
            if acc.minimum is None:
                raise RuntimeError("no value")
            return acc.minimum

        @staticmethod
        def get_maximum_value(acc):
            if acc.maximum is None:
                raise RuntimeError("no value")
            return acc.maximum

        @staticmethod
        def get_minimum_increment(acc):
            step = getattr(acc, "increment", None)
            if step is None:
                raise RuntimeError("no increment")
            return step

        @staticmethod
        def set_current_value(acc, new):
            acc.value_sets.append(float(new))
            if acc.hold_value:
                return True
            upper = acc.maximum if acc.maximum is not None else float(new)
            lower = acc.minimum if acc.minimum is not None else float(new)
            acc.value = min(max(float(new), lower), upper)
            return True

    class Hypertext:
        @staticmethod
        def get_n_links(acc):
            return len(getattr(acc, "links", ()) or ())

        @staticmethod
        def get_link(acc, index):
            return (getattr(acc, "links")[index],)

    class Hyperlink:
        @staticmethod
        def get_object(link, index):
            return link[0] if index == 0 else None


def _element(ref, role, title, *, editable=False, clickable=False, x=10, y=10, w=120, h=24):
    return Element(
        ref, role, title, None, Bounds(0, x, y, w, h), "snap-value",
        editable=editable, clickable=clickable, enabled=True,
    )


def _driver(monkeypatch, handle):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: handle)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    return driver


def _combo():
    red = _Node("menu item", "Red", states={"SELECTED"})
    green = _Node("menu item", "Green")
    blue = _Node("menu item", "Blue")
    combo = _Node(
        "combo box", "Color", text="\ufffc",
        states={"EXPANDABLE", "ENABLED", "SENSITIVE"},
        children=[red, green, blue],
        actions=["press", "collapse"],
    )
    return combo, red, green, blue


def test_combo_set_value_selects_its_own_item_and_does_not_type_elsewhere(monkeypatch) -> None:
    combo, red, green, _blue = _combo()
    notes = _Entry("Paris", "Notes")
    driver = _driver(monkeypatch, combo)
    element = _element("e2", "AXComboBox", "Color", editable=True, clickable=True)
    assert driver.set_value(element, "Green") is True
    assert "SELECTED" in green.states
    assert "SELECTED" not in red.states
    assert notes.text == "Paris"
    assert combo.action_log == ["collapse"]  # the popup select opened is closed
    assert "EXPANDED" not in combo.states
    assert driver._focused_editable is None


def test_unknown_combo_option_is_invalid_before_any_input(monkeypatch) -> None:
    combo, red, _green, _blue = _combo()
    notes = _Entry("Paris", "Notes")
    driver = _driver(monkeypatch, combo)
    element = _element("e2", "AXComboBox", "Color", editable=True, clickable=True)
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "Mars")
    message = str(exc.value)
    assert "Red" in message and "Green" in message and "Blue" in message
    assert "SELECTED" in red.states
    assert notes.text == "Paris"
    assert combo.action_log == []
    assert combo.text == "\ufffc"


def test_editable_combo_writes_its_own_entry(monkeypatch) -> None:
    notes = _Entry("Paris", "Notes")
    paris = _Node("menu item", "Paris", states={"SELECTED"})
    entry = _Entry("Blue", "CityEntry")
    menu = _Node("menu", "", children=[paris])
    combo = _Node("combo box", "City", text="", children=[menu, entry], states={"EXPANDABLE"})
    typed: list[str] = []
    monkeypatch.setattr(_atspi, "_type_string", lambda text: typed.append(text))
    driver = _driver(monkeypatch, combo)
    element = _element("e3", "AXComboBox", "City", editable=True)
    assert driver.set_value(element, "Lima") is True
    assert entry.text == "Lima"
    assert entry.text != "BlueLima"
    assert notes.text == "Paris"
    assert typed == []
    assert ATSPIAccessor().read(combo).value == "Lima"


def test_gtk_combo_selects_through_the_combo_not_the_popup_highlight(monkeypatch) -> None:
    """A popup menu's Selection only highlights a row. The combo's Selection sets it."""
    notes = _Entry("Paris", "Notes")
    red = _Node("menu item", "Red", states={"SELECTED"})
    green = _Node("menu item", "Green")
    blue = _Node("menu item", "Blue")
    menu = _Node("menu", "", children=[red, green, blue])

    def menu_select(index):
        menu.action_log.append(f"menu-select:{index}")
        for child in menu.children:
            child.states.discard("SELECTED")
        menu.children[index].states.add("SELECTED")
        return True

    menu.get_selection_iface = lambda: menu
    menu.select_child = menu_select
    combo = _Node(
        "combo box", "Color", text="", children=[menu],
        actions=["press", "collapse"], states={"EXPANDABLE", "ENABLED"},
    )
    combo.active = 0

    def model_select(index):
        combo.active = index
        combo.action_log.append(f"model-select:{index}")
        return True

    def selected_child(index):
        if index != 0 or not 0 <= combo.active < len(menu.children):
            return None
        return menu.children[combo.active]

    combo.select_child = model_select
    combo.get_selected_child = selected_child
    driver = _driver(monkeypatch, combo)
    element = _element("e2", "AXComboBox", "Color", editable=True, clickable=True)
    assert driver.set_value(element, "Green") is True
    assert combo.active == 1
    assert combo.action_log == ["model-select:1"]
    assert menu.action_log == []
    assert notes.text == "Paris"
    assert ATSPIAccessor().read(combo).value == "Green"


def _gecko_combo():
    """A Firefox <select>: options live in a menu, and the app toolkit is Gecko."""
    red = _Node("menu item", "Red", actions=["select"], states={"SELECTED", "SHOWING", "VISIBLE"})
    green = _Node("menu item", "Green", actions=["select"], states={"VISIBLE"})
    blue = _Node("menu item", "Blue", actions=["select"], states={"VISIBLE"})
    menu = _Node("menu", "", children=[red, green, blue], states={"VISIBLE"})
    combo = _Node(
        "combo box", "Color", text="", children=[menu],
        actions=["open"], states={"EXPANDABLE", "ENABLED", "SENSITIVE"},
    )
    app = _Node("application", "Firefox")
    app.toolkit = "Gecko"
    combo.get_application = lambda: app
    # Firefox's combo Selection does not report the active option. The menu
    # item's SELECTED state does. Drop the GTK selection iface.
    combo.get_selection_iface = lambda: None
    return combo, red, green, blue


def test_firefox_select_uses_the_option_action_when_it_lands(monkeypatch) -> None:
    """Synthetic Gecko combo. The option action selects. No key is sent."""
    from a11y_computer_use.drivers import _linux_input

    combo, red, _green, blue = _gecko_combo()

    def select(index, node=blue):
        node.action_log.append(node.actions[index])
        for item in (red, _green, blue):
            item.states.discard("SELECTED")
        node.states.add("SELECTED")
        return True

    blue.do_action = select
    keys: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    driver = _driver(monkeypatch, combo)
    element = _element("e2", "AXComboBox", "Color", editable=True, clickable=True)
    assert driver.set_value(element, "Blue") is True
    assert "SELECTED" in blue.states
    assert "SELECTED" not in red.states
    assert keys == []


def test_firefox_select_uses_keys_when_the_option_action_does_not_land(monkeypatch) -> None:
    """Synthetic Gecko combo. select returns false. Down moves the selection."""
    from a11y_computer_use.drivers import _linux_input

    combo, red, green, blue = _gecko_combo()
    items = [red, green, blue]

    def miss(index, node=blue):
        node.action_log.append("select")
        return False

    for item in items:
        item.do_action = lambda index, node=item: miss(index, node)

    def grab_focus() -> bool:
        combo.states.add("FOCUSED")
        return True

    combo.grab_focus = grab_focus

    def press(chord: str) -> None:
        keys.append(chord)
        if chord != "Down":
            return
        current = next(i for i, item in enumerate(items) if "SELECTED" in item.states)
        items[current].states.discard("SELECTED")
        items[min(current + 1, len(items) - 1)].states.add("SELECTED")

    keys: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", press)
    driver = _driver(monkeypatch, combo)
    element = _element("e2", "AXComboBox", "Color", editable=True, clickable=True)
    assert driver.set_value(element, "Blue") is True
    assert "SELECTED" in blue.states
    assert keys == ["Down", "Down"]
    assert blue.action_log == ["select"]


def test_chrome_select_and_number_reject_values_that_cannot_land(monkeypatch) -> None:
    kazakhstan = _Node("menu item", "Kazakhstan", states={"SELECTED"})
    japan = _Node("menu item", "Japan")
    peru = _Node("menu item", "Peru")
    select = _Node(
        "combo box", "Country", text="\ufffc",
        children=[kazakhstan, japan, peru], actions=["press"],
    )
    driver = _driver(monkeypatch, select)
    element = _element("e4", "AXComboBox", "Country", editable=True, clickable=True)
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "Mars")
    assert "Kazakhstan" in str(exc.value) and "Peru" in str(exc.value)
    assert select.action_log == []
    assert "SELECTED" in kazakhstan.states

    assert driver.set_value(element, "Peru") is True
    assert "SELECTED" in peru.states

    number = _Node("spin button", "Seats", text="", value=0.0, minimum=0.0, maximum=100.0)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: number)
    field = _element("e5", "AXTextField", "Seats", editable=True)
    with pytest.raises(ValueError) as exc:
        driver.set_value(field, "abc")
    assert "0" in str(exc.value) and "100" in str(exc.value)
    assert number.value_sets == []
    assert number.value == 0.0


class _ChromeOption(_Node):
    def __init__(self, name, selected=False):
        super().__init__(
            "menu item", name, actions=["select"],
            states={"SELECTED"} if selected else set(),
        )

    def do_action(self, index):
        name = self.actions[index]
        self.action_log.append(name)
        if name != "select" or self.parent is None:
            return True
        for child in self.parent.children:
            child.states.discard("SELECTED")
        self.states.add("SELECTED")
        return True


class _ChromeSelect(_Node):
    """A Chrome ``<select>``.

    The combobox has no Selection interface. Its text is U+FFFC and its name
    stays the aria-label. The child menu holds the options, and the option's
    ``select`` action is what moves SELECTED and the HTML value. ``press``
    opens the popup; ``collapse`` closes it.
    """

    def __init__(self, names=("Kazakhstan", "Japan", "Peru"), selected="Kazakhstan"):
        self.items = [_ChromeOption(name, selected=(name == selected)) for name in names]
        menu = _Node("menu", "", children=self.items)
        super().__init__(
            "combo box", "Country", text="\ufffc", children=[menu],
            actions=["press", "collapse"], states={"EXPANDABLE", "ENABLED"},
        )

    def get_application(self):
        return self

    def get_toolkit_name(self):
        return "Chromium"

    def get_selection_iface(self):
        return None

    def do_action(self, index):
        name = self.actions[index]
        self.action_log.append(name)
        if name == "press":
            self.states.add("EXPANDED")
        if name == "collapse":
            self.states.discard("EXPANDED")
        return True


def test_chrome_select_sets_a_valid_option_and_closes_the_popup(monkeypatch) -> None:
    select = _ChromeSelect()
    driver = _driver(monkeypatch, select)
    element = _element("e4", "AXComboBox", "Country", editable=True, clickable=True)
    assert driver.set_value(element, "Kazakhstan") is True
    assert select.action_log == []
    assert "EXPANDED" not in select.states
    assert "SELECTED" in select.items[0].states

    assert driver.set_value(element, "Peru") is True
    assert select.items[2].action_log == ["select"]
    assert select.action_log == []
    assert "EXPANDED" not in select.states
    assert "SELECTED" in select.items[2].states
    assert "SELECTED" not in select.items[0].states
    assert ATSPIAccessor().read(select).value == "Peru"

    fresh = _ChromeSelect()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: fresh)
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "Mars")
    assert "Kazakhstan" in str(exc.value) and "Peru" in str(exc.value)
    assert fresh.action_log == []
    assert "EXPANDED" not in fresh.states


def test_chrome_select_closes_the_popup_when_the_option_does_not_land(monkeypatch) -> None:
    select = _ChromeSelect()
    for item in select.items:
        item.do_action = lambda index, item=item: item.action_log.append(item.actions[index]) or True
    monkeypatch.setattr(_atspi, "_click_center", lambda _node: None)
    driver = _driver(monkeypatch, select)
    element = _element("e4", "AXComboBox", "Country", editable=True, clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        driver.set_value(element, "Peru")
    assert exc.value.detail["reason"] in {"text_mismatch", "popup_open"}
    assert "EXPANDED" not in select.states
    assert "collapse" in select.action_log


def _select_option(name, *, selected=False):
    """A Chrome menu item whose click selects it and leaves the popup open."""
    node = _Node(
        "menu item", name, actions=["click"],
        states={"SELECTED"} if selected else set(),
    )
    node.toolkit = "Chromium"

    def do_action(index, option=node):
        option.action_log.append(option.actions[index])
        root = option
        while root.parent is not None:
            root = root.parent

        def walk(current):
            if current.role == "menu item":
                if current is option:
                    current.states.add("SELECTED")
                else:
                    current.states.discard("SELECTED")
            for child in current.children:
                walk(child)

        walk(root)
        return True

    node.do_action = do_action
    return node


def _optgroup_select(*, expanded=True):
    """A Chromium select whose options sit under optgroup labels."""
    apple = _select_option("Apple", selected=True)
    cherry = _select_option("Cherry")
    banana = _select_option("Banana")
    lemon = _select_option("Lemon")
    # Chrome exposes an optgroup as a panel named with the group's label.
    red = _Node("panel", "Red", children=[apple, cherry])
    yellow = _Node("panel", "Yellow", children=[banana, lemon])
    menu = _Node("menu", "", children=[red, yellow])
    states = {"EXPANDABLE", "ENABLED", "EXPANDED"} if expanded else {"EXPANDABLE", "ENABLED"}
    select = _Node(
        "combo box", "Fruit", text="\ufffc", children=[menu],
        actions=["press", "collapse"], states=states,
    )
    select.toolkit = "Chromium"
    for node in (red, yellow, menu):
        node.toolkit = "Chromium"
    return select, apple, lemon


def test_chrome_optgroup_option_click_closes_the_popup(monkeypatch) -> None:
    """A ref click that selects the option also collapses the select.

    Collapse closes it, so Escape is not sent. The option stays selected.
    """
    from a11y_computer_use.drivers import _linux_input

    select, apple, lemon = _optgroup_select()
    keys: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    driver = _driver(monkeypatch, lemon)
    element = _element("e8", "AXMenuItem", "Lemon", clickable=True)
    assert driver.press_element(element) is True
    assert lemon.action_log == ["click"]
    assert "SELECTED" in lemon.states
    assert "SELECTED" not in apple.states
    assert "EXPANDED" not in select.states
    assert "collapse" in select.action_log
    assert keys == []
    assert ATSPIAccessor().read(select).value == "Lemon"


def test_chrome_optgroup_option_click_escapes_only_while_the_popup_stays_open(monkeypatch) -> None:
    """Escape runs only when collapse left the select expanded.

    A click that already collapsed the popup sends no key. A popup that
    stays expanded after Escape is popup_open, not a successful click.
    """
    from a11y_computer_use.drivers import _linux_input

    closed, _apple, lemon = _optgroup_select(expanded=False)

    def already_closed(index, option=lemon):
        option.action_log.append(option.actions[index])
        option.states.add("SELECTED")
        return True

    lemon.do_action = already_closed
    keys: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    driver = _driver(monkeypatch, lemon)
    element = _element("e8", "AXMenuItem", "Lemon", clickable=True)
    assert driver.press_element(element) is True
    assert keys == []
    assert "collapse" not in closed.action_log

    stuck, _apple, lemon = _optgroup_select()

    def ignore(index, node=stuck):
        node.action_log.append(node.actions[index])
        return True

    stuck.do_action = ignore
    keys.clear()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: lemon)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.detail["reason"] == "popup_open"
    assert "still open" in exc.value.message
    assert "Lemon" in exc.value.message
    assert keys == ["Escape", "Escape", "Escape", "Escape"]
    assert "EXPANDED" in stuck.states
    assert lemon.action_log == ["click"]


def test_chrome_menu_item_outside_a_select_does_not_send_escape(monkeypatch) -> None:
    """An application menu item is not a select option."""
    from a11y_computer_use.drivers import _linux_input

    item = _Node("menu item", "Save", actions=["click"])
    item.toolkit = "Chromium"
    menu = _Node("menu", "File", children=[item])
    _Node("menu bar", "", children=[menu])
    keys: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    driver = _driver(monkeypatch, item)
    element = _element("e3", "AXMenuItem", "Save", clickable=True)
    assert driver.press_element(element) is True
    assert item.action_log == ["click"]
    assert keys == []


def _gtk_combo_tree(*, open_menu: bool):
    """A GTK combo whose popup window is ACTIVE and whose menu stays under the combo."""
    item_states = {"SHOWING", "VISIBLE", "ENABLED"} if open_menu else {"VISIBLE", "ENABLED"}
    none = _Node("menu item", "None", states=set(item_states) | {"SELECTED"}, actions=["click"])
    classic = _Node("menu item", "Classic", states=set(item_states), actions=["click"])
    cobalt = _Node(
        "menu item", "Cobalt", states=set(item_states), actions=["click"],
        x=40, y=110, w=120, h=28,
    )
    menu_states = {"SHOWING", "VISIBLE", "ENABLED"} if open_menu else {"ENABLED"}
    menu_box = (40, 80, 120, 90) if open_menu else (-2147483648, -2147483648, 1, 1)
    menu = _Node("menu", "", states=menu_states, children=[none, classic, cobalt],
                 x=menu_box[0], y=menu_box[1], w=menu_box[2], h=menu_box[3])
    combo = _Node(
        "combo box", "Color", text="", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[menu], actions=["press"], x=40, y=40, w=120, h=34,
    )
    dialog = _Node(
        "dialog", "Preferences", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[combo], x=20, y=20, w=300, h=220,
    )
    frame = _Node(
        "frame", "Editor", states={"SHOWING", "VISIBLE", "ENABLED"},
        x=10, y=10, w=400, h=300,
    )
    window = _Node(
        "window", "", states={"SHOWING", "VISIBLE", "ACTIVE", "ENABLED"},
        children=[menu], x=40, y=80, w=120, h=90,
    )
    # The window lists the same menu. Its parent stays the combo.
    menu.parent = combo
    app = _Node("application", "mousepad", children=[frame, dialog, window])
    for node in (none, classic, cobalt, menu, combo, dialog, frame, window, app):
        node.toolkit = "gtk"
    return app, dialog, frame, window, combo, menu, cobalt


def _screen():
    from a11y_computer_use.schema import Display

    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    return (observe.DisplayGeometry(display=display, origin=(0.0, 0.0)),)


def test_gtk_combo_popup_stays_under_the_combo(monkeypatch) -> None:
    """The ACTIVE popup window is not the snapshot root.

    The menu items keep their names and the selected state, nested under
    the combo, and the combo reports expanded. The same items are not
    listed again from the popup window.
    """
    from a11y_computer_use.schema import Scope

    app, dialog, _frame, window, combo, _menu, _cobalt = _gtk_combo_tree(open_menu=True)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    picked = _atspi._preferred_window(
        app.children, lambda frame: "ACTIVE" in frame.states,
    )
    assert picked is dialog
    assert _atspi._combo_owning_popup(window) is combo
    assert _atspi._combo_owning_popup(dialog) is None
    snap = observe.build_snapshot(
        dialog, ATSPIAccessor(), scope=Scope.WINDOW, app="mousepad", pid=None,
        geometry=_screen(),
    )
    by_ref = {el.ref: el for el in snap.elements}
    box = next(el for el in snap.elements if el.role == "AXComboBox" and el.title == "Color")
    assert box.expanded is True
    cobalt = next(el for el in snap.elements if el.role == "AXMenuItem" and el.title == "Cobalt")
    chosen = next(el for el in snap.elements if el.role == "AXMenuItem" and el.title == "None")
    assert chosen.selected is True
    assert cobalt.selected is False
    ancestor = cobalt
    while ancestor.parent is not None and ancestor.ref != box.ref:
        ancestor = by_ref[ancestor.parent]
    assert ancestor.ref == box.ref
    rendered = observe.render_text(snap)
    assert "expanded" in rendered.split('combobox "Color"')[1].splitlines()[0]
    assert 'menuitem "Cobalt"' in rendered
    whole = observe.build_snapshot(
        app, ATSPIAccessor(), scope=Scope.APP, app="mousepad", pid=None,
        geometry=_screen(),
    )
    assert [el.title for el in whole.elements if el.role == "AXMenuItem" and el.title == "Cobalt"] == ["Cobalt"]
    assert not any(el.role == "AXWindow" and not el.title for el in whole.elements)

    _app, closed, _frame, _window, _combo, _menu, _cobalt = _gtk_combo_tree(open_menu=False)
    collapsed = observe.build_snapshot(
        closed, ATSPIAccessor(), scope=Scope.WINDOW, app="mousepad", pid=None,
        geometry=_screen(),
    )
    box = next(el for el in collapsed.elements if el.role == "AXComboBox")
    assert box.expanded is False
    assert not any(el.role == "AXMenuItem" for el in collapsed.elements)
    assert "collapsed" in observe.render_text(collapsed)


def _gtk_popup_menu(names, *, showing=True, selected=None, role="menu", title=""):
    """Fake menu. ``showing`` marks the menu and its items SHOWING."""
    states = {"ENABLED", "SENSITIVE"}
    if showing:
        states |= {"SHOWING", "VISIBLE"}
    items = []
    for name in names:
        item_states = {"ENABLED", "SENSITIVE", "VISIBLE"}
        if showing:
            item_states.add("SHOWING")
        if name == selected:
            item_states.add("SELECTED")
        items.append(_Node(
            "menu item", name, states=item_states, actions=["click"],
            x=40, y=80, w=160, h=24,
        ))
    return _Node(
        role, title, states=states, children=items,
        x=40, y=80, w=160, h=24 * max(len(names), 1),
    )


def _stamp_gtk(nodes) -> None:
    for node in nodes:
        node.toolkit = "gtk"


def test_separate_gtk_combo_popup_is_not_listed_twice(monkeypatch) -> None:
    """The same open options are not listed again as an application menu.

    The combo keeps a SHOWING menu. A second menu under the application,
    and an untitled window whose own menu is not parented to the combo,
    repeat those titles. Both are the popup. A context menu and a named
    File menu stay. These nodes are fakes. A Chromium menu is not a GTK
    combo popup.
    """
    from a11y_computer_use.schema import Scope

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    sizes = ("Very Small", "Smaller", "Small", "Normal", "Large", "Larger", "Very Large")
    nested = _gtk_popup_menu(sizes, selected="Large")
    duplicate = _gtk_popup_menu(sizes, selected="Large")
    context = _gtk_popup_menu(("Cut", "Copy", "Paste"))
    file_menu = _gtk_popup_menu(("New", "Open"), title="File")
    combo = _Node(
        "combo box", "Large", text="", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[nested], actions=["press"], x=40, y=40, w=160, h=34,
    )
    dialog = _Node(
        "dialog", "Application Finder", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[combo], x=20, y=20, w=420, h=320,
    )
    frame = _Node(
        "frame", "Application Finder", states={"SHOWING", "VISIBLE", "ENABLED"},
        x=10, y=10, w=640, h=480,
    )
    app = _Node(
        "application", "xfce4-appfinder",
        children=[frame, dialog, duplicate, context, file_menu],
    )
    _stamp_gtk([
        nested, duplicate, context, file_menu, combo, dialog, frame, app,
        *nested.children, *duplicate.children, *context.children, *file_menu.children,
    ])
    assert _atspi._combo_owning_popup(duplicate) is combo
    assert _atspi._combo_owning_popup(context) is None
    assert _atspi._combo_owning_popup(file_menu) is None
    snap = observe.build_snapshot(
        app, ATSPIAccessor(), scope=Scope.APP, app="xfce4-appfinder", pid=None,
        geometry=_screen(),
    )
    titles = [el.title for el in snap.elements if el.role == "AXMenuItem"]
    assert titles.count("Very Small") == 1
    assert titles.count("Very Large") == 1
    assert [title for title in titles if title in {"Cut", "Copy", "Paste", "New", "Open"}] == [
        "Cut", "Copy", "Paste", "New", "Open",
    ]
    by_ref = {el.ref: el for el in snap.elements}
    small = next(el for el in snap.elements if el.role == "AXMenuItem" and el.title == "Very Small")
    box = next(el for el in snap.elements if el.role == "AXComboBox")
    ancestor = small
    while ancestor.parent is not None and ancestor.ref != box.ref:
        ancestor = by_ref[ancestor.parent]
    assert ancestor.ref == box.ref

    popup_items = _gtk_popup_menu(sizes, selected="Large")
    popup_menu = popup_items
    window = _Node(
        "window", "", states={"SHOWING", "VISIBLE", "ACTIVE", "ENABLED"},
        children=[popup_menu], x=40, y=80, w=180, h=200,
    )
    owned = _gtk_popup_menu(sizes, selected="Large")
    other = _Node(
        "combo box", "Large", text="", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[owned], actions=["press"], x=40, y=40, w=160, h=34,
    )
    prefs = _Node(
        "dialog", "Preferences", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[other], x=20, y=20, w=420, h=320,
    )
    editor = _Node(
        "frame", "Editor", states={"SHOWING", "VISIBLE", "ENABLED"},
        x=10, y=10, w=640, h=480,
    )
    mousepad = _Node("application", "mousepad", children=[editor, prefs, window])
    _stamp_gtk([
        popup_menu, window, owned, other, prefs, editor, mousepad,
        *popup_menu.children, *owned.children,
    ])
    assert popup_menu.parent is window
    assert _atspi._combo_owning_popup(window) is other
    picked = _atspi._preferred_window(
        mousepad.children, lambda frame: "ACTIVE" in frame.states,
    )
    assert picked is prefs
    whole = observe.build_snapshot(
        mousepad, ATSPIAccessor(), scope=Scope.APP, app="mousepad", pid=None,
        geometry=_screen(),
    )
    assert [el.title for el in whole.elements if el.role == "AXMenuItem" and el.title == "Large"].count(
        "Large"
    ) == 1
    assert not any(el.role == "AXWindow" and not el.title for el in whole.elements)

    chrome_menu = _gtk_popup_menu(sizes, selected="Large")
    chrome_nested = _gtk_popup_menu(sizes, selected="Large")
    chrome_combo = _Node(
        "combo box", "Large", text="", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[chrome_nested], actions=["press"], x=40, y=40, w=160, h=34,
    )
    chrome_frame = _Node(
        "frame", "Chrome", states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[chrome_combo], x=10, y=10, w=640, h=480,
    )
    chrome = _Node(
        "application", "Google Chrome", children=[chrome_frame, chrome_menu],
    )
    for node in (
        chrome_menu, chrome_nested, chrome_combo, chrome_frame, chrome,
        *chrome_menu.children, *chrome_nested.children,
    ):
        node.toolkit = "chrome"
    assert _atspi._combo_owning_popup(chrome_menu) is None
    chrome_snap = observe.build_snapshot(
        chrome, ATSPIAccessor(), scope=Scope.APP, app="Google Chrome", pid=None,
        geometry=_screen(),
    )
    assert [el.title for el in chrome_snap.elements if el.role == "AXMenuItem"].count("Very Small") == 2


def test_gtk_combo_option_click_closes_the_popup(monkeypatch) -> None:
    """A ref click on a GTK option collapses the combo and sends no Escape.

    A popup that stays open is popup_open. A menu-bar item is not a combo
    option, so it does not send Escape either.
    """
    from a11y_computer_use.drivers import _linux_input

    _app, _dialog, _frame, _window, combo, menu, cobalt = _gtk_combo_tree(open_menu=True)

    def choose(index, option=cobalt, popup=menu):
        option.action_log.append(option.actions[index])
        for child in popup.children:
            child.states.discard("SELECTED")
        option.states.add("SELECTED")
        popup.states.discard("SHOWING")
        popup.states.discard("VISIBLE")
        return True

    cobalt.do_action = choose
    keys: list[str] = []
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    driver = _driver(monkeypatch, cobalt)
    element = _element("e8", "AXMenuItem", "Cobalt", clickable=True)
    assert driver.press_element(element) is True
    assert cobalt.action_log == ["click"]
    assert "SELECTED" in cobalt.states
    assert keys == []
    assert ATSPIAccessor().read(combo).expanded is False

    _app, _dialog, _frame, _window, stuck, menu, cobalt = _gtk_combo_tree(open_menu=True)

    def ignore(index, option=cobalt):
        option.action_log.append(option.actions[index])
        return True

    cobalt.do_action = ignore
    stuck.do_action = ignore
    keys.clear()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: cobalt)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.detail["reason"] == "popup_open"
    assert "Cobalt" in exc.value.message
    assert keys == ["Escape", "Escape", "Escape", "Escape"]

    item = _Node("menu item", "Save", actions=["click"])
    item.toolkit = "gtk"
    bar_menu = _Node("menu", "File", children=[item])
    bar_menu.toolkit = "gtk"
    _Node("menu bar", "", children=[bar_menu]).toolkit = "gtk"
    keys.clear()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: item)
    assert driver.press_element(_element("e3", "AXMenuItem", "Save", clickable=True)) is True
    assert keys == []
    assert item.action_log == ["click"]


def test_spin_button_uses_the_value_interface_and_rejects_out_of_range(monkeypatch) -> None:
    spin = _Node("spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0)
    driver = _driver(monkeypatch, spin)
    element = _element("e6", "AXTextField", "Quantity", editable=True)
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "15")
    assert "15" in str(exc.value) and "0" in str(exc.value) and "10" in str(exc.value)
    assert spin.value == 3.0 and spin.value_sets == []
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "-2")
    assert spin.value_sets == []
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "abc")
    assert "not a number" in str(exc.value)
    assert driver.set_value(element, "7") is True
    assert spin.value == 7.0 and spin.value_sets == [7.0]

    stuck = _Node("spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0)
    stuck.hold_value = True
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: stuck)
    with pytest.raises(ComputerUseError) as exc:
        driver.set_value(element, "7")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert stuck.value == 3.0


def test_spin_button_rounds_off_step_values_and_reports_what_it_holds(monkeypatch) -> None:
    spin = _Node("spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0)
    spin.increment = 1
    driver = _driver(monkeypatch, spin)
    element = _element("e6", "AXTextField", "Quantity", editable=True)
    assert driver.set_value(element, "4.6") == "5"
    assert spin.value == 5.0
    assert spin.value_sets == [5.0]

    shown = _Node("spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: shown)
    assert driver.set_value(element, "4.6") == "5"
    assert shown.value == 5.0

    number = _Entry("3", "Seats")
    number.role = "spin button"
    number.value = 3.0
    number.minimum = 0.0
    number.maximum = 100.0
    number.increment = 1
    number.attrs = {"tag": "input", "text-input-type": "number"}
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: number)
    field = _element("e5", "AXTextField", "Seats", editable=True)
    assert driver.set_value(field, "4.6") is True
    assert number.value == 4.6
    assert driver.set_value(field, "") is True
    assert number.text == ""
    assert number.value_sets == [4.6]

    gtk = _Node("spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: gtk)
    with pytest.raises(ValueError):
        driver.set_value(element, "")
    assert gtk.value == 3.0


def test_runtime_reports_the_spin_value_that_was_held() -> None:
    from a11y_computer_use import server

    element = _element("e6", "AXTextField", "Quantity", editable=True)
    snap = type("S", (), {"app": "gtk"})()
    runtime = server.Runtime.__new__(server.Runtime)
    runtime._resolve = lambda ref, kind: (snap, element)
    runtime._run_gated = lambda action, app, execute, **_kwargs: execute()
    runtime._refuse_disabled = lambda target, verb="": None

    class _Driver:
        def set_value(self, _element, _value):
            return "5"

    runtime.driver = _Driver()
    assert runtime.set_value("e6", "4.6") == "set e6 = '5'"


def test_empty_chrome_number_is_typed_and_zero_is_not_a_false_success(monkeypatch) -> None:
    """An empty bounded number input is not 0 just because Value reads 0.0."""
    number = _Node(
        "spin button", "Guests", text="", value=0.0, minimum=0.0, maximum=10.0,
        attrs={"tag": "input", "text-input-type": "number"},
    )
    number.hold_value = True
    driver = _driver(monkeypatch, number)
    typed: list[str] = []

    def type_text(text, _node=number):
        typed.append(text)
        _node.text = (_node.text or "") + text

    def click_center(node) -> None:
        node.states.add("FOCUSED")

    monkeypatch.setattr(_atspi, "_type_string", type_text)
    monkeypatch.setattr(_atspi, "_click_center", click_center)
    field = _element("e9", "AXTextField", "Guests", editable=True)
    assert driver.set_value(field, "0") is True
    assert number.text == "0"
    assert typed == ["0"]
    assert number.value_sets == []
    assert number.value == 0.0

    number.text = ""
    assert driver.set_value(field, "7") is True
    assert number.text == "7"
    assert typed == ["0", "7"]

    number.text = ""

    def miss(text, _node=number):
        typed.append("miss:" + text)

    monkeypatch.setattr(_atspi, "_type_string", miss)
    with pytest.raises(ComputerUseError) as exc:
        driver.set_value(field, "3")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert number.text == ""
    assert number.value_sets == []
    assert number.value == 0.0


def test_chrome_number_without_editable_text_is_cleared_by_a_click_and_keys(monkeypatch) -> None:
    """Chrome 154's number input has no EditableText. A click has to land first.

    The generic clear sends ctrl+a and BackSpace after grab_focus and stops
    after a few short reads. This field stays ``3`` until a click has been
    recorded, which is the caret moving into the input, and then until
    BackSpace follows ctrl+a.
    """
    number = _Node(
        "spin button", "Seats", text="3", value=3.0, minimum=None, maximum=None,
        attrs={"tag": "input", "text-input-type": "number"},
    )
    clicked: list[tuple[int, int]] = []
    chords: list[str] = []

    def click(x, y, **_kwargs):
        clicked.append((x, y))
        number.states.add("FOCUSED")

    def press_chord(chord: str) -> None:
        chords.append(chord)
        if chord == "backspace" and clicked and "ctrl+a" in chords:
            number.text = ""

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    from a11y_computer_use.drivers import _linux_input

    monkeypatch.setattr(_linux_input, "click", click)
    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    assert _atspi.set_numeric_value(number, "") is True
    assert number.text == ""
    assert clicked
    assert chords[:3] == ["ctrl+a", "backspace", "delete"]


def _number_and_name(number_states=()):
    """A Count spin button and a Name entry under one document."""
    name = _Node(
        "entry", "Name", text="Ayşe café ₸",
        attrs={"tag": "input", "id": "name", "text-input-type": "text"},
    )
    number = _Node(
        "spin button", "Count", text="", value=0.0, minimum=0.0, maximum=100.0,
        states=set(number_states),
        attrs={"tag": "input", "text-input-type": "number", "id": "count"},
    )
    _Node("document web", "form", children=[name, number])
    return name, number


def test_number_set_value_types_nothing_when_focus_is_elsewhere(monkeypatch) -> None:
    """Focus stays on Name. The digits are not sent, and Name is unchanged."""
    name, number = _number_and_name()
    name.states.add("FOCUSED")
    typed: list[str] = []

    def type_string(text: str) -> None:
        typed.append(text)
        name.text += text

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_atspi, "_type_string", type_string)
    monkeypatch.setattr(_atspi, "_click_center", lambda _node: None)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_numeric_value(number, "12")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["reason"] == "focus_lost"
    assert exc.value.detail["outcome"] == "refused"
    assert "keyboard" not in exc.value.detail["next"]
    assert typed == []
    assert name.text == "Ayşe café ₸"
    assert number.text == ""


def test_number_set_value_stops_when_focus_moves_mid_write(monkeypatch) -> None:
    """The first digit lands in Count. Focus then moves. The next digit is not sent."""
    name, number = _number_and_name({"FOCUSED"})
    typed: list[str] = []

    def type_string(text: str) -> None:
        typed.append(text)
        if "FOCUSED" not in number.states:
            name.text += text
            return
        number.text += text
        number.states.discard("FOCUSED")
        name.states.add("FOCUSED")

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_atspi, "_type_string", type_string)
    monkeypatch.setattr(_atspi, "_click_center", lambda node: node.states.add("FOCUSED"))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_numeric_value(number, "12")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["outcome"] == "refused"
    assert typed == ["1"]
    assert number.text == "1"
    assert name.text == "Ayşe café ₸"


def test_failed_set_value_reports_focus_lost_when_another_field_changes(monkeypatch) -> None:
    """Keys that land in Name are not reported as a plain text_mismatch."""
    name, number = _number_and_name({"FOCUSED"})

    def type_string(text: str) -> None:
        name.text += text

    monkeypatch.setattr(_atspi, "_type_string", type_string)
    monkeypatch.setattr(_atspi, "_click_center", lambda node: node.states.add("FOCUSED"))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    driver = _driver(monkeypatch, number)
    field = _element("e9", "AXTextField", "Count", editable=True)
    with pytest.raises(ComputerUseError) as exc:
        driver.set_value(field, "12")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["reason"] == "focus_lost"
    assert exc.value.detail["outcome"] == "refused"
    assert exc.value.detail["changed_fields"]
    assert name.text == "Ayşe café ₸12"
    assert number.text == ""


def test_linux_runtime_does_not_type_when_focus_cannot_be_confirmed() -> None:
    """A Linux set_value that returns False must not fall through to type_text."""
    from a11y_computer_use import server

    element = _element("e9", "AXTextField", "Count", editable=True)
    snap = type("S", (), {"app": "chrome", "scope": None})()
    runtime = server.Runtime.__new__(server.Runtime)
    runtime._resolve = lambda ref, kind: (snap, element)
    runtime._run_gated = lambda action, app, execute, **_kwargs: execute()
    runtime._refuse_disabled = lambda target, verb="": None
    calls: list[str] = []

    class _Driver:
        name = "linux"

        def set_value(self, _element, _value):
            return False

        def press_element(self, _element):
            calls.append("press")
            return True

        def type_text(self, text, pid=None):
            calls.append(text)

    runtime.driver = _Driver()
    with pytest.raises(ComputerUseError) as exc:
        runtime.set_value("e9", "12")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert exc.value.detail["outcome"] == "refused"
    assert calls == []


def test_slider_set_value_uses_the_value_interface(monkeypatch) -> None:
    slider = _Node("slider", "Volume", text=None, value=40.0, minimum=0.0, maximum=100.0)
    driver = _driver(monkeypatch, slider)
    element = _element("e7", "AXSlider", "Volume", clickable=True)
    assert driver.set_value(element, "55") is True
    assert slider.value == 55.0
    with pytest.raises(ValueError) as exc:
        driver.set_value(element, "150")
    assert "0" in str(exc.value) and "100" in str(exc.value)
    assert slider.value == 55.0


def _table():
    rows = []
    for name in ("Row A", "Row B", "Row C", "Row D", "Row E"):
        states = {"SELECTED"} if name == "Row B" else set()
        rows.append(_Node(
            "table cell", name, states=states,
            actions=["expand or contract", "edit", "activate"],
            y=30 + 20 * len(rows),
        ))
    table = _Node("table", "rows", children=rows, y=20, h=140)
    return table, rows


def test_tree_row_click_selects_through_the_parent(monkeypatch) -> None:
    table, rows = _table()
    target = rows[2]
    driver = _driver(monkeypatch, target)
    clicked = []
    driver.click = lambda element, **_kwargs: clicked.append(element.ref)  # type: ignore[method-assign]
    element = _element("e19", "AXGroup", "Row C", clickable=True, y=70)
    assert driver.press_element(element) is True
    assert "SELECTED" in target.states
    assert "SELECTED" not in rows[1].states
    assert target.action_log == []
    assert clicked == []


def test_tree_row_click_uses_the_center_when_selection_does_not(monkeypatch) -> None:
    table, rows = _table()
    table.select_fails = True
    target = rows[2]
    driver = _driver(monkeypatch, target)

    def click(element, **_kwargs):
        target.states.add("SELECTED")
        rows[1].states.discard("SELECTED")

    driver.click = click  # type: ignore[method-assign]
    element = _element("e19", "AXGroup", "Row C", clickable=True, y=70)
    assert driver.press_element(element) is True
    assert "SELECTED" in target.states
    assert target.action_log == []


def _treeitem(name, *, expanded=False, checked=None, actions=("click",)):
    """An ARIA treeitem row. The tree exposes Selection. The item does not select."""
    states = {"EXPANDABLE", "SELECTABLE", "ENABLED", "SENSITIVE", "SHOWING"}
    if expanded:
        states.add("EXPANDED")
    if checked is not None:
        states.add("CHECKABLE")
        if checked:
            states.add("CHECKED")
    item = _Node("tree item", name, states=states, actions=list(actions))
    tree = _Node("tree", "Files", children=[item])
    tree.select_fails = True
    return tree, item


def test_aria_treeitem_click_waits_for_expanded_and_does_not_click_again(monkeypatch) -> None:
    """The click action flips the node a beat later. A second click is not sent.

    Chrome returns from the click action while the treeitem is still
    collapsed, then publishes EXPANDED. The selection does not move. A
    pointer click at the center would run the handler again and collapse it.
    """
    _tree, item = _treeitem("Folder A")
    driver = _driver(monkeypatch, item)

    def do_action(index, _node=item):
        item.action_log.append(item.actions[index])
        item.armed = True
        return True

    def sleep(_seconds, _node=item):
        if getattr(_node, "armed", False) and "EXPANDED" not in _node.states:
            _node.states.add("EXPANDED")

    item.do_action = do_action
    monkeypatch.setattr(_atspi.time, "sleep", sleep)
    clicks: list[str] = []

    def click(element, **_kwargs):
        clicks.append(element.ref)
        if "EXPANDED" in item.states:
            item.states.discard("EXPANDED")
        else:
            item.states.add("EXPANDED")

    driver.click = click  # type: ignore[method-assign]
    element = _element("e26", "AXRow", "Folder A", clickable=True)
    assert driver.press_element(element) is True
    assert "EXPANDED" in item.states
    assert "SELECTED" not in item.states
    assert item.action_log == ["click"]
    assert clicks == []


def test_aria_treeitem_collapse_is_one_activation(monkeypatch) -> None:
    """A click on an open treeitem leaves it collapsed. The pointer is not used."""
    _tree, item = _treeitem("Folder A", expanded=True)
    driver = _driver(monkeypatch, item)

    def do_action(index, _node=item):
        item.action_log.append(item.actions[index])
        item.states.discard("EXPANDED")
        return True

    item.do_action = do_action
    clicks: list[str] = []
    driver.click = lambda element, **_kwargs: clicks.append(element.ref)  # type: ignore[method-assign]
    element = _element("e26", "AXRow", "Folder A", clickable=True)
    assert driver.press_element(element) is True
    assert "EXPANDED" not in item.states
    assert item.action_log == ["click"]
    assert clicks == []


def test_aria_treeitem_click_that_changes_nothing_does_not_click_again(monkeypatch) -> None:
    """A toggle row whose click action changes nothing is not clicked twice."""
    _tree, item = _treeitem("Folder A")
    driver = _driver(monkeypatch, item)

    def do_action(index, _node=item):
        item.action_log.append(item.actions[index])
        return True

    item.do_action = do_action
    clicks: list[str] = []
    driver.click = lambda element, **_kwargs: clicks.append(element.ref)  # type: ignore[method-assign]
    element = _element("e26", "AXRow", "Folder A", clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert item.action_log == ["click"]
    assert clicks == []
    assert "EXPANDED" not in item.states


def test_checkable_row_click_toggles_once(monkeypatch) -> None:
    """A checkable row's click sets CHECKED. A second click is not sent."""
    states = {"CHECKABLE", "SELECTABLE", "ENABLED"}
    item = _Node("table cell", "Agree", states=states, actions=["click"])
    table = _Node("table", "Rows", children=[item])
    table.select_fails = True
    driver = _driver(monkeypatch, item)

    def do_action(index, _node=item):
        item.action_log.append(item.actions[index])
        item.states.add("CHECKED")
        return True

    item.do_action = do_action
    clicks: list[str] = []
    driver.click = lambda element, **_kwargs: clicks.append(element.ref)  # type: ignore[method-assign]
    element = _element("e26", "AXRow", "Agree", clickable=True)
    assert driver.press_element(element) is True
    assert "CHECKED" in item.states
    assert "SELECTED" not in item.states
    assert item.action_log == ["click"]
    assert clicks == []


def test_expandable_gtk_cell_still_uses_the_center_when_selection_does_not(monkeypatch) -> None:
    """A GTK cell's expand action is not a click. The center click still selects."""
    _table_node, rows = _table()
    _table_node.select_fails = True
    target = rows[2]
    target.states.add("EXPANDABLE")
    driver = _driver(monkeypatch, target)
    clicks: list[str] = []

    def click(element, **_kwargs):
        clicks.append(element.ref)
        target.states.add("SELECTED")
        rows[1].states.discard("SELECTED")

    driver.click = click  # type: ignore[method-assign]
    element = _element("e19", "AXGroup", "Row C", clickable=True, y=70)
    assert driver.press_element(element) is True
    assert "SELECTED" in target.states
    assert target.action_log == []
    assert clicks == ["e19"]


def _window(entry_text, body, *, crumb=None):
    """A frame whose toolbar is a location entry or one path-bar button."""
    if crumb is not None:
        bar_child = crumb
    else:
        bar_child = _Node("entry", "", text=entry_text)
    toolbar = _Node("tool bar", "", children=[bar_child])
    panel = _Node("panel", "", children=[toolbar, body])
    return _Node("frame", "Files", children=[panel]), bar_child


def _side_place(entry_text="/home/ubuntu/"):
    """A Thunar shortcut cell outside the folder view, with a location entry."""
    place = _Node(
        "table cell", "File System",
        actions=["expand or contract", "edit", "activate"],
        states={"ENABLED", "SENSITIVE", "SHOWING"},
        x=100, y=200, w=130, h=24,
    )
    row = _Node(
        "table cell", "", children=[place],
        actions=["expand or contract", "edit", "activate"],
    )
    table = _Node("table", "", children=[row])
    scroll = _Node("scroll pane", "", children=[table])
    filler = _Node("filler", "", children=[scroll])
    split = _Node("split pane", "", children=[filler])
    frame, entry = _window(entry_text, split)
    return place, entry, frame


def test_side_pane_click_uses_the_pointer_and_not_activate(monkeypatch) -> None:
    """A shortcut cell opens on a pointer click. Selection and activate do not.

    The cell's first actions are expand and edit. activate reports success
    and leaves the folder. The click is the row's center.
    """
    place, entry, _frame = _side_place()
    driver = _driver(monkeypatch, place)
    clicked = []

    def click(element, **_kwargs):
        clicked.append((element.ref, element.bounds.x, element.bounds.y))
        entry.text = "/"

    driver.click = click  # type: ignore[method-assign]
    element = _element("e33", "AXCell", "File System", clickable=True, x=100, y=200, w=130, h=24)
    assert _atspi.navigation_gesture(place) == "pointer"
    assert driver.press_element(element) is True
    assert clicked == [("e33", 100, 200)]
    assert place.action_log == []
    assert "SELECTED" not in place.states
    assert entry.text == "/"


def test_location_entry_nested_under_a_wide_toolbar_is_the_place() -> None:
    """Thunar nests the location entry under a toolbar of many empty panels.

    The entry is panel, panel, filler, then a text field, and that panel is
    not one of the first few toolbar children. The walk has to reach it.
    """
    entry = _Node("text", "", text="/home/ubuntu/")
    nested = _Node("panel", "", children=[
        _Node("panel", "", children=[
            _Node("filler", "", children=[entry]),
        ]),
    ])
    slots = []
    for index in range(18):
        if index == 14:
            slots.append(nested)
        else:
            slots.append(_Node("panel", "", children=[
                _Node("push button", "Back" if index == 0 else ""),
            ]))
    toolbar = _Node("tool bar", "", children=slots)
    place = _Node(
        "table cell", "File System",
        actions=["expand or contract", "edit", "activate"],
        states={"ENABLED"},
    )
    table = _Node("table", "", children=[place])
    split = _Node("split pane", "", children=[table])
    panel = _Node("panel", "", children=[toolbar, split])
    frame = _Node("frame", "ubuntu - Thunar", children=[panel])
    assert _atspi.folder_place(place) == ("/home/ubuntu/", "location")
    assert _atspi.navigating_folder_row(place) is True
    del frame


def test_folder_view_row_still_selects(monkeypatch) -> None:
    """A file row inside the view tab is selected. It is not opened."""
    file_cell = _Node(
        "table cell", "adduser",
        actions=["expand or contract", "edit", "activate"],
        states={"ENABLED", "SENSITIVE"},
    )
    file_table = _Node("table", "", children=[file_cell])
    view = _Node("page tab list", "", children=[file_table])
    split = _Node("split pane", "", children=[view])
    _frame, entry = _window("/usr/share/doc/", split)
    driver = _driver(monkeypatch, file_cell)
    clicked = []
    driver.click = lambda element, **_kwargs: clicked.append(element.ref)  # type: ignore[method-assign]
    element = _element("e47", "AXCell", "adduser", clickable=True)
    assert _atspi.navigation_gesture(file_cell) is None
    assert driver.press_element(element) is True
    assert clicked == []
    assert file_cell.action_log == []
    assert "SELECTED" in file_cell.states
    assert entry.text == "/usr/share/doc/"


def test_tree_row_with_its_own_activate_action_uses_that_action(monkeypatch) -> None:
    """A tree row whose only action is activate opens through that action.

    Expand and edit are not present, so activate is the row's own
    activation. The pointer is not sent.
    """
    item = _Node("tree item", "Documents", actions=["activate"], states={"ENABLED"})
    tree = _Node("tree", "Folders", children=[item])
    frame, entry = _window("/home/ubuntu/", tree)

    def do_action(index, _node=item):
        item.action_log.append(item.actions[index])
        entry.text = "/home/ubuntu/Documents"
        return True

    item.do_action = do_action
    driver = _driver(monkeypatch, item)
    clicked = []
    driver.click = lambda element, **_kwargs: clicked.append(element.ref)  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Documents", clickable=True)
    assert _atspi.navigation_gesture(item) == "activate"
    assert driver.press_element(element) is True
    assert item.action_log == ["activate"]
    assert clicked == []
    assert entry.text == "/home/ubuntu/Documents"
    del frame


def test_return_opens_a_navigating_row_that_has_no_box(monkeypatch) -> None:
    """Return is the activation only when the row has no box and no activate."""
    from a11y_computer_use.drivers import _linux_input

    item = _Node("tree item", "Remote", actions=[], states={"ENABLED"}, w=0, h=0)
    tree = _Node("tree", "Folders", children=[item])
    _frame, _entry = _window("/home/ubuntu/", tree)
    driver = _driver(monkeypatch, item)
    keys = []
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(_linux_input, "press_chord", lambda key: keys.append(key))
    element = _element("e9", "AXRow", "Remote", clickable=True, w=0, h=0)
    assert _atspi.navigation_gesture(item) == "return"
    assert driver.press_element(element) is True
    assert keys == ["Return"]
    assert "SELECTED" in item.states


def test_path_bar_change_confirms_and_a_selected_row_does_not(monkeypatch) -> None:
    """The path bar is the location when there is no entry.

    A different crumb confirms. The same crumb is partial, even if the
    row became selected.
    """
    crumb = _Node("push button", "ubuntu", states={"CHECKED"})
    place = _Node(
        "table cell", "File System",
        actions=["expand or contract", "edit", "activate"],
        states={"ENABLED"},
    )
    row = _Node("table cell", "", children=[place])
    table = _Node("table", "", children=[row])
    split = _Node("split pane", "", children=[table])
    frame, _button = _window("", split, crumb=crumb)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi, "_FOLDER_LOCATION_WAIT_S", 0)
    assert _atspi.folder_place(place) == ("ubuntu", "path bar")
    crumb.name = "File System"
    assert _atspi.folder_navigation_verdict(place, "ubuntu") == (
        "confirmed", "the path bar changed",
    )
    crumb.name = "ubuntu"
    assert _atspi.folder_navigation_verdict(place, "ubuntu") == (
        "partial", "the row was selected but the location did not change",
    )
    del frame


def test_tree_row_click_errors_when_the_selection_stays_put(monkeypatch) -> None:
    table, rows = _table()
    table.select_fails = True
    target = rows[2]
    driver = _driver(monkeypatch, target)
    driver.click = lambda element, **_kwargs: None  # type: ignore[method-assign]
    element = _element("e19", "AXGroup", "Row C", clickable=True, y=70)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert "SELECTED" in rows[1].states
    assert "SELECTED" not in target.states


def test_chrome_list_click_that_does_not_select_is_not_success(monkeypatch) -> None:
    cherry = _Node("list item", "Cherry", actions=["click"])

    def noop(index, _node=cherry):
        cherry.action_log.append(cherry.actions[index])
        return True

    cherry.do_action = noop
    box = _Node("list box", "Fruits", children=[
        _Node("list item", "Apple"),
        _Node("list item", "Banana", states={"SELECTED"}),
        cherry,
    ])
    box.select_fails = True
    driver = _driver(monkeypatch, cherry)

    def click(element, **_kwargs):
        cherry.states.add("SELECTED")

    driver.click = click  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Cherry", clickable=True)
    assert driver.press_element(element) is True
    assert "SELECTED" in cherry.states
    assert cherry.action_log == ["click"]

    cherry.states.discard("SELECTED")
    driver.click = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert "SELECTED" not in cherry.states


def _chrome_list():
    apple = _Node("list item", "Apple", actions=["select"])
    banana = _Node("list item", "Banana", states={"SELECTED"}, actions=["select"])
    cherry = _Node("list item", "Cherry", actions=["select"])
    box = _Node("list box", "Fruits", children=[apple, banana, cherry])
    box.toolkit = "Chromium"
    return box, apple, banana, cherry


def test_chrome_list_row_is_selected_by_a_center_click(monkeypatch) -> None:
    """The option select action is not used. A center click selects the row."""
    box, _apple, banana, cherry = _chrome_list()
    driver = _driver(monkeypatch, cherry)

    def click(element, **_kwargs):
        for child in box.children:
            child.states.discard("SELECTED")
        cherry.states.add("SELECTED")

    driver.click = click  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Cherry", clickable=True)
    assert driver.press_element(element) is True
    assert "SELECTED" in cherry.states
    assert cherry.action_log == []
    assert banana.action_log == []


def test_chrome_list_click_that_misses_restores_the_previous_selection(monkeypatch) -> None:
    """A click that does not select must not leave the list empty."""
    box, _apple, banana, cherry = _chrome_list()
    driver = _driver(monkeypatch, cherry)

    def click(element, **_kwargs):
        for child in box.children:
            child.states.discard("SELECTED")

    driver.click = click  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Cherry", clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert "SELECTED" in banana.states
    assert "SELECTED" not in cherry.states
    assert cherry.action_log == []
    assert banana.action_log == []


def test_chrome_list_option_click_still_selects(monkeypatch) -> None:
    cherry = _Node("list item", "Cherry", actions=["click"])
    box = _Node("list box", "Fruits", children=[
        _Node("list item", "Apple"),
        _Node("list item", "Banana", states={"SELECTED"}),
        cherry,
    ])
    driver = _driver(monkeypatch, cherry)
    element = _element("e8", "AXRow", "Cherry", clickable=True)
    assert driver.press_element(element) is True
    assert "SELECTED" in cherry.states
    assert cherry.action_log == ["click"]


class _Relation:
    def __init__(self, kind, targets):
        self.kind = kind
        self.targets = list(targets)

    def get_relation_type(self):
        return self.kind

    def get_n_targets(self):
        return len(self.targets)

    def get_target(self, index):
        return self.targets[index]


def _aria_city(*, toolkit="Chromium", nest=False):
    """An ARIA combobox and its listbox. ``nest`` hangs the list off the input."""
    city = _Node(
        "entry", "City", text="",
        states={"EXPANDABLE", "EXPANDED", "ENABLED", "EDITABLE", "FOCUSABLE", "SENSITIVE"},
        attrs={"xml-roles": "combobox", "tag": "input", "id": "city"},
    )
    berlin = _Node("list item", "Berlin", actions=["click"])
    bern = _Node("list item", "Bern", actions=["click"])
    box = _Node(
        "list box", "cities", children=[berlin, bern],
        attrs={"xml-roles": "listbox", "tag": "ul", "id": "lb"},
    )
    box.toolkit = toolkit
    if nest:
        city.children.append(box)
        box.parent = city
        box.get_relation_set = lambda: []
    else:
        box.get_relation_set = lambda: [_Relation("controlled_by", [city])]
    return city, box, berlin, bern


def _close_on(city, box, option, *, text=None, name=None, collapse=True, selected_attr=False):
    """The option handler: set the input, and drop the row when the list closes."""

    def click(_element, **_kwargs):
        if text is not None:
            city.text = text
        if name is not None:
            city.name = name
        if selected_attr:
            option.attrs["aria-selected"] = "true"
        if collapse:
            city.states.discard("EXPANDED")
            option.states.add("DEFUNCT")
            option.parent = None
            box.children = [child for child in box.children if child is not option]

    return click


def test_aria_combobox_option_click_reports_success_when_the_list_closes(monkeypatch) -> None:
    """The row disappears. The combobox value is the selection."""
    city, box, _berlin, bern = _aria_city()
    driver = _driver(monkeypatch, bern)
    driver.click = _close_on(city, box, bern, text="Bern")  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    assert driver.press_element(element) is True
    assert city.text == "Bern"
    assert "EXPANDED" not in city.states
    assert "SELECTED" not in bern.states
    assert "DEFUNCT" in bern.states


def test_aria_combobox_option_nested_under_the_input_reports_success(monkeypatch) -> None:
    """The list is a child of the combobox, with no controls relation."""
    city, box, _berlin, bern = _aria_city(nest=True)
    driver = _driver(monkeypatch, bern)
    driver.click = _close_on(city, box, bern, text="Bern")  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    assert driver.press_element(element) is True
    assert city.text == "Bern"


def test_aria_combobox_option_click_confirms_from_aria_selected(monkeypatch) -> None:
    """aria-selected is the commit when the input text does not change."""
    city, _box, _berlin, bern = _aria_city()
    driver = _driver(monkeypatch, bern)

    def click(_element, **_kwargs):
        bern.attrs["aria-selected"] = "true"

    driver.click = click  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    assert driver.press_element(element) is True
    assert "SELECTED" not in bern.states
    assert city.text == ""
    assert bern.attrs["aria-selected"] == "true"


def test_aria_combobox_option_click_confirms_when_the_popup_collapses_onto_the_value(monkeypatch) -> None:
    """The input already shows the option. Closing the popup is the change."""
    city, box, _berlin, bern = _aria_city()
    city.text = "Bern"
    driver = _driver(monkeypatch, bern)
    driver.click = _close_on(city, box, bern, collapse=True)  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    assert driver.press_element(element) is True
    assert city.text == "Bern"
    assert "EXPANDED" not in city.states


def test_aria_combobox_option_click_confirms_when_the_name_becomes_the_option(monkeypatch) -> None:
    """Some trees publish the current item as the accessible name."""
    city, box, _berlin, bern = _aria_city()
    driver = _driver(monkeypatch, bern)
    driver.click = _close_on(city, box, bern, name="Bern")  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    assert driver.press_element(element) is True
    assert city.name == "Bern"
    assert city.text == ""


def test_aria_combobox_option_click_that_changes_nothing_is_unsupported(monkeypatch) -> None:
    """A click that leaves the value, the name, and the popup alone is a failure."""
    city, _box, berlin, bern = _aria_city()
    berlin.states.add("SELECTED")
    driver = _driver(monkeypatch, bern)
    driver.click = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert city.text == ""
    assert city.name == "City"
    assert "EXPANDED" in city.states
    assert "SELECTED" not in bern.states
    assert "SELECTED" in berlin.states


def test_aria_combobox_option_click_that_picks_a_different_value_is_unsupported(monkeypatch) -> None:
    """Closing the list onto another city is not a selection of this option."""
    city, box, _berlin, bern = _aria_city()
    driver = _driver(monkeypatch, bern)
    driver.click = _close_on(city, box, bern, text="Berlin")  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert city.text == "Berlin"
    assert "SELECTED" not in bern.states


def test_aria_combobox_option_action_that_closes_the_list_does_not_click_again(monkeypatch) -> None:
    """A non-Chromium list action that commits the combobox is success.

    The row never becomes SELECTED. A second pointer click is not sent.
    """
    city, box, _berlin, bern = _aria_city(toolkit="")
    box.select_fails = True

    def do_action(index, _node=bern):
        _node.action_log.append(_node.actions[index])
        city.text = "Bern"
        city.states.discard("EXPANDED")
        _node.states.add("DEFUNCT")
        _node.parent = None
        return True

    bern.do_action = do_action
    driver = _driver(monkeypatch, bern)
    pointer: list[str] = []
    driver.click = lambda *_args, **_kwargs: pointer.append("click")  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    assert driver.press_element(element) is True
    assert bern.action_log == ["click"]
    assert pointer == []
    assert city.text == "Bern"
    assert "SELECTED" not in bern.states


def test_aria_combobox_option_action_that_changes_nothing_is_unsupported(monkeypatch) -> None:
    """The same action path reports failure when the combobox stays put."""
    city, box, _berlin, bern = _aria_city(toolkit="")
    box.select_fails = True

    def do_action(index, _node=bern):
        _node.action_log.append(_node.actions[index])
        return True

    bern.do_action = do_action
    driver = _driver(monkeypatch, bern)
    driver.click = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    element = _element("e8", "AXRow", "Bern", clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert city.text == ""
    assert "EXPANDED" in city.states
    assert "SELECTED" not in bern.states
    assert bern.action_log == ["click"]


def test_chrome_snapshot_reports_selected_text_pressed_and_empty_number(monkeypatch) -> None:
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    country = _Node(
        "combo box", "Country", text="\ufffc",
        children=[
            _Node("menu item", "Kazakhstan", states={"SELECTED"}),
            _Node("menu item", "Japan"),
            _Node("menu item", "Peru"),
        ],
        y=10,
    )
    fruits = _Node(
        "list box", "Fruits", text="\ufffc\ufffc\ufffc\ufffc",
        children=[
            _Node("list item", "Apple"),
            _Node("list item", "Banana", states={"SELECTED"}),
            _Node("list item", "Cherry"),
            _Node("list item", "Date"),
        ],
        y=40,
    )
    label = _Node("label", "Country", text="Country \ufffc", y=70)
    toggle = _Node(
        "push button", "Italic toggle", text=None,
        states={"PRESSED", "ENABLED", "SENSITIVE", "FOCUSABLE"},
        actions=["click"], y=100,
    )
    off = _Node(
        "push button", "Bold toggle", text=None,
        states={"ENABLED", "SENSITIVE", "FOCUSABLE"},
        attrs={"aria-pressed": "false"}, actions=["click"], y=130,
    )
    seats = _Node("spin button", "Seats", text="3", value=3.0, minimum=0.0, maximum=100.0, y=160)
    empty = _Node("spin button", "Empty", text="", value=0.0, minimum=0.0, maximum=100.0, y=190)
    volume = _Node("slider", "Volume", text=None, value=40.0, minimum=0.0, maximum=100.0, y=220)
    checked = _Node(
        "check box", "Agree", text=None,
        states={"CHECKED", "CHECKABLE", "ENABLED"}, actions=["click"], y=250,
    )
    window = _Node("frame", "Chrome", text=None, children=[
        country, fruits, label, toggle, off, seats, empty, volume, checked,
    ], x=0, y=0, w=400, h=400)
    accessor = ATSPIAccessor()
    assert accessor.read(country).value == "Kazakhstan"
    assert accessor.read(fruits).value == "Banana"
    fruits.children[1].states.add("SELECTED")
    fruits.children[3].states.add("SELECTED")

    def only_the_first(index, _banana=fruits.children[1]):
        return _banana if index == 0 else None

    fruits.get_selected_child = only_the_first
    assert accessor.read(fruits).value == "Banana, Date"
    assert accessor.read(label).value == "Country"
    assert "\ufffc" not in str(accessor.read(label).value)
    assert accessor.read(toggle).checked is True
    assert accessor.read(off).checked is False
    assert accessor.read(seats).value == "3"
    assert accessor.read(empty).value is None
    assert accessor.read(volume).value == 40.0
    assert accessor.read(checked).checked is True
    rendered = []
    for node in window.children:
        raw = accessor.read(node)
        rendered.append(f"{raw.role} {raw.title!r} value={raw.value!r} checked={raw.checked}")
    blob = "\n".join(rendered)
    assert "\ufffc" not in blob
    assert "value=0.0" not in blob and "value='0.0'" not in blob

    link = _Node("link", "quick brown", text="quick brown", y=280)
    lazy = _Node("static", "", text="lazy", y=280)
    fox = _Node(
        "paragraph", "", text="The \ufffc fox jumps over the \ufffc dog.", y=280, w=500,
    )
    fox.links = [link, lazy]
    bravo = _Node("entry", "Bravo", text="", y=310)
    named = _Node("label", "", text="Bravo \ufffc", y=310)
    named.links = [bravo]
    bare = _Node("entry", "Bare para input", text="", y=340)
    bare_para = _Node("paragraph", "", text="\ufffc", y=340)
    bare_para.links = [bare]
    wrapped = _Node("combo box", "Country", text="\ufffc", y=370)
    country_label = _Node("label", "Country", text="Country \ufffc", y=370)
    country_label.links = [wrapped]
    first = _Node("paragraph", "", text="ZZFirst para", y=400)
    second = _Node("paragraph", "", text="Second bold para", y=420)
    editor = _Node("section", "Editor B", text="\ufffc\ufffc", y=400)
    editor.links = [first, second]
    link_only = _Node("link", "a link", text="a link", y=460)
    div = _Node(
        "section", "", text="Div with span text and \ufffc inside.", y=460, w=500,
        attrs={"tag": "div", "id": "d1"},
    )
    div.links = [link_only]
    button_only = _Node(
        "section", "", text="\ufffc", y=490, attrs={"tag": "div"},
        children=[_Node("push button", "Div button", text="Div button")],
    )
    button_only.links = [button_only.children[0]]
    panel = _Node("panel", "", text="\ufffc\ufffc\ufffc", y=520)
    assert accessor.read(fox).value == "The quick brown fox jumps over the lazy dog."
    assert accessor.read(div).value == "Div with span text and a link inside."
    assert accessor.read(button_only).value is None
    assert accessor.read(panel).value is None
    assert accessor.read(named).value == "Bravo"
    assert accessor.read(bare_para).value == "Bare para input"
    assert accessor.read(country_label).value == "Country"
    assert "ZZFirst para" in _atspi._readable_text(editor)
    assert _atspi._typed_visible(
        "Hello world", "Hello world\u00a0 two spaces end\u00a0", "  two spaces end ",
    )
    assert _atspi._typed_visible("", "\u00a0more", " more")


def test_runtime_does_not_type_into_the_focused_field_when_a_combo_cannot_be_set() -> None:
    from a11y_computer_use import server

    calls: list[str] = []
    element = _element("e2", "AXComboBox", "Color", editable=True, clickable=True)
    snap = type("S", (), {"app": "gtk"})()
    runtime = server.Runtime.__new__(server.Runtime)
    runtime._resolve = lambda ref, kind: (snap, element)
    runtime._run_gated = lambda action, app, execute, **_kwargs: execute()
    runtime._recheck_target = lambda app, target: None
    runtime._refuse_disabled = lambda target, verb="": None

    class _Driver:
        def set_value(self, _element, _value):
            return False

        def press_element(self, _element):
            calls.append("press")
            return True

        def type_text(self, text):
            calls.append(text)

    runtime.driver = _Driver()
    with pytest.raises(ComputerUseError) as exc:
        runtime.set_value("e2", "Green")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert calls == []


def test_number_input_with_min_and_no_real_max_is_unbounded_above(monkeypatch) -> None:
    """Chrome reports maximum 0 when the input has min and no max."""
    number = _Node(
        "spin button", "Qty", text="1", value=1.0, minimum=0.0, maximum=0.0,
        attrs={"tag": "input", "text-input-type": "number"},
    )
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)

    def set_current_value(acc, new):
        acc.value_sets.append(float(new))
        acc.value = float(new)
        return True

    monkeypatch.setattr(_Atspi.Value, "set_current_value", staticmethod(set_current_value))
    assert _atspi.set_numeric_value(number, "3") is True
    assert number.value_sets == [3.0]
    with pytest.raises(ValueError, match="below the minimum 0") as low:
        _atspi.set_numeric_value(number, "-1")
    assert "0..0" not in str(low.value)
    with pytest.raises(ValueError, match="minimum is 0") as bad:
        _atspi.set_numeric_value(number, "abc")
    assert "0..0" not in str(bad.value)

    bounded = _Node("spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0)
    with pytest.raises(ValueError, match=r"outside 0\.\.10"):
        _atspi.set_numeric_value(bounded, "11")


def _date_segment(name, valuetext, *, kind="date", value=171994.0):
    editor = _Node(
        "date editor", "Date of birth", text="",
        attrs={"tag": "input", "text-input-type": kind, "id": "dob"},
    )
    editor.toolkit = "Chromium"
    segment = _Node(
        "spin button", name, text="", value=value, minimum=1.0, maximum=275760.0,
        attrs={"tag": "span", "xml-roles": "spinbutton", "valuetext": valuetext, "placeholder": "yyyy"},
    )
    segment.toolkit = "Chromium"
    editor.children.append(segment)
    segment.parent = editor
    return editor, segment


def test_chrome_date_segment_uses_valuetext_not_the_value_float(monkeypatch) -> None:
    """The year segment's Value float is not the read-back.

    171994.0 is the cross-segment number from #146. valuetext "1994" is the
    segment. set_value does not call Value.set_current_value. A segment that
    stays unset reports the displayed text, which is empty, not 0.0.
    """
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_atspi, "_click_center", lambda _acc: None)
    typed: list[str] = []
    segments: list = []

    def grab_focus(acc) -> bool:
        acc.states.add("FOCUSED")
        return True

    def type_string(text):
        typed.append(text)
        for seg in segments:
            if "FOCUSED" in seg.states:
                seg.buf = getattr(seg, "buf", "") + text
                if seg.buf == "03":
                    seg.attrs["valuetext"] = "March"
                elif seg.buf == "17":
                    seg.attrs["valuetext"] = "17"

    monkeypatch.setattr(_atspi, "grab_focus", grab_focus)
    monkeypatch.setattr(_atspi, "_type_string", type_string)

    _editor, year = _date_segment("Year Date of birth", "1994")
    assert _atspi.control_kind(year) is None
    assert ATSPIAccessor().read(year).value == "1994"
    assert year.value == 171994.0
    assert _atspi.set_text(year, "1994") is True
    assert year.value_sets == []
    assert typed == []

    _blank_editor, day = _date_segment("Day Date of birth", "0", value=0.0)
    segments.append(day)
    assert ATSPIAccessor().read(day).value is None
    assert _atspi.set_text(day, "17") is True
    assert "".join(typed) == "17"
    assert ATSPIAccessor().read(day).value == "17"
    assert day.value == 0.0

    _month_editor, month = _date_segment("Month Month", "0", kind="month", value=0.0)
    segments.append(month)
    assert _atspi.set_text(month, "03") is True
    assert month.attrs["valuetext"] == "March"
    assert ATSPIAccessor().read(month).value == "March"

    stuck = _date_segment("Day Date of birth", "0", value=0.0)[1]

    def type_nothing(text):
        typed.append(text)

    monkeypatch.setattr(_atspi, "_type_string", type_nothing)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_text(stuck, "17")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert exc.value.detail["actual"] == ""
    assert exc.value.detail["expected"] == "17"
    assert "0.0" not in exc.value.message
    assert "171994" not in exc.value.message

    gtk = _Node("spin button", "Quantity", text="5", value=5.0, minimum=0.0, maximum=10.0)
    gtk.toolkit = "GTK"
    assert _atspi.control_kind(gtk) == "value"
    assert ATSPIAccessor().read(gtk).value == "5"
    assert ATSPIAccessor().read(gtk).role == "AXTextField"


def test_chrome_aria_spinbutton_reports_role_and_value_not_an_edit_field(monkeypatch) -> None:
    """A custom role=spinbutton is a spinbutton with its value, not a text field.

    valuetext wins over the Value float. Minimum and maximum are part of the
    value when the range is real. An ``<input type=number>`` stays a text
    field whose value is the text, with no range suffix. set_value still
    refuses: the Value interface does not move a widget the page owns.
    """
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    guests = _Node(
        "spin button", "Guests", text="", value=3.0, minimum=0.0, maximum=10.0,
        states=("FOCUSABLE", "ENABLED", "SENSITIVE"),
        attrs={"tag": "div", "xml-roles": "spinbutton", "valuetext": "3 guests"},
    )
    guests.toolkit = "Chromium"
    guests.hold_value = True
    raw = ATSPIAccessor().read(guests)
    assert raw.role == "AXSpinButton"
    assert raw.value == "3 guests (min 0, max 10)"
    assert raw.editable is False
    _clickable, editable, _secure = observe._flags(raw)
    assert editable is False
    line = observe._render_line(Element(
        "e30", raw.role, "Guests", str(raw.value), Bounds(0, 10, 10, 40, 24), "snap",
        editable=editable,
    ), bounds=False)
    assert line == 'e30 spinbutton "Guests" ="3 guests (min 0, max 10)"'
    assert "(edit)" not in line
    assert "AXSpinButton" in observe._INTERACTIVE_ROLES

    plain = _Node(
        "spin button", "Plain", text="", value=6.0, minimum=1.0, maximum=9.0,
        attrs={"tag": "div", "xml-roles": "spinbutton"},
    )
    plain.toolkit = "Chromium"
    assert ATSPIAccessor().read(plain).value == "6 (min 1, max 9)"

    number = _Node(
        "spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0,
        states=("EDITABLE", "FOCUSABLE", "ENABLED", "SENSITIVE"),
        attrs={"tag": "input", "text-input-type": "number", "valuetext": "3"},
    )
    number.toolkit = "Chromium"
    native = ATSPIAccessor().read(number)
    assert native.role == "AXTextField"
    assert native.value == "3"
    assert observe._flags(native)[1] is True

    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_numeric_value(guests, "9")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert guests.value == 3.0


def _native_chrome_select(monkeypatch):
    """Chrome's settled renderer hit is the select, not an option."""
    from a11y_computer_use.drivers import linux, _linux_input
    from contextlib import contextmanager

    box, apple, banana, cherry = _chrome_list()
    box.attrs = {"tag": "select"}
    box.states.update({"VISIBLE", "SHOWING"})
    box.rect = _Rect(10, 10, 120, 72)
    for index, row in enumerate(box.children):
        row.attrs = {"tag": "option"}
        row.toolkit = "Chromium"
        row.states.update({"VISIBLE", "SHOWING"})
        row.rect = _Rect(10, 10 + 24 * index, 120, 24)
    document = _Node("document web", children=[box], states={"VISIBLE", "SHOWING"})
    _Node("frame", children=[document], states={"VISIBLE", "SHOWING"})
    document.get_accessible_at_point = lambda *args: box
    box.get_accessible_at_point = lambda *args: box
    selected = lambda: [row for row in box.children if "SELECTED" in row.states]
    box.get_n_selected_children = lambda: len(selected())
    box.get_selected_child = lambda index: selected()[index]
    box.select_child = lambda index: pytest.fail("unrequested Selection mutation")
    driver = _driver(monkeypatch, cherry)
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(driver, "_raise_handle_window", lambda *args: None)
    monkeypatch.setattr(driver, "_visible_action", lambda *args: None)
    events = []

    @contextmanager
    def held(modifiers):
        events.append(("held", modifiers))
        yield

    monkeypatch.setattr(_linux_input, "held", held)
    element = _element("e987", "AXRow", "Cherry", clickable=True, y=58)
    return driver, element, document, box, apple, banana, cherry, events


@pytest.mark.parametrize("mods,initial,after", [
    ((), {1}, {2}),
    (("ctrl",), {1}, {1, 2}),
    (("ctrl",), {1, 2}, {1}),
    (("control",), {1, 2}, {1}),
    (("shift",), {0}, {0, 1, 2}),
])
def test_native_chrome_select_click_preserves_gesture_and_checks_set(monkeypatch, mods, initial, after):
    from a11y_computer_use.drivers import _linux_input

    driver, element, document, box, apple, banana, cherry, events = _native_chrome_select(monkeypatch)
    for index, row in enumerate(box.children):
        row.states.discard("SELECTED")
        if index in initial:
            row.states.add("SELECTED")

    def click(x, y, **kwargs):
        events.append(("click", x, y, kwargs))
        for index, row in enumerate(box.children):
            row.states.discard("SELECTED")
            if index in after:
                row.states.add("SELECTED")

    monkeypatch.setattr(_linux_input, "click", click)
    if mods:
        driver.click(element, modifiers=mods)
    else:
        assert driver.press_element(element) is True
    assert events == [("held", mods), ("click", 70, 70, {"button": "left", "count": 1})]
    assert {i for i, row in enumerate(box.children) if "SELECTED" in row.states} == after
    assert all(not row.action_log for row in box.children)


@pytest.mark.parametrize("fault", [
    "other_hit", "missing_hit", "aria", "moved", "overlap", "hidden", "outside_list",
    "missing_child", "too_many_children", "nested_options", "unknown_bounds",
])
def test_native_chrome_select_parent_hit_still_refuses_unsafe_points(monkeypatch, fault):
    from a11y_computer_use.drivers import _linux_input

    driver, element, document, box, apple, banana, cherry, events = _native_chrome_select(monkeypatch)
    if fault == "other_hit":
        document.get_accessible_at_point = lambda *args: banana
    elif fault == "missing_hit":
        document.get_accessible_at_point = lambda *args: None
    elif fault == "aria":
        box.attrs = {"tag": "div"}
    elif fault == "moved":
        cherry.rect.y += 100
    elif fault == "overlap":
        banana.rect = cherry.rect
    elif fault == "hidden":
        cherry.states.discard("SHOWING")
    elif fault == "outside_list":
        box.rect.height = 24  # native options can retain off-viewport rectangles
    elif fault == "missing_child":
        box.get_child_at_index = lambda index: None
    elif fault == "too_many_children":
        box.get_child_count = lambda: _atspi._MAX_CHILDREN_FETCH + 1
    elif fault == "nested_options":
        apple.attrs = {"tag": "optgroup"}
    elif fault == "unknown_bounds":
        apple.get_extents = lambda coord: None
    monkeypatch.setattr(_linux_input, "click", lambda *a, **kw: pytest.fail("unsafe input"))
    with pytest.raises(ComputerUseError) as exc:
        if fault == "aria":
            driver.click(element)
        else:
            driver.press_element(element)
    assert exc.value.detail["reason"] in {"hit_test_unverified", "not_showing"}
    assert not events
    assert banana.states >= {"SELECTED"}


@pytest.mark.parametrize("fault", ["unchanged", "wrong_set", "unreadable_before", "unreadable_after", "flicker"])
def test_native_chrome_select_never_confirms_missing_selection_effect(monkeypatch, fault):
    from a11y_computer_use.drivers import _linux_input

    driver, element, document, box, apple, banana, cherry, events = _native_chrome_select(monkeypatch)
    if fault == "unreadable_before":
        box.get_n_selected_children = lambda: None

    def click(*args, **kwargs):
        events.append("click")
        if fault == "wrong_set":
            # Target selection alone must not confirm a plain replacement.
            cherry.states.add("SELECTED")
        elif fault == "unreadable_after":
            box.get_n_selected_children = lambda: None
        elif fault == "flicker":
            reads = iter([frozenset({_atspi._ancestor_key(cherry)}), None] * 4)
            monkeypatch.setattr(_atspi, "chromium_select_selection", lambda parent: next(reads))

    monkeypatch.setattr(_linux_input, "click", click)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.detail["reason"] == (
        "selection_unreadable" if fault == "unreadable_before" else "selection_unchanged"
    )
    assert events == ([] if fault == "unreadable_before" else [("held", ()), "click"])
    assert "SELECTED" in banana.states  # no compensating Selection writes
    assert all(not row.action_log for row in box.children)
