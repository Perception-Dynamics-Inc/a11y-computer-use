"""#1388: Chrome fields with CSS text-transform read back their painted case.

The DOM value is the source of truth there. These tests pin that the
case-insensitive acceptance applies only when the read-back is exactly a CSS
case transform, the field is Chromium, and the DOM value matches exactly.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi


@pytest.fixture
def chromium(monkeypatch):
    state = {"chromium": True, "dom": None, "reads": 0}

    def dom(acc):
        state["reads"] += 1
        return state["dom"]

    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: state["chromium"])
    monkeypatch.setattr(_atspi, "chromium_dom_text", dom)
    return state


def test_case_variants_cover_css_transforms():
    assert _atspi.css_case_variants("hello World") == ("HELLO WORLD", "hello world", "Hello World")


@pytest.mark.parametrize(
    "rendered,value,ok",
    [
        ("MIXED CASE", "Mixed Case", True),
        ("mixed case", "Mixed Case", True),
        ("Hello World", "hello world", True),
        ("Mixed Case", "Mixed Case", False),  # nothing to explain
        ("MIXED CASES", "Mixed Case", False),  # extra character
        ("MIXED", "Mixed Case", False),  # truncated
        ("MiXED CASE", "Mixed Case", False),  # not a CSS transform
        (None, "x", False),
    ],
)
def test_css_case_rendering_is_exact(rendered, value, ok):
    assert _atspi.css_case_rendering(rendered, value) is ok


def test_value_confirms_needs_exact_dom(chromium):
    chromium["dom"] = "Mixed Case"
    assert _atspi.css_transformed_value_confirms(object(), "MIXED CASE", "Mixed Case")
    chromium["dom"] = "MIXED CASE"  # a script upper-cased the value itself
    assert not _atspi.css_transformed_value_confirms(object(), "MIXED CASE", "Mixed Case")
    chromium["dom"] = None  # unreadable DOM stays unverified
    assert not _atspi.css_transformed_value_confirms(object(), "MIXED CASE", "Mixed Case")


def test_value_confirms_never_reads_dom_for_a_real_mismatch(chromium):
    chromium["dom"] = "Mixed Case"
    assert not _atspi.css_transformed_value_confirms(object(), "MIXED CAS", "Mixed Case")
    assert not _atspi.css_transformed_value_confirms(object(), "Other", "Mixed Case")
    assert chromium["reads"] == 0


def test_value_confirms_only_on_chromium(chromium):
    chromium["chromium"] = False
    chromium["dom"] = "Mixed Case"
    assert not _atspi.css_transformed_value_confirms(object(), "MIXED CASE", "Mixed Case")
    assert chromium["reads"] == 0


def test_type_confirms_empty_and_append(chromium):
    chromium["dom"] = "hello world"
    assert _atspi.css_transformed_type_confirms(object(), "", "HELLO WORLD", "hello world") == "hello world"
    chromium["dom"] = "hello world again"
    assert (
        _atspi.css_transformed_type_confirms(object(), "HELLO WORLD", "HELLO WORLD AGAIN", " again")
        == "hello world again"
    )


def test_type_confirms_refuses_script_uppercase_and_doubling(chromium):
    chromium["dom"] = "HELLO"  # JS upper-cased the DOM: the text did not land as typed
    assert _atspi.css_transformed_type_confirms(object(), "", "HELLO", "hello") is None
    chromium["dom"] = "hellohello"
    assert _atspi.css_transformed_type_confirms(object(), "", "HELLOHELLO", "hello") is None
    assert _atspi.css_transformed_type_confirms(object(), "", "HELP", "hello") is None


def test_type_confirms_only_on_chromium(chromium):
    chromium["chromium"] = False
    chromium["dom"] = "hello"
    assert _atspi.css_transformed_type_confirms(object(), "", "HELLO", "hello") is None
    assert chromium["reads"] == 0


def test_css_write_marker_survives_set_text_and_carries_rendering():
    done = _atspi._CssTransformedWrite.of("MIXED CASE")
    assert done and done.css_rendered == "MIXED CASE"
