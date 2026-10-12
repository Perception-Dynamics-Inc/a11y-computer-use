"""Synthetic AT-SPI. An empty GTK text field has no Value interface.

CurrentValue is an uninitialized double (about 6.93e-310). The snapshot
value is absent. A character count of 0 is an empty value, so Value is
not read. When the text interface does not report a count, a missing
Value interface is not read either. A present interface drops the double
and keeps 0 and 1e-6. A filler keeps the double. Not a live gnome-calculator.
"""

from __future__ import annotations

import a11y_computer_use.drivers._atspi as _atspi


_JUNK = 6.93570962000123e-310


class _Api:
    calls = 0

    class Text:
        @staticmethod
        def get_character_count(acc):
            return len(acc.text)

        @staticmethod
        def get_text(acc, _start, _end):
            return acc.text

    class Value:
        @staticmethod
        def get_current_value(acc):
            _Api.calls += 1
            return acc.value


class _Node:
    def __init__(self, role, text, value, iface="missing"):
        self.role = role
        self.text = text
        self.value = value
        self._iface = iface

    def get_role_name(self):
        return self.role

    def get_value_iface(self):
        if self._iface == "missing":
            raise AssertionError("get_value_iface is not this node's method")
        return self._iface


def test_empty_gtk_text_does_not_read_a_missing_value_interface(monkeypatch) -> None:
    """No Value interface: CurrentValue is not called, and the value is absent."""
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    _Api.calls = 0
    field = _Node("text", "", _JUNK, iface=None)
    assert _atspi._value_interface_present(field) is False
    assert _atspi._value_text(field, "AXTextArea", "text") is None
    assert _Api.calls == 0
    editor = _Node("text", "", _JUNK, iface=None)
    assert _atspi._value_text(editor, "AXTextField", "entry") is None
    assert _Api.calls == 0
    shown = _Node("text", "77", _JUNK, iface=None)
    assert _atspi._value_text(shown, "AXTextArea", "text") == "77"
    assert _Api.calls == 0


def test_text_role_drops_a_subnormal_and_keeps_a_real_number(monkeypatch) -> None:
    """A character count of 0 is an empty value, so Value is not read.

    When the text interface does not report a count, a present Value
    interface is read. The uninitialized double is absent. 0 and 1e-6
    stay. A filler keeps the double. A test double with no
    ``get_value_iface`` still reads Value.
    """
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    _Api.calls = 0
    iface = object()
    empty = _Node("text", "", _JUNK, iface=iface)
    assert _atspi._value_interface_present(empty) is True
    assert _atspi._uninitialized_double(_JUNK) is True
    assert _atspi._value_text(empty, "AXTextArea", "text") is None
    zero = _Node("text", "", 0.0, iface=iface)
    assert _atspi._value_text(zero, "AXTextArea", "text") is None
    small = _Node("entry", "", 0.001, iface=iface)
    assert _atspi._value_text(small, "AXTextField", "entry") is None
    paragraph = _Node("paragraph", "", _JUNK, iface=iface)
    assert _atspi._value_text(paragraph, "AXStaticText", "paragraph") is None
    assert _Api.calls == 0

    class _Bare:
        def __init__(self, role, value):
            self.role = role
            self.value = value

        def get_role_name(self):
            return self.role

    bare = _Bare("text", _JUNK)
    assert _atspi._value_interface_present(bare) is True
    assert _atspi._value_text(bare, "AXTextArea", "text") is None
    assert _Api.calls == 1
    filler = _Bare("filler", _JUNK)
    assert _atspi._value_text(filler, "AXGroup", "filler") == _JUNK
    kept = _Bare("text", 1e-6)
    assert _atspi._value_text(kept, "AXTextArea", "text") == 1e-6
    zero_bare = _Bare("text", 0.0)
    assert _atspi._value_text(zero_bare, "AXTextArea", "text") == 0.0
    small_bare = _Bare("entry", 0.001)
    assert _atspi._value_text(small_bare, "AXTextField", "entry") == 0.001


def test_missing_value_interface_is_not_read_when_the_text_count_fails(monkeypatch) -> None:
    """No Value interface and no character count: CurrentValue is not called."""

    class _ApiNoCount(_Api):
        class Text:
            @staticmethod
            def get_character_count(_acc):
                raise RuntimeError("no text")

            @staticmethod
            def get_text(_acc, _start, _end):
                raise RuntimeError("no text")

    monkeypatch.setattr(_atspi, "_atspi", lambda: _ApiNoCount)
    _Api.calls = 0
    field = _Node("text", "", _JUNK, iface=None)
    assert _atspi._value_interface_present(field) is False
    assert _atspi._value_text(field, "AXTextArea", "text") is None
    assert _Api.calls == 0
    present = _Node("text", "", _JUNK, iface=object())
    assert _atspi._value_text(present, "AXTextArea", "text") is None
    assert _Api.calls == 1
    zero = _Node("entry", "", 0.0, iface=object())
    assert _atspi._value_text(zero, "AXTextField", "entry") == 0.0
