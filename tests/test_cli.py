"""CLI smoke tests: doctor, the snapshot permission path, run-once, mcp wiring.

Everything runs in-process through `cli.main` (plus one subprocess smoke test
of the ``python -m a11y_computer_use`` wiring). Permission-dependent paths are
monkeypatched so they are deterministic on granted and ungranted machines;
the one live ungranted-path test is skipif-guarded in the reverse direction.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from a11y_computer_use import act, cli, doctor, drivers, observe, safety, server
from a11y_computer_use.drivers import browser
from a11y_computer_use.schema import ComputerUseError, Display, ErrorCode
from tests.conftest import HAS_AX, build_synthetic_snapshot
from tests.test_arena import _png
from tests.test_doctor import TERMINAL_PS, _canned

FRONT = "com.test.front"


@pytest.fixture
def fake_front(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: FRONT)


@pytest.fixture
def home(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point ``~`` at tmp so the default PermissionStore/AuditLog paths
    (used by ``run-once``) never touch the real user config."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --- doctor -------------------------------------------------------------------

#: The first doctor check differs per platform: TCC's responsible app on macOS,
#: the display session on Linux, the UI Automation import on Windows.
_FIRST_CHECK = {"darwin": "responsible_app", "win32": "uiautomation_import"}.get(
    sys.platform, "display_session"
)


def test_doctor_exits_zero_and_names_responsible_app(capsys, monkeypatch) -> None:
    chain = doctor.parent_chain(500, run_ps=_canned(TERMINAL_PS))
    monkeypatch.setattr(doctor, "parent_chain", lambda pid: chain)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert _FIRST_CHECK in out
    if sys.platform == "darwin":
        assert "Terminal" in out
    assert "checks passed" in out


def test_doctor_subprocess_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "a11y_computer_use", "doctor"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert _FIRST_CHECK in result.stdout
    assert "checks passed" in result.stdout


# --- snapshot -------------------------------------------------------------------


class _SnapshotDriver:
    """Stand-in for `drivers.get_driver()`: the CLI goes through the Driver seam,
    so these tests exercise the CLI on every platform, not the macOS observe path."""

    def __init__(self, build, front=("com.apple.TextEdit", 42)) -> None:
        self._build = build
        self._front = front
        self.seen: list[str] = []

    def frontmost_app(self):
        return self._front

    def snapshot(self, scope, app):
        self.seen.append(app)
        return self._build()


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the macOS observe trust probe")
def test_snapshot_without_ax_exits_nonzero_with_structured_message(
    capsys, monkeypatch
) -> None:
    monkeypatch.setattr(observe, "_is_trusted", lambda: False)
    assert cli.main(["snapshot", "--app", "TextEdit"]) == 1
    err = capsys.readouterr().err
    assert "permission_denied_accessibility" in err
    assert "doctor" in err


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS TCC path")
@pytest.mark.skipif(HAS_AX, reason="machine holds the AX grant; ungranted path unreachable")
def test_snapshot_without_ax_live_permission_path(capsys) -> None:
    """The real (unmocked) ungranted call degrades to the same structured error."""
    assert cli.main(["snapshot", "--app", "TextEdit"]) == 1
    assert "permission_denied_accessibility" in capsys.readouterr().err


def test_snapshot_prints_pruned_tree(capsys, monkeypatch, snapshot_builder) -> None:
    monkeypatch.setattr(drivers, "get_driver", lambda name=None: _SnapshotDriver(snapshot_builder))
    assert cli.main(["snapshot", "--app", "TextEdit"]) == 0
    out = capsys.readouterr().out
    assert "[snap-test-1]" in out
    assert 'e2 button "Save"' in out


def test_snapshot_defaults_to_frontmost_app(capsys, monkeypatch, snapshot_builder) -> None:
    fake = _SnapshotDriver(snapshot_builder, front=("com.apple.TextEdit", 42))
    monkeypatch.setattr(drivers, "get_driver", lambda name=None: fake)
    assert cli.main(["snapshot"]) == 0
    assert fake.seen == ["com.apple.TextEdit"]


# --- run-once -------------------------------------------------------------------


def test_run_once_rejects_invalid_json(capsys) -> None:
    assert cli.main(["run-once", "{not json"]) == 2
    assert "invalid JSON" in capsys.readouterr().err


def test_run_once_rejects_missing_tool_key(capsys) -> None:
    assert cli.main(["run-once", '{"chord": "cmd+s"}']) == 2
    assert '"tool" key' in capsys.readouterr().err


def test_run_once_rejects_unknown_tool(capsys, home) -> None:
    assert cli.main(["run-once", '{"tool": "frobnicate"}']) == 2
    assert "unknown tool" in capsys.readouterr().err


def test_run_once_does_not_offer_wait_for(capsys, home) -> None:
    # A one-shot process has no snapshot epoch, so refs can never resolve;
    # advertising wait_for here would be a tool that always fails.
    assert cli.main(["run-once", '{"tool": "wait_for", "ref": "e2"}']) == 2
    assert "unknown tool" in capsys.readouterr().err


def test_run_once_ungranted_app_is_refused_and_audited(capsys, home, fake_front) -> None:
    assert cli.main(["run-once", '{"tool": "key", "chord": "cmd+s"}']) == 1
    err = capsys.readouterr().err
    assert "needs_permission" in err
    assert FRONT in err
    audit_files = list((home / ".a11y-computer-use" / "audit").glob("*.jsonl"))
    assert audit_files, "refused action was not audit-logged"


def test_run_once_granted_action_executes(capsys, home, fake_front, monkeypatch) -> None:
    safety.PermissionStore(home / ".a11y-computer-use" / "permissions.json").set_tier(
        FRONT, safety.Tier.FULL
    )
    pressed: list[str] = []
    # the Runtime routes key through the driver, which delegates to act.key_chord
    # on the macOS driver; select it explicitly so the seam is the same on every OS.
    monkeypatch.setenv("A11Y_COMPUTER_USE_DRIVER", "macos")
    monkeypatch.setattr(act, "key_chord", lambda chord, **kw: pressed.append(chord) or [])
    assert cli.main(["run-once", '{"tool": "key", "chord": "cmd+s"}']) == 0
    assert capsys.readouterr().out.strip() == "pressed cmd+s"
    assert pressed == ["cmd+s"]


# --- mcp ------------------------------------------------------------------------


def test_mcp_subcommand_runs_server_over_stdio(monkeypatch) -> None:
    transports: list[str] = []
    fake = SimpleNamespace(run=lambda transport: transports.append(transport))
    monkeypatch.setattr(server, "build_server", lambda **kw: fake)
    assert cli.main(["mcp"]) == 0
    assert transports == ["stdio"]


# --- snapshot view flags ---------------------------------------------------------


def test_snapshot_mode_budget_and_bounds_flags(capsys, monkeypatch, snapshot_builder) -> None:
    monkeypatch.setattr(drivers, "get_driver", lambda name=None: _SnapshotDriver(snapshot_builder))
    assert cli.main(["snapshot", "--app", "TextEdit", "--mode", "interactive"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("interactive") and 'e2 button "Save"' in out
    assert cli.main(["snapshot", "--app", "TextEdit", "--budget", "12"]) == 0
    assert "truncated at ~12 tokens" in capsys.readouterr().out
    assert cli.main(["snapshot", "--app", "TextEdit", "--bounds"]) == 0
    assert capsys.readouterr().out.count(" @1:") == 5  # every element line carries geometry


# --- bench desktop / bench web flags ------------------------------------------------


class _BenchDriver:
    """A Driver stand-in for the bench subcommands (no OS, no browser)."""

    name = "fake"

    def __init__(self, endpoint=None, fail: bool = False) -> None:
        self.fail = fail
        self.urls: list[str] = []

    def ensure_trusted(self) -> None:
        if self.fail:
            raise ComputerUseError(ErrorCode.PERMISSION_DENIED_ACCESSIBILITY, "no grant")

    def frontmost_app(self):
        return "com.test.app", 1

    def snapshot(self, scope, app):
        return build_synthetic_snapshot(app=app)

    def screenshot(self, display_id=None):
        return SimpleNamespace(png=_png(1600, 1200), display=Display(0, 1600, 1200, 2.0, True))

    def navigate(self, url, **kw) -> None:
        self.urls.append(url)

    def close(self) -> None:
        pass


def test_bench_desktop_prints_report_and_json(capsys, monkeypatch) -> None:
    monkeypatch.setattr(drivers, "get_driver", lambda name=None: _BenchDriver())
    assert cli.main(["bench", "desktop", "--rounds", "2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("cu-arena desktop  app=com.test.app") and "a11y interactive" in out
    assert cli.main(["bench", "desktop", "--app", "com.test.app", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["app"] == "com.test.app" and set(data["modes"]) == {"full", "interactive"}


def test_bench_desktop_permission_error_is_structured(capsys, monkeypatch) -> None:
    monkeypatch.setattr(drivers, "get_driver", lambda name=None: _BenchDriver(fail=True))
    assert cli.main(["bench", "desktop"]) == 1
    assert "permission_denied_accessibility" in capsys.readouterr().err


def test_bench_web_mode_and_json(capsys, monkeypatch) -> None:
    monkeypatch.setattr(browser, "BrowserDriver", _BenchDriver)
    assert cli.main(["bench", "web", "https://example.com", "--mode", "interactive",
                     "--json", "--rounds", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "interactive" and len(data["observations"]) == 1
    assert cli.main(["bench", "web", "https://example.com", "--rounds", "1"]) == 0
    assert "cu-arena  observations=1" in capsys.readouterr().out


def test_grant_target_accepts_an_installed_but_not_running_app(monkeypatch) -> None:
    """--grant must not abort when the target app is not running yet: the loop can launch it."""
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    class _RT:
        def _frontmost(self): return "com.front.app"
        def _resolve_app(self, ident):
            raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "nope", detail={"app": ident})

    assert cli._grant_target(_RT(), "org.krita") == "org.krita"          # bundle id as given
    with pytest.raises(ComputerUseError) as info:
        cli._grant_target(_RT(), "definitely-not-an-app-9f3")
    assert info.value.code is ErrorCode.APP_NOT_FOUND
    with pytest.raises(ComputerUseError):
        cli._grant_target(_RT(), None)                                    # frontmost must resolve

    class _RTOk(_RT):
        def _resolve_app(self, ident): return (object(), "com.resolved.app")

    assert cli._grant_target(_RTOk(), "Whatever") == "com.resolved.app"
