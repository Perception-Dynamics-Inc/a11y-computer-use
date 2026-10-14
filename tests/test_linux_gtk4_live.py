"""Live GTK 4 bounds on gnome-calculator and gnome-text-editor.

GTK 4 reports every SCREEN origin as (0, 0). A snapshot moved off that
corner, a click on button 7, and set_value on an empty document are the
check. The AT-SPI bus has to be up. Without a display the test skips.
When the bus is up the binaries are required, the same way the mousepad
live test requires mousepad.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402


def _require_bus(driver) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY: GTK4 apps cannot open a window")
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:  # pragma: no cover - env guard
        pytest.skip(f"PyGObject/Atspi missing: {exc}")


def _stop_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def _launch(binary: str, *args: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["GTK_A11Y"] = "atspi"
    env["NO_AT_BRIDGE"] = "0"
    return subprocess.Popen([binary, *args], env=env, start_new_session=True)


def _window(driver, needle: str, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for row in driver.windows() or []:
            blob = " ".join(
                str(row.get(key) or "") for key in ("app", "title", "wm_class")
            ).casefold()
            if needle in blob and row.get("on_screen", True) and row.get("window_id"):
                return row
        time.sleep(0.2)
    return None


def _runtime(tmp_path, driver):
    from a11y_computer_use import safety, server

    store = safety.PermissionStore(tmp_path / "permissions.json")
    names = [
        "gnome-calculator", "gnome-calculato",
        "gnome-text-editor", "gnome-text-edit",
    ]
    front = driver.frontmost_app()[0]
    if front:
        names.append(front)
    for row in driver.windows() or []:
        if row.get("app"):
            names.append(str(row["app"]))
    for name in names:
        store.set_tier(name, safety.Tier.FULL)
    return server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver,
    )


def test_gtk4_calculator_click_lands_on_the_button(tmp_path) -> None:
    """Button 7 is not at screen (0, 0), and clicking it types 7.

    The window is moved away from the origin first. A coordinate click at
    the collapsed SCREEN box would hit the top-left of the display and
    leave the calculator empty.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    found = subprocess.run(["bash", "-lc", "command -v gnome-calculator"], capture_output=True, text=True)
    binary = found.stdout.strip()
    assert binary, "gnome-calculator is not installed"
    proc = _launch(binary)
    try:
        row = _window(driver, "calculat")
        assert row is not None, driver.windows()
        driver.move_window(int(row["window_id"]), 200, 100)
        driver.focus_window(int(row["window_id"]))
        runtime = _runtime(tmp_path, driver)
        button = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and button is None:
            runtime.desktop_snapshot("gnome-calculator")
            current = runtime._current
            if current is not None:
                button = next(
                    (el for el in current.elements if el.role == "AXButton" and el.title == "7"),
                    None,
                )
            time.sleep(0.25)
        assert button is not None, [
            (el.role, el.title, el.bounds.x, el.bounds.y) for el in (runtime._current.elements if runtime._current else [])
        ]
        assert not (button.bounds.x == 0 and button.bounds.y == 0), button.bounds
        assert button.bounds.y > 100, button.bounds
        runtime.click(button.ref)
        shown = None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            shot = driver.snapshot(Scope.WINDOW, "gnome-calculator")
            shown = next((el for el in shot.elements if el.role == "AXTextArea" and el.value), None)
            if shown is not None and "7" in str(shown.value):
                break
            time.sleep(0.25)
        assert shown is not None and "7" in str(shown.value), getattr(shown, "value", None)
    finally:
        _stop_group(proc)


