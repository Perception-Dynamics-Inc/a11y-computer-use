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
GREEN = {"tag": "input", "id": "gValueContainer", "text-input-type": "text", "maxlength": "3"}


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
        self.dom: str | None = None  # the DOM value when it differs from the rendered text

    def dom_value(self):
        return self.text if self.dom is None else self.dom


def _keyed(monkeypatch, field, chords, attrs=HEX):
    _chrome_field(monkeypatch, attrs)
    # The DOM value as read through PRIMARY: on Chromium only it confirms a rollback.
    monkeypatch.setattr(_atspi, "chromium_dom_text", lambda acc: field.dom_value())
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


def test_green_channel_mismatch_rolls_back_with_restored_true(monkeypatch) -> None:
    # Tester's forced mismatch: Green holds 10, 77 is written and does not stick.
    chords: list[str] = []
    field = _Field("10", lambda t: "7" if t == "77" else t)
    _keyed(monkeypatch, field, chords, GREEN)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    detail = exc.value.detail
    assert detail["outcome"] == "partial" and detail["input_sent"] is True
    assert field.dom_value() == "10" and detail["restored"] is True
    assert chords == []


def test_green_channel_rollback_retries_a_restore_that_did_not_land(monkeypatch) -> None:
    # The first typed-back "10" is swallowed by the popup's parse; the DOM is
    # left at 77 until the rollback confirms and retries.
    chords: list[str] = []
    restores = []

    def accept(t):
        if t == "10":
            restores.append(t)
            return "77" if len(restores) == 1 else "10"
        return t

    field = _Field("10", accept)
    _keyed(monkeypatch, field, chords, GREEN)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)  # 77 forced to mismatch
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    assert len(restores) == 2
    assert field.dom_value() == "10" and exc.value.detail["restored"] is True


def test_green_channel_failed_rollback_reports_restored_false(monkeypatch) -> None:
    chords: list[str] = []
    field = _Field("10", lambda t: "77")  # the field keeps 77 whatever is typed back
    _keyed(monkeypatch, field, chords, GREEN)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    assert field.dom_value() == "77"
    assert exc.value.detail["restored"] is False
    assert "'77'" in exc.value.detail["evidence"]


def test_rendered_match_with_wrong_dom_reports_restored_false(monkeypatch) -> None:
    # The rendered read shows the original back, but the DOM value is not it
    # (as under CSS text-transform): restored must follow the DOM.
    chords: list[str] = []
    field = _Field("10", lambda t: t)
    _keyed(monkeypatch, field, chords, GREEN)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)
    real_type = _atspi._type_into_target

    def type_into(acc, text):
        real_type(acc, text)
        field.dom = "77"  # the page keeps 77 whatever the rendered text says

    monkeypatch.setattr(_atspi, "_type_into_target", type_into)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    assert field.text == "10" and field.dom_value() == "77"
    assert exc.value.detail["restored"] is False


def test_keyed_mismatch_without_a_rollback_result_checks_the_dom(monkeypatch) -> None:
    field = _Field("ABC", lambda t: t)
    field.dom = "xyz"
    _keyed(monkeypatch, field, [], GREEN)
    err = _atspi._keyed_write_mismatch(object(), "new", "ABC")
    assert err.detail["restored"] is False
    field.dom = "ABC"
    assert _atspi._keyed_write_mismatch(object(), "new", "ABC").detail["restored"] is True


def _popup_input(monkeypatch, attrs, shown):
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_get_attributes", lambda acc: dict(attrs))
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: shown)
    # The popup's select-all never reaches PRIMARY: the scoped read must not use it.
    monkeypatch.setattr(_atspi, "_primary_tool", lambda: pytest.fail("PRIMARY read in the popup"))
    monkeypatch.setattr(_atspi, "_chord_into_target", lambda *a: pytest.fail("chord in the popup read"))


def test_popup_channel_input_dom_value_is_read_from_the_input(monkeypatch) -> None:
    _popup_input(monkeypatch, GREEN, "10")
    assert _atspi.chromium_dom_text(object()) == "10"
    _popup_input(monkeypatch, SAT, "50%")
    assert _atspi.chromium_dom_text(object()) == "50%"


@pytest.mark.parametrize("attrs, shown", [(GREEN, None), (GREEN, "ab"), (HEX, "#00FFZZ"), (HEX, "\uff10\uff10"), (HEX, None)])
def test_popup_input_with_cased_or_unreadable_text_is_unknown(monkeypatch, attrs, shown) -> None:
    _popup_input(monkeypatch, attrs, shown)
    assert _atspi.chromium_dom_text(object()) is None


