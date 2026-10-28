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
    # No test reads a real panel; the composition tests set this themselves.
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: False)
    # The settle is real time on a real panel; none of it is needed against a stub.
    monkeypatch.setattr(_linux_ime, "_SETTLE_S", 0.0)
    monkeypatch.setattr(_linux_ime, "_BUDGET_S", 0.2)
    monkeypatch.setattr(_linux_ime, "_POLL_S", 0.0)

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


# --- an open composition is never discarded (#1219) ---------------------------


def _open_composition(monkeypatch) -> None:
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: True)


@pytest.mark.parametrize("text", ["abc", " abc", "你好世界", None])
def test_type_string_refuses_before_any_switch_or_key_while_a_composition_is_open(
    ibus, monkeypatch, text,
) -> None:
    ibus.set("libpinyin")
    _open_composition(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(_linux_input, "_type_string_keys", lambda t, delay=None: sent.append(t))
    with pytest.raises(ComputerUseError) as exc:
        _linux_input.type_string(text or "x")
    err = exc.value
    assert sent == []
    assert ibus.switches == []  # the engine is not touched, so the preedit survives
    assert ibus.engine == "libpinyin"
    assert err.detail["reason"] == "ime_composition_open"
    assert err.detail["ime"] == {"framework": "ibus", "engine": "libpinyin"}
    assert err.detail["typed"] is False
    assert "libpinyin" in str(err) and "Escape" in str(err)


def test_type_into_target_and_set_value_leave_an_open_composition_alone(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    _open_composition(monkeypatch)
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: "你好")
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)

    def unexpected(*args, **kwargs):
        pytest.fail("a key was sent or focus was moved")

    for name in ("_x11_select_all_and_delete", "_restore_text", "grab_focus", "_type_string"):
        monkeypatch.setattr(_atspi, name, unexpected)
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda acc: True)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda acc: True)
    with pytest.raises(ComputerUseError) as exc:
        _atspi._replace_with_keys(object(), "hello")
    assert exc.value.detail["reason"] == "ime_composition_open"
    with pytest.raises(ComputerUseError) as again:
        _atspi._type_into_target(object(), "hello")
    assert again.value.detail["reason"] == "ime_composition_open"
    assert ibus.switches == []


def test_no_composition_means_the_engine_is_switched_as_before(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: False)
    monkeypatch.setattr(_linux_input, "_type_string_keys", lambda t, delay=None: None)
    _linux_input.type_string("abc")
    assert ibus.switches == ["set xkb:us::eng", "set libpinyin"]


def test_a_plain_layout_does_not_look_for_a_composition(ibus, monkeypatch) -> None:
    ibus.set("xkb:us::eng")
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: pytest.fail("looked"))
    monkeypatch.setattr(_linux_input, "_type_string_keys", lambda t, delay=None: None)
    _linux_input.type_string("abc")


def test_a_nested_use_checks_the_composition_once(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    calls: list[int] = []
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: calls.append(1) or False)
    with _linux_ime.ascii_passthrough("a"):
        with _linux_ime.ascii_passthrough("b"):
            pass
    nested = len(calls)
    calls.clear()
    with _linux_ime.ascii_passthrough("a"):
        pass
    assert nested == len(calls)


class _Node:
    def __init__(self, role, name="", showing=True, children=()):
        self._role, self._name, self._showing, self._children = role, name, showing, list(children)

    def get_role_name(self):
        return self._role

    def get_name(self):
        return self._name

    def get_child_count(self):
        return len(self._children)

    def get_child_at_index(self, index):
        return self._children[index]

    def get_state_set(self):
        outer = self

        class States:
            names = {"SHOWING"} if outer._showing else set()

        return States()


def _panel(*, preedit="", candidates=()):
    rows = [
        _Node("panel", children=[_Node("label", f"{i}."), _Node("label", c)])
        for i, c in enumerate(candidates, 1)
    ] + [
        _Node("panel", showing=False, children=[_Node("label", "6."), _Node("label", "", showing=False)]),
    ]
    window = _Node("window", children=[_Node("filler", children=[
        _Node("label", ""), _Node("label", preedit), _Node("separator"), _Node("filler", children=rows),
    ])])
    return _Node("application", "ibus-ui-gtk3", children=[window])


@pytest.mark.parametrize(
    "panel,expected",
    [
        (_panel(preedit="ni hao |", candidates=["你好", "你好吗"]), True),
        (_panel(candidates=["你好"]), True),
        (_panel(preedit="ni"), True),
        (_panel(), False),  # the window stays mapped after a commit; no label has text
    ],
    ids=["preedit+candidates", "candidates-only", "preedit-only", "idle"],
)
def test_the_panel_tree_tells_a_composition_from_an_idle_panel(monkeypatch, panel, expected) -> None:
    monkeypatch.setattr(
        _atspi, "desktop_application_roots",
        lambda: _atspi.DesktopApplications([("ibus-ui-gtk3", panel), ("chrome", _Node("application"))], 0),
    )
    assert _atspi.ime_composition_open() is expected


