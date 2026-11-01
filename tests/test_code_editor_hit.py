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
    layer = {"monaco-editor": "view-lines", "ace_editor": "ace_content"}[container_cls.split()[0]]
    overlay = N("section", layer, box)
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
    box_b.hit.hit = N("section", "view-line", box_b.hit)
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


@pytest.mark.parametrize("widget", [
    "suggest-widget", "find-widget", "monaco-hover", "parameter-hints-widget",
    "context-view", "overlayWidgets", "contentWidgets",
])
def test_monaco_overlay_widget_div_is_refused(tree, widget):
    # The live suggest-widget hit is a plain div.message, not a control role.
    box, overlay, textarea = _editor(tree, "monaco-editor", "inputarea")
    holder = N("section", widget, box)
    box.hit = N("section", "message", holder)
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False


@pytest.mark.parametrize("widget", ["ace_autocomplete", "ace_search", "ace_tooltip"])
def test_ace_widget_div_is_refused(tree, widget):
    box, overlay, textarea = _editor(tree, "ace_editor", "ace_text-input")
    box.hit = N("section", "", N("section", widget, box))
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False


def test_in_editor_hit_outside_text_layers_is_refused(tree):
    box, overlay, textarea = _editor(tree, "monaco-editor", "inputarea")
    box.hit = N("section", "margin", box)  # gutter, scrollbar, minimap, ...
    assert _atspi.chromium_pointer_target(textarea, 5, 5) is False


class _Live:
    """Shaped like Chrome's ATK node: attributes only via get_attributes()."""

    def __init__(self, role, attrs, parent=None):
        self.role, self.attrs, self.parent = role, attrs, parent

    def get_attributes(self):
        return dict(self.attrs)


def test_detection_from_realistic_chrome_attributes(monkeypatch):
    def chain(acc, limit=None):
        while acc is not None:
            yield acc
            acc = acc.parent
    monkeypatch.setattr(_atspi, "_ancestor_chain", chain)
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    doc = _Live("document web", {"tag": "#document"})
    host = _Live("section", {"tag": "div", "class": "monaco-editor no-user-select showUnused vs",
                             "display": "block", "id": "m"}, doc)
    guard = _Live("section", {"tag": "div", "class": "overflow-guard", "display": "block"}, host)
    textarea = _Live("entry", {
        "tag": "textarea", "class": "inputarea monaco-mouse-cursor-text",
        "xml-roles": "textbox", "autocomplete": "off", "display": "block",
        "text-input-type": "", "placeholder": "",
    }, guard)
    assert _atspi.code_editor_container(textarea) is host
    ace = _Live("section", {"tag": "div", "class": "ace_editor ace_hidpi ace-tm", "display": "block"}, doc)
    ace_input = _Live("entry", {"tag": "textarea", "class": "ace_text-input",
                                "xml-roles": "textbox", "display": "block"}, ace)
    assert _atspi.code_editor_container(ace_input) is ace
    plain = _Live("entry", {"tag": "textarea", "class": "form-control", "display": "block"}, host)
    assert _atspi.code_editor_container(plain) is None


def _driver(monkeypatch, focused, handle):
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _linux_input, linux

    driver = linux.LinuxDriver()
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *a: handle)
    for name in ("_check_modal_target", "_refuse_hidden", "_raise_handle_window",
                 "_visible_action", "_refuse_xtest_password_focus"):
        monkeypatch.setattr(driver, name, lambda *a: None)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(linux, "_accepts_text", lambda el: True)
    monkeypatch.setattr(_atspi, "qt_item_view_cell", lambda h: False)
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda h: False)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda h: focused)
    for name in ("insert_text", "grab_focus", "web_entry_context"):
        monkeypatch.setattr(_atspi, name, lambda *a, n=name: pytest.fail(f"{n} must not run"))
    monkeypatch.setattr(driver, "click", lambda *a, **k: pytest.fail("no pointer click"))
    sent = []
    monkeypatch.setattr(_linux_input, "type_string", sent.append)
    monkeypatch.setattr(_linux_input, "click", lambda *a, **k: pytest.fail("no pointer click"))
    return driver, sent


def _el(role="AXTextArea"):
    from a11y_computer_use.schema import Element
    return Element(ref="e38", role=role, title="Cursor at row 2", value="", bounds=None, snapshot_id=1)


def test_type_ref_focused_code_editor_types_at_caret_unverified(tree, monkeypatch):
    _box, _o, textarea = _editor(tree, "ace_editor", "ace_text-input")
    driver, sent = _driver(monkeypatch, True, textarea)
    assert driver.type_into(_el(), " QQ") == 3
    assert sent == [" QQ"]
    assert driver._type_unverified is True and driver._code_editor_typed is True


def test_type_ref_unfocused_code_editor_refuses_without_click(tree, monkeypatch):
    _box, _o, textarea = _editor(tree, "monaco-editor", "inputarea")
    driver, sent = _driver(monkeypatch, False, textarea)
    with pytest.raises(ComputerUseError) as exc:
        driver.type_into(_el(), "x")
    assert exc.value.detail["reason"] == "code_editor_focus_required"
    assert exc.value.detail["unchanged"] is True
    assert sent == []


def test_click_ref_on_hidden_caret_input_is_refused(tree, monkeypatch):
    _box, _o, textarea = _editor(tree, "ace_editor", "ace_text-input")
    driver, _sent = _driver(monkeypatch, True, textarea)
    with pytest.raises(ComputerUseError) as exc:
        driver._refuse_code_editor_input_click(_el(), textarea)
    assert exc.value.detail["reason"] == "code_editor_hidden_input"
    plain = N("entry", "form-control", tree)
    driver._refuse_code_editor_input_click(_el(), plain)  # no refusal
