"""#1394: Chrome's <input type=color> popup channel fields.

A value the field cannot hold is refused before any focus, write or key.
A keyed write that does not stick is reported as ``partial`` with
``input_sent``, never as a clean refusal, and no commit key is sent.
"""
from __future__ import annotations

import contextlib

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _linux_ime, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.outcome import next_for_error, refused_result
from a11y_computer_use.schema import Bounds, ComputerUseError, Element

RED = {"tag": "input", "id": "rValueContainer", "text-input-type": "text", "maxlength": "3"}
HUE = {"tag": "input", "id": "hValueContainer", "maxlength": "3"}
SAT = {"tag": "input", "id": "sValueContainer", "maxlength": "4"}
HEX = {"tag": "input", "id": "hexValueContainer", "maxlength": "7"}


@pytest.mark.parametrize("attrs,value", [
    (RED, "#00ff00"), (RED, "256"), (RED, "-1"), (RED, "1.5"), (RED, ""), (RED, "0x1"),
    (HUE, "360"), (SAT, "101%"), (SAT, "50 %"),
    (HEX, "00ff00x"), (HEX, "#00ff0"), (HEX, "#00gg00"), (HEX, "#00ff00ff"),
    ({"tag": "input", "maxlength": "4"}, "hello"),
    ({"tag": "input", "maxlength": "2"}, "😀a"),  # UTF-16 units, as the DOM counts
])
def test_invalid_values_are_named(attrs, value) -> None:
    assert _atspi._invalid_value_reason(attrs, value)


@pytest.mark.parametrize("attrs,value", [
    (RED, "0"), (RED, "255"), (RED, "51"), (HUE, "359"), (SAT, "100%"), (SAT, "0"),
    (HEX, "#00ff00"), (HEX, "#00FF0A"), (HEX, "00ff00"),
    ({"tag": "input", "maxlength": "4"}, "four"),
    ({"tag": "input"}, "no limit at all"),
    ({"tag": "textarea", "id": "rValueContainer", "maxlength": "1"}, "not an input"),
])
def test_valid_values_pass(attrs, value) -> None:
    assert _atspi._invalid_value_reason(attrs, value) is None


def _chrome_field(monkeypatch, attrs, *, chromium=True):
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: chromium)
    monkeypatch.setattr(_atspi, "_get_attributes", lambda acc: dict(attrs))


def test_refusal_is_invalid_value_with_no_retry(monkeypatch) -> None:
    _chrome_field(monkeypatch, RED)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._refuse_invalid_field_value(object(), "#00ff00")
    detail = exc.value.detail
    assert detail["reason"] == "invalid_value"
    assert detail["outcome"] == "refused"
    assert "nothing was sent" in str(exc.value)
    assert next_for_error(exc.value.code.value, detail) == ()


def test_other_toolkits_are_not_checked(monkeypatch) -> None:
    _chrome_field(monkeypatch, RED, chromium=False)
    _atspi._refuse_invalid_field_value(object(), "#00ff00")


def _no_input(monkeypatch):
    def boom(*a, **k):
        pytest.fail("input reached the field")

    for mod, names in ((_linux_input, ("press_chord", "type_string", "click")),
                       (_atspi, ("grab_focus", "_type_into_target", "_x11_select_all_and_delete",
                                 "_set_text_body"))):
        for name in names:
            monkeypatch.setattr(mod, name, boom)


def test_set_text_refuses_before_any_key(monkeypatch) -> None:
    _chrome_field(monkeypatch, RED)
    _no_input(monkeypatch)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_text(object(), "#00ff00")
    assert exc.value.detail["reason"] == "invalid_value"


