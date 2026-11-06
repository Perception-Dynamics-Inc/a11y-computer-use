"""#1470 ?sib: a menu drawn as its button's sibling. Every native hit test at
the item's point lands on the editor under the menu (entry "Document"), so
re-asking cannot help. The item is pressed through its own AT-SPI action
only when its menu is the single open menu, the hit is outside that menu,
and the action exists; no pointer click is ever sent at that point.
Hermetic: the AT-SPI tree and the native hit test are fakes."""
import inspect
import json

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode
from tests.test_linux_menu_portal_owner_1470 import INSIDE, _tree, _wire
from tests.test_linux_synthetic import _GeckoNode


NATIVE_DO_ACTION_NAMED = _atspi._do_action_named


def _item(role="AXMenuItemRadio"):
    return Element(ref="e42", role=role, title="Mono", value=None, snapshot_id="s1",
                   bounds=Bounds(display_id=1, x=392, y=282, width=69, height=31))


@pytest.fixture(params=["portal", "sibling"])
def page(monkeypatch, request):
    """The live shape: one open menu; the hit names entry "Document"."""
    app, document, button, menu, mono, rects = _tree()
    if request.param == "sibling":
        document.children.remove(menu)
        button.parent.children.append(menu)
        menu.parent = button.parent
    editor = _GeckoNode("entry", "Document", ("SHOWING", "VISIBLE", "EDITABLE"))
    document.children.insert(1, editor)
    editor.parent = document
    _wire(monkeypatch, app, rects)
    hit = {"node": editor}
    monkeypatch.setattr(_atspi, "_component", lambda node: node)
    monkeypatch.setattr(_atspi, "_hit_at", lambda comp, x, y: hit["node"])
    monkeypatch.setattr(_atspi, "code_editor_container", lambda acc: None)
    monkeypatch.setattr(_atspi, "_chromium_select_option_at_point", lambda *a: False)
    pressed = []

    def act(acc, names):
        if not any(name.lower() in names for name in getattr(acc, "actions", ())):
            return False
        pressed.append((acc, names))
        return True

    monkeypatch.setattr(_atspi, "_do_action_named", act)
    driver = LinuxDriver()
    driver._run = lambda fn: fn()  # type: ignore[method-assign]
    return {"driver": driver, "mono": mono, "menu": menu, "document": document,
            "editor": editor, "hit": hit, "pressed": pressed}


def test_the_live_hit_names_the_editor_under_the_menu(page):
    trace = []
    assert _atspi.chromium_pointer_target(page["mono"], *INSIDE, trace=trace) is False
    assert trace[0]["hit"] == {"role": "entry", "name": "Document"}


def test_a_document_entry_hit_presses_the_item_by_its_own_action(page):
    pressed = page["driver"]._verify_chromium_pointer_target(
        _item(), page["mono"], *INSIDE, may_press=True)
    assert pressed is True
    assert [acc for acc, _ in page["pressed"]] == [page["mono"]]


def test_without_may_press_the_same_hit_refuses(page):
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(_item(), page["mono"], *INSIDE)
    assert raised.value.detail["reason"] == "hit_test_unverified" and not page["pressed"]


def test_a_hit_on_another_item_inside_the_menu_refuses(page):
    page["hit"]["node"] = page["menu"].children[0]  # "Sans"
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(
            _item(), page["mono"], *INSIDE, may_press=True)
    assert raised.value.detail["reason"] == "hit_test_unverified" and not page["pressed"]


def test_an_item_without_its_own_action_refuses(page):
    page["mono"].actions = []
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(
            _item(), page["mono"], *INSIDE, may_press=True)
    assert raised.value.detail["reason"] == "hit_test_unverified"


def test_two_open_menus_refuse_before_any_press(page, monkeypatch):
    second = _GeckoNode("menu", "Actions", ("SHOWING", "VISIBLE"),
                        [_GeckoNode("menu item", "Delete", ("SHOWING", "VISIBLE"))])
    page["document"].children.append(second)
    second.parent = page["document"]
    assert _atspi.chromium_sole_open_menu(page["mono"], *INSIDE) is None
    with pytest.raises(ComputerUseError):
        page["driver"]._verify_chromium_pointer_target(
            _item(), page["mono"], *INSIDE, may_press=True)
    assert not page["pressed"]


def test_a_non_menu_target_is_never_pressed(page):
    button = Element(ref="e7", role="AXButton", title="Mono", value=None, snapshot_id="s1",
                     bounds=Bounds(display_id=1, x=392, y=282, width=69, height=31))
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(button, page["mono"], *INSIDE, may_press=True)
    assert raised.value.detail["reason"] == "hit_test_unverified" and not page["pressed"]


