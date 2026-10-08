"""Live Linux-backend test — runs on the ubuntu CI runner under Xvfb + a real
AT-SPI2 bus.

Proves the Linux `Driver` walks a real AT-SPI2 tree through the SHARED pruning
engine and drives it accessibility-first: it launches a tiny GTK window (a
labelled button + a text entry), snapshots it, **presses the button through the
AT-SPI action API** (no pointer movement — the a11y-first payoff) and observes
the effect, and **enters text through AT-SPI EditableText** (deterministic; needs
no widget focus, which headless X cannot grant). macOS/Windows skip this file; it
also self-skips when the AT-SPI bus is unreachable, so it never breaks a
non-configured environment.

Note: synthetic key chords (`key_chord`) go through XTEST, which requires real
widget focus and so is only meaningful in a full desktop session — its parsing
is covered by tests/test_linux_synthetic.py; its live effect is not asserted here.
"""

from __future__ import annotations

import subprocess
import os
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Point, Scope  # noqa: E402

_APP = "cuatestapp"

_GTK_APP = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    GLib.set_prgname("cuatestapp")
    win = Gtk.Window(title="cuatestapp")
    win.set_name("cuatestapp")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    entry = Gtk.Entry()
    btn = Gtk.Button(label="Save")
    # Pressing the button (via AT-SPI do_action) has an observable effect,
    # so the test can prove the a11y activation actually fired.
    btn.connect("clicked", lambda _b: entry.set_text("SAVED"))
    box.pack_start(btn, False, False, 0)
    box.pack_start(entry, False, False, 0)
    win.add(box)
    win.set_default_size(400, 200)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.present()
    Gtk.main()
    """
)


def _require_bus(driver) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY: the GTK test app cannot open a window")
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:  # pragma: no cover - env guard
        pytest.skip(f"PyGObject/Atspi missing: {exc}")


def _launch_app(tmp_path) -> subprocess.Popen:
    script = tmp_path / "cuatestapp.py"
    script.write_text(_GTK_APP)
    return subprocess.Popen([sys.executable, str(script)])


def _wait_for_snapshot(driver, timeout_s: float = 15.0):
    """Poll until the GTK app is on the bus and its entry is in the snapshot.

    Until the process registers, snapshot is ``app_not_found``. That is the
    same answer as a name that was never launched, so the wait retries it.
    Any other error stops the wait.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = driver.snapshot(Scope.WINDOW, _APP)
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            last = None
        else:
            if last.elements and any(el.editable for el in last.elements):
                return last
        time.sleep(0.5)
    return last


def test_linux_driver_reports_its_name() -> None:
    from a11y_computer_use.drivers import get_driver

    assert get_driver().name == "linux"


def test_linux_atspi_snapshot_of_gtk_app(tmp_path) -> None:
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        assert snap and snap.elements, "AT-SPI walk produced no elements"
        roles = {el.role for el in snap.elements}
        assert "AXWindow" in roles, f"no window in snapshot; roles={roles}"
        assert snap.elements[0].ref == "e1"  # shared engine indexed it
        assert any(el.clickable and "Save" in el.title for el in snap.elements), \
            f"no Save button; {[(e.role, e.title) for e in snap.elements]}"
        assert any(el.editable for el in snap.elements), f"no editable entry; roles={roles}"
    finally:
        proc.terminate()