def test_gtk4_column_view_left_edge_row_activates(tmp_path) -> None:
    """A Baobab-like flush row click updates a real GTK4 activation label."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    script = tmp_path / "column617.py"
    script.write_text('''
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
def activate(app):
    window = Gtk.ApplicationWindow(application=app, title="Column617")
    window.set_default_size(640, 400)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    status = Gtk.Label(label="Home Folder")
    model = Gtk.SingleSelection.new(Gtk.StringList.new(["Folder617", "Other617"]))
    view = Gtk.ColumnView.new(model)
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda factory, item: item.set_child(Gtk.Label(xalign=0)))
    factory.connect("bind", lambda factory, item: item.get_child().set_label(item.get_item().get_string()))
    column = Gtk.ColumnViewColumn.new("Folder", factory)
    column.set_expand(True)
    view.append_column(column)
    view.set_single_click_activate(True)
    view.connect("activate", lambda view, index: status.set_label("Opened " + model.get_item(index).get_string()))
    box.append(status)
    box.append(view)
    window.set_child(box)
    window.present()
app = Gtk.Application(application_id="org.example.Column617")
app.connect("activate", activate)
app.run([])
''')
    proc = _launch(sys.executable, str(script))
    try:
        row = _window(driver, "column617")
        assert row is not None, driver.windows()
        assert int(row.get("pid") or 0) == proc.pid, row
        driver.move_window(int(row["window_id"]), 200, 100)
        driver.focus_window(int(row["window_id"]))
        runtime = _runtime(tmp_path, driver)
        app = str(row["app"])
        deadline = time.monotonic() + 15
        target = None
        while time.monotonic() < deadline:
            runtime.desktop_snapshot(app)
            snap = runtime._current
            target = next((el for el in snap.elements
                           if el.role == "AXRow" and "Folder617" in el.title), None) if snap else None
            if target is not None:
                break
            time.sleep(0.2)
        assert target is not None, snap
        runtime.click(target.ref)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            shot = driver.snapshot(Scope.WINDOW, app)
            if any(el.title == "Opened Folder617" for el in shot.elements):
                break
            time.sleep(0.2)
        assert any(el.title == "Opened Folder617" for el in shot.elements), shot
    finally:
        _stop_group(proc)


def _largest_editable(elements):
    """The document, not a smaller field that appears while the window is opening."""
    areas = [el for el in elements if el.role == "AXTextArea" and el.editable]
    if not areas:
        return None
    return max(areas, key=lambda el: el.bounds.width * el.bounds.height)


def _same_box(left, right) -> bool:
    if left is None or right is None or left.path != right.path:
        return False
    return (
        abs(left.bounds.x - right.bounds.x) < 8
        and abs(left.bounds.y - right.bounds.y) < 8
        and abs(left.bounds.width - right.bounds.width) < 8
        and abs(left.bounds.height - right.bounds.height) < 8
    )


def test_gtk4_text_editor_set_value_is_not_covered(tmp_path) -> None:
    """An empty document is not reported covered because its box was (0, 0).

    The editor is moved off the origin. set_value writes through
    EditableText, and the covered check uses the recovered box. The
    document has to keep the same path and box across two snapshots:
    the first tree while the window is opening does not rematch.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    found = subprocess.run(["bash", "-lc", "command -v gnome-text-editor"], capture_output=True, text=True)
    binary = found.stdout.strip()
    assert binary, "gnome-text-editor is not installed"
    proc = _launch(binary)
    try:
        row = _window(driver, "text-edit")
        assert row is not None, driver.windows()
        driver.move_window(int(row["window_id"]), 180, 80)
        driver.focus_window(int(row["window_id"]))
        runtime = _runtime(tmp_path, driver)
        area = None
        previous = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            runtime.desktop_snapshot("gnome-text-editor")
            current = runtime._current
            found_area = _largest_editable(current.elements) if current is not None else None
            if _same_box(found_area, previous):
                area = found_area
                break
            previous = found_area
            time.sleep(0.25)
        assert area is not None, [
            (el.role, el.path, el.bounds.x, el.bounds.y, el.bounds.width, el.bounds.height, el.editable)
            for el in (runtime._current.elements if runtime._current else [])
            if el.role == "AXTextArea"
        ]
        assert not (area.bounds.x == 0 and area.bounds.y == 0), area.bounds
        assert driver.occlusion(area, "gnome-text-editor") is None

        def write() -> None:
            runtime.desktop_snapshot("gnome-text-editor")
            current = runtime._current
            assert current is not None
            target = _largest_editable(current.elements)
            assert target is not None
            assert not (target.bounds.x == 0 and target.bounds.y == 0), target.bounds
            runtime.set_value(target.ref, "hello")

        try:
            write()
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.STALE_REF:
                raise
            write()
        shot = driver.snapshot(Scope.WINDOW, "gnome-text-editor")
        shown = next(el for el in shot.elements if el.role == "AXTextArea" and el.editable)
        assert shown.value == "hello", shown.value
    finally:
        _stop_group(proc)


