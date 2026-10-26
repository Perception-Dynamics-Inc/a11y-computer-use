"""Qt AT-SPI text length, value display, and combo selection.

Hermetic. The accessibles are fakes: a Qt insert resizes by character count
the way ``QString::resize`` does, and ``GetText`` stops at a NUL while the
character count includes what follows. No display, no bus, no Qt process.
"""

from __future__ import annotations

import time as _real_time

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers._atspi import ATSPIAccessor
from a11y_computer_use.schema import ComputerUseError, ErrorCode


class _WaitClock:
    """Deterministic stand-in for a driver module's ``time``.

    ``sleep`` records the pause and advances the clock without waiting, so a
    timing bound measures the waits the code asks for rather than how late
    the host wakes it. Each clock read adds a microsecond, so a deadline loop
    that never sleeps still ends. Everything else is the real module.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(float(seconds), 0.0)

    def monotonic(self) -> float:
        self.now += 1e-6
        return self.now

    perf_counter = monotonic

    def monotonic_ns(self) -> int:
        return int(self.monotonic() * 1e9)

    def __getattr__(self, name):
        return getattr(_real_time, name)


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


@pytest.mark.parametrize("states", [
    {"SELECTABLE", "FOCUSABLE"},
    {"SELECTABLE", "FOCUSABLE", "SELECTED"},
    {"SELECTABLE", "FOCUSABLE", "EXPANDABLE"},
])
@pytest.mark.parametrize("bounds_available", [False, True])
@pytest.mark.parametrize("actions", [["Toggle"], ["Toggle", "SetFocus"]])
def test_okular_toc_toggle_refuses_without_selecting_or_clicking(
    atspi, monkeypatch, states, bounds_available, actions,
) -> None:
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Bounds, Element

    cell = _Node("table cell", "Chapter 5 Heading", actions=actions,
                 toolkit="Qt", states=states)
    tree = _Node("tree", "Contents", children=[cell], toolkit="Qt")
    app = _Node("application", "okular", children=[tree], toolkit="Qt")
    cell.get_application = lambda: app
    element = Element(
        "e24", "AXCell", cell.name, None,
        Bounds(0, 10, 40, 80, 20) if bounds_available else None,
        "snap", clickable=True,
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_raise_handle_window", lambda *_args: None)
    monkeypatch.setattr("a11y_computer_use.observe.ax_handle_for", lambda *_args: cell)
    monkeypatch.setattr(driver, "click", lambda *_args: pytest.fail("unexpected pointer click"))
    monkeypatch.setattr(tree, "select_child", lambda *_args: pytest.fail("unexpected selection"))

    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "toc_activation_unverified"
    assert "coordinate click" in exc.value.detail["hint"]
    assert "page number" in exc.value.detail["hint"]
    assert cell.action_log == []
    assert cell.states == states


@pytest.mark.parametrize("app_name,toolkit,parent_role,actions", [
    ("other", "Qt", "tree", ["Toggle"]),
    ("okular", "gtk", "tree", ["Toggle"]),
    ("okular", "Qt", "table", ["Toggle"]),
    ("okular", "Qt", "tree", ["Press", "Toggle"]),
])
def test_okular_toc_refusal_is_scoped(atspi, app_name, toolkit, parent_role, actions):
    cell = _Node("table cell", "Chapter", toolkit=toolkit, actions=actions)
    parent = _Node(parent_role, children=[cell], toolkit=toolkit)
    app = _Node("application", app_name, children=[parent], toolkit=toolkit)
    cell.get_application = lambda: app
    assert _atspi.okular_toc_toggle_cell(cell) is False


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


@pytest.mark.parametrize("role", ["menu item", "check menu item", "radio menu item"])
def test_gtk4_popover_item_uses_relation_label_without_reading_value(atspi, monkeypatch, role) -> None:
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Display, Scope

    label = _Node("label", text="Terminate…", toolkit="gtk")
    item = _Node(
        role, text=None, actions=["click"], toolkit="gtk",
        states={"ENABLED", "SENSITIVE", "SHOWING", "VISIBLE"},
    )
    item.relations = [_Relation("LABELLED_BY", [label])]
    item.get_value_iface = lambda: None

    def forbidden_value(_acc):
        pytest.fail("CurrentValue must not be read without a Value interface")

    monkeypatch.setattr(_Atspi.Value, "get_current_value", forbidden_value)
    raw = _read(item)
    assert raw.title == "Terminate…"
    assert raw.value is None
    assert raw.actions  # The original click capability remains available.
    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    snap = observe.build_snapshot(
        item, ATSPIAccessor(), scope=Scope.WINDOW, app="GTK4 Menu", pid=None,
        geometry=(observe.DisplayGeometry(display=display, origin=(0.0, 0.0)),),
    )
    assert any(el.title == "Terminate…" and el.value is None for el in snap.elements)
    assert "Terminate…" in observe.render_text(snap)
    matches = observe.find_elements(snap, text="Terminate", role="menuitem", clickable=True)
    assert len(matches) == 1
    assert matches[0].title == "Terminate…"

    item.name = "Existing name"
    assert _read(item).title == "Existing name"
    item.name = ""
    label.name = "Properties"
    assert _read(item).title == "Properties"
    label.name = '<b title="a > b">Properties</b>'
    assert _read(item).title == "Properties"
    item.relations = []
    assert _read(item).title == ""


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


def test_accessible_name_strips_rich_text_and_keeps_angle_brackets(atspi) -> None:
    """Qt rich-text names lose their tags. Text that is not markup stays.

    FeatherPad's word-count button exposes
    ``<p style='white-space:pre'>Calculate number of words</p>``. The
    snapshot title is that sentence. ``<not-a-tag>``, ``a < b``, ``<3 cats>``,
    and a bare ``<a>`` are not markup. Entities decode, including a name
    whose tags were stored as ``&lt;p&gt;``. A name with no ``<`` and no
    ``&`` is unchanged, backslash included.
    """
    cases = {
        "<p style='white-space:pre'>Calculate number of words</p>": "Calculate number of words",
        "<P STYLE='white-space:pre'>Calculate number of words</P>": "Calculate number of words",
        "<p>Calculate<br/>number of words</p>": "Calculate number of words",
        "<p>Hello</p><p>World</p>": "Hello World",
        "<p>Tom &amp; <b>Jerry</b></p>": "Tom & Jerry",
        "<span style=\"color:red\">Calculate number of words</span>": "Calculate number of words",
        "&lt;p&gt;Hi&lt;/p&gt;": "Hi",
        "&lt;not-a-tag&gt;": "<not-a-tag>",
        "&#60;not-a-tag&#62;": "<not-a-tag>",
        "<not-a-tag>": "<not-a-tag>",
        "a < b and c > d": "a < b and c > d",
        "press <a>": "press <a>",
        "Tom &amp; Jerry": "Tom & Jerry",
        "already plain": "already plain",
        "C:\\notes": "C:\\notes",
        "<3 cats>": "<3 cats>",
        "if (x < y)": "if (x < y)",
    }
    for raw, want in cases.items():
        assert _atspi._plain_accessible_name(raw) == want, raw

    button = _Node(
        "push button",
        "<p style='white-space:pre'>Calculate number of words</p>",
        states={"SHOWING", "VISIBLE", "ENABLED"},
        actions=["press"],
        toolkit="Qt",
    )
    literal = _Node(
        "push button", "<not-a-tag>",
        states={"SHOWING", "VISIBLE", "ENABLED"},
        actions=["press"], toolkit="Qt", x=10, y=40,
    )
    compared = _Node(
        "push button", "a < b and c > d",
        states={"SHOWING", "VISIBLE", "ENABLED"},
        actions=["press"], toolkit="Qt", x=10, y=70,
    )
    bare = _Node(
        "push button", "press <a>",
        states={"SHOWING", "VISIBLE", "ENABLED"},
        actions=["press"], toolkit="Qt", x=10, y=100,
    )
    frame = _Node(
        "frame", "FeatherPad",
        states={"SHOWING", "VISIBLE", "ENABLED"},
        children=[button, literal, compared, bare],
        toolkit="Qt", x=0, y=0, w=400, h=300,
    )
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Display, Scope

    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    snap = observe.build_snapshot(
        frame, ATSPIAccessor(), scope=Scope.WINDOW, app="featherpad", pid=None,
        geometry=(observe.DisplayGeometry(display=display, origin=(0.0, 0.0)),),
    )
    titles = {el.title for el in snap.elements}
    assert "Calculate number of words" in titles
    assert "<not-a-tag>" in titles
    assert "a < b and c > d" in titles
    assert "press <a>" in titles
    assert not any("<p" in el.title or "</" in el.title for el in snap.elements)
    rendered = observe.render_text(snap)
    assert 'button "Calculate number of words"' in rendered
    assert "<p style" not in rendered
    assert 'button "<not-a-tag>"' in rendered
    assert 'button "a < b and c > d"' in rendered
    assert 'button "press <a>"' in rendered


@pytest.mark.parametrize("use_description", [False, True])
@pytest.mark.parametrize("markup, expected", [
    ("<p style='white-space:pre'>Calculate number of words</p>", "Calculate number of words"),
    ('<p title="Words > 0">Calculate <b>number</b> of words</p>', "Calculate number of words"),
    ("<p title='Words > 0'>Calculate number of words</p>", "Calculate number of words"),
    ("<p-custom>Literal label</p-custom>", "<p-custom>Literal label</p-custom>"),
])
def test_rich_text_snapshot_names_and_description_fallback(atspi, use_description, markup, expected):
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Display, Scope

    button = _Node(
        "push button", "" if use_description else markup,
        states={"SHOWING", "VISIBLE", "ENABLED"}, actions=["press"], toolkit="Qt",
    )
    if use_description:
        button.get_description = lambda: markup
    frame = _Node(
        "frame", "FeatherPad", children=[button], toolkit="Qt",
        states={"SHOWING", "VISIBLE", "ENABLED"}, x=0, y=0, w=400, h=300,
    )
    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    snap = observe.build_snapshot(
        frame, ATSPIAccessor(), scope=Scope.WINDOW, app="featherpad", pid=None,
        geometry=(observe.DisplayGeometry(display=display, origin=(0.0, 0.0)),),
    )
    control = next(el for el in snap.elements if el.role == "AXButton")
    assert control.title == expected
    assert f'button "{expected}"' in observe.render_text(snap)
    assert control in observe.find_elements(snap, text=expected)


@pytest.mark.parametrize("states", [("SHOWING", "EDITABLE"), ("SHOWING", "EDITABLE", "SINGLE_LINE")])
def test_qt_line_edit_does_not_request_document_viewport(atspi, monkeypatch, states):
    """Qt line edits must retain their value without a bounded-range bus call."""
    edit = _Node("text", name="Qt name", text="Ada", toolkit="Qt", states=states)

    def unexpected_viewport(node):
        pytest.fail("single-line Qt control requested document viewport text")

    monkeypatch.setattr(_atspi, "_qt_viewport_text", unexpected_viewport)
    assert ATSPIAccessor().read(edit).value == "Ada"


@pytest.mark.parametrize("owner_name,expected", [("Kate", None), ("FeatherPad", "VISIBLE_TOKEN")])
def test_qt_viewport_read_preserves_kate_visibility_refusal(atspi, monkeypatch, owner_name, expected):
    """Generic Qt viewport reads must retain Kate's app-specific refusal."""
    owner = _Node("application", name=owner_name, toolkit="Qt")
    edit = _Node(
        "text", text="VISIBLE_TOKEN\nOFFSCREEN_TOKEN", toolkit="Qt",
        states={"SHOWING", "VISIBLE", "EDITABLE", "MULTI_LINE"},
    )
    edit.get_application = lambda: owner
    monkeypatch.setattr(_atspi, "_qt_viewport_text", lambda node: "VISIBLE_TOKEN")
    raw = ATSPIAccessor().read(edit)
    assert raw.value == expected
    assert raw.editable
    if owner_name == "Kate":
        assert "Document text visibility unavailable" in raw.description