def test_the_sole_open_menu_is_the_targets_own(page):
    assert _atspi.chromium_sole_open_menu(page["mono"], *INSIDE) is page["menu"]


def test_click_sends_no_pointer_after_an_action_press():
    src = inspect.getsource(LinuxDriver.click)
    verify = src.index("_verify_chromium_pointer_target")
    tail = src[verify:]
    assert "may_press=" in tail[:400] and "return None" in tail[:600]
    assert tail.index("return None") < tail.index("_linux_input.click(")


def test_the_open_menu_rule_still_decides_before_any_hit_test(monkeypatch):
    driver = LinuxDriver()
    driver._run = lambda fn: fn()  # type: ignore[method-assign]
    blocked = ComputerUseError(ErrorCode.STALE_REF, "covered", detail={"reason": "occluded_target"})
    calls = []

    def occluder(*a, **k):
        raise blocked

    monkeypatch.setattr(driver, "_refuse_chromium_occluded", occluder)
    monkeypatch.setattr(_atspi, "chromium_pointer_target",
                        lambda *a, **k: calls.append("hit") or True)
    with pytest.raises(ComputerUseError) as raised:
        driver._verify_chromium_pointer_target(_item(), object(), 140, 210, may_press=True)
    assert raised.value.detail["reason"] == "occluded_target" and not calls


def test_debug_flag_dumps_the_walk_and_the_press(page, monkeypatch, capsys):
    monkeypatch.setenv("A11Y_DEBUG_HIT_TEST", "1")
    page["driver"]._verify_chromium_pointer_target(_item(), page["mono"], *INSIDE, may_press=True)
    line = next(l for l in capsys.readouterr().err.splitlines() if l.startswith("hit_test_debug: "))
    data = json.loads(line.split(": ", 1)[1])
    assert data["verified"] is False and data["pressed_by_action"] is True
    assert data["walk"][0]["hit"] == {"role": "entry", "name": "Document"}


def test_no_debug_output_without_the_flag(page, monkeypatch, capsys):
    monkeypatch.delenv("A11Y_DEBUG_HIT_TEST", raising=False)
    page["driver"]._verify_chromium_pointer_target(_item(), page["mono"], *INSIDE, may_press=True)
    assert "hit_test_debug" not in capsys.readouterr().err


def test_a_second_menu_away_from_the_point_still_blocks_the_press(page, monkeypatch):
    # A second showing menu elsewhere on the page is never the target's.
    away = _GeckoNode("menu", "Other", ("SHOWING", "VISIBLE"))
    page["document"].children.append(away)
    away.parent = page["document"]
    real = _atspi._raw_rect
    monkeypatch.setattr(_atspi, "_raw_rect",
                        lambda node, coord: (1000, 700, 50, 50) if node is away else real(node, coord))
    assert _atspi.chromium_sole_open_menu(page["mono"], *INSIDE) is None


