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


def test_ref_type_inserts_at_a_mid_text_caret_like_real_typing(monkeypatch, field):
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
    assert field.text == "aZbc" and field.keys == ["Z"], "no End: keys land at the caret"


def _ref_type_driver(monkeypatch, f):
    drv = _driver(monkeypatch, f)
    for name, value in (("drawing_text_editable", False), ("math_command_editable", False),
                        ("gtk4_entry_needs_pointer_focus", False), ("gecko_entry_caret_to_end", None),
                        ("gecko_plain_entry", False), ("_chromium_app", False),
                        ("changed_peer_fields", [])):
        monkeypatch.setattr(_atspi, name, (lambda v: lambda *a, **k: v)(value))

    def append_target_entry(acc):
        # The GTK rule (#1232) collapses a whole selection; a Qt line edit
        # must never reach it, or a caller's ctrl+a would be lost.
        raise AssertionError("append_target_entry consulted for a Qt line edit")
    monkeypatch.setattr(_atspi, "append_target_entry", append_target_entry)
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *a: None, raising=False)
    monkeypatch.setattr(linux.LinuxDriver, "_refuse_hidden", lambda self, *a: None)
    return drv


def test_ref_type_after_ctrl_a_replaces_the_whole_field(monkeypatch, field):
    drv = _ref_type_driver(monkeypatch, field)
    field.chord("ctrl+a")                       # the caller's own select-all
    field.keys.clear()
    assert drv.type_into(_element(), "#00ff00") == 7
    assert field.text == "#00ff00" and field.edited == ["#00ff00"]
    assert field.keys == ["#00ff00"], "End must not be pressed over a selection"


def test_ref_type_replaces_a_partial_selection(monkeypatch, field):
    drv = _ref_type_driver(monkeypatch, field)
    field.text, field.caret, field.sel = "#ff0000", 3, (1, 3)   # "ff" selected
    assert drv.type_into(_element(), "00") == 2
    assert field.text == "#000000" and field.keys == ["00"]


def test_ref_type_with_a_bare_caret_inserts_at_the_caret_offset(monkeypatch, field):
    drv = _ref_type_driver(monkeypatch, field)
    field.text, field.caret, field.sel = "hello world", 2, (2, 2)   # a plain QLineEdit
    assert drv.type_into(_element(), "XY") == 2
    assert field.text == "heXYllo world" and field.keys == ["XY"]


def test_hex_field_caret_z_is_refused_and_restored_not_inserted(monkeypatch, field):
    # QColorDialog's hex validator drops "Z", so "#fZf0000" can never read
    # back; the honest result is text_mismatch with the original restored.
    drv = _ref_type_driver(monkeypatch, field)
    field.accepts = _hex_validator
    field.text, field.caret, field.sel = "#ff0000", 2, (2, 2)
    with pytest.raises(ComputerUseError) as err:
        drv.type_into(_element(), "Z")
    d = err.value.detail
    assert d["reason"] == "text_mismatch" and d["expected"] == "#fZf0000"
    assert d["actual"] == "#ff0000" and d["restored"] is True and field.text == "#ff0000"


def test_ref_type_at_a_caret_refuses_when_the_read_back_is_not_the_caret_insert(monkeypatch, field):
    drv = _ref_type_driver(monkeypatch, field)
    field.text, field.caret, field.sel = "#ff0000", 2, (2, 2)
    real_type = field.type

    def appends(text):                  # the app moved the caret to End first, once
        field.type = real_type
        field.caret, field.sel = len(field.text), (len(field.text),) * 2
        real_type(text)
    field.type = appends
    with pytest.raises(ComputerUseError) as err:
        drv.type_into(_element(), "Z")
    assert err.value.detail["reason"] == "text_mismatch"
    assert err.value.detail["expected"] == "#fZf0000"
    assert err.value.detail["actual"] == "#ff0000Z"
    assert err.value.detail["restored"] is True and field.text == "#ff0000"


class _ColorDialog:
    """QColorDialog: the HTML field's textEdited rewrites the RGB spin boxes."""

    def __init__(self, f):
        self.f = f
        self.peers = {"Red": "255", "Green": "0", "Blue": "0"}
        real = f.type

        def type_and_sync(text):
            real(text)
            v = f.text
            if len(v) == 7 and v.startswith("#"):
                self.peers = {k: str(int(v[i:i + 2], 16))
                              for k, i in (("Red", 1), ("Green", 3), ("Blue", 5))}
        f.type = type_and_sync

    def collect(self, acc):
        return {f"spin box|{k}|#0": v for k, v in self.peers.items()}


