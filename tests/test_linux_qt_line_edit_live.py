"""#1418 live: set_value and type reach a Qt 6 QLineEdit's user-edit path.

The fixture's label follows ``textEdited`` only, like QColorDialog's "HTML:"
field. An EditableText write changes the text without that signal; the fix
types real keys, so the label and the colour dialog both take the value.
Skipped without an X display, an AT-SPI bus, and PyQt6.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError  # noqa: E402
from tests.test_linux_gtk_selection_click_live import _ref_of  # noqa: E402
from tests.test_linux_live import _require_bus, _runtime_for, _stop  # noqa: E402
from tests.test_linux_qt_check_item_live import _qt_python  # noqa: E402

_FIXTURE = textwrap.dedent(
    """
    import os
    import sys
    os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import (QApplication, QColorDialog, QFormLayout, QLabel,
                                 QLineEdit, QVBoxLayout, QWidget)

    app = QApplication(sys.argv)
    app.setApplicationName("cuqtedit")
    win = QWidget()
    win.setWindowTitle("cuqtedit")
    form = QFormLayout()
    ed = QLineEdit("#ff0000")
    form.addRow("Hex colour:", ed)
    lab = QLabel("applied: #ff0000")
    form.addRow(lab)
    ed.textEdited.connect(lambda t: (lab.setText("applied: " + t),
                                     print("EDITED " + t, flush=True)))
    dlg = QColorDialog(QColor("red"))
    dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
    dlg.setOption(QColorDialog.ColorDialogOption.NoButtons)
    dlg.currentColorChanged.connect(lambda c: print("COLOUR " + c.name(), flush=True))
    lay = QVBoxLayout(win)
    lay.addLayout(form)
    lay.addWidget(dlg)
    win.move(0, 0)
    win.show()
    sys.exit(app.exec())
    """
)


def _wait_for(log_path, mark, needle, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = log_path.read_text()[mark:]
        if needle in new:
            return new
        time.sleep(0.05)
    return log_path.read_text()[mark:]


def test_set_value_and_type_fire_qt_text_edited(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    python = _qt_python()
    script = tmp_path / "cuqtedit.py"
    script.write_text(_FIXTURE)
    log_path = tmp_path / "edit.log"
    log = open(log_path, "w", encoding="utf-8")
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "xcb"
    proc = subprocess.Popen([python, str(script)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        runtime = _runtime_for(tmp_path, driver, os.path.basename(python),
                               "python", "python3", "cuqtedit")
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                text = runtime.desktop_snapshot("cuqtedit", mode="full")
            except ComputerUseError:
                continue
            if '"Hex colour:"' in text and '"HTML:"' in text:
                break
        assert '"Hex colour:"' in text and '"HTML:"' in text, text

        mark = len(log_path.read_text())
        runtime.set_value(ref=_ref_of(text, "Hex colour:"), value="#00ff00")
        new = _wait_for(log_path, mark, "EDITED #00ff00")
        assert "EDITED #00ff00" in new, new

        text = runtime.desktop_snapshot("cuqtedit", mode="full")
        assert "applied: #00ff00" in text, text
        mark = len(log_path.read_text())
        runtime.type_text(text="1", ref=_ref_of(text, "Hex colour:"))
        new = _wait_for(log_path, mark, "EDITED #00ff001")
        assert "EDITED #00ff001" in new, new

        text = runtime.desktop_snapshot("cuqtedit", mode="full")
        mark = len(log_path.read_text())
        runtime.set_value(ref=_ref_of(text, "HTML:"), value="#00ff00")
        new = _wait_for(log_path, mark, "COLOUR #00ff00")
        assert "COLOUR #00ff00" in new, new
    finally:
        _stop(proc)
        log.close()
