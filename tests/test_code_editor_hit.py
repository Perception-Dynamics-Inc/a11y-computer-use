"""Monaco/Ace hidden input textarea hit test and honest type (#1424)."""

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import ComputerUseError


class N:
    def __init__(self, role, cls="", parent=None, hit=None):
        self.role, self.cls, self.parent, self.hit = role, cls, parent, hit


@pytest.fixture
def tree(monkeypatch):
    def chain(acc, limit=None):
        while acc is not None:
            yield acc
            acc = acc.parent
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_ancestor_chain", chain)
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    monkeypatch.setattr(_atspi, "_ancestor_key", lambda n: id(n))
    monkeypatch.setattr(_atspi, "_get_attributes", lambda n: {"class": n.cls})
    monkeypatch.setattr(_atspi, "_component", lambda n: n)
    monkeypatch.setattr(_atspi, "_hit_at", lambda comp, x, y: comp.hit)
    monkeypatch.setattr(_atspi, "_chromium_select_option_at_point", lambda *a: False)
    doc = N("document web")
    return doc


def _editor(doc, container_cls, input_cls):
    box = N("section", container_cls, doc)
    overlay = N("section", "view-overlays", box)
    textarea = N("entry", input_cls, box)
    doc.hit = box
    box.hit = overlay
    return box, overlay, textarea


@pytest.mark.parametrize("container,inp", [
    ("monaco-editor no-user-select", "inputarea monaco-mouse-cursor-text"),
    ("ace_editor ace-tm", "ace_text-input"),
])
def test_hit_on_own_editor_overlay_is_accepted(tree, container, inp):
    _box, _overlay, textarea = _editor(tree, container, inp)
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is True


def test_hit_on_other_editor_is_refused(tree):
    box_a = N("section", "monaco-editor", tree)
    textarea = N("entry", "inputarea", box_a)
    box_b = N("section", "monaco-editor", tree)
    tree.hit = box_b
    box_b.hit = N("section", "view-lines", box_b)
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False


def test_hit_on_widget_inside_editor_is_refused(tree):
    box, overlay, textarea = _editor(tree, "monaco-editor", "inputarea")
    find = N("entry", "input", N("section", "find-widget", box))
    box.hit = find
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False
    button = N("push button", "codicon", box)
    box.hit = button
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False


def test_nested_editor_between_hit_and_container_is_refused(tree):
    outer, _overlay, textarea = _editor(tree, "monaco-editor", "inputarea")
    inner = N("section", "monaco-editor", outer)
    outer.hit = N("section", "view-lines", inner)
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False


def test_plain_textarea_outside_editor_keeps_strict_check(tree):
    box = N("section", "monaco-editor", tree)
    textarea = N("entry", "", box)  # no inputarea token
    tree.hit = box
    box.hit = N("section", "view-lines", box)
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False
    look_alike = N("entry", "inputarea-preview", box)
    assert _atspi.chromium_pointer_target(look_alike, 5, 5) is False


def test_code_editor_input_without_container_is_not_accepted(tree):
    textarea = N("entry", "inputarea", tree)
    tree.hit = N("section", "", tree)
    assert _atspi.code_editor_container(textarea) is None
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False


def test_set_value_on_code_editor_input_is_refused_unchanged(tree):
    _box, _o, textarea = _editor(tree, "ace_editor", "ace_text-input")
    with pytest.raises(ComputerUseError) as exc:
        _atspi._refuse_model_editor_replace(textarea)
    assert exc.value.detail == {"reason": "code_editor_replace_unsafe", "unchanged": True}


def _driver(monkeypatch, focused):
    from a11y_computer_use.drivers import _linux_input, _linux_system, linux

    driver = linux.LinuxDriver()
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(_linux_system, "refuse_unviewable_input", lambda: None)
    monkeypatch.setattr(driver, "_verify_remembered_editable", lambda h: "chrome")
    monkeypatch.setattr(driver, "_refuse_xtest_password_focus", lambda: None)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "is_secure", lambda h: False)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda h: focused)
    monkeypatch.setattr(_atspi, "insert_text",
                        lambda *a: pytest.fail("textarea insert is not the editor model"))
    sent = []
    monkeypatch.setattr(_linux_input, "type_string", sent.append)
    return driver, sent


def test_type_into_code_editor_sends_keys_and_reports_unverified(tree, monkeypatch):
    _box, _o, textarea = _editor(tree, "monaco-editor", "inputarea")
    driver, sent = _driver(monkeypatch, True)
    driver.visible_actions = False
    driver._focused_editable = textarea
    assert driver.type_text("foo(") == 4
    assert sent == ["foo("]
    assert driver._type_unverified is True


def test_type_into_unfocused_code_editor_sends_nothing(tree, monkeypatch):
    _box, _o, textarea = _editor(tree, "ace_editor", "ace_text-input")
    driver, sent = _driver(monkeypatch, False)
    driver.visible_actions = False
    driver._focused_editable = textarea
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text("x")
    assert exc.value.detail["reason"] == "focus_lost"
    assert sent == []
