"""Linux file_dialog and set_value. Synthetic trees and a stubbed frontmost.

Not a live desktop, not Mousepad, and not Chrome. The AT-SPI objects below
are fakes. They record focus, clicks, and keystrokes so a refusal that
still drives the target fails the test.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import observe, safety, server
from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Scope,
    Snapshot,
)


class _TextApi:
    @staticmethod
    def get_character_count(acc):
        return len(acc.text)

    @staticmethod
    def get_text(acc, start, end):
        if int(end) < 0:
            return acc.text
        return acc.text[int(start):int(end)]

    @staticmethod
    def set_selection(acc, _selection_num, start_offset, end_offset):
        acc.selection = (int(start_offset), int(end_offset))
        return True

    @staticmethod
    def add_selection(acc, start_offset, end_offset):
        acc.selection = (int(start_offset), int(end_offset))
        return True


class _Atspi:
    Text = _TextApi


class _Node:
    """Synthetic accessible. ``events`` records focus, clicks, and chords."""

    def __init__(self, text: str, events: list):
        self.text = text
        self.events = events

    def get_editable_text_iface(self):
        return None

    def get_editable_text(self):
        return None

    def clear_cache(self):
        return None

    def get_component_iface(self):
        return self

    def grab_focus(self):
        self.events.append("focus")
        return True

    def get_action_iface(self):
        return self

    def get_n_actions(self):
        return 1

    def get_action_name(self, _index):
        return "click"

    def do_action(self, _index):
        self.events.append("click")
        return True


class _ReplacingField:
    """GTK-shaped fake: set_text_contents replaces the buffer."""

    def __init__(self, text: str):
        self.text = text
        self.deletes = 0

    def get_editable_text_iface(self):
        return self

    def clear_cache(self):
        return None

    def set_text_contents(self, text):
        self.text = text
        return True

    def delete_text(self, start, end):
        self.deletes += 1
        self.text = self.text[:start] + self.text[end:]
        return True

    def insert_text(self, pos, text, length):
        self.text = self.text[:pos] + text[:length] + self.text[pos:]
        return True


class _AppendingField(_ReplacingField):
    """Chromium-shaped fake: set_text_contents appends and still returns true.

    delete_text clears the buffer only when the whole range was selected.
    """

    def set_text_contents(self, text):
        self.text += text
        return True

    def delete_text(self, start, end):
        self.deletes += 1
        selected = getattr(self, "selection", None)
        if selected == (0, len(self.text)) and int(start) == 0 and int(end) >= len(self.text):
            self.text = ""
            self.selection = None
        return True


def _element(ref: str, role: str, title: str, *, editable: bool = False, value: str | None = None) -> Element:
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=value,
        bounds=Bounds(0, 10, 20, 80, 24),
        snapshot_id="snap-synth",
        editable=editable,
        clickable=True,
    )


def _snapshot(*elements: Element, app: str = "mousepad") -> Snapshot:
    return Snapshot(
        snapshot_id="snap-synth",
        scope=Scope.WINDOW,
        app=app,
        pid=7,
        created_at=0.0,
        displays=(Display(0, 1280, 800, 1.0, True),),
        elements=elements,
    )


def _runtime(tmp_path, driver):
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("mousepad", safety.Tier.FULL)
    store.set_tier("xfce4-terminal", safety.Tier.FULL)
    store.set_tier("com.apple.TextEdit", safety.Tier.FULL)
    return server.Runtime(
        driver=driver,
        store=store,
        audit=safety.AuditLog(tmp_path / "audit"),
    )


@pytest.fixture
def spies(monkeypatch):
    events: list = []
    document = {"text": "CONTROL"}
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)

    def press_chord(chord: str) -> None:
        events.append(("chord", chord))

    def type_string(text: str) -> None:
        events.append(("type", text))
        document["text"] += text

    monkeypatch.setattr(_linux_input, "press_chord", press_chord)
    monkeypatch.setattr(_linux_input, "type_string", type_string)
    return events, document


def test_linux_file_dialog_for_a_background_app_is_unsupported(tmp_path, monkeypatch) -> None:
    """A granted app that is not frontmost must not be a retry hint.

    Synthetic frontmost. The driver raises before any chooser is driven.
    """
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "xfce4-terminal")
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, identifier))
    runtime = _runtime(tmp_path, LinuxDriver())
    with pytest.raises(ComputerUseError) as exc:
        runtime.file_dialog("open", "/tmp/notes.txt", app="mousepad")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.message.startswith(
        "file_dialog is not supported on Linux: GTK and portal file choosers are not driven"
    )
    assert exc.value.detail["reason"] == "no_file_dialog"
    assert "re-observe" not in exc.value.message
    assert exc.value.code is not ErrorCode.FOCUS_CHANGED


def test_linux_file_dialog_for_an_app_that_is_not_frontmost_is_unsupported(tmp_path, monkeypatch) -> None:
    """The named app is not the frontmost app. Same unsupported result.

    ``app_not_found`` would also be acceptable. ``focus_changed`` is not.
    Synthetic frontmost, not a live process list.
    """
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "mousepad")
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, identifier))
    runtime = _runtime(tmp_path, LinuxDriver())
    with pytest.raises(ComputerUseError) as exc:
        runtime.file_dialog("open", "/tmp/notes.txt", app="xfce4-terminal")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert "not supported on Linux" in exc.value.message
    assert "re-observe" not in exc.value.message


def test_linux_file_dialog_without_an_app_stays_unsupported(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "mousepad")
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, identifier))
    runtime = _runtime(tmp_path, LinuxDriver())
    with pytest.raises(ComputerUseError) as exc:
        runtime.file_dialog("save", "/tmp/out.txt")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "no_file_dialog"


def test_linux_key_still_rechecks_frontmost(tmp_path, monkeypatch) -> None:
    """The file_dialog skip does not drop the guard on other Linux actions."""
    calls = {"n": 0}

    def frontmost() -> str:
        calls["n"] += 1
        return "mousepad" if calls["n"] == 1 else "xfce4-terminal"

    monkeypatch.setattr(server, "_frontmost_bundle", frontmost)
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, identifier))
    typed: list[str] = []
    driver = LinuxDriver()
    driver.key_chord = lambda chord, **_kwargs: typed.append(chord)  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.key("escape")
    assert exc.value.code is ErrorCode.FOCUS_CHANGED
    assert "re-observe and retry" in exc.value.message
    assert typed == []


def test_other_drivers_still_recheck_file_dialog_frontmost(tmp_path) -> None:
    """A non-Linux file_dialog still aborts when focus moves, and does not drive."""

    class _Driver:
        name = "macos"
        resolves_apps = True

        def __init__(self):
            self.calls: list = []
            self._fronts = ["com.apple.TextEdit", "com.other"]

        def frontmost_app(self):
            return self._fronts.pop(0), 1

        def file_dialog(self, verb, path, app):
            self.calls.append((verb, path, app))
            return {"action": verb, "path": path, "steps": ["go-to-folder"]}

    driver = _Driver()
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.file_dialog("open", "/tmp/notes.txt")
    assert exc.value.code is ErrorCode.FOCUS_CHANGED
    assert "re-observe and retry" in exc.value.message
    assert driver.calls == []


@pytest.mark.parametrize(
    ("role", "title"),
    [
        ("AXMenu", "File"),
        ("AXStaticText", "Heading"),
        ("AXStaticText", "Label"),
        ("AXButton", "Reload"),
    ],
)
def test_set_value_on_a_non_editable_element_sends_nothing(tmp_path, monkeypatch, spies, role, title) -> None:
    """Synthetic menu, static text, and button. Not a live toolkit."""
    events, document = spies
    node = _Node("File" if role == "AXMenu" else title, events)
    element = _element("e2", role, title)
    snap = _snapshot(element)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: node)
    driver = LinuxDriver()
    driver.resolve_ref = lambda snapshot, ref, live=None: snapshot.element(ref)  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    runtime._current = snap
    with pytest.raises(ComputerUseError) as exc:
        runtime.set_value("e2", "NE_TEST")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert "is not editable" in exc.value.message
    assert exc.value.detail["reason"] == "not_editable"
    assert exc.value.detail["role"] == role
    assert events == []
    assert document["text"] == "CONTROL"
    assert node.text == ("File" if role == "AXMenu" else title)


def test_set_value_on_a_gtk_text_area_replaces_and_reads_back(spies, monkeypatch) -> None:
    """Synthetic GTK EditableText. Success is Text.get_text(0, -1)."""
    events, document = spies
    field = _ReplacingField("CONTROL")
    element = _element("e3", "AXTextArea", "Document", editable=True, value="CONTROL")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: field)
    assert LinuxDriver().set_value(element, "BETA") is True
    assert field.text == "BETA"
    assert _atspi._atspi().Text.get_text(field, 0, -1) == "BETA"
    assert field.deletes == 0
    assert events == []
    assert document["text"] == "CONTROL"


def test_set_value_on_a_chromium_field_still_replaces(spies, monkeypatch) -> None:
    """Synthetic Chromium field whose set_text_contents appends.

    The snapshot read is the whole buffer. The bounded echo is not the check.
    """
    events, _document = spies
    field = _AppendingField("earlier")
    element = _element("e3", "AXTextField", "Name", editable=True, value="earlier")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: field)
    driver = LinuxDriver()
    assert driver.set_value(element, "ALPHA") is True
    assert _atspi._atspi().Text.get_text(field, 0, -1) == "ALPHA"
    assert driver.set_value(element, "BETA") is True
    assert field.text == "BETA"
    assert events == []


def test_text_area_role_without_the_flag_is_still_editable(spies, monkeypatch) -> None:
    """A synthetic element can name AXTextArea and leave ``editable`` false.

    The role is the same one the pruner marks editable. That path keeps working.
    """
    _events, _document = spies
    field = _ReplacingField("old")
    element = _element("e3", "AXTextArea", "Document", editable=False, value="old")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: field)
    assert LinuxDriver().set_value(element, "NEW") is True
    assert _atspi._atspi().Text.get_text(field, 0, -1) == "NEW"