@pytest.mark.parametrize("case", ["success", "miss", "missing_option", "no_popup", "no_geometry", "ambiguous", "popup_stays"])
def test_qt_quick_combo_uses_new_frame_menu_and_verifies_commit(atspi, monkeypatch, case):
    display = _Node("text", text="Apple", states={"EDITABLE", "SHOWING"}, toolkit="Qt")
    combo = _Node("combo box", "Fruit", children=[display], actions=["SetFocus"], toolkit="Qt")
    cherry = _Node("menu item", "Cherry", x=40, y=80, w=100, h=24, toolkit="Qt")
    popup = _Node("menu", children=[_Node("menu item", "Apple"), cherry], toolkit="Qt")
    unrelated = _Node("menu", states={"SHOWING"}, children=[_Node("menu item", "Cherry")])
    frame = _Node("frame", children=[combo, unrelated], toolkit="Qt")
    other_popup = _Node("menu", states={"SHOWING"}, children=[_Node("menu item", "Cherry")])
    clicks = []
    chords = []
    monkeypatch.setattr(_atspi.time, "sleep", lambda _: None)
    if case == "no_geometry":
        combo.rect.width = 0

    def click(x, y, *_args, **_kwargs):
        clicks.append((int(x), int(y)))
        if len(clicks) == 1:
            if case != "no_popup":
                popup.states.add("SHOWING")
                popup.parent = frame
                frame.children.append(popup)
                if case == "ambiguous":
                    frame.children.append(other_popup)
        else:
            if case != "popup_stays":
                popup.states.discard("SHOWING")
            if case != "miss":
                display.text = "Cherry"

    def escape(chord):
        chords.append(chord)
        popup.states.discard("SHOWING")

    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.press_chord", escape)
    assert _atspi._combo_entry(combo) is None
    _atspi.set_combo_value(combo, "Apple")
    assert clicks == []
    if case == "success":
        with _atspi.read_cache():
            _atspi.set_combo_value(combo, "Cherry")
        assert display.text == "Cherry"
        assert clicks == [(70, 22), (90, 92)]
        assert chords == []
    elif case == "missing_option":
        with pytest.raises(ValueError, match="Apple, Cherry"):
            _atspi.set_combo_value(combo, "Durian")
        assert len(clicks) == 1
        assert chords == ["Escape"]
    else:
        with pytest.raises(ComputerUseError) as exc:
            _atspi.set_combo_value(combo, "Cherry")
        assert exc.value.detail["reason"] == ("text_mismatch" if case in {"miss", "popup_stays"} else "popup_missing")
        assert len(clicks) == (2 if case in {"miss", "popup_stays"} else 0 if case == "no_geometry" else 1)
        assert display.text == ("Cherry" if case == "popup_stays" else "Apple")
        if case == "popup_stays":
            assert chords == ["Escape"]
    assert combo.action_log == []
    assert "SHOWING" in unrelated.states


