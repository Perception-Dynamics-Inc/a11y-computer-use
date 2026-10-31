"""#1425: a Chrome ref type after the caller's ctrl+a replaces the selection.

The pointer press type_into used to send first collapsed that selection, so
"QQ" + ctrl+a + type(ref, "hello") read "QQhello" and still reported success.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import ComputerUseError
from tests.test_linux_focus_safety import focus_driver  # noqa: F401


class _Textarea:
    def __init__(self, text, sel):
        self.text, self.sel = text, sel

    def insert(self, text):
        s, e = self.sel
        self.text = self.text[:s] + text + self.text[e:]
        self.sel = (s + len(text),) * 2


def _setup(driver, monkeypatch, area, *, focused=True):
    events = []
    for name in ("_check_modal_target", "_refuse_hidden", "_raise_handle_window",
                 "_visible_action", "_refuse_input_off_target"):
        monkeypatch.setattr(driver, name, lambda *a, **k: None)
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda h: False)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda h: True)
    monkeypatch.setattr(_atspi, "web_entry_context", lambda h: None)
    monkeypatch.setattr(_atspi, "peer_field_texts", lambda h: {})
    monkeypatch.setattr(_atspi, "append_target_entry", lambda h: False)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda h: focused)
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda h: True)
    monkeypatch.setattr(_atspi, "_full_text", lambda h: area.text)
    monkeypatch.setattr(_atspi, "entry_text_and_selection",
                        lambda h: (area.text, *area.sel) if area.sel[0] != area.sel[1] else None)
    monkeypatch.setattr(_atspi, "entry_text_and_caret", lambda h: (area.text, area.sel[0]))
    monkeypatch.setattr(_atspi.time, "sleep", lambda s: None)

    def click(target):
        events.append("pointer")
        area.sel = (len(area.text),) * 2          # a press collapses the selection
    monkeypatch.setattr(driver, "click", click)
    monkeypatch.setattr(driver, "_type_confirmed_handle",
                        lambda h, text: events.append("type") or area.insert(text) or len(text))
    return events


def test_ref_type_after_ctrl_a_replaces_the_whole_text(focus_driver, monkeypatch):
    driver, _, field, _, _, _ = focus_driver
    area = _Textarea("QQ", (0, 2))
    events = _setup(driver, monkeypatch, area)
    assert driver.type_into(field, "hello") == 5
    assert area.text == "hello"
    assert events == ["type"], "no pointer press over the caller's selection"


def test_ref_type_replaces_a_partial_selection(focus_driver, monkeypatch):
    driver, _, field, _, _, _ = focus_driver
    area = _Textarea("abcdef", (2, 4))
    _setup(driver, monkeypatch, area)
    assert driver.type_into(field, "XY") == 2
    assert area.text == "abXYef"


def test_append_instead_of_replace_is_refused_not_success(focus_driver, monkeypatch):
    driver, _, field, _, _, _ = focus_driver
    area = _Textarea("QQ", (0, 2))
    _setup(driver, monkeypatch, area)

    def collapses_then_inserts(h, text):          # the page dropped the selection
        area.sel = (2, 2)
        area.insert(text)
        return len(text)
    monkeypatch.setattr(driver, "_type_confirmed_handle", collapses_then_inserts)
    with pytest.raises(ComputerUseError) as err:
        driver.type_into(field, "hello")
    d = err.value.detail
    assert d["reason"] == "selection_not_replaced"
    assert d["expected"] == "hello" and d["actual"] == "QQhello"
    assert d["input_sent"] is True


def test_selection_lost_before_the_keys_types_nothing(focus_driver, monkeypatch):
    driver, _, field, _, _, _ = focus_driver
    area = _Textarea("QQ", (0, 2))
    events = _setup(driver, monkeypatch, area)
    reads = iter([("QQ", 0, 2), None])
    monkeypatch.setattr(_atspi, "entry_text_and_selection", lambda h: next(reads))
    with pytest.raises(ComputerUseError) as err:
        driver.type_into(field, "hello")
    assert err.value.detail["reason"] == "selection_lost"
    assert err.value.detail["input_sent"] is False
    assert "type" not in events and area.text == "QQ"


def test_unfocused_field_still_gets_the_pointer_press(focus_driver, monkeypatch):
    driver, _, field, _, _, _ = focus_driver
    area = _Textarea("QQ", (2, 2))
    events = _setup(driver, monkeypatch, area, focused=False)
    assert driver.type_into(field, "hello") == 5
    assert events == ["pointer", "type"] and area.text == "QQhello"


def test_entry_text_and_selection_reads_only_a_real_selection(monkeypatch):
    class Text:
        sel = (0, 2)

        def get_character_count(self, acc):
            return 2

        def get_caret_offset(self, acc):
            return 2

        def get_n_selections(self, acc):
            return 1 if self.sel[0] != self.sel[1] else 0

        def get_selection(self, acc, i):
            return self.sel
    text = Text()
    monkeypatch.setattr(_atspi, "_atspi", lambda: type("A", (), {"Text": text}))
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: "QQ")
    monkeypatch.setattr(_atspi, "_selection_bounds", lambda b: b)
    assert _atspi.entry_text_and_selection(object()) == ("QQ", 0, 2)
    text.sel = (2, 2)
    assert _atspi.entry_text_and_selection(object()) is None