def _color_dialog_driver(monkeypatch, field):
    drv = _ref_type_driver(monkeypatch, field)
    dialog = _ColorDialog(field)
    monkeypatch.setattr(_atspi, "_collect_peer_fields", dialog.collect)
    monkeypatch.setattr(_atspi, "peer_field_texts", dialog.collect)
    monkeypatch.setattr(_atspi, "changed_peer_fields", _REAL_CHANGED_PEER_FIELDS)
    return drv, dialog


_REAL_CHANGED_PEER_FIELDS = _atspi.changed_peer_fields
_REAL_ATSPI = type("Real", (), {"peer_field_texts": staticmethod(_atspi.peer_field_texts),
                                "changed_peer_fields": staticmethod(_atspi.changed_peer_fields)})


def test_color_dialog_spin_boxes_following_the_html_field_are_not_focus_lost(monkeypatch, field):
    drv, dialog = _color_dialog_driver(monkeypatch, field)
    field.chord("ctrl+a")
    field.keys.clear()
    assert drv.type_into(_element(), "#00ff00") == 7
    assert field.text == "#00ff00" and dialog.peers == {"Red": "0", "Green": "255", "Blue": "0"}
    assert len(drv._qt_dependent_changes) == 2     # Red and Green, recorded not refused


class _EchoMirror:
    """Tester's probe: Echo's textEdited copies its text into a sibling Mirror.

    Both are plain QLineEdits with no objectName, so Qt 6 gives them the
    same accessible-id; only the bus path tells them apart.
    """

    def __init__(self, f):
        self.echo, self.mirror = f, _LineEdit("mirror")
        real = f.type

        def type_and_mirror(text):
            real(text)
            self.mirror.text = f.text
        f.type = type_and_mirror


@pytest.mark.parametrize("setup,text,want", [
    ("whole", "peer", "peer"),
    ("caret", "Q", "ecQho"),
])
def test_echo_mirror_is_reported_as_a_dependent_change_not_refused(monkeypatch, field, setup, text, want):
    drv = _ref_type_driver(monkeypatch, field)
    probe = _EchoMirror(field)
    field.text = "echo"
    if setup == "whole":
        field.chord("ctrl+a")
    else:
        field.caret, field.sel = 2, (2, 2)
    root = object()
    kids = {root: [field, probe.mirror]}
    monkeypatch.setattr(_atspi, "_field_walk_root", lambda acc: root)
    monkeypatch.setattr(_atspi, "_child_count", lambda n: len(kids.get(n, [])))
    monkeypatch.setattr(_atspi, "_child_at", lambda n, i: kids[n][i])
    monkeypatch.setattr(_atspi, "_parent_of", lambda n: None if n is root else root)
    monkeypatch.setattr(_atspi, "_stable_id", lambda n, *a: "QWidget.QLineEdit")
    monkeypatch.setattr(_atspi, "_node_name", lambda n: "Mirror:" if n is probe.mirror else "Echo:")
    monkeypatch.setattr(_atspi, "_is_tracked_field", lambda n: n is not root)
    monkeypatch.setattr(_atspi, "_full_text", lambda n: n.text)
    for name in ("peer_field_texts", "changed_peer_fields"):
        monkeypatch.setattr(_atspi, name, getattr(_REAL_ATSPI, name))
    assert drv.type_into(_element(), text) == len(text)
    assert field.text == want and probe.mirror.text == want
    assert len(drv._qt_dependent_changes) == 1
    assert "Mirror:" in drv._qt_dependent_changes[0] and repr(want) in drv._qt_dependent_changes[0]


def test_same_qt_accessible_id_on_a_sibling_is_not_the_target(monkeypatch):
    target, sibling = object(), object()
    monkeypatch.setattr(_atspi, "_stable_id", lambda n, *a: "QWidget.QLineEdit")
    monkeypatch.setattr(_atspi, "_role_name", lambda n: "text")
    monkeypatch.setattr(_atspi, "_parent_of", lambda n: None)
    monkeypatch.setattr(_atspi, "_qt_app", lambda n: True)
    assert _atspi._under_target(sibling, target) is False
    assert _atspi._under_target(target, target) is True
    monkeypatch.setattr(_atspi, "_qt_app", lambda n: False)   # a web remount keeps its DOM id
    assert _atspi._under_target(sibling, target) is True