def test_driver_set_value_refuses_before_focus_raise_or_dom_write(monkeypatch) -> None:
    field = object()
    _chrome_field(monkeypatch, RED)
    _no_input(monkeypatch)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_a: field)
    monkeypatch.setattr(LinuxDriver, "_check_modal_target", lambda self, el: None)
    monkeypatch.setattr(LinuxDriver, "_refuse_chromium_occluded", lambda self, el, h: None)
    for name in ("_raise_handle_window", "_apply_set_value", "_visible_action"):
        monkeypatch.setattr(LinuxDriver, name, lambda *a, **k: pytest.fail(f"{name} ran"))
    monkeypatch.setattr(_atspi, "refuse_open_composition", lambda h: pytest.fail("ime probe ran"))
    el = Element("e38", "AXTextField", "Red channel", "51", Bounds(0, 122, 306, 53, 33),
                 "snap-1", editable=True, clickable=True)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().set_value(el, "#00ff00")
    assert exc.value.detail["reason"] == "invalid_value"


class _Field:
    """A Chrome channel input that, like the popup, rejects what it cannot parse."""

    def __init__(self, text, accept):
        self.text = text
        self.accept = accept
        self.typed: list[str] = []


def _keyed(monkeypatch, field, chords):
    _chrome_field(monkeypatch, HEX)
    monkeypatch.setattr(_linux_ime, "ascii_passthrough", lambda _t: contextlib.nullcontext())
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: field.text)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_wait_until_gone", lambda acc: field.text == "")
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)
    monkeypatch.setattr(_atspi, "_x11_select_all_and_delete", lambda acc: setattr(field, "text", ""))
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: field.text == text)

    def type_into(acc, text):
        field.typed.append(text)
        field.text = field.accept(text)

    monkeypatch.setattr(_atspi, "_type_into_target", type_into)
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: chords.append(chord))


def test_a_keyed_write_that_does_not_stick_is_partial_not_refused(monkeypatch) -> None:
    chords: list[str] = []
    field = _Field("#336699", lambda t: t if t == "#336699" else t[:3])
    _keyed(monkeypatch, field, chords)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "#00ff0a")
    detail = exc.value.detail
    assert detail["input_sent"] is True and detail["outcome"] == "partial"
    assert detail["restored"] is True and field.text == "#336699"
    assert field.typed == ["#00ff0a", "#336699"]
    assert "No commit key was sent" in detail["evidence"]
    assert not any(c.lower() in {"return", "enter", "kp_enter", "tab"} for c in chords)
    result = refused_result(text="x", code=exc.value.code.value, message=str(exc.value), detail=detail)
    assert result.outcome == "partial"


def test_a_failed_restore_is_reported(monkeypatch) -> None:
    chords: list[str] = []
    field = _Field("#336699", lambda t: "#000")
    _keyed(monkeypatch, field, chords)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "#00ff0a")
    assert exc.value.detail["restored"] is False
    assert "'#000'" in exc.value.detail["evidence"]
    assert chords == []


def test_hex_field_confirms_up_to_letter_case(monkeypatch) -> None:
    chords: list[str] = []
    field = _Field("#336699", lambda t: t.upper())  # the popup shows hex upper-cased
    _keyed(monkeypatch, field, chords)
    assert _atspi._replace_with_keys(object(), "#00ff0a") is True
    assert field.typed == ["#00ff0a"]


@pytest.mark.parametrize("attrs,shown", [
    (RED, "#00FF0A"),                       # not the hex field
    (HEX, "#00FF0B"),                       # a different colour
    ({"tag": "input", "id": "other"}, "#00FF0A"),
])
def test_case_insensitive_confirm_is_only_the_hex_colour(monkeypatch, attrs, shown) -> None:
    _chrome_field(monkeypatch, attrs)
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: shown)
    assert _atspi._hex_colour_confirmed(object(), "#00ff0a") is False


def test_non_chromium_keyed_failure_keeps_returning_false(monkeypatch) -> None:
    chords: list[str] = []
    field = _Field("old", lambda t: "x")
    _keyed(monkeypatch, field, chords)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: False)
    assert _atspi._replace_with_keys(object(), "new") is False
