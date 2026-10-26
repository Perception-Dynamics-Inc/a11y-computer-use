"""Evince 48 page visibility: native SHOWING and Text ranges, not page boxes."""
from types import SimpleNamespace
import os
import shutil
import subprocess
import sys
import time

import pytest

from a11y_computer_use import observe, safety, server
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import Scope
from tests.test_evince_page_text import (
    _Node, _Text, _FakeAccessor, _geometry, _pdf, _snap, fake_atspi,
)


def _tree(monkeypatch, showing=4):
    app = _Node("application", None, name="org.gnome.Evince")
    viewport = _Node("scroll pane", None, box=((0., 0.), (600., 600.)))
    pages = []
    for number in range(1, 13):
        text = f"PAGE {number:02} HEADER\n" + (
            "NEEDLE ZEBRA QUOKKA" if number == 9 else f"Body {number}"
        )
        page = _Node("page", text, box=((529., number * 2500.), (1097., 1420.)))
        page.number = number
        page.get_application = lambda: app
        page.get_parent = lambda: viewport
        page.get_state_set = lambda page=page: SimpleNamespace(
            names={"SHOWING", "VISIBLE"} if page.number == viewport.showing else {"VISIBLE"})
        pages.append(page)
    viewport.showing = showing
    viewport.get_application = lambda: app
    viewport.get_child_count = lambda: len(pages)
    viewport.get_child_at_index = lambda i: pages[i]
    def extents(page, start, end, coord):
        # Only the showing page has text in the viewport. Its page box is wrong.
        return SimpleNamespace(x=40, y=80 if page.number == viewport.showing else 3000,
                               width=240, height=40)
    monkeypatch.setattr(_Text, "get_range_extents", staticmethod(extents), raising=False)
    return viewport, pages


@pytest.mark.parametrize("name", ["evince", "org.gnome.Evince", "ORG.GNOME.EVINCE"])
def test_evince_names_share_page_detection(fake_atspi, name):
    page = _Node("page", "text")
    page.get_application = lambda: _Node("application", None, name=name)
    assert _atspi._evince_page(page)


def test_showing_page_uses_text_bounds_and_omits_hidden_pages(fake_atspi, monkeypatch):
    viewport, pages = _tree(monkeypatch)
    accessor = _atspi.ATSPIAccessor()
    assert accessor.children(viewport) == [pages[3]]
    raw = accessor.read(pages[3])
    assert raw.position == (40, 80) and raw.size == (240, 40)
    snap = _snap(raw)
    match, = observe.find_elements(snap, text="PAGE 04 HEADER")
    assert match.bounds is not None
    assert "off-screen" not in observe.render_matches(snap, observe.find_elements(snap, text="PAGE 04 HEADER"))
    assert not observe.find_elements(snap, text="PAGE 02 HEADER")


def test_partial_page_only_indexes_lines_inside_text_viewport(fake_atspi, monkeypatch):
    viewport, pages = _tree(monkeypatch, showing=9)
    def extent(page, start, end, coord):
        return SimpleNamespace(x=40, y=80 if start else -100,
                               width=240, height=220 if end == len(page.text) and not start else 20)
    monkeypatch.setattr(_Text, "get_range_extents", staticmethod(extent))
    raw = _atspi.ATSPIAccessor().read(pages[8])
    snap = _snap(raw)
    assert observe.find_elements(snap, text="NEEDLE ZEBRA QUOKKA")
    assert not observe.find_elements(snap, text="PAGE 09 HEADER")
    assert raw.position == (40, 80) and raw.size == (240, 20)


def test_missing_range_keeps_honest_offscreen_fallback(fake_atspi, monkeypatch):
    viewport, pages = _tree(monkeypatch)
    monkeypatch.setattr(_Text, "get_range_extents", staticmethod(lambda *args: None))
    raw = _atspi.ATSPIAccessor().read(pages[3])
    assert raw.position is None and raw.unallocated
    snap = _snap(raw)
    assert "off-screen" in observe.render_matches(snap, observe.find_elements(snap, text="PAGE 04 HEADER"))


