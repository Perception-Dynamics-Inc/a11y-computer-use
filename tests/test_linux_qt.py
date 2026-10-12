"""Qt AT-SPI text length, value display, and combo selection.

Hermetic. The accessibles are fakes: a Qt insert resizes by character count
the way ``QString::resize`` does, and ``GetText`` stops at a NUL while the
character count includes what follows. No display, no bus, no Qt process.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers._atspi import ATSPIAccessor
from a11y_computer_use.schema import ComputerUseError, ErrorCode


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


class _Node:
    def __init__(self, role, name="", text="", states=(), children=(), actions=(), *,
                 value=None, minimum=None, maximum=None, toolkit="",
                 x=10, y=10, w=120, h=24):
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
        self.toolkit = toolkit
        self.rect = _Rect(x, y, w, h)
        self.action_log: list[str] = []
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_toolkit_name(self):
        return self.toolkit

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
        return {}

    def get_action_iface(self):
        return self if self.actions else None

    def get_n_actions(self):
        return len(self.actions)

    def get_action_name(self, index):
        return self.actions[index]

    def do_action(self, index):
        name = self.actions[index]
        self.action_log.append(name)
        if name in {"showmenu", "press", "show", "open"}:
            self.states.add("EXPANDED")
        if name == "collapse":
            self.states.discard("EXPANDED")
        return True

    def get_selection_iface(self):
        if self.toolkit == "Qt":
            return None
        if self.role in {"combo box", "list box"}:
            return self
        return None

    def select_child(self, index):
        for child in self.children:
            child.states.discard("SELECTED")
        self.children[index].states.add("SELECTED")
        return True

    def get_component_iface(self):
        return self

    def get_extents(self, _coord):
        return self.rect

    def clear_cache(self):
        return None

    def get_relation_set(self):
        return list(getattr(self, "relations", ()) or ())


class _Atspi:
    class StateType:
        ENABLED = "ENABLED"
        SENSITIVE = "SENSITIVE"
        FOCUSED = "FOCUSED"
        FOCUSABLE = "FOCUSABLE"
        CHECKED = "CHECKED"
        CHECKABLE = "CHECKABLE"
        SELECTED = "SELECTED"
        EXPANDED = "EXPANDED"
        EXPANDABLE = "EXPANDABLE"
        SHOWING = "SHOWING"
        VISIBLE = "VISIBLE"

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
            # A D-Bus string cannot carry an embedded NUL. The widget can.
            shown = acc.text.split("\x00", 1)[0] if getattr(acc, "dbus_string", False) else acc.text
            if end is not None and int(end) < 0:
                return shown
            return shown[int(start):int(end)]

        @staticmethod
        def get_caret_offset(acc):
            return int(getattr(acc, "caret", len(acc.text)))

        @staticmethod
        def get_n_selections(acc):
            return 0

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


class _Field:
    def __init__(self, text="", caret=None, toolkit=""):
        self.text = text
        self.caret = len(text) if caret is None else caret
        self.toolkit = toolkit
        self.dbus_string = True
        self.lengths: list[int] = []

    def get_toolkit_name(self):
        return self.toolkit

    def get_editable_text_iface(self):
        return self

    def clear_cache(self):
        return None

    def delete_text(self, start, end):
        self.text = self.text[:int(start)] + self.text[int(end):]
        self.caret = int(start)
        return True


class _QtField(_Field):
    """Qt's InsertText: ``QString::resize(length)`` then insert that string.

    A length past the character count extends the string with uninitialized
    characters. The extra for a short overrun is a NUL and then ``a``, which
    is what a byte count does to ``ünï``.
    """

    def __init__(self, text="", caret=None):
        super().__init__(text, caret, toolkit="Qt")

    def insert_text(self, pos, text, length):
        self.lengths.append(int(length))
        chars = list(text)
        count = int(length)
        if count < len(chars):
            chars = chars[:count]
        elif count > len(chars):
            extra = count - len(chars)
            chars = chars + ["\x00"] + ["a"] * (extra - 1)
        chunk = "".join(chars)
        pos = int(pos)
        self.text = self.text[:pos] + chunk + self.text[pos:]
        self.caret = pos + len(chunk)
        return True


class _ByteField(_Field):
    """GTK: the length argument is a UTF-8 byte count."""

    def insert_text(self, pos, text, length):
        self.lengths.append(int(length))
        piece = text.encode("utf-8")[:int(length)]
        try:
            chunk = piece.decode("utf-8")
        except UnicodeDecodeError:
            return True
        pos = int(pos)
        self.text = self.text[:pos] + chunk + self.text[pos:]
        self.caret = pos + len(chunk)
        return True


_ByteField.insert_text._length_unit = "bytes"

_DENORM = 6.9e-310


@pytest.fixture
def atspi(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)


def test_text_value_keeps_line_breaks_for_qt_and_gtk(atspi) -> None:
    """``Text.get_text`` newlines stay in the value. Neither toolkit flattens them.

    The snapshot field used to turn those breaks into spaces later. This
    read is the string ``Element.value`` stores.
    """
    qt = _Node("text", text="AAA\nBBB\nCCC", toolkit="Qt", states=("SHOWING", "EDITABLE"))
    gtk = _Node("text", text="AAA\r\nBBB", toolkit="gtk", states=("SHOWING", "EDITABLE", "MULTI_LINE"))
    assert _atspi._value_text(qt, "AXTextArea", "text") == "AAA\nBBB\nCCC"
    assert _atspi._value_text(gtk, "AXTextArea", "text") == "AAA\r\nBBB"


def test_qt_character_length_inserts_exactly_and_a_byte_length_is_rejected(atspi) -> None:
    field = _QtField("Start", caret=5)
    assert _atspi.insert_text(field, "ünï") == 3
    assert field.text == "Startünï"
    assert field.lengths == [3]
    assert "\x00" not in field.text

    field = _QtField("Start", caret=5)
    assert _atspi.insert_text(field, "日本") == 2
    assert field.text == "Start日本"
    assert field.lengths == [2]

    field = _QtField("ünï", caret=1)
    assert _atspi.insert_text(field, "X") == 1
    assert field.text == "üXnï"
    assert field.lengths == [1]

    field = _QtField("Startünï", caret=len("Startünï"))
    assert _atspi.insert_text(field, "!") == 1
    assert field.text == "Startünï!"

    _QtField.insert_text._length_unit = "bytes"
    try:
        field = _QtField("Start", caret=5)
        with pytest.raises(ComputerUseError) as exc:
            _atspi.insert_text(field, "ünï")
        assert exc.value.code is ErrorCode.UNSUPPORTED
        assert exc.value.detail["reason"] == "text_mismatch"
        assert field.lengths == [len("ünï".encode("utf-8"))]
        assert field.text.split("\x00", 1)[0] == "Startünï"
        assert field.text != "Startünï"
        assert len(field.text) == len("Start") + len("ünï".encode("utf-8"))
    finally:
        del _QtField.insert_text._length_unit


def test_gtk_byte_length_is_unchanged_and_wins_over_a_qt_toolkit(atspi) -> None:
    field = _ByteField(toolkit="gtk")
    assert _atspi.insert_text(field, "ünï") == 3
    assert field.text == "ünï"
    assert field.lengths == [len("ünï".encode("utf-8"))]

    field = _ByteField("Start", caret=5, toolkit="Qt")
    assert _atspi.insert_text(field, "日本") == 2
    assert field.text == "Start日本"
    assert field.lengths == [len("日本".encode("utf-8"))]


def test_insert_length_uses_characters_for_qt_and_bytes_for_gtk(atspi) -> None:
    def gtk_insert(pos, text, length):
        return True

    gtk_insert.__module__ = "gi.repository.Atspi"

    class _App:
        def __init__(self, toolkit):
            self.toolkit = toolkit

        def get_toolkit_name(self):
            return self.toolkit

    assert _atspi._insert_length(gtk_insert, "ünï", _App("gtk")) == 5
    assert _atspi._insert_length(gtk_insert, "ünï", _App("Qt")) == 3
    assert _atspi._insert_length(gtk_insert, "日本", None) == 6
    gtk_insert._length_unit = "bytes"
    assert _atspi._insert_length(gtk_insert, "ünï", _App("Qt")) == 5
    gtk_insert._length_unit = "chars"
    assert _atspi._insert_length(gtk_insert, "ünï", _App("gtk")) == 3


def _read(node):
    return ATSPIAccessor().read(node)


def test_qt_hides_bogus_values_and_keeps_a_real_range(atspi) -> None:
    label = _Node("label", "Name", text=None, value=_DENORM, minimum=_DENORM, maximum=_DENORM, toolkit="Qt")
    empty = _Node("text", "Qt name", text="", value=_DENORM, minimum=_DENORM, maximum=_DENORM, toolkit="Qt")
    check = _Node("check box", "Notify", text=None, value=0.0, minimum=0.0, maximum=0.0, toolkit="Qt")
    radio = _Node("radio button", "Choice", text=None, value=0.0, minimum=0.0, maximum=0.0, toolkit="Qt")
    row = _Node("list item", "Row A", text=None, value=0.0, minimum=0.0, maximum=0.0, toolkit="Qt")
    slider = _Node("slider", "Volume", text=None, value=40.0, minimum=0.0, maximum=100.0, toolkit="Qt")
    progress = _Node("progress bar", "Progress", text=None, value=25.0, minimum=0.0, maximum=100.0, toolkit="Qt")
    zero = _Node("slider", "Mute", text=None, value=0.0, minimum=0.0, maximum=100.0, toolkit="Qt")
    broken = _Node("slider", "Broken", text=None, value=_DENORM, minimum=_DENORM, maximum=_DENORM, toolkit="Qt")
    dirty = _Node("progress bar", "Dirty", text=None, value=_DENORM, minimum=0.0, maximum=100.0, toolkit="Qt")
    spin = _Node("spin button", "Quantity", text="3", value=3.0, minimum=0.0, maximum=10.0, toolkit="Qt")

    assert _read(label).value is None
    assert _read(empty).value is None
    assert _read(check).value is None
    assert _read(radio).value is None
    assert _read(row).value is None
    assert _read(slider).value == 40.0
    assert _read(slider).role == "AXSlider"
    assert _read(progress).value == 25.0
    assert _read(progress).role == "AXProgressIndicator"
    assert _read(zero).value == 0.0
    assert _read(broken).value is None
    assert _read(dirty).value is None
    assert _read(spin).value == "3"

    assert _atspi.control_kind(label) is None
    assert _atspi.control_kind(empty) is None
    assert _atspi.control_kind(check) is None
    assert _atspi.control_kind(slider) == "value"
    assert _atspi.control_kind(progress) == "value"
    assert _atspi.control_kind(broken) is None
    assert _atspi.control_kind(spin) == "value"


def test_gtk_value_fallback_is_unchanged(atspi) -> None:
    label = _Node("label", "Caption", text=None, value=0.0, minimum=0.0, maximum=0.0, toolkit="gtk")
    slider = _Node("slider", "Volume", text=None, value=40.0, minimum=0.0, maximum=100.0, toolkit="gtk")
    panel = _Node("panel", "Meter", text=None, value=2.0, minimum=0.0, maximum=10.0, toolkit="gtk")
    qt_panel = _Node("panel", "Meter", text=None, value=2.0, minimum=0.0, maximum=10.0, toolkit="Qt")
    assert _read(label).value == 0.0
    assert _read(slider).value == 40.0
    assert _atspi.control_kind(slider) == "value"
    assert _atspi.control_kind(panel) == "value"
    assert _atspi.control_kind(qt_panel) is None


def _qt_combo():
    label = _Node("label", "Qt color", toolkit="Qt")
    red = _Node("list item", "Red", states={"SHOWING", "VISIBLE"}, actions=["toggle"], toolkit="Qt")
    green = _Node("list item", "Green", states={"SHOWING", "VISIBLE"}, actions=["toggle"], toolkit="Qt")
    blue = _Node(
        "list item", "Blue", states={"SHOWING", "VISIBLE"}, actions=["toggle"],
        toolkit="Qt", x=40, y=80, w=100, h=24,
    )
    listed = _Node(
        "list", "", children=[red, green, blue],
        states={"SHOWING", "VISIBLE"}, toolkit="Qt",
    )
    combo = _Node(
        "combo box", "Red", text="", toolkit="Qt",
        states={"ENABLED", "SENSITIVE"},
        children=[listed],
        actions=["showmenu"],
    )
    combo.relations = [_Relation("LABELLED_BY", [label])]
    return combo, blue


def test_qt_combo_is_named_from_its_label_and_set_by_a_popup_click(atspi, monkeypatch) -> None:
    combo, blue = _qt_combo()
    clicks: list[tuple[int, int]] = []
    chords: list[str] = []

    def click(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))
        combo.name = blue.name
        combo.states.discard("EXPANDED")

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_input.press_chord",
        lambda chord: chords.append(chord),
    )

    raw = _read(combo)
    assert raw.title == "Qt color"
    assert raw.value == "Red"
    assert raw.role == "AXComboBox"

    _atspi.set_combo_value(combo, "Red")
    assert clicks == [] and combo.action_log == []

    _atspi.set_combo_value(combo, "Blue")
    assert clicks == [(90, 92)]
    assert combo.action_log == ["showmenu"]
    assert blue.action_log == []
    assert combo.name == "Blue"
    assert "EXPANDED" not in combo.states
    assert chords == []
    assert _read(combo).value == "Blue"
    assert _read(combo).title == "Qt color"


def test_qt_combo_unknown_option_does_nothing_and_a_miss_is_not_success(atspi, monkeypatch) -> None:
    combo, _blue = _qt_combo()
    clicks: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_input.click",
        lambda x, y, *_args, **_kwargs: clicks.append((int(x), int(y))),
    )
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.press_chord", lambda _chord: None)

    with pytest.raises(ValueError) as exc:
        _atspi.set_combo_value(combo, "Mars")
    assert "Red" in str(exc.value) and "Blue" in str(exc.value)
    assert clicks == [] and combo.action_log == []
    assert combo.name == "Red"

    def miss(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))
        combo.states.discard("EXPANDED")

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", miss)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_combo_value(combo, "Blue")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert combo.name == "Red"
    assert clicks


def _qt_grid_cells():
    other = _Node(
        "table cell", "R3C2",
        states={"SELECTABLE", "FOCUSABLE", "SELECTED"},
        actions=["toggle"], toolkit="Qt", x=10, y=10, w=40, h=20,
    )
    cell = _Node(
        "table cell", "R1C0",
        states={"SELECTABLE", "FOCUSABLE"},
        actions=["toggle"], toolkit="Qt", x=10, y=40, w=40, h=20,
    )
    header = _Node("table column header", "1", toolkit="Qt")
    _Node("table", "Grid", children=[header, other, cell], toolkit="Qt")
    return other, cell


def test_qt_table_cell_pointer_selects_only_that_cell_and_focuses_it(atspi, monkeypatch) -> None:
    """Toggle adds a Qt cell and leaves the current cell. A center click does not.

    Success is the cell that was clicked: it is selected and focused, and
    the cell that was already selected is not. A second call on that cell
    does not click again. A tree item is not a grid cell.
    """
    other, cell = _qt_grid_cells()
    clicks: list[tuple[int, int]] = []

    def click(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))
        other.states.discard("SELECTED")
        other.states.discard("FOCUSED")
        cell.states.add("SELECTED")
        cell.states.add("FOCUSED")

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    assert _atspi.qt_table_cell(cell) is True
    assert _atspi.activate_qt_table_cell(cell) is True
    assert clicks == [(30, 50)]
    assert cell.action_log == []
    assert other.action_log == []
    assert "SELECTED" in cell.states and "FOCUSED" in cell.states
    assert "SELECTED" not in other.states

    assert _atspi.activate_qt_table_cell(cell) is True
    assert clicks == [(30, 50)]

    fruits = _Node("table cell", "Fruits", actions=["toggle"], toolkit="Qt")
    _Node("tree", "Tree", children=[fruits], toolkit="Qt")
    gtk = _Node("table cell", "A1", toolkit="gtk")
    _Node("table", "Sheet", children=[gtk], toolkit="gtk")
    assert _atspi.qt_table_cell(fruits) is False
    assert _atspi.activate_qt_table_cell(fruits) is False
    assert _atspi.qt_table_cell(gtk) is False
    assert fruits.action_log == []
    assert clicks == [(30, 50)]


def test_qt_table_cell_that_only_gets_added_is_not_success(atspi, monkeypatch) -> None:
    """A click that leaves two cells selected, or skips focus, is not success."""
    other, cell = _qt_grid_cells()
    clicks: list[tuple[int, int]] = []

    def click(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))
        cell.states.add("SELECTED")

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    assert _atspi.activate_qt_table_cell(cell) is False
    assert clicks == [(30, 50)]
    assert cell.action_log == []
    assert "FOCUSED" not in cell.states
    assert "SELECTED" in other.states


def test_press_element_uses_the_pointer_for_a_qt_grid_cell(atspi, monkeypatch) -> None:
    """The driver does not Toggle a Qt grid cell, and it does not confirm a miss."""
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Bounds, Element

    other, cell = _qt_grid_cells()
    clicks: list[tuple[int, int]] = []
    pressed: list[object] = []

    def click(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))
        other.states.discard("SELECTED")
        cell.states.update({"SELECTED", "FOCUSED"})

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    monkeypatch.setattr(_atspi, "do_press", lambda acc: pressed.append(acc) or True)
    element = Element(
        "e1", "AXCell", "R1C0", None, Bounds(0, 10, 40, 40, 20), "snap", clickable=True,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: cell,
    )
    assert driver.press_element(element) is True
    assert clicks == [(30, 50)]
    assert pressed == []
    assert cell.action_log == []

    other.states.add("SELECTED")
    cell.states.discard("FOCUSED")

    def add_only(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))
        cell.states.add("SELECTED")

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", add_only)
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "selection_unchanged"
    assert "only that cell" in exc.value.message
    assert pressed == []


def test_press_element_still_toggles_a_qt_tree_item(atspi, monkeypatch) -> None:
    """A tree item is not a grid cell. Toggle selects it and does not expand it."""
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Bounds, Element

    fruits = _Node(
        "table cell", "Fruits", actions=["toggle"], toolkit="Qt",
        states={"SELECTABLE", "FOCUSABLE", "EXPANDABLE"},
    )
    _Node("tree", "Tree", children=[fruits], toolkit="Qt")
    clicks: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_input.click",
        lambda x, y, *_args, **_kwargs: clicks.append((int(x), int(y))),
    )
    element = Element(
        "e2", "AXCell", "Fruits", None, Bounds(0, 10, 40, 80, 20), "snap", clickable=True,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(
        "a11y_computer_use.observe.ax_handle_for", lambda *_args, **_kwargs: fruits,
    )
    assert driver.press_element(element) is True
    assert fruits.action_log == ["toggle"]
    assert clicks == []


def test_gtk_combo_still_uses_its_own_selection(atspi, monkeypatch) -> None:
    red = _Node("menu item", "Red", states={"SELECTED"}, toolkit="gtk")
    green = _Node("menu item", "Green", toolkit="gtk")
    combo = _Node(
        "combo box", "Color", text="", toolkit="gtk",
        states={"EXPANDABLE", "ENABLED"},
        children=[red, green],
        actions=["press", "collapse"],
    )
    clicks: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "a11y_computer_use.drivers._linux_input.click",
        lambda x, y, *_args, **_kwargs: clicks.append((int(x), int(y))),
    )
    _atspi.set_combo_value(combo, "Green")
    assert "SELECTED" in green.states
    assert "SELECTED" not in red.states
    assert clicks == []
    assert _read(combo).title == "Color"
    assert _read(combo).value == "Green"


def test_unnamed_gtk_combo_takes_its_name_from_labelled_by_and_its_value_from_the_selection(atspi) -> None:
    """An empty GTK combo name is the LABELLED_BY label, without the colon.

    The value is the selected item. A selected item whose name is empty
    contributes its text. A combo that already has a name keeps that name.
    Qt still uses the label as the title and the accessible name as the value.
    """
    label = _Node("label", "Orientation:", toolkit="gtk")
    landscape = _Node("menu item", "Landscape", toolkit="gtk")
    portrait = _Node("menu item", "Portrait", states={"SELECTED"}, toolkit="gtk")
    menu = _Node("menu", "", children=[landscape, portrait], toolkit="gtk")
    combo = _Node(
        "combo box", "", text="", toolkit="gtk",
        states={"ENABLED", "SENSITIVE", "SHOWING", "VISIBLE"},
        children=[menu],
    )
    combo.relations = [_Relation("LABELLED_BY", [label])]
    raw = _read(combo)
    assert raw.title == "Orientation"
    assert raw.value == "Portrait"

    blank = _Node("menu item", "", text="Landscape", states={"SELECTED"}, toolkit="gtk")
    text_menu = _Node("menu", "", children=[blank], toolkit="gtk")
    text_combo = _Node("combo box", "", text="", toolkit="gtk", children=[text_menu])
    text_combo.relations = [_Relation("LABELLED_BY", [label])]
    text_raw = _read(text_combo)
    assert text_raw.title == "Orientation"
    assert text_raw.value == "Landscape"

    named_label = _Node("label", "Color:", toolkit="gtk")
    red = _Node("menu item", "Red", states={"SELECTED"}, toolkit="gtk")
    named = _Node("combo box", "Color", text="", toolkit="gtk", children=[red])
    named.relations = [_Relation("LABELLED_BY", [named_label])]
    named_raw = _read(named)
    assert named_raw.title == "Color"
    assert named_raw.value == "Red"


def test_unnamed_gtk_combo_value_is_displayed_text_or_a_selected_cell(atspi) -> None:
    """No selected menu item: the value is the text the combo shows.

    A label under the combo is that text. A selected table cell is that
    text when the popup is a tree. The caption is not the value.
    """
    caption = _Node("label", "Margin:", toolkit="gtk")
    shown = _Node("label", "1.25 cm", toolkit="gtk")
    combo = _Node(
        "combo box", "", text="", toolkit="gtk",
        children=[shown],
    )
    combo.relations = [_Relation("LABELLED_BY", [caption])]
    raw = _read(combo)
    assert raw.title == "Margin"
    assert raw.value == "1.25 cm"

    entry = _Node("text", "", text="Narrow", toolkit="gtk")
    filler = _Node("filler", "", toolkit="gtk")
    sibling = _Node("combo box", "", text="", toolkit="gtk")
    filler.children = [entry, sibling]
    entry.parent = filler
    sibling.parent = filler
    sibling.relations = [_Relation("LABELLED_BY", [_Node("label", "Format:", toolkit="gtk")])]
    sibling_raw = _read(sibling)
    assert sibling_raw.title == "Format"
    assert sibling_raw.value == "Narrow"

    cell = _Node("table cell", "Portrait", states={"SELECTED"}, toolkit="gtk")
    other = _Node("table cell", "Landscape", toolkit="gtk")
    table = _Node("table", "", children=[other, cell], toolkit="gtk")
    tree_combo = _Node("combo box", "", text="", toolkit="gtk", children=[table])
    tree_combo.relations = [_Relation("LABELLED_BY", [_Node("label", "Orientation:", toolkit="gtk")])]
    tree_raw = _read(tree_combo)
    assert tree_raw.title == "Orientation"
    assert tree_raw.value == "Portrait"


def test_hidden_window_is_omitted_and_a_showing_window_stays(atspi) -> None:
    """A nested frame that is not showing and not visible leaves the snapshot.

    FeatherPad keeps a Replace dialog in the tree as a frame named
    Replacement. Its state set is only ENABLED, and its box sits on the
    parent window, so the pruner would list it. SHOWING or VISIBLE keeps
    the window. An empty state set is not hidden. A button that is not
    showing is not a window and is still returned. The hidden frame's
    children are not walked.
    """
    secret = _Node(
        "push button", "Secret",
        states={"SHOWING", "VISIBLE", "ENABLED"}, actions=["press"],
        toolkit="Qt", x=4, y=4, w=40, h=16,
    )
    label = _Node(
        "label", "Replace with:", states={"ENABLED"}, toolkit="Qt",
        x=0, y=0, w=80, h=16,
    )
    hidden = _Node(
        "frame", "Replacement", states={"ENABLED"}, children=[label, secret],
        toolkit="Qt", x=0, y=0, w=100, h=30,
    )
    tool_button = _Node(
        "push button", "Inside tools",
        states={"SHOWING", "VISIBLE", "ENABLED"}, actions=["press"],
        toolkit="Qt", x=410, y=50, w=80, h=24,
    )
    tools = _Node(
        "window", "Tools",
        states={"SHOWING", "VISIBLE", "ENABLED"}, children=[tool_button],
        toolkit="Qt", x=400, y=40, w=180, h=80,
    )
    parked = _Node(
        "frame", "Parked", states={"VISIBLE", "ENABLED"},
        toolkit="Qt", x=20, y=400, w=80, h=40,
    )
    find = _Node(
        "dialog", "Find", states={"SHOWING", "ENABLED"},
        toolkit="Qt", x=120, y=400, w=80, h=40,
    )
    about = _Node(
        "alert", "About", states={"ENABLED"},
        toolkit="Qt", x=220, y=400, w=80, h=40,
    )
    unset = _Node(
        "frame", "Unset", states=(),
        toolkit="Qt", x=320, y=400, w=80, h=40,
    )
    buried = _Node(
        "push button", "Buried", states={"ENABLED"}, actions=["press"],
        toolkit="Qt", x=10, y=300, w=60, h=24,
    )
    document = _Node(
        "text", "Document", text="hello",
        states={"SHOWING", "VISIBLE", "EDITABLE"},
        toolkit="Qt", x=10, y=40, w=360, h=200,
    )
    frame = _Node(
        "frame", "FeatherPad",
        states={"SHOWING", "VISIBLE", "ENABLED", "ACTIVE"},
        children=[document, buried, hidden, tools, parked, find, about, unset],
        toolkit="Qt", x=0, y=0, w=800, h=600,
    )
    sibling = _Node(
        "frame", "Replacement", states={"ENABLED"},
        toolkit="Qt", x=0, y=0, w=100, h=30,
    )
    app = _Node(
        "application", "featherpad", children=[frame, sibling], toolkit="Qt",
    )

    kids = list(ATSPIAccessor().children(frame))
    assert hidden not in kids
    assert about not in kids
    assert tools in kids and parked in kids and find in kids and unset in kids
    assert document in kids and buried in kids
    app_kids = list(ATSPIAccessor().children(app))
    assert frame in app_kids
    assert sibling not in app_kids

    from a11y_computer_use import observe
    from a11y_computer_use.schema import Display, Scope

    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    geometry = (observe.DisplayGeometry(display=display, origin=(0.0, 0.0)),)
    snap = observe.build_snapshot(
        frame, ATSPIAccessor(), scope=Scope.WINDOW, app="featherpad", pid=None,
        geometry=geometry,
    )
    titles = [el.title for el in snap.elements]
    assert "Replacement" not in titles
    assert "Replace with:" not in titles
    assert "Secret" not in titles
    assert "About" not in titles
    assert "Tools" in titles
    assert "Parked" in titles
    assert "Find" in titles
    assert "Unset" in titles
    assert "Buried" in titles
    rendered = observe.render_text(snap)
    assert 'window "Replacement"' not in rendered
    assert 'window "Tools"' in rendered
    whole = observe.build_snapshot(
        app, ATSPIAccessor(), scope=Scope.APP, app="featherpad", pid=None,
        geometry=geometry,
    )
    assert not any(el.title == "Replacement" for el in whole.elements)
    assert any(el.title == "Tools" for el in whole.elements)


def test_hidden_frame_that_wraps_a_showing_document_stays(atspi) -> None:
    """A Firefox content frame with no SHOWING state still holds the page.

    The outer window is SHOWING. The frame under it reports only ENABLED,
    and the showing document web is inside that frame. Dropping the frame
    would omit the page. A sibling dialog that is not showing, and that
    does not wrap a document, is still omitted.
    """
    button = _Node(
        "push button", "Div button",
        states={"SHOWING", "VISIBLE", "ENABLED"}, actions=["press"],
        toolkit="Gecko", x=20, y=80, w=80, h=24,
    )
    document = _Node(
        "document web", "cuapara",
        states={"SHOWING", "VISIBLE", "ENABLED"}, children=[button],
        toolkit="Gecko", x=0, y=60, w=800, h=500,
    )
    content = _Node(
        "frame", "content", states={"ENABLED"}, children=[document],
        toolkit="Gecko", x=0, y=40, w=800, h=540,
    )
    notice = _Node(
        "label", "Update", states={"ENABLED"}, toolkit="Gecko",
        x=8, y=8, w=80, h=16,
    )
    hidden_dialog = _Node(
        "dialog", "Hidden notice", states={"ENABLED"}, children=[notice],
        toolkit="Gecko", x=200, y=200, w=120, h=40,
    )
    chrome = _Node(
        "frame", "cuapara — Mozilla Firefox",
        states={"SHOWING", "VISIBLE", "ENABLED", "ACTIVE"},
        children=[content, hidden_dialog],
        toolkit="Gecko", x=0, y=0, w=900, h=700,
    )
    app = _Node("application", "firefox", children=[chrome], toolkit="Gecko")

    kids = list(ATSPIAccessor().children(chrome))
    assert content in kids
    assert hidden_dialog not in kids
    assert document in list(ATSPIAccessor().children(content))

    from a11y_computer_use import observe
    from a11y_computer_use.schema import Display, Scope

    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    geometry = (observe.DisplayGeometry(display=display, origin=(0.0, 0.0)),)
    snap = observe.build_snapshot(
        app, ATSPIAccessor(), scope=Scope.APP, app="firefox", pid=None,
        geometry=geometry,
    )
    titles = [el.title for el in snap.elements]
    assert "Div button" in titles
    assert "cuapara" in titles
    assert "Hidden notice" not in titles
    assert "Update" not in titles