def test_without_a_panel_or_when_it_cannot_be_read_the_composition_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        _atspi, "desktop_application_roots",
        lambda: _atspi.DesktopApplications([("chrome", _Node("application"))], 0),
    )
    assert _atspi.ime_composition_open() is None

    def boom():
        raise RuntimeError("no bus")

    monkeypatch.setattr(_atspi, "desktop_application_roots", boom)
    assert _atspi.ime_composition_open() is None
    monkeypatch.setattr(_linux_ime, "_composition_open", _linux_ime._composition_open)
    monkeypatch.setattr(_atspi, "ime_composition_open", boom)
    assert _linux_ime._composition_open() is None


# --- the check is settled, never skipped, and runs before any clear (#1219) ----


def test_a_composition_the_panel_publishes_late_is_still_found(ibus, monkeypatch) -> None:
    """The panel lags the keys that made the preedit; one idle read is not an answer."""
    ibus.set("libpinyin")
    monkeypatch.setattr(_linux_ime, "_SETTLE_S", 0.15)
    started = time.monotonic()
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: time.monotonic() - started > 0.06)
    assert _linux_ime.composition_state() is True
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as exc:
        with _linux_ime.ascii_passthrough("abc"):
            pytest.fail("typed over an open composition")
    assert exc.value.detail["reason"] == "ime_composition_open"
    assert ibus.switches == []


def test_idle_is_only_reported_after_the_settle_period(ibus, monkeypatch) -> None:
    monkeypatch.setattr(_linux_ime, "_SETTLE_S", 0.1)
    monkeypatch.setattr(_linux_ime, "_POLL_S", 0.01)
    began = time.monotonic()
    assert _linux_ime.composition_state() is False
    assert time.monotonic() - began >= 0.1


def test_a_transient_unreadable_panel_is_retried_not_refused(ibus, monkeypatch) -> None:
    reads = iter([None, None, False, False, False, False, False, False])
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: next(reads, False))
    assert _linux_ime.composition_state() is False


def test_an_unreadable_panel_is_unknown_and_refuses_before_any_switch(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: None)
    with pytest.raises(ComputerUseError) as exc:
        with _linux_ime.ascii_passthrough("abc"):
            pytest.fail("typed although a composition could not be ruled out")
    assert exc.value.detail["reason"] == "ime_composition_unknown"
    assert exc.value.detail["typed"] is False
    assert ibus.switches == []
    monkeypatch.setenv("A11Y_IME_COMPOSITION_UNKNOWN", "proceed")
    with _linux_ime.ascii_passthrough("abc"):
        assert ibus.engine == "xkb:us::eng"


def test_a_late_composition_after_unknown_reads_is_found(ibus, monkeypatch) -> None:
    reads = iter([None, None, True])
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: next(reads, None))
    assert _linux_ime.composition_state() is True


def _firefox_field(monkeypatch, text="keep me"):
    """A Firefox entry whose clear and typing are recorded instead of sent."""
    sent: list[str] = []
    field = {"text": text}
    monkeypatch.setattr(_atspi, "_full_text", lambda acc: field["text"])
    monkeypatch.setattr(_atspi, "_x11_keys_available", lambda: True)
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: True)
    monkeypatch.setattr(_atspi, "_chord_into_target", lambda acc, chord: (sent.append(chord), field.update(text="") if chord == "backspace" else None))
    monkeypatch.setattr(_atspi, "_wait_until_gone", lambda acc: True)
    monkeypatch.setattr(_atspi, "_type_into_target", lambda acc, t: sent.append(f"type {t}"))
    monkeypatch.setattr(_atspi, "_confirm_text_landed", lambda acc, t: True)
    monkeypatch.setattr(_atspi, "_shown_matches", lambda acc, t: False)
    return field, sent


def test_firefox_set_value_with_an_unswitchable_engine_never_clears_the_field(ibus, monkeypatch) -> None:
    """The clear (ctrl+a, BackSpace) used to run before the IME check: 0/3 kept."""
    ibus.set("libpinyin")
    monkeypatch.setenv("FAKE_IBUS_REFUSE", "1")
    field, sent = _firefox_field(monkeypatch)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: False)
    monkeypatch.setattr(_atspi, "_gecko_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_refuse_model_editor_replace", lambda acc: None)
    monkeypatch.setattr(_atspi, "_capture_text_points", lambda acc: [])
    monkeypatch.setattr(_atspi, "_restore_text_points", lambda saved: None)
    # The unguarded body: the clear would run and empty the field.
    monkeypatch.setattr(_atspi, "_set_text_body", lambda acc, t, force=False: _atspi._focus_and_replace(acc, t))
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_text(object(), "hello world 42")
    assert exc.value.detail["reason"] == "ime_active"
    assert exc.value.detail["typed"] is False
    assert sent == []
    assert field["text"] == "keep me"
    assert ibus.switches == ["set xkb:us::eng"]