def test_linux_a11y_press_button(tmp_path) -> None:
    """The a11y-first activation path: press the Save button through the AT-SPI
    action API (no pointer movement); its handler sets the entry to 'SAVED',
    confirmed by a re-snapshot."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        button = next((el for el in snap.elements if el.clickable and "Save" in el.title), None)
        assert button is not None, f"no Save button; {[(e.role, e.title) for e in snap.elements]}"

        assert driver.press_element(driver.resolve_ref(snap, button.ref)), "AT-SPI press failed"
        time.sleep(0.4)

        after = driver.snapshot(Scope.WINDOW, _APP)
        values = [el.value for el in after.elements if el.value]
        assert any("SAVED" in (v or "") for v in values), f"button press had no effect; {values}"
    finally:
        proc.terminate()


def test_linux_a11y_type_text(tmp_path) -> None:
    """Ref typing needs a verified app owner, but not widget focus. A desktop
    with no window manager must refuse implicit typing instead of trusting an
    old handle; explicit set_value is verified separately below."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        entry = next((el for el in snap.elements if el.editable), None)
        assert entry is not None, f"no editable entry; roles={[e.role for e in snap.elements]}"

        driver.press_element(driver.resolve_ref(snap, entry.ref))  # records + focuses it
        time.sleep(0.3)
        if driver.frontmost_app()[0] is None:
            with pytest.raises(ComputerUseError) as error:
                driver.type_text("must not enter an unverified app")
            assert error.value.code is ErrorCode.FOCUS_CHANGED
            after = driver.snapshot(Scope.WINDOW, _APP)
            assert not any("must not enter" in (el.value or "") for el in after.elements)
            return
        driver.type_text("hello atspi")
        time.sleep(0.3)
        driver.type_text(" more")  # a second call appends — insert-at-caret semantics
        time.sleep(0.3)

        after = driver.snapshot(Scope.WINDOW, _APP)
        values = [el.value for el in after.elements if el.value]
        assert any("hello atspi more" in (v or "") for v in values), f"typed text missing; {values}"
    finally:
        proc.terminate()


def test_linux_explicit_set_value_without_frontmost(tmp_path, monkeypatch) -> None:
    """An explicit element target remains usable on a headless desktop."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        entry = next((el for el in snap.elements if el.editable), None)
        assert entry is not None
        target = driver.resolve_ref(snap, entry.ref)
        monkeypatch.setattr(driver, "frontmost_app", lambda: (None, None))
        assert driver.set_value(target, "explicit headless text")
        after = driver.snapshot(Scope.WINDOW, _APP)
        assert any(el.value == "explicit headless text" for el in after.elements)
    finally:
        proc.terminate()


def test_linux_forces_a11y_status() -> None:
    """The org.a11y.Status flip: after the driver initializes, the desktop's
    accessibility flags are on so Chromium/Electron apps expose their tree with
    no relaunch (the Linux counter to Grok's a11y-OFF desktop)."""
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import GLib, Gio

    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)  # ensure_trusted() -> enable_a11y_status()

    def getp(name: str):
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        r = bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.a11y.Status", name)), GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE, -1, None,
        )
        return r.unpack()[0]

    assert getp("IsEnabled") is True
    assert getp("ScreenReaderEnabled") is True


def _pointer_xy():
    """Current pointer position from the X server (None if Xlib is unavailable)."""
    d = None
    try:
        from Xlib import display as _xd

        d = _xd.Display()
        q = d.screen().root.query_pointer()
        return int(q.root_x), int(q.root_y)
    except Exception:  # pragma: no cover - environment guard
        return None
    finally:
        if d is not None:
            try:
                d.close()
            except Exception:
                pass


def test_linux_coordinate_click_lands_with_pointer_away_from_origin(tmp_path) -> None:
    """The coordinate/vision-fallback click: position the pointer ABSOLUTELY and
    press. The pointer is first parked away from (0, 0) on purpose: under Xvfb it
    starts at the origin, where a relative warp and an absolute move coincide,
    which is how a relative `warp_pointer` shipped unnoticed until a real desktop
    (Budgie/Xorg, docs/box-testbed.md) put every click at pointer + (x, y)."""
    from a11y_computer_use.drivers import _linux_input
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Point

    driver = LinuxDriver()
    _require_bus(driver)
    if _pointer_xy() is None:
        pytest.skip("no X pointer (python-xlib or DISPLAY unavailable)")
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        button = next((el for el in snap.elements if el.clickable and "Save" in el.title), None)
        assert button is not None, f"no Save button; {[(e.role, e.title) for e in snap.elements]}"
        b = button.bounds
        cx, cy = int(b.x + b.width / 2), int(b.y + b.height / 2)

        _linux_input._move(cx + 150, cy + 120)  # park the pointer off-origin, off-target
        _linux_input._flush()
        parked = _pointer_xy()
        assert parked != (cx, cy), "test setup: pointer must start away from the target"

        try:
            driver.click(Point(display_id=driver.main_display_id(), x=cx, y=cy))
        except ComputerUseError as exc:
            pytest.skip(f"coordinate input unsupported here: {exc.message}")
        time.sleep(0.4)

        assert _pointer_xy() == (cx, cy), f"pointer ended at {_pointer_xy()}, wanted {(cx, cy)}"
        after = driver.snapshot(Scope.WINDOW, _APP)
        values = [el.value for el in after.elements if el.value]
        assert any("SAVED" in (v or "") for v in values), f"coordinate click missed; {values}"
    finally:
        proc.terminate()


