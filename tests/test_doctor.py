"""Doctor tests: report structure, responsible-process parsing, grant paths.

Everything here runs without TCC grants (probes are monkeypatched, ps output
is canned). The live-probe tests at the bottom are skipif-guarded in *both*
directions so the suite stays green on granted and ungranted machines while
always exercising the structured permission path that applies.
"""

from __future__ import annotations

import importlib
import importlib.metadata

import pytest

from computeruse import doctor
from computeruse.doctor import (
    ACCESSIBILITY_SETTINGS_URL,
    SCREEN_RECORDING_SETTINGS_URL,
    CheckResult,
    parent_chain,
    render_text,
    responsible_app,
    run_doctor,
)
from tests.conftest import HAS_AX, HAS_SCREEN

# --- canned `ps -o ppid=,comm=` tables: pid -> raw ps output ----------------

TERMINAL_PS = {
    500: "  400 /Users/dev/project/.venv/bin/python\n",
    400: "  300 -zsh\n",
    300: "  200 login\n",
    200: "    1 /System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n",
}

CLAUDE_PS = {
    500: "  400 /Users/dev/project/.venv/bin/python\n",
    400: "  300 /usr/local/bin/node\n",
    300: "    1 /Applications/Claude.app/Contents/MacOS/Claude\n",
}

ORPHAN_PS = {
    500: "  400 /usr/bin/python3\n",
    400: "    1 /usr/libexec/somethingd\n",
}


def _canned(table: dict[int, str]):
    return lambda pid: table.get(pid, "")


def _by_check(report: list[CheckResult], check: str) -> CheckResult:
    return next(result for result in report if result["check"] == check)


# --- responsible-process parser ---------------------------------------------


def test_parent_chain_terminal() -> None:
    assert parent_chain(500, run_ps=_canned(TERMINAL_PS)) == [
        "/Users/dev/project/.venv/bin/python",
        "-zsh",
        "login",
        "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
    ]


def test_responsible_app_terminal_chain() -> None:
    assert responsible_app(parent_chain(500, run_ps=_canned(TERMINAL_PS))) == "Terminal"


def test_responsible_app_claude_chain() -> None:
    assert responsible_app(parent_chain(500, run_ps=_canned(CLAUDE_PS))) == "Claude"


def test_responsible_app_orphan_chain() -> None:
    assert responsible_app(parent_chain(500, run_ps=_canned(ORPHAN_PS))) is None


@pytest.mark.parametrize(
    ("comm", "expected"),
    [
        ("/Applications/iTerm.app/Contents/MacOS/iTerm2", "iTerm"),
        ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
    ],
)
def test_responsible_app_extracts_bundle_name(comm: str, expected: str) -> None:
    assert responsible_app([comm]) == expected


def test_parent_chain_stops_on_ppid_cycle() -> None:
    cycle = {500: "  400 child\n", 400: "  500 parent\n"}
    assert parent_chain(500, run_ps=_canned(cycle)) == ["child", "parent"]


def test_parent_chain_respects_max_depth() -> None:
    # 500 -> 499 -> 498 -> ... each pid parents to pid-1, far past the cap.
    endless = lambda pid: f"  {pid - 1} /bin/proc{pid}\n"  # noqa: E731
    assert len(parent_chain(500, run_ps=endless, max_depth=5)) == 5


def test_parent_chain_empty_when_ps_unavailable() -> None:
    assert parent_chain(500, run_ps=lambda pid: "") == []


@pytest.mark.parametrize("raw", ["", "   \n", "notdigits /bin/zsh", "42"])
def test_parse_ps_line_rejects_garbage(raw: str) -> None:
    assert doctor._parse_ps_line(raw) is None


def test_parse_ps_line_keeps_spaces_in_command_path() -> None:
    raw = "    1 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
    assert doctor._parse_ps_line(raw) == (
        1,
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )


# --- run_doctor report structure (probes monkeypatched) ----------------------


EXPECTED_CHECKS = [
    "responsible_app",
    "accessibility_grant",
    "screen_recording_grant",
    "python_version",
    "pyobjc_version",
    "mcp_import",
]


def _patch_environment(
    monkeypatch: pytest.MonkeyPatch, *, ax: bool, screen: bool, ps_table: dict[int, str]
) -> None:
    monkeypatch.setattr(doctor, "_ax_trusted", lambda: ax)
    monkeypatch.setattr(doctor, "_screen_capture_preflight", lambda: screen)
    monkeypatch.setattr(doctor, "_run_ps", _canned(ps_table))
    monkeypatch.setattr(doctor.os, "getpid", lambda: 500)


def test_run_doctor_all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_environment(monkeypatch, ax=True, screen=True, ps_table=TERMINAL_PS)
    report = run_doctor()
    assert [result["check"] for result in report] == EXPECTED_CHECKS
    assert all(set(result) == {"check", "ok", "detail", "fix"} for result in report)
    assert all(result["ok"] for result in report)
    assert all(result["fix"] is None for result in report)


