"""Live Swing JTable. scroll_to_find must not type into the selected cell.

OpenJDK and the Java ATK Wrapper, not a synthetic tree. Skips without a
display, a JDK, or libatk-wrapper-java. When those are present and the bus
is up, a table that never appears is a failure. A click grant audits a
denial. A full grant sends no query. The selected cell stays ROW-005.
"""

from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

_JAVA = textwrap.dedent(
    """
    import java.nio.file.Files;
    import java.nio.file.Path;
    import java.awt.BorderLayout;
    import javax.swing.JFrame;
    import javax.swing.JLabel;
    import javax.swing.JScrollPane;
    import javax.swing.JTable;
    import javax.swing.ListSelectionModel;
    import javax.swing.SwingUtilities;
    import javax.swing.event.TableModelEvent;
    import javax.swing.event.TableModelListener;
    import javax.swing.table.DefaultTableModel;

    public class CuaSwingTable {
        public static void main(String[] args) throws Exception {
            if (args.length != 1) {
                System.err.println("usage: CuaSwingTable status.txt");
                System.exit(2);
            }
            Path status = Path.of(args[0]);
            Files.writeString(status, "idle\\n");
            SwingUtilities.invokeAndWait(() -> build(status));
        }

        private static void build(Path status) {
            String[] columns = {"id", "item"};
            Object[][] data = new Object[40][2];
            for (int i = 0; i < data.length; i++) {
                String label = String.format("ROW-%03d", i + 1);
                data[i][0] = label;
                data[i][1] = label;
            }
            DefaultTableModel model = new DefaultTableModel(data, columns) {
                @Override public boolean isCellEditable(int row, int column) {
                    return column == 1;
                }
            };
            model.addTableModelListener(new TableModelListener() {
                @Override public void tableChanged(TableModelEvent event) {
                    if (event.getType() != TableModelEvent.UPDATE) {
                        return;
                    }
                    int row = event.getFirstRow();
                    int column = event.getColumn();
                    if (row < 0 || column < 0) {
                        return;
                    }
                    try {
                        Files.writeString(status, "EDITED " + model.getValueAt(row, column) + "\\n");
                    } catch (Exception ex) {
                        ex.printStackTrace();
                    }
                }
            });
            JTable table = new JTable(model);
            table.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
            table.changeSelection(4, 1, false, false);
            JLabel statusLabel = new JLabel("Status: idle");
            JFrame frame = new JFrame("cuajava");
            frame.getAccessibleContext().setAccessibleName("cuajava");
            frame.setDefaultCloseOperation(JFrame.DISPOSE_ON_CLOSE);
            frame.add(new JScrollPane(table), BorderLayout.CENTER);
            frame.add(statusLabel, BorderLayout.SOUTH);
            frame.setSize(480, 420);
            frame.setLocation(40, 40);
            frame.setVisible(true);
            table.requestFocusInWindow();
        }
    }
    """
)