def test_linux_runtime_click_without_display_id(tmp_path) -> None:
    """`Runtime.click(x, y)` with no display_id must resolve the default display
    through the driver (it used to call Quartz unconditionally and raise
    NameError off macOS). Needs a window manager so the GTK app is the active
    window the gate keys on; self-skips when there is none (Xvfb without a WM)."""
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers import _linux_input
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    if _pointer_xy() is None:
        pytest.skip("no X pointer (python-xlib or DISPLAY unavailable)")
    proc = _launch_app(tmp_path)
    try:
        snap = _wait_for_snapshot(driver)
        button = next((el for el in snap.elements if el.clickable and "Save" in el.title), None)
        assert button is not None
        b = button.bounds
        cx, cy = int(b.x + b.width / 2), int(b.y + b.height / 2)

        store = safety.PermissionStore(tmp_path / "permissions.json")
        rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
        front = rt._frontmost()
        if not front:
            pytest.skip("no active window (no window manager): the gate has no app to key on")
        store.set_tier(front, safety.Tier.FULL)

        _linux_input._move(cx + 150, cy + 120)
        _linux_input._flush()
        try:
            msg = rt.click(x=cx, y=cy)  # no display_id: the driver seam supplies it
        except ComputerUseError as exc:
            if exc.code is ErrorCode.UNSUPPORTED:
                pytest.skip(f"coordinate input unsupported here: {exc.message}")
            raise
        assert msg.startswith("clicked (") and "on display" in msg, msg
        assert _pointer_xy() == (cx, cy)
    finally:
        proc.terminate()


_TYPE_APP = "cuatypeapp"

