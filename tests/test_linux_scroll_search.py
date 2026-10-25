"""Search-only Chrome scrolling: bounded reads, real rows, and ordinal navigation."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _linux_input, linux
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, Scope, Snapshot


def shot(head=1, prefix="Open "):
    anchor = Element("list", "AXList", "Virtual rows", None, Bounds(0, 10, 100, 520, 420), "s")
    rows = tuple(Element(f"r{i}", "AXButton", f"{prefix}{i}", None,
                         Bounds(0, 10, 100 + (i-head)*30, 100, 30), "s", parent="list")
                 for i in range(head, head+14))
    return Snapshot("s", Scope.WINDOW, "chrome", 1, 0, (), (anchor, *rows))


@pytest.mark.parametrize("head,text,dy,expected", [
    (1, "Open 5000", 5, 24),
    (5000, "Open 20", 5, -24),
    (1, "Open 20", 5, 1),
    (1, "missing", -5, -2),
])
def test_bursts_use_observed_viewport_and_ordinal_distance(head, text, dy, expected):
    s = shot(head)
    assert linux._search_notches(s, s.elements[0], text, dy, (10, 100, 520, 420)) == expected


def test_unrelated_or_inconsistent_ordinals_do_not_accelerate():
    s = shot()
    unrelated = replace(s, elements=(s.elements[0], *(replace(e, parent="other") for e in s.elements[1:])))
    assert linux._search_notches(unrelated, s.elements[0], "Open 5000", 5, (10, 100, 520, 420)) == 2
    shuffled = replace(s, elements=(*s.elements[:-1], replace(s.elements[-1], bounds=Bounds(0, 10, 101, 100, 30))))
    assert linux._search_notches(shuffled, s.elements[0], "Open 5000", 5, (10, 100, 520, 420)) == 2


def setup_step(monkeypatch, samples):
    s = shot()
    handle = SimpleNamespace(__gpointer__=1)
    driver = linux.LinuxDriver.__new__(linux.LinuxDriver)
    driver._run = lambda fn: fn()
    driver._visible_action = lambda *a: None
    driver._raise_handle_window = lambda *a: None
    monkeypatch.setattr(linux, "_on_wayland", lambda: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *a: handle)
    monkeypatch.setattr(_atspi, "list_container", lambda h: handle)
    monkeypatch.setattr(_atspi, "scroll_container_kind", lambda h: "overflow")
    monkeypatch.setattr(_atspi, "_viewport_edges", lambda h: (10, 100, 530, 520))
    calls = []
    values = iter(samples)
    def read(h):
        calls.append("read")
        return next(values)
    monkeypatch.setattr(_atspi, "_in_view_named_rows", read)
    monkeypatch.setattr(_atspi, "row_names", lambda rows: tuple(rows))
    monkeypatch.setattr(_atspi, "commit_shown_rows", lambda h, rows: calls.append(("commit", rows)))
    monkeypatch.setattr(_linux_input, "scroll", lambda x, y, **kw: calls.append(("wheel", x, y, kw)))
    monkeypatch.setattr(linux, "_grab_region", lambda b: "pixels")
    clock = [0.0]
    monkeypatch.setattr(linux.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(linux.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    # Any return to the legacy fallback or whole-tree work is a regression.
    monkeypatch.setattr(_atspi, "wait_for_shown_rows", lambda *a: pytest.fail("legacy wait"))
    monkeypatch.setattr(linux, "_list_pixels_moved", lambda *a: pytest.fail("pixel polling"))
    driver.snapshot = lambda *a: pytest.fail("whole-tree snapshot inside a step")
    return driver, s, calls


def test_moving_step_has_one_baseline_and_two_confirmation_reads(monkeypatch):
    driver, s, calls = setup_step(monkeypatch, [("Row 1", "Row 2"), ("Row 90", "Row 91"), ("Row 90", "Row 91")])
    assert driver.scroll_search_step(s.elements[0], snapshot=s, text="Open 5000", dy=5)
    assert calls.count("read") == 3
    assert ("wheel", 270, 310, {"dy": 24, "notch_interval_s": 0.008}) in calls
    assert calls[-1] == ("commit", ("Row 90", "Row 91"))


@pytest.mark.parametrize("pixels_changed,reason", [(False, "page_unchanged"), (True, "rows_stale")])
def test_stale_rows_never_commit_or_report_movement(monkeypatch, pixels_changed, reason):
    driver, s, calls = setup_step(monkeypatch, [("Row 1", "Row 2")] * 30)
    monkeypatch.setattr(linux, "_region_mean_change", lambda *a: 10 if pixels_changed else 0)
    with pytest.raises(ComputerUseError) as error:
        driver.scroll_search_step(s.elements[0], snapshot=s, text="Open 5000", dy=5)
    assert error.value.detail["reason"] == reason
    assert not any(isinstance(c, tuple) and c[0] == "commit" for c in calls)


def test_unknown_viewport_does_not_dispatch_search_wheel(monkeypatch):
    driver, s, calls = setup_step(monkeypatch, [])
    monkeypatch.setattr(_atspi, "_viewport_edges", lambda h: None)
    assert not driver.scroll_search_step(s.elements[0], snapshot=s, text="Open 5000", dy=5)
    assert calls == []