@pytest.mark.parametrize("query, direction, start", [
    ("NEEDLE ZEBRA QUOKKA", "down", 1), ("absent token", "down", 1),
    ("absent token", "up", 12),
])
def test_scroll_search_reaches_nine_and_stops_at_ends(fake_atspi, monkeypatch, query, direction, start):
    viewport, pages = _tree(monkeypatch, start)
    accessor = _atspi.ATSPIAccessor()
    scrolls = []
    bar = SimpleNamespace(value=float((start - 1) * 480))
    monkeypatch.setattr(_atspi, "_viewport_vertical_bar", lambda node: bar)
    monkeypatch.setattr(_atspi, "_bar_axis", lambda node: "vertical")
    monkeypatch.setattr(_atspi, "_read_value", lambda node: node.value)
    monkeypatch.setattr(_atspi, "_value_bound", lambda node, names, fallback:
                        0. if "minimum" in names[0] else 5280.)
    def write(node, value):
        node.value = max(0., min(5280., value))
        viewport.showing = int(node.value / 480) + 1
    monkeypatch.setattr(_atspi, "_write_value", write)
    monkeypatch.setattr(_atspi, "refuse_outside_modal", lambda node: None)
    class Driver:
        def ensure_trusted(self):
            pass
        def snapshot(self, scope, app):
            root = (observe.RawNode(role="AXWindow", position=(0., 0.), size=(600., 600.)), [
                (observe.RawNode(role="AXScrollArea", position=(0., 0.), size=(600., 600.)),
                 [(accessor.read(p), []) for p in accessor.children(viewport)])])
            return observe.build_snapshot(root, _FakeAccessor(), scope=Scope.WINDOW,
                                          app="evince", pid=os.getpid(), geometry=_geometry())
        def scroll(self, target, **kw):
            step = 1 if kw["dy"] > 0 else -1
            scrolls.append(step)
            moved = _atspi.evince_search_scroll(viewport, kw["dy"])
            if moved is False:
                from a11y_computer_use.schema import ComputerUseError, ErrorCode
                raise ComputerUseError(ErrorCode.UNSUPPORTED, "end", detail={
                    "reason": "page_unchanged", "surface": "document"})
        def scroll_offset(self, target):
            return bar.value
    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = Driver()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kw: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "evince"))
    out = rt.scroll_to_find("evince", text=query, direction=direction, max_scrolls=40)
    if query.startswith("NEEDLE"):
        assert out.startswith("found after 8 scroll(s)"), out
        assert viewport.showing == 9
        assert "off-screen" not in out
    else:
        assert out.startswith("not found"), out
        assert viewport.showing == (12 if direction == "down" else 1)
        assert len(scrolls) < 40
    assert all(step == (1 if direction == "down" else -1) for step in scrolls)


