"""Wake paths for Linux waits. Stand-ins only: no accessibility bus.

``wait_for`` must not snapshot again when the event wait times out.
``wait_until`` snapshot text uses a structural wake, and a caret blink
does not end that wait early.
"""

from __future__ import annotations

import time

from a11y_computer_use.conditions import Checker
from a11y_computer_use.drivers import _atspi_events
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, Element, WaitCondition
from a11y_computer_use import server


def _element() -> Element:
    return Element("e1", "AXButton", "Save", None, Bounds(0, 0, 0, 10, 10), "snap-1")


def test_wait_for_skips_the_snapshot_when_the_event_wait_times_out(monkeypatch) -> None:
    calls = {"checker": 0, "wait": 0}

    def checker(target, condition):
        calls["checker"] += 1
        if calls["checker"] == 2:
            return target
        return None

    def wait_for_event(since_seq, timeout=0.5):
        calls["wait"] += 1
        if calls["wait"] == 1:
            return None
        return (1, "object:children-changed", 0)

    monkeypatch.setattr(_atspi_events, "enabled", lambda: True)
    monkeypatch.setattr(_atspi_events, "current_seq", lambda: 0)
    monkeypatch.setattr(_atspi_events, "wait_for_event", wait_for_event)
    found = LinuxDriver().wait_for(
        _element(), condition=WaitCondition.EXISTS, timeout_s=5, checker=checker,
    )
    assert found.ref == "e1"
    assert calls["checker"] == 2
    assert calls["wait"] == 2


def test_checker_wake_probes_again_without_sleeping(monkeypatch) -> None:
    probes = {"n": 0}
    seen: list[float] = []

    def snapshot_text(app):
        probes["n"] += 1
        return "Ready" if probes["n"] >= 2 else "Loading"

    def wake(seconds: float) -> bool:
        seen.append(seconds)
        return True

    def fail_sleep(*_args, **_kwargs):
        raise AssertionError("wake should replace the poll sleep")

    monkeypatch.setattr("a11y_computer_use.conditions._sleep", fail_sleep)
    result = Checker(snapshot_text=snapshot_text).wait(
        {"snapshot_text": "Ready"}, timeout_s=5, poll_s=2, wake=wake,
    )
    assert result["polls"] == 2
    assert probes["n"] == 2
    assert len(seen) == 1
    assert 1.5 <= seen[0] <= 2


def test_snapshot_text_wake_is_off_unless_events_are_enabled(monkeypatch) -> None:
    monkeypatch.setattr(_atspi_events, "enabled", lambda: False)
    assert server._snapshot_text_wake() is None
    monkeypatch.setattr(_atspi_events, "enabled", lambda: True)
    wake = server._snapshot_text_wake()
    assert wake is not None


def test_structural_wait_ignores_a_queued_state_change() -> None:
    start = _atspi_events.current_seq()
    _atspi_events._on_event(_Event("object:state-changed"))
    began = time.monotonic()
    found = _atspi_events.wait_for_structural(start, timeout=0.05)
    elapsed = time.monotonic() - began
    assert found is None
    assert elapsed >= 0.04


def test_structural_wait_returns_a_queued_children_change() -> None:
    start = _atspi_events.current_seq()
    _atspi_events._on_event(_Event("object:children-changed"))
    began = time.monotonic()
    found = _atspi_events.wait_for_structural(start, timeout=1)
    assert found is not None
    assert "children-changed" in found[1]
    assert time.monotonic() - began < 0.2


class _Event:
    def __init__(self, kind: str) -> None:
        self.type = kind
        self.detail1 = 0
