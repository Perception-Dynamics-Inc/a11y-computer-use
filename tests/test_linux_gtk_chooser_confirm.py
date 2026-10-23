"""Return in a GTK file chooser activates Open or Save. It does not send the key.

Synthetic trees and a stubbed window list. Not a live desktop. The live
five-open run is ``test_linux_gtk_file_chooser_return_opens_one_file_five_times``.
"""

from __future__ import annotations

import time

import pytest

from a11y_computer_use.drivers import _atspi, _linux_input, _linux_system
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.observe import DisplayGeometry
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Scope,
    Snapshot,
)


class _Node:
    def __init__(self, role, name="", text="", children=(), states=None):
        self.role = role
        self.name = name
        self.text = text
        self.children = list(children)
        self.states = set(states or ())
        self.pressed = False

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_text(self, _start, _end):
        return self.text

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[int(index)]

    def get_action_iface(self):
        return self

    def get_n_actions(self):
        return 1

    def get_action_name(self, _index):
        return "click"

    def do_action(self, _index):
        self.pressed = True
        return True


def _screen(monkeypatch) -> None:
    display = Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True)
    monkeypatch.setattr(
        _atspi, "primary_geometry", lambda: (DisplayGeometry(display=display, origin=(0.0, 0.0)),),
    )


def _clock(monkeypatch) -> None:
    now = {"t": 1_000.0}

    monkeypatch.setattr(time, "monotonic", lambda: now["t"])

    def sleep(seconds) -> None:
        now["t"] += float(seconds)

    monkeypatch.setattr(time, "sleep", sleep)


def _chooser(monkeypatch, *, path="/tmp/docs/picked.txt", box=(40, 40, 80, 30), close=True, show=True):
    """Stub the active chooser. ``box`` is the Open button. Return the records."""
    _screen(monkeypatch)
    _clock(monkeypatch)
    label = _Node("label", name="chosen=")
    host = _Node("frame", name="FileHost", children=[label])
    app = _Node("application", name="cuafileapp", children=[host])
    button = _Node("push button", name="Open")
    state = {
        "open": True,
        "x": 20,
        "y": 0,
        "w": 900,
        "h": 1400,
        "pressed": False,
    }
    scrolled = []
    resized = []
    moved = []
    keys = []

    def windows():
        if not state["open"]:
            return [{
                "window_id": 3,
                "app": "python3",
                "title": "FileHost",
                "bounds": {"x": 40, "y": 40, "width": 420, "height": 160},
            }]
        return [{
            "window_id": 7,
            "app": "python3",
            "title": "Open File",
            "bounds": {"x": state["x"], "y": state["y"], "width": state["w"], "height": state["h"]},
        }]

    def resize(_window_id, width, height):
        state["w"] = int(width)
        state["h"] = int(height)
        resized.append((int(width), int(height)))
        return True

    def move(_window_id, x, y):
        state["x"] = int(x)
        state["y"] = int(y)
        moved.append((int(x), int(y)))
        return True

    def controls(_pid, _title):
        return button, path, app

    def press(acc) -> bool:
        acc.pressed = True
        state["pressed"] = True
        if close:
            state["open"] = False
        if show:
            label.name = f"chosen={path}"
        return True

    monkeypatch.setattr(_linux_system, "active_window", lambda: {
        "window_id": 7, "app": "python3", "pid": 4242, "title": "Open File",
    })
    monkeypatch.setattr(_linux_system, "windows", windows)
    monkeypatch.setattr(_linux_system, "resize_window", resize)
    monkeypatch.setattr(_linux_system, "move_window", move)
    monkeypatch.setattr(_atspi, "file_chooser_controls", controls)
    monkeypatch.setattr(_atspi, "atspi_screen_box", lambda _acc: box)
    monkeypatch.setattr(_atspi, "scroll_to", lambda acc: scrolled.append(acc) or False)
    monkeypatch.setattr(_atspi, "do_press", press)
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    return {
        "button": button,
        "label": label,
        "scrolled": scrolled,
        "resized": resized,
        "moved": moved,
        "keys": keys,
        "state": state,
    }


def test_return_on_a_chooser_activates_open_and_does_not_send_the_key(monkeypatch) -> None:
    found = _chooser(monkeypatch)
    LinuxDriver().key_chord("Return")
    assert found["keys"] == []
    assert found["state"]["pressed"] is True
    assert found["scrolled"] == []
    assert found["resized"] == []


def test_enter_and_kp_enter_use_the_same_button(monkeypatch) -> None:
    first = _chooser(monkeypatch)
    LinuxDriver().key_chord("Enter")
    assert first["keys"] == []
    assert first["state"]["pressed"] is True
    second = _chooser(monkeypatch)
    LinuxDriver().key_chord("kp_enter")
    assert second["keys"] == []
    assert second["state"]["pressed"] is True


