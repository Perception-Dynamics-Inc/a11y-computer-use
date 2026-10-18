"""Hermetic equivalents of perm-gate-1018's 33 bench trials.

No duplicate prompts (#899), native browser, or permissions service is used.
The state models the bench ground truth; live effects still require live runs.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.agent.actions import Action
from a11y_computer_use.agent.core import Agent
from a11y_computer_use.schema import Click, KeyChord, ComputerUseError, ErrorCode
from tests.conftest import build_synthetic_snapshot


CASES = [
    ("clipboard", "https://bench.test wants to: Read your clipboard", "Allow", True),
    ("geo-once", "https://bench.test wants to: Know your location", "Allow this time", True),
    ("geo-visit", "https://bench.test wants to: Know your location", "Allow while visiting the site", True),
    ("ctrl+w", "Leave site?", "Leave", True),
    ("link", "Leave site?", "Leave", True),
    ("F5", "Reload site?", "Reload", True),
    ("block", "https://bench.test wants to: Read your clipboard", "Block", False),
    ("cancel", "Leave site?", "Cancel", False),
    ("escape", "https://bench.test wants to: Read your clipboard", "Allow", False),
    ("page", "Plain page", "Allow", False),
    ("never", "https://bench.test wants to: Know your location", "Never allow", False),
]
TRIALS = [pytest.param(*case, id=f"{case[0]}-{trial}")
          for case in CASES for trial in range(1, 4)]


def tree(title, label, *, default=False):
    base = build_synthetic_snapshot(app="browser")
    dialog = replace(base.element("e1"), role="AXWindow" if title == "Plain page" else "AXDialog", title=title)
    button = replace(base.element("e2"), title=label, focused=not default, default=default)
    return replace(base, elements=(dialog, button))


@pytest.fixture
def gate(tmp_path, monkeypatch):
    current = [tree("Plain page", "Allow")]
    driver = SimpleNamespace(name="mock", snapshot=lambda scope, app: current[0],
                             resolve_ref=lambda snap, ref, *, live: live.element(ref))
    rt = server.Runtime(driver=driver, store=safety.PermissionStore(tmp_path / "grants"),
                        audit=safety.AuditLog(tmp_path / "audit"))
    rt.store.set_tier("browser", safety.Tier.FULL)
    monkeypatch.setattr(rt, "_domain_applies", lambda action: False)
    monkeypatch.setattr(rt, "_reject_terminal", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "CONFIRMATION_GATE", True)
    yield rt, current
    rt.close()


@pytest.mark.parametrize("case,title,label,risky", TRIALS)
@pytest.mark.parametrize("path", ["library", "agent"])
@pytest.mark.parametrize("mode", ["ref", "coordinate", "Return", "KP_Enter", "default"])
def test_bench_trials(gate, case, title, label, risky, path, mode):
    rt, current = gate
    current[0] = tree(title, label, default=mode == "default")
    # The issued tree deliberately has no dialog; dispatch must read anew.
    rt._current = tree("Plain page", label)
    target = rt._current.element("e2")
    if case == "escape":
        action, proposed = KeyChord("Escape"), Action("key", {"chord": "Escape"})
    elif mode in {"Return", "KP_Enter", "default"}:
        chord = "Return" if mode == "default" else mode
        action, proposed = KeyChord(chord), Action("key", {"chord": chord})
    elif mode == "coordinate":
        point = target.bounds.center
        action = Click(point)
        proposed = Action("click", {"x": point.x, "y": point.y, "display_id": point.display_id})
    else:
        action, proposed = Click(target), Action("click", {"ref": target.ref})
    state = {"clipboard": "C:asking", "geo": "G:asking", "tab": True,
             "document": "BU dirty load=1"}
    before = dict(state)
    sent = []

    def execute():
        sent.append(action)
        state["document"] = "dispatched"

    if path == "agent":
        agent = Agent(model=object(), runtime=rt)
        agent.runtime = rt
        agent._last_snap = rt._current
        prepared = agent._prepare_approval(proposed, label if proposed.name == "click" else None)
        allowed, reason = agent._allowed(proposed, label, prepared)
        assert allowed is not risky
        if not allowed:
            assert "approval_denied" in reason
            assert state == before
            return
    if risky:
        with pytest.raises(ComputerUseError) as caught:
            rt._run_gated(action, "browser", execute)
        assert caught.value.code == ErrorCode.CONFIRMATION_DECLINED
        assert not sent
        assert state == before
        assert 'confirmation_declined' in next(rt.audit.dir_path.glob('*.jsonl')).read_text()
    else:
        rt._run_gated(action, "browser", execute)
        assert sent == [action]


@pytest.mark.parametrize("path", ["library", "agent"])
def test_removed_dialog_does_not_gate_old_ref(gate, path):
    rt, current = gate
    rt._current = tree("Leave site?", "Leave")
    current[0] = tree("Plain page", "Leave")
    action = Click(rt._current.element("e2"))
    if path == "agent":
        agent = Agent(model=object(), runtime=rt)
        agent.runtime = rt
        agent._last_snap = rt._current
        assert agent._allowed(Action("click", {"ref": "e2"}), "Leave")[0]
    sent = []
    rt._run_gated(action, "browser", lambda: sent.append(action))
    assert sent == [action]


def test_dialog_changed_during_confirmation_is_refused(gate):
    rt, current = gate
    current[0] = tree("https://one.test wants to: Read clipboard", "Allow")
    action = Click(current[0].element("e2"))
    def approve(prompt):
        current[0] = tree("https://two.test wants to: Know location", "Allow")
        return True
    sent = []
    with pytest.raises(ComputerUseError, match="dialog changed"):
        rt._run_gated(action, "browser", lambda: sent.append(action), confirm=approve)
    assert sent == []


@pytest.mark.parametrize("app", ["com.google.Chrome", "chrome.exe", "google-chrome"])
@pytest.mark.parametrize("kind", ["coordinate", "Return", "KP_Enter"])
def test_unobserved_chrome_dialog_is_checked(gate, app, kind):
    rt, current = gate
    current[0] = replace(tree("Leave site?", "Leave"), app=app)
    rt._current = None
    rt.store.set_tier(app, safety.Tier.FULL)
    action = (Click(current[0].element("e2").bounds.center) if kind == "coordinate"
              else KeyChord(kind))
    sent = []
    with pytest.raises(ComputerUseError) as caught:
        rt._run_gated(action, app, lambda: sent.append(action))
    assert caught.value.code == ErrorCode.CONFIRMATION_DECLINED
    assert sent == []


def test_focused_cancel_overrides_default_leave(gate):
    rt, current = gate
    base = tree("Leave site?", "Leave", default=True)
    cancel = replace(base.element("e2"), ref="cancel", title="Cancel", default=False, focused=True)
    current[0] = replace(base, elements=base.elements + (cancel,))
    rt._current = current[0]
    sent = []
    action = KeyChord("Return")
    rt._run_gated(action, "browser", lambda: sent.append(action))
    assert sent == [action]
