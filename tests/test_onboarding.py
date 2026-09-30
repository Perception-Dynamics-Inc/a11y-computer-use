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