def test_a_clipped_open_button_is_scrolled_then_resized(monkeypatch) -> None:
    found = _chooser(monkeypatch, box=(40, 9000, 80, 30))
    LinuxDriver().key_chord("return")
    assert found["keys"] == []
    assert found["scrolled"]
    assert found["resized"]
    width, height = found["resized"][0]
    assert width <= 1280 - 64
    assert height <= 800 - 64
    assert height < 1400
    assert found["state"]["pressed"] is True


def test_a_zero_size_open_button_is_scrolled_then_resized(monkeypatch) -> None:
    found = _chooser(monkeypatch, box=(40, 40, 0, 0))
    LinuxDriver().key_chord("Return")
    assert found["scrolled"]
    assert found["resized"]
    assert found["keys"] == []


def test_return_outside_a_chooser_still_sends_the_key(monkeypatch) -> None:
    keys = []
    monkeypatch.setattr(_linux_system, "active_window", lambda: {
        "window_id": 3, "app": "python3", "pid": 1, "title": "FileHost",
    })
    monkeypatch.setattr(
        _atspi, "file_chooser_controls", lambda *_args: (_ for _ in ()).throw(AssertionError("walked")),
    )
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    LinuxDriver().key_chord("Return")
    assert keys == ["Return"]


def test_ctrl_return_is_still_a_key(monkeypatch) -> None:
    keys = []
    monkeypatch.setattr(
        _linux_system, "active_window", lambda: (_ for _ in ()).throw(AssertionError("window")),
    )
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    LinuxDriver().key_chord("ctrl+Return")
    assert keys == ["ctrl+Return"]


def _page(title: str, *extra: Element) -> Snapshot:
    button = Element(
        ref="e2", role="AXButton", title=title, value=None,
        bounds=Bounds(0, 40, 80, 180, 28), snapshot_id="s",
    )
    return Snapshot(
        snapshot_id="s", scope=Scope.WINDOW, app="chrome", pid=8, created_at=0.0,
        displays=(Display(0, 1280, 800, 1.0, True),),
        elements=(button, *extra),
    )


def _browser_chooser(monkeypatch, *, path="/tmp/docs/picked.txt", close=True, show=True, address=False):
    """Chrome's Open File dialog: an X window and no accessibility button."""
    _screen(monkeypatch)
    _clock(monkeypatch)
    state = {"open": True, "x": 20, "y": 0, "w": 900, "h": 1400, "title": "Upload: No file chosen"}
    keys = []
    clicks = []
    resized = []
    moved = []
    focused = []
    board = {"value": "keep-me"}

    def windows():
        if not state["open"]:
            return [{
                "window_id": 3,
                "app": "chrome",
                "title": "picked.txt - Google Chrome",
                "bounds": {"x": 40, "y": 40, "width": 1000, "height": 700},
            }]
        return [{
            "window_id": 9,
            "app": "chrome",
            "title": "Open File",
            "bounds": {"x": state["x"], "y": state["y"], "width": state["w"], "height": state["h"]},
        }]

    def press(chord: str) -> None:
        keys.append(chord)
        if chord == "ctrl+c":
            board["value"] = path

    def click(x: int, y: int, **_kwargs) -> None:
        clicks.append((int(x), int(y)))
        if close:
            state["open"] = False
        if show:
            state["title"] = "Upload: picked.txt"

    def snapshot(_scope, _app):
        extras = ()
        if address:
            extras = (
                Element(
                    ref="e3", role="AXTextField", title=path, value=path,
                    bounds=Bounds(0, 80, 8, 600, 24), snapshot_id="s",
                ),
            )
        return _page(state["title"], *extras)

    monkeypatch.setattr(_linux_system, "active_window", lambda: {
        "window_id": 9, "app": "chrome", "pid": 8, "title": "Open File",
    })
    monkeypatch.setattr(_linux_system, "windows", windows)
    monkeypatch.setattr(_linux_system, "resize_window", lambda _wid, width, height: resized.append((int(width), int(height))) or state.update(w=int(width), h=int(height)) or True)
    monkeypatch.setattr(_linux_system, "move_window", lambda _wid, x, y: moved.append((int(x), int(y))) or state.update(x=int(x), y=int(y)) or True)
    monkeypatch.setattr(_linux_system, "focus_window", lambda window_id: focused.append(window_id) or True)
    monkeypatch.setattr(_linux_system, "write_clipboard", lambda text: board.update(value=text))
    monkeypatch.setattr(_linux_system, "read_clipboard", lambda: board["value"])
    monkeypatch.setattr(_atspi, "file_chooser_controls", lambda *_args: None)
    monkeypatch.setattr(_linux_input, "press_chord", press)
    monkeypatch.setattr(_linux_input, "click", click)
    driver = LinuxDriver()
    driver.snapshot = snapshot
    return {
        "driver": driver,
        "keys": keys,
        "clicks": clicks,
        "resized": resized,
        "moved": moved,
        "focused": focused,
        "board": board,
        "state": state,
    }