def test_live_evince_twelve_page_search(tmp_path):
    if not sys.platform.startswith("linux") or not shutil.which("evince"):
        pytest.skip("evince is not installed")
    if not os.environ.get("DISPLAY"):
        pytest.skip("private X11 display required")
    from a11y_computer_use.drivers.linux import LinuxDriver
    driver = LinuxDriver()
    driver.ensure_trusted()
    pages = [[f"PAGE {n:02} HEADER"] + [
        "NEEDLE ZEBRA QUOKKA on page nine" if n == 9 and line == 21
        else "ALPHA MARKER four" if n == 4 and line == 6
        else f"Line {line:02} of page {n:02} filler text"
        for line in range(1, 31)] for n in range(1, 13)]
    path = tmp_path / "twelve-pages.pdf"
    path.write_bytes(_pdf(pages))
    proc = subprocess.Popen(["evince", str(path)])
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                snap = driver.snapshot(Scope.WINDOW, "evince")
                if observe.find_elements(snap, text="PAGE 01 HEADER"):
                    break
            except Exception:
                if proc.poll() is not None:
                    raise
            time.sleep(.2)
        else:
            pytest.fail("Evince did not expose page one")
        from a11y_computer_use.drivers import _linux_input
        # Explicit fixture navigation, separate from the search under test.
        for page, marker in ((4, "ALPHA MARKER four"), (1, "PAGE 01 HEADER")):
            _linux_input.press_chord("ctrl+l")
            _linux_input.type_string(str(page))
            _linux_input.press_chord("Return")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                snap = driver.snapshot(Scope.WINDOW, "evince")
                matches = observe.find_elements(snap, text=marker)
                if matches and all(el.bounds is not None for el in matches):
                    break
                time.sleep(.2)
            else:
                pytest.fail(f"Page {page} text did not become visible")
            assert "off-screen" not in observe.render_matches(snap, matches)
        store = safety.PermissionStore(tmp_path / "permissions.json")
        store.set_tier("evince", safety.Tier.FULL)
        with server.Runtime(driver=driver, store=store, audit=safety.AuditLog(tmp_path / "audit")) as rt:
            out = rt.scroll_to_find("evince", text="NEEDLE ZEBRA QUOKKA", max_scrolls=40)
            assert out.startswith("found after"), out
            assert "off-screen" not in out
            snap = driver.snapshot(Scope.WINDOW, "evince")
            assert observe.find_elements(snap, text="NEEDLE ZEBRA QUOKKA")
            for direction in ("down", "up"):
                out = rt.scroll_to_find("evince", text="NONEXISTENT QUOKKA", direction=direction,
                                        max_scrolls=40)
                assert out.startswith("not found"), out
                assert "after 40 scroll(s)" not in out
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("start, dy, expected", [(0, 5, 480), (480, -5, 0), (1000, 5, 1000), (0, -5, 0)])
def test_evince_value_search_step_reads_back_and_stops(fake_atspi, monkeypatch, start, dy, expected):
    viewport, _ = _tree(monkeypatch)
    bar = SimpleNamespace(value=float(start))
    monkeypatch.setattr(_atspi, "_viewport_vertical_bar", lambda node: bar)
    monkeypatch.setattr(_atspi, "_bar_axis", lambda node: "vertical")
    monkeypatch.setattr(_atspi, "_read_value", lambda node: node.value)
    monkeypatch.setattr(_atspi, "_value_bound", lambda node, names, fallback:
                        0. if "minimum" in names[0] else 1000.)
    monkeypatch.setattr(_atspi, "_write_value", lambda node, value:
                        setattr(node, "value", max(0., min(1000., value))))
    checked = []
    monkeypatch.setattr(_atspi, "refuse_outside_modal", lambda node: checked.append(node))
    assert _atspi.evince_search_scroll(viewport, dy) is (start != expected)
    assert bar.value == expected
    assert checked == [viewport]


def test_evince_ignored_value_is_not_a_document_end(fake_atspi, monkeypatch):
    from a11y_computer_use.schema import ComputerUseError
    viewport, _ = _tree(monkeypatch)
    bar = object()
    monkeypatch.setattr(_atspi, "_viewport_vertical_bar", lambda node: bar)
    monkeypatch.setattr(_atspi, "_bar_range", lambda node: (500., 0., 1000.))
    monkeypatch.setattr(_atspi, "_nudge_scrollbar", lambda *args, **kw: False)
    monkeypatch.setattr(_atspi, "_read_value", lambda node: 500.)
    monkeypatch.setattr(_atspi, "refuse_outside_modal", lambda node: None)
    with pytest.raises(ComputerUseError, match="could not be verified"):
        _atspi.evince_search_scroll(viewport, 5)


def test_linux_search_uses_evince_value_before_wheel(fake_atspi, monkeypatch):
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ComputerUseError
    viewport, _ = _tree(monkeypatch)
    snap = _snap(observe.RawNode(role="AXScrollArea", position=(0., 0.), size=(600., 600.)))
    target = next(el for el in snap.elements if el.role == "AXScrollArea")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: viewport)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_raise_handle_window", lambda handle: None)
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(_atspi, "evince_search_scroll", lambda node, dy: True)
    assert driver.scroll_search_step(target, snapshot=snap, text="NEEDLE", dy=5)
    monkeypatch.setattr(_atspi, "evince_search_scroll", lambda node, dy: False)
    with pytest.raises(ComputerUseError) as caught:
        driver.scroll_search_step(target, snapshot=snap, text="absent", dy=5)
    assert caught.value.detail == {"reason": "page_unchanged", "surface": "document"}


def test_hidden_evince_page_never_reuses_on_screen_page_box(fake_atspi, monkeypatch):
    viewport, pages = _tree(monkeypatch)
    pages[1].box = ((40., 80.), (240., 40.))
    raw = _atspi.ATSPIAccessor().read(pages[1])
    assert raw.position is None and raw.size is None
    snap = _snap(raw)
    matches = observe.find_elements(snap, text="PAGE 02 HEADER")
    assert "off-screen" in observe.render_matches(snap, matches)
