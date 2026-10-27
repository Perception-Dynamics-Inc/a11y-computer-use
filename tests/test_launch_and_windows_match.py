"""#1191: a launched name and a Windows identifier match only their own program."""

import sys

import pytest

from a11y_computer_use import server
from a11y_computer_use.drivers import _win_system
from a11y_computer_use.schema import ComputerUseError, ErrorCode


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")


@pytest.mark.parametrize("app_id, command", [
    ("code-insiders", "code"),
    ("firefox-esr", "firefox"),
    ("python3", "python"),
    ("gnome-calculator", "gnome-calc"),
])
def test_a_longer_sibling_is_not_the_launched_app(linux, app_id, command):
    assert server._launched_as(app_id, command) is False


@pytest.mark.parametrize("app_id, command", [
    ("gnome-terminal-", "gnome-terminal"),        # comm cut at 15 bytes
    ("gnome-terminal-", "gnome-terminal-server"),
    ("chrome", "google-chrome"),                   # vendor prefix dropped
    ("code", "code"),
])
def test_the_launched_app_still_matches(linux, app_id, command):
    assert server._launched_as(app_id, command) is True


@pytest.mark.parametrize("ident, exe", [
    ("notepad", "notepad++.exe"),
    ("notepad", "chrome.exe"),     # a tab titled "notepad tips" is not Notepad
    ("code", "codehelper.exe"),
    ("note", "notepad.exe"),
])
def test_a_windows_identifier_is_never_a_substring(ident, exe):
    assert _win_system.exe_matches(ident, exe) is False


@pytest.mark.parametrize("ident", ["notepad", "Notepad.exe", "NOTEPAD", r"C:\Windows\notepad.exe"])
def test_a_windows_identifier_names_its_exe(ident):
    assert _win_system.exe_matches(ident, "notepad.exe") is True


def test_pick_window_skips_titles_and_siblings():
    rows = [("tab", 10, "chrome.exe"), ("npp", 11, "notepad++.exe"), ("np", 12, "notepad.exe")]
    assert _win_system.pick_window("notepad", rows) == "np"
    assert _win_system.pick_window("notepad", rows[:2]) is None


def test_two_processes_of_one_program_are_ambiguous():
    rows = [("a", 12, "notepad.exe"), ("b", 13, "notepad.exe")]
    with pytest.raises(ComputerUseError) as err:
        _win_system.pick_window("notepad", rows)
    assert err.value.code == ErrorCode.APP_NOT_FOUND
    assert err.value.detail["reason"] == "ambiguous_app"
    assert [c["pid"] for c in err.value.detail["candidates"]] == [12, 13]
    assert _win_system.pick_window("notepad", [("a", 12, "notepad.exe"), ("a2", 12, "notepad.exe")]) == "a"


def test_windows_resolve_app_ignores_titles(monkeypatch):
    rows = [("tab", 10, "chrome.exe"), ("npp", 11, "notepad++.exe")]
    monkeypatch.setattr(_win_system, "_top_level_rows", lambda: rows, raising=False)
    assert _win_system.resolve_app("notepad") == "notepad"
    assert _win_system.resolve_app("Notepad++") == "notepad++.exe"
