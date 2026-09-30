"""Agents report defects and bottlenecks: composition, redaction, filing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from a11y_computer_use import reporting

ENV = {"a11y-computer-use": "0.3.2", "platform": "macos", "os": "macOS-15", "python": "3.13", "driver": "macos"}


def test_redact_strips_tokens_emails_and_home() -> None:
    home = str(Path.home())
    text = (f"token ghp_abcdefghijklmnopqrstuvwxyz0123 and github_pat_11ABCDEFGHIJKLMNOPQRSTUV "
            f"api_key=sk-live-1234567890abcdef Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6 mail me@example.com "
            f"path {home}/Desktop/file.png")
    out = reporting.redact(text)
    for leaked in ("ghp_", "github_pat_", "sk-live", "eyJhbGci", "me@example.com", home):
        assert leaked not in out, leaked
    assert "~/Desktop/file.png" in out


def test_compose_labels_and_environment_table() -> None:
    title, body, labels = reporting.compose("bottleneck", "  desktop_snapshot took 14 s  ",
                                            "call: desktop_snapshot(app=Figma)\nresult: ok after 14.2 s",
                                            tool="desktop_snapshot", env=ENV)
    assert title == "[bottleneck] desktop_snapshot took 14 s"
    assert labels == ["agent-report", "bottleneck"]
    assert "| a11y-computer-use | 0.3.2 |" in body and "| tool | `desktop_snapshot` |" in body
    assert body.startswith("call: desktop_snapshot")


@pytest.mark.parametrize("kind,title,body", [("nope", "t", "b"), ("bug", "", "b"), ("bug", "t", "  ")])
def test_compose_rejects_bad_input(kind, title, body) -> None:
    with pytest.raises(ValueError):
        reporting.compose(kind, title, body, env=ENV)


def test_new_issue_url_is_prefilled_and_bounded() -> None:
    url = reporting.new_issue_url("[bug] x", "y" * 20000, ["agent-report", "bug"])
    assert url.startswith(reporting.ISSUES_URL + "/new?labels=agent-report%2Cbug&title=%5Bbug%5D%20x&body=")
    assert len(url) <= reporting._URL_BUDGET + 100 and "truncated" in url


def _runner(auth_ok=True, create_ok=True, url="https://github.com/o/r/issues/7"):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(cmd, 0 if auth_ok else 1, "", "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return subprocess.CompletedProcess(cmd, 0 if create_ok else 1, url + "\n" if create_ok else "", "boom")
        raise AssertionError(cmd)

    run.calls = calls
    return run


def test_report_posts_only_with_gh_and_a_yes(monkeypatch) -> None:
    monkeypatch.setattr(reporting.shutil, "which", lambda name: "/usr/bin/gh")
    run = _runner()
    out = reporting.report("bug", "click crashed", "internal_error: click crashed: KeyError", tool="click",
                           confirm=lambda prompt: "click crashed" in prompt, env=ENV, runner=run)
    assert out == "posted: https://github.com/o/r/issues/7"
    create = [c for c in run.calls if c[:3] == ["gh", "issue", "create"]][0]
    assert create[create.index("-R") + 1] == reporting.REPO
    assert create.count("--label") == 2 and "bug" in create and "agent-report" in create

    out = reporting.report("bug", "t", "b", confirm=lambda prompt: False, env=ENV, runner=_runner())
    assert out.startswith("not posted: the user declined") and "/issues/new?" in out

    out = reporting.report("bug", "t", "b", confirm=None, env=ENV, runner=_runner())
    assert out.startswith("not posted: this host has no confirmation channel")

    out = reporting.report("bug", "t", "b", confirm=lambda p: True, env=ENV, runner=_runner(create_ok=False))
    assert out.startswith("not posted: gh issue create failed") and "boom" in out


def test_report_without_gh_returns_the_link(monkeypatch) -> None:
    monkeypatch.setattr(reporting.shutil, "which", lambda name: None)
    out = reporting.report("missing_capability", "no drag on Windows", "drag returns unsupported",
                           confirm=lambda p: True, env=ENV, runner=_runner())
    assert out.startswith("not posted: no signed-in GitHub CLI") and "labels=agent-report%2Cmissing-capability" in out


def test_slow_call_note_skips_waiting_tools(monkeypatch) -> None:
    monkeypatch.setattr(reporting, "SLOW_CALL_S", 10.0)
    assert reporting.slow_call_note("desktop_snapshot", 3.0) is None
    assert "report_issue(kind='bottleneck', tool='desktop_snapshot'" in reporting.slow_call_note("desktop_snapshot", 12.5)
    assert reporting.slow_call_note("wait_until", 500.0) is None
    monkeypatch.setattr(reporting, "SLOW_CALL_S", 0.0)
    assert reporting.slow_call_note("click", 99.0) is None


def test_internal_error_text_names_the_tool_and_redacts() -> None:
    text = reporting.internal_error_text("click", KeyError(str(Path.home()) + "/x"))
    assert text.startswith("internal_error: click crashed: KeyError") and "report_issue(kind='bug', tool='click'" in text
    assert str(Path.home()) not in text
