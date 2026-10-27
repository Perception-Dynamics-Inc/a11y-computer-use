"""#1219: typing into Chrome with an IBus engine that transforms ASCII.

Hermetic. A Python stand-in for the ``ibus`` CLI replaces the real one, so no
test can change a developer's engine. ``tests/conftest.py`` turns the bypass off
for every other test; these tests turn it back on with ``A11Y_IME_BYPASS=1``.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from a11y_computer_use import outcome
from a11y_computer_use.drivers import _atspi, _linux_ime, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode

@pytest.fixture
def ibus(monkeypatch):
    """Model the ibus CLI in Python, so no test can change a developer's engine.

    No shell script or executable bit: the same fixture has to work on Windows
    and macOS, where the suite also runs these tests.
    """
    state = {"engine": ""}
    switches: list[str] = []

    def run(args, *, capture_output, text, timeout, check):
        assert args[:2] == ["fake-ibus", "engine"]
        if len(args) == 3:
            switches.append(f"set {args[2]}")
            if os.environ.get("FAKE_IBUS_REFUSE"):
                return subprocess.CompletedProcess(args, 1, "Set global engine failed.\n", "")
            state["engine"] = args[2]
            return subprocess.CompletedProcess(args, 0, "", "")
        engine = state["engine"]
        if not engine:
            return subprocess.CompletedProcess(args, 1, "No engine is set.\n", "")
        return subprocess.CompletedProcess(args, 0, engine + "\n", "")

    monkeypatch.setattr(_linux_ime, "_ibus_binary", lambda: "fake-ibus")
    monkeypatch.setattr(_linux_ime.subprocess, "run", run)
    monkeypatch.setenv("A11Y_IME_BYPASS", "1")
    monkeypatch.delenv("FAKE_IBUS_REFUSE", raising=False)

    class Fake:
        def set(self, name: str) -> None:
            state["engine"] = name

        @property
        def engine(self) -> str:
            return state["engine"]

        @property
        def switches(self) -> list[str]:
            return switches

    return Fake()


@pytest.mark.parametrize("engine", ["libpinyin", "m17n:ru:translit"])
def test_transforming_engine_is_detected(ibus, engine) -> None:
    ibus.set(engine)
    ime = _linux_ime.active_ime()
    assert ime is not None and ime.engine == engine and ime.framework == "ibus"


def test_plain_layouts_and_missing_ibus_are_not_an_ime(ibus, monkeypatch) -> None:
    assert _linux_ime.active_ime() is None  # "No engine is set."
    ibus.set("xkb:us::eng")
    assert _linux_ime.active_ime() is None
    ibus.set("libpinyin")
    monkeypatch.setenv("A11Y_IME_BYPASS", "0")
    assert _linux_ime.active_ime() is None
    monkeypatch.setenv("A11Y_IME_BYPASS", "1")
    monkeypatch.setattr(_linux_ime, "_ibus_binary", lambda: None)
    assert _linux_ime.active_ime() is None


@pytest.mark.parametrize("engine", ["libpinyin", "m17n:ru:translit"])
def test_engine_is_pass_through_while_typing_and_restored_after(ibus, engine) -> None:
    ibus.set(engine)
    with _linux_ime.ascii_passthrough():
        assert ibus.engine == _linux_ime.PASSTHROUGH_ENGINE
    assert ibus.engine == engine


def test_restored_when_the_typing_raises(ibus) -> None:
    ibus.set("libpinyin")
    with pytest.raises(RuntimeError):
        with _linux_ime.ascii_passthrough():
            raise RuntimeError("boom")
    assert ibus.engine == "libpinyin"


def test_nested_use_switches_once(ibus) -> None:
    ibus.set("libpinyin")
    with _linux_ime.ascii_passthrough():
        with _linux_ime.ascii_passthrough():
            assert ibus.engine == _linux_ime.PASSTHROUGH_ENGINE
        assert ibus.engine == _linux_ime.PASSTHROUGH_ENGINE
    assert ibus.engine == "libpinyin"
    assert ibus.switches == [f"set {_linux_ime.PASSTHROUGH_ENGINE}", "set libpinyin"]


@pytest.mark.parametrize("engine", [None, "xkb:us::eng", "xkb:ru::rus"])
def test_no_switch_without_a_transforming_engine(ibus, engine) -> None:
    if engine:
        ibus.set(engine)
    with _linux_ime.ascii_passthrough():
        pass
    assert ibus.switches == []


def test_type_string_sends_keys_with_the_engine_switched(ibus, monkeypatch) -> None:
    ibus.set("m17n:ru:translit")
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _linux_input, "_type_string_keys",
        lambda text, delay=None: seen.append((text, ibus.engine)),
    )
    _linux_input.type_string("hello world 42")
    assert seen == [("hello world 42", _linux_ime.PASSTHROUGH_ENGINE)]
    assert ibus.engine == "m17n:ru:translit"


def test_type_into_target_switches_once_for_the_whole_string(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda acc: True)
    monkeypatch.setattr(_atspi, "_type_string", lambda ch: sent.append((ch, ibus.engine)))
    _atspi._type_into_target(object(), "hello 42")
    assert [c for c, _ in sent] == list("hello 42")
    assert {e for _, e in sent} == {_linux_ime.PASSTHROUGH_ENGINE}
    assert ibus.switches == [f"set {_linux_ime.PASSTHROUGH_ENGINE}", "set libpinyin"]
    assert ibus.engine == "libpinyin"


def test_unswitchable_engine_refuses_before_any_key_and_names_the_ime(ibus, monkeypatch) -> None:
    ibus.set("m17n:ru:translit")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    sent: list[str] = []
    monkeypatch.setattr(_linux_input, "_type_string_keys", lambda text, delay=None: sent.append(text))
    with pytest.raises(ComputerUseError) as exc:
        _linux_input.type_string("hello world 42")
    assert sent == []
    err = exc.value
    assert err.code is ErrorCode.UNSUPPORTED
    assert err.detail["reason"] == "ime_active"
    assert err.detail["ime"] == {"framework": "ibus", "engine": "m17n:ru:translit"}
    assert err.detail["typed"] is False
    assert "m17n:ru:translit" in str(err) and "xkb:us::eng" in str(err)
    assert outcome.next_for_error(err.code.value, err.detail) == ("cdp",)
    assert ibus.engine == "m17n:ru:translit"  # the user's engine is untouched


def test_text_outside_ascii_is_not_refused_when_the_switch_fails(ibus, monkeypatch) -> None:
    """Spare-keycode characters pass an engine unchanged, so they can still be typed."""
    ibus.set("libpinyin")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    sent: list[str] = []
    monkeypatch.setattr(_linux_input, "_type_string_keys", lambda text, delay=None: sent.append(text))
    _linux_input.type_string("привет合理")
    assert sent == ["привет合理"]
    assert _linux_ime.needs_passthrough("a") and not _linux_ime.needs_passthrough("привет")
    assert _linux_ime.needs_passthrough(None)


def test_type_into_target_refuses_before_the_first_character(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    sent: list[str] = []
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda acc: True)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda acc: True)
    monkeypatch.setattr(_atspi, "_type_string", sent.append)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._type_into_target(object(), "hello")
    assert exc.value.detail["reason"] == "ime_active"
    assert sent == []


def test_a_refused_type_leaves_no_text_in_the_field(ibus, monkeypatch) -> None:
    """The regression: an unswitchable engine used to type anyway and leave IME text behind."""
    ibus.set("m17n:ru:translit")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    field = {"text": ""}

    def keys(text, delay=None):
        field["text"] += "хелло ворлд" if _linux_ime.active_ime() else text

    monkeypatch.setattr(_linux_input, "_type_string_keys", keys)
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda acc: True)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda acc: None)
    with pytest.raises(ComputerUseError):
        _atspi._type_into_target(object(), "hello world")
    assert field["text"] == ""


def test_a_set_value_that_cannot_bypass_leaves_a_filled_field_alone(ibus, monkeypatch) -> None:
    """The original cannot be typed back through the engine, so it is not cleared."""
    ibus.set("libpinyin")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: "keep me")
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)

    def unexpected(*args, **kwargs):
        pytest.fail("the field was cleared or typed into")

    for name in ("_x11_select_all_and_delete", "_type_into_target", "_restore_text", "grab_focus"):
        monkeypatch.setattr(_atspi, name, unexpected)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "hello world 42")
    assert exc.value.detail["reason"] == "ime_active"
    assert exc.value.detail["ime"]["engine"] == "libpinyin"
    assert "nothing was typed and the field was not changed" in str(exc.value)


@pytest.mark.parametrize(
    "original,text",
    [("", "hello world 42"), ("   ", "hello"), ("keep me", "привет")],
    ids=["empty-field", "blank-field", "ascii-original-needs-a-restore"],
)
def test_replace_with_keys_refuses_before_any_key_when_the_engine_cannot_be_switched(
    ibus, monkeypatch, original, text,
) -> None:
    ibus.set("libpinyin")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: original)
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)

    def unexpected(*args, **kwargs):
        pytest.fail("a key was sent or focus was moved")

    for name in ("_x11_select_all_and_delete", "_type_into_target", "_restore_text", "grab_focus"):
        monkeypatch.setattr(_atspi, name, unexpected)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), text)
    assert exc.value.detail["reason"] == "ime_active"


def test_replace_with_keys_still_types_non_ascii_into_an_empty_field(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    field = {"text": ""}
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: field["text"])
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: field["text"] == text)
    monkeypatch.setattr(_atspi, "_type_into_target", lambda acc, text: field.update(text=text))
    assert _atspi._replace_with_keys(object(), "привет") is True


def test_replace_with_keys_lands_ascii_with_the_engine_switched(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    field = {"text": ""}
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: field["text"])
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)
    monkeypatch.setattr(_atspi, "_confirm_text", lambda acc, text: field["text"] == text)

    def type_into(acc, text):
        # A transforming engine would produce something else.
        field["text"] += text if ibus.engine.startswith("xkb:") else "合理咯"

    monkeypatch.setattr(_atspi, "_type_into_target", type_into)
    monkeypatch.setattr(_atspi, "_x11_select_all_and_delete", lambda acc: field.update(text=""))
    assert _atspi._replace_with_keys(object(), "hello world 42") is True
    assert field["text"] == "hello world 42"
    assert ibus.engine == "libpinyin"


def _element() -> Element:
    return Element("e1", "AXTextField", "Name field", "", Bounds(0, 0, 0, 200, 30), "snap-1", editable=True)


def test_failed_set_value_leaves_the_field_focused(ibus, monkeypatch) -> None:
    """Restoring the caret selects the document; the field must have the keyboard again."""
    from a11y_computer_use.drivers import _cdp_forms

    focus = {"on": "field"}
    handle = object()
    monkeypatch.setattr(_cdp_forms, "write_element", lambda *a, **k: None)
    monkeypatch.setattr(_atspi, "_refuse_model_editor_replace", lambda h: None)
    monkeypatch.setattr(_atspi, "is_sheet_cell", lambda h: False)
    monkeypatch.setattr(_atspi, "control_kind", lambda h: "text")
    monkeypatch.setattr(_atspi, "_refuse_drawing_multiline_value", lambda h, v: None)
    monkeypatch.setattr(_atspi, "_capture_text_points", lambda h: ["caret"])
    monkeypatch.setattr(_atspi, "_restore_text_points", lambda saved: focus.update(on="document"))
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda h: False)
    monkeypatch.setattr(_atspi, "web_entry_context", lambda h: None)
    monkeypatch.setattr(_atspi, "set_text", lambda h, v: False)
    monkeypatch.setattr(_atspi, "grab_focus", lambda h: focus.update(on="field") or True)
    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    monkeypatch.setattr(driver, "_dom_window_title", lambda h: "")
    monkeypatch.setattr(driver, "_dom_pids", lambda h: ())
    monkeypatch.setattr(driver, "_focus_web_entry", lambda element, h, **kw: h)
    with pytest.raises(ComputerUseError) as exc:
        driver._apply_set_value(_element(), "hello world 42", handle)
    assert exc.value.detail["reason"] == "text_mismatch"
    assert focus["on"] == "field"
