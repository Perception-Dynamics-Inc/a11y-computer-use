"""Hermetic visible-action scheduling and native-dispatch regressions."""
import queue
from types import SimpleNamespace

import pytest

from a11y_computer_use import visible
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds


@pytest.fixture
def feedback(monkeypatch):
    for name in ("A11Y_VISIBLE_ACTIONS", "A11Y_VISIBLE_ACTIONS_POINTER", "A11Y_VISIBLE_ACTIONS_HIGHLIGHT",
                 "WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "A11Y_VISIBLE_ACTIONS_GLIDE_MS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":fake")
    result = visible.VisibleActions()
    result.thread = SimpleNamespace(is_alive=lambda: True)
    return result


def test_defaults_and_overrides(feedback, monkeypatch):
    assert visible.enabled()
    assert not visible.enabled(False)
    monkeypatch.setenv("A11Y_VISIBLE_ACTIONS", "1")
    assert visible.enabled(False)
    monkeypatch.setenv("A11Y_VISIBLE_ACTIONS", "0")
    assert not visible.enabled(True)
    monkeypatch.delenv("DISPLAY")
    monkeypatch.setenv("A11Y_VISIBLE_ACTIONS", "1")
    assert not visible.enabled()


@pytest.mark.parametrize("bounds", [None, Bounds(0, 1, 2, 0, 10), Bounds(0, 1, 2, 10, -1)])
def test_missing_geometry_does_nothing(feedback, bounds, monkeypatch):
    monkeypatch.setenv("A11Y_VISIBLE_ACTIONS_POINTER", "1")
    monkeypatch.setattr(feedback, "_glide", lambda *args: pytest.fail("no geometry"))
    feedback.prepare("Click", "Save", bounds)
    assert feedback.pending.empty()


def test_latest_label_and_pointer_opt_in(feedback, monkeypatch):
    moves = []
    monkeypatch.setattr(feedback, "_glide", lambda bounds: moves.append(bounds))
    bounds = Bounds(0, 10, 20, 30, 40)
    feedback.prepare("Click", "Save", bounds)
    assert not moves
    feedback.announce("Type", "Field name", bounds)
    assert feedback.pending.get_nowait() == (bounds, "Type: Field name")
    monkeypatch.setenv("A11Y_VISIBLE_ACTIONS_POINTER", "1")
    monkeypatch.setenv("A11Y_VISIBLE_ACTIONS_HIGHLIGHT", "0")
    feedback.prepare("Click", "Save", bounds)
    assert moves == [bounds]
    assert feedback.pending.empty()
    feedback.prepare("Set value", "Password", bounds, secure=True)
    assert moves == [bounds]


