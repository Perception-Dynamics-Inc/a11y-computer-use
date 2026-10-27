"""#1230 live: links inside a GTK3 label are listed and a ref click follows them.

Skipped unless there is an X display with AT-SPI, and PyGObject with GTK3.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402
from tests.test_linux_live import _runtime_for, _stop_group  # noqa: E402

_APP = r'''
import gi, sys
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
log = open(sys.argv[1], "a")
def record(text):
    log.write(text + "\n"); log.flush(); return True
w = Gtk.Window(title="Link Lab"); w.move(0, 0)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); w.add(box)
one = Gtk.Label()
one.set_markup('Read the <a href="lab://alpha">alpha guide</a> or the <a href="lab://beta">beta notes</a> today.')
two = Gtk.Label(); two.set_line_wrap(True); two.set_max_width_chars(40)
two.set_markup('A wrapped paragraph that is long enough to wrap and then has a '
               '<a href="lab://gamma">gamma link that wraps across lines</a> at the end.')
for label in (one, two):
    label.connect("activate-link", lambda widget, uri: record(uri)); box.add(label)
button = Gtk.LinkButton.new_with_label("lab://delta", "Delta button link")
button.connect("activate-link", lambda widget: record(widget.get_uri())); box.add(button)
w.connect("destroy", Gtk.main_quit); w.show_all(); Gtk.main()
'''


@pytest.fixture
def lab(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")
    python = shutil.which("python3")
    try:
        subprocess.run([python, "-c", "import gi; gi.require_version('Gtk','3.0')"],
                       check=True, capture_output=True, timeout=20)
    except Exception:
        pytest.skip("no PyGObject GTK3")
    script, log = tmp_path / "links.py", tmp_path / "activated.log"
    script.write_text(_APP)
    log.write_text("")
    proc = subprocess.Popen([python, str(script), str(log)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    driver = LinuxDriver()
    app = os.path.basename(python)
    try:
        shot = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, app)
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                if any(e.title == "Delta button link" for e in shot.elements):
                    break
            time.sleep(0.5)
        else:
            pytest.skip("the GTK3 label never appeared")
        yield driver, _runtime_for(tmp_path, driver, app), app, log
    finally:
        _stop_group(proc)


def _links(driver, app):
    shot = driver.snapshot(Scope.WINDOW, app)
    return shot, {e.title: e for e in shot.elements if e.role == "AXLink"}


def test_label_links_are_listed_with_bounds_inside_their_label(lab) -> None:
    driver, _runtime, app, _log = lab
    shot, links = _links(driver, app)
    assert set(links) == {"alpha guide", "beta notes", "gamma link that wraps across lines"}
    labels = [e for e in shot.elements if e.role == "AXStaticText"]
    for link in links.values():
        assert link.clickable
        assert link.bounds is not None and link.bounds.width > 0 and link.bounds.height > 0
        assert any(
            label.bounds.x <= link.bounds.x and link.bounds.x + link.bounds.width <= label.bounds.x + label.bounds.width
            and label.bounds.y <= link.bounds.y and link.bounds.y + link.bounds.height <= label.bounds.y + label.bounds.height
            for label in labels
        )
    alpha, beta = links["alpha guide"], links["beta notes"]
    assert alpha.bounds.x + alpha.bounds.width <= beta.bounds.x  # two boxes, not one label box
    gamma = links["gamma link that wraps across lines"]
    assert gamma.bounds.height < 30  # the first drawn line only: it wraps onto a second


def test_find_by_role_link_and_ref_clicks_activate_each_link(lab) -> None:
    driver, runtime, app, log = lab
    found = runtime.find(app=app, role="link")
    for title in ("alpha guide", "beta notes", "gamma link that wraps across lines"):
        assert title in found
    for title, uri in (("beta notes", "lab://beta"),
                       ("gamma link that wraps across lines", "lab://gamma"),
                       ("alpha guide", "lab://alpha")):
        _shot, links = _links(driver, app)
        before = log.read_text().count(uri)
        runtime._current = driver.snapshot(Scope.WINDOW, app)
        link = next(e for e in runtime._current.elements if e.title == title)
        runtime.click(link.ref)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and log.read_text().count(uri) == before:
            time.sleep(0.1)
        assert log.read_text().count(uri) == before + 1, f"{title} did not open {uri}"
    assert "lab://delta" not in log.read_text()


def _outcome(result) -> str:
    return getattr(result, "outcome", None) or str(result)


def test_ref_click_on_an_unvisited_label_link_is_confirmed_by_visited(lab) -> None:
    """A fired inline link turns VISITED; a revisit has no evidence (#1230)."""
    driver, runtime, app, log = lab
    runtime._current = driver.snapshot(Scope.WINDOW, app)
    link = next(e for e in runtime._current.elements if e.title == "beta notes")
    result = runtime.click(link.ref)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "lab://beta" not in log.read_text():
        time.sleep(0.1)
    assert log.read_text().count("lab://beta") == 1
    assert getattr(result, "outcome", None) == "confirmed", _outcome(result)
    assert "inline link became visited" in (getattr(result, "evidence", "") or "")
    # Already visited: the second click fires but VISITED cannot change, so
    # the outcome must not claim confirmation. Wait out GTK's double-click
    # window so the second press is a separate click.
    time.sleep(1.0)
    runtime._current = driver.snapshot(Scope.WINDOW, app)
    link = next(e for e in runtime._current.elements if e.title == "beta notes")
    again = runtime.click(link.ref)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and log.read_text().count("lab://beta") < 2:
        time.sleep(0.1)
    assert log.read_text().count("lab://beta") == 2
    assert getattr(again, "outcome", None) != "confirmed", _outcome(again)
