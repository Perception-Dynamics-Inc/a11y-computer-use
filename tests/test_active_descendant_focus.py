"""aria-activedescendant focus is not focus loss; partial type is honest (#1430)."""

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import ComputerUseError, ErrorCode


class Node:
    def __init__(self, role, states=(), text=""):
        self.role, self.states, self.text = role, set(states), text
        self.relations = {}

    def get_state_set(self):
        return self.states


@pytest.fixture
def page(monkeypatch):
    entry = Node("entry", {"EDITABLE", "FOCUSABLE", "FOCUSED"})
    listbox = Node("list box")
    option = Node("list item", {"SELECTED"})
    entry.relations["controller_for"] = [listbox]
    focused = []
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, st: st in acc.states)
    monkeypatch.setattr(_atspi, "_descendant_focused", lambda acc: False)
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    monkeypatch.setattr(_atspi, "_ancestor_key", lambda n: id(n))
    monkeypatch.setattr(_atspi, "_relation_targets",
                        lambda acc, kind: list(getattr(acc, "relations", {}).get(kind, [])))
    monkeypatch.setattr(_atspi, "_focused_accessibles",
                        lambda root, limit=6: [n for n in focused if n.owner is root])
    monkeypatch.setattr(_atspi, "_readable_text", lambda acc: acc.text)
    option.owner = listbox

    def move_focus_to(node):
        entry.states.discard("FOCUSED")
        node.states.add("FOCUSED")
        focused.append(node)
    return entry, listbox, option, move_focus_to


def test_focus_on_controlled_active_option_counts_as_target_focus(page):
    entry, _lb, option, move = page
    move(option)
    assert _atspi._focus_on_target(entry) is True


def test_focus_on_unrelated_option_is_focus_loss(page):
    entry, _lb, _option, move = page
    other_list = Node("list box")
    stray = Node("list item")
    stray.owner = other_list
    move(stray)
    assert _atspi._focus_on_target(entry) is False


def test_focused_popup_itself_or_non_option_is_focus_loss(page):
    entry, listbox, _option, move = page
    button = Node("push button")
    button.owner = listbox
    move(button)
    assert _atspi._focus_on_target(entry) is False


def test_non_editable_or_non_chromium_target_keeps_strict_check(page, monkeypatch):
    entry, _lb, option, move = page
    move(option)
    entry.states.discard("EDITABLE")
    assert _atspi._focus_on_target(entry) is False
    entry.states.add("EDITABLE")
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: False)
    assert _atspi._focus_on_target(entry) is False


def test_typing_continues_when_suggestions_take_active_descendant(page, monkeypatch):
    entry, _lb, option, move = page
    sent = []

    def key(ch):
        sent.append(ch)
        entry.text += ch
        if len(entry.text) == 2:
            move(option)
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *a: None)
    monkeypatch.setattr(_atspi, "_type_string", key)
    _atspi._type_into_target(entry, "Array.prototype.flat")
    assert "".join(sent) == "Array.prototype.flat"


def test_real_focus_loss_mid_type_reports_partial_with_value(page, monkeypatch):
    entry, _lb, _option, move = page
    elsewhere = Node("entry", {"EDITABLE"})
    elsewhere.owner = None

    def key(ch):
        entry.text += ch
        if len(entry.text) == 3:
            move(elsewhere)
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *a: None)
    monkeypatch.setattr(_atspi, "_type_string", key)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._type_into_target(entry, "Array")
    err = exc.value
    assert err.code is ErrorCode.FOCUS_LOST
    assert "nothing was typed" not in str(err)
    assert "typed 3 of 5 characters" in str(err) and "'Arr'" in str(err)
    assert err.detail["sent"] == 3 and err.detail["value"] == "Arr"
    assert err.detail["reason"] == "focus_lost" and err.detail["partial"] is True
    assert err.detail["next"] == []  # a plain retry would double the prefix


def test_no_key_sent_when_focus_is_already_gone(page, monkeypatch):
    entry, _lb, _option, move = page
    elsewhere = Node("entry")
    elsewhere.owner = None
    move(elsewhere)
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *a: None)
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda acc: True)
    monkeypatch.setattr(_atspi, "_type_string", lambda ch: pytest.fail("sent"))
    with pytest.raises(ComputerUseError) as exc:
        _atspi._type_into_target(entry, "A")
    assert "nothing was typed" in str(exc.value)