def test_popup_green_rollback_confirms_restored_through_the_scoped_read(monkeypatch) -> None:
    chords: list[str] = []
    field = _Field("10", lambda t: "7" if t == "77" else t)
    _keyed(monkeypatch, field, chords, GREEN)
    monkeypatch.setattr(_atspi, "chromium_dom_text", _REAL_DOM_TEXT)
    monkeypatch.setattr(_atspi, "_primary_tool", lambda: pytest.fail("PRIMARY read in the popup"))
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    assert field.text == "10" and exc.value.detail["restored"] is True
    assert field.typed == ["77", "10"]  # one rollback, no forced retry


def test_popup_rollback_that_did_not_land_reports_false(monkeypatch) -> None:
    field = _Field("10", lambda t: "77")
    _keyed(monkeypatch, field, [], GREEN)
    monkeypatch.setattr(_atspi, "chromium_dom_text", _REAL_DOM_TEXT)
    monkeypatch.setattr(_atspi, "_primary_tool", lambda: pytest.fail("PRIMARY read in the popup"))
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    assert exc.value.detail["restored"] is False


_REAL_DOM_TEXT = _atspi.chromium_dom_text


@pytest.mark.parametrize("shown", ["#00FF0A", "00ff0A", "00FF0A"])
def test_popup_hex_input_reads_as_canonical_lowercase(monkeypatch, shown) -> None:
    _popup_input(monkeypatch, HEX, shown)
    assert _atspi.chromium_dom_text(object()) == shown.lower()


def test_popup_hex_without_hash_rollback_reports_restored(monkeypatch) -> None:
    # The popup shows hex in upper case whatever was typed.
    field = _Field("00FF0A", lambda t: "12" if t == "12" else t.upper())
    _keyed(monkeypatch, field, [], HEX)
    monkeypatch.setattr(_atspi, "chromium_dom_text", _REAL_DOM_TEXT)
    monkeypatch.setattr(_atspi, "_primary_tool", lambda: pytest.fail("PRIMARY read in the popup"))
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)
    monkeypatch.setattr(_atspi, "_hex_colour_confirmed", lambda *a, **k: False)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "12")
    assert field.text == "00FF0A" and exc.value.detail["restored"] is True


def test_popup_hex_left_dirty_reports_false(monkeypatch) -> None:
    field = _Field("00FF0A", lambda t: "12")
    _keyed(monkeypatch, field, [], HEX)
    monkeypatch.setattr(_atspi, "chromium_dom_text", _REAL_DOM_TEXT)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)
    monkeypatch.setattr(_atspi, "_hex_colour_confirmed", lambda *a, **k: False)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "12")
    assert exc.value.detail["restored"] is False


def test_rollback_clears_even_when_the_field_reads_blank_and_never_appends(monkeypatch) -> None:
    """Live #1394: the write left "7", a read showed blank, and "10" was typed after it."""
    field = _Field("10", lambda t: "7" if t == "77" else t)
    _keyed(monkeypatch, field, [], GREEN)
    monkeypatch.setattr(_atspi, "chromium_dom_text", _REAL_DOM_TEXT)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)
    blank_once = {"n": 1}
    real_shown = lambda acc: field.text  # noqa: E731

    def shown(acc):
        if field.text == "7" and blank_once["n"]:
            blank_once["n"] -= 1  # the popup re-rendering after its commit
            return ""
        return real_shown(acc)

    monkeypatch.setattr(_atspi, "_full_text", shown)
    cleared: list[str] = []

    def clear(acc):
        cleared.append(field.text)
        field.text = ""

    monkeypatch.setattr(_atspi, "_x11_select_all_and_delete", clear)

    def type_into(acc, text):
        field.typed.append(text)
        field.text = field.text + text if text == "10" else field.accept(text)

    monkeypatch.setattr(_atspi, "_type_into_target", type_into)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    assert field.text == "10" and exc.value.detail["restored"] is True
    assert cleared[-1] == "7"


def test_rollback_types_nothing_when_the_field_refills_after_the_clear(monkeypatch) -> None:
    field = _Field("10", lambda t: "7" if t == "77" else t)
    _keyed(monkeypatch, field, [], GREEN)
    monkeypatch.setattr(_atspi, "chromium_dom_text", _REAL_DOM_TEXT)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: False)
    state = {"cleared": 0}

    def clear(acc):
        state["cleared"] += 1
        field.text = ""

    monkeypatch.setattr(_atspi, "_x11_select_all_and_delete", clear)
    monkeypatch.setattr(_atspi, "_wait_until_gone", lambda acc: True)
    # After the first clear the popup commits "7" back into the field.
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: "7" if state["cleared"] >= 2 and field.text == "" else field.text)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "77")
    assert field.typed == ["77"]  # no rollback text appended to a refilled field
    assert exc.value.detail["restored"] is False