def test_chrome_return_clicks_open_and_the_page_shows_the_file(monkeypatch) -> None:
    found = _browser_chooser(monkeypatch)
    found["driver"].key_chord("Return")
    assert "return" not in {key.casefold() for key in found["keys"]}
    assert found["keys"] == ["ctrl+a", "ctrl+c", "right"]
    assert found["resized"] == [(900, 736)]
    assert found["moved"] == [(32, 32)]
    assert found["clicks"] == [(32 + 900 - 53, 32 + 736 - 27)]
    assert found["state"]["open"] is False
    assert found["board"]["value"] == "keep-me"
    assert found["focused"] == [9]


def test_chrome_return_fails_when_the_page_still_says_no_file_chosen(monkeypatch) -> None:
    found = _browser_chooser(monkeypatch, close=True, show=False, address=True)
    with pytest.raises(ComputerUseError) as exc:
        found["driver"].key_chord("Return")
    assert "return" not in {key.casefold() for key in found["keys"]}
    assert found["clicks"]
    assert found["state"]["open"] is False
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "file_not_chosen"
    assert exc.value.detail["expected"] == "picked.txt"


def test_chrome_return_fails_when_the_dialog_stays_open(monkeypatch) -> None:
    found = _browser_chooser(monkeypatch, close=False, show=False)
    with pytest.raises(ComputerUseError) as exc:
        found["driver"].key_chord("Return")
    assert "return" not in {key.casefold() for key in found["keys"]}
    assert found["clicks"]
    assert exc.value.detail["reason"] == "file_chooser_unchanged"


def test_chrome_return_with_an_empty_location_does_not_click(monkeypatch) -> None:
    found = _browser_chooser(monkeypatch, path="")
    with pytest.raises(ComputerUseError) as exc:
        found["driver"].key_chord("Return")
    assert found["clicks"] == []
    assert found["resized"] == []
    assert "return" not in {key.casefold() for key in found["keys"]}
    assert exc.value.detail["reason"] == "file_not_chosen"


def test_a_non_browser_chooser_without_a_button_still_sends_the_key(monkeypatch) -> None:
    keys = []
    monkeypatch.setattr(_linux_system, "active_window", lambda: {
        "window_id": 9, "app": "python3", "pid": 8, "title": "Open File",
    })
    monkeypatch.setattr(_atspi, "file_chooser_controls", lambda *_args: None)
    monkeypatch.setattr(_linux_input, "press_chord", lambda chord: keys.append(chord))
    LinuxDriver().key_chord("Return")
    assert keys == ["Return"]


def test_a_dialog_that_stays_open_is_an_error_and_the_key_was_not_sent(monkeypatch) -> None:
    found = _chooser(monkeypatch, close=False, show=False)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().key_chord("Return")
    assert found["keys"] == []
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "file_chooser_unchanged"


def test_a_dialog_that_closes_without_the_file_is_an_error(monkeypatch) -> None:
    found = _chooser(monkeypatch, close=True, show=False)
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().key_chord("Return")
    assert found["keys"] == []
    assert exc.value.detail["reason"] == "file_not_chosen"
    assert exc.value.detail["expected"] == "picked.txt"


def test_an_empty_location_does_not_send_return(monkeypatch) -> None:
    found = _chooser(monkeypatch, path="")
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().key_chord("Return")
    assert found["keys"] == []
    assert found["state"]["pressed"] is False
    assert exc.value.detail["reason"] == "file_not_chosen"


def test_file_dialog_stays_unsupported() -> None:
    with pytest.raises(ComputerUseError) as exc:
        LinuxDriver().file_dialog("open", "/tmp/docs/picked.txt", "mousepad")
    assert exc.value.detail["reason"] == "no_file_dialog"
    assert "Return activates Open or Save" in exc.value.detail["hint"]


def test_open_button_is_found_beside_a_long_file_list() -> None:
    cells = [_Node("table cell", name=f"row-{index}.txt") for index in range(120)]
    selected = _Node("table cell", name="other.txt", states={"SELECTED"})
    table = _Node("table", children=[*cells, selected])
    entry = _Node("text", name="Location", text="/tmp/docs/picked.txt", states={"FOCUSED"})
    cancel = _Node("push button", name="Cancel")
    opened = _Node("push button", name="Open")
    frame = _Node("frame", name="Open File", children=[table, entry, cancel, opened])
    found = _atspi.chooser_controls_under(frame, "Open File")
    assert found is not None
    button, path = found
    assert button is opened
    assert path == "/tmp/docs/picked.txt"


