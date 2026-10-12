"""Window move coordinates: reject a bad point, send a negative frame as CARD32.

Hermetic. A fake X display records the client message. python-xlib is
imported only in the test that constructs a real ClientMessage, and that
test skips where Xlib is not installed.
"""

from __future__ import annotations

import types

import pytest

from a11y_computer_use import cli, safety, server
from a11y_computer_use.drivers import _linux_system
from a11y_computer_use.schema import Display

_INT32_MIN = -1 << 31
_INT32_MAX = (1 << 31) - 1
_FLAGS = 1 | (3 << 8)


def _card32(value: int) -> int:
    """The unsigned bits EWMH reads back as ``value``. Not a clamp."""
    assert isinstance(value, int) and not isinstance(value, bool)
    assert _INT32_MIN <= value <= _INT32_MAX
    if value < 0:
        return value + (1 << 32)
    return value


def _install(monkeypatch, *, width=None, height=None, insets=(0, 0), hidden=False):
    """A display whose client messages are recorded. No real X connection."""
    messages: list[tuple[str, list]] = []
    opened: list[int] = []

    class _Win:
        id = 7

        def get_full_property(self, atom, kind):
            return None

    def screen():
        root = types.SimpleNamespace()
        found = types.SimpleNamespace(root=root)
        if width is not None:
            found.width_in_pixels = width
            found.height_in_pixels = height
        return found

    display = types.SimpleNamespace(
        screen=screen,
        intern_atom=lambda name: name,
        flush=lambda: None,
        close=lambda: None,
    )

    def open_display():
        opened.append(1)
        return display

    monkeypatch.setattr(_linux_system, "_display", open_display)
    monkeypatch.setattr(_linux_system, "_managed_windows", lambda d: [_Win()])
    monkeypatch.setattr(_linux_system, "_frame_insets", lambda win, d: insets)
    monkeypatch.setattr(_linux_system, "_is_hidden", lambda win, d: hidden)

    def client(_d, _win, atom, data):
        messages.append((atom, list(data)))

    monkeypatch.setattr(_linux_system, "_client_message", client)
    return messages, opened


def _moved(messages) -> list:
    return [data for atom, data in messages if atom == "_NET_MOVERESIZE_WINDOW"][-1]


def test_negative_frame_origin_is_sent_as_card32(monkeypatch) -> None:
    """A point inside the title bar. The client origin is unchanged.

    Insets (8, 40) make a move to (5, 21) ask for frame (-3, -19). Those
    bits are the two's complement of that frame, not zero and not (5, 21).
    """
    messages, _opened = _install(monkeypatch, insets=(8, 40))
    assert _linux_system.move_window(7, 5, 21) is True
    data = _moved(messages)
    assert data == [_FLAGS, _card32(5 - 8), _card32(21 - 40), 0, 0]
    assert data[1] != 0 and data[2] != 0
    assert data[1:] != [5, 21, 0, 0]


@pytest.mark.parametrize(
    ("x", "y", "left", "top"),
    [
        (35, 18, 0, 33),
        (50, 26, 0, 33),
        (10, 22, 0, 30),
        (15, 23, 12, 28),
    ],
)
def test_reported_title_bar_points_keep_their_client_origin(monkeypatch, x, y, left, top) -> None:
    """The coordinates from the overflow report, with a title bar that covers them."""
    messages, _opened = _install(monkeypatch, insets=(left, top))
    assert _linux_system.move_window(7, x, y) is True
    data = _moved(messages)
    assert data == [_FLAGS, _card32(x - left), _card32(y - top), 0, 0]
    assert x - left < 0 or y - top < 0
    assert data[1] != 0 and data[2] != 0


def test_positive_frame_origin_is_the_shifted_client_point(monkeypatch) -> None:
    """A 5px border and a 29px title bar still send the frame to (95, 51)."""
    messages, _opened = _install(monkeypatch, insets=(5, 29))
    assert _linux_system.move_window(7, 100, 80) is True
    assert _moved(messages) == [_FLAGS, 95, 51, 0, 0]


