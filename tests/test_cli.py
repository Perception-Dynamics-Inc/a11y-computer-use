"""CLI smoke tests: doctor, the snapshot permission path, run-once, mcp wiring.

Everything runs in-process through `cli.main` (plus one subprocess smoke test
of the ``python -m computeruse`` wiring). Permission-dependent paths are
monkeypatched so they are deterministic on granted and ungranted machines;
the one live ungranted-path test is skipif-guarded in the reverse direction.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from computeruse import cli, doctor, observe, safety, server
from tests.conftest import HAS_AX
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


def test_doctor_exits_zero_and_names_responsible_app(capsys, monkeypatch) -> None:
    chain = doctor.parent_chain(500, run_ps=_canned(TERMINAL_PS))
    monkeypatch.setattr(doctor, "parent_chain", lambda pid: chain)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "responsible_app" in out
    assert "Terminal" in out
    assert "checks passed" in out


def test_doctor_subprocess_smoke() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "computeruse", "doctor"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "responsible_app" in result.stdout
    assert "checks passed" in result.stdout


# --- snapshot -------------------------------------------------------------------


def test_snapshot_without_ax_exits_nonzero_with_structured_message(
    capsys, monkeypatch
) -> None:
    monkeypatch.setattr(observe, "_is_trusted", lambda: False)
    assert cli.main(["snapshot", "--app", "TextEdit"]) == 1
    err = capsys.readouterr().err
    assert "permission_denied_accessibility" in err
    assert "doctor" in err


@pytest.mark.skipif(HAS_AX, reason="machine holds the AX grant; ungranted path unreachable")
def test_snapshot_without_ax_live_permission_path(capsys) -> None:
    """The real (unmocked) ungranted call degrades to the same structured error."""
    assert cli.main(["snapshot", "--app", "TextEdit"]) == 1
    assert "permission_denied_accessibility" in capsys.readouterr().err


def test_snapshot_prints_pruned_tree(capsys, monkeypatch, snapshot_builder) -> None:
    monkeypatch.setattr(observe, "snapshot", lambda scope, *, app: snapshot_builder())
    assert cli.main(["snapshot", "--app", "TextEdit"]) == 0
    out = capsys.readouterr().out
    assert "[snap-test-1]" in out
    assert 'e2 button "Save"' in out


def test_snapshot_defaults_to_frontmost_app(capsys, monkeypatch, snapshot_builder) -> None:
    seen: list[str] = []
    monkeypatch.setattr(safety, "frontmost_app", lambda: ("com.apple.TextEdit", 42))
    monkeypatch.setattr(
        observe, "snapshot", lambda scope, *, app: seen.append(app) or snapshot_builder()
    )
    assert cli.main(["snapshot"]) == 0
    assert seen == ["com.apple.TextEdit"]


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
    audit_files = list((home / ".computeruse" / "audit").glob("*.jsonl"))
    assert audit_files, "refused action was not audit-logged"


def test_run_once_granted_action_executes(capsys, home, fake_front, monkeypatch) -> None:
    safety.PermissionStore(home / ".computeruse" / "permissions.json").set_tier(
        FRONT, safety.Tier.FULL
    )
    pressed: list[str] = []
    monkeypatch.setattr(server.act, "key_chord", lambda chord, **kw: pressed.append(chord) or [])
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