def test_firefox_set_value_with_an_open_composition_never_clears_the_field(ibus, monkeypatch) -> None:
    ibus.set("libpinyin")
    _open_composition(monkeypatch)
    field, sent = _firefox_field(monkeypatch, text="ni hao")
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: False)
    monkeypatch.setattr(_atspi, "_gecko_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_refuse_model_editor_replace", lambda acc: None)
    monkeypatch.setattr(_atspi, "_capture_text_points", lambda acc: [])
    monkeypatch.setattr(_atspi, "_restore_text_points", lambda saved: None)
    monkeypatch.setattr(_atspi, "_set_text_body", lambda acc, t, force=False: _atspi._focus_and_replace(acc, t))
    with pytest.raises(ComputerUseError) as exc:
        _atspi.set_text(object(), "hello world 42")
    assert exc.value.detail["reason"] == "ime_composition_open"
    assert sent == [] and field["text"] == "ni hao"
    assert ibus.switches == []


def test_the_key_clear_refuses_on_its_own_when_a_composition_is_open(ibus, monkeypatch) -> None:
    """Backstop for a caller that reaches the clear without the guard."""
    ibus.set("libpinyin")
    _open_composition(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(_atspi, "grab_focus", lambda acc: sent.append("focus"))
    monkeypatch.setattr(_atspi, "_chord_into_target", lambda acc, chord: sent.append(chord))
    with pytest.raises(ComputerUseError) as exc:
        _atspi._x11_select_all_and_delete(object())
    assert exc.value.detail["reason"] == "ime_composition_open"
    assert sent == []
    # Inside the guard (already checked) it sends the keys, and without an IME too.
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: False)
    _atspi._x11_select_all_and_delete(object())
    assert sent == ["focus", "ctrl+a", "backspace"]
    ibus.set("xkb:us::eng")
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: pytest.fail("looked"))
    _atspi._x11_select_all_and_delete(object())


def test_the_driver_refuses_set_value_before_focus_dom_write_or_window_raise(ibus, monkeypatch) -> None:
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _cdp_forms

    ibus.set("libpinyin")
    _open_composition(monkeypatch)
    handle = object()
    monkeypatch.setattr(observe, "ax_handle_for", lambda snapshot, ref: handle)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_gecko_app", lambda acc: False)

    def unexpected(*args, **kwargs):
        pytest.fail("the field was touched")

    monkeypatch.setattr(_cdp_forms, "write_element", unexpected)
    driver = LinuxDriver()
    driver._run = lambda fn: fn()
    monkeypatch.setattr(driver, "_check_modal_target", lambda element: None)
    for name in ("_raise_handle_window", "_focus_web_entry", "_apply_set_value"):
        monkeypatch.setattr(driver, name, unexpected)
    with pytest.raises(ComputerUseError) as exc:
        driver.set_value(_element(), "hello world 42")
    assert exc.value.detail["reason"] == "ime_composition_open"


def test_a_panel_listed_absent_is_unknown_after_the_settle_window(monkeypatch) -> None:
    # #1220: a disabled panel will not appear by waiting the whole budget.
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: None)
    monkeypatch.setattr(_linux_ime, "_panel_absent", lambda: True)
    started = time.monotonic()
    assert _linux_ime.composition_state() is None
    took = time.monotonic() - started
    assert _linux_ime._SETTLE_S <= took < _linux_ime._BUDGET_S, took


def test_an_unreadable_panel_still_gets_the_whole_budget(monkeypatch) -> None:
    monkeypatch.setattr(_linux_ime, "_composition_open", lambda: None)
    monkeypatch.setattr(_linux_ime, "_panel_absent", lambda: False)
    started = time.monotonic()
    assert _linux_ime.composition_state() is None
    assert time.monotonic() - started >= _linux_ime._BUDGET_S


def test_a_panel_that_appears_during_the_settle_window_is_read(monkeypatch) -> None:
    started = time.monotonic()
    monkeypatch.setattr(
        _linux_ime, "_composition_open",
        lambda: None if time.monotonic() - started < 0.1 else True,
    )
    monkeypatch.setattr(_linux_ime, "_panel_absent", lambda: True)
    assert _linux_ime.composition_state() is True


def test_panel_absent_needs_a_clean_listing(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    listing = lambda ready, silent: (lambda: _atspi.DesktopApplications(ready, silent))
    monkeypatch.setattr(_atspi, "desktop_application_roots", listing([("firefox", 1)], 0))
    assert _atspi.ime_panel_absent() is True
    monkeypatch.setattr(_atspi, "desktop_application_roots", listing([("firefox", 1), ("ibus-ui-gtk3", 2)], 0))
    assert _atspi.ime_panel_absent() is False
    monkeypatch.setattr(_atspi, "desktop_application_roots", listing([("firefox", 1)], 1))
    assert _atspi.ime_panel_absent() is False
    monkeypatch.setattr(_atspi, "desktop_application_roots", listing([], 0))
    assert _atspi.ime_panel_absent() is False
