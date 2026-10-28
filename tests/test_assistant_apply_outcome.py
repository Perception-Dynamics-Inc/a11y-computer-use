"""#1281: a committed Apply must never get retry hints; a closed window is the effect."""
from __future__ import annotations

from types import SimpleNamespace

from a11y_computer_use import observe, outcome, server
from a11y_computer_use.drivers import _atspi, linux
from a11y_computer_use.schema import Bounds, Element


def _apply(**kw):
    base = dict(ref="e11", role="AXButton", title="Apply", value=None, default=True,
                bounds=Bounds(0, 560, 410, 44, 30), snapshot_id="s3", path=("AXButton",))
    base.update(kw)
    return Element(**base)


def _conclude(monkeypatch, verdict, element, effect=None, before="showing"):
    rt = server.Runtime.__new__(server.Runtime)
    drv = SimpleNamespace(name="linux")
    if effect is not None:
        drv.click_window_state = effect
    rt.driver = drv
    rt._click_window = (element, before)
    probe = outcome.RetryEvidence(
        context=("assistant", 1, "Confirm"),
        signals=tuple((name, "same") for name in sorted(outcome.RETRY_SIGNALS["click"])),
    )
    rt._retry_reads = ("assistant", SimpleNamespace(modifiers=()), probe, probe)
    monkeypatch.setattr(rt, "_take_keysym_mismatch", lambda: None, raising=False)
    return rt._conclude(f"clicked {element.ref}", tool="click", app="assistant",
                        element=element, had_ref=True, verdict=verdict)


def _info(result):
    return result.as_dict()


def test_quiet_tree_after_apply_is_not_a_retryable_noop(monkeypatch):
    result = _info(_conclude(monkeypatch, ("suspected_noop", "the accessibility state did not change"),
                             _apply()))
    assert result["outcome"] == "unverifiable"
    assert not result["next"]
    assert "repeat a committed action" in result["evidence"]


def test_default_button_with_any_label_is_not_retried(monkeypatch):
    result = _info(_conclude(monkeypatch, ("suspected_noop", "the accessibility state did not change"),
                             _apply(title="Go", default=True)))
    assert result["outcome"] == "unverifiable" and not result["next"]


def test_plain_button_still_gets_retry_hints(monkeypatch):
    result = _info(_conclude(monkeypatch, ("suspected_noop", "the accessibility state did not change"),
                             _apply(title="Apply", default=False)))
    assert result["outcome"] == "suspected_noop" and result["next"]


def test_window_hidden_or_closed_after_apply_is_confirmed(monkeypatch):
    for state in ("hidden", "closed"):
        for judged in ("unverifiable", "suspected_noop"):
            result = _info(_conclude(monkeypatch, (judged, "the accessibility state could not be read"),
                                     _apply(), effect=lambda el, s=state: s))
            assert result["outcome"] == "confirmed"
            assert result["evidence"] == f"the target's window was {state} by the click"
            assert not result["next"]


def test_a_window_that_was_not_showing_before_is_no_signal(monkeypatch):
    result = _info(_conclude(monkeypatch, ("unverifiable", "the accessibility state could not be read"),
                             _apply(default=False), effect=lambda el: "hidden", before=None))
    assert result["outcome"] == "unverifiable"


def test_a_window_still_showing_is_no_signal(monkeypatch):
    result = _info(_conclude(monkeypatch, ("suspected_noop", "the accessibility state did not change"),
                             _apply(default=False), effect=lambda el: "showing"))
    assert result["outcome"] == "suspected_noop" and result["next"]


def test_commits_action_is_the_native_default_button():
    assert outcome.commits_action(_apply())
    assert not outcome.commits_action(_apply(title="Next", default=False))
    assert not outcome.commits_action(_apply(role="AXCheckBox"))


class _Node:
    def __init__(self, role, states, parent=None):
        self.role, self.states, self.parent = role, set(states), parent


def _patch_tree(monkeypatch):
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda n: None)
    monkeypatch.setattr(_atspi, "_state_has", lambda n, s: s in n.states)
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    monkeypatch.setattr(_atspi, "_call_first",
                        lambda n, names: n.parent if names[0] == "get_parent" else n.states)


def test_atspi_reads_hidden_and_closed_windows(monkeypatch):
    _patch_tree(monkeypatch)
    app = _Node("application", [])
    shown = _Node("frame", ["SHOWING", "VISIBLE"], app)
    hidden = _Node("frame", ["RESIZABLE"], app)
    assert _atspi.click_window_state(_Node("push button", ["SHOWING"], shown)) == "showing"
    assert _atspi.click_window_state(_Node("push button", [], hidden)) == "hidden"
    assert _atspi.click_window_state(_Node("push button", ["DEFUNCT"], shown)) == "closed"


def test_linux_driver_hook_uses_the_ref_handle(monkeypatch):
    _patch_tree(monkeypatch)
    app = _Node("application", [])
    hidden = _Node("frame", [], app)
    button = _Node("push button", [], hidden)
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: button)
    drv = object.__new__(linux.LinuxDriver)
    drv._run = lambda fn: fn()
    assert drv.click_window_state(_apply()) == "hidden"
