"""Run the installed CLI, then stop native AT-SPI before Python finalization.

The pinned wheel and head use the same launcher and cleanup inside the R2
subprocess timer. The actual CLI still parses and dispatches the action.
"""
from __future__ import annotations

import runpy
import sys


def close_atspi():
    """Join the native event loop before releasing libatspi's global cache."""
    events = sys.modules.get("a11y_computer_use.drivers._atspi_events")
    atspi = sys.modules.get("gi.repository.Atspi")
    if atspi is None:
        return
    thread = getattr(events, "_thread", None)
    if thread is not None:
        if thread.is_alive() and events.listening():
            events.submit(atspi.event_quit)
        thread.join(timeout=15)
        if thread.is_alive():
            raise RuntimeError("AT-SPI event thread did not stop")
    atspi.exit()


def main():
    """Preserve the real CLI's result and errors, including SystemExit."""
    try:
        runpy.run_module("a11y_computer_use", run_name="__main__", alter_sys=True)
    finally:
        close_atspi()


if __name__ == "__main__":
    main()