def test_python_xlib_packs_the_card32_bits_and_rejects_a_raw_negative() -> None:
    """The bits move_window sends are what python-xlib can put on the wire."""
    pytest.importorskip("Xlib")
    from Xlib.protocol import event

    data = [_FLAGS, _card32(-3), _card32(-19), 0, 0]
    event.ClientMessage(window=1, client_type=1, data=(32, data))
    with pytest.raises(OverflowError, match="negative"):
        event.ClientMessage(
            window=1, client_type=1, data=(32, [_FLAGS, -3, -19, 0, 0]),
        )


@pytest.mark.parametrize("x,y", [(-1, 10), (10, -1), (True, 0), (0, False), (10.5, 10), (10, 4.0)])
def test_invalid_move_sends_no_message(monkeypatch, x, y) -> None:
    messages, opened = _install(monkeypatch, width=1280, height=800)
    with pytest.raises(ValueError, match="window move"):
        _linux_system.move_window(7, x, y)
    assert messages == []
    assert opened == []


def test_screen_edge_is_width_minus_one(monkeypatch) -> None:
    """1280 is past a 1280-wide screen. 1279 is the last pixel, and it is sent."""
    messages, opened = _install(monkeypatch, width=1280, height=800)
    with pytest.raises(ValueError, match=r"valid x is 0\.\.1279") as exc:
        _linux_system.move_window(7, 1280, 10)
    assert "valid x is 0..1280" not in str(exc.value)
    assert messages == []
    assert opened == [1]

    messages.clear()
    with pytest.raises(ValueError, match=r"y is 0\.\.799"):
        _linux_system.move_window(7, 10, 800)
    assert messages == []

    messages.clear()
    assert _linux_system.move_window(7, 1279, 799) is True
    assert _moved(messages) == [_FLAGS, 1279, 799, 0, 0]
    assert _linux_system.move_window(7, 0, 0) is True
    assert _moved(messages) == [_FLAGS, 0, 0, 0, 0]


def test_missing_screen_size_is_not_a_1280x800_screen(monkeypatch) -> None:
    """No width_in_pixels. A point past 1280 is still sent. A negative is not."""
    messages, opened = _install(monkeypatch)
    assert _linux_system.move_window(7, 300, 180) is True
    assert _linux_system.move_window(7, 2000, 2000) is True
    assert _linux_system.move_window(7, _INT32_MAX, 0) is True
    assert [data for atom, data in messages if atom == "_NET_MOVERESIZE_WINDOW"] == [
        [_FLAGS, 300, 180, 0, 0],
        [_FLAGS, 2000, 2000, 0, 0],
        [_FLAGS, _INT32_MAX, 0, 0, 0],
    ]
    sent = len(messages)
    opens = len(opened)
    with pytest.raises(ValueError, match="signed screen range"):
        _linux_system.move_window(7, _INT32_MAX + 1, 0)
    with pytest.raises(ValueError, match="signed screen range"):
        _linux_system.move_window(7, -1, 0)
    assert len(messages) == sent
    assert len(opened) == opens


def test_frame_past_signed_32_bit_sends_nothing(monkeypatch) -> None:
    """An inset that would underflow the frame is an error, not a clamp to zero.

    The window is hidden. The uniconify is not sent either: the frame is
    rejected before any client message.
    """
    left = 100 - (_INT32_MIN - 1)
    messages, _opened = _install(monkeypatch, insets=(left, 0), hidden=True)
    with pytest.raises(ValueError, match="signed 32-bit"):
        _linux_system.move_window(7, 100, 80)
    assert messages == []


def test_hidden_window_is_restored_after_a_valid_negative_frame(monkeypatch) -> None:
    messages, _opened = _install(monkeypatch, insets=(8, 40), hidden=True)
    assert _linux_system.move_window(7, 5, 21) is True
    atoms = [atom for atom, _data in messages]
    assert atoms[0] == "WM_CHANGE_STATE"
    assert atoms[-1] == "_NET_MOVERESIZE_WINDOW"
    assert _moved(messages)[1:] == [_card32(5 - 8), _card32(21 - 40), 0, 0]


