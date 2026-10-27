"""#1235 live: a ref from a fresh snapshot after a GTK 3 stack page switch works.

A ``Gtk.Stack`` swaps its page in place, so the new page's controls take the
numbers the old page used. The ref the agent already held for AlphaBtn stays
refused (#1021); the fresh snapshot must name BetaBtn with a ref that clicks.
Skipped without an X display and GTK 3.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError  # noqa: E402
from tests.test_linux_live import _runtime_for, _stop_group  # noqa: E402

_FIXTURE = textwrap.dedent(
    """
    import sys
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    log = open(sys.argv[1], "a")
    win = Gtk.Window(title="Stack1235")
    win.move(0, 0)
    header = Gtk.HeaderBar()
    header.set_show_close_button(True)
    win.set_titlebar(header)
    stack = Gtk.Stack()
    switcher = Gtk.StackSwitcher()
    switcher.set_stack(stack)
    header.set_custom_title(switcher)
    alpha = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    alpha_btn = Gtk.Button(label="AlphaBtn")
    alpha_btn.connect("clicked", lambda *_: (log.write("AlphaBtn\\n"), log.flush()))
    alpha.pack_start(alpha_btn, False, False, 0)
    beta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    beta_btn = Gtk.Button(label="BetaBtn")
    def beta_clicked(button):
        log.write("BetaBtn\\n")
        log.flush()
        button.set_label("BetaDone")
    beta_btn.connect("clicked", beta_clicked)
    beta.pack_start(beta_btn, False, False, 0)
    stack.add_titled(alpha, "alpha", "Alpha")
    stack.add_titled(beta, "beta", "Beta")
    win.add(stack)
    win.set_default_size(520, 320)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
    """
)


def _gtk3_ready() -> None:
    if not os.environ.get("DISPLAY"):
        pytest.skip("needs an X display")
    probe = subprocess.run(
        ["/usr/bin/python3", "-c",
         "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk"],
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("no GTK 3 for /usr/bin/python3")


def _ref_of(text: str, label: str) -> str:
    for line in text.splitlines():
        if label in line:
            found = re.search(r"\be\d+\b", line)
            if found:
                return found.group(0)
    raise AssertionError(f"{label} is not in the snapshot:\n{text}")


def test_fresh_ref_after_a_stack_page_switch_clicks(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    _gtk3_ready()
    script = tmp_path / "stack1235.py"
    script.write_text(_FIXTURE)
    log = tmp_path / "clicks.log"
    log.write_text("")
    proc = subprocess.Popen(["/usr/bin/python3", str(script), str(log)], start_new_session=True)
    try:
        runtime = _runtime_for(tmp_path, LinuxDriver(), "python3")
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                text = runtime.desktop_snapshot("python3", mode="full")
            except ComputerUseError:
                continue
            if "AlphaBtn" in text and "Beta" in text:
                break
        alpha_ref = _ref_of(text, "AlphaBtn")
        runtime.click(ref=_ref_of(text, "Beta"))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.5)
            text = runtime.desktop_snapshot("python3", mode="full")
            if "BetaBtn" in text:
                break
        beta_ref = _ref_of(text, "BetaBtn")
        assert beta_ref != alpha_ref, text
        result = runtime.click(ref=beta_ref)
        time.sleep(0.5)
        # The result names the ref the agent used (not the number the backend
        # gave the live node) and judges the click against that control: the
        # label change is seen, so the click is confirmed, not a no-op.
        assert f"clicked {beta_ref} " in str(result), str(result)
        assert alpha_ref not in str(result).split("(")[0], str(result)
        assert result.outcome == "confirmed", (result.outcome, result.evidence)
        assert log.read_text().split() == ["BetaBtn"]
        with pytest.raises(ComputerUseError) as caught:
            runtime.click(ref=alpha_ref)
        assert caught.value.detail["reason"] == "target_changed"
        assert log.read_text().split() == ["BetaBtn"]
    finally:
        _stop_group(proc)