_GTK_TYPE_APP = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    GLib.set_prgname("cuatypeapp")
    win = Gtk.Window(title="cuatypeapp")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    bar = Gtk.MenuBar()
    edit = Gtk.MenuItem(label="Edit")
    menu = Gtk.Menu()
    undo = Gtk.MenuItem(label="Undo")
    undo.set_sensitive(False)
    menu.append(undo)
    edit.set_submenu(menu)
    bar.append(edit)
    box.pack_start(bar, False, False, 0)
    entry = Gtk.Entry()
    entry.get_accessible().set_name("single")
    view = Gtk.TextView()
    view.get_accessible().set_name("multi")
    view.set_size_request(200, 80)
    disabled = Gtk.Button(label="Save")
    disabled.set_sensitive(False)
    box.pack_start(entry, False, False, 0)
    box.pack_start(view, True, True, 0)
    box.pack_start(disabled, False, False, 0)
    win.add(box)
    win.set_default_size(420, 280)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    win.present()
    Gtk.main()
    """
)


def _launch_type_app(tmp_path) -> subprocess.Popen:
    script = tmp_path / "cuatypeapp.py"
    script.write_text(_GTK_TYPE_APP)
    return subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_named(driver, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = driver.snapshot(Scope.WINDOW, _TYPE_APP)
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            last = None
        else:
            titles = {el.title for el in last.elements}
            if {"single", "multi", "Save"} <= titles:
                return last
        time.sleep(0.5)
    return last


def _value_of(snap, title: str):
    return next(el.value for el in snap.elements if el.title == title)


def test_linux_gtk_type_caret_unicode_and_disabled_controls(tmp_path) -> None:
    """Live AT-SPI: byte-exact insert, caret, selection, CRLF, entry role, disabled click and menu."""
    import json

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_type_app(tmp_path)
    try:
        snap = _wait_named(driver)
        assert snap is not None, "the GTK type window never exposed single, multi, and Save"
        single = next(el for el in snap.elements if el.title == "single")
        multi = next(el for el in snap.elements if el.title == "multi")
        save = next(el for el in snap.elements if el.title == "Save")
        assert single.role == "AXTextField", single.role
        assert multi.role == "AXTextArea", multi.role
        assert save.enabled is False and save.clickable

        if driver.frontmost_app()[0] is None:
            pytest.skip("no active window: caret typing and the disabled click need a window manager")

        def retype(text: str, *, into: str = "") -> str:
            live = driver.resolve_ref(snap, single.ref)
            assert driver.set_value(live, into)
            count = driver.type_text(text)
            assert count == len(text.replace("\r\n", "\n")), count
            got = _value_of(driver.snapshot(Scope.WINDOW, _TYPE_APP), "single")
            return got

        for sample in ("Привет", "中文字", "ok 😀", "naïve café"):
            assert retype(sample) == sample

        assert retype("a\r\nb") == "a\nb"

        live = driver.resolve_ref(snap, single.ref)
        assert driver.set_value(live, "world")
        assert Atspi.Text.set_caret_offset(driver._focused_editable, 0)
        assert driver.type_text("hello ") == 6
        assert _value_of(driver.snapshot(Scope.WINDOW, _TYPE_APP), "single") == "hello world"

        assert driver.set_value(driver.resolve_ref(snap, single.ref), "keep DROP keep")
        handle = driver._focused_editable
        from a11y_computer_use.drivers import _atspi

        _atspi.grab_focus(handle)
        selected = bool(Atspi.Text.set_selection(handle, 0, 5, 9)) or bool(
            Atspi.Text.add_selection(handle, 5, 9)
        )
        assert selected, "the entry did not accept a selection on DROP"
        assert driver.type_text("NEW") == 3
        assert _value_of(driver.snapshot(Scope.WINDOW, _TYPE_APP), "single") == "keep NEW keep"

        store = safety.PermissionStore(tmp_path / "permissions.json")
        store.set_tier(_TYPE_APP, safety.Tier.FULL)
        front = driver.frontmost_app()[0]
        if front and front != _TYPE_APP:
            store.set_tier(front, safety.Tier.FULL)
        rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
        current = driver.snapshot(Scope.WINDOW, _TYPE_APP)
        rt._current = current
        save_now = next(el for el in current.elements if el.title == "Save")
        with pytest.raises(ComputerUseError) as exc:
            rt.click(save_now.ref)
        assert exc.value.code is ErrorCode.ELEMENT_DISABLED
        batch = json.loads(rt.act_batch([{"do": "click", "ref": save_now.ref}]))
        assert batch[0]["ok"] is False and batch[0]["error"].startswith("element_disabled:")
        assert _value_of(driver.snapshot(Scope.WINDOW, _TYPE_APP), "single") == "keep NEW keep"

        with pytest.raises(ComputerUseError) as exc:
            driver.menu_press(_TYPE_APP, "Edit > Undo")
        assert exc.value.detail["reason"] == "disabled"
        assert driver.menu_state(_TYPE_APP) == {"open": False, "path": []}

        def click_then_type(sample: str) -> str:
            live = driver.resolve_ref(snap, single.ref)
            assert driver.set_value(live, "")
            fresh = next(el for el in driver.snapshot(Scope.WINDOW, _TYPE_APP).elements if el.title == "single")
            box = fresh.bounds
            assert box is not None
            driver.click(Point(box.display_id, box.x + box.width // 2, box.y + max(box.height // 2, 1)))
            assert driver._focused_editable is None
            deadline = time.monotonic() + 5
            focused = False
            while time.monotonic() < deadline:
                now = next(
                    el for el in driver.snapshot(Scope.WINDOW, _TYPE_APP).elements if el.title == "single"
                )
                if now.focused:
                    focused = True
                    break
                time.sleep(0.1)
            assert focused, "the coordinate click did not focus the entry"
            count = driver.type_text(sample)
            assert count == len(sample), count
            return _value_of(driver.snapshot(Scope.WINDOW, _TYPE_APP), "single")

        for sample in ("Привет", "中文字", "ok 😀", "ab ✓ ok"):
            assert click_then_type(sample) == sample
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


_VALUE_APP = "cuavalueapp"

_GTK_VALUE_APP = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    GLib.set_prgname("cuavalueapp")
    win = Gtk.Window(title="cuavalueapp")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    color = Gtk.ComboBoxText()
    color.get_accessible().set_name("Color")
    for item in ("Red", "Green", "Blue"):
        color.append_text(item)
    color.set_active(0)
    city = Gtk.ComboBoxText.new_with_entry()
    city.get_accessible().set_name("City")
    city.append_text("Paris")
    city.append_text("Lima")
    city.get_child().set_text("Paris")
    city.get_child().get_accessible().set_name("CityEntry")
    notes = Gtk.Entry()
    notes.set_text("Paris")
    notes.get_accessible().set_name("Notes")
    color_probe = Gtk.Label(label="color=Red")
    color.connect("changed", lambda widget: color_probe.set_text(
        "color=%s" % (widget.get_active_text() or "")))
    spin = Gtk.SpinButton.new_with_range(0, 10, 1)
    spin.set_value(3)
    spin.get_accessible().set_name("Quantity")
    qty = Gtk.Label(label="qty=3.0")
    spin.connect("value-changed", lambda widget: qty.set_text("qty=%s" % widget.get_value()))
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
    scale.set_value(40)
    scale.set_size_request(180, -1)
    scale.get_accessible().set_name("Volume")
    vol = Gtk.Label(label="vol=40.0")
    scale.connect("value-changed", lambda widget: vol.set_text("vol=%s" % widget.get_value()))
    store = Gtk.ListStore(str)
    for name in ("Row A", "Row B", "Row C", "Row D", "Row E"):
        store.append([name])
    tree = Gtk.TreeView(model=store)
    tree.set_headers_visible(False)
    tree.get_accessible().set_name("Rows")
    column = Gtk.TreeViewColumn("row", Gtk.CellRendererText(), text=0)
    tree.append_column(column)
    selection = tree.get_selection()
    selection.select_path("1")
    row_probe = Gtk.Label(label="row=Row B")
    def on_select(sel):
        model, iterator = sel.get_selected()
        text = model.get_value(iterator, 0) if iterator is not None else ""
        row_probe.set_text("row=%s" % text)
    selection.connect("changed", on_select)
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_size_request(200, 140)
    scrolled.add(tree)
    for child in (color, city, notes, color_probe, spin, qty, scale, vol, scrolled, row_probe):
        box.pack_start(child, False, False, 0)
    win.add(box)
    win.set_default_size(420, 520)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    city.get_child().grab_focus()
    win.present()
    Gtk.main()
    """
)