@pytest.mark.parametrize("kind", ["plain", "masked", "password"])
def test_gtk4_entry_visibility_snapshot_find_and_writes(tmp_path, kind):
    """Real layout strings distinguish Gtk.Entry masking, unlike GetText."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    script = tmp_path / "entry584.py"
    script.write_text(f'''
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
def activate(app):
    window = Gtk.ApplicationWindow(application=app, title="Entry584")
    entry = Gtk.PasswordEntry() if {kind!r} == "password" else Gtk.Entry()
    if {kind!r} == "masked":
        entry.set_visibility(False)
    entry.set_text("ProbeSecret584")
    entry.update_property([Gtk.AccessibleProperty.LABEL], ["Field584"])
    window.set_child(entry)
    window.present()
app = Gtk.Application(application_id="org.example.Entry584")
app.connect("activate", activate)
app.run([])
''')
    proc = _launch(sys.executable, str(script))
    try:
        row = _window(driver, "entry584")
        assert row is not None, driver.windows()
        driver.focus_window(int(row["window_id"]))
        runtime = _runtime(tmp_path, driver)
        deadline = time.monotonic() + 15
        while True:
            runtime.desktop_snapshot(str(row["app"]))
            snap = runtime._current
            fields = [el for el in snap.elements if el.title == "Field584"] if snap else []
            if fields or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        assert len(fields) == 1
        field = fields[0]
        if kind == "plain":
            assert field.value == "ProbeSecret584"
            assert observe.find_elements(snap, text="ProbeSecret584")
        else:
            assert field.role == "AXSecureTextField"
            assert field.value is None
            assert "ProbeSecret584" not in repr(snap)
            assert not observe.find_elements(snap, text="ProbeSecret584")
            for operation in (
                lambda: runtime.set_value(field.ref, "ReplacementSecret584"),
                lambda: runtime.type_text("ReplacementSecret584", ref=field.ref),
            ):
                with pytest.raises(ComputerUseError) as caught:
                    operation()
                assert caught.value.code is ErrorCode.SECURE_FIELD
                assert "ReplacementSecret584" not in str(caught.value)
                assert "ProbeSecret584" not in str(caught.value)
    finally:
        _stop_group(proc)


@pytest.mark.parametrize("kind", ["plain", "masked", "password"])
@pytest.mark.parametrize("operation", ["type", "set"])
def test_gtk4_empty_entry_first_write_is_private(tmp_path, kind, operation):
    """An empty unknown entry allows one privately verified, redacted write."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    script = tmp_path / "entry584.py"
    script.write_text(f'''
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
def activate(app):
    window = Gtk.ApplicationWindow(application=app, title="Entry584")
    entry = Gtk.PasswordEntry() if {kind!r} == "password" else Gtk.Entry()
    if {kind!r} == "masked":
        entry.set_visibility(False)

    entry.update_property([Gtk.AccessibleProperty.LABEL], ["Field584"])
    window.set_child(entry)
    window.present()
app = Gtk.Application(application_id="org.example.Entry584")
app.connect("activate", activate)
app.run([])
''')
    proc = _launch(sys.executable, str(script))
    try:
        row = _window(driver, "entry584")
        assert row is not None, driver.windows()
        driver.focus_window(int(row["window_id"]))
        runtime = _runtime(tmp_path, driver)
        deadline = time.monotonic() + 15
        while True:
            runtime.desktop_snapshot(str(row["app"]))
            snap = runtime._current
            fields = [el for el in snap.elements if el.title == "Field584"] if snap else []
            if fields or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        assert len(fields) == 1
        field = fields[0]
        secret = "FirstWrite584"
        if kind == "password":
            assert field.secure
            for operation in (lambda: runtime.type_text(secret, ref=field.ref),
                              lambda: runtime.set_value(field.ref, secret)):
                with pytest.raises(ComputerUseError) as caught:
                    operation()
                assert caught.value.code is ErrorCode.SECURE_FIELD
                assert secret not in str(caught.value)
        else:
            assert not field.secure
            result = (runtime.type_text(secret, ref=field.ref) if operation == "type"
                      else runtime.set_value(field.ref, secret))
            assert result.outcome == "confirmed"
            assert secret not in str(result) + repr(result.as_dict())
            audit_files = list(runtime.audit.dir_path.glob("*.jsonl"))
            assert audit_files
            audit_text = "".join(path.read_text() for path in audit_files)
            assert audit_text
            assert secret not in audit_text
            runtime.desktop_snapshot(str(row["app"]))
            snap = runtime._current
            field = next(el for el in snap.elements if el.title == "Field584")
            if kind == "plain":
                assert field.value == secret
                assert observe.find_elements(snap, text=secret)
            else:
                assert field.secure and field.value is None
                assert secret not in repr(snap)
                assert not observe.find_elements(snap, text=secret)
    finally:
        _stop_group(proc)