def test_qt_combo_enabled_entry_is_preserved(atspi):
    entry = _Node("text", states={"ENABLED", "SENSITIVE", "FOCUSABLE"}, toolkit="Qt")
    combo = _Node("combo box", children=[entry], toolkit="Qt")
    assert _atspi._combo_entry(combo) is entry
    assert _atspi._qt_quick_display(combo) is None


@pytest.mark.parametrize("placement", ["frame", "nested", "direct"])
@pytest.mark.parametrize("delay", [0.0, 1.0])
def test_qt_quick_combo_waits_for_shown_rows(atspi, monkeypatch, placement, delay):
    """Delayed menus and combo-owned rows must be ready before option input."""
    display = _Node("text", text="Apple", states={"EDITABLE", "SHOWING"}, toolkit="Qt")
    combo = _Node("combo box", "Fruit", children=[display], toolkit="Qt")
    cherry = _Node("menu item", "Cherry", states={"SHOWING"}, toolkit="Qt")
    popup = _Node("menu", children=[cherry], toolkit="Qt")
    frame = _Node("frame", children=[combo], toolkit="Qt")
    unrelated = _Node("menu", states={"SHOWING"}, toolkit="Qt")
    frame.children.append(unrelated)
    clock = [0.0]
    clicks = []
    probing = [False]

    def publish():
        if clock[0] < delay or not clicks:
            return
        if placement == "direct":
            combo.states.add("EXPANDED")
            combo.children = [display, cherry]
            cherry.parent = combo
        else:
            popup.states.add("SHOWING")
            parent = frame if placement == "frame" else combo
            if popup not in parent.children:
                parent.children.append(popup)
                popup.parent = parent

    def sleep(seconds):
        assert not probing[0], "readiness wait blocked the native event thread"
        clock[0] += seconds
        publish()

    def run(fn):
        assert not probing[0]
        probing[0] = True
        try:
            return fn()
        finally:
            probing[0] = False

    def click(*args, **kwargs):
        clicks.append(clock[0])
        if len(clicks) == 1:
            publish()
        else:
            assert clock[0] >= delay
            display.text = "Cherry"
            popup.states.discard("SHOWING")
            combo.states.discard("EXPANDED")

    monkeypatch.setattr(_atspi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_atspi.time, "sleep", sleep)
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    with _atspi.read_cache():
        _atspi._set_qt_quick_combo(combo, display, "Cherry", run=run)
    assert len(clicks) == 2
    assert delay <= clock[0] <= delay + 0.05
    assert display.text == "Cherry"


