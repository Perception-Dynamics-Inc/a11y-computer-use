"""GTK3 fixture for the M1 live agent tests.

A multi-line note, an in-app save path (Linux ``file_dialog`` is unsupported),
a format combo, a confirmation label, and File > Save As. Save As does not
open a GTK file chooser. It focuses the path field and asks for a path there.
The Save button writes that path and updates the confirmation label.

Launch: ``python tests/agent_fixtures/gtk_app.py``
The AT-SPI application name is ``cuagentfix`` (``GLib.set_prgname``).
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402


APP_NAME = "cuagentfix"


def _accessible_name(widget: Gtk.Widget, name: str) -> None:
    """The name AT-SPI reports. Set after realize; GTK drops a pre-realize name."""

    def apply(*_args: object) -> None:
        accessible = widget.get_accessible()
        if accessible is not None:
            accessible.set_name(name)

    widget.connect("realize", apply)
    apply()


def _build() -> Gtk.Window:
    GLib.set_prgname(APP_NAME)
    GLib.set_application_name(APP_NAME)

    window = Gtk.Window(title=APP_NAME)
    window.set_default_size(560, 420)
    window.set_position(Gtk.WindowPosition.CENTER)

    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    window.add(root)

    menubar = Gtk.MenuBar()
    file_item = Gtk.MenuItem(label="File")
    file_menu = Gtk.Menu()
    file_item.set_submenu(file_menu)
    save_as = Gtk.MenuItem(label="Save As")
    file_menu.append(save_as)
    file_menu.append(Gtk.SeparatorMenuItem())
    quit_item = Gtk.MenuItem(label="Quit")
    file_menu.append(quit_item)
    menubar.append(file_item)
    root.pack_start(menubar, False, False, 0)

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    body.set_border_width(8)
    root.pack_start(body, True, True, 0)

    notes_label = Gtk.Label(label="Notes", xalign=0)
    body.pack_start(notes_label, False, False, 0)
    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_min_content_height(160)
    notes = Gtk.TextView()
    notes.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    notes.get_buffer().set_text("")
    scrolled.add(notes)
    body.pack_start(scrolled, True, True, 0)
    _accessible_name(notes, "Notes")

    path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    path_caption = Gtk.Label(label="Save path", xalign=0)
    path_entry = Gtk.Entry()
    path_entry.set_placeholder_text("/tmp/note.txt")
    path_entry.set_hexpand(True)
    path_row.pack_start(path_caption, False, False, 0)
    path_row.pack_start(path_entry, True, True, 0)
    body.pack_start(path_row, False, False, 0)
    _accessible_name(path_entry, "Save path")

    format_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    format_caption = Gtk.Label(label="Format", xalign=0)
    combo = Gtk.ComboBoxText()
    combo.append_text("markdown")
    combo.append_text("plain")
    combo.set_active(0)  # markdown, so a script must change it to plain
    format_row.pack_start(format_caption, False, False, 0)
    format_row.pack_start(combo, False, False, 0)
    body.pack_start(format_row, False, False, 0)
    _accessible_name(combo, "Format")

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    save = Gtk.Button(label="Save")
    ping = Gtk.Button(label="Ping")
    buttons.pack_start(save, False, False, 0)
    buttons.pack_start(ping, False, False, 0)
    body.pack_start(buttons, False, False, 0)

    # The confirmation is a label whose accessible name stays "Status" and
    # whose text is the value a snapshot reads back. Idle until a real save.
    status = Gtk.Label(label="idle", xalign=0)
    status.set_selectable(True)
    body.pack_start(status, False, False, 0)
    _accessible_name(status, "Status")

    def set_status(text: str) -> None:
        status.set_text(text)
        # Keep the accessible name stable so a value condition can find "Status".
        accessible = status.get_accessible()
        if accessible is not None:
            accessible.set_name("Status")
        window.set_title(f"{APP_NAME} {text}" if text != "idle" else APP_NAME)

    def on_save_as(*_args: object) -> None:
        # In-app path, not a file chooser. file_dialog is unsupported on Linux.
        path_entry.grab_focus()
        set_status("choose-path")

    def on_save(*_args: object) -> None:
        raw = path_entry.get_text().strip()
        if not raw:
            set_status("need-path")
            return
        buf = notes.get_buffer()
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True)
        chosen = combo.get_active_text() or ""
        if chosen not in {"plain", "markdown"}:
            set_status("need-format")
            return
        try:
            Path(raw).write_text(text)
        except OSError as exc:
            set_status(f"error {exc.__class__.__name__}")
            return
        set_status(f"saved {chosen}")

    def on_ping(*_args: object) -> None:
        # Deliberately no tree change. Repeated presses are how a script
        # forces the stuck / strategy-change path.
        return None

    def on_quit(*_args: object) -> None:
        Gtk.main_quit()

    save_as.connect("activate", on_save_as)
    save.connect("clicked", on_save)
    ping.connect("clicked", on_ping)
    quit_item.connect("activate", on_quit)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    window.present()
    # Ping is the stuck control: it changes nothing, including when a repeated
    # no-op is retried as a coordinate click or as Return. Keeping it focused
    # means that keyboard fallback activates Ping instead of typing into Notes.
    ping.grab_focus()
    _accessible_name(notes, "Notes")
    _accessible_name(path_entry, "Save path")
    _accessible_name(combo, "Format")
    _accessible_name(status, "Status")
    return window


def main() -> None:
    _build()

    def _stop(*_args: object) -> None:
        Gtk.main_quit()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        Gtk.main()
    except KeyboardInterrupt:
        Gtk.main_quit()


if __name__ == "__main__":
    sys.exit(main() or 0)
