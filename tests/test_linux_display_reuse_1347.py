"""#1347: one X connection per thread per action, closed when the action ends."""

from __future__ import annotations

import threading

import pytest

from a11y_computer_use.drivers import _linux_system
from tests.test_linux_proc_scan_1347 import ME, REAL, _runtime, _world


class _FakeDisplay:
    def __init__(self, log: list) -> None:
        self.closed = False
        log.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def xlog(monkeypatch) -> list:
    log: list = []
    monkeypatch.setattr(_linux_system, "_display", lambda: _FakeDisplay(log))
    return log


def _probe(n: int = 6) -> None:
    for _ in range(n):  # what the frontmost/identity probes do: open, query, close
        with _linux_system._open_display():
            pass


def test_screenshot_opens_one_display_for_all_threads(tmp_path, monkeypatch, xlog) -> None:
    _world(monkeypatch, {52: ("mousepad", REAL, ME)}, 52)
    # A fake X server with no managed windows; resolve_app still opens a display.
    monkeypatch.setattr(_linux_system, "_managed_windows", lambda d: [])
    rt = _runtime(tmp_path, {"mousepad": "full"})
    shot = rt.driver.screenshot

    def screenshot(display_id=None):
        _probe()  # main thread
        t = threading.Thread(target=_probe)  # the AT-SPI worker
        t.start()
        t.join()
        return shot(display_id)

    rt.driver.screenshot = screenshot
    rt.screenshot()
    assert len(xlog) == 1  # shared by both threads; was one per probe (13 on a live desktop)
    assert all(d.closed for d in xlog)  # nothing outlives the action
    rt.screenshot()
    assert len(xlog) == 2  # never carried into the next action
    assert all(d.closed for d in xlog)


def test_outside_an_action_every_probe_has_its_own_connection(xlog) -> None:
    _probe(3)
    assert len(xlog) == 3 and all(d.closed for d in xlog)


def test_a_failed_probe_drops_the_connection(xlog) -> None:
    with _linux_system.x_scope():
        with pytest.raises(OSError), _linux_system._open_display():
            raise OSError("broken pipe")
        assert xlog[0].closed
        _probe(2)
        assert len(xlog) == 2 and not xlog[1].closed
    assert xlog[1].closed


def test_nested_scopes_close_only_at_the_outermost_exit(xlog) -> None:
    with _linux_system.x_scope():
        with _linux_system.x_scope():
            _probe(2)
        assert len(xlog) == 1 and not xlog[0].closed
        _probe(2)
        assert len(xlog) == 1
    assert xlog[0].closed


def test_a_swapped_opener_is_never_answered_from_the_old_connection(
    monkeypatch, xlog
) -> None:
    other: list = []
    with _linux_system.x_scope():
        _probe(2)
        monkeypatch.setattr(_linux_system, "_display", lambda: _FakeDisplay(other))
        with _linux_system._open_display() as d:
            assert d is other[0]
    assert xlog[0].closed and other[0].closed
