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


GAP_CASES = [
    ("leave_return", "Leave site?", "Leave", "Return", True),
    ("clip_allow_xy", "https://bench.test wants to: Read your clipboard", "Allow", "xy", True),
    ("leave_stale", "Leave site?", "Leave", "stale", True),
    ("leave_stale_dispatch", "Leave site?", "Leave", "dispatch", True),
    ("leave_escape", "Leave site?", "Leave", "Escape", False),
    ("leave_tab", "Leave site?", "Leave", "Tab", False),
    ("clip_tab", "https://bench.test wants to: Read your clipboard", "Allow", "Tab", False),
    ("clip_block_xy", "https://bench.test wants to: Read your clipboard", "Block", "xy", False),
]
GAP_TRIALS = [pytest.param(*case, id=f"{case[0]}-{trial}")
              for case in GAP_CASES for trial in range(1, 4)]


def check_paths(rt, action, proposed, path, risky):
    """Assert denial before mutation, or delivery of the exact requested input."""
    sent = []
    if path == "agent":
        agent = Agent(model=object(), runtime=rt)
        agent.runtime = rt
        agent._last_snap = rt._current
        allowed, reason = agent._allowed(proposed, None)
        assert allowed is not risky
        if risky:
            assert "approval_denied" in reason
            assert sent == []
            return
    if risky:
        with pytest.raises(ComputerUseError) as caught:
            rt._run_gated(action, "browser", lambda: sent.append(action))
        assert caught.value.code == ErrorCode.CONFIRMATION_DECLINED
        assert sent == []
        assert "confirmation_declined" in next(rt.audit.dir_path.glob("*.jsonl")).read_text()
    else:
        rt._run_gated(action, "browser", lambda: sent.append(action))
        assert sent == [action]


@pytest.mark.parametrize("case,title,label,kind,risky", GAP_TRIALS)
@pytest.mark.parametrize("path", ["library", "agent"])
def test_gap_trials(gate, case, title, label, kind, risky, path):
    rt, current = gate
    first = tree(title, label, default=True)
    rt._current = first
    current[0] = first
    if kind in {"stale", "dispatch"}:
        # Close the first dialog, then reopen with new native/snapshot refs.
        current[0] = tree("Plain page", "page")
        sent = []
        rt._run_gated(KeyChord("Escape"), "browser", lambda: sent.append("Escape"))
        assert sent == ["Escape"]
        reopened = replace(first.element("e2"), ref="e77", snapshot_id="reopened")
        current[0] = replace(first, snapshot_id="reopened", elements=(first.elements[0], reopened))
        rt.driver.resolve_ref = lambda snap, ref, *, live: live.element("e77")
        if kind == "dispatch":
            # No risky issuing observation: only the live dispatch context can gate.
            rt._current = tree("Plain page", label)
        action = Click(rt._current.element("e2"))
        proposed = Action("click", {"ref": "e2"})
    elif kind == "xy":
        point = first.element("e2").bounds.center
        action = Click(point)
        proposed = Action("click", {"x": point.x, "y": point.y, "display_id": point.display_id})
    else:
        action = KeyChord(kind)
        proposed = Action("key", {"chord": kind})
    check_paths(rt, action, proposed, path, risky)


@pytest.mark.parametrize("title,label,chord", [
    ("Leave site?", "Leave", "Alt+L"),
    ("Reload site?", "Reload", "Alt+R"),
    ("https://bench.test wants to: Read clipboard", "Allow", "Alt+A"),
    ("Leave site?", "Leave", "space"),
])
@pytest.mark.parametrize("path", ["library", "agent"])
def test_dialog_access_keys(gate, title, label, chord, path):
    rt, current = gate
    rt._current = current[0] = tree(title, label)
    check_paths(rt, KeyChord(chord), Action("key", {"chord": chord}), path, True)


@pytest.mark.parametrize("title,label", [
    ("Leave site?", "Leave"),
    ("Reload site?", "Reload"),
    ("https://bench.test wants to: Read clipboard", "Allow"),
])
@pytest.mark.parametrize("path", ["library", "agent"])
@pytest.mark.parametrize("mode", ["ref", "xy"])
def test_page_namesake_with_open_dialog(gate, title, label, path, mode):
    rt, current = gate
    base = tree(title, label)
    page = replace(base.element("e2"), ref="page", parent=None,
                   bounds=replace(base.element("e2").bounds, x=900))
    rt._current = current[0] = replace(base, elements=base.elements + (page,))
    if mode == "ref":
        action, proposed = Click(page), Action("click", {"ref": "page"})
    else:
        point = page.bounds.center
        action = Click(point)
        proposed = Action("click", {"x": point.x, "y": point.y, "display_id": point.display_id})
    check_paths(rt, action, proposed, path, False)


