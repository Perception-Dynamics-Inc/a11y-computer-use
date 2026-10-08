"""Window list matching, window verbs, and Linux clipboard bytes.

Hermetic: fake drivers and a mocked clipboard subprocess. No display.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from a11y_computer_use import cli, safety, server
from a11y_computer_use.drivers import _linux_system
from a11y_computer_use.schema import ComputerUseError, ErrorCode


class _Windows:
    """A driver the Runtime can gate. resolves_apps so names are used as given."""

    name = "linux"
    resolves_apps = True

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple] = []
        self.owner = {row["window_id"]: row.get("app") or "" for row in rows}

    def frontmost_app(self):
        return ("front", 1)

    def windows(self):
        return list(self.rows)

    def window_owner(self, window_id):
        if window_id not in self.owner:
            raise ComputerUseError(
                ErrorCode.APP_NOT_FOUND, f"no window {window_id}", detail={"window_id": window_id},
            )
        return self.owner[window_id]

    def raise_window(self, window_id):
        self.calls.append(("raise", window_id))

    def focus_window(self, window_id):
        self.calls.append(("focus", window_id))

    def minimize_window(self, window_id):
        self.calls.append(("minimize", window_id))

    def maximize_window(self, window_id):
        self.calls.append(("maximize", window_id))

    def move_window(self, window_id, x, y):
        self.calls.append(("move", window_id, x, y))

    def resize_window(self, window_id, width, height):
        self.calls.append(("resize", window_id, width, height))

    def close_window(self, window_id):
        self.calls.append(("close", window_id))


def _runtime(tmp_path, driver, tier=safety.Tier.CLICK, app="mousepad"):
    store = safety.PermissionStore(tmp_path / "p.json")
    if app is not None and tier is not None:
        store.set_tier(app, tier)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


_ROWS = [
    {"window_id": 1, "app": "mousepad", "title": "notes", "on_screen": True,
     "bounds": {"display_id": 0, "x": 1, "y": 2, "width": 3, "height": 4}},
    {"window_id": 2, "app": "", "title": "xmessage", "on_screen": True,
     "bounds": {"display_id": 0, "x": 0, "y": 0, "width": 9, "height": 9}},
    {"window_id": 3, "app": "xmessage", "title": "hello", "on_screen": True,
     "bounds": {"display_id": 0, "x": 5, "y": 5, "width": 6, "height": 6}},
    {"window_id": 4, "app": "mousepad", "title": "hidden", "on_screen": False, "bounds": None},
    {"window_id": 5, "app": "MousePad-extra", "title": "other", "on_screen": True, "bounds": None},
]


def test_window_list_app_is_an_exact_case_insensitive_match(tmp_path) -> None:
    driver = _Windows(_ROWS)
    rt = _runtime(tmp_path, driver, tier=safety.Tier.READ)
    for name in ("MousePad", "xmessage", "thunar", "mouse", "xfce4-terminal"):
        rt.store.set_tier(name, safety.Tier.READ)
    mousepad = json.loads(rt.window("list", app="MousePad"))
    assert [row["window_id"] for row in mousepad] == [1, 4]
    hidden = next(row for row in mousepad if row["window_id"] == 4)
    assert hidden["on_screen"] is False and hidden["bounds"] is None
    assert json.loads(rt.window("list", app="xmessage")) == [
        {**_ROWS[2], "on_screen": True},
    ]
    # An app that is not running, and a substring of a running app.
    assert json.loads(rt.window("list", app="thunar")) == []
    assert json.loads(rt.window("list", app="mouse")) == []
    assert json.loads(rt.window("list", app="xfce4-terminal")) == []
    # The empty app name on window 2 never matches a non-empty filter.
    listed = json.loads(rt.window("list", app="mousepad"))
    assert all(str(row.get("app") or "").strip() for row in listed)


def test_window_list_empty_app_is_invalid_arguments(tmp_path) -> None:
    driver = _Windows(_ROWS)
    rt = _runtime(tmp_path, driver, tier=safety.Tier.READ)
    for name in ("", "   "):
        with pytest.raises(ValueError, match="non-empty") as exc:
            rt.window("list", app=name)
        assert "needs_permission" not in str(exc.value)
        assert "permission grant" not in str(exc.value)


def test_unfiltered_list_without_a_focused_window_lists_granted_apps(tmp_path) -> None:
    driver = _Windows(_ROWS)
    driver.frontmost_app = lambda: (None, None)
    rt = _runtime(tmp_path, driver, tier=safety.Tier.READ, app="mousepad")
    rows = json.loads(rt.window("list"))
    assert [row["window_id"] for row in rows] == [1, 4]
    assert all(row["app"] == "mousepad" for row in rows)


def test_unfiltered_list_without_focus_or_grants_names_the_reason(tmp_path) -> None:
    driver = _Windows(_ROWS)
    driver.frontmost_app = lambda: (None, None)
    rt = _runtime(tmp_path, driver, app=None, tier=None)
    with pytest.raises(ComputerUseError) as exc:
        rt.window("list")
    text = f"{exc.value.code.value}: {exc.value.message} {exc.value.detail}"
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "no_focused_window"
    assert "unknown has no permission grant" not in text
    assert "needs_permission" not in text


def test_unfiltered_list_with_no_windows_and_no_focus_is_empty(tmp_path) -> None:
    driver = _Windows([])
    driver.frontmost_app = lambda: (None, None)
    rt = _runtime(tmp_path, driver, app=None, tier=None)
    assert json.loads(rt.window("list")) == []


def test_unfiltered_list_keeps_a_minimized_window_off_screen(tmp_path) -> None:
    driver = _Windows(_ROWS)
    rt = _runtime(tmp_path, driver, tier=safety.Tier.READ, app="front")
    rows = json.loads(rt.window("list"))
    hidden = next(row for row in rows if row["window_id"] == 4)
    assert hidden["on_screen"] is False and hidden["bounds"] is None


def test_owner_unknown_does_not_ask_for_an_empty_grant(tmp_path) -> None:
    driver = _Windows(_ROWS)
    rt = _runtime(tmp_path, driver, app=None, tier=None)
    with pytest.raises(ComputerUseError) as exc:
        rt.window("raise", window_id=2)
    text = f"{exc.value.code.value}: {exc.value.message} {exc.value.detail}"
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "owner_unknown"
    assert "needs_permission" not in text
    assert "permission grant" not in text
    assert driver.calls == []


def test_each_window_verb_is_gated_like_raise(tmp_path) -> None:
    driver = _Windows(_ROWS)
    rt = _runtime(tmp_path, driver, tier=safety.Tier.READ)
    with pytest.raises(server.ActionRefused):
        rt.window("minimize", window_id=1)
    assert driver.calls == []
    store = rt.store
    store.set_tier("mousepad", safety.Tier.CLICK)
    store.set_tier("xmessage", safety.Tier.CLICK)
    assert rt.window("raise", window_id=1) == "raised window 1 (mousepad)"
    assert rt.window("focus", window_id=1) == "focused window 1 (mousepad)"
    assert rt.window("minimize", window_id=1) == "minimized window 1 (mousepad)"
    assert rt.window("maximize", window_id=1) == "maximized window 1 (mousepad)"
    assert rt.window("move", window_id=1, x=8, y=9) == "moved window 1 to (8, 9) (mousepad)"
    assert rt.window("resize", window_id=1, width=20, height=10) == "resized window 1 to 20x10 (mousepad)"
    assert rt.window("close", window_id=3) == "closed window 3 (xmessage)"
    assert [call[0] for call in driver.calls] == [
        "raise", "focus", "minimize", "maximize", "move", "resize", "close",
    ]


def test_unimplemented_verb_names_the_platform(tmp_path) -> None:
    class _Mac:
        name = "macos"
        resolves_apps = True

        def frontmost_app(self):
            return ("com.apple.TextEdit", 1)

        def windows(self):
            return []

        def window_owner(self, window_id):
            raise AssertionError("owner is not consulted when the verb does not exist")

        def raise_window(self, window_id):
            raise AssertionError("raise is not the verb under test")

    rt = _runtime(tmp_path, _Mac(), app=None, tier=None)
    for action in ("focus", "minimize", "maximize", "move", "resize", "close"):
        with pytest.raises(ComputerUseError) as exc:
            rt.window(action, window_id=1, x=1, y=1, width=2, height=2)
        assert exc.value.code is ErrorCode.UNSUPPORTED
        assert "macos" in exc.value.message
        assert "invalid_arguments" not in exc.value.message
        assert "not a valid WindowVerb" not in exc.value.message


def test_substring_resolution_does_not_widen_the_list(tmp_path, monkeypatch) -> None:
    """``mouse`` resolving to ``mousepad`` is not an exact app match."""
    driver = _Windows(_ROWS)
    driver.resolves_apps = False
    rt = _runtime(tmp_path, driver, tier=safety.Tier.READ)
    rt.store.set_tier("mouse", safety.Tier.READ)
    monkeypatch.setattr(server, "_running_app", lambda ident: (None, "mousepad"))
    assert json.loads(rt.window("list", app="mouse")) == []
    assert server._window_app_exact(
        {"app": "TextEdit"}, "com.apple.TextEdit",
    )
    assert not server._window_app_exact({"app": ""}, "com.apple.TextEdit")
    assert not server._window_app_exact({"app": "mousepad"}, "mouse")
    assert server._same_window_app("google-chrome", "chrome")
    assert server._same_window_app("TextEdit", "com.apple.TextEdit")
    assert not server._same_window_app("mouse", "mousepad")
    assert not server._same_window_app("", "mousepad")


def test_bad_window_action_names_the_real_verbs(tmp_path) -> None:
    rt = _runtime(tmp_path, _Windows(_ROWS), tier=safety.Tier.FULL)
    with pytest.raises(ValueError, match="focus") as exc:
        rt.window("explode")
    message = str(exc.value)
    assert "not a valid WindowVerb" not in message
    for name in ("close", "maximize", "minimize", "move", "resize"):
        assert name in message


def test_move_and_resize_require_their_arguments(tmp_path) -> None:
    driver = _Windows(_ROWS)
    rt = _runtime(tmp_path, driver)
    with pytest.raises(ValueError, match="x and y"):
        rt.window("move", window_id=1)
    with pytest.raises(ValueError, match="width and height"):
        rt.window("resize", window_id=1, width=10)
    with pytest.raises(ValueError, match="positive"):
        rt.window("resize", window_id=1, width=0, height=10)
    assert driver.calls == []


class _Proc:
    def __init__(self, returncode, stdout, stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install(monkeypatch, tool):
    def which(name):
        return f"/usr/bin/{name}" if name == tool else None

    monkeypatch.setattr(_linux_system.shutil, "which", which)


def test_clipboard_read_preserves_cr_and_crlf(monkeypatch) -> None:
    _install(monkeypatch, "xclip")
    payloads = {"first": b"a\r\nb", "second": b"a\rb"}

    def run(cmd, input=None, **kwargs):
        assert kwargs.get("text") is not True
        if "TARGETS" in cmd:
            return _Proc(0, b"TARGETS\nUTF8_STRING\nSTRING\n")
        payloads["seen"] = True
        return _Proc(0, payloads["body"])

    monkeypatch.setattr(_linux_system.subprocess, "run", run)
    for body in (b"a\r\nb", b"a\rb", b"line\n", "héllo\x00\t\x1b".encode()):
        payloads["body"] = body
        assert _linux_system.read_clipboard() == body.decode("utf-8")


def test_clipboard_read_errors_are_not_a_silent_empty_string(monkeypatch) -> None:
    _install(monkeypatch, "xclip")

    def run_image(cmd, input=None, **kwargs):
        if "TARGETS" in cmd:
            return _Proc(0, b"TARGETS\nimage/png\n")
        raise AssertionError("a non-text clipboard is not decoded")

    monkeypatch.setattr(_linux_system.subprocess, "run", run_image)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.read_clipboard()
    assert exc.value.detail["reason"] == "clipboard_not_text"
    assert "image/png" in exc.value.message
    assert exc.value.message != ""

    def run_bad(cmd, input=None, **kwargs):
        if "TARGETS" in cmd:
            return _Proc(0, b"UTF8_STRING\n")
        return _Proc(0, b"ok\xff\xfe")

    monkeypatch.setattr(_linux_system.subprocess, "run", run_bad)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.read_clipboard()
    assert exc.value.detail["reason"] == "clipboard_invalid_utf8"

    def run_owner(cmd, input=None, **kwargs):
        return _Proc(1, b"", b"Error: target STRING not available\n")

    monkeypatch.setattr(_linux_system.subprocess, "run", run_owner)
    with pytest.raises(ComputerUseError) as exc:
        _linux_system.read_clipboard()
    assert exc.value.detail["reason"] == "clipboard_no_owner"

    def run_empty(cmd, input=None, **kwargs):
        if "TARGETS" in cmd:
            return _Proc(0, b"UTF8_STRING\n")
        return _Proc(0, b"")

    monkeypatch.setattr(_linux_system.subprocess, "run", run_empty)
    assert _linux_system.read_clipboard() == ""


def test_clipboard_missing_tool_is_unsupported_on_read_and_write(monkeypatch) -> None:
    monkeypatch.setattr(_linux_system.shutil, "which", lambda name: None)
    for call in (_linux_system.read_clipboard, lambda: _linux_system.write_clipboard("hi")):
        with pytest.raises(ComputerUseError) as exc:
            call()
        assert exc.value.code is ErrorCode.UNSUPPORTED
        assert exc.value.detail["reason"] == "missing_clipboard_tool"
        text = exc.value.message
        assert "xclip" in text and "xsel" in text and "wl-clipboard" in text
        assert "report a defect" not in text
        assert not isinstance(exc.value, RuntimeError)


def test_clipboard_write_keeps_unicode_nul_and_large_payloads(monkeypatch) -> None:
    _install(monkeypatch, "xclip")
    captured = {}

    def run(cmd, input=None, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["kwargs"] = kwargs
        captured["text"] = kwargs.get("text", False)
        return _Proc(0, b"")

    monkeypatch.setattr(_linux_system.subprocess, "run", run)
    text = "héllo\x00\r\n" + ("x" * 4_000_000)
    _linux_system.write_clipboard(text)
    assert captured["text"] is False
    assert captured["kwargs"].get("capture_output") is not True
    assert captured["kwargs"].get("stdout") is subprocess.DEVNULL
    assert captured["kwargs"].get("stderr") is subprocess.DEVNULL
    assert captured["input"] == text.encode("utf-8")
    assert len(captured["input"]) == len(text.encode("utf-8"))


def test_clipboard_write_of_a_lone_surrogate_is_value_error() -> None:
    with pytest.raises(ValueError, match="surrogate"):
        _linux_system.write_clipboard("bad\ud800x")


def test_run_once_lone_surrogate_is_one_line_invalid_arguments(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    class _D:
        name = "fake"
        resolves_apps = True

        def frontmost_app(self):
            return ("app", 1)

        def write_clipboard(self, text):
            raise AssertionError("the surrogate is rejected before the tool runs")

    monkeypatch.setattr("a11y_computer_use.drivers.get_driver", lambda *a, **k: _D())
    code = cli.main(["run-once", '{"tool":"clipboard","action":"write","text":"bad\\ud800x"}'])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("invalid_arguments: clipboard:")
    assert "surrogate" in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0


def test_xsel_and_wl_paste_read_the_named_text_target(monkeypatch) -> None:
    seen = []

    def which_xsel(name):
        return "/usr/bin/xsel" if name == "xsel" else None

    def run(cmd, input=None, **kwargs):
        seen.append(cmd)
        if "TARGETS" in cmd:
            return _Proc(0, b"text/plain\n")
        return _Proc(0, b"from-xsel")

    monkeypatch.setattr(_linux_system.shutil, "which", which_xsel)
    monkeypatch.setattr(_linux_system.subprocess, "run", run)
    assert _linux_system.read_clipboard() == "from-xsel"
    assert seen[0] == ["xsel", "--clipboard", "--output", "--target", "TARGETS"]
    assert seen[1] == ["xsel", "--clipboard", "--output", "--target", "text/plain"]

    seen.clear()

    def which_wl(name):
        return "/usr/bin/wl-paste" if name == "wl-paste" else None

    def run_wl(cmd, input=None, **kwargs):
        seen.append(cmd)
        if cmd[:2] == ["wl-paste", "--list-types"]:
            return _Proc(0, b"text/plain;charset=utf-8\n")
        return _Proc(0, "ü".encode())

    monkeypatch.setattr(_linux_system.shutil, "which", which_wl)
    monkeypatch.setattr(_linux_system.subprocess, "run", run_wl)
    assert _linux_system.read_clipboard() == "ü"
    assert seen[0] == ["wl-paste", "--list-types"]
    assert seen[1] == ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"]
