"""#1372: set_value with two same-titled Chrome windows.

The DOM writer chose the page by window title. Two windows showing the same
page both match, and the later one won, so set_value on window 1's textarea
overwrote window 2's. With no unique page, no DOM evaluation may run in any
page; the write stays on the ref's own accessibility handle.
"""
from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _atspi, _cdp_forms
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, Element

TITLE = "Multi Lab - Google Chrome"
TWINS = [
    {"title": "Multi Lab", "type": "page", "url": "http://127.0.0.1/mw.html?w=1",
     "webSocketDebuggerUrl": "ws://w1"},
    {"title": "Multi Lab", "type": "page", "url": "http://127.0.0.1/mw.html?w=2",
     "webSocketDebuggerUrl": "ws://w2"},
]


def _textarea() -> Element:
    return Element("e33", "AXTextArea", "Paste box", "T-inactive", Bounds(0, 10, 100, 300, 80),
                   "snap-1", editable=True, enabled=True, instance_id="pid:9|start:1")


@pytest.fixture
def twin_chrome(monkeypatch):
    """Both page targets contain the textarea; record every DOM evaluation."""
    evaluated: list[str] = []
    values = {"ws://w1": "T-inactive", "ws://w2": "BRAVO text in window B"}

    def eval_socket(socket, expression):
        evaluated.append(socket)
        if '"action": "set"' in expression:
            values[socket] = "30-w1"
            return {"found": True, "wrote": True, "value": "30-w1", "kind": "text"}
        return {"found": True}

    monkeypatch.setattr(_cdp_forms, "ports_for_pid", lambda pid: (9333,))
    monkeypatch.setattr("a11y_computer_use.drivers._cdp.page_targets", lambda endpoint: [dict(p) for p in TWINS])
    monkeypatch.setattr(_cdp_forms, "_eval_socket", eval_socket)
    return evaluated, values


def test_choose_socket_refuses_two_equally_matching_pages() -> None:
    assert _cdp_forms.choose_socket([dict(p) for p in TWINS], TITLE) is None


@pytest.mark.parametrize("title", [TITLE, ""])
def test_dom_write_touches_neither_twin_window(twin_chrome, title) -> None:
    evaluated, values = twin_chrome
    assert _cdp_forms.write_element(_textarea(), "30-w1", window_title=title, pids=(9,)) is None
    assert evaluated == []
    assert values == {"ws://w1": "T-inactive", "ws://w2": "BRAVO text in window B"}


def test_unique_title_still_uses_the_dom(twin_chrome, monkeypatch) -> None:
    evaluated, values = twin_chrome
    pages = [dict(TWINS[0]), dict(TWINS[1], title="Lab Two")]
    monkeypatch.setattr("a11y_computer_use.drivers._cdp.page_targets", lambda endpoint: pages)
    assert _cdp_forms.write_element(_textarea(), "30-w1", window_title=TITLE, pids=(9,)) is True
    assert evaluated == ["ws://w1"]
    assert values["ws://w2"] == "BRAVO text in window B"


def test_set_value_in_window_one_writes_only_the_refs_own_handle(twin_chrome, monkeypatch) -> None:
    """The driver path: no DOM write in window 2; set_text gets window 1's handle."""
    evaluated, values = twin_chrome
    window1_handle = object()
    written: list[tuple[object, str]] = []
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn(), raising=False)
    monkeypatch.setattr(driver, "_dom_window_title", lambda handle: TITLE)
    monkeypatch.setattr(driver, "_dom_pids", lambda handle: (9,))
    monkeypatch.setattr(driver, "_focus_web_entry", lambda element, handle, **kw: handle)
    for name, result in {
        "_refuse_model_editor_replace": None, "is_sheet_cell": False,
        "qt_checkable_item": False, "qt_item_view_cell": False, "control_kind": "text",
        "_refuse_drawing_multiline_value": None, "_capture_text_points": [],
        "_blocks_text_replace": False, "web_entry_context": None,
    }.items():
        monkeypatch.setattr(_atspi, name, lambda *a, _r=result, **k: _r)

    def set_text(handle, value):
        written.append((handle, value))
        return True

    monkeypatch.setattr(_atspi, "set_text", set_text)
    assert driver._apply_set_value(_textarea(), "30-w1", window1_handle) is True
    assert evaluated == []
    assert values["ws://w2"] == "BRAVO text in window B"
    assert written == [(window1_handle, "30-w1")]
