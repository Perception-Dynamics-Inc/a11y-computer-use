"""Hermetic coverage of the shared GTK 4 live-fixture launcher."""

import sys

from tests import test_linux_gtk4_live


def test_python_fixture_uses_shared_gtk4_launch_environment(monkeypatch):
    """Python fixtures keep the active interpreter and native app bridge flags."""
    calls = []
    process = object()

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setenv("GTK_A11Y", "none")
    monkeypatch.setenv("NO_AT_BRIDGE", "1")
    monkeypatch.setattr(test_linux_gtk4_live.subprocess, "Popen", popen)

    assert test_linux_gtk4_live._launch(sys.executable, "entry584.py") is process
    argv, kwargs = calls.pop()
    assert argv == [sys.executable, "entry584.py"]
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["GTK_A11Y"] == "atspi"
    assert kwargs["env"]["NO_AT_BRIDGE"] == "0"
