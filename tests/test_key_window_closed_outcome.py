"""#1283: a key that closes or hides its own window is confirmed, like #1281's click."""
from __future__ import annotations

from types import SimpleNamespace

from a11y_computer_use import outcome, server
from a11y_computer_use.drivers import _atspi, linux


def _conclude(monkeypatch, verdict, *, after=None, probe=("wizard-frame", "showing"),
              chord="Return"):
    rt = server.Runtime.__new__(server.Runtime)
    drv = SimpleNamespace(name="linux")
    if after is not None:
        drv.key_window_state = after
    rt.driver = drv
    signals = outcome.RetryEvidence(
        context=("wizard", 1, "Last page"),
        signals=tuple((name, "same") for name in sorted(outcome.RETRY_SIGNALS.get("key", ()))),
    )
    rt._retry_reads = ("wizard", SimpleNamespace(modifiers=()), signals, signals)
    monkeypatch.setattr(rt, "_take_keysym_mismatch", lambda: None, raising=False)
    monkeypatch.setattr(rt, "_libreoffice_without_bridge", lambda app: False, raising=False)
    return rt._conclude(f"pressed {chord} in wizard", tool="key", app="wizard",
                        key_focus=True, key_chord=chord, verdict=verdict,
                        key_window=probe).as_dict()


def test_return_or_escape_that_closes_the_wizard_is_confirmed(monkeypatch):
    for chord in ("Return", "Escape"):
        for state in ("hidden", "closed"):
            for judged in ("unverifiable", "suspected_noop"):
                result = _conclude(monkeypatch, (judged, "the accessibility state could not be read"),
                                   after=lambda probe, s=state: s, chord=chord)
                assert result["outcome"] == "confirmed", result
                assert result["evidence"] == f"the focused window was {state} by the key"
                assert not result["next"]


def test_a_window_still_showing_is_no_signal(monkeypatch):
    result = _conclude(monkeypatch, ("unverifiable", "the accessibility state could not be read"),
                       after=lambda probe: "showing")
    assert result["outcome"] == "unverifiable"


def test_no_before_read_is_no_signal(monkeypatch):
    result = _conclude(monkeypatch, ("unverifiable", "the accessibility state could not be read"),
                       after=lambda probe: "closed", probe=None)
    assert result["outcome"] == "unverifiable"


def test_an_unreadable_after_state_is_no_signal(monkeypatch):
    result = _conclude(monkeypatch, ("unverifiable", "the accessibility state could not be read"),
                       after=lambda probe: None)
    assert result["outcome"] == "unverifiable"


def test_a_confirmed_key_is_left_alone(monkeypatch):
    def boom(probe):
        raise AssertionError("no window read when the key is already judged")
    result = _conclude(monkeypatch, ("confirmed", "focus moved"), after=boom)
    assert result["outcome"] == "confirmed" and result["evidence"] == "focus moved"


def test_runtime_probe_accepts_only_a_showing_window():
    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = SimpleNamespace(key_window_probe=lambda app: ("frame", "showing"))
    assert rt._key_window_probe("wizard") == ("frame", "showing")
    rt.driver = SimpleNamespace(key_window_probe=lambda app: None)
    assert rt._key_window_probe("wizard") is None
    rt.driver = SimpleNamespace()
    assert rt._key_window_probe("wizard") is None


class _Node:
    def __init__(self, role, states):
        self.role, self.states = role, set(states)


def _patch_tree(monkeypatch, top):
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda n: None)
    monkeypatch.setattr(_atspi, "_state_has", lambda n, s: s in n.states)
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: top)


def test_atspi_reads_the_same_window_before_and_after(monkeypatch):
    wizard = _Node("dialog", ["SHOWING", "VISIBLE", "ACTIVE"])
    _patch_tree(monkeypatch, wizard)
    assert _atspi.key_window_handle("wizard") is wizard
    assert _atspi.key_window_state(wizard) == "showing"
    wizard.states = {"VISIBLE"}
    assert _atspi.key_window_state(wizard) == "hidden"
    wizard.states = {"DEFUNCT"}
    assert _atspi.key_window_state(wizard) == "closed"


def test_atspi_handle_is_only_a_showing_top_level(monkeypatch):
    _patch_tree(monkeypatch, _Node("application", []))
    assert _atspi.key_window_handle("wizard") is None
    _patch_tree(monkeypatch, _Node("frame", ["VISIBLE"]))
    assert _atspi.key_window_handle("wizard") is None
    _patch_tree(monkeypatch, None)
    assert _atspi.key_window_handle("wizard") is None


def test_linux_driver_hooks(monkeypatch):
    wizard = _Node("dialog", ["SHOWING"])
    _patch_tree(monkeypatch, wizard)
    drv = object.__new__(linux.LinuxDriver)
    drv._run = lambda fn: fn()
    probe = drv.key_window_probe("wizard")
    assert probe == (wizard, "showing")
    wizard.states = {"DEFUNCT"}
    assert drv.key_window_state(probe) == "closed"
    assert drv.key_window_state(None) is None
