"""Retry authorization requires positive, complete no-change evidence."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from a11y_computer_use import outcome, server
from a11y_computer_use.schema import Click, Element, KeyChord


READ = outcome.RetryEvidence(
    context=((), (), ("parent", 0)),
    signals=tuple((name, ()) for name in sorted(set().union(*outcome.RETRY_SIGNALS.values()))),
)


@pytest.mark.parametrize("tool,reason,toggle", [
    ("key", "formatting attributes unavailable (#1028 Writer/Calc)", True),
    ("click", "selection set unavailable (#1038 Draw shift-select)", True),
    ("click", "cross-frame challenge unreadable (#1059 captcha)", True),
    ("key", "popup frame set unreadable (#1041 Electron Escape)", False),
    ("drag", "coordinate source unreadable (#1054 HTML5 card)", False),
    ("type", "focused paragraph unreadable (#1027 Writer)", False),
    ("drag", "drawing children unreadable (#1037 Draw/GTK4)", False),
    ("click", "value-set selection unreadable (#1033 Impress)", False),
])
def test_repro_families_cannot_authorize_a_blind_retry(tool, reason, toggle):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="test")
    action = KeyChord("ctrl+b") if tool == "key" and toggle else Click(None, modifiers=("shift",) if toggle else ())
    unreadable = replace(READ, signals=(None,), reason=reason)
    runtime._retry_reads = ("app", action, READ, unreadable)
    result = runtime._conclude(
        "delivered", tool=tool, app="app", verdict=("partial" if "#1033" in reason else "suspected_noop", reason),
        key_chord="ctrl+b" if toggle else None,
    )
    assert result.outcome == "unverifiable"
    assert result.next == ()
    assert "re-observe first" in result.evidence
    assert "coordinates" not in result.evidence


@pytest.mark.parametrize("where", ["before", "after"])
@pytest.mark.parametrize("missing", [
    None, replace(READ, context=None), replace(READ, signals=None),
    replace(READ, context=((), (), ("parent", None))),
    replace(READ, signals=(("selection", None),)),
    replace(READ, opaque=True, reason="opaque target"),
])
def test_any_unreadable_signal_blocks_retry(where, missing):
    before, after = (missing, READ) if where == "before" else (READ, missing)
    verdict = outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)
    assert verdict[0] == "unverifiable"
    assert "re-observe first" in verdict[1]
    assert outcome.suggest_next("click", verdict[0]) == ()


@pytest.mark.parametrize("tool,signal", [(tool, signal) for tool, signals in outcome.RETRY_SIGNALS.items()
                                         for signal in sorted(signals)])
def test_omitting_any_required_signal_is_not_a_readable_noop(tool, signal):
    missing = replace(READ, signals=tuple(row for row in READ.signals if row[0] != signal))
    judged, evidence = outcome.retry_verdict(("suspected_noop", "unchanged"), READ, missing, tool=tool)
    assert judged == "unverifiable"
    assert signal in evidence
    assert "re-observe first" in evidence
    assert outcome.suggest_next(tool, judged) == ()


@pytest.mark.parametrize("tool", ["click", "key", "type", "drag", "scroll", "select", "menu", "set_value"])
def test_complete_unchanged_reads_keep_real_noops(tool):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="test")
    runtime._retry_reads = ("app", Click(None), READ, READ)
    result = runtime._conclude("delivered", tool=tool, app="app",
                               verdict=("suspected_noop", "unchanged"))
    assert result.outcome == "suspected_noop"
    assert result.next


@pytest.mark.parametrize("changed", [replace(READ, context=(("new focus",), (), ("parent", 0))),
                                     replace(READ, signals=(("new selection",),))])
def test_changed_context_or_action_is_not_a_noop(changed):
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), READ, changed)[0] == "unverifiable"


@pytest.mark.parametrize("role", ["AXCheckBox", "AXSwitch", "AXToggleButton", "AXRadioButton"])
def test_unchanged_toggle_never_offers_retry(role):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="test")
    runtime._retry_reads = ("app", Click(None), READ, READ)
    element = Element("e1", role, "toggle", None, None, "s")
    result = runtime._conclude("clicked", tool="click", app="app", element=element,
                               verdict=("suspected_noop", "unchanged"))
    assert result.outcome == "unverifiable"
    assert result.next == ()
    assert "undo a toggle" in result.evidence


def test_positive_attribute_change_confirms_but_a_different_owner_does_not():
    before = replace(READ, effect=("owner", 3, "weight", "400"), toggle=True)
    after = replace(before, effect=("owner", 3, "weight", "600"))
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)[0] == "confirmed"
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before,
                                 replace(after, effect=("other", 3, "weight", "600")))[0] == "unverifiable"


def test_positive_verification_and_1070_opaque_unknown_survive():
    assert outcome.retry_verdict(("confirmed", "read back"), None, None) == ("confirmed", "read back")
    judged, evidence = outcome.retry_verdict(("unverifiable", "opaque target; screenshot"), None, None)
    assert judged == "unverifiable"
    assert "opaque target" in evidence
    assert outcome.suggest_next("click", judged) == ()


def test_modifier_click_is_unverifiable_even_with_complete_unchanged_reads():
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="test")
    runtime._retry_reads = ("app", Click(None, modifiers=("shift",)), READ, READ)
    result = runtime._conclude("clicked", tool="click", app="app",
                               verdict=("suspected_noop", "unchanged selection"))
    assert result.outcome == "unverifiable"
    assert result.next == ()
    assert "undo a toggle" in result.evidence
    assert runtime._retry_reads is None


@pytest.mark.parametrize("chord,attribute,old,new", [
    ("ctrl+b", "weight", "400", "600"),
    ("ctrl+i", "style", "normal", "italic"),
    ("ctrl+u", "underline", "none", "single"),
])
def test_native_formatting_reads_selected_attribute_without_discovery(monkeypatch, chord, attribute, old, new):
    from a11y_computer_use.drivers import _atspi

    target = object()
    value = [old]
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda node: None)
    monkeypatch.setattr(_atspi, "is_secure", lambda node: False)
    monkeypatch.setattr(_atspi, "_state_has", lambda node, state: state == "FOCUSED")
    monkeypatch.setattr(_atspi, "_focused_node", lambda *args: pytest.fail("must not discover focus"))
    api = SimpleNamespace(
        get_caret_offset=lambda node: 7,
        get_n_selections=lambda node: 1,
        get_selection=lambda node, index: (3, 7),
        get_attribute_run=lambda node, offset, defaults: ({attribute: value[0]}, 3, 7),
    )
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(Text=api))
    before = _atspi._retry_evidence(KeyChord(chord), target, target)
    value[0] = new
    after = _atspi._retry_evidence(KeyChord(chord), target, target)
    assert before.effect == (target, 3, attribute, old)
    assert after.effect == (target, 3, attribute, new)
    assert outcome.retry_verdict(("suspected_noop", "tree unchanged"), before, after)[0] == "confirmed"
    assert outcome.retry_verdict(("suspected_noop", "tree unchanged"), after, after)[0] == "unverifiable"
    api.get_attribute_run = lambda *args: ({}, 3, 7)
    missing = _atspi._retry_evidence(KeyChord(chord), target, target)
    assert missing.effect is None
    assert outcome.retry_verdict(("suspected_noop", "tree unchanged"), before, missing)[0] == "unverifiable"


@pytest.mark.parametrize("failure", ["parent_count", "window_count", "name", "states", "child"])
def test_native_missing_reads_never_become_empty_sets(monkeypatch, failure):
    from a11y_computer_use.drivers import _atspi

    class Node:
        def __init__(self, name, role):
            self.name, self.role, self.children, self.count = name, role, [], 0
            self.states = ("SHOWING", "VISIBLE")

        def get_interfaces(self): return ()
        def get_name(self): return self.name
        def get_role_name(self): return self.role
        def get_state_set(self): return SimpleNamespace(get_states=lambda: self.states)
        def get_child_count(self): return self.count
        def get_child_at_index(self, index): return self.children[index]
        def get_parent(self): return window if self is parent else parent
        def get_application(self): return app

    app, window = Node("application", "application"), Node("window", "frame")
    parent, label, focus = Node("Controls", "panel"), Node("Label", "label"), Node("Field", "text")
    focus.states += ("FOCUSED",)
    app.children, app.count = [window], 1
    parent.children, parent.count = [label, focus], 2
    monkeypatch.setattr(_atspi, "_call_first", lambda obj, names, *args, default=None:
                        next((getattr(obj, name)(*args) for name in names if hasattr(obj, name)), default))
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda node: None)
    monkeypatch.setattr(_atspi, "is_secure", lambda node: False)
    monkeypatch.setattr(_atspi, "_state_has", lambda node, state: node is focus and state == "FOCUSED")
    monkeypatch.setattr(_atspi, "_chromium_app", lambda node: False)
    monkeypatch.setattr(_atspi, "_gecko_app", lambda node: False)
    monkeypatch.setattr(_atspi, "_full_text", lambda node: node.name)
    monkeypatch.setattr(_atspi, "_raw_child_count", lambda node: pytest.fail("false-default counts are not evidence"))
    bypasses = []
    monkeypatch.setattr(_atspi, "_without_child_cache", lambda node, fn: (bypasses.append(node), fn())[1])
    action = Click(Element("e1", "AXStaticText", "Label", None, None, "s"))
    before = _atspi.retry_evidence(action, label, focus)
    assert before.context is not None and before.signals is not None
    if failure == "parent_count": parent.count = None
    if failure == "window_count": app.count = None
    if failure == "name": label.name = None
    if failure == "states": focus.states = None
    if failure == "child": parent.children[1] = None
    after = _atspi.retry_evidence(action, label, focus)
    assert after is None
    assert bypasses == [label, label]
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)[0] == "unverifiable"


def test_addressed_action_cannot_use_another_apps_focused_text(monkeypatch):
    from a11y_computer_use import observe
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda fn: pytest.fail("must not read a foreign app's native handles")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: pytest.fail("foreign snapshot"))
    assert driver.retry_evidence(KeyChord("ctrl+b"), "target", SimpleNamespace(app="other")) is None


def test_retry_probe_failure_is_unknown_without_an_extra_snapshot():
    runtime = server.Runtime.__new__(server.Runtime)
    def fail(*args):
        raise RuntimeError("native signal unavailable")
    runtime.driver = SimpleNamespace(retry_evidence=fail,
                                     snapshot=lambda *args: pytest.fail("must not walk a window"))
    assert runtime._retry_probe(KeyChord("ctrl+b"), "app") is None


def test_native_toggle_metadata_suppresses_even_process_exit_retry_hints():
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="test")
    toggle = replace(READ, toggle=True)
    runtime._retry_reads = ("app", Click(None), toggle, toggle)
    result = runtime._conclude("clicked", tool="click", app="app",
                               verdict=("partial", "the target process exited after the action"))
    assert result.outcome == "partial"
    assert result.next == ()


def test_readback_text_cannot_impersonate_process_death_evidence():
    verdict = ("partial", "read back 'the target process exited after the action'")
    judged, evidence = outcome.retry_verdict(verdict, None, None)
    assert judged == "unverifiable"
    assert "re-observe first" in evidence


def test_unverified_fallback_error_keeps_unknown_outcome_without_retry_hints():
    element = Element("e1", "AXButton", "Canvas fallback", None, None, "s")
    error = server._canvas_fallback_error(element, unconfirmed=True)
    result = outcome.refused_result(text=str(error), code=error.code.value,
                                    message=error.message, detail=error.detail)
    assert result.outcome == "unverifiable"
    assert result.next == ()
    assert "re-observe first" in result.evidence


def test_error_conversion_cannot_reintroduce_retry_routes_for_unknown_effects():
    assert outcome.next_for_error("unsupported", {"outcome": "unverifiable", "next": ["coordinates"]}) == ()


@pytest.fixture
def readable_native_button(monkeypatch):
    from a11y_computer_use.drivers import _atspi

    class Node:
        def __init__(self, name, role, parent=None, interfaces=()):
            self.name, self.role, self.parent = name, role, parent
            self.children, self.interfaces = [], interfaces
            self.url = "file:///form.html"
            self.states = ("VISIBLE", "SHOWING")
            if parent is not None:
                parent.children.append(self)

        def get_name(self): return self.name
        def get_role_name(self): return self.role
        def get_state_set(self): return SimpleNamespace(get_states=lambda: self.states)
        def get_child_count(self): return len(self.children)
        def get_child_at_index(self, index): return self.children[index]
        def get_parent(self): return self.parent
        def get_application(self): return app
        def get_interfaces(self): return self.interfaces
        def get_document_iface(self): return self
        def get_document_attributes(self): return {"DocURL": self.url}

    app = Node("application", "application")
    window = Node("Window", "frame", app)
    document = Node("file:///form.html", "document web", window)
    parent = Node("", "section", document)
    button = Node("Div button", "push button", parent)
    button.states += ("FOCUSED",)
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda node: None)
    monkeypatch.setattr(_atspi, "is_secure", lambda node: False)
    monkeypatch.setattr(_atspi, "_without_child_cache", lambda node, fn: fn())
    action = Click(Element("e1", "AXButton", "Div button", None, None, "s"))
    return _atspi, action, button, document, Node


def test_native_button_with_unnamed_web_parent_has_complete_noop_evidence(readable_native_button):
    native, action, button, document, _ = readable_native_button
    before = native.retry_evidence(action, button, None)
    after = native.retry_evidence(action, button, None)
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)[0] == "suspected_noop"
    document.url = "file:///changed.html"
    changed = native.retry_evidence(action, button, None)
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, changed)[0] == "unverifiable"


@pytest.mark.parametrize("failure", ["text", "value", "capabilities", "document", "opaque", "subtree"])
def test_native_button_incomplete_scoped_reads_cannot_authorize_retry(readable_native_button, monkeypatch, failure):
    native, action, button, document, Node = readable_native_button
    before = native.retry_evidence(action, button, None)
    assert before is not None
    if failure == "text":
        button.interfaces = ("Text",)
        monkeypatch.setattr(native, "_full_text", lambda node: None)
    elif failure == "value":
        button.interfaces = ("Value",)
    elif failure == "capabilities":
        button.interfaces = None
    elif failure == "document":
        document.get_document_attributes = lambda: None
    elif failure == "opaque":
        Node("", "drawing area", button.parent)
    else:
        for index in range(33):
            Node(str(index), "label", button.parent)
    after = native.retry_evidence(action, button, None)
    assert after is None
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)[0] == "unverifiable"


def test_verified_row_selection_stays_partial_without_retry_advice():
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="linux")
    evidence = "the row was selected and activates on double-click"
    before = outcome.RetryEvidence(effect=("row", "outline_selection", False))
    after = outcome.RetryEvidence(effect=("row", "outline_selection", True))
    runtime._retry_reads = ("geany", Click(None), before, after)
    result = runtime._conclude("clicked", tool="click", app="geany", verdict=("partial", evidence))
    assert result.outcome == "partial"
    assert result.evidence == evidence
    assert result.next == ()


def test_native_button_reads_target_text_without_reading_unrelated_fields(readable_native_button, monkeypatch):
    native, action, button, document, Node = readable_native_button
    button.interfaces = ("Text",)
    Node("Unrelated field", "text", button.parent, interfaces=("Text",))
    calls = []

    def read_text(node):
        assert node is button, "unrelated text is not a required click signal"
        calls.append(node)
        return "Div button"

    monkeypatch.setattr(native, "_full_text", read_text)
    before = native.retry_evidence(action, button, None)
    after = native.retry_evidence(action, button, None)
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)[0] == "suspected_noop"
    assert calls == [button, button]
    Node("New challenge", "section", document)
    changed = native.retry_evidence(action, button, None)
    assert outcome.retry_verdict(("suspected_noop", "unchanged"), before, changed)[0] == "unverifiable"


@pytest.mark.parametrize("selected", [False, True])
def test_native_outline_partial_requires_selected_state(readable_native_button, selected):
    native, _action, button, _document, _Node = readable_native_button
    element = Element("e1", "AXCell", "Symbol", None, None, "s", note="activates on double-click")
    action = Click(element)
    before = native.retry_evidence(action, button, None)
    if selected:
        button.states += ("SELECTED",)
    after = native.retry_evidence(action, button, None)
    verdict = ("partial", "the row was selected and activates on double-click")
    assert outcome.retry_verdict(verdict, before, after)[0] == ("partial" if selected else "unverifiable")
    button.states = None
    assert native.retry_evidence(action, button, None) is None


def test_native_retry_deadline_exhaustion_remains_unknown(readable_native_button, monkeypatch):
    native, action, button, _document, _Node = readable_native_button
    before = native.retry_evidence(action, button, None)
    assert before is not None
    ticks = iter((0.0, 0.041))
    monkeypatch.setattr(native.time, "monotonic", lambda: next(ticks, 0.041))
    after = native.retry_evidence(action, button, None)
    assert after is None
    verdict = outcome.retry_verdict(("suspected_noop", "unchanged"), before, after)
    assert verdict[0] == "unverifiable"
    assert outcome.suggest_next("click", verdict[0]) == ()