def test_run_doctor_missing_grants_name_the_host_app(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_environment(monkeypatch, ax=False, screen=False, ps_table=TERMINAL_PS)
    report = run_doctor()

    ax = _by_check(report, "accessibility_grant")
    assert ax["ok"] is False
    assert ax["fix"] is not None
    assert "Grant Accessibility to Terminal" in ax["fix"]
    assert "Privacy & Security" in ax["fix"]
    assert ACCESSIBILITY_SETTINGS_URL in ax["fix"]

    screen = _by_check(report, "screen_recording_grant")
    assert screen["ok"] is False
    assert screen["fix"] is not None
    assert "Grant Screen Recording to Terminal" in screen["fix"]
    assert SCREEN_RECORDING_SETTINGS_URL in screen["fix"]


def test_run_doctor_claude_chain_names_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_environment(monkeypatch, ax=False, screen=False, ps_table=CLAUDE_PS)
    report = run_doctor()
    ax = _by_check(report, "accessibility_grant")
    assert ax["fix"] is not None
    assert "Grant Accessibility to Claude" in ax["fix"]


def test_run_doctor_orphan_chain_falls_back_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_environment(monkeypatch, ax=False, screen=False, ps_table=ORPHAN_PS)
    report = run_doctor()

    host = _by_check(report, "responsible_app")
    assert host["ok"] is False
    assert host["fix"] is not None

    ax = _by_check(report, "accessibility_grant")
    assert ax["fix"] is not None
    assert "could not auto-detect" in ax["fix"]
    assert ACCESSIBILITY_SETTINGS_URL in ax["fix"]


def test_run_doctor_never_raises_when_ps_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_run_ps", lambda pid: "")
    report = run_doctor()  # failures are data, not exceptions
    assert _by_check(report, "responsible_app")["ok"] is False


def test_environment_checks_pass_in_this_venv() -> None:
    # pyproject pins python>=3.11 and installs pyobjc + mcp; all three hold here.
    report = run_doctor()
    assert _by_check(report, "python_version")["ok"] is True
    assert _by_check(report, "pyobjc_version")["ok"] is True
    assert _by_check(report, "mcp_import")["ok"] is True


def test_mcp_check_reports_broken_import(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str):
        raise ImportError("boom")

    monkeypatch.setattr(importlib, "import_module", broken)
    result = doctor._check_mcp()
    assert result["ok"] is False
    assert result["fix"] == "Install the MCP SDK: pip install mcp"


def test_pyobjc_check_reports_missing_install(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    result = doctor._check_pyobjc()
    assert result["ok"] is False
    assert result["fix"] is not None
    assert "pyobjc-framework-ApplicationServices" in result["fix"]


# --- render_text --------------------------------------------------------------


def test_render_text_lists_checks_fixes_and_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_environment(monkeypatch, ax=False, screen=True, ps_table=TERMINAL_PS)
    text = render_text(run_doctor())
    assert "[FAIL] accessibility_grant" in text
    assert "[ OK ] screen_recording_grant" in text
    assert "fix: Grant Accessibility to Terminal" in text
    assert "5/6 checks passed" in text


# --- live TCC probes (structured permission path, guarded both ways) ----------


@pytest.mark.skipif(HAS_AX, reason="AX grant present; this asserts the ungranted path")
def test_live_accessibility_missing_grant_is_structured_not_raised() -> None:
    ax = _by_check(run_doctor(), "accessibility_grant")
    assert ax["ok"] is False
    assert ax["fix"] is not None
    assert "Grant Accessibility to" in ax["fix"]
    assert ACCESSIBILITY_SETTINGS_URL in ax["fix"]


@pytest.mark.skipif(not HAS_AX, reason="requires the Accessibility TCC grant")
def test_live_accessibility_grant_detected() -> None:
    ax = _by_check(run_doctor(), "accessibility_grant")
    assert ax["ok"] is True
    assert ax["fix"] is None


@pytest.mark.skipif(HAS_SCREEN, reason="Screen Recording grant present; asserts the ungranted path")
def test_live_screen_recording_missing_grant_is_structured_not_raised() -> None:
    screen = _by_check(run_doctor(), "screen_recording_grant")
    assert screen["ok"] is False
    assert screen["fix"] is not None
    assert SCREEN_RECORDING_SETTINGS_URL in screen["fix"]


@pytest.mark.skipif(not HAS_SCREEN, reason="requires the Screen Recording TCC grant")
def test_live_screen_recording_grant_detected() -> None:
    screen = _by_check(run_doctor(), "screen_recording_grant")
    assert screen["ok"] is True
    assert screen["fix"] is None