def test_qt_quick_combo_readiness_deadline(atspi, monkeypatch):
    display = _Node("text", text="Apple", states={"EDITABLE", "SHOWING"}, toolkit="Qt")
    combo = _Node("combo box", children=[display], toolkit="Qt")
    _Node("frame", children=[combo], toolkit="Qt")
    clock = [0.0]
    clicks = []
    monkeypatch.setattr(_atspi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_atspi.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", lambda *a, **k: clicks.append(a))
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_combo_value(combo, "Cherry")
    assert exc.value.detail["reason"] == "popup_missing"
    assert clock[0] == _atspi._MENU_DIALOG_WAIT_S
    assert len(clicks) == 1
    assert display.text == "Apple"


def test_qt_quick_combo_waits_for_rows_after_menu_shows(atspi, monkeypatch):
    display = _Node("text", text="Apple", states={"EDITABLE", "SHOWING"}, toolkit="Qt")
    combo = _Node("combo box", children=[display], toolkit="Qt")
    cherry = _Node("menu item", "Cherry", toolkit="Qt")
    popup = _Node("menu", toolkit="Qt")
    frame = _Node("frame", children=[combo], toolkit="Qt")
    clock = [0.0]
    clicks = []

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] >= 1.0:
            popup.children = [cherry]
            cherry.parent = popup

    def click(*args, **kwargs):
        clicks.append(clock[0])
        if len(clicks) == 1:
            frame.children.append(popup)
            popup.parent = frame
            popup.states.add("SHOWING")
        else:
            assert popup.children == [cherry]
            display.text = "Cherry"
            popup.states.discard("SHOWING")

    monkeypatch.setattr(_atspi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_atspi.time, "sleep", sleep)
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    _atspi.set_combo_value(combo, "Cherry")
    assert len(clicks) == 2
    assert 1.0 <= clicks[1] <= 1.05


def test_qt_quick_driver_keeps_wait_outside_native_dispatch(atspi, monkeypatch):
    from a11y_computer_use.drivers import _cdp_forms
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Element

    display = _Node("text", text="Apple", states={"EDITABLE", "SHOWING"}, toolkit="Qt")
    combo = _Node("combo box", children=[display], toolkit="Qt")
    driver = LinuxDriver()
    active = [False]
    calls = []

    def run(fn):
        active[0] = True
        try:
            return fn()
        finally:
            active[0] = False

    def select(node, child, value, *, run):
        assert not active[0]
        assert (node, child, value) == (combo, display, "Cherry")
        run(lambda: calls.append(active[0]))

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(driver, "_dom_window_title", lambda handle: "")
    monkeypatch.setattr(driver, "_dom_pids", lambda handle: set())
    monkeypatch.setattr(_cdp_forms, "write_element", lambda *a, **k: None)
    monkeypatch.setattr(_atspi, "is_sheet_cell", lambda handle: False)
    monkeypatch.setattr(_atspi, "control_kind", lambda handle: "combo")
    monkeypatch.setattr(_atspi, "_set_qt_quick_combo", select)
    element = Element("e1", "AXComboBox", "Fruit", "Apple", None, "snap")
    assert driver._apply_set_value(element, "Cherry", combo) is True
    assert calls == [True]


