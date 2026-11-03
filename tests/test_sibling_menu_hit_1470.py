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


def _item():
    return Element(ref="e42", role="AXMenuItemRadio", title="Mono", value=None, snapshot_id="s1",
                   bounds=Bounds(display_id=1, x=392, y=282, width=69, height=31))


@pytest.fixture
def page(monkeypatch):
    """The live shape: one open menu; the hit names entry "Document"."""
    app, document, button, menu, mono, rects = _tree()
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
        if not getattr(acc, "actions", None):
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
