"""#1346: typing by ref into a page field under a sticky header or cookie banner.

A fake Chromium page: a 1280x800 viewport, a 140 px sticky header at the
top, an optional 130 px fixed cookie banner at the bottom (or one that
covers the whole viewport), and rows of 60 px with a "Qty N" entry.
Hit tests go through the real `chromium_cover_at` walk against a model of
Chromium's cached hit test: the first query at a point is a box-only walk
that returns the LAST child whose box holds the point (so a row listed
after the header or banner wins), and it caches the real paint-order
answer; a later query at a point inside that cached node's box returns it.
That first answer is what made the old single read see no cover on the
live page. No gi import: macOS and Windows collect this file.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.drivers import linux as linux_mod
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element

VIEW_H = 800
HEADER_H = 140
BANNER_H = 130
ROW_H = 60


class N:
    def __init__(self, role, name="", parent=None):
        self.role, self.name, self.parent, self.children = role, name, parent, []
        if parent is not None:
            parent.children.append(self)


class Page:
    def __init__(self, *, banner: str | None, scroll: int = 0):
        self.scroll = scroll
        self.banner_kind = banner
        self.doc = N("document web", "Shop")
        self.header = N("section", "", self.doc)
        N("push button", "Menu", self.header)
        self.cookie = None
        if banner:
            self.cookie = N("section", "", self.doc)
            N("static", "We use cookies", self.cookie)
            N("push button", "Accept cookies", self.cookie)
        self.rows = []
        for index in range(60):
            row = N("section", "", self.doc)
            self.rows.append(N("entry", f"Qty {index}", row))

    def field_top(self, field) -> int:
        index = self.rows.index(field)
        return HEADER_H + index * ROW_H - self.scroll

    def max_scroll(self) -> int:
        return HEADER_H + 60 * ROW_H - VIEW_H

    def set_scroll(self, value: int) -> None:
        self.scroll = max(0, min(self.max_scroll(), int(value)))

    def cover_box(self, y: int):
        """The overlay painted over screen row ``y``, or None."""
        if self.cookie is not None:
            if self.banner_kind == "full":
                return self.cookie
            if y >= VIEW_H - BANNER_H:
                return self.cookie
        if y < HEADER_H:
            return self.header
        return None

    def box(self, node):
        if node is self.header:
            return (0, 0, 1280, HEADER_H)
        if node is self.cookie:
            if self.banner_kind == "full":
                return (0, 0, 1280, VIEW_H)
            return (0, VIEW_H - BANNER_H, 1280, BANNER_H)
        field = node.children[0] if node.children and node.children[0] in self.rows else node
        return (400, self.field_top(field), 200, 30)

    def _inside(self, node, x, y) -> bool:
        left, top, width, height = self.box(node)
        return left <= x < left + width and top <= y < top + height

    def _real_hit(self, x, y):
        cover = self.cover_box(y)
        if cover is not None:
            return cover.children[-1]
        for field in self.rows:
            if self._inside(field, x, y):
                return field
        return None

    def hit(self, comp, x, y):
        if comp is self.doc:
            cached = getattr(self, "cache", None)
            if cached is not None and self._inside(cached.parent if cached.parent is not self.doc else cached, x, y):
                return cached
            # Box-only guess, last child first; the real answer is cached.
            self.cache = self._real_hit(x, y)
            for child in reversed(self.doc.children):
                if self._inside(child, x, y):
                    return child
            return None
        if comp in (self.header, self.cookie):
            return comp.children[-1]
        if comp.role == "section" and comp.children and comp.children[0] in self.rows:
            return comp.children[0]
        return None


@pytest.fixture
def driver(monkeypatch):
    clicks = []
    verified = []

    def chain(acc, limit=None):
        while acc is not None:
            yield acc
            acc = acc.parent

    def bounded(acc, limit=48):
        out, stack = [], [acc]
        while stack and len(out) < limit:
            node = stack.pop(0)
            out.append(node)
            stack.extend(node.children)
        return out

    page_box = {}
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_ancestor_chain", chain)
    monkeypatch.setattr(_atspi, "_role_name", lambda n: n.role)
    monkeypatch.setattr(_atspi, "_node_name", lambda n: n.name)
    monkeypatch.setattr(_atspi, "_ancestor_key", lambda n: id(n))
    monkeypatch.setattr(_atspi, "_component", lambda n: n)
    monkeypatch.setattr(_atspi, "_bounded_descendants", bounded)
    monkeypatch.setattr(_atspi, "_hit_at", lambda comp, x, y: page_box["page"].hit(comp, x, y))

    def extents(acc, **_):
        page = page_box["page"]
        if acc is page.doc:
            return (0, 0), (1280, VIEW_H)
        return (400, page.field_top(acc)), (200, 30)

    def scroll_to(acc):
        page = page_box["page"]
        top = page.field_top(acc)
        if top < 0 or top + 30 > VIEW_H:  # ANYWHERE: only scrolls when off screen
            page.set_scroll(page.scroll + top)
        return True

    def scroll_to_point(acc, x, y):
        page = page_box["page"]
        page.set_scroll(page.scroll + page.field_top(acc) - y)
        return True

    def scroll_to_edge(acc, edge):
        page = page_box["page"]
        top = page.field_top(acc)
        page.set_scroll(page.scroll + (top if edge == "TOP_EDGE" else top + 30 - VIEW_H))
        return True

    monkeypatch.setattr(_atspi, "_extents", extents)
    monkeypatch.setattr(_atspi, "scroll_to", scroll_to)
    monkeypatch.setattr(_atspi, "scroll_to_point", scroll_to_point)
    monkeypatch.setattr(_atspi, "scroll_to_edge", scroll_to_edge)
    monkeypatch.setattr(_linux_input, "click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(linux_mod, "_on_wayland", lambda: False)
    monkeypatch.setattr(LinuxDriver, "_COVER_SETTLE_S", 0.0)
    monkeypatch.setattr(LinuxDriver, "_COVER_READ_GAP_S", 0.0)
    drv = LinuxDriver()
    drv._run = lambda fn: fn()  # type: ignore[method-assign]
    drv._verify_chromium_pointer_target = lambda el, h, x, y: verified.append((x, y))  # type: ignore[method-assign]
    drv.page_box, drv.clicks, drv.verified = page_box, clicks, verified
    return drv


def _element(name: str) -> Element:
    return Element("e7", "AXTextField", name, None, Bounds(0, 400, 150, 200, 30),
                   "snap-1346", clickable=True, enabled=True)


def _uncovered_at_click(page: Page, field, click) -> bool:
    x, y = click
    top = page.field_top(field)
    return page.cover_box(y) is None and top <= y < top + 30


def test_field_under_the_sticky_header_is_scrolled_clear_then_clicked(driver):
    page = Page(banner="bottom", scroll=1500)
    driver.page_box["page"] = page
    field = page.rows[24]
    page.set_scroll(HEADER_H + 24 * ROW_H - 100)  # field at screen y=100
    # The Tester's live failure: one read at the covered point names the field.
    assert _atspi.chromium_cover_at(field, 500, 115) is _atspi.COVER_CLEAR
    # The settled read names the header's control.
    assert driver._cover_now(field)[0] is page.header.children[-1]
    page.cache = None
    assert driver._click_clear_of_overlay(_element("Qty 24"), field) is True
    assert len(driver.clicks) == 1 and driver.verified == driver.clicks
    assert _uncovered_at_click(page, field, driver.clicks[0])


def test_field_under_the_cookie_banner_is_scrolled_clear_then_clicked(driver):
    page = Page(banner="bottom", scroll=0)
    driver.page_box["page"] = page
    field = page.rows[9]  # screen y = 140 + 540 = 680, under the banner
    assert page.cover_box(page.field_top(field) + 15) is page.cookie
    assert driver._click_clear_of_overlay(_element("Qty 9"), field) is True
    assert len(driver.clicks) == 1
    assert _uncovered_at_click(page, field, driver.clicks[0])


def test_uncovered_field_keeps_the_usual_click_path(driver):
    page = Page(banner="bottom", scroll=0)
    driver.page_box["page"] = page
    assert driver._click_clear_of_overlay(_element("Qty 3"), page.rows[3]) is False
    assert driver.clicks == [] and page.scroll == 0


def test_banner_over_the_whole_viewport_refuses_and_names_it(driver):
    page = Page(banner="full", scroll=0)
    driver.page_box["page"] = page
    with pytest.raises(ComputerUseError) as raised:
        driver._click_clear_of_overlay(_element("Qty 9"), page.rows[9])
    exc = raised.value
    assert driver.clicks == [] and driver.verified == []
    assert exc.detail["reason"] == "covered_by_overlay"
    assert exc.detail["outcome"] == "refused"
    assert exc.detail["cover"]["control"] == "Accept cookies"
    assert "We use cookies" in exc.message and "nothing was typed" in exc.message
    hint = exc.detail["hint"]
    assert "'Accept cookies'" in hint and "Escape" in hint
    assert exc.detail["cover"]["control_ref"] is None
    text = (exc.message + " " + hint).lower()
    assert "coordinate" not in text and "x=" not in text and "queued" not in text
    assert "responding" not in text


def test_type_into_uses_the_overlay_clearance_instead_of_the_covered_click():
    import inspect
    source = inspect.getsource(LinuxDriver._type_into_unguarded)
    assert "self._click_clear_of_overlay(element, handle)" in source
    assert source.index("_click_clear_of_overlay") < source.index("self.click(element)")


def test_full_viewport_refusal_names_the_dismiss_buttons_ref(driver, monkeypatch):
    from a11y_computer_use import observe

    page = Page(banner="full", scroll=0)
    driver.page_box["page"] = page
    accept = page.cookie.children[-1]
    monkeypatch.setitem(observe._HANDLES, "snap-1346", {"e9": page.rows[9], "e32": accept})
    with pytest.raises(ComputerUseError) as raised:
        driver._click_clear_of_overlay(_element("Qty 9"), page.rows[9])
    exc = raised.value
    assert exc.detail["cover"]["control_ref"] == "e32"
    assert "click e32 ('Accept cookies') by ref, or press Escape" in exc.detail["hint"]
    assert driver.clicks == []


def test_a_hit_that_never_settles_keeps_the_usual_verified_path(driver, monkeypatch):
    """No settled answer is not a cover: the caller's hit_test_unverified check decides."""
    page = Page(banner="bottom", scroll=0)
    driver.page_box["page"] = page
    answers = iter([_atspi.COVER_CLEAR, page.cookie, _atspi.COVER_CLEAR, page.cookie,
                    _atspi.COVER_CLEAR, page.cookie])
    monkeypatch.setattr(_atspi, "chromium_cover_at", lambda acc, x, y: next(answers))
    assert driver._click_clear_of_overlay(_element("Qty 9"), page.rows[9]) is False
    assert driver.clicks == []