@pytest.mark.parametrize("page_role", ["frame", "window"])
@pytest.mark.parametrize("active_dialog", [True, False])
@pytest.mark.parametrize("write_lands", [True, False])
def test_qt_dialog_spin_write_raises_owning_dialog_and_verifies(
    monkeypatch, page_role, active_dialog, write_lands,
):
    """Scribus's unnamed tab-page window must not raise its blocked main window."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _linux_system
    from a11y_computer_use.drivers import linux as _linux
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Element

    # The bound is on the waits the driver asks for, not on how long the CI
    # host takes to wake from them: macOS runners oversleep each 20 ms
    # read-back pause enough to push three of them past 0.25 s (#797).
    clock = _WaitClock()
    for module in (_atspi, _linux, _linux_system):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    spin = _Node("spin button", "Number of Pages:", text="1", value=1.0,
                 minimum=1.0, maximum=999.0, toolkit="Qt")
    page = _Node(page_role, children=[spin], toolkit="Qt", x=280, y=180, w=700, h=456)
    dialog = _Node("dialog", "New Document", children=[page], toolkit="Qt",
                   x=269, y=142, w=722, h=536)
    app = _Node("application", "scribus", children=[dialog], toolkit="Qt")
    for node in (spin, page, dialog):
        node.get_application = lambda: app
    monkeypatch.setattr(_atspi, "pid_of", lambda node: 99)
    calls = []

    def bounded(*args):
        calls.append("bounded")
        raise RuntimeError("Qt cannot marshal QSpiTextRangeList")

    def write(node, value):
        calls.append("write")
        if write_lands:
            node.value = value
            node.text = str(int(value))
        return True  # An acknowledgement alone must not verify success.

    def read(node):
        calls.append("read")
        return node.value

    monkeypatch.setattr(_Atspi.Text, "get_bounded_ranges", bounded, raising=False)
    monkeypatch.setattr(_Atspi.Value, "set_current_value", write, raising=False)
    monkeypatch.setattr(_Atspi.Value, "get_current_value", read)
    rows = [
        dict(window_id=1, title="Scribus 1.6.3", pid=99, on_screen=True,
             bounds=dict(x=128, y=90, width=1024, height=720)),
        dict(window_id=2, title="New Document", pid=99, on_screen=True,
             bounds=dict(x=269, y=142, width=722, height=536)),
    ]
    monkeypatch.setattr(_linux_system, "windows", lambda: rows)
    monkeypatch.setattr(_linux_system, "active_window",
                        lambda: dict(window_id=2 if active_dialog else 3))
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: spin)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_visible_action", lambda *args: None)
    raised = []

    def raise_window(window_id):
        assert window_id == 2, "blocked main window would exhaust the activation wait"
        raised.append(window_id)

    monkeypatch.setattr(driver, "raise_window", raise_window)
    element = Element("e1", "AXSpinButton", spin.name, "1", None, "snap")
    start = clock.now
    if write_lands:
        assert driver.set_value(element, "3") is True
        assert spin.value == 3.0 and spin.text == "3"
    else:
        with pytest.raises(ComputerUseError) as exc:
            driver.set_value(element, "3")
        assert exc.value.detail["reason"] == "text_mismatch"
        assert spin.value == 1.0
    assert clock.now - start < 0.25
    # Only the read-back poll pauses; no activation or focus wait runs.
    assert clock.sleeps == [_atspi._TEXT_CONFIRM_PAUSE_S] * (0 if write_lands else 3)
    assert raised == ([] if active_dialog else [2])
    assert "bounded" not in calls
    assert calls.count("write") == 1
    # Success ends the read-back poll on the first matching Value response.
    assert calls[calls.index("write") + 1:] == ["read"] * (1 if write_lands else 4)


@pytest.mark.parametrize("toolkit,name,role", [
    ("gtk", "", "window"), ("Qt", "Named window", "window"),
    ("Qt", "", "dialog"),
])
def test_dialog_raise_target_preserves_other_window_shapes(atspi, toolkit, name, role):
    child = _Node(role, name, toolkit=toolkit)
    _Node("dialog", "Parent dialog", children=[child], toolkit=toolkit)
    assert _atspi.toplevel_raise_target(child)[1] == name


def test_unanswered_bounded_ranges_are_remembered_per_app_instance(monkeypatch):
    from types import SimpleNamespace as NS

    monkeypatch.setattr(_atspi, "_unanswered_bounded_range_apps", set())
    monkeypatch.setattr(_atspi, "_toolkit_name", lambda node: "unknown")
    monkeypatch.setattr(_atspi, "app_instance_id", lambda node: node.instance)
    monkeypatch.setattr(_atspi, "_raw_rect", lambda *args: (10, 10, 100, 30))
    monkeypatch.setattr(_atspi, "_drain_atspi_replies", lambda: None)
    bounded_calls = []
    visible_reads = []

    def bounded(node, *args):
        bounded_calls.append(node.instance)
        raise RuntimeError("Did not receive a reply")

    def visible(node, box):
        visible_reads.append(node.instance)
        return node.visible

    monkeypatch.setattr(_atspi, "_qt_character_viewport_text", visible)
    monkeypatch.setattr(_atspi, "_atspi", lambda: NS(
        CoordType=NS(SCREEN=0), TextClipType=NS(BOTH=3),
        Text=NS(get_bounded_ranges=bounded),
    ))
    first = NS(instance="pid:9|start:1|bus::1.2", visible="first")
    sibling = NS(instance=first.instance, visible="scrolled")
    restarted = NS(instance="pid:9|start:2|bus::1.3", visible="new process")
    for node in (first, sibling, restarted):
        assert _atspi._qt_viewport_text(node) == node.visible
    assert bounded_calls == [first.instance, restarted.instance]
    assert visible_reads == [first.instance, sibling.instance, restarted.instance]


@pytest.mark.parametrize("role", ["dialog", "menu"])
@pytest.mark.parametrize("placement", ["frame", "combo"])
@pytest.mark.parametrize("delay", [0.0, 1.0])
def test_qt_quick_combo_refreshes_delayed_popup_container(atspi, monkeypatch, role, placement, delay):
    """A cached hidden popup must publish live state and rows before input."""
    display = _Node("text", text="Apple", states={"EDITABLE", "SHOWING"}, toolkit="Qt")
    combo = _Node("combo box", "Fruit", children=[display], actions=["SetFocus"], toolkit="Qt")
    cherry = _Node("menu item", "Cherry", states={"SHOWING"}, toolkit="Qt")
    popup = _Node(role, toolkit="Qt")
    frame = _Node("frame", children=[combo], toolkit="Qt")
    parent = frame if placement == "frame" else combo
    parent.children.append(popup)
    popup.parent = parent
    clock = [0.0]
    clicks = []
    native = {"opened": False, "closed": False}
    probing = [False]

    def refresh():
        if native["closed"]:
            popup.states.discard("SHOWING")
        elif native["opened"] and clock[0] >= delay:
            popup.states.add("SHOWING")
            popup.children = [cherry]
            cherry.parent = popup

    popup.clear_cache = refresh

    def click(*args, **kwargs):
        clicks.append(clock[0])
        if len(clicks) == 1:
            native["opened"] = True
        else:
            assert "SHOWING" in popup.states
            assert popup.children == [cherry]
            display.text = "Cherry"
            native["closed"] = True
            refresh()

    def sleep(seconds):
        assert not probing[0], "readiness wait must leave the event thread free"
        clock[0] += seconds

    def run(fn):
        probing[0] = True
        try:
            return fn()
        finally:
            probing[0] = False

    monkeypatch.setattr(_atspi.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_atspi.time, "sleep", sleep)
    monkeypatch.setattr("a11y_computer_use.drivers._linux_input.click", click)
    with _atspi.read_cache():
        _atspi._set_qt_quick_combo(combo, display, "Cherry", run=run)
    assert clicks[0] == 0.0
    assert len(clicks) == 2
    assert delay <= clicks[1] <= delay + 0.05
    assert display.text == "Cherry"
    assert "SHOWING" not in popup.states
    assert combo.action_log == []


class _Utf16QtField(_Field):
    """QString storage with native UTF-16 insertion/deletion offsets."""

    def __init__(self, text="", caret=None, selection=None):
        super().__init__(text, caret, toolkit="Qt")
        self.caret = len(text.encode("utf-16-le")) // 2 if caret is None else caret
        self.selection = selection
        self.deletions = []

    def insert_text(self, pos, text, length):
        self.lengths.append(length)
        raw = self.text.encode("utf-16-le")
        chunk = text.encode("utf-16-le")[:length * 2]
        self.text = (raw[:pos * 2] + chunk + raw[pos * 2:]).decode("utf-16-le", errors="replace")
        self.caret = pos + length
        return True

    def delete_text(self, start, end):
        self.deletions.append((start, end))
        raw = self.text.encode("utf-16-le")
        self.text = (raw[:start * 2] + raw[end * 2:]).decode("utf-16-le")
        self.caret = start
        self.selection = None
        return True

    def set_text_contents(self, text):
        self.text = text
        return True


@pytest.fixture
def utf16_atspi(atspi, monkeypatch):
    monkeypatch.setattr(_Atspi.Text, "get_character_count",
                        staticmethod(lambda acc: len(acc.text.encode("utf-16-le")) // 2))
    monkeypatch.setattr(_Atspi.Text, "get_n_selections",
                        staticmethod(lambda acc: int(acc.selection is not None)))
    monkeypatch.setattr(_Atspi.Text, "get_selection",
                        staticmethod(lambda acc, index: acc.selection), raising=False)


@pytest.mark.parametrize("text", ["😀👍🏽", "👩‍💻", "a😀b", "𠀀", "日本語", "e\u0301"])
def test_qt_utf16_insert_and_set_text(utf16_atspi, text):
    field = _Utf16QtField()
    assert _atspi.insert_text(field, text) == len(text)
    assert field.text == text
    assert field.lengths == [len(text.encode("utf-16-le")) // 2]
    field = _Utf16QtField("old")
    assert _atspi.set_text(field, text)
    assert field.text == text


@pytest.mark.parametrize("caret", [2, None])
def test_qt_insert_after_astral_prefix(utf16_atspi, caret):
    field = _Utf16QtField("😀", caret=caret)
    assert _atspi.insert_text(field, "b") == 1
    assert field.text == "😀b"
    assert field.caret == 3


def test_qt_insert_in_middle_after_astral_prefix(utf16_atspi):
    field = _Utf16QtField("😀tail", caret=2)
    assert _atspi.insert_text(field, "👩‍💻") == 3
    assert field.text == "😀👩‍💻tail"
    assert field.caret == 7


def test_qt_replace_selection_with_astral_prefix_and_suffix(utf16_atspi):
    field = _Utf16QtField("😀a👍🏽z𠀀", caret=7, selection=(2, 7))
    assert _atspi.insert_text(field, "👩‍💻") == 3
    assert field.text == "😀👩‍💻z𠀀"
    assert field.deletions == [(2, 7)]
    assert field.caret == 7


@pytest.mark.parametrize("selection", [None, (1, 2)])
def test_qt_refuses_split_surrogate_before_mutation(utf16_atspi, selection):
    field = _Utf16QtField("😀", caret=1, selection=selection)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.insert_text(field, "b")
    assert exc.value.detail["reason"] == "invalid_text_offset"
    assert field.text == "😀"
    assert field.lengths == []
    assert field.deletions == []


def test_qt_astral_readback_still_rejects_hidden_suffix(utf16_atspi):
    field = _Utf16QtField("😀\x00junk")
    assert not _atspi._confirm_text(field, "😀")


def test_gtk_astral_insert_keeps_codepoint_offsets(atspi):
    field = _ByteField("😀tail", caret=1, toolkit="gtk")
    assert _atspi.insert_text(field, "👩‍💻") == 3
    assert field.text == "😀👩‍💻tail"
    assert field.lengths == [len("👩‍💻".encode("utf-8"))]


@pytest.mark.parametrize("qt", [False, True])
@pytest.mark.parametrize("selected", [False, True])
def test_insert_advances_stationary_caret_in_native_units(utf16_atspi, monkeypatch, qt, selected):
    """Scintilla-style inserts must compose in order, including Unicode."""
    if not qt:
        monkeypatch.setattr(_Atspi.Text, "get_character_count", lambda acc: len(acc.text))
    unit_length = _atspi._utf16_length if qt else len
    prefix = "😀"
    start = unit_length(prefix)
    field = (_Utf16QtField(prefix + "oldtail", caret=start) if qt
             else _ByteField(prefix + "oldtail", caret=start, toolkit="gtk"))
    field.selection = (start, start + 3) if selected else None
    original_delete = field.delete_text

    def delete_selection(start, end):
        result = original_delete(start, end)
        field.selection = None
        return result

    field.delete_text = delete_selection
    original_insert = field.insert_text

    def stationary_insert(pos, text, length):
        result = original_insert(pos, text, length)
        field.caret = pos
        return result

    if not qt:
        stationary_insert._length_unit = "bytes"
    field.insert_text = stationary_insert
    moves = []

    def set_caret(acc, offset):
        moves.append(offset)
        acc.caret = offset
        return True

    monkeypatch.setattr(_Atspi.Text, "set_caret_offset", set_caret, raising=False)
    with _atspi.read_cache():
        assert _atspi.insert_text(field, "A👩‍💻") == 4
        assert _atspi.insert_text(field, "CD") == 2
    assert field.text == prefix + "A👩‍💻CD" + ("tail" if selected else "oldtail")
    assert field.caret == unit_length(prefix + "A👩‍💻CD")
    assert moves == [unit_length(prefix + "A👩‍💻"), field.caret]


@pytest.mark.parametrize("failure", ["no_effect", "refused", "raises"])
def test_insert_reports_unverified_caret_after_text_changed(atspi, monkeypatch, failure):
    field = _ByteField("prefix", caret=6, toolkit="gtk")
    original_insert = field.insert_text

    def stationary_insert(pos, text, length):
        original_insert(pos, text, length)
        field.caret = pos
        return True

    field.insert_text = stationary_insert

    def set_caret(acc, offset):
        if failure == "raises":
            raise RuntimeError("caret unavailable")
        return failure == "no_effect"

    monkeypatch.setattr(_Atspi.Text, "set_caret_offset", set_caret, raising=False)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.insert_text(field, "AB")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "caret_not_advanced"
    assert exc.value.detail["unchanged"] is False
    assert field.text == "prefixAB"
    assert field.caret == 6


@pytest.mark.parametrize("text", ["", "AB", "👩‍💻"])
def test_insert_preserves_native_caret_advancement(atspi, monkeypatch, text):
    field = _ByteField("prefix", caret=6, toolkit="gtk")
    moves = []
    monkeypatch.setattr(_Atspi.Text, "set_caret_offset",
                        lambda acc, offset: moves.append(offset), raising=False)
    assert _atspi.insert_text(field, text) == len(text)
    assert field.caret == 6 + len(text)
    assert moves == []


@pytest.mark.parametrize("role", ["file chooser", "color chooser"])
@pytest.mark.parametrize("cover", [False, True])
def test_embedded_gtk_chooser_uses_owning_dialog_for_occlusion(atspi, monkeypatch, role, cover):
    """#918: embedded chooser geometry must not match the blocked main frame."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Bounds, Element

    cell = _Node("table cell", "track-b.ogg", toolkit="gtk")
    chooser = _Node(role, children=[cell], toolkit="gtk", x=100, y=100, w=400, h=300)
    dialog = _Node("dialog", "Open Media Files", children=[chooser], toolkit="gtk",
                   states=("ACTIVE",), x=90, y=90, w=420, h=340)
    frame = _Node("frame", "Parole", children=[dialog], toolkit="gtk")
    monkeypatch.setattr(_atspi, "pid_of", lambda node: 42)
    rows = [dict(window_id=1, title="Parole", pid=42, on_screen=True,
                 bounds=dict(x=0, y=0, width=800, height=600)),
            dict(window_id=2, title="Open Media Files", pid=42, on_screen=True,
                 bounds=dict(x=90, y=90, width=420, height=340))]
    if cover:
        rows.append(dict(window_id=3, title="Other dialog", pid=42, on_screen=True,
                         bounds=dict(x=90, y=90, width=420, height=340)))
    monkeypatch.setattr(_linux_system, "windows", lambda: rows)
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: 42)
    monkeypatch.setattr(_linux_system, "window_at_point", lambda x, y: rows[-1])
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_gtk4_point_untrusted", lambda *args: False)
    element = Element("e1", "AXCell", "track-b.ogg", None, Bounds(0, 120, 120, 100, 25), "snap")
    for node in (cell, chooser, dialog):
        monkeypatch.setattr(observe, "ax_handle_for", lambda *args: node)
        assert _atspi.toplevel_raise_target(node)[:3] == (True, "Open Media Files", 42)
        expected = "Other dialog" if cover else None
        assert driver.same_app_window_cover(element, "parole") == expected
        assert driver.same_app_window_cover(
            element, "parole", _discovery=(element, rows, {}),
        ) == expected
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: frame)
    assert driver.same_app_window_cover(element, "parole") == (
        "Other dialog" if cover else "Open Media Files"
    )