@pytest.fixture
def native_gate(gate, monkeypatch):
    """Exercise the real Linux scoped probe using a synthetic native transport."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi as api
    from a11y_computer_use.drivers.linux import LinuxDriver

    rt, current = gate
    native = object.__new__(LinuxDriver)
    native._run = lambda read: read()
    nodes = {}
    reads = []

    def scoped(action, app):
        nodes.clear()
        for el in current[0].elements:
            nodes[el.ref] = SimpleNamespace(el=el, kids=[], parent=None)
        for node in nodes.values():
            node.parent = nodes.get(node.el.parent)
            if node.parent is not None:
                node.parent.kids.append(node)
        # Match the stale ref to the reopened button exactly as resolve_ref
        # does before Runtime's dispatch gate, without retaining its old parent.
        if "e77" in nodes:
            nodes["e2"] = nodes["e77"]
        return native.browser_dialog_confirmation(action, app)

    monkeypatch.setattr(rt.driver, "browser_dialog_confirmation", scoped, raising=False)
    monkeypatch.setattr(rt.driver, "snapshot", lambda *a: pytest.fail("full-window reread"))
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: nodes.get(ref))
    monkeypatch.setattr(api, "_drop_cached_properties", lambda node: None)
    monkeypatch.setattr(api, "_atspi_wrapper_key", lambda node: id(node))
    roles = {"AXDialog": "dialog", "AXWindow": "frame", "AXButton": "button", "AXWebArea": "document web"}
    monkeypatch.setattr(api, "_role_name", lambda node: roles.get(node.el.role, node.el.role))
    monkeypatch.setattr(api, "_call_first", lambda node, methods, **kw: node.el.title)
    monkeypatch.setattr(api, "_state_has", lambda node, name: node.el.focused)
    monkeypatch.setattr(api, "_has_default_state", lambda node: node.el.default)
    monkeypatch.setattr(api, "_extents", lambda node: ((node.el.bounds.x, node.el.bounds.y),
                                                     (node.el.bounds.width, node.el.bounds.height)))
    monkeypatch.setattr(api, "_parent_of", lambda node: node.parent)
    monkeypatch.setattr(api, "find_root", lambda app, scope: nodes["e1"])

    def count(node):
        reads.append(node.el.ref)
        assert node.el.role != "AXWebArea", "page subtree must never be read"
        return len(node.kids)

    monkeypatch.setattr(api, "_child_count", count)
    monkeypatch.setattr(api, "_child_at", lambda node, index: node.kids[index])
    return gate, reads


@pytest.mark.parametrize("case,title,label,kind,risky", GAP_TRIALS)
@pytest.mark.parametrize("path", ["library", "agent"])
def test_native_gap_trials(native_gate, case, title, label, kind, risky, path):
    gate, reads = native_gate
    test_gap_trials(gate, case, title, label, kind, risky, path)
    if kind in {"stale", "dispatch", "Escape", "Tab"}:
        assert reads == []  # Ref dispatch reads ancestry; safe keys read nothing.


@pytest.mark.parametrize("title,label,chord", [
    ("Leave site?", "Leave", "Alt+L"),
    ("Reload site?", "Reload", "Alt+R"),
    ("https://bench.test wants to: Read clipboard", "Allow", "Alt+A"),
    ("Leave site?", "Leave", "space"),
])
@pytest.mark.parametrize("path", ["library", "agent"])
def test_native_access_keys(native_gate, title, label, chord, path):
    gate, _ = native_gate
    test_dialog_access_keys(gate, title, label, chord, path)


@pytest.mark.parametrize("title,label", [
    ("Leave site?", "Leave"),
    ("Reload site?", "Reload"),
    ("https://bench.test wants to: Read clipboard", "Allow"),
])
@pytest.mark.parametrize("path", ["library", "agent"])
def test_native_page_namesake(native_gate, title, label, path):
    gate, reads = native_gate
    test_page_namesake_with_open_dialog(gate, title, label, path, "ref")
    assert reads == []


def test_native_key_probe_skips_document(native_gate):
    (rt, current), reads = native_gate
    base = tree("Plain page", "Page button")
    document = replace(base.element("e2"), role="AXWebArea", ref="web")
    rt._current = current[0] = replace(base, elements=base.elements + (document,))
    check_paths(rt, KeyChord("Return"), Action("key", {"chord": "Return"}), "library", False)
    assert "web" not in reads
