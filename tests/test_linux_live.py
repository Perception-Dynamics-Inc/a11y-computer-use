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
        quantity = _by_title(shot, "Quantity")
        held = runtime.set_value(quantity.ref, "4.6")
        assert "4.6" not in held and "'5'" in held, held
        shot = current()
        assert any(el.title == "qty=5.0" for el in shot.elements), \
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
        "<option>Cherry</option><option selected>Date</option></select>"
        "<button id=italic aria-pressed=false>Italic toggle</button>"
        "<script>document.getElementById('italic').addEventListener('click', function () {"
        "var on = this.getAttribute('aria-pressed') === 'true';"
        "this.setAttribute('aria-pressed', on ? 'false' : 'true');});</script>"
        "<label>Seats <input id=seats type=number aria-label=Seats value=3></label>"
        "<label>Empty <input id=empty type=number aria-label=Empty></label>"
        "<label>Guests <input id=guests type=number aria-label=Guests min=0 max=12></label>"
        "<div id=gstat role=status aria-label=gstat=>gstat=</div>"
        "<script>document.getElementById('guests').addEventListener('input', function () {"
        "var text = 'gstat=' + this.value;"
        "var node = document.getElementById('gstat');"
        "node.textContent = text; node.setAttribute('aria-label', text);});</script>"
        "<select id=colors aria-label=Colors size=4>"
        "<option>Red</option><option>Green</option><option>Blue</option><option>Gray</option>"
        "</select>"
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
        # Chrome on a busy runner can take well over 10s to publish the
        # document (the same image's CDP port has taken ~10s). The strings
        # below still all have to be present; this only waits longer.
        deadline = time.monotonic() + 45
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
                if (
                    "Kazakhstan" in rendered and "Italic toggle" in rendered
                    and "Seats" in rendered and "Guests" in rendered and "Colors" in rendered
                    and "gstat=" in rendered
                ):
                    snap = shot
                    break
            time.sleep(0.5)
        assert snap is not None, "Chrome did not expose the form through AT-SPI"
        rendered = observe.render_text(snap)
        assert "\ufffc" not in rendered, rendered
        assert "Kazakhstan" in rendered
        assert "Banana" in rendered
        fruits = next(el for el in snap.elements if el.title == "Fruits")
        assert fruits.value and "Banana" in str(fruits.value) and "Date" in str(fruits.value), rendered
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

        def _choice(shot, title):
            return next(
                el for el in shot.elements
                if el.title == title and el.role in {"AXComboBox", "AXPopUpButton", "AXList"}
            )

        def _set_country(value: str) -> None:
            shot = driver.snapshot(Scope.WINDOW, "chrome")
            country = _choice(shot, "Country")
            runtime._current = shot
            result = runtime.set_value(country.ref, value)
            assert result.startswith("set "), result
            deadline = time.monotonic() + 4
            shown = expanded = None
            last = shot
            while time.monotonic() < deadline:
                last = driver.snapshot(Scope.WINDOW, "chrome")
                country = _choice(last, "Country")
                shown, expanded = country.value, country.expanded
                if shown == value and expanded is not True:
                    return
                time.sleep(0.25)
            assert shown == value and expanded is not True, observe.render_text(last)

        _set_country("Kazakhstan")
        _set_country("Peru")
        _set_country("Japan")
        def _click_row(title: str, box_title: str) -> None:
            shot = driver.snapshot(Scope.WINDOW, "chrome")
            row = next(
                el for el in shot.elements
                if el.title == title and el.role == "AXRow" and el.clickable
            )
            runtime._current = shot
            clicked = runtime.click(row.ref)
            assert "clicked" in clicked, clicked
            deadline = time.monotonic() + 4
            selected = False
            last = shot
            while time.monotonic() < deadline:
                last = driver.snapshot(Scope.WINDOW, "chrome")
                row = next(
                    (el for el in last.elements if el.title == title and el.role == "AXRow"),
                    None,
                )
                box = next((el for el in last.elements if el.title == box_title), None)
                selected = bool(row and row.selected) or bool(
                    box and box.value and title in str(box.value)
                )
                if selected:
                    return
                time.sleep(0.25)
            assert selected, observe.render_text(last)

        for name in ("Apple", "Banana", "Cherry", "Date"):
            _click_row(name, "Fruits")
        for name in ("Red", "Green", "Blue", "Gray"):
            _click_row(name, "Colors")
        listed = driver.snapshot(Scope.WINDOW, "chrome")
        runtime._current = listed
        seats = next(el for el in listed.elements if el.title == "Seats")
        runtime.set_value(seats.ref, "")
        deadline = time.monotonic() + 4
        cleared = None
        while time.monotonic() < deadline:
            listed = driver.snapshot(Scope.WINDOW, "chrome")
            seats = next(el for el in listed.elements if el.title == "Seats")
            cleared = seats.value
            if cleared in (None, ""):
                break
            time.sleep(0.25)
        assert cleared in (None, ""), cleared

        def _guest_status(rendered: str) -> str | None:
            marker = "gstat="
            for line in rendered.splitlines():
                at = line.find(marker)
                if at < 0:
                    continue
                rest = line[at + len(marker):]
                digits: list[str] = []
                for ch in rest:
                    if ch.isdigit() or ch == ".":
                        digits.append(ch)
                    else:
                        break
                return "".join(digits)
            return None

        def _refill_guests(value: str) -> None:
            shot = driver.snapshot(Scope.WINDOW, "chrome")
            guests = next(el for el in shot.elements if el.title == "Guests")
            runtime._current = shot
            runtime.set_value(guests.ref, "")
            deadline = time.monotonic() + 4
            emptied = None
            while time.monotonic() < deadline:
                shot = driver.snapshot(Scope.WINDOW, "chrome")
                guests = next(el for el in shot.elements if el.title == "Guests")
                emptied = guests.value
                if emptied in (None, ""):
                    break
                time.sleep(0.25)
            assert emptied in (None, ""), emptied
            shot = driver.snapshot(Scope.WINDOW, "chrome")
            guests = next(el for el in shot.elements if el.title == "Guests")
            runtime._current = shot
            result = runtime.set_value(guests.ref, value)
            assert result.startswith("set "), result
            deadline = time.monotonic() + 4
            shown = None
            last = shot
            while time.monotonic() < deadline:
                last = driver.snapshot(Scope.WINDOW, "chrome")
                shown = _guest_status(observe.render_text(last))
                if shown == value:
                    return
                time.sleep(0.25)
            assert shown == value, observe.render_text(last)

        for number in ("0", "3", "7"):
            _refill_guests(number)
        listed = driver.snapshot(Scope.WINDOW, "chrome")
        volume = next((el for el in listed.elements if el.title == "Volume"), None)
        if volume is not None and volume.role == "AXSlider":
            runtime._current = listed
            runtime.set_value(volume.ref, "55")
            listed = driver.snapshot(Scope.WINDOW, "chrome")
        toggle = next(el for el in listed.elements if el.title == "Italic toggle")
        runtime._current = listed
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


