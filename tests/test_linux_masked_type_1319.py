"""#1319: a refused Chrome type must never erase a value the field's own mask formatted.

Regression from #1388 (15db54e4): ``undo_chromium_type`` ran on every
``text_mismatch`` in Chrome, so ``5551234567`` read back as
``(555) 123-4567`` was removed again.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError


def _driver(monkeypatch, before, after):
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._type_expected = None
    undone = []
    monkeypatch.setattr(driver, "_run", lambda fn: fn(), raising=False)
    monkeypatch.setattr(_atspi, "writer_document_for", lambda h: None)
    monkeypatch.setattr(_atspi, "_readable_text", lambda h: before)
    monkeypatch.setattr(_atspi, "insert_text", lambda h, t: False)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_type_into_target", lambda h, t: None)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda h: True)
    monkeypatch.setattr(_atspi, "_poll_typed_text", lambda read, b, t, chrome=False: after)
    monkeypatch.setattr(_atspi, "css_transformed_type_confirms", lambda *a: None)
    monkeypatch.setattr(
        _atspi, "undo_chromium_type", lambda h, b, t: undone.append(t) or True
    )
    return driver, undone


@pytest.mark.parametrize(
    "before,text,after",
    [
        ("", "5551234567", "(555) 123-4567"),
        ("", "4111111111111111", "4111 1111 1111 1111"),
        ("", "1228", "12/28"),
        ("", "save10", "SAVE10"),
        ("", "SAVE10", "save10"),
        ("", "123456789", "12345-6789"),
        ("(555) 123", "4567", "(555) 123-4567"),
    ],
)
def test_masked_value_is_kept_and_confirmed(monkeypatch, before, text, after):
    driver, undone = _driver(monkeypatch, before, after)
    assert driver._type_confirmed_handle(object(), text) == len(text)
    assert undone == []
    assert driver._type_expected == after


@pytest.mark.parametrize(
    "before,text,after",
    [
        ("", "5551234567", ""),                 # nothing landed
        ("", "1228", "12/29"),                  # wrong expiry
        ("", "5551234567", "zzz"),              # JS set something else
        ("", "12345678", "12346"),              # not a prefix
        ("abc", "5551234567", "abc"),           # went elsewhere
        ("", "5551234567", "(555) 123-4568"),   # wrong digit
    ],
)
def test_wrong_value_is_still_refused_and_removed(monkeypatch, before, text, after):
    driver, undone = _driver(monkeypatch, before, after)
    with pytest.raises(ComputerUseError) as exc:
        driver._type_confirmed_handle(object(), text)
    assert exc.value.detail["reason"] == "text_mismatch"
    assert undone == [text]


@pytest.mark.parametrize(
    "text,after",
    [
        ("12345678", "12345"),                  # zip maxlength=5
        ("5551234567", "(555) 123-456"),
        ("save10", "SAVE1"),
        ("4111111111111111", "4111 1111 1111 111"),
    ],
)
def test_maxlength_truncation_is_kept_but_reported(monkeypatch, text, after):
    driver, undone = _driver(monkeypatch, "", after)
    with pytest.raises(ComputerUseError) as exc:
        driver._type_confirmed_handle(object(), text)
    detail = exc.value.detail
    assert detail["truncated"] is True and detail["shown"] == after
    assert detail["text_removed"] is False
    assert undone == []


def test_plain_field_unchanged(monkeypatch):
    driver, undone = _driver(monkeypatch, "", "hello")
    assert driver._type_confirmed_handle(object(), "hello") == 5
    assert undone == []
