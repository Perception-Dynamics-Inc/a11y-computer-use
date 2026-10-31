"""#1388: Chrome fields with CSS text-transform read back their painted case.

The DOM value is the only thing that accepts a write: the loose case shape
only decides whether the DOM is read, and the DOM has to hold exactly the
requested text. Refusals roll set_value back and take typed text back out.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi


@pytest.fixture
def chromium(monkeypatch):
    state = {"chromium": True, "dom": None, "reads": 0, "set": []}

    def dom(acc):
        state["reads"] += 1
        return state["dom"]() if callable(state["dom"]) else state["dom"]

    def replace(acc, target):
        state["set"].append(target)
        return True

    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: state["chromium"])
    monkeypatch.setattr(_atspi, "chromium_dom_text", dom)
    monkeypatch.setattr(_atspi, "chromium_replace_dom_text", replace)
    return state


@pytest.mark.parametrize(
    "rendered,value",
    [
        ("MIXED CASE", "Mixed Case"),
        ("mixed case", "Mixed Case"),
        ("Hello World", "hello world"),
        ("STRASSE", "straße"),  # uppercase is not one-to-one
        ("İSTANBUL", "istanbul"),  # lang=tr uppercase
        ("ISTANBUL", "ıstanbul"),
        ("ǅemal", "ǆemal"),  # capitalize of a digraph
        ("FILE", "ﬁle"),  # uppercase of a ligature
    ],
)
def test_case_shape_triggers_dom_read(rendered, value):
    assert _atspi.css_case_rendering(rendered, value)


@pytest.mark.parametrize(
    "rendered,value",
    [
        ("Mixed Case", "Mixed Case"),  # nothing to explain
        ("MIXED CASES", "Mixed Case"),
        ("MIXED", "Mixed Case"),
        ("STRASE", "straße"),
        (None, "x"),
    ],
)
def test_real_differences_never_trigger(rendered, value):
    assert not _atspi.css_case_rendering(rendered, value)


@pytest.mark.parametrize(
    "rendered,value",
    [("STRASSE", "straße"), ("İSTANBUL", "istanbul"), ("ǅemal", "ǆemal"), ("FILE", "ﬁle"), ("MIXED CASE", "Mixed Case")],
)
def test_value_accepted_only_on_exact_dom(chromium, rendered, value):
    chromium["dom"] = value
    assert _atspi.css_transformed_value_confirms(object(), rendered, value)
    chromium["dom"] = rendered  # a script re-cased the value itself
    assert not _atspi.css_transformed_value_confirms(object(), rendered, value)
    chromium["dom"] = value.upper()
    assert value.upper() == rendered or not _atspi.css_transformed_value_confirms(object(), rendered, value)
    chromium["dom"] = None
    assert not _atspi.css_transformed_value_confirms(object(), rendered, value)


def test_value_never_reads_dom_for_a_real_mismatch_or_off_chromium(chromium):
    chromium["dom"] = "Mixed Case"
    assert not _atspi.css_transformed_value_confirms(object(), "Other", "Mixed Case")
    chromium["chromium"] = False
    assert not _atspi.css_transformed_value_confirms(object(), "MIXED CASE", "Mixed Case")
    assert chromium["reads"] == 0


@pytest.mark.parametrize(
    "after,text", [("STRASSE", "straße"), ("İSTANBUL", "istanbul"), ("ǅemal", "ǆemal"), ("FILE", "ﬁle")]
)
def test_type_accepts_exact_dom_only(chromium, after, text):
    chromium["dom"] = text
    assert _atspi.css_transformed_type_confirms(object(), "", after, text) == text
    chromium["dom"] = after
    assert _atspi.css_transformed_type_confirms(object(), "", after, text) is None


def test_type_append_and_doubling(chromium):
    chromium["dom"] = "straße again"
    assert _atspi.css_transformed_type_confirms(object(), "STRASSE", "STRASSE AGAIN", " again") == "straße again"
    chromium["dom"] = "hellohello"
    assert _atspi.css_transformed_type_confirms(object(), "", "HELLOHELLO", "hello") is None


def test_type_never_reads_dom_off_chromium(chromium):
    chromium["chromium"] = False
    chromium["dom"] = "hello"
    assert _atspi.css_transformed_type_confirms(object(), "", "HELLO", "hello") is None
    assert chromium["reads"] == 0


def test_undo_type_removes_the_typed_text(chromium):
    chromium["dom"] = "abcHELLO"  # JS upper-cased only what was typed: not verbatim
    assert not _atspi.undo_chromium_type(object(), "ABC", "hello")  # prior unknown: partial
    assert chromium["set"] == []
    chromium["dom"] = "HELLO"
    assert _atspi.undo_chromium_type(object(), "", "hello")
    assert chromium["set"] == [""]
    chromium["dom"] = "abc hello"
    assert _atspi.undo_chromium_type(object(), "ABC", " hello")
    assert chromium["set"][-1] == "abc"


@pytest.fixture
def keys(monkeypatch, chromium):
    log = []
    field = {"text": "ABC"}
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: field["text"])
    monkeypatch.setattr(_atspi, "_x11_select_all_and_delete", lambda acc: log.append("clear"))
    monkeypatch.setattr(_atspi, "_wait_until_gone", lambda acc: True)
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)
    monkeypatch.setattr(_atspi, "_type_into_target", lambda acc, t: log.append(("type", t)))
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, t: False)
    monkeypatch.setattr(_atspi, "_restore_text", lambda acc, o: log.append(("restore_rendered", o)))
    return log, field


def test_set_value_refusal_rolls_back_to_dom_original(chromium, keys):
    log, field = keys
    chromium["dom"] = "abc"
    field["text"] = "ABC"
    # The write lands re-cased by script: DOM never equals the request.
    assert _atspi._replace_with_keys(object(), "straße") is False
    assert chromium["set"] == ["abc"]
    assert ("restore_rendered", "ABC") not in log


def test_set_value_exception_still_rolls_back(chromium, keys, monkeypatch):
    log, field = keys
    chromium["dom"] = "abc"

    def boom(acc, t):
        raise _atspi.ComputerUseError(_atspi.ErrorCode.UNSUPPORTED, "x", detail={})

    monkeypatch.setattr(_atspi, "_confirm_text", boom)
    with pytest.raises(_atspi.ComputerUseError):
        _atspi._replace_with_keys(object(), "straße")
    assert chromium["set"] == ["abc"]


def test_set_value_accepts_exact_dom(chromium, keys):
    log, field = keys
    field["text"] = "ABC"
    reads = iter(["abc", "straße"])
    chromium["dom"] = lambda: next(reads)

    def typed(acc, t):
        log.append(("type", t))
        field["text"] = "STRASSE"

    import a11y_computer_use.drivers._atspi as m

    m._type_into_target = typed  # restored by monkeypatch teardown of the fixture
    done = _atspi._replace_with_keys(object(), "straße")
    assert done and done.css_rendered == "STRASSE"
    assert chromium["set"] == []