_MENU_APP = "cuamenuapp"

_GTK_MENU_APP = textwrap.dedent(
    """
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib
    GLib.set_prgname("cuamenuapp")
    win = Gtk.Window(title="cuamenuapp")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    bar = Gtk.MenuBar()
    menu = Gtk.Menu()
    file_item = Gtk.MenuItem.new_with_mnemonic("_File")
    file_item.set_submenu(menu)
    mark = Gtk.MenuItem.new_with_mnemonic("_Mark")
    probe = Gtk.Label(label="mark=0")
    mark.connect("activate", lambda *_a: probe.set_text("mark=1"))
    menu.append(mark)
    bar.append(file_item)
    search_menu = Gtk.Menu()
    find_item = Gtk.MenuItem.new_with_mnemonic("_Find")
    search_menu.append(find_item)
    search_item = Gtk.MenuItem.new_with_mnemonic("_Search")
    search_item.set_submenu(search_menu)
    bar.append(search_item)
    buf = Gtk.TextBuffer()
    buf.set_text("line one\\nline two\\nline three")
    lines = Gtk.Label(label="lines=3")
    def on_changed(buffer):
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        lines.set_text("lines=%d" % (text.count("\\n") + 1))
    buf.connect("changed", on_changed)
    view = Gtk.TextView(buffer=buf)
    view.get_accessible().set_name("Document")
    box.pack_start(bar, False, False, 0)
    box.pack_start(probe, False, False, 0)
    box.pack_start(lines, False, False, 0)
    box.pack_start(view, True, True, 0)
    win.add(box)
    win.set_default_size(420, 240)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    view.grab_focus()
    win.present()
    Gtk.main()
    """
)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _runtime_for(tmp_path, driver, *apps: str):
    from a11y_computer_use import safety, server

    store = safety.PermissionStore(tmp_path / "permissions.json")
    for app in apps:
        store.set_tier(app, safety.Tier.FULL)
    front = driver.frontmost_app()[0]
    if front and front not in apps:
        store.set_tier(front, safety.Tier.FULL)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