def test_latency_no_native_work_or_sleep(feedback, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(visible.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(visible.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    start = visible.time.monotonic()
    feedback.announce("Click", "Save", Bounds(0, 10, 20, 30, 40))
    assert visible.time.monotonic() - start < 0.150
    assert not feedback.pending.empty()


def test_glide_eight_steps_budget(feedback, monkeypatch):
    from a11y_computer_use.drivers import _linux_input
    moves, sleeps = [], []
    clock = [0.0]
    screen = SimpleNamespace(width_in_pixels=1000, height_in_pixels=1000,
                             root=SimpleNamespace(query_pointer=lambda: SimpleNamespace(root_x=0, root_y=0)))
    flushed, synced = [], []
    monkeypatch.setattr(_linux_input, "_disp", lambda: SimpleNamespace(
        screen=lambda: screen, has_extension=lambda ext: True,
        flush=lambda: flushed.append(len(moves)), sync=lambda: synced.append(len(moves))))
    monkeypatch.setattr(_linux_input, "_move", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(visible.time, "monotonic", lambda: clock[0])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(visible.time, "sleep", fake_sleep)
    feedback._glide(Bounds(0, 10, 20, 30, 40))
    # Eight eased steps plus a final jump to the target centre.
    assert len(moves) == 9
    assert moves[-1] == (25, 40)
    # Every intermediate step is flushed right after its move (no round trip),
    # and only the final jump is synced.
    assert flushed == list(range(1, 9))
    assert synced == [9]
    assert abs(sum(sleeps) - 0.08) < 1e-9
    assert sum(sleeps) < 0.150
    assert clock[0] <= 0.08 + 1e-9
    feedback._glide(Bounds(0, 2000, 0, 30, 40))
    assert len(moves) == 9


def test_glide_jumps_when_deadline_exceeded(feedback, monkeypatch):
    """Slow intermediate work must not exceed the configured glide budget."""
    from a11y_computer_use.drivers import _linux_input
    moves, sleeps = [], []
    clock = [0.0]
    screen = SimpleNamespace(width_in_pixels=1000, height_in_pixels=1000,
                             root=SimpleNamespace(query_pointer=lambda: SimpleNamespace(root_x=0, root_y=0)))
    monkeypatch.setattr(_linux_input, "_disp", lambda: SimpleNamespace(
        screen=lambda: screen, has_extension=lambda ext: True, flush=lambda: None, sync=lambda: None))
    monkeypatch.setattr(_linux_input, "_move", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(visible.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("A11Y_VISIBLE_ACTIONS_GLIDE_MS", "80")

    def slow_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += 0.05  # each step burns budget faster than planned

    monkeypatch.setattr(visible.time, "sleep", slow_sleep)
    feedback._glide(Bounds(0, 10, 20, 30, 40))
    assert moves[-1] == (25, 40)
    assert clock[0] <= 0.08 + 1e-9 or len(moves) < 9
    assert sum(sleeps) <= 0.08 + 1e-9


def test_type_into_ref_announces_type_not_typed_text(monkeypatch):
    """type(ref=...) uses type_into; it must show Type, never the payload."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi

    driver = LinuxDriver()
    bounds = Bounds(0, 10, 20, 100, 24)
    element = SimpleNamespace(
        secure=False,
        snapshot_id="s",
        ref="e1",
        bounds=bounds,
        title="Name",
        role="entry",
        editable=True,
    )
    handle = object()
    announces = []
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: handle)
    monkeypatch.setattr(driver, "_refuse_hidden", lambda *args: None)
    monkeypatch.setattr(driver, "_raise_handle_window", lambda *args: None)
    monkeypatch.setattr(_atspi, "_blocks_text_replace", lambda h: False)
    monkeypatch.setattr(_atspi, "drawing_text_editable", lambda h: False)
    monkeypatch.setattr(_atspi, "grab_focus", lambda h: True)
    monkeypatch.setattr(_atspi, "_confirm_focus_on_target", lambda h: True)
    monkeypatch.setattr(_atspi, "peer_field_texts", lambda h: {})
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_type_confirmed_handle", lambda h, t: len(t))
    monkeypatch.setattr(driver, "_raise_if_peers_changed", lambda *args: None)

    def capture(kind, label, b, config=None, *, secure=False):
        announces.append((kind, label, b, secure))

    monkeypatch.setattr(visible.visible_actions, "prepare", capture)
    assert driver.type_into(element, "secret-payload") == len("secret-payload")
    assert announces == [("Type", "Name", bounds, False)]
    assert all("secret-payload" not in str(item) for item in announces)


def test_type_text_converts_window_relative_extents_to_screen(monkeypatch):
    """GTK4 SCREEN (0,0) + WINDOW offset must recover the screen rect for Type."""
    from a11y_computer_use.drivers import _atspi

    driver = LinuxDriver()
    handle = SimpleNamespace(get_name=lambda: "Editor")
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "_extents", lambda h: ((0.0, 0.0), (100.0, 24.0)))
    monkeypatch.setattr(
        _atspi,
        "gtk4_recovered_box",
        lambda h, position, size, cache=None: ((361.0, 302.0), (100.0, 24.0)),
    )
    monkeypatch.setattr(_atspi, "published_origin_untrusted", lambda h: True)
    assert driver._screen_bounds_for_visible(handle) == Bounds(0, 361.0, 302.0, 100.0, 24.0)


def test_type_text_skips_overlay_when_screen_rect_unknown(monkeypatch):
    """Unrecovered GTK4 (0,0) SCREEN must draw no overlay (never fabricate)."""
    from a11y_computer_use.drivers import _atspi

    driver = LinuxDriver()
    handle = SimpleNamespace(get_name=lambda: "Editor")
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "_extents", lambda h: ((0.0, 0.0), (700.0, 473.0)))
    monkeypatch.setattr(_atspi, "gtk4_recovered_box", lambda *a, **k: None)
    monkeypatch.setattr(_atspi, "published_origin_untrusted", lambda h: True)
    assert driver._screen_bounds_for_visible(handle) is None


def test_type_text_keeps_non_gtk4_screen_extents(monkeypatch):
    from a11y_computer_use.drivers import _atspi

    driver = LinuxDriver()
    handle = SimpleNamespace(get_name=lambda: "Editor")
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "_extents", lambda h: ((40.0, 50.0), (120.0, 30.0)))
    monkeypatch.setattr(_atspi, "gtk4_recovered_box", lambda *a, **k: None)
    monkeypatch.setattr(_atspi, "published_origin_untrusted", lambda h: False)
    assert driver._screen_bounds_for_visible(handle) == Bounds(0, 40.0, 50.0, 120.0, 30.0)


def test_set_value_preserves_native_dispatch_even_on_overlay_failure(monkeypatch):
    from a11y_computer_use import observe
    driver = LinuxDriver()
    element = SimpleNamespace(secure=False, snapshot_id="s", ref="e", bounds=None, title="Name")
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: None)
    calls = []
    monkeypatch.setattr(driver, "_apply_set_value", lambda *args: calls.append(args) or True)
    monkeypatch.setattr(visible.visible_actions, "prepare", lambda *args, **kw: (_ for _ in ()).throw(RuntimeError("X unavailable")))
    assert driver.set_value(element, "value") is True
    assert calls == [(element, "value", None)]


def test_worker_failure_disables_and_capture_does_not_hang(feedback, monkeypatch):
    from a11y_computer_use import _x11_overlay
    monkeypatch.setattr(_x11_overlay, "X11Overlay", lambda: (_ for _ in ()).throw(OSError("display died")))
    feedback._consume()
    assert feedback.disabled
    with feedback.capture():
        assert feedback.capturing == 1
    assert feedback.capturing == 0


def test_wayland_noop(feedback, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    feedback.announce("Click", "Save", Bounds(0, 10, 20, 30, 40))
    assert feedback.pending.empty()


def test_capture_suppresses_queued_feedback(feedback):
    with feedback.capture():
        feedback.announce("Click", "Save", Bounds(0, 10, 20, 30, 40))
        assert feedback.pending.empty()


def test_cli_config():
    from a11y_computer_use.cli import _build_parser
    assert _build_parser().parse_args(["mcp", "--no-visible-actions"]).visible_actions is False


def test_x11_window_reuse_and_input_shape(monkeypatch, tmp_path):
    """Run explicitly under Xvfb; headless/platform runs retain an honest skip."""
    import shutil
    import subprocess
    import time
    if not shutil.which("Xvfb"):
        pytest.skip("requires Xvfb for isolated X11 integration")
    pytest.importorskip("Xlib")
    number_path = tmp_path / "display"
    with number_path.open("w") as output:
        process = subprocess.Popen(["Xvfb", "-displayfd", str(output.fileno()),
                                    "-screen", "0", "800x600x24"], pass_fds=(output.fileno(),))
    def cleanup():
        process.terminate()
        process.wait()
    # Fixture finalizer also runs if initialization or an assertion fails.
    import atexit
    atexit.register(cleanup)
    for _ in range(100):
        number = number_path.read_text().strip()
        if number:
            break
        time.sleep(0.01)
    assert number
    monkeypatch.setenv("DISPLAY", f":{number}")
    from Xlib import X, display
    from Xlib.ext import shape
    from a11y_computer_use._x11_overlay import X11Overlay
    connection = display.Display()
    root = connection.screen().root
    app = root.create_window(20, 20, 300, 200, 0, connection.screen().root_depth,
                             X.InputOutput, X.CopyFromParent, background_pixel=0)
    app.map()
    app.set_input_focus(X.RevertToParent, X.CurrentTime)
    root.warp_pointer(50, 50)
    connection.sync()
    backend = X11Overlay()
    try:
        before = connection.get_input_focus().focus.id
        window_id = backend.window.id
        start = time.perf_counter()
        assert backend.show(Bounds(0, 20, 20, 300, 200), "Click: Save")
        elapsed = time.perf_counter() - start
        assert backend.window.shape_get_rectangles(shape.SK.Input).rectangles == []
        assert connection.get_input_focus().focus.id == before
        assert root.query_pointer().child.id == app.id
        backend.hide()
        backend.show(Bounds(0, 30, 30, 200, 100), "Type: Name")
        assert backend.window.id == window_id
        print(f"reused overlay show: {elapsed * 1000:.3f} ms")
        assert elapsed < 0.150
    finally:
        backend.close()
        app.destroy()
        connection.close()
        cleanup()
        atexit.unregister(cleanup)


def test_capture_waits_for_worker_unmap(feedback, monkeypatch):
    import threading
    from a11y_computer_use import _x11_overlay
    mapped, hidden = threading.Event(), threading.Event()
    class Backend:
        def show(self, bounds, label):
            mapped.set()
            return True
        def hide(self):
            hidden.set()
        def close(self):
            pass
    monkeypatch.setattr(_x11_overlay, "X11Overlay", Backend)
    feedback.thread = None
    feedback.announce("Click", "Save", Bounds(0, 10, 20, 30, 40))
    assert mapped.wait(1)
    with feedback.capture():
        assert hidden.is_set()
        assert not feedback.mapped


def test_runtime_config(monkeypatch, tmp_path):
    from a11y_computer_use.server import Runtime
    from a11y_computer_use import safety
    driver = LinuxDriver()
    Runtime(driver=driver, visible_actions=False,
            store=safety.PermissionStore(tmp_path / "permissions.json"),
            audit=safety.AuditLog(tmp_path / "audit.jsonl"))
    assert driver.visible_actions is False


def test_glide_pointer_at_target_when_call_returns(feedback, monkeypatch):
    """PR #719 regression: queued XTEST motion must reach the server before
    _glide returns, or the pointer only jumps during the next X call."""
    from a11y_computer_use.drivers import _linux_input
    queued, server = [], {"pointer": (0, 0), "seen": []}

    def deliver():
        for point in queued:
            server["seen"].append(point)
            server["pointer"] = point
        queued.clear()

    screen = SimpleNamespace(width_in_pixels=1000, height_in_pixels=1000,
                             root=SimpleNamespace(query_pointer=lambda: SimpleNamespace(root_x=0, root_y=0)))
    monkeypatch.setattr(_linux_input, "_disp", lambda: SimpleNamespace(
        screen=lambda: screen, has_extension=lambda ext: True, flush=deliver, sync=deliver))
    monkeypatch.setattr(_linux_input, "_move", lambda x, y: queued.append((x, y)))
    monkeypatch.setattr(visible.time, "sleep", lambda seconds: None)
    feedback._glide(Bounds(0, 100, 200, 30, 40))
    assert queued == []
    assert server["pointer"] == (115, 220)
    # Intermediate steps reached the server too, so the glide was visible.
    assert len(server["seen"]) == 9