def test_standalone_unnamed_gtk_chooser_keeps_own_window(atspi):
    chooser = _Node("file chooser", toolkit="gtk")
    _Node("application", "parole", children=[chooser], toolkit="gtk")
    assert _atspi.toplevel_raise_target(chooser)[1] == ""


def test_named_gtk_chooser_keeps_own_window(atspi):
    chooser = _Node("file chooser", "Open File", toolkit="gtk")
    _Node("frame", "Editor", children=[chooser], toolkit="gtk")
    assert _atspi.toplevel_raise_target(chooser)[1] == "Open File"


@pytest.mark.parametrize("lands", [True, False])
def test_qt_focus_retry_activates_window_and_requires_native_focus(atspi, monkeypatch, lands):
    """GrabFocus on an inactive Qt child acknowledges without focusing it."""
    from a11y_computer_use.drivers import _linux_system

    field = _Node("text", "Name", toolkit="Qt", states={"EDITABLE"})
    window = _Node("filler", "Qt form", toolkit="Qt", children=[field])
    _Node("application", "qtapp", toolkit="Qt", children=[window])
    monkeypatch.setattr(_atspi, "pid_of", lambda node: 42)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *args: None)
    clock = _WaitClock()
    monkeypatch.setattr(_atspi, "time", clock)
    calls = []
    active = False
    rows = [dict(window_id=7, pid=42)]
    monkeypatch.setattr(_linux_system, "windows", lambda: rows + [dict(window_id=8, pid=43)])

    def match(**kwargs):
        assert kwargs == dict(title="Qt form", pid=42, extents=(10., 10., 120., 24.), rows=rows)
        return 7

    def activate(window_id):
        nonlocal active
        assert window_id == 7
        calls.append("activate")
        active = lands
        return True  # An acknowledged request is not proof of focus.

    def grab(acc):
        assert acc is field
        calls.append("grab")
        if active:
            field.states.add("FOCUSED")
        return True

    monkeypatch.setattr(_linux_system, "window_id_for_toplevel", match)
    monkeypatch.setattr(_linux_system, "request_input_activation", activate)
    monkeypatch.setattr(_atspi, "grab_focus", grab)
    monkeypatch.setattr(_atspi, "_type_string", lambda text: calls.append(text))
    if lands:
        _atspi._type_into_target(field, "OK")
        assert calls == ["activate", "grab", "O", "K"]
        assert len(clock.sleeps) == _atspi._FOCUS_CONFIRM_REGRAB
    else:
        with pytest.raises(ComputerUseError, match="nothing was typed") as exc:
            _atspi._type_into_target(field, "OK")
        assert exc.value.code is ErrorCode.FOCUS_LOST
        assert calls == ["activate", "grab"]
        assert len(clock.sleeps) == _atspi._FOCUS_CONFIRM_POLLS - 1
    assert all(pause == _atspi._FOCUS_CONFIRM_PAUSE_S for pause in clock.sleeps)