class _MoveDriver:
    """Runtime double. ``size`` None means this driver cannot name a display."""

    name = "linux"
    resolves_apps = True

    def __init__(self, size: tuple[int, int] | None = None):
        self.size = size
        self.calls: list[tuple] = []
        self.owners = 0
        self.rows = [{
            "window_id": 7, "app": "gedit", "title": "notes", "on_screen": True,
            "bounds": {"display_id": 0, "x": 1, "y": 2, "width": 100, "height": 80},
        }]
        if size is not None:
            width, height = size

            def displays():
                return [Display(display_id=0, width=width, height=height, scale=1.0, is_main=True)]

            self.displays = displays

    def frontmost_app(self):
        return ("gedit", 1)

    def windows(self):
        return list(self.rows)

    def window_owner(self, window_id):
        self.owners += 1
        return "gedit"

    def move_window(self, window_id, x, y):
        self.calls.append((window_id, x, y))
        self.rows[0]["bounds"] = {**self.rows[0]["bounds"], "x": x, "y": y}


def _runtime(tmp_path, driver):
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("gedit", safety.Tier.CLICK)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


def test_runtime_rejects_a_bad_move_before_the_owner_lookup(tmp_path) -> None:
    driver = _MoveDriver(size=(1280, 800))
    rt = _runtime(tmp_path, driver)
    for kwargs in (
        {"x": -1, "y": 10},
        {"x": 1280, "y": 10},
        {"x": 10, "y": 800},
        {"x": True, "y": 0},
        {"x": 10.5, "y": 10},
        {"x": "10", "y": 10},
    ):
        with pytest.raises(ValueError, match="window move"):
            rt.window("move", window_id=7, **kwargs)
    assert driver.calls == []
    assert driver.owners == 0

    text = rt.window("move", window_id=7, x=1279, y=799)
    assert text == "moved window 7 to (1279, 799) (gedit)"
    assert driver.calls == [(7, 1279, 799)]
    assert all(isinstance(item, int) and not isinstance(item, bool) for item in driver.calls[0])

    bare = _MoveDriver()
    other = _runtime(tmp_path, bare)
    assert other.window("move", window_id=7, x=8, y=9) == "moved window 7 to (8, 9) (gedit)"
    assert other.window("move", window_id=7, x=40, y=50) == "moved window 7 to (40, 50) (gedit)"
    assert bare.calls == [(7, 8, 9), (7, 40, 50)]
    with pytest.raises(ValueError, match="window move"):
        other.window("move", window_id=7, x=-1, y=9)
    assert bare.calls == [(7, 8, 9), (7, 40, 50)]
    assert bare.owners == 2


def test_run_once_bad_move_is_one_line_invalid_arguments(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    driver = _MoveDriver(size=(1280, 800))
    monkeypatch.setattr("a11y_computer_use.drivers.get_driver", lambda *a, **k: driver)
    code = cli.main([
        "run-once",
        '{"tool":"window","action":"move","window_id":7,"x":-1,"y":10}',
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("invalid_arguments: window:")
    assert "OverflowError" not in err
    assert "internal_error" not in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0
    assert driver.calls == []
    assert driver.owners == 0

    code = cli.main([
        "run-once",
        '{"tool":"window","action":"move","window_id":7,"x":1280,"y":10}',
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("invalid_arguments: window:")
    assert "valid x is 0..1279" in err
    assert "valid x is 0..1280" not in err
    assert driver.calls == []

    code = cli.main([
        "run-once",
        '{"tool":"window","action":"move","window_id":7,"x":10.5,"y":10}',
    ])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("invalid_arguments: window:")
    assert "Traceback" not in err
    assert driver.calls == []

    # Runtime() reads ~/.a11y-computer-use, and HOME is tmp_path.
    store = safety.PermissionStore()
    store.set_tier("gedit", safety.Tier.CLICK)
    code = cli.main([
        "run-once",
        '{"tool":"window","action":"move","window_id":7,"x":18,"y":12}',
    ])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert out.out.startswith("moved window 7 to (18, 12)")
    assert driver.calls == [(7, 18, 12)]
