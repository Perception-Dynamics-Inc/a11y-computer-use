"""#1287 live: set_value checks and unchecks a checkable Qt 6 list item.

Qt 6 exposes a QListWidget item with ItemIsUserCheckable (and a
QStandardItem with setCheckable) as a CHECKABLE ``list item`` with no Value
or EditableText. ``set_value("true"/"false")`` must change its check state,
come back confirmed from the live CHECKED state, and send nothing when the
item already reads the requested state. Skipped without an X display and PyQt6.
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

_FIXTURE = textwrap.dedent(
    """
    import os
    import sys
    os.environ["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QStandardItem, QStandardItemModel
    from PyQt6.QtWidgets import QApplication, QListView, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

    app = QApplication(sys.argv)
    app.setApplicationName("cuqtcheck")
    win = QWidget()
    win.setWindowTitle("cuqtcheck")
    lay = QVBoxLayout(win)
    lw = QListWidget()
    lw.setAccessibleName("Fruits")
    for text in ("Apple", "Banana", "Cherry"):
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        lw.addItem(item)
    lw.itemChanged.connect(lambda i: print("CHECK %s %s" % (i.text(), i.checkState().name), flush=True))
    model = QStandardItemModel()
    for text in ("Test", "Other"):
        item = QStandardItem(text)
        item.setCheckable(True)
        model.appendRow(item)
    model.itemChanged.connect(lambda i: print("CHECK %s %s" % (i.text(), i.checkState().name), flush=True))
    view = QListView()
    view.setAccessibleName("Model")
    view.setModel(model)
    lay.addWidget(lw)
    lay.addWidget(view)
    win.resize(320, 360)
    win.move(0, 0)
    win.show()
    sys.exit(app.exec())
    """
)


def _qt_python() -> str:
    for candidate in (os.environ.get("CU_QT_PYTHON"), sys.executable, "/usr/bin/python3"):
        if candidate and subprocess.run(
            [candidate, "-c", "import PyQt6.QtWidgets"], capture_output=True
        ).returncode == 0:
            return candidate
    pytest.skip("PyQt6 is not installed")


def test_set_value_toggles_checkable_qt_items_and_is_confirmed(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    python = _qt_python()
    script = tmp_path / "cuqtcheck.py"
    script.write_text(_FIXTURE)
    log_path = tmp_path / "check.log"
    log = open(log_path, "w", encoding="utf-8")
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "xcb"
    proc = subprocess.Popen([python, str(script)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        runtime = _runtime_for(tmp_path, driver, os.path.basename(python),
                               "python", "python3", "cuqtcheck")
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                text = runtime.desktop_snapshot("cuqtcheck", mode="full")
            except ComputerUseError:
                continue
            if '"Other"' in text:
                break
        assert '"Cherry"' in text and '"Test"' in text, text

        steps = (("Cherry", "true", "CHECK Cherry Checked"),
                 ("Cherry", "true", None),
                 ("Cherry", "false", "CHECK Cherry Unchecked"),
                 ("Test", "true", "CHECK Test Checked"))
        for label, value, expect in steps:
            text = runtime.desktop_snapshot("cuqtcheck", mode="full")
            mark = len(log_path.read_text())
            result = runtime.set_value(ref=_ref_of(text, label), value=value)
            time.sleep(0.2)
            new = log_path.read_text()[mark:]
            assert result.outcome == "confirmed", (label, value, result, result.evidence)
            if expect:
                assert expect in new, (label, value, new)
            else:
                assert "CHECK" not in new, (label, value, new)
        text = runtime.desktop_snapshot("cuqtcheck", mode="full")
        line = next(l for l in text.splitlines() if '"Test"' in l)
        assert "checked" in line and "unchecked" not in line, text
    finally:
        _stop(proc)
        log.close()