def _require_swing_host() -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY: the Swing window cannot open")
    if shutil.which("java") is None or shutil.which("javac") is None:
        pytest.skip("java/javac not installed")
    probe = subprocess.run(
        ["dpkg-query", "-W", "libatk-wrapper-java"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip("libatk-wrapper-java is not installed")


def _jni_dir() -> str | None:
    matches = glob.glob("/usr/lib/**/libatk-wrapper.so*", recursive=True)
    if not matches:
        return None
    return str(Path(matches[0]).parent)


def _wrapper_jar() -> str | None:
    matches = glob.glob("/usr/share/java/java-atk-wrapper.jar")
    if not matches:
        return None
    return matches[0]


def _stop(proc: subprocess.Popen) -> None:
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


def _blob(snap) -> str:
    parts: list[str] = []
    for el in snap.elements:
        parts.append(el.title or "")
        parts.append("" if el.value is None else str(el.value))
    return "\n".join(parts)


def _audit_rows(path: Path) -> list[dict]:
    import json

    return [
        json.loads(line)
        for file in path.glob("*.jsonl")
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _assert_cell_untouched(status: Path, snap) -> None:
    """The query was not written into the table.

    A wheel can move ROW-005 off the screen. That is not an edit. The model
    listener stays idle unless a keystroke changes a cell. The snapshot must
    not contain the query or the query glued onto the old value.
    """
    text = status.read_text(encoding="utf-8") if status.exists() else ""
    assert text.startswith("idle"), text
    assert "EDITED" not in text
    assert "ROW-MISSING" not in text
    blob = _blob(snap)
    assert "ROW-MISSING" not in blob
    assert "ROW-005ROW" not in blob


def test_linux_swing_scroll_to_find_does_not_type_into_the_selected_cell(tmp_path: Path) -> None:
    """Click, then full. The selected editor is not given the query.

    The search string is not in the table. A miss is not found. A wheel
    that raises page_unchanged still must leave the cell as ROW-005.
    """
    from a11y_computer_use import safety, server
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope

    _require_swing_host()
    driver = LinuxDriver()
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:
        pytest.skip(f"PyGObject/Atspi missing: {exc}")

    source = tmp_path / "CuaSwingTable.java"
    source.write_text(_JAVA, encoding="utf-8")
    compiled = subprocess.run(
        ["javac", "-encoding", "UTF-8", str(source)],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    status = tmp_path / "status.txt"
    log_path = tmp_path / "java.log"
    jar = _wrapper_jar()
    classpath = f"{jar}:{tmp_path}" if jar else str(tmp_path)
    command = [
        "java",
        "-cp", classpath,
        "-Djavax.accessibility.assistive_technologies=org.GNOME.Accessibility.AtkWrapper",
    ]
    jni = _jni_dir()
    if jni:
        command.append(f"-Djava.library.path={jni}")
    command.extend(["CuaSwingTable", str(status)])
    env = os.environ.copy()
    env["NO_AT_BRIDGE"] = "0"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        command, cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 25
        snap = None
        app = None
        last = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.flush()
                raise AssertionError(
                    f"Swing exited {proc.returncode}\n{log_path.read_text(encoding='utf-8')}"
                )
            for name in ("java", "cuajava"):
                try:
                    shot = driver.snapshot(Scope.WINDOW, name)
                except ComputerUseError as exc:
                    if exc.code is not ErrorCode.APP_NOT_FOUND:
                        raise
                    last = exc.message
                    continue
                if "ROW-005" in _blob(shot):
                    snap = shot
                    app = name
                    break
            if snap is not None:
                break
            time.sleep(0.4)
        if snap is None or app is None:
            shown = []
            try:
                shown = driver.running_apps()
            except Exception as exc:  # noqa: BLE001 - the failure text names the bus
                shown = [str(exc)]
            log.flush()
            java_log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            raise AssertionError(
                f"Swing table did not expose ROW-005 ({last!r})\napps={shown}\n{java_log}"
            )

        try:
            app = driver.activate_app(app)
        except ComputerUseError:
            pass

        def run(tier: safety.Tier, label: str) -> str:
            store = safety.PermissionStore(tmp_path / f"permissions-{label}.json")
            names = {app, "java"}
            front = driver.frontmost_app()[0]
            if front:
                names.add(front)
            for name in names:
                if name:
                    store.set_tier(name, tier)
            runtime = server.Runtime(
                store=store,
                audit=safety.AuditLog(tmp_path / f"audit-{label}"),
                driver=driver,
                allowed_domains="",
                blocked_domains="",
            )
            try:
                return runtime.scroll_to_find(
                    app, text="ROW-MISSING", max_scrolls=2, budget_s=4,
                )
            except ComputerUseError:
                return "not found"
            finally:
                runtime.close()

        click_out = run(safety.Tier.CLICK, "click")
        fresh = driver.snapshot(Scope.WINDOW, app)
        _assert_cell_untouched(status, fresh)
        assert click_out.startswith("not found")
        click_rows = _audit_rows(tmp_path / "audit-click")
        denies = [row for row in click_rows if row["action"] == "typetext"]
        assert denies, click_rows
        assert all(row["result"] == "deny" for row in denies)
        reason = denies[0]["decision"]["reason"]
        assert "full" in reason and "click" in reason
        assert not any(
            row["action"] == "keychord"
            and row["params"].get("chord") == "ctrl+f"
            and row["result"] == "ok"
            for row in click_rows
        )

        full_out = run(safety.Tier.FULL, "full")
        fresh = driver.snapshot(Scope.WINDOW, app)
        _assert_cell_untouched(status, fresh)
        assert full_out.startswith("not found")
        full_rows = _audit_rows(tmp_path / "audit-full")
        assert not any(
            row["action"] == "typetext" and row["result"] == "ok" for row in full_rows
        )
        assert not any(
            row["action"] == "keychord"
            and row["params"].get("chord") == "ctrl+f"
            and row["result"] == "ok"
            for row in full_rows
        )
    finally:
        log.close()
        _stop(proc)
