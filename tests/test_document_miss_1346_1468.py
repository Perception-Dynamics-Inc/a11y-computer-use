"""#1346 / #1468: Chromium's document hit test answering with the document itself.

The live Tester probe: ``get_accessible_at_point`` on the ``document web``
returned the document node at the field's (or opacity-0 checkbox's)
center. That names no element, so the old walk saw "no cover" (#1346) and
the verifier refused ``hit_test_unverified`` (#1346 covered cases, #1468
checkboxes). A miss is not a cover: the snapshot's own boxes name an
overlay when one holds the point; otherwise the ref's own AT-SPI action
(grab_focus for a field, check for a checkbox) acts and is read back.
No gi import: macOS and Windows collect this file.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import Bounds, ComputerUseError, Element

from tests.test_type_under_overlay_1346 import HEADER_H, ROW_H, N, Page, _element, driver  # noqa: F401


class MissPage(Page):
    """Every document hit test answers with the document itself (the live probe)."""

    def hit(self, comp, x, y):
        if comp is self.doc:
            return self.doc
        return super().hit(comp, x, y)


@pytest.fixture
def miss(driver, monkeypatch):
    focus = {"node": None, "grabs": 0}

    def extents(acc, **_):
        page = driver.page_box["page"]
        if acc is page.doc:
            return (0, 0), (1280, 800)
        node = acc
        while node is not None and node.parent is not page.doc:
            node = node.parent
        if node in (page.header, page.cookie):
            left, top, width, height = page.box(node)
            if acc is not node:  # a control inside the overlay: a small box in it
                return (left + 10, top + 10), (100, 30)
            return (left, top), (width, height)
        if acc in page.rows:
            return (400, page.field_top(acc)), (200, 30)
        if acc.children and acc.children[0] in page.rows:
            return (0, page.field_top(acc.children[0]) - 15), (1280, ROW_H)
        return None, None

    def grab(acc):
        focus["node"], focus["grabs"] = acc, focus["grabs"] + 1
        return True

    monkeypatch.setattr(_atspi, "_extents", extents)
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: name == "SHOWING")
    monkeypatch.setattr(_atspi, "grab_focus", grab)
    monkeypatch.setattr(_atspi, "_focus_on_target", lambda acc: focus["node"] is acc)
    monkeypatch.setattr(_atspi, "_FOCUS_CONFIRM_PAUSE_S", 0.0)
    driver.focus = focus
    return driver


def _handles(page, extra=None):
    held = {f"r{i}": row.parent for i, row in enumerate(page.rows)}
    held.update({f"f{i}": row for i, row in enumerate(page.rows)})
    held["e5"] = page.header
    if page.cookie is not None:
        held["e30"] = page.cookie
        held["e32"] = page.cookie.children[-1]
    held.update(extra or {})
    return held


def test_the_old_walk_saw_no_cover_when_the_document_answers_itself(miss):
    page = MissPage(banner="full")
    miss.page_box["page"] = page
    assert _atspi.chromium_cover_at(page.rows[9], 500, 695) is None
    assert _atspi.chromium_document_miss(page.rows[9], 500, 695) is True


def test_full_viewport_banner_on_a_miss_refuses_covered_by_overlay_naming_e32(miss, monkeypatch):
    page = MissPage(banner="full")
    miss.page_box["page"] = page
    monkeypatch.setitem(observe._HANDLES, "snap-1346", _handles(page))
    with pytest.raises(ComputerUseError) as raised:
        miss._click_clear_of_overlay(_element("Qty 9"), page.rows[9])
    exc = raised.value
    assert exc.detail["reason"] == "covered_by_overlay"
    assert exc.detail["cover"]["control_ref"] == "e32"
    assert "click e32 ('Accept cookies') by ref, or press Escape" in exc.detail["hint"]
    assert miss.clicks == [] and miss.focus["grabs"] == 0


def test_sticky_header_on_a_miss_is_scrolled_clear_then_focused_without_the_pointer(miss, monkeypatch):
    page = MissPage(banner=None)
    miss.page_box["page"] = page
    field = page.rows[24]
    page.set_scroll(HEADER_H + 24 * ROW_H - 100)  # field center under the header
    monkeypatch.setitem(observe._HANDLES, "snap-1346", _handles(page))
    assert miss._click_clear_of_overlay(_element("Qty 24"), field) is True
    assert miss.focus["node"] is field and miss.clicks == []
    top = page.field_top(field)
    assert top >= HEADER_H  # moved out from under the header


def test_uncovered_field_on_a_miss_is_focused_through_its_action(miss, monkeypatch):
    page = MissPage(banner=None)
    miss.page_box["page"] = page
    monkeypatch.setitem(observe._HANDLES, "snap-1346", _handles(page))
    assert miss._click_clear_of_overlay(_element("Qty 3"), page.rows[3]) is True
    assert miss.focus["node"] is page.rows[3] and miss.clicks == [] and page.scroll == 0


def test_a_real_other_node_answer_is_not_a_miss_and_keeps_hit_test_unverified(miss, monkeypatch):
    """A hit on a different element is never turned into an action fallback."""
    page = Page(banner="bottom")  # the document answers with real children
    miss.page_box["page"] = page
    monkeypatch.setitem(observe._HANDLES, "snap-1346", _handles(page))
    el = _element("Qty 3")
    assert miss._click_on_document_miss(el, page.rows[3], 500, 335) is False
    assert miss._geometry_cover(el, page.rows[3], (500, 335)) is None


# --- #1468: opacity-0 checkboxes ------------------------------------------

class Box:
    """A TodoMVC row: an opacity-0 checkbox on top of its drawn circle."""

    def __init__(self):
        self.doc = N("document web", "Todos")
        self.row = N("list item", "", self.doc)
        self.check = N("check box", "", self.row)
        self.label = N("static", "Buy milk", self.row)
        self.checked = False


@pytest.fixture
def todo(driver, monkeypatch):
    box = Box()
    boxes = {id(box.doc): ((0, 0), (1280, 800)), id(box.row): ((300, 280), (680, 40)),
             id(box.check): ((364, 280), (41, 40)), id(box.label): ((424, 286), (84, 25))}
    actions = []
    monkeypatch.setattr(_atspi, "_hit_at", lambda comp, x, y: box.doc)
    monkeypatch.setattr(_atspi, "_extents", lambda acc, **_: boxes[id(acc)])
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: name == "SHOWING")
    monkeypatch.setattr(_atspi, "checked_state", lambda acc: box.checked)
    monkeypatch.setattr(_atspi, "_FOCUS_CONFIRM_PAUSE_S", 0.0)

    def act(acc, names):
        actions.append(acc)
        if box.works and "check" in names:
            box.checked = not box.checked
        return True

    monkeypatch.setattr(_atspi, "_do_action_named", act)
    box.works = True
    monkeypatch.setitem(observe._HANDLES, "snap-1468",
                        {"e34": box.row, "e35": box.check, "e38": box.label})
    driver.box, driver.actions = box, actions
    return driver


def _checkbox() -> Element:
    return Element("e35", "AXCheckBox", "", None, Bounds(0, 364, 280, 41, 40),
                   "snap-1468", clickable=True, enabled=True)


def test_hidden_checkbox_on_a_miss_is_checked_through_its_action_and_read_back(todo):
    assert todo._click_on_document_miss(_checkbox(), todo.box.check, 384, 300) is True
    assert todo.box.checked is True and todo.actions == [todo.box.check]
    assert todo.clicks == []


def test_hidden_checkbox_whose_state_does_not_change_is_unconfirmed(todo):
    todo.box.works = False
    with pytest.raises(ComputerUseError) as raised:
        todo._click_on_document_miss(_checkbox(), todo.box.check, 384, 300)
    assert raised.value.detail["outcome"] == "unconfirmed"
    assert todo.clicks == []


def test_click_tries_the_miss_fallback_before_the_hit_test_refusal():
    import inspect
    from a11y_computer_use.drivers.linux import LinuxDriver

    src = inspect.getsource(LinuxDriver.click)
    assert src.index("_click_on_document_miss") < src.index("_verify_chromium_pointer_target")


def test_click_fallback_is_limited_to_checkable_roles():
    """A button whose document answers itself (a carousel transform) still
    refuses hit_test_unverified after one probe; only checkboxes, radios and
    switches take the action fallback (#1468)."""
    import inspect
    from a11y_computer_use.drivers.linux import LinuxDriver

    src = inspect.getsource(LinuxDriver.click)
    assert "target.role in self._MISS_CHECK_ROLES" in src