_PARA_PAGE = """<!doctype html><meta charset=utf-8><title>cuapara</title>
<style>
body{margin:8px;font:13px sans-serif}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
p,label,div,h1{margin:2px}
h1{font-size:14px}
</style>
<h1>Para variants</h1>
<div class=row>
<p><label>Bravo <input id=bravo></label></p>
<p>Read <a href="#x">the docs link</a> now.</p>
<p><button type=button>Para button</button></p>
<p><input aria-label="Bare para input"></p>
<p><input type=checkbox id=k> <label for=k>Para checkbox</label></p>
<div><button type=button>Div button</button></div>
<form><p><label>Name <input id=name></label></p></form>
<label>Form name <input id=formname></label>
</div>
<p>The <a href="#a">quick brown</a> fox <b>jumps</b> over the <em>lazy</em> dog.</p>
<div class=row>
<div id=ed contenteditable=true role=textbox aria-label="Editor A">Hello world</div>
<div contenteditable=true aria-label="Editor B"><p>First para</p><p>Second <b>bold</b> para</p></div>
<label><input type=checkbox aria-label="Verify you are human"></label>
<label><input type=checkbox aria-label="Accept terms"></label>
<label><input type=radio name=r aria-label="Option one"></label>
<label><input aria-label="Your answer"></label>
<label><input type=checkbox id=x2> Text label</label>
<label><input type=checkbox aria-label="Icon checkbox"><span aria-hidden=true>✓</span></label>
<input type=checkbox aria-label="Bare checkbox">
</div>
"""

_PARA_CONTROLS = (
    "Bravo",
    "the docs link",
    "Para button",
    "Bare para input",
    "Para checkbox",
    "Div button",
    "Name",
    "Form name",
    "Verify you are human",
    "Accept terms",
    "Option one",
    "Your answer",
    "Text label",
    "Icon checkbox",
    "Bare checkbox",
)


