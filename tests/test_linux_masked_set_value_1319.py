"""#1319 follow-up: set_value on a Chrome masked field must not end empty.

Chrome inputs have no EditableText, so set_value clears the field and types
through ``_replace_with_keys``. The mask formats what was typed, the exact
read-back missed, and the rollback typed the blank original back: every
masked field ended empty with ``outcome: partial`` and no error flag.
"""

from __future__ import annotations

import contextlib

import pytest

from a11y_computer_use.drivers import _atspi, _linux_ime
from a11y_computer_use.schema import ComputerUseError


class _Field:
    def __init__(self, shown: str):
        self.text = ""
        self.shown = shown
        self.typed = []
        self.dom_writes = []


def _wire(monkeypatch, field: _Field):
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: field.text)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: True)
    monkeypatch.setattr(_linux_ime, "ascii_passthrough", lambda s: contextlib.nullcontext())
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)

    def type_into(acc, text):
        field.typed.append(text)
        field.text = field.shown

    def dom_replace(acc, value):
        field.dom_writes.append(value)
        field.text = value
        return True

    monkeypatch.setattr(_atspi, "_type_into_target", type_into)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: field.text == text)
    monkeypatch.setattr(_atspi, "_hex_colour_confirmed", lambda acc, text: False)
    monkeypatch.setattr(_atspi, "css_transformed_value_confirms", lambda acc, r, v: False)
    monkeypatch.setattr(_atspi, "chromium_replace_dom_text", dom_replace)
    monkeypatch.setattr(_atspi, "chromium_dom_text", lambda acc: field.text)
    monkeypatch.setattr(_atspi.time, "sleep", lambda s: None)


@pytest.mark.parametrize(
    "text,shown",
    [
        ("5551234567", "(555) 123-4567"),
        ("4111111111111111", "4111 1111 1111 1111"),
        ("1228", "12/28"),
        ("save10", "SAVE10"),
    ],
)
def test_masked_set_value_is_kept_and_confirmed(monkeypatch, text, shown):
    field = _Field(shown)
    _wire(monkeypatch, field)
    result = _atspi._replace_with_keys(object(), text)
    assert result
    assert isinstance(result, _atspi._FieldFormattedWrite)
    assert result.css_rendered == shown
    assert field.text == shown
    assert field.dom_writes == []  # never rolled back


def test_maxlength_cut_is_kept_and_refused_as_truncated(monkeypatch):
    field = _Field("12345")
    _wire(monkeypatch, field)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "12345678")
    detail = exc.value.detail
    assert detail["truncated"] is True
    assert detail["shown"] == "12345"
    assert detail["input_sent"] is True
    assert field.text == "12345"
    assert field.dom_writes == []


@pytest.mark.parametrize("shown", ["zzz", "(555) 123-4568", "12/29", ""])
def test_wrong_value_fails_closed_never_partial_on_an_empty_field(monkeypatch, shown):
    field = _Field(shown)
    _wire(monkeypatch, field)
    text = "1228" if shown == "12/29" else "5551234567"
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), text)
    detail = exc.value.detail
    assert field.text == ""
    assert detail["reason"] == "text_mismatch"
    assert detail["outcome"] == "refused"
    assert detail["outcome"] != "partial"
    assert detail["input_sent"] is True
    assert detail["restored"] is True
    assert "type" in detail["next"]
    assert "type(ref)" in str(exc.value)


def test_plain_field_set_value_unchanged(monkeypatch):
    field = _Field("hello")
    _wire(monkeypatch, field)
    assert _atspi._replace_with_keys(object(), "hello") is True


def test_formatted_render_with_a_different_dom_value_is_not_kept(monkeypatch):
    field = _Field("(555) 123-4567")
    _wire(monkeypatch, field)
    monkeypatch.setattr(_atspi, "chromium_dom_text", lambda acc: "" if field.text == "" else "zzz")
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "5551234567")
    assert exc.value.detail["outcome"] != "confirmed"
    assert field.dom_writes == [""]


def test_field_that_forces_another_value_fails_closed_with_type_guidance(monkeypatch):
    """A page that rewrites any input to "zzz" and keeps it (#1445 tester)."""
    field = _Field("zzz")
    _wire(monkeypatch, field)

    def sticky(acc, value):
        field.dom_writes.append(value)
        field.text = "zzz"
        return True

    monkeypatch.setattr(_atspi, "chromium_replace_dom_text", sticky)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "hello")
    detail = exc.value.detail
    assert field.text == "zzz"
    assert detail["reason"] == "text_mismatch"
    assert detail["outcome"] == "refused"
    assert detail["input_sent"] is True
    assert detail["restored"] is False
    assert "type" in detail["next"]
    assert "type(ref)" in str(exc.value)