@pytest.mark.parametrize("toolkit,parent_role,expected", [
    ("Qt", "application", True), ("gtk", "application", False),
    ("Qt", "panel", False),
])
def test_qwidget_raise_target_requires_qt_application_child(atspi, toolkit, parent_role, expected):
    field = _Node("text", toolkit=toolkit)
    window = _Node("filler", "Form", toolkit=toolkit, children=[field])
    _Node(parent_role, toolkit=toolkit, children=[window])
    target = _atspi.toplevel_raise_target(field)
    assert (target is not None) is expected
    if expected:
        assert target[1] == "Form"


@pytest.mark.parametrize("toolkit,focused", [("Qt", True), ("gtk", False)])
def test_focus_retry_does_not_activate_focused_or_non_qt_targets(atspi, monkeypatch, toolkit, focused):
    from a11y_computer_use.drivers import _linux_system

    field = _Node("text", toolkit=toolkit, states={"FOCUSED"} if focused else set())
    monkeypatch.setattr(_linux_system, "windows", lambda: pytest.fail("unexpected activation lookup"))
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)
    assert _atspi._confirm_focus_on_target(field) is focused


def test_qt_focus_recovery_does_not_match_another_process(atspi, monkeypatch):
    from a11y_computer_use.drivers import _linux_system

    field = _Node("text", toolkit="Qt")
    window = _Node("filler", "Same title", toolkit="Qt", children=[field])
    _Node("application", toolkit="Qt", children=[window])
    monkeypatch.setattr(_atspi, "pid_of", lambda node: 42)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_linux_system, "windows", lambda: [dict(
        window_id=8, pid=43, title="Same title", on_screen=True,
        bounds=dict(x=10, y=10, width=120, height=24),
    )])
    monkeypatch.setattr(_linux_system, "request_input_activation",
                        lambda wid: pytest.fail("activated a different app"))
    _atspi._request_qt_focus_activation(field)