def test_color_dialog_rejected_value_is_still_refused(monkeypatch, field):
    drv, dialog = _color_dialog_driver(monkeypatch, field)
    field.accepts = _hex_validator
    field.chord("ctrl+a")
    with pytest.raises(ComputerUseError) as err:
        drv.type_into(_element(), "hello")
    assert err.value.detail["reason"] in {"text_mismatch", "focus_lost"}
    assert err.value.detail["restored"] is True and err.value.detail["original"] == "#ff0000"
    assert field.text == "#ff0000"


def test_peer_focus_lost_after_a_refused_set_value_keeps_the_restore_flags(monkeypatch):
    drv = object.__new__(linux.LinuxDriver)
    drv._run = lambda fn: fn()
    monkeypatch.setattr(_atspi, "changed_peer_fields", lambda acc, peers: ["Red: '255' -> '0'"])
    first = ComputerUseError(linux.ErrorCode.UNSUPPORTED, "mismatch",
                             detail={"reason": "text_mismatch", "input_sent": True,
                                     "original": "#ff0000", "restored": False,
                                     "keys_sent": ["ctrl+a", "#0"]})
    with pytest.raises(ComputerUseError) as err:
        drv._refuse_changed_peers(object(), {"x": "1"}, _element(), first)
    d = err.value.detail
    assert d["reason"] == "focus_lost" and d["restored"] is False and d["original"] == "#ff0000"
    assert "could not be restored" in str(err.value)


class _FakeText:
    def __init__(self, text, caret, sel):
        self.text, self.caret, self.sel = text, caret, sel

    def get_character_count(self, acc):
        return len(acc.text.encode("utf-16-le")) // 2

    def get_caret_offset(self, acc):
        return acc.caret

    def get_n_selections(self, acc):
        return 1 if acc.sel[0] != acc.sel[1] else 0

    def get_selection(self, acc, i):
        return acc.sel


@pytest.mark.parametrize("text,caret,sel,want", [
    ("a😀b", 4, (4, 4), ("a😀b", 3, 3, 3)),        # caret at the end in UTF-16 units
    ("a😀b", 1, (1, 3), ("a😀b", 1, 1, 2)),        # the emoji selected
    ("Startünï", 8, (8, 8), ("Startünï", 8, 8, 8)),
])
def test_qt_text_state_reads_utf16_offsets(monkeypatch, text, caret, sel, want):
    acc = _FakeText(text, caret, sel)
    fake = type("A", (), {"Text": acc})
    monkeypatch.setattr(_atspi, "_atspi", lambda: fake)
    monkeypatch.setattr(_atspi, "_full_text", lambda a: a.text)
    monkeypatch.setattr(_atspi, "_selection_bounds", lambda b: b)
    assert _atspi._qt_text_state(acc) == want


def test_qt_text_state_refuses_an_offset_inside_a_surrogate_pair(monkeypatch):
    acc = _FakeText("a😀b", 2, (2, 2))
    fake = type("A", (), {"Text": acc})
    monkeypatch.setattr(_atspi, "_atspi", lambda: fake)
    monkeypatch.setattr(_atspi, "_full_text", lambda a: a.text)
    assert _atspi._qt_text_state(acc) is None


def test_ref_type_into_an_unfocused_field_focuses_then_types_at_the_caret(monkeypatch, field):
    drv = _ref_type_driver(monkeypatch, field)
    focus = {"on": False}

    def grab(acc):
        focus["on"] = True          # Qt's setFocus keeps the caret, no select-all
        return True
    monkeypatch.setattr(_atspi, "grab_focus", grab)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda acc: focus["on"])
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda acc: focus["on"])
    field.text, field.caret, field.sel = "abc", 0, (0, 0)
    assert drv.type_into(_element(), "d") == 1
    assert focus["on"] and field.text == "dabc" and field.keys == ["d"]