@pytest.mark.parametrize("action_name", ["click", "Click", "Press", "activate", "check", "select"])
@pytest.mark.parametrize("hit_kind", ["entry", "document"])
@pytest.mark.parametrize("item_role", ["AXMenuItem", "AXMenuItemRadio", "AXMenuItemCheckbox"])
def test_native_menu_action_changes_page_font_without_pointer_input(
    page, monkeypatch, action_name, hit_kind, item_role,
):
    """Use the actual action-name matcher, including radio-menu ``check``."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _linux_input, linux

    # Undo only the fixture's action stub: exercise the actual AT-SPI action
    # interface and matcher. All window/native input dependencies stay fake.
    monkeypatch.setattr(_atspi, "_do_action_named", NATIVE_DO_ACTION_NAMED)
    node = page["mono"]
    node.actions = ["showContextMenu", action_name]
    state = {"font": "Sans"}
    dispatched = []

    def do_action(index):
        dispatched.append((node, index))
        state["font"] = "Mono"
        return True

    monkeypatch.setattr(node, "do_action", do_action)
    if hit_kind == "document":
        page["hit"]["node"] = page["document"]
    driver = page["driver"]
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_: node)
    for name in ("_check_modal_target", "_verify_chromium_row_target",
                 "_refuse_calendar_click", "_refuse_expander_click", "_refuse_gnumeric_go_jump",
                 "_refuse_unverified_pdf_thumbnail", "_refuse_hidden", "_raise_handle_window",
                 "_visible_action", "_refuse_code_editor_input_click"):
        monkeypatch.setattr(driver, name, lambda *a, **kw: None)
    monkeypatch.setattr(driver, "_gtk4_point_untrusted", lambda *a: False)
    for name in ("notification_window", "reveal_unpainted_table_click",
                 "gtk4_in_window_dialog", "gtk4_window_origin_untrusted",
                 "is_libreoffice_shape_text", "place_paragraph_caret", "libreoffice_app",
                 "geany_symbol_destination"):
        monkeypatch.setattr(_atspi, name, lambda *a: None)
    pointer_events = []
    monkeypatch.setattr(_linux_input, "click", lambda *a, **kw: pointer_events.append((a, kw)))

    from a11y_computer_use.schema import MouseButton

    for options in ({"button": MouseButton.RIGHT}, {"count": 2}, {"modifiers": ("ctrl",)}):
        with pytest.raises(ComputerUseError) as raised:
            driver.click(_item(item_role), **options)
        assert raised.value.detail["reason"] == "hit_test_unverified"
        assert state["font"] == "Sans"
        assert dispatched == [] and pointer_events == []

    driver.click(_item(item_role))

    assert state["font"] == "Mono"
    assert dispatched == [(node, 1)]
    assert pointer_events == []


@pytest.mark.parametrize("action_name", ["showContextMenu", "clickAncestor", "expand", ""])
def test_an_unrelated_native_action_refuses(page, monkeypatch, action_name):
    monkeypatch.setattr(_atspi, "_do_action_named", NATIVE_DO_ACTION_NAMED)
    page["mono"].actions = [action_name]
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(
            _item(), page["mono"], *INSIDE, may_press=True)
    assert raised.value.detail["reason"] == "hit_test_unverified"
    assert not page["mono"].pressed


def test_native_action_failure_refuses_without_pointer_fallback(page, monkeypatch):
    monkeypatch.setattr(_atspi, "_do_action_named", NATIVE_DO_ACTION_NAMED)
    monkeypatch.setattr(page["mono"], "do_action", lambda index: False)
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(
            _item(), page["mono"], *INSIDE, may_press=True)
    assert raised.value.detail["reason"] == "hit_test_unverified"


def test_a_foreign_button_hit_is_not_a_document_entry_miss(page):
    button = _GeckoNode("push button", "Other", ("SHOWING", "VISIBLE"))
    button.parent = page["document"]
    page["hit"]["node"] = button
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(
            _item(), page["mono"], *INSIDE, may_press=True)
    assert raised.value.detail["reason"] == "hit_test_unverified"
    assert not page["pressed"]


def test_debug_records_each_action_gate(page, monkeypatch, capsys):
    monkeypatch.setenv("A11Y_DEBUG_HIT_TEST", "1")
    page["driver"]._verify_chromium_pointer_target(_item(), page["mono"], *INSIDE, may_press=True)
    line = capsys.readouterr().err.strip()
    gates = json.loads(line.split(": ", 1)[1])["menu_action_gates"]
    for name in ("plain_left_click", "menu_item", "target_missed", "sole_open_menu",
                 "hit_outside_menu", "document_entry_miss", "own_action_available",
                 "action_succeeded", "windows_known", "page_readable",
                 "at_most_one_opener", "no_foreign_menu", "one_own_showing_menu"):
        assert gates[name] is True, (name, gates)
    assert gates["actions"] == ["check"]


def test_unreadable_page_gate_refuses_and_is_reported(page, monkeypatch, capsys):
    monkeypatch.setenv("A11Y_DEBUG_HIT_TEST", "1")
    original = _atspi._occluder_children
    monkeypatch.setattr(_atspi, "_occluder_children",
                        lambda node, cap: None if node is page["editor"] else original(node, cap))
    gates = {}
    assert page["driver"]._press_menu_item_off_hit(page["mono"], *INSIDE, gates=gates) is False
    assert gates["page_readable"] is False
    assert gates["sole_open_menu"] is False
    assert gates["action_succeeded"] is False
    assert not page["pressed"]


@pytest.mark.parametrize("hit_kind", ["missing", "other_document", "nested_document", "inside_menu"])
def test_background_miss_requires_a_readable_hit_outside_menu_in_same_document(page, hit_kind):
    hit = None
    if hit_kind != "missing":
        hit = _GeckoNode("entry", "Document", ("SHOWING", "VISIBLE"))
        if hit_kind == "inside_menu":
            hit.parent = page["menu"]
        else:
            document = _GeckoNode("document web", "Other page", ("SHOWING", "VISIBLE"))
            hit.parent = document
            if hit_kind == "nested_document":
                document.parent = page["document"]
    page["hit"]["node"] = hit
    with pytest.raises(ComputerUseError) as raised:
        page["driver"]._verify_chromium_pointer_target(
            _item(), page["mono"], *INSIDE, may_press=True)
    assert raised.value.detail["reason"] == "hit_test_unverified"
    assert not page["pressed"]
