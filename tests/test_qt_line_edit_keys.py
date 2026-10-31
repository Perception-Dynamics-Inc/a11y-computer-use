"""#1418: Qt line edits are written with key events so Qt emits textEdited.

An AT-SPI EditableText write changes a QLineEdit's text without Qt's
user-edit signal, so QColorDialog's "HTML:" field shows the new value while
the colour stays unchanged. The fake field below models that: EditableText
writes change the text only, and key events also count as a user edit.
"""
from __future__ import annotations

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _cdp_forms, linux
from a11y_computer_use.schema import Bounds, ComputerUseError, Element


# Probed from a real PyQt6 6.11 / Qt 6.12 QColorDialog over the AT-SPI bus:
# the "HTML:" QLineEdit is role "text" with these states and Text plus
# EditableText; QPlainTextEdit and QTextEdit add MULTI_LINE; a read-only
# QLineEdit adds READ_ONLY. The application's ToolkitName is "Qt".
_REAL_QT6_LINE_EDIT_STATES = frozenset(
    {"EDITABLE", "ENABLED", "FOCUSABLE", "SENSITIVE", "SHOWING", "VISIBLE"})


class _LineEdit:
    """A QLineEdit: keys fire textEdited, EditableText does not."""

    def __init__(self, text="#ff0000", *, selects_all=True, accepts=None):
        self.text, self.caret, self.sel = text, len(text), (len(text), len(text))
        self.selects_all, self.accepts = selects_all, accepts
        self.edited = []          # textEdited emissions (user edits)
        self.keys = []            # chords and typed strings, in order
        self.editable_text_writes = 0

    def chord(self, chord):
        self.keys.append(chord)
        if chord == "ctrl+a" and self.selects_all:
            self.sel = (0, len(self.text))
        elif chord == "End":
            self.caret = len(self.text)
            self.sel = (self.caret, self.caret)
        elif chord == "BackSpace":
            start, end = self.sel
            if start != end:
                self.text = self.text[:start] + self.text[end:]
                self.caret, self.sel = start, (start, start)
                self.edited.append(self.text)

    def type(self, text):
        self.keys.append(text)
        start, end = self.sel if self.sel[0] != self.sel[1] else (self.caret, self.caret)
        new = self.text[:start] + text + self.text[end:]
        if self.accepts is not None:
            new = self.accepts(new, self.text)
        self.text = new
        self.caret = start + len(text)
        self.sel = (self.caret, self.caret)
        self.edited.append(new)

    def editable_text_set(self, value):
        self.editable_text_writes += 1
        self.text = value
        return True


def _element(qt=True):
    return Element(ref="e3", role="AXTextField", title="Hex colour:", value="#ff0000",
                   bounds=Bounds(0, 10, 10, 120, 24), snapshot_id="s0",
                   path=("AXTextField",), editable=True)


@pytest.fixture
def field(monkeypatch):
    f = _LineEdit()
    # The state set real Qt 6 exposes for a QLineEdit: no SINGLE_LINE.
    states = set(_REAL_QT6_LINE_EDIT_STATES) | {"FOCUSED"}
    monkeypatch.setattr(_atspi, "_qt_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_editable_iface", lambda acc: object())
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    monkeypatch.setattr(_atspi, "_role_name", lambda acc: "text")
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: name in states)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: acc.text)
    monkeypatch.setattr(_atspi, "_qt_text_state",
                        lambda acc: (acc.text, acc.caret, acc.sel[0], acc.sel[1]), raising=False)
    monkeypatch.setattr(_atspi, "_chord_into_target", lambda acc, chord: acc.chord(chord))
    monkeypatch.setattr(_atspi, "_type_into_target", lambda acc, text: acc.type(text))
    monkeypatch.setattr(_atspi, "set_text", lambda acc, value: acc.editable_text_set(value))
    monkeypatch.setattr(_atspi, "insert_text",
                        lambda acc, text: acc.editable_text_set(acc.text + text) and len(text))
    monkeypatch.setattr(_atspi.time, "sleep", lambda s: None)
    monkeypatch.setattr(linux.time, "sleep", lambda s: None)
    return f


def _driver(monkeypatch, f):
    drv = object.__new__(linux.LinuxDriver)
    drv._run = lambda fn: fn()
    drv._focused_editable = None
    drv._type_expected = None
    drv._writer_cell_verified = None
    drv._check_readback = None
    for name in ("_refuse_input_off_target", "_visible_action", "_check_modal_target",
                 "_raise_handle_window", "_refuse_chromium_occluded"):
        monkeypatch.setattr(linux.LinuxDriver, name, lambda self, *a, **k: None)
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: f)
    monkeypatch.setattr(_cdp_forms, "write_element", lambda *a, **k: None)
    for name, value in (("_refuse_invalid_field_value", None), ("refuse_open_composition", None),
                        ("_refuse_gecko_date_segment_replace", None), ("peer_field_texts", {}),
                        ("_refuse_model_editor_replace", None), ("is_sheet_cell", False),
                        ("qt_checkable_item", False), ("qt_item_view_cell", False),
                        ("control_kind", "text"), ("_refuse_drawing_multiline_value", None),
                        ("_capture_text_points", []), ("_blocks_text_replace", False),
                        ("web_entry_context", None), ("grab_focus", True),
                        ("_confirm_focus_on_target", True), ("_focus_on_target", True),
                        ("focus_web_entry", None)):
        monkeypatch.setattr(_atspi, name, (lambda v: lambda *a, **k: v)(value))
    monkeypatch.setattr(_atspi, "focus_web_entry", lambda acc, **k: acc)
    return drv