def _firefox_binary() -> str | None:
    import shutil

    for name in ("firefox", "firefox-esr"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _launch_para_browser(tmp_path, kind: str) -> tuple[subprocess.Popen, str]:
    page = tmp_path / "cuapara.html"
    page.write_text(_PARA_PAGE)
    url = page.resolve().as_uri()
    profile = tmp_path / f"{kind}-profile"
    profile.mkdir()
    if kind == "chrome":
        binary = _chrome_binary()
        if binary is None:
            pytest.skip("no Chrome/Chromium binary for the paragraph AT-SPI test")
        proc = subprocess.Popen(
            [
                binary, "--force-renderer-accessibility", "--no-sandbox", "--disable-gpu",
                "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
                "--disable-component-update", f"--user-data-dir={profile}",
                "--window-size=1100,800", url,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return proc, "chrome"
    binary = _firefox_binary()
    if binary is None:
        pytest.skip("no Firefox binary for the paragraph AT-SPI test")
    (profile / "user.js").write_text(
        'user_pref("accessibility.force_disabled", -1);\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
        'user_pref("browser.aboutwelcome.enabled", false);\n'
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);\n'
    )
    env = os.environ.copy()
    env["MOZ_ENABLE_ACCESSIBILITY"] = "1"
    proc = subprocess.Popen(
        [binary, "-no-remote", "-profile", str(profile), url],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc, "firefox"


def _wait_para_snapshot(driver, app: str):
    from a11y_computer_use import observe

    deadline = time.monotonic() + 30
    last = ""
    while time.monotonic() < deadline:
        try:
            shot = driver.snapshot(Scope.WINDOW, app)
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            shot = None
        else:
            last = observe.render_text(shot)
            if "Div button" in last and "the docs link" in last and "Verify you are human" in last:
                return shot
        time.sleep(0.5)
    raise AssertionError(f"{app} did not expose the paragraph page through AT-SPI\n{last}")


def _para_page_lists_controls(driver, runtime, app: str) -> None:
    """Live AT-SPI page. Full and interactive snapshots list the controls, and find matches."""
    from a11y_computer_use import observe

    snap = _wait_para_snapshot(driver, app)
    for mode in ("full", "interactive"):
        rendered = observe.render_text(snap, mode=mode)
        missing = [name for name in _PARA_CONTROLS if name not in rendered]
        assert not missing, f"{app} {mode} missing {missing}\n{rendered}"
    sentence = "The quick brown fox jumps over the lazy dog."
    assert any(sentence in str(el.value or "") for el in snap.elements), observe.render_text(snap)
    runtime._current = snap
    for text in ("Bravo", "Para button", "quick brown fox", "The quick", "Verify you are human", "Accept terms"):
        found = runtime.find(app, text=text)
        assert "no elements match" not in found, found
    boxes = runtime.find(app, role="checkbox")
    for name in ("Verify you are human", "Accept terms", "Para checkbox", "Bare checkbox"):
        assert name in boxes, boxes
    fields = runtime.find(app, role="textfield")
    assert "Bare para input" in fields and "Your answer" in fields, fields
    human = next(
        el for el in snap.elements
        if el.role == "AXCheckBox" and el.title == "Verify you are human"
    )
    runtime._current = snap
    clicked = runtime.click(human.ref)
    assert "clicked" in clicked, clicked
    deadline = time.monotonic() + 4
    checked = None
    last = snap
    while time.monotonic() < deadline:
        last = driver.snapshot(Scope.WINDOW, app)
        human = next(
            (el for el in last.elements if el.role == "AXCheckBox" and el.title == "Verify you are human"),
            None,
        )
        checked = human.checked if human is not None else None
        if checked is True:
            return
        time.sleep(0.25)
    raise AssertionError(observe.render_text(last))


def _para_page_edits_contenteditable(driver, runtime, app: str) -> None:
    """Live contenteditable. set_value lands, and type does not report a false mismatch."""
    from a11y_computer_use import observe

    driver.activate_app(app)
    for value in ("Set 0", "Set 1", "Set 2"):
        shot = _wait_para_snapshot(driver, app)
        editor = next(el for el in shot.elements if el.title == "Editor A" and el.editable)
        runtime._current = shot
        result = runtime.set_value(editor.ref, value)
        assert result.startswith("set "), result
        deadline = time.monotonic() + 4
        shown = None
        last = shot
        while time.monotonic() < deadline:
            last = driver.snapshot(Scope.WINDOW, app)
            editor = next(el for el in last.elements if el.title == "Editor A")
            shown = "" if editor.value is None else str(editor.value).replace("\u00a0", " ").strip()
            if shown == value:
                break
            time.sleep(0.25)
        assert shown == value, f"{app} editor read {shown!r}\n{observe.render_text(last)}"
    shot = driver.snapshot(Scope.WINDOW, app)
    editor = next(el for el in shot.elements if el.title == "Editor A")
    runtime._current = shot
    runtime.click(editor.ref)
    runtime.key("ctrl+end")
    typed = runtime.type_text("  two spaces end ")
    assert "typed" in typed, typed
    shot = driver.snapshot(Scope.WINDOW, app)
    editor = next(el for el in shot.elements if el.title == "Editor A")
    shown = "" if editor.value is None else str(editor.value).replace("\u00a0", " ")
    assert "two spaces end" in shown, shown
    other = next(el for el in shot.elements if el.title == "Editor B")
    runtime._current = shot
    runtime.click(other.ref)
    typed = runtime.type_text("ZZ")
    assert "typed" in typed, typed
    shot = driver.snapshot(Scope.WINDOW, app)
    blob = " ".join(
        f"{el.title} {el.value or ''}" for el in shot.elements if el.title in {"Editor B", "Editor A"} or "ZZ" in f"{el.title} {el.value or ''}"
    )
    assert "ZZ" in blob, blob


def _browser_ids(driver, *needles: str) -> tuple[str, ...]:
    """Permission-keying comms for a browser, plus whatever is in front.

    A Firefox tarball's window owner is ``firefox-bin`` while the AT-SPI name
    still matches ``firefox``. The grant has to be the comm ``find`` resolves.
    """
    names = {needle for needle in needles if needle}
    front = driver.frontmost_app()[0]
    if front:
        names.add(front)
    try:
        from a11y_computer_use.drivers import _linux_system

        folded = tuple(needle.lower() for needle in needles if needle)
        for app in _linux_system.running_apps():
            comm = str(app.get("bundle_id") or "")
            if comm and any(needle in comm.lower() for needle in folded):
                names.add(comm)
    except Exception:
        pass
    return tuple(names)


def test_linux_chrome_paragraph_label_and_contenteditable(tmp_path) -> None:
    """Live Chrome AT-SPI: controls in p and in an empty label, the sentence, set_value, and type.

    Skips when no Chrome binary is on PATH. The Linux CI image has one.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc, app = _launch_para_browser(tmp_path, "chrome")
    try:
        _wait_para_snapshot(driver, app)
        app = driver.activate_app(app)
        runtime = _runtime_for(tmp_path, driver, *_browser_ids(driver, app))
        _para_page_lists_controls(driver, runtime, app)
        _para_page_edits_contenteditable(driver, runtime, app)
    finally:
        _stop(proc)


def test_linux_firefox_paragraph_and_label_checkbox(tmp_path) -> None:
    """Live Firefox AT-SPI: the same local page, including the empty-label checkbox click.

    Skips when no Firefox binary is on PATH.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    proc, app = _launch_para_browser(tmp_path, "firefox")
    try:
        _wait_para_snapshot(driver, app)
        app = driver.activate_app(app)
        runtime = _runtime_for(tmp_path, driver, *_browser_ids(driver, app, "firefox", "firefox-bin"))
        _para_page_lists_controls(driver, runtime, app)
    finally:
        _stop(proc)


def test_linux_key_reaches_an_open_gtk_menu(tmp_path) -> None:
    """Down and Return stay in the open File menu. The document is not edited."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    script = tmp_path / "cuamenuapp.py"
    script.write_text(_GTK_MENU_APP)
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 15
        snap = None
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, _MENU_APP)
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                shot = None
            else:
                titles = {el.title for el in shot.elements}
                if (
                    "File" in titles and "Search" in titles and "Document" in titles
                    and "mark=0" in titles and "lines=3" in titles
                ):
                    snap = shot
                    break
            time.sleep(0.4)
        assert snap is not None, "the GTK menu window never appeared"
        runtime = _runtime_for(tmp_path, driver, _MENU_APP)
        runtime._current = snap
        file_item = next(el for el in snap.elements if el.title == "File" and el.clickable)
        runtime.click(file_item.ref)
        opened = False
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = driver.menu_state(_MENU_APP)
            if state.get("open") and "File" in (state.get("path") or []):
                opened = True
                break
            time.sleep(0.2)
        assert opened, driver.menu_state(_MENU_APP)
        runtime.key("down")
        runtime.key("return")
        deadline = time.monotonic() + 4
        marked = False
        while time.monotonic() < deadline:
            shot = driver.snapshot(Scope.WINDOW, _MENU_APP)
            titles = {el.title for el in shot.elements}
            marked = "mark=1" in titles and "lines=3" in titles and "lines=4" not in titles
            if marked:
                break
            time.sleep(0.2)
        assert marked, [(el.role, el.title, el.value) for el in shot.elements]
        shot = driver.snapshot(Scope.WINDOW, _MENU_APP)
        runtime._current = shot
        file_item = next(el for el in shot.elements if el.title == "File" and el.clickable)
        runtime.click(file_item.ref)
        opened = False
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            state = driver.menu_state(_MENU_APP)
            if state.get("open") and "File" in (state.get("path") or []):
                opened = True
                break
            time.sleep(0.2)
        assert opened, driver.menu_state(_MENU_APP)
        runtime.key("alt+s")
        switched = False
        deadline = time.monotonic() + 4
        state = {"open": False, "path": []}
        while time.monotonic() < deadline:
            state = driver.menu_state(_MENU_APP)
            path = state.get("path") or []
            if state.get("open") and path and path[0] == "Search":
                switched = True
                break
            time.sleep(0.2)
        assert switched, state
    finally:
        _stop(proc)


def test_linux_launch_fails_fast_and_an_absolute_path_window_is_reported(tmp_path) -> None:
    """`true` exits before a window. An absolute-path GTK script's window is the result."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    runtime = _runtime_for(tmp_path, driver, "true", "false")
    runtime.APP_LAUNCH_WAIT_S = 30
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "true")
    assert time.monotonic() - started < 5
    assert exc.value.detail.get("reason") == "process_exited"
    assert exc.value.detail.get("exit_code") == 0
    assert "status 0" in exc.value.message
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "false")
    assert time.monotonic() - started < 5
    assert exc.value.detail.get("exit_code") == 1

    script = tmp_path / "cualaunch"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk, Gtk, GLib
            GLib.set_prgname("cualaunch")
            Gdk.set_program_class("cualaunch")
            win = Gtk.Window(title="cualaunchwin")
            win.set_default_size(200, 80)
            win.connect("destroy", Gtk.main_quit)
            win.show_all()
            win.present()
            Gtk.main()
            """
        )
    )
    script.chmod(0o755)
    from a11y_computer_use import safety

    runtime.store.set_tier(str(script), safety.Tier.CLICK)
    runtime.store.set_tier("cualaunch", safety.Tier.CLICK)
    result = runtime.app("launch", str(script))
    assert "first window:" in result and "cualaunchwin" in result, result
    runtime.store.set_tier("python3", safety.Tier.CLICK)
    focused = runtime.app("focus", str(script))
    assert focused.startswith("focused "), focused
    assert "app_not_found" not in focused
    child = None
    for row in driver.windows():
        if row.get("title") == "cualaunchwin" and row.get("pid"):
            child = int(row["pid"])
            break
    if child:
        os.kill(child, 15)


def test_linux_second_launch_reports_the_window_the_running_instance_opened(tmp_path) -> None:
    """A second launch exits 0 and the running window's new title is the result.

    The second process writes a bump file and exits. The first process changes
    its title. ``activate: false`` still names that title. Focus of the same
    absolute path resolves the running app.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    lock = tmp_path / "lock"
    bump = tmp_path / "bump"
    script = tmp_path / "cuahandoff"
    script.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import os
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk, GLib, Gtk
            GLib.set_prgname("cuahandoff")
            Gdk.set_program_class("cuahandoff")
            LOCK = __LOCK__
            BUMP = __BUMP__
            if os.path.exists(LOCK):
                open(BUMP, "w", encoding="utf-8").write("1")
                raise SystemExit(0)
            open(LOCK, "w", encoding="utf-8").write("1")
            win = Gtk.Window(title="handoff=1")
            win.set_default_size(220, 80)
            def poll():
                if os.path.exists(BUMP):
                    win.set_title("handoff=2")
                    return False
                return True
            GLib.timeout_add(100, poll)
            win.connect("destroy", Gtk.main_quit)
            win.show_all()
            win.present()
            Gtk.main()
            """
        ).replace("__LOCK__", repr(str(lock))).replace("__BUMP__", repr(str(bump)))
    )
    script.chmod(0o755)
    runtime = _runtime_for(tmp_path, driver, "cuahandoff", "python3", str(script))
    runtime.APP_LAUNCH_WAIT_S = 20
    first = runtime.app("launch", str(script))
    assert "first window:" in first and "handoff=1" in first, first
    second = runtime.app("launch", str(script), activate=False)
    assert "first window:" in second and "handoff=2" in second, second
    focused = runtime.app("focus", str(script))
    assert focused.startswith("focused "), focused
    assert "app_not_found" not in focused
    for row in driver.windows():
        title = str(row.get("title") or "")
        if title.startswith("handoff=") and row.get("pid"):
            try:
                os.kill(int(row["pid"]), 15)
            except OSError:
                pass


def test_linux_quit_reports_an_unsaved_dialog_without_clicking_discard(tmp_path) -> None:
    """ctrl+q raises a question dialog. Quit names it and does not press a button.

    After quit the front window is the dialog, so a window snapshot lists
    Cancel, Don't Save, and Save and not the parent label. A button press is
    recorded in a file by the dialog's response handler. The file staying
    ``none`` is the proof nothing was clicked.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    choice = tmp_path / "choice.txt"
    choice.write_text("none")
    script = tmp_path / "cuaquit.py"
    script.write_text(textwrap.dedent(
        """
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk, GLib
        GLib.set_prgname("cuaquitapp")
        CHOICE = __CHOICE__
        win = Gtk.Window(title="cuaquitapp")
        label = Gtk.Label(label="choice=none")
        def on_key(_win, event):
            if event.keyval == Gdk.KEY_q and event.state & Gdk.ModifierType.CONTROL_MASK:
                dialog = Gtk.MessageDialog(parent=win, modal=True, text="Save changes?")
                dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
                dialog.add_button("Don't Save", Gtk.ResponseType.NO)
                dialog.add_button("Save", Gtk.ResponseType.YES)
                def responded(dlg, response):
                    label.set_text("choice=%s" % int(response))
                    open(CHOICE, "w", encoding="utf-8").write("clicked %s" % int(response))
                    dlg.destroy()
                dialog.connect("response", responded)
                dialog.show_all()
                return True
            return False
        win.connect("key-press-event", on_key)
        win.add(label)
        win.set_default_size(280, 120)
        win.connect("destroy", Gtk.main_quit)
        win.show_all()
        win.present()
        Gtk.main()
        """
    ).replace("__CHOICE__", repr(str(choice))))
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        deadline = time.monotonic() + 15
        seen = False
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, "cuaquitapp")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                if any(el.title == "choice=none" for el in shot.elements):
                    seen = True
                    break
            time.sleep(0.4)
        assert seen, "the quit dialog app never appeared"
        runtime = _runtime_for(tmp_path, driver, "cuaquitapp")
        runtime.QUIT_SETTLE_S = 1.2
        result = runtime.app("quit", "cuaquitapp")
        assert "showing a dialog" in result, result
        assert "unsaved changes" in result
        assert proc.poll() is None
        shot = driver.snapshot(Scope.WINDOW, "cuaquitapp")
        titles = {el.title for el in shot.elements}
        assert {"Cancel", "Don't Save", "Save"} <= titles, [(el.title, el.value) for el in shot.elements]
        assert choice.read_text() == "none"
    finally:
        _stop(proc)


