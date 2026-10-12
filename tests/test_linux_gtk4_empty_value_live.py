"""Live GTK 4 empty fields on gnome-calculator and gnome-text-editor.

An empty text widget used to publish CurrentValue, an uninitialized
double (about 6.93e-310), because the node has Text and no Value. The
snapshot value of that field is absent. The AT-SPI bus has to be up.
Without a display the test skips. When the bus is up the binaries are
required, the same way the mousepad live test requires mousepad.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, Scope  # noqa: E402

_TEXT_ROLES = frozenset({"AXTextArea", "AXTextField"})


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
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
    home = getattr(proc, "gtk4_home", None)
    if isinstance(home, str):
        shutil.rmtree(home, ignore_errors=True)


def _stop_named(binary: str) -> None:
    """Close an already-open copy so the snapshot is the process this test starts."""
    subprocess.run(["pkill", "-f", binary], check=False)
    time.sleep(0.3)


def _launch(binary: str, args: tuple[str, ...] = ()) -> subprocess.Popen:
    """Start ``binary`` with a private data dir so a saved draft is not the document."""
    env = os.environ.copy()
    env["GTK_A11Y"] = "atspi"
    env["NO_AT_BRIDGE"] = "0"
    root = tempfile.mkdtemp(prefix="gtk4-empty-")
    for name in ("data", "config", "cache"):
        os.makedirs(os.path.join(root, name))
    env["XDG_DATA_HOME"] = os.path.join(root, "data")
    env["XDG_CONFIG_HOME"] = os.path.join(root, "config")
    env["XDG_CACHE_HOME"] = os.path.join(root, "cache")
    proc = subprocess.Popen([binary, *args], env=env, start_new_session=True)
    proc.gtk4_home = root  # type: ignore[attr-defined]
    return proc


def _family(pid: int) -> set[int]:
    """The launched pid and the children it has forked."""
    found = {pid}
    pending = [pid]
    while pending:
        current = pending.pop()
        path = f"/proc/{current}/task/{current}/children"
        try:
            text = open(path, encoding="ascii").read()
        except OSError:
            continue
        for part in text.split():
            if not part.isdigit():
                continue
            child = int(part)
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _window(driver, needle: str, pid: int | None = None, timeout_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        family = _family(pid) if pid is not None else set()
        for row in driver.windows() or []:
            blob = " ".join(
                str(row.get(key) or "") for key in ("app", "title", "wm_class")
            ).casefold()
            if needle not in blob or not row.get("on_screen", True) or not row.get("window_id"):
                continue
            if family and int(row.get("pid") or 0) not in family:
                continue
            return row
        time.sleep(0.2)
    return None


def _garbage(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"", "nan", "+nan", "-nan", "inf", "+inf", "-inf"}:
            return text not in {""}
        try:
            number = float(text)
        except ValueError:
            return "e-" in text and "310" in text
    else:
        number = float(value)
    if not math.isfinite(number):
        return True
    return number != 0.0 and abs(number) < sys.float_info.min


def _empty_text_values(driver, app: str):
    deadline = time.monotonic() + 15
    shot = None
    while time.monotonic() < deadline:
        shot = driver.snapshot(Scope.WINDOW, app)
        texts = [el for el in shot.elements if el.role in _TEXT_ROLES]
        if texts:
            return texts
        time.sleep(0.25)
    return [el for el in (shot.elements if shot is not None else []) if el.role in _TEXT_ROLES]


def _assert_empty(texts) -> None:
    assert texts, "no text field in the snapshot"
    for el in texts:
        assert not _garbage(el.value), (el.role, el.title, el.value)
        assert el.value in (None, ""), (el.role, el.title, el.value)


def test_gtk4_calculator_empty_display_has_no_garbage_value() -> None:
    """A freshly opened calculator display is empty, not a subnormal double."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    found = subprocess.run(["bash", "-lc", "command -v gnome-calculator"], capture_output=True, text=True)
    binary = found.stdout.strip()
    assert binary, "gnome-calculator is not installed"
    _stop_named(binary)
    proc = _launch(binary)
    try:
        row = _window(driver, "calculat", proc.pid)
        assert row is not None, driver.windows()
        driver.focus_window(int(row["window_id"]))
        _assert_empty(_empty_text_values(driver, "gnome-calculator"))
    finally:
        _stop_group(proc)


def test_gtk4_text_editor_empty_document_has_no_garbage_value() -> None:
    """A new text-editor document is empty, not a subnormal double."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    found = subprocess.run(["bash", "-lc", "command -v gnome-text-editor"], capture_output=True, text=True)
    binary = found.stdout.strip()
    assert binary, "gnome-text-editor is not installed"
    _stop_named(binary)
    proc = _launch(binary, ("--standalone",))
    try:
        row = _window(driver, "text-edit", proc.pid)
        assert row is not None, driver.windows()
        driver.focus_window(int(row["window_id"]))
        _assert_empty(_empty_text_values(driver, "gnome-text-editor"))
    finally:
        _stop_group(proc)
