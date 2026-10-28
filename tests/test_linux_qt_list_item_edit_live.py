"""#1288 live: editable Qt 6 QListWidget items edit through F2.

Qt 6 exposes QListWidget items as ``list item`` nodes with the TableCell
interface and no EditableText, the same shape as the table cells #1278 edits
through the delegate editor. ``set_value`` and ``type(ref)`` must rename an
editable item, and a read-only item must still be refused ``not_editable``
with its text unchanged. Skipped without an X display and PyQt6.
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
    from PyQt6.QtWidgets import QAbstractItemView, QApplication, QListWidget, QListWidgetItem

    app = QApplication(sys.argv)
    app.setApplicationName("cuqtlist")
    lw = QListWidget()
    lw.setAccessibleName("Rename")
    lw.setWindowTitle("cuqtlist")
    for text in ("Alpha", "Beta", "Gamma"):
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        lw.addItem(item)
    fixed = QListWidgetItem("Fixed")
    fixed.setFlags(fixed.flags() & ~Qt.ItemFlag.ItemIsEditable)
    lw.addItem(fixed)
    lw.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                       | QAbstractItemView.EditTrigger.EditKeyPressed)
    lw.itemChanged.connect(lambda i: print("CHANGED " + i.text(), flush=True))
    lw.resize(300, 200)
    lw.move(0, 0)
    lw.show()
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


def test_qt_list_items_edit_through_f2_and_read_only_is_refused(tmp_path):
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    python = _qt_python()
    script = tmp_path / "cuqtlist.py"
    script.write_text(_FIXTURE)
    log_path = tmp_path / "list.log"
    log = open(log_path, "w", encoding="utf-8")
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "xcb"
    proc = subprocess.Popen([python, str(script)], env=env, stdout=log, stderr=subprocess.STDOUT)
    app = os.path.basename(python)
    try:
        runtime = _runtime_for(tmp_path, driver, app, "python", "python3", "cuqtlist")
        deadline = time.monotonic() + 30
        text = ""
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                text = runtime.desktop_snapshot("cuqtlist", mode="full")
            except ComputerUseError:
                continue
            if '"Fixed"' in text:
                break
        assert '"Gamma"' in text, text

        runtime.set_value(ref=_ref_of(text, "Gamma"), value="Golf")
        text = runtime.desktop_snapshot("cuqtlist", mode="full")
        assert '"Golf"' in text and '"Gamma"' not in text, text

        runtime.type_text(ref=_ref_of(text, "Alpha"), text="X")
        text = runtime.desktop_snapshot("cuqtlist", mode="full")
        assert '"AlphaX"' in text, text

        with pytest.raises(ComputerUseError) as err:
            runtime.set_value(ref=_ref_of(text, "Fixed"), value="Nope")
        assert err.value.detail["reason"] == "not_editable"
        text = runtime.desktop_snapshot("cuqtlist", mode="full")
        assert '"Fixed"' in text and '"Nope"' not in text, text
        assert "CHANGED Golf" in log_path.read_text()
    finally:
        _stop(proc)
        log.close()
