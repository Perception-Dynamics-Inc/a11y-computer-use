"""Empty Chromium entries: no subnormal value, and a write that reads back.

Synthetic AT-SPI. Not a live browser. A filler still keeps the subnormal
double. A dialog's zero stays.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.schema import ComputerUseError, ErrorCode


_JUNK = 6.9305862814588e-310


class _States:
    def __init__(self, names: set[str]) -> None:
        self.names = names


class _ValueApi:
    @staticmethod
    def get_current_value(acc):
        return acc.value

    @staticmethod
    def get_minimum_value(acc):
        return getattr(acc, "minimum", 0.0)

    @staticmethod
    def get_maximum_value(acc):
        return getattr(acc, "maximum", 1.0)


class _FakeAtspi:
    class Text:
        @staticmethod
        def get_character_count(acc):
            if getattr(acc, "text_missing", False):
                raise RuntimeError("no text")
            if acc.text is None:
                return 0
            return len(acc.text)

        @staticmethod
        def get_text(acc, _start, _end):
            if getattr(acc, "text_missing", False):
                raise RuntimeError("no text")
            return acc.text

    Value = _ValueApi


class _Node:
    def __init__(self, role: str, *, value=None, text=None, text_missing: bool = False):
        self.role = role
        self.value = value
        self.text = text
        self.text_missing = text_missing
        self.minimum = 0.0
        self.maximum = 1.0

    def get_role_name(self):
        return self.role


@pytest.fixture
def fake_atspi(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _FakeAtspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)


def test_empty_entry_drops_the_subnormal_and_filler_keeps_it(fake_atspi) -> None:
    entry = _Node("entry", value=_JUNK, text=None)
    assert _atspi._value_text(entry, "AXTextField", "entry") is None
    filled = _Node("entry", value=_JUNK, text="Anchorage")
    assert _atspi._value_text(filled, "AXTextField", "entry") == "Anchorage"
    static = _Node("static", value=_JUNK, text_missing=True)
    assert _atspi._value_text(static, "AXStaticText", "static") is None
    filler = _Node("filler", value=_JUNK, text_missing=True)
    assert _atspi._value_text(filler, "AXGroup", "filler") == _JUNK
    dialog = _Node("dialog", value=0.0, text_missing=True)
    assert _atspi._value_text(dialog, "AXDialog", "dialog") == 0.0
    dialog.value = _JUNK
    assert _atspi._value_text(dialog, "AXDialog", "dialog") is None


class _NullEntry:
    """EditableText returns false. get_text(0, -1) is NULL until keys land."""

    def __init__(self) -> None:
        self.text = None
        self.writes: list[str] = []
        self.role = "entry"
        self.states: set[str] = set()
        self.focused_attempts = 0

    def get_role_name(self):
        return self.role

    def get_editable_text_iface(self):
        return self

    def set_text_contents(self, text: str) -> bool:
        self.writes.append(text)
        return False

    def get_state_set(self):
        return _States(set(self.states))

    def get_component_iface(self):
        return self

    def grab_focus(self) -> bool:
        self.focused_attempts += 1
        if getattr(self, "can_focus", False):
            self.states.add("FOCUSED")
            return True
        return False


def test_set_text_types_when_the_empty_read_is_null_and_confirms(fake_atspi, monkeypatch) -> None:
    field = _NullEntry()
    field.can_focus = True

    def type_string(text: str) -> None:
        field.text = (field.text or "") + text

    monkeypatch.setattr(_linux_input, "type_string", type_string)
    assert _atspi.set_text(field, "Anchorage") is True
    assert field.text == "Anchorage"
    assert field.writes == ["Anchorage"]


def test_set_text_does_not_succeed_when_the_typed_text_does_not_land(
    fake_atspi, monkeypatch
) -> None:
    field = _NullEntry()
    field.can_focus = True
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: None)
    assert _atspi.set_text(field, "Anchorage") is False
    assert field.text is None


def test_set_text_raises_when_the_empty_field_never_focuses(fake_atspi, monkeypatch) -> None:
    field = _NullEntry()
    typed: list[str] = []
    monkeypatch.setattr(_linux_input, "type_string", typed.append)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_text(field, "Anchorage")
    assert exc.value.code is ErrorCode.FOCUS_LOST
    assert typed == []
    assert field.text is None


def test_set_text_still_trusts_a_write_it_cannot_read_back(fake_atspi, monkeypatch) -> None:
    field = _NullEntry()

    def set_text_contents(text: str) -> bool:
        field.writes.append(text)
        field.text = text
        field.text_missing = True
        return True

    field.set_text_contents = set_text_contents
    field.text_missing = True
    monkeypatch.setattr(_linux_input, "type_string", lambda _text: (_ for _ in ()).throw(AssertionError("typed")))
    assert _atspi.set_text(field, "BETA") is True
    assert field.writes == ["BETA"]
