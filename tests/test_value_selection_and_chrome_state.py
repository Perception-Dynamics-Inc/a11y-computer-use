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
        if self.role in {"combo box", "list box", "table", "tree table"}:
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
        _node.text = text

    monkeypatch.setattr(_atspi, "_type_string", type_text)
    monkeypatch.setattr(_atspi, "_click_center", lambda _node: None)
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
    monkeypatch.setattr(_atspi, "grab_focus", lambda _acc: True)
    monkeypatch.setattr(_atspi, "_click_center", lambda _acc: None)
    typed: list[str] = []

    def type_string(text):
        typed.append(text)
        if text == "03":
            month.attrs["valuetext"] = "March"
        elif text == "17":
            day.attrs["valuetext"] = text

    monkeypatch.setattr(_atspi, "_type_string", type_string)

    _editor, year = _date_segment("Year Date of birth", "1994")
    assert _atspi.control_kind(year) is None
    assert ATSPIAccessor().read(year).value == "1994"
    assert year.value == 171994.0
    assert _atspi.set_text(year, "1994") is True
    assert year.value_sets == []
    assert typed == []

    _blank_editor, day = _date_segment("Day Date of birth", "0", value=0.0)
    assert ATSPIAccessor().read(day).value is None
    assert _atspi.set_text(day, "17") is True
    assert typed == ["17"]
    assert ATSPIAccessor().read(day).value == "17"
    assert day.value == 0.0

    _month_editor, month = _date_segment("Month Month", "0", kind="month", value=0.0)
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