def test_set_value_on_qt_line_edit_types_keys_and_fires_text_edited(monkeypatch, field):
    drv = _driver(monkeypatch, field)
    assert drv.set_value(_element(), "#00ff00") is True
    assert field.text == "#00ff00"
    assert field.edited == ["#00ff00"], "the app's user-edit path must fire"
    assert field.editable_text_writes == 0
    assert field.keys == ["ctrl+a", "#00ff00"]


def test_set_value_refuses_when_select_all_does_not_cover_the_value(monkeypatch, field):
    field.selects_all = False
    drv = _driver(monkeypatch, field)
    with pytest.raises(ComputerUseError) as err:
        drv.set_value(_element(), "#00ff00")
    assert err.value.detail["reason"] == "selection_not_whole"
    assert field.keys == ["ctrl+a"] and field.text == "#ff0000"
    assert field.editable_text_writes == 0


def test_set_value_reports_mismatch_when_the_app_rejects_the_keys(monkeypatch, field):
    field.accepts = lambda new, old: new[:3]   # a validator keeps three characters
    drv = _driver(monkeypatch, field)
    with pytest.raises(ComputerUseError) as err:
        drv.set_value(_element(), "#00ff00")
    assert err.value.detail["reason"] == "text_mismatch"
    assert err.value.detail["input_sent"] is True
    assert field.editable_text_writes == 0


def test_set_value_never_falls_back_to_editable_text_without_x11_keys(monkeypatch, field):
    drv = _driver(monkeypatch, field)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: False)
    with pytest.raises(ComputerUseError):
        drv.set_value(_element(), "#00ff00")
    assert field.text == "#ff0000" and field.edited == [] and field.editable_text_writes == 0


def test_ref_less_type_after_select_all_replaces_with_keys(monkeypatch, field):
    from a11y_computer_use.drivers import _linux_system

    drv = _driver(monkeypatch, field)
    monkeypatch.setattr(_linux_system, "refuse_unviewable_input", lambda: None)
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    monkeypatch.setattr(_atspi, "note_focused_find_bar", lambda app: None)
    monkeypatch.setattr(linux.LinuxDriver, "_verify_remembered_editable", lambda self, h: "python")
    monkeypatch.setattr(linux.LinuxDriver, "visible_actions", None, raising=False)
    field.chord("ctrl+a")              # the caller's own key ctrl+a
    field.keys.clear()
    drv._focused_editable = field
    assert drv._type_text_unguarded("#00ff00") == 7
    assert field.text == "#00ff00" and field.edited == ["#00ff00"]
    assert field.editable_text_writes == 0 and field.keys == ["#00ff00"]


def test_ref_type_into_qt_line_edit_uses_keys(monkeypatch, field):
    drv = _driver(monkeypatch, field)
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *a: None, raising=False)
    monkeypatch.setattr(_atspi, "drawing_text_editable", lambda acc: False)
    monkeypatch.setattr(_atspi, "math_command_editable", lambda acc: False)
    monkeypatch.setattr(_atspi, "gtk4_entry_needs_pointer_focus", lambda acc: False)
    monkeypatch.setattr(_atspi, "gecko_entry_caret_to_end", lambda acc: None)
    monkeypatch.setattr(_atspi, "gecko_plain_entry", lambda acc: False)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: False)
    monkeypatch.setattr(_atspi, "append_target_entry", lambda acc: False)
    monkeypatch.setattr(_atspi, "changed_peer_fields", lambda acc, peers: [])
    monkeypatch.setattr(linux.LinuxDriver, "_refuse_hidden", lambda self, *a: None)
    field.text, field.caret, field.sel = "", 0, (0, 0)
    assert drv.type_into(_element(), "#00ff00") == 7
    assert field.text == "#00ff00" and field.edited == ["#00ff00"]
    assert field.editable_text_writes == 0


def test_newline_in_set_value_is_refused_before_any_key(monkeypatch, field):
    drv = _driver(monkeypatch, field)
    with pytest.raises(ComputerUseError) as err:
        drv.set_value(_element(), "a\nb")
    assert err.value.detail["reason"] == "single_line_field" and field.keys == []