def _launch_value_app(tmp_path) -> subprocess.Popen:
    script = tmp_path / "cuavalueapp.py"
    script.write_text(_GTK_VALUE_APP)
    return subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_value_app(driver, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = driver.snapshot(Scope.WINDOW, _VALUE_APP)
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            last = None
        else:
            titles = {el.title for el in last.elements}
            if {"Color", "Notes", "Quantity", "Volume", "Row C", "color=Red"} <= titles:
                return last
        time.sleep(0.4)
    return last


def _by_title(snap, title, role=None):
    matches = [el for el in snap.elements if el.title == title and (role is None or el.role == role)]
    assert matches, f"no {title!r} role={role}; {[(e.role, e.title, e.value) for e in snap.elements]}"
    return matches[0]


def test_linux_combo_spin_slider_and_tree_selection(tmp_path) -> None:
    """Live AT-SPI: combo selection, spin/slider Value, and a tree-row click.

    The probes are labels the widgets update from their own signals, so a
    text-only write that leaves the GTK value alone fails. Not a coordinate
    session beyond the row-center fallback the driver may use.
    """
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc = _launch_value_app(tmp_path)
    try:
        snap = _wait_value_app(driver)
        assert snap is not None, "the GTK value window never exposed its controls"
        store = safety.PermissionStore(tmp_path / "permissions.json")
        store.set_tier(_VALUE_APP, safety.Tier.FULL)
        front = driver.frontmost_app()[0]
        if front and front != _VALUE_APP:
            store.set_tier(front, safety.Tier.FULL)
        runtime = server.Runtime(
            store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver,
        )

        def current():
            shot = driver.snapshot(Scope.WINDOW, _VALUE_APP)
            runtime._current = shot
            return shot

        shot = current()
        color = _by_title(shot, "Color", "AXComboBox")
        with pytest.raises(ValueError) as exc:
            runtime.set_value(color.ref, "Mars")
        assert "Red" in str(exc.value) and "Green" in str(exc.value) and "Blue" in str(exc.value)
        shot = current()
        assert any(el.title == "color=Red" for el in shot.elements)
        assert _by_title(shot, "Notes").value == "Paris"

        city_entry = _by_title(shot, "CityEntry")
        runtime.click(city_entry.ref)
        shot = current()
        color = _by_title(shot, "Color", "AXComboBox")
        assert "set " in runtime.set_value(color.ref, "Green")
        shot = current()
        assert any(el.title == "color=Green" for el in shot.elements), \
            [(el.role, el.title, el.value) for el in shot.elements]
        assert _by_title(shot, "Notes").value == "Paris"
        assert _by_title(shot, "CityEntry").value == "Paris"
        assert _by_title(shot, "Color", "AXComboBox").expanded is not True

        city = _by_title(shot, "City", "AXComboBox")
        runtime.set_value(city.ref, "Lima")
        shot = current()
        assert _by_title(shot, "CityEntry").value == "Lima"
        assert _by_title(shot, "Notes").value == "Paris"
        assert _by_title(shot, "City", "AXComboBox").value == "Lima"

        quantity = _by_title(shot, "Quantity")
        with pytest.raises(ValueError) as exc:
            runtime.set_value(quantity.ref, "15")
        assert "0" in str(exc.value) and "10" in str(exc.value)
        with pytest.raises(ValueError):
            runtime.set_value(quantity.ref, "abc")
        shot = current()
        assert any(el.title == "qty=3.0" for el in shot.elements)
        quantity = _by_title(shot, "Quantity")
        runtime.set_value(quantity.ref, "7")
        shot = current()
        assert any(el.title == "qty=7.0" for el in shot.elements), \
            [el.title for el in shot.elements]

        volume = _by_title(shot, "Volume", "AXSlider")
        runtime.set_value(volume.ref, "55")
        shot = current()
        assert any(el.title == "vol=55.0" for el in shot.elements), \
            [el.title for el in shot.elements]

        rows = [el for el in shot.elements if el.title == "Row C" and el.clickable]
        assert rows, [(el.role, el.title, el.clickable) for el in shot.elements if "Row" in el.title]
        row = rows[0]
        result = runtime.click(row.ref)
        assert "clicked" in result
        deadline = time.monotonic() + 3
        chosen = ""
        while time.monotonic() < deadline:
            shot = current()
            chosen = next((el.title for el in shot.elements if el.title.startswith("row=")), "")
            if chosen == "row=Row C":
                break
            time.sleep(0.2)
        assert chosen == "row=Row C", [(el.role, el.title, el.selected) for el in shot.elements]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _chrome_binary() -> str | None:
    import shutil

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def test_linux_chrome_form_state_and_set_value(tmp_path) -> None:
    """Live Chrome AT-SPI: selected option text, pressed, empty number, rejected values.

    Skips when no Chrome binary is on PATH. The Linux CI image has one. A
    missing accessibility bus skips the same way the GTK tests do.
    """
    import shutil

    from a11y_computer_use import observe, safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver

    binary = _chrome_binary()
    if binary is None:
        pytest.skip("no Chrome/Chromium binary for the AT-SPI form-state test")
    driver = LinuxDriver()
    _require_bus(driver)
    page = tmp_path / "form.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>cuachromeform</title>"
        "<label>Country <select id=country aria-label=Country>"
        "<option selected>Kazakhstan</option><option>Japan</option><option>Peru</option>"
        "</select></label>"
        "<select id=fruits aria-label=Fruits multiple size=4>"
        "<option>Apple</option><option selected>Banana</option>"
        "<option>Cherry</option><option>Date</option></select>"
        "<button id=italic aria-pressed=false>Italic toggle</button>"
        "<script>document.getElementById('italic').addEventListener('click', function () {"
        "var on = this.getAttribute('aria-pressed') === 'true';"
        "this.setAttribute('aria-pressed', on ? 'false' : 'true');});</script>"
        "<label>Seats <input id=seats type=number aria-label=Seats value=3></label>"
        "<label>Empty <input id=empty type=number aria-label=Empty></label>"
        "<input id=volume type=range aria-label=Volume min=0 max=100 value=40>"
    )
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    url = page.resolve().as_uri()
    proc = subprocess.Popen(
        [
            binary, "--force-renderer-accessibility", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--window-size=1000,800", url,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 25
        snap = None
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, "chrome")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                shot = None
            else:
                rendered = observe.render_text(shot)
                if "Kazakhstan" in rendered and "Italic toggle" in rendered and "Seats" in rendered:
                    snap = shot
                    break
            time.sleep(0.5)
        assert snap is not None, "Chrome did not expose the form through AT-SPI"
        rendered = observe.render_text(snap)
        assert "\ufffc" not in rendered, rendered
        assert "Kazakhstan" in rendered
        assert "Banana" in rendered
        empty = next(el for el in snap.elements if el.title == "Empty")
        assert empty.value in (None, ""), rendered
        seats = next(el for el in snap.elements if el.title == "Seats")
        assert seats.value is not None and "3" in str(seats.value)
        store = safety.PermissionStore(tmp_path / "chrome-permissions.json")
        store.set_tier("chrome", safety.Tier.FULL)
        front = driver.frontmost_app()[0]
        if front and front != "chrome":
            store.set_tier(front, safety.Tier.FULL)
        runtime = server.Runtime(
            store=store, audit=safety.AuditLog(tmp_path / "chrome-audit"), driver=driver,
        )
        runtime._current = snap
        country = next(
            el for el in snap.elements
            if el.title == "Country" and el.role in {"AXComboBox", "AXPopUpButton", "AXList"}
        )
        with pytest.raises(ValueError) as exc:
            runtime.set_value(country.ref, "Mars")
        assert "Kazakhstan" in str(exc.value) and "Japan" in str(exc.value) and "Peru" in str(exc.value)
        after = driver.snapshot(Scope.WINDOW, "chrome")
        assert "\ufffc" not in observe.render_text(after)
        seats = next(el for el in after.elements if el.title == "Seats")
        runtime._current = after
        with pytest.raises(ValueError):
            runtime.set_value(seats.ref, "abc")
        still = driver.snapshot(Scope.WINDOW, "chrome")
        seats = next(el for el in still.elements if el.title == "Seats")
        assert seats.value is not None and "3" in str(seats.value)
        volume = next((el for el in still.elements if el.title == "Volume"), None)
        if volume is not None and volume.role == "AXSlider":
            runtime._current = still
            runtime.set_value(volume.ref, "55")
        toggle = next(el for el in still.elements if el.title == "Italic toggle")
        runtime._current = still
        runtime.click(toggle.ref)
        pressed = None
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            shot = driver.snapshot(Scope.WINDOW, "chrome")
            button = next(el for el in shot.elements if el.title == "Italic toggle")
            pressed = button.checked
            if pressed is True:
                break
            time.sleep(0.3)
        assert pressed is True, observe.render_text(shot)
        shutil.rmtree(profile, ignore_errors=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