def test_qt_edit_logs_one_phase_timing_line(monkeypatch, field, caplog):
    import logging
    drv = _driver(monkeypatch, field)
    with caplog.at_level(logging.DEBUG, logger="a11y_computer_use.qt_edit"):
        drv.set_value(_element(), "#00ff00")
    lines = [r.getMessage() for r in caplog.records if r.name == "a11y_computer_use.qt_edit"]
    assert len(lines) == 1
    for part in ("op=set_value", "focus=", "select=", "keys=", "readback=", "restore=", "total="):
        assert part in lines[0], lines[0]


class _Node:
    def __init__(self, role, name="", sid="", text=None, parent=None):
        self.role, self.name, self.sid, self.text = role, name, sid, text
        self.parent, self.kids = parent, []
        if parent is not None:
            parent.kids.append(self)


def _frameless_qt_tree(monkeypatch):
    """Tester's live probe: application > filler 'qcprobe' > Echo, Mirror (#1420)."""
    app = _Node("application", "qcprobe")
    filler = _Node("filler", "qcprobe", "QApplication.QWidget", parent=app)
    echo = _Node("text", "Echo:", "QApplication.QWidget.QLineEdit", "echo", filler)
    mirror = _Node("text", "Mirror:", "QApplication.QWidget.QLineEdit", "echo", filler)
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    monkeypatch.setattr(_atspi, "_parent_of", lambda n: n.parent)
    monkeypatch.setattr(_atspi, "_child_count", lambda n: len(n.kids))
    monkeypatch.setattr(_atspi, "_child_at", lambda n, i: n.kids[i])
    monkeypatch.setattr(_atspi, "_ancestor_key", lambda n: id(n))
    monkeypatch.setattr(_atspi, "_stable_id", lambda n, *a: n.sid or None)
    monkeypatch.setattr(_atspi, "_node_name", lambda n: n.name)
    monkeypatch.setattr(_atspi, "_full_text", lambda n: n.text)
    monkeypatch.setattr(_atspi, "_qt_app", lambda n: True)
    monkeypatch.setattr(_atspi, "_state_has", lambda n, s: n.role == "text" and s in {"EDITABLE", "FOCUSABLE"})
    monkeypatch.setattr(_atspi, "_number_input", lambda n: False)
    monkeypatch.setattr(_atspi, "_get_attributes", lambda n: {})
    monkeypatch.setattr(_atspi, "_is_tracked_field", _REAL_IS_TRACKED)
    for name in ("peer_field_texts", "changed_peer_fields"):
        monkeypatch.setattr(_atspi, name, getattr(_REAL_ATSPI, name))
    return app, filler, echo, mirror


_REAL_IS_TRACKED = _atspi._is_tracked_field


def test_frameless_qt_top_level_walk_root_is_the_filler(monkeypatch):
    app, filler, echo, mirror = _frameless_qt_tree(monkeypatch)
    assert _atspi._field_walk_root(echo) is filler
    # Same default Qt id, still distinct: only Mirror is a peer of Echo.
    assert _atspi.peer_field_texts(echo) == {"text|Mirror:|QApplication.QWidget.QLineEdit#0": "echo"}


def test_target_directly_under_application_has_no_walk_root(monkeypatch):
    app, filler, echo, mirror = _frameless_qt_tree(monkeypatch)
    lone = _Node("text", "Lone:", "x", "a", app)
    assert _atspi._field_walk_root(lone) is None       # never the target, never the app
    assert _atspi.peer_field_texts(lone) == {}


def test_frameless_echo_mirror_change_is_reported(monkeypatch, caplog):
    app, filler, echo, mirror = _frameless_qt_tree(monkeypatch)
    before = _atspi.peer_field_texts(echo)
    echo.text = mirror.text = "peer"
    changed = _atspi.changed_peer_fields(echo, before)
    assert changed == ["text|Mirror:|QApplication.QWidget.QLineEdit#0: 'echo' -> 'peer'"]
    mirror.name = "Renamed:"
    with caplog.at_level("DEBUG", logger="a11y_computer_use.peer_fields"):
        assert _atspi.changed_peer_fields(echo, before) == []
    assert "peer field missing after edit" in caplog.text


def test_empty_peer_snapshot_is_logged(monkeypatch, caplog):
    app, filler, echo, mirror = _frameless_qt_tree(monkeypatch)
    filler.kids.remove(mirror)
    with caplog.at_level("DEBUG", logger="a11y_computer_use.peer_fields"):
        assert _atspi.peer_field_texts(echo) == {}
    assert "peer field snapshot empty root=filler" in caplog.text