@pytest.mark.parametrize("toolkit_qt,states,role,expect", [
    (True, {"SINGLE_LINE", "EDITABLE"}, "text", True),
    (True, {"SINGLE_LINE", "EDITABLE"}, "entry", True),
    (True, {"MULTI_LINE", "EDITABLE"}, "text", False),
    (True, {"SINGLE_LINE"}, "text", False),
    (False, {"SINGLE_LINE", "EDITABLE"}, "text", False),   # GTK and Chrome unchanged
    (True, {"SINGLE_LINE", "EDITABLE"}, "spin button", False),
])
def test_qt_line_edit_detection(monkeypatch, toolkit_qt, states, role, expect):
    monkeypatch.setattr(_atspi, "_editable_iface", lambda acc: object())
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    monkeypatch.setattr(_atspi, "_qt_app", lambda acc: toolkit_qt)
    monkeypatch.setattr(_atspi, "_role_name", lambda acc: role)
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: name in states)
    assert _atspi.qt_line_edit(object()) is expect


class _QtApp:
    def get_toolkit_name(self):
        return "Qt"


class _QtNode:
    def __init__(self, role="text"):
        self.role = role

    def get_application(self):
        return _QtApp()

    def get_role_name(self):
        return self.role


@pytest.mark.parametrize("extra,role,expect", [
    (set(), "text", True),                      # QLineEdit (QColorDialog "HTML:")
    ({"MULTI_LINE"}, "text", False),            # QPlainTextEdit / QTextEdit
    ({"READ_ONLY"}, "text", False),             # read-only QLineEdit
])
def test_qt_line_edit_detection_on_the_real_qt6_attribute_shape(monkeypatch, extra, role, expect):
    states = set(_REAL_QT6_LINE_EDIT_STATES) | extra
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: name in states)
    monkeypatch.setattr(_atspi, "_editable_iface", lambda acc: object())
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    assert "SINGLE_LINE" not in states
    assert _atspi.qt_line_edit(_QtNode(role)) is expect


def test_qt_password_field_is_not_taken_by_the_key_path(monkeypatch):
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: name in _REAL_QT6_LINE_EDIT_STATES)
    monkeypatch.setattr(_atspi, "_editable_iface", lambda acc: object())
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: True)
    assert _atspi.qt_line_edit(_QtNode()) is False


def _hex_validator(new, old):
    """QColorDialog's HTML validator: '#' and up to six hex digits."""
    import re
    return new if re.fullmatch(r"#?[0-9A-Fa-f]{0,6}", new) else old


def _typing(validator):
    """Key-by-key acceptance, the way Qt applies a validator per keystroke."""
    def accepts(new, old):
        return validator(new, old)
    return accepts


@pytest.mark.parametrize("value,partial", [("hello", "#e"), ("#00ff00ff", "#00ff00")])
def test_set_value_with_a_rejected_value_is_refused_and_restored(monkeypatch, field, value, partial):
    def per_key(f):
        orig = f.type

        def typ(text):
            for ch in text:
                orig(ch)
        return typ
    field.accepts = _hex_validator
    field.type = per_key(field)
    drv = _driver(monkeypatch, field)
    with pytest.raises(ComputerUseError) as err:
        drv.set_value(_element(), value)
    d = err.value.detail
    # Real Qt left "#e" and "#00ff00" (probed); the fake's validator differs in detail.
    assert d["reason"] == "text_mismatch" and d["actual"] != value
    assert d["restored"] is True and field.text == "#ff0000"
    assert field.editable_text_writes == 0


def test_max_length_truncation_is_refused_and_the_original_restored(monkeypatch, field):
    field.text, field.caret, field.sel = "abcd", 4, (4, 4)
    field.accepts = lambda new, old: new[:4]
    drv = _driver(monkeypatch, field)
    with pytest.raises(ComputerUseError) as err:
        drv.set_value(_element(), "wxyz12")
    assert err.value.detail["actual"] == "wxyz"
    assert err.value.detail["restored"] is True and field.text == "abcd"


def test_restore_failure_is_reported_honestly(monkeypatch, field):
    field.accepts = lambda new, old: new[:3]
    drv = _driver(monkeypatch, field)
    real_chord = field.chord

    def chord(c):
        real_chord(c)
        if len(field.keys) > 1:      # the restore's ctrl+a does not select
            field.sel = (field.caret, field.caret)
    field.chord = chord
    with pytest.raises(ComputerUseError) as err:
        drv.set_value(_element(), "#00ff00")
    assert err.value.detail["restored"] is False and field.text == "#00"


def test_ref_type_appends_at_the_end_even_when_the_caret_is_mid_text(monkeypatch, field):
    drv = _driver(monkeypatch, field)
    for name, value in (("drawing_text_editable", False), ("math_command_editable", False),
                        ("gtk4_entry_needs_pointer_focus", False), ("gecko_entry_caret_to_end", None),
                        ("gecko_plain_entry", False), ("_chromium_app", False),
                        ("append_target_entry", False), ("changed_peer_fields", [])):
        monkeypatch.setattr(_atspi, name, (lambda v: lambda *a, **k: v)(value))
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *a: None, raising=False)
    monkeypatch.setattr(linux.LinuxDriver, "_refuse_hidden", lambda self, *a: None)
    field.text, field.caret, field.sel = "abc", 1, (1, 1)    # a click left the caret mid-text
    assert drv.type_into(_element(), "Z") == 1
    assert field.text == "abcZ" and field.keys == ["End", "Z"]
