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


@pytest.fixture
def rebuilding(page, monkeypatch):
    """The page with a document root, parents, and a popup that rebuilds."""
    entry, listbox, option, move = page
    doc = Node("document web")
    for n, parent in ((entry, doc), (listbox, doc), (option, listbox)):
        n.parent = parent
    focused = []
    monkeypatch.setattr(_atspi, "_parent_of", lambda n: getattr(n, "parent", None))
    monkeypatch.setattr(_atspi, "_focused_accessibles", lambda root, limit=6: (
        list(focused) if root is doc else [n for n in focused if getattr(n, "owner", None) is root]))
    sleeps = []
    monkeypatch.setattr(_atspi.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(_atspi, "_refuse_chromium_tab_text", lambda *a: None)

    def focus(*nodes):
        entry.states.discard("FOCUSED")
        for n in (entry, listbox, option):
            n.states.discard("FOCUSED")
        focused[:] = list(nodes)
        for n in nodes:
            n.states.add("FOCUSED")
    return entry, listbox, option, doc, focus, sleeps


def _type_with(monkeypatch, entry, text, at, event):
    sent = []

    def key(ch):
        sent.append(ch)
        entry.text += ch
        if len(entry.text) == at:
            event()
    monkeypatch.setattr(_atspi, "_type_string", key)
    _atspi._type_into_target(entry, text)
    return "".join(sent)


def test_transient_focus_on_listbox_container_is_not_focus_loss(rebuilding, monkeypatch):
    entry, listbox, option, _doc, focus, _s = rebuilding
    assert _type_with(monkeypatch, entry, "Array.prototype.flat", 13,
                      lambda: focus(listbox)) == "Array.prototype.flat"


def test_unlinked_reRendered_option_then_linked_is_not_focus_loss(rebuilding, monkeypatch):
    """Chrome focuses a new option before the input's relation points at it."""
    entry, listbox, _option, doc, focus, sleeps = rebuilding
    fresh_list = Node("list box")
    fresh_list.parent = doc
    fresh = Node("list item")
    fresh.parent, fresh.owner = fresh_list, fresh_list
    reads = {"n": 0}
    real = _atspi._relation_targets

    def relations(acc, kind):
        reads["n"] += 1
        if focus_state["moved"] and reads["n"] > 8 and kind == "controller_for":
            return [fresh_list]
        return real(acc, kind)
    focus_state = {"moved": False}
    monkeypatch.setattr(_atspi, "_relation_targets", relations)

    def move():
        focus_state["moved"] = True
        reads["n"] = 0
        focus(fresh)
    assert _type_with(monkeypatch, entry, "Array.prototype.flat", 18, move) == "Array.prototype.flat"
    assert sleeps  # it re-read focus, it did not trust the first miss


def test_momentary_no_focus_then_input_focused_again_continues(rebuilding, monkeypatch):
    entry, _lb, _option, _doc, focus, sleeps = rebuilding
    calls = {"n": 0}
    real = _atspi._focus_on_target

    def flicker(acc):
        calls["n"] += 1
        return real(acc)
    monkeypatch.setattr(_atspi, "_focus_on_target", flicker)

    def gone():
        focus()  # nothing focused while the popup rebuilds
        monkeypatch.setattr(_atspi.time, "sleep",
                            lambda s: (sleeps.append(s), focus(), entry.states.add("FOCUSED")))
    assert _type_with(monkeypatch, entry, "Array", 2, gone) == "Array"
    assert sleeps == [_atspi._COMBOBOX_RECONFIRM_DELAY_S]


def test_focus_to_another_control_is_still_lost_without_polling(rebuilding, monkeypatch):
    entry, _lb, _option, doc, focus, sleeps = rebuilding
    button = Node("push button")
    button.parent = doc
    with pytest.raises(ComputerUseError) as exc:
        _type_with(monkeypatch, entry, "Array", 3, lambda: focus(button))
    assert "typed 3 of 5 characters" in str(exc.value)
    assert sleeps == []


def test_no_focus_for_the_whole_reconfirm_is_loss(rebuilding, monkeypatch):
    entry, _lb, _option, _doc, focus, sleeps = rebuilding
    with pytest.raises(ComputerUseError) as exc:
        _type_with(monkeypatch, entry, "Array", 3, lambda: focus())
    assert exc.value.detail["sent"] == 3
    assert len(sleeps) == _atspi._COMBOBOX_RECONFIRM_POLLS - 1
