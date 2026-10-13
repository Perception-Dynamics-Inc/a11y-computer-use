"""Pick a scroll point that stays inside an element and on its process.

The center of a tall scroll area can land on another window, such as a
panel that overlaps the box. These helpers only return points that are
inside the element's own rectangle. A miss is None. Callers do not treat
that as a successful scroll.
"""

from __future__ import annotations

from collections.abc import Callable

from a11y_computer_use.schema import Bounds

#: How far inside an edge the alternate points sit, in physical pixels.
_INSET = 12


def candidate_points(bounds: Bounds) -> tuple[tuple[int, int], ...]:
    """Points inside ``bounds``, center first.

    Later points sit ``_INSET`` pixels in from the top, bottom, left, and
    right, on the center of the other axis. A point that would fall outside
    the box, or that repeats the center, is omitted. A box too small for
    the inset yields the center only.
    """
    left = int(bounds.x)
    top = int(bounds.y)
    width = int(bounds.width)
    height = int(bounds.height)
    right = left + width
    bottom = top + height
    cx = left + width // 2
    cy = top + height // 2
    points: list[tuple[int, int]] = [(cx, cy)]
    for x, y in (
        (cx, top + _INSET),
        (cx, bottom - 1 - _INSET),
        (left + _INSET, cy),
        (right - 1 - _INSET, cy),
    ):
        if (x, y) == (cx, cy):
            continue
        if left <= x < right and top <= y < bottom and (x, y) not in points:
            points.append((x, y))
    return tuple(points)


def _on_screen(x: int, y: int, screen: tuple[int, int] | None) -> bool:
    if screen is None:
        return True
    width, height = screen
    if width <= 0 or height <= 0:
        return True
    return 0 <= x < int(width) and 0 <= y < int(height)


def point_for_pid(
    bounds: Bounds,
    pid: int,
    pid_at: Callable[[int, int], int | None],
    *,
    screen: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    """The first candidate whose ``pid_at`` equals ``pid``, or None.

    ``pid_at`` is ``(x, y) -> pid``. A candidate outside ``screen``
    ``(width, height)`` is skipped. ``screen`` None does not filter.
    An exception from ``pid_at`` is a miss, not a guess. None means no
    point inside the box belongs to ``pid``.
    """
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    for x, y in candidate_points(bounds):
        if not _on_screen(x, y, screen):
            continue
        try:
            found = pid_at(int(x), int(y))
        except Exception:
            return None
        if isinstance(found, bool) or not isinstance(found, int):
            continue
        if int(found) == int(pid):
            return (int(x), int(y))
    return None


def recheck_point(
    bounds: Bounds,
    pid: int,
    pid_at: Callable[[int, int], int | None],
    *,
    screen: tuple[int, int] | None = None,
) -> tuple[int, int] | None:
    """An in-box point other than the center whose pid is ``pid``.

    None when the center already belongs to ``pid``, or when no candidate
    does. The caller then hit-tests the center, so a foreign window still
    fails the recheck.
    """
    found = point_for_pid(bounds, pid, pid_at, screen=screen)
    if found is None:
        return None
    center = (int(bounds.center.x), int(bounds.center.y))
    if found == center:
        return None
    return found