def test_a_selected_file_is_the_path_when_the_location_entry_is_empty() -> None:
    row = _Node("table cell", name="notes.txt", states={"SELECTED"})
    table = _Node("table", children=[row])
    opened = _Node("push button", name="Open")
    frame = _Node("frame", name="Open File", children=[table, opened])
    _button, path = _atspi.chooser_controls_under(frame, "Open File")
    assert path == "notes.txt"


def test_the_chooser_frame_does_not_count_as_the_app_showing_the_file() -> None:
    dialog = _Node("frame", name="Open File", children=[
        _Node("text", name="Location", text="/tmp/docs/picked.txt"),
    ])
    host = _Node("frame", name="FileHost", children=[_Node("label", name="chosen=")])
    app = _Node("application", name="cuafileapp", children=[dialog, host])
    assert _atspi.tree_shows_file(app, "/tmp/docs/picked.txt", skip_titles=("Open File",)) is False
    host.children[0].name = "chosen=/tmp/docs/picked.txt n=2"
    assert _atspi.tree_shows_file(app, "/tmp/docs/picked.txt", skip_titles=("Open File",)) is True


def test_dry_run_does_not_activate_the_chooser(monkeypatch) -> None:
    monkeypatch.setattr(
        _linux_system, "active_window", lambda: (_ for _ in ()).throw(AssertionError("window")),
    )
    assert LinuxDriver().key_chord("Return", dry_run=True) is None


@pytest.mark.parametrize("title", ["Open", "Save"])
def test_return_in_an_ordinary_browser_window_is_a_key(monkeypatch, title) -> None:
    keys = []
    monkeypatch.setattr(_linux_system, "active_window", lambda: {
        "window_id": 3, "app": "chrome", "pid": 8, "title": title,
    })
    monkeypatch.setattr(_atspi, "file_chooser_controls", lambda *_args: (
        _ for _ in ()).throw(AssertionError("walked")))
    monkeypatch.setattr(_linux_input, "press_chord", keys.append)
    LinuxDriver().key_chord("Return")
    assert keys == ["Return"]


def test_missing_chooser_frame_does_not_use_app_buttons() -> None:
    host = _Node("frame", name="FileHost", children=[
        _Node("push button", name="Open"),
        _Node("text", name="Location", text="/tmp/picked.txt"),
    ])
    app = _Node("application", name="host", children=[host])
    assert _atspi._chooser_in_apps([app], "open file", "Open File") is None
    chooser = _Node("frame", name="Open File", children=[
        _Node("push button", name="Open"),
        _Node("text", name="Location", text="/tmp/picked.txt"),
    ])
    app.children.append(chooser)
    assert _atspi._chooser_in_apps([app], "open file", "Open File") is not None


@pytest.mark.parametrize("other_title", ["Cover letter: No file chosen", "Cover letter: No file selected", "Cover letter: picked.txt"])
def test_chrome_chooser_verifies_selection_with_another_file_input(monkeypatch, other_title):
    found = _browser_chooser(monkeypatch)
    driver = found["driver"]
    snapshots = []

    def snapshot(scope, app):
        snapshots.append((scope, app, found["state"]["open"]))
        other = Element(ref="e3", role="AXButton", title=other_title, value=None, bounds=None, snapshot_id="s")
        return _page(found["state"]["title"], other)

    driver.snapshot = snapshot
    driver.key_chord("Return")

    assert found["state"]["open"] is False
    assert len(found["clicks"]) == 1
    # One baseline and one verification pass, without polling to the deadline.
    assert [opened for _, _, opened in snapshots] == [True, True, False, False]


def test_chrome_chooser_rejects_an_unchanged_preexisting_selected_input(monkeypatch):
    found = _browser_chooser(monkeypatch, show=False, address=True)
    original = found["driver"].snapshot

    def snapshot(scope, app):
        shot = original(scope, app)
        other = Element(ref="e4", role="AXButton", title="Cover letter: picked.txt", value=None, bounds=None, snapshot_id="s")
        return _page(found["state"]["title"], *shot.elements[1:], other)

    found["driver"].snapshot = snapshot
    with pytest.raises(ComputerUseError) as exc:
        found["driver"].key_chord("Return")
    assert exc.value.detail["reason"] == "file_not_chosen"
    assert len(found["clicks"]) == 1


def test_chrome_chooser_rejects_disappearing_empty_input_without_new_selection(monkeypatch):
    found = _browser_chooser(monkeypatch, show=False)

    def snapshot(scope, app):
        selected = Element(ref="e3", role="AXButton", title="Cover letter: picked.txt", value=None, bounds=None, snapshot_id="s")
        if found["state"]["open"]:
            return _page(found["state"]["title"], selected)
        return _page(selected.title)

    found["driver"].snapshot = snapshot
    with pytest.raises(ComputerUseError) as exc:
        found["driver"].key_chord("Return")
    assert exc.value.detail["reason"] == "file_not_chosen"
