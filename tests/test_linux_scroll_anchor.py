"""Scroll points stay inside the element. A miss is not a scroll."""

from __future__ import annotations

from a11y_computer_use.drivers._linux_scroll_anchor import (
    candidate_points,
    point_for_pid,
    recheck_point,
)
from a11y_computer_use.schema import Bounds, Element


def _box() -> Bounds:
    return Bounds(0, 100, 200, 400, 300)


def test_candidates_stay_inside_the_box() -> None:
    bounds = _box()
    points = candidate_points(bounds)
    assert points[0] == (int(bounds.center.x), int(bounds.center.y))
    for x, y in points:
        assert bounds.x <= x < bounds.x + bounds.width
        assert bounds.y <= y < bounds.y + bounds.height
    assert len(points) >= 2


def test_center_is_used_when_it_matches() -> None:
    bounds = _box()
    center = candidate_points(bounds)[0]

    def pid_at(x: int, y: int) -> int:
        return 5 if (x, y) == center else 9

    assert point_for_pid(bounds, 5, pid_at) == center
    assert recheck_point(bounds, 5, pid_at) is None


def test_inset_is_used_when_the_center_is_another_pid() -> None:
    bounds = _box()
    center = candidate_points(bounds)[0]
    seen: list[tuple[int, int]] = []

    def pid_at(x: int, y: int) -> int:
        seen.append((x, y))
        if (x, y) == center:
            return 9
        return 5

    found = point_for_pid(bounds, 5, pid_at)
    assert found is not None and found != center
    assert found in candidate_points(bounds)
    assert recheck_point(bounds, 5, pid_at) == found
    assert all(
        bounds.x <= x < bounds.x + bounds.width and bounds.y <= y < bounds.y + bounds.height
        for x, y in seen
    )


def test_no_matching_point_is_a_miss() -> None:
    bounds = _box()

    def pid_at(x: int, y: int) -> int:
        del x, y
        return 9

    assert point_for_pid(bounds, 5, pid_at) is None
    assert recheck_point(bounds, 5, pid_at) is None


def test_offscreen_center_is_skipped_when_an_inset_is_on_screen() -> None:
    bounds = Bounds(0, 0, -40, 200, 80)
    screen = (100, 100)

    def pid_at(x: int, y: int) -> int:
        return 5

    found = point_for_pid(bounds, 5, pid_at, screen=screen)
    assert found is not None
    assert 0 <= found[0] < 100 and 0 <= found[1] < 100
    assert found != (int(bounds.center.x), int(bounds.center.y))


def test_driver_scroll_wheels_the_inset_when_the_center_is_another_pid(monkeypatch) -> None:
    from a11y_computer_use.drivers import _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use import observe

    bounds = _box()
    center = (int(bounds.center.x), int(bounds.center.y))
    element = Element("e4", "AXScrollArea", "Form", None, bounds, "snap-1")
    handle = type("Handle", (), {"get_process_id": staticmethod(lambda: 5)})()
    wheeled: list[tuple[int, int]] = []

    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: handle)
    monkeypatch.setattr(
        "a11y_computer_use.drivers._atspi.list_container", lambda _handle: None,
    )
    monkeypatch.setattr(
        "a11y_computer_use.drivers._atspi.list_with_overflow_ancestor", lambda _handle: None,
    )
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._screen_span", lambda: (1280, 800),
    )

    def pid_at(x: int, y: int) -> int:
        return 9 if (int(x), int(y)) == center else 5

    monkeypatch.setattr(_linux_system, "pid_at_point", pid_at)

    def cdp(x: int, y: int, *, dx: int, dy: int) -> bool:
        wheeled.append((int(x), int(y)))
        return True

    monkeypatch.setattr("a11y_computer_use.drivers.linux._cdp_scroll_pixels", cdp)
    monkeypatch.setattr(
        "a11y_computer_use.drivers.linux._settle_shown_names", lambda *_args, **_kwargs: None,
    )
    LinuxDriver().scroll(element, dy=1)
    assert wheeled
    assert wheeled[0] != center
    assert bounds.x <= wheeled[0][0] < bounds.x + bounds.width
    assert bounds.y <= wheeled[0][1] < bounds.y + bounds.height