def test_linux_caps_lock_does_not_invert_keystroke_typing(tmp_path) -> None:
    """XTEST typing into a widget with no EditableText keeps the requested case."""
    import shutil

    from a11y_computer_use.drivers import _linux_input
    from a11y_computer_use.drivers.linux import LinuxDriver

    if shutil.which("xdotool") is None and not os.environ.get("DISPLAY"):
        pytest.skip("no DISPLAY for the Caps Lock keystroke test")
    driver = LinuxDriver()
    _require_bus(driver)
    script = tmp_path / "cuacaps.py"
    script.write_text(textwrap.dedent(
        """
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, GLib
        GLib.set_prgname("cuacapsapp")
        win = Gtk.Window(title="cuacapsapp")
        label = Gtk.Label(label="typed=")
        def on_key(_win, event):
            if event.string:
                label.set_text(label.get_text() + event.string)
            return True
        win.connect("key-press-event", on_key)
        win.add(label)
        win.set_default_size(240, 80)
        win.connect("destroy", Gtk.main_quit)
        win.show_all()
        win.present()
        win.grab_focus()
        Gtk.main()
        """
    ))
    proc = subprocess.Popen([sys.executable, str(script)])
    turned_on = False
    try:
        deadline = time.monotonic() + 15
        seen = False
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, "cuacapsapp")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                if any(el.title == "typed=" for el in shot.elements):
                    seen = True
                    break
            time.sleep(0.4)
        assert seen, "the caps-lock window never appeared"
        caps, _num = _linux_input._lock_mask()
        if not caps:
            _linux_input.press_chord("capslock")
            turned_on = True
            time.sleep(0.1)
        runtime = _runtime_for(tmp_path, driver, "cuacapsapp")
        driver.activate_app("cuacapsapp")
        time.sleep(0.3)
        runtime.type_text("Ab")
        runtime.key("b")
        runtime.key("shift+c")
        deadline = time.monotonic() + 4
        shown = ""
        while time.monotonic() < deadline:
            shot = driver.snapshot(Scope.WINDOW, "cuacapsapp")
            titles = {el.title for el in shot.elements}
            if "typed=AbbC" in titles:
                shown = "typed=AbbC"
                break
            shown = " ".join(sorted(titles))
            time.sleep(0.2)
        assert shown == "typed=AbbC", [(el.role, el.title, el.value) for el in shot.elements]
    finally:
        if turned_on:
            try:
                _linux_input.press_chord("capslock")
            except Exception:
                pass
        _stop(proc)
