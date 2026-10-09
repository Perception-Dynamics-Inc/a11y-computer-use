"""Linux ``type``/``key`` with ``app=``, and a blank ``app`` on every tool.

Hermetic: a fake Linux driver and recorded windows. No display, no AT-SPI
bus, and no keystrokes leave the process. The live landing of text in the
named window is ``tests/test_linux_live.py``.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope, Snapshot


class _LinuxKeys:
    """Windows plus an active-window id. ``focus_window`` moves that id."""

    name = "linux"
    resolves_apps = True

    def __init__(self, rows: list[dict], active_id: int) -> None:
        self.rows = rows
        self.active_id = active_id
        self.typed: list[str] = []
        self.chords: list[str] = []
        self.focused: list[int] = []
        self.stick = True

    def ensure_trusted(self) -> None:
        return None

    def frontmost_app(self):
        row = self._row(self.active_id)
        return ((row or {}).get("app"), (row or {}).get("pid"))

    def windows(self):
        return list(self.rows)

    def active_window(self):
        row = self._row(self.active_id)
        if row is None:
            return None
        return {
            "window_id": row["window_id"],
            "app": row.get("app"),
            "pid": row.get("pid") or 0,
            "title": row.get("title") or "",
        }

    def focus_window(self, window_id: int) -> None:
        self.focused.append(int(window_id))
        if self.stick:
            self.active_id = int(window_id)

    def type_text(self, text, **_kwargs) -> int:
        self.typed.append(text)
        return len(text)

    def key_chord(self, chord, **_kwargs) -> None:
        self.chords.append(chord)

    def _row(self, window_id: int):
        for row in self.rows:
            if int(row["window_id"]) == int(window_id):
                return row
        return None


def _rows() -> list[dict]:
    return [
        {"window_id": 1, "app": "other", "title": "other", "pid": 11,
         "on_screen": True, "wm_class": "other", "wm_class_class": "Other"},
        {"window_id": 2, "app": "chrome", "title": "Chrome", "pid": 22,
         "on_screen": True, "wm_class": "google-chrome", "wm_class_class": "Google-chrome"},
        {"window_id": 3, "app": "python3", "title": "cuakeytarget", "pid": 33,
         "on_screen": True, "wm_class": "cuakeytarget", "wm_class_class": "Cuakeytarget"},
        {"window_id": 4, "app": "python3", "title": "cuakeyother", "pid": 44,
         "on_screen": True, "wm_class": "cuakeyother", "wm_class_class": "Cuakeyother"},
    ]


def _runtime(tmp_path, driver: _LinuxKeys, *apps: str):
    store = safety.PermissionStore(tmp_path / "p.json")
    for app in apps:
        store.set_tier(app, safety.Tier.FULL)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.setattr(server, "_atspi_pids_for", lambda _app: set())


def test_type_and_key_with_app_when_that_window_is_already_active(tmp_path, no_bus) -> None:
    driver = _LinuxKeys(_rows(), active_id=2)
    rt = _runtime(tmp_path, driver, "chrome")
    typed = rt.type_text("https://example.com", app="chrome")
    pressed = rt.key("ctrl+l", app="chrome")
    assert typed == "typed 19 characters into chrome"
    assert pressed == "pressed ctrl+l in chrome"
    assert "macOS" not in typed and "macOS" not in pressed
    assert driver.focused == []
    assert driver.typed == ["https://example.com"]
    assert driver.chords == ["ctrl+l"]


def test_type_and_key_focus_the_named_window_when_another_is_active(tmp_path, no_bus) -> None:
    driver = _LinuxKeys(_rows(), active_id=1)
    rt = _runtime(tmp_path, driver, "chrome")
    typed = rt.type_text("https://example.com", app="chrome")
    assert "focused its window first" in typed
    assert driver.focused == [2]
    driver.active_id = 1  # the other window is in front again
    pressed = rt.key("ctrl+l", app="chrome")
    assert "focused its window first" in pressed
    assert "macOS" not in typed and "macOS" not in pressed
    assert driver.focused == [2, 2]
    assert driver.active_id == 2
    assert driver.typed == ["https://example.com"]
    assert driver.chords == ["ctrl+l"]


def test_app_matches_the_atspi_pid_when_the_window_comm_does_not(tmp_path, monkeypatch) -> None:
    """A GTK script is python3 on the window list and cuakeytarget on AT-SPI."""
    monkeypatch.setattr(server, "_atspi_pids_for", lambda app: {33} if app == "cuakeytarget" else set())
    driver = _LinuxKeys(_rows(), active_id=4)
    rt = _runtime(tmp_path, driver, "cuakeytarget")
    rt.type_text("landed", app="cuakeytarget")
    assert driver.focused == [3]
    assert driver.typed == ["landed"]
    assert driver.active_id == 3


def test_wm_class_selects_the_script_when_the_bus_has_no_pid(tmp_path, no_bus) -> None:
    driver = _LinuxKeys(_rows(), active_id=4)
    rt = _runtime(tmp_path, driver, "cuakeytarget")
    rt.key("a", app="cuakeytarget")
    assert driver.focused == [3]
    assert driver.chords == ["a"]


def test_focus_that_does_not_stick_is_focus_changed_and_sends_nothing(tmp_path, no_bus) -> None:
    driver = _LinuxKeys(_rows(), active_id=1)
    driver.stick = False
    rt = _runtime(tmp_path, driver, "chrome")
    rt.APP_FOCUS_WAIT_S = 0.05
    with pytest.raises(ComputerUseError) as exc:
        rt.type_text("nope", app="chrome")
    assert exc.value.code is ErrorCode.FOCUS_CHANGED
    text = f"{exc.value.code.value}: {exc.value.message}"
    assert "macOS" not in text
    assert "other" in exc.value.message
    assert exc.value.detail["window_id"] == 2
    assert driver.typed == []
    assert driver.focused == [2]


def test_missing_window_is_app_not_found_and_not_macos_only(tmp_path, no_bus) -> None:
    driver = _LinuxKeys(_rows(), active_id=1)
    rt = _runtime(tmp_path, driver, "soffice.bin")
    with pytest.raises(ComputerUseError) as exc:
        rt.key("ctrl+s", app="soffice.bin")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert exc.value.detail["reason"] == "no_window"
    assert "macOS" not in exc.value.message
    assert driver.chords == []
    assert driver.focused == []


def test_ungranted_app_is_needs_permission_and_is_not_focused(tmp_path, no_bus) -> None:
    driver = _LinuxKeys(_rows(), active_id=1)
    rt = _runtime(tmp_path, driver)
    with pytest.raises(server.ActionRefused) as exc:
        rt.type_text("hi", app="chrome")
    assert exc.value.decision.verdict is safety.Verdict.NEEDS_PERMISSION
    assert exc.value.decision.app == "chrome"
    assert driver.focused == []
    assert driver.typed == []


def test_background_mode_focuses_the_observed_app(tmp_path, no_bus, monkeypatch) -> None:
    monkeypatch.setattr(server, "FOCUS_MODE", "background")
    driver = _LinuxKeys(_rows(), active_id=1)
    rt = _runtime(tmp_path, driver, "chrome")
    rt._current = Snapshot(
        snapshot_id="s", scope=Scope.WINDOW, app="chrome", pid=22,
        created_at=0.0, displays=(), elements=(),
    )
    rt.type_text("x")
    assert driver.focused == [2]
    assert driver.typed == ["x"]


def test_blank_app_is_rejected_before_a_permission_check(tmp_path) -> None:
    driver = _LinuxKeys(_rows(), active_id=1)
    driver.ensure_trusted = lambda: (_ for _ in ()).throw(AssertionError("must not run"))
    rt = _runtime(tmp_path, driver, "chrome")
    calls = [
        lambda: rt.desktop_snapshot(""),
        lambda: rt.desktop_snapshot("   ", mode="interactive"),
        lambda: rt.find("", text="x"),
        lambda: rt.find("  ", text="x"),
        lambda: rt.menu(""),
        lambda: rt.menu(" "),
        lambda: rt.scroll_to_find("", text="x"),
        lambda: rt.screen_text(app=""),
        lambda: rt.type_text("hi", app=""),
        lambda: rt.type_text("hi", app="   "),
        lambda: rt.key("ctrl+l", app=""),
        lambda: rt.file_dialog("open", "/tmp/notes.txt", app=""),
        lambda: rt.window("list", app=""),
        lambda: rt.window("raise", window_id=1, app="  "),
        lambda: rt.console(""),
        lambda: rt.network(" "),
        lambda: rt.webmcp(""),
        lambda: rt.wait_until({"snapshot_text": "done", "app": ""}),
        lambda: rt.wait_until({"snapshot_text": "done", "app": "  "}),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="non-empty") as exc:
            call()
        text = str(exc.value)
        assert "needs_permission" not in text
        assert "permission grant" not in text
    assert driver.typed == []
    assert driver.focused == []


def test_atspi_pid_is_not_widened_to_the_resolved_shared_comm(tmp_path, monkeypatch) -> None:
    """``cuakeytarget`` resolves to comm ``python3``. The bus pid stays the target.

    Looking up AT-SPI for ``python3`` would return the other Python window,
    which is the one in front. The keystrokes must still go to pid 33.
    """
    def pids(app: str) -> set[int]:
        if app == "cuakeytarget":
            return {33}
        if app == "python3":
            return {44}
        return set()

    monkeypatch.setattr(server, "_atspi_pids_for", pids)
    monkeypatch.setattr(server, "_running_app", lambda _ident: (None, "python3"))
    driver = _LinuxKeys(_rows(), active_id=4)
    driver.resolves_apps = False
    rt = _runtime(tmp_path, driver, "python3", "cuakeytarget")
    rt.type_text("landed", app="cuakeytarget")
    assert driver.focused == [3]
    assert driver.typed == ["landed"]
    assert driver.active_id == 3


def test_launcher_alias_finds_the_resolved_comm_without_a_class_or_pid() -> None:
    rows = [{
        "window_id": 2, "app": "chrome", "title": "Chrome", "pid": 22,
        "on_screen": True, "wm_class": "", "wm_class_class": "",
    }]
    found = server.linux_windows_for_app(rows, "google-chrome", "chrome", set())
    assert [row["window_id"] for row in found] == [2]


def test_a_specific_name_does_not_take_every_window_of_a_shared_comm() -> None:
    rows = [
        {"window_id": 3, "app": "python3", "title": "cuakeytarget", "pid": 33,
         "on_screen": True, "wm_class": "", "wm_class_class": ""},
        {"window_id": 4, "app": "python3", "title": "cuakeyother", "pid": 44,
         "on_screen": True, "wm_class": "", "wm_class_class": ""},
    ]
    assert server.linux_windows_for_app(rows, "cuakeytarget", "python3", set()) == []


def test_omitted_app_still_types_into_the_frontmost_window(tmp_path, no_bus, monkeypatch) -> None:
    driver = _LinuxKeys(_rows(), active_id=1)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "other")
    rt = _runtime(tmp_path, driver, "other")
    # resolves_apps is set, so the frontmost name comes from the driver.
    assert rt.type_text("hi") == "typed 2 characters"
    assert driver.focused == []
    assert driver.typed == ["hi"]