def test_gtk_selected_item_names_use_labels_in_snapshot_and_find(atspi):
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Display, Scope

    states = {"ENABLED", "SENSITIVE", "SHOWING", "VISIBLE"}
    combos, labels = [], []
    for index, (caption, value) in enumerate([
        ("Hinting", "None"), ("Sub-pixel order", "None"), ("Window Scaling", "1x"),
    ]):
        label = _Node("label", caption + ":", toolkit="gtk", x=10,
                      y=50 + index * 40, w=120, states=states)
        item = _Node("menu item", value, toolkit="gtk", states={"SELECTED"})
        combo = _Node("combo box", value, toolkit="gtk", children=[item],
                      x=150, y=50 + index * 40, states=states, actions=["press"])
        if index == 2:
            combo.relations = [_Relation("LABELLED_BY", [label])]
            # The explicit relation must win even over a different row label.
            label = _Node("label", "Wrong row label:", toolkit="gtk",
                          x=10, y=130, w=120, states=states)
        combos.append(combo)
        labels.append(label)
    panel = _Node("panel", children=[*reversed(combos), *reversed(labels)],
                  toolkit="gtk", x=0, y=0, w=500, h=300, states=states)
    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    for selected in ("None", "Full"):
        combos[0].name = combos[0].children[0].name = selected
        snap = observe.build_snapshot(
            panel, ATSPIAccessor(), scope=Scope.WINDOW, app="ComboRepro", pid=None,
            geometry=(observe.DisplayGeometry(display=display, origin=(0.0, 0.0)),),
        )
        for index, caption in enumerate(("Hinting", "Sub-pixel order", "Window Scaling")):
            matches = observe.find_elements(snap, text=caption, clickable=True)
            assert len(matches) == 1
            element = matches[0]
            assert element.title == caption
            assert element.value == combos[index].name
            assert element.bounds.x == 150
            assert element.bounds.y == 50 + index * 40
            assert observe.ax_handle_for(snap.snapshot_id, element.ref) is combos[index]


@pytest.mark.parametrize("case", ["explicit", "non_gtk", "unknown_value", "missing_bounds",
                                  "other_row", "right_label", "ambiguous", "no_label"])
def test_gtk_combo_names_preserve_names_without_matching_value_and_label(atspi, case):
    toolkit = "other" if case == "non_gtk" else "gtk"
    item = _Node("menu item", "None", toolkit=toolkit, states={"SELECTED"})
    name = "Explicit name" if case == "explicit" else "None"
    combo = _Node("combo box", name, toolkit=toolkit,
                  children=[] if case == "unknown_value" else [item], x=150, y=50)
    label = _Node("label", "Hinting:", toolkit=toolkit, x=10, y=50, w=120)
    if case == "missing_bounds":
        combo.get_component_iface = lambda: None
    if case == "other_row":
        label.rect.y = 100
    if case == "right_label":
        label.rect.x = 300
    children = [combo] if case == "no_label" else [combo, label]
    if case == "ambiguous":
        children.append(_Node("label", "Other:", toolkit=toolkit, x=10, y=50, w=120))
    _Node("panel", children=children, toolkit=toolkit)
    assert _read(combo).title == name


def test_combo_entry_rejects_child_owned_by_another_parent(atspi):
    # A stale child list once handed a Firefox <select> the focused Notes
    # textarea; set_value then wrote there and reported "set".
    notes = _Node("entry", states={"ENABLED", "SENSITIVE", "FOCUSABLE"})
    form = _Node("section", children=[notes])
    combo = _Node("combo box", children=[])
    combo.children.append(notes)
    assert notes.get_parent() is form
    assert _atspi._combo_entry(combo) is None
