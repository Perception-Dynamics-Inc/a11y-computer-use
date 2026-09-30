"""One-step macOS grants: the OS dialog, the exact pane, the wait."""

from __future__ import annotations

import sys

import pytest

from a11y_computer_use import onboarding


@pytest.fixture(autouse=True)
def _fresh_prompt_memory(monkeypatch):
    monkeypatch.setattr(onboarding, "_prompted", set())


def test_request_off_macos_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "linux")
    out = onboarding.request("accessibility")
    assert out == {"needed": False, "granted": True, "kind": "accessibility", "host_app": None,
                   "steps": ["Accessibility grants are a macOS concept; nothing to do here."]}


def test_request_fires_dialog_opens_pane_and_waits_for_the_switch(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(onboarding, "host_app", lambda: "Codex")
    prompted, opened = [], []
    state = {"granted": False, "t": 0.0}

    def check(kind):  # granted on the third poll
        state["polls"] = state.get("polls", 0) + 1
        return state["polls"] >= 3

    out = onboarding.request(
        "accessibility", wait_s=90, poll_s=1,
        opener=lambda cmd: opened.append(cmd), is_granted=check,
        prompt=lambda kind: prompted.append(kind) or True,
        clock=lambda: state["t"], sleep=lambda s: state.__setitem__("t", state["t"] + s))
    assert prompted == ["accessibility"]
    assert opened == [["open", onboarding.doctor.ACCESSIBILITY_SETTINGS_URL]]
    assert out["granted"] and out["host_app"] == "Codex" and out["dialog_shown"] and out["pane_opened"]
    assert out["steps"] == ["Accessibility granted to Codex after 1 s."]


def test_request_times_out_with_the_steps_for_the_user(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(onboarding, "host_app", lambda: None)
    t = {"now": 0.0}
    out = onboarding.request(
        "screen_recording", wait_s=5, poll_s=1, opener=lambda cmd: None, is_granted=lambda k: False,
        prompt=lambda k: False, clock=lambda: t["now"], sleep=lambda s: t.__setitem__("now", t["now"] + s))
    assert not out["granted"] and out["waited_s"] == 5.0 and not out["dialog_shown"]
    assert "the app that launched a11y-computer-use" in out["steps"][1]
    assert out["steps"][2].startswith("Screen Recording only takes effect after")
    assert out["settings_url"] == onboarding.doctor.SCREEN_RECORDING_SETTINGS_URL


def test_request_already_granted_does_not_prompt(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(onboarding, "host_app", lambda: "Ghostty")
    out = onboarding.request("accessibility", is_granted=lambda k: True,
                             prompt=lambda k: pytest.fail("must not prompt"), opener=lambda c: pytest.fail("no pane"))
    assert out["granted"] and out["steps"] == ["Accessibility is already granted to Ghostty."]


def test_request_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        onboarding.request("microphone")


def test_first_hint_prompts_once_per_process(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setattr(onboarding, "host_app", lambda: "Codex")
    prompts, panes = [], []
    hint = onboarding.first_hint("accessibility", opener=lambda c: panes.append(c), prompt=lambda k: prompts.append(k) or True)
    assert hint.startswith("macOS is showing its Accessibility dialog") and "switch on 'Codex'" in hint
    assert "request_permission(kind='accessibility')" in hint
    again = onboarding.first_hint("accessibility", opener=lambda c: panes.append(c), prompt=lambda k: prompts.append(k) or True)
    assert again.startswith("Accessibility is not granted to 'Codex'.")
    assert prompts == ["accessibility"] and len(panes) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the real TCC state")
def test_granted_reads_the_real_grants() -> None:
    assert isinstance(onboarding.granted("accessibility"), bool)
    assert isinstance(onboarding.granted("screen_recording"), bool)


def _osascript(stdout="", returncode=0, stderr=""):
    import subprocess

    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    run.calls = calls
    return run


def test_native_confirm_reads_the_button(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.delenv("A11Y_COMPUTER_USE_NO_OS_PROMPT", raising=False)
    run = _osascript("button returned:Allow\n")  # the NSAlert helper gets this too: not JSON, so it falls through
    assert onboarding.native_confirm("t", 'Allow "X"?', runner=run) is True
    assert run.calls[0][1:3] == ["-m", "a11y_computer_use._alert"] and run.calls[-1][0] == "osascript"
    script = run.calls[-1][2]
    assert 'display dialog "Allow \\"X\\"?"' in script and 'default button "Don\'t Allow"' in script
    assert onboarding.native_confirm("t", "m", runner=_osascript("", 1, "execution error: User canceled. (-128)")) is False
    assert onboarding.native_confirm("t", "m", runner=_osascript("button returned:, gave up:true")) is False
    assert onboarding.native_confirm("t", "m", runner=_osascript("", 1, "osascript: no display")) is None


def test_native_confirm_is_none_off_macos_or_when_quiet(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "linux")
    assert onboarding.native_confirm("t", "m", runner=_osascript("button returned:Allow")) is None
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.setenv("A11Y_COMPUTER_USE_NO_OS_PROMPT", "1")
    assert onboarding.native_confirm("t", "m", runner=_osascript("button returned:Allow")) is None


def _helper(stdout="", returncode=0):
    import subprocess

    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    run.calls = calls
    return run


def test_native_confirm_uses_the_nsalert_helper_and_reads_its_json(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.delenv("A11Y_COMPUTER_USE_NO_OS_PROMPT", raising=False)
    run = _helper('{"button": "allow", "remember": true}\n')
    out = onboarding.native_confirm("Allow X?", "one line", details="the body", remember_label="Always", runner=run)
    assert out is True and onboarding.last_remember is True
    cmd = run.calls[0]
    assert cmd[1:3] == ["-m", "a11y_computer_use._alert"] and "--details" in cmd and "--remember" in cmd
    assert onboarding.native_confirm("t", "m", runner=_helper('{"button": "deny", "remember": false}')) is False
    assert onboarding.last_remember is False
    assert onboarding.native_confirm("t", "m", runner=_helper('{"button": "timeout", "remember": false}')) is False


def test_native_confirm_falls_back_to_osascript_when_the_helper_fails(monkeypatch) -> None:
    monkeypatch.setattr(onboarding.sys, "platform", "darwin")
    monkeypatch.delenv("A11Y_COMPUTER_USE_NO_OS_PROMPT", raising=False)
    import subprocess

    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "osascript":
            return subprocess.CompletedProcess(cmd, 0, "button returned:Allow", "")
        return subprocess.CompletedProcess(cmd, 1, "", "no display")

    assert onboarding.native_confirm("t", "m", details="d", runner=run) is True
    assert [c[0] for c in calls][-1] == "osascript" and any("a11y_computer_use._alert" in c for c in calls[0])


def test_settings_remember_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(onboarding, "SETTINGS_PATH", str(tmp_path / "cfg" / "settings.json"))
    assert onboarding.settings() == {}
    onboarding.remember("report_issue_always", True)
    assert onboarding.settings() == {"report_issue_always": True}

