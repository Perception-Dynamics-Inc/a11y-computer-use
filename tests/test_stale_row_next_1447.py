"""#1447: a stale or recycled row ref never suggests a point-based retry.

After a treegrid parent collapses, a held child ref's last bounds belong to
the next row. A refusal that offers ``coordinates`` invites a click on a
different row (a different email). The refusal should point at a fresh find.
No gi import: macOS and Windows collect this file.
"""

from __future__ import annotations

import dataclasses

import pytest

from a11y_computer_use import outcome
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element


def _row_ref():
    element = Element(
        "e50", "AXLink", "jane@example.com", None, Bounds(0, 543, 233, 133, 18),
        "snap-1447", clickable=True, enabled=True,
    )
    return dataclasses.replace(element, row_identity=("row", "jane@example.com", 2))


def _refusal() -> ComputerUseError:
    driver = LinuxDriver()
    driver._run = lambda fn: fn()  # type: ignore[method-assign]
    # The collapsed child row reads as gone (or as the next row).
    with pytest.raises(ComputerUseError) as raised:
        driver._verify_chromium_row_target(_row_ref(), None, current=("row", "ted@example.com", 1))
    return raised.value


def test_collapsed_treegrid_child_refusal_does_not_offer_coordinates() -> None:
    exc = _refusal()
    result = outcome.refused_result(
        text=exc.message, code=exc.code.value, message=exc.message, detail=exc.detail,
    )
    assert result.outcome == "refused"
    assert "coordinates" not in result.next
    assert "ref" in result.next
    assert "coordinates" not in str(exc.detail.get("hint", "")).replace("old coordinates", "")


@pytest.mark.parametrize("detail", [
    {},
    {"reason": "target_changed"},
    {"reason": "target_changed", "next": ["coordinates", "ref"]},
    {"next": ["ref", "coordinates", "keyboard"]},
])
def test_no_stale_ref_path_suggests_coordinates(detail) -> None:
    assert "coordinates" not in outcome.next_for_error("stale_ref", detail)


def test_recycled_target_reason_under_another_code_drops_coordinates() -> None:
    assert "coordinates" not in outcome.next_for_error(
        "invalid_arguments", {"reason": "target_changed", "next": ["coordinates", "ref"]},
    )


def test_other_refusals_keep_their_coordinate_route() -> None:
    assert "coordinates" in outcome.next_for_error("unsupported", {})


def test_old_bounds_hitting_another_node_fail_closed_and_suggest_a_fresh_find(monkeypatch) -> None:
    """The ref's old point now hits a different node: refuse, re-find, no coordinates."""
    from a11y_computer_use.drivers import _atspi

    driver = LinuxDriver()
    driver._run = lambda fn: fn()  # type: ignore[method-assign]
    monkeypatch.setattr(driver, "_refuse_chromium_occluded", lambda *a, **k: None)
    monkeypatch.setattr(_atspi, "chromium_pointer_target", lambda handle, x, y: False)
    with pytest.raises(ComputerUseError) as raised:
        driver._verify_chromium_pointer_target(_row_ref(), object(), 609, 242)
    exc = raised.value
    assert exc.detail["outcome"] == "refused"
    hint = str(exc.detail["hint"])
    assert "fresh snapshot" in hint and "find the control again" in hint
    assert "screenshot" not in hint
    result = outcome.refused_result(
        text=exc.message, code=exc.code.value, message=exc.message, detail=exc.detail,
    )
    assert "coordinates" not in result.next
