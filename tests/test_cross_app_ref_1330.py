"""#1330: a ref never acts in a different app than the one the agent means.

Refs restart in every snapshot. Tester's repro: Mousepad's ``e13`` clicked
after a Writer snapshot opened Writer's Form menu (via AX press, uncovered,
reported confirmed), and again after ``app(focus, mousepad)``.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import server
from tests.test_server import _two_app_drag_runtime


def _refused(rt, call, reason):
    with pytest.raises(server.ComputerUseError) as caught:
        call()
    err = caught.value
    assert err.code is server.ErrorCode.STALE_REF
    assert err.detail["reason"] == reason
    assert err.detail["outcome"] == "refused"
    return err


def _switch_refused(rt, call, focused, here):
    """An unrecorded switch: STALE_REF on desktop drivers; a driver that
    resolves apps itself (the fake, the browser) keeps its FOCUS_CHANGED
    contract. Either way it is raised before any ref resolution or input."""
    with pytest.raises(server.ComputerUseError) as caught:
        call()
    err = caught.value
    if rt._resolves_apps():
        assert err.code is server.ErrorCode.FOCUS_CHANGED
        assert err.detail == {"gated_app": here, "frontmost_app": focused}
    else:
        assert err.code is server.ErrorCode.STALE_REF
        assert err.detail["reason"] == "other_app_focused"
        assert err.detail["focused_app"] == focused and err.detail["current_app"] == here
    return err


@pytest.mark.parametrize("call", [
    lambda rt: rt.click(ref="e3"),
    lambda rt: rt.type_text("x", ref="e3"),
])
def test_ref_issued_by_both_apps_is_refused_although_uncovered(tmp_path, monkeypatch, call) -> None:
    """Step 4: the other app's snapshot also issued the ref; nothing covers its point."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    err = _refused(rt, lambda: call(rt), "other_snapshot")
    assert err.detail["issued_by_app"] == "com.target" and err.detail["current_app"] == "com.test"
    assert calls == []


def test_ref_after_focusing_another_app_is_refused(tmp_path, monkeypatch) -> None:
    """Step 7: re-observed source, then app(focus) on the other app, then its old ref."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")  # clears the #836 ambiguity, as Tester's step 5 did
    rt.driver.activate_app = lambda name: None
    assert "focused com.target" in rt.app("focus", "com.target")
    for call in (lambda: rt.click(ref="e3"), lambda: rt.type_text("x", ref="e3"),
                 lambda: rt.drag(start_ref="e3", end_x=5, end_y=5)):
        err = _refused(rt, call, "other_app_focused")
        assert err.detail["focused_app"] == "com.target" and err.detail["current_app"] == "com.test"
    assert calls == []


def test_refusal_is_audited(tmp_path, monkeypatch) -> None:
    rt, _calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.target")
    seen = []
    monkeypatch.setattr(rt, "_audit_stale", lambda kind, ref, err: seen.append((kind, ref, err.detail["reason"])))
    _refused(rt, lambda: rt.click(ref="e3"), "other_app_focused")
    assert seen == [("click", "e3", "other_app_focused")]


def test_focusing_the_snapshots_own_app_keeps_its_refs(tmp_path, monkeypatch) -> None:
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.test")
    rt.drag(start_ref="e3", end_x=5, end_y=5)
    assert len(calls) == 1 and calls[0][0].title == "dnd-file-Q9.txt"


def test_a_snapshot_after_the_focus_rebinds_refs(tmp_path, monkeypatch) -> None:
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.target")
    rt.desktop_snapshot("com.test")  # the agent observed again: these refs are meant
    rt.desktop_snapshot("com.test")
    rt.drag(start_ref="e3", end_x=5, end_y=5)
    assert len(calls) == 1 and calls[0][0].title == "dnd-file-Q9.txt"


# --- R&D review of #1331 (gaps 1-4; spec T1-T7) ---------------------------


def _front(rt, app_id, pid):
    rt.driver.frontmost_app = lambda: (app_id, pid)


def test_t1_control_without_the_focus_switch_the_ref_acts(tmp_path, monkeypatch) -> None:
    """T1 control: the same steps without app(focus B) act in A."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt.drag(start_ref="e3", end_x=5, end_y=5)
    assert len(calls) == 1 and calls[0][0].title == "dnd-file-Q9.txt"


@pytest.mark.parametrize("focus", [False, True])
def test_t2_scroll_to_find_with_an_old_ref_is_refused(tmp_path, monkeypatch, focus) -> None:
    """Gap 1: scroll_to_find anchors through _anchor_audited, not _resolve."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    if focus:
        rt.desktop_snapshot("com.test")
        rt.driver.activate_app = lambda name: None
        rt.app("focus", "com.target")
    reason = "other_app_focused" if focus else "other_snapshot"
    _refused(rt, lambda: rt.scroll_to_find("com.test", text="zz-none", ref="e3"), reason)
    assert calls == []


@pytest.mark.parametrize("call", [
    lambda rt: rt.click(ref="e3"),
    lambda rt: rt.type_text("x", ref="e3"),
    lambda rt: rt.drag(start_ref="e3", end_x=5, end_y=5),
    lambda rt: rt.scroll_to_find("com.test", text="zz-none", ref="e3"),
])
def test_t3_an_unrecorded_switch_to_another_app_refuses_old_refs(tmp_path, monkeypatch, call) -> None:
    """Gap 2: window(focus), alt+tab, a launch, a coordinate click or the app
    raising itself all just change the frontmost app; none calls app(focus)."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    _front(rt, "com.target", 2)
    _switch_refused(rt, lambda: call(rt), "com.target", "com.test")
    assert calls == []


def test_t3_window_focus_on_another_app_refuses_old_refs(tmp_path, monkeypatch) -> None:
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    monkeypatch.setattr(rt, "_front_identity", lambda: ("com.target", 2))
    _switch_refused(rt, lambda: rt.click(ref="e3"), "com.target", "com.test")
    assert calls == []


def test_t3_control_frontmost_back_on_the_snapshots_app_acts(tmp_path, monkeypatch) -> None:
    """Switching away and back again, or to another window of the same
    process, leaves the refs meant for that app."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    _front(rt, "com.target", 2)
    _switch_refused(rt, lambda: rt.click(ref="e3"), "com.target", "com.test")
    _front(rt, "com.test", 1)
    rt.drag(start_ref="e3", end_x=5, end_y=5)
    assert len(calls) == 1


def test_background_snapshot_with_an_unchanged_frontmost_app_still_acts(tmp_path, monkeypatch) -> None:
    """Acting on a background app's fresh snapshot is not a focus change."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    _front(rt, "org.terminal", 9)
    rt.desktop_snapshot("com.test")
    # The fake's own frontmost recheck would stop the stroke; the #1330 guard
    # is what this asserts lets the ref through.
    assert rt._refuse_cross_app_ref("e3", "click") is None


def test_t4_app_less_snapshot_refs_are_refused_after_a_focus(tmp_path, monkeypatch) -> None:
    """Gap 3: a snapshot with no app (display scope or merged) used to skip
    the guard; it cannot show the ref belongs to the focused app."""
    import dataclasses

    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt._current = dataclasses.replace(rt._current, app=None, pid=None)
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.target")
    err = _refused(rt, lambda: rt.click(ref="e3"), "other_app_focused")
    assert err.detail["current_app"] is None and err.detail["focused_app"] == "com.target"
    assert calls == []


def test_t4_app_less_snapshot_refs_are_refused_after_an_unrecorded_switch(tmp_path, monkeypatch) -> None:
    import dataclasses

    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt._current = dataclasses.replace(rt._current, app=None, pid=None)
    _front(rt, "com.target", 2)
    _switch_refused(rt, lambda: rt.click(ref="e3"), "com.target", None)
    assert calls == []


def test_t5_interpreter_hosted_app_is_matched_by_pid_not_refused(tmp_path, monkeypatch) -> None:
    """Gap 4: the frontmost id of a python-hosted app is ``python3``, not the
    name its snapshot carries; the owner pid still ties it to the snapshot."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    _front(rt, "org.terminal", 9)
    rt.desktop_snapshot("com.test")  # pid 1
    _front(rt, "python3", 1)  # the agent clicked the app's window at x,y
    assert rt._refuse_cross_app_ref("e3", "click") is None
    _front(rt, "python3", 7)  # another python app is not the snapshot's
    _switch_refused(rt, lambda: rt._refuse_cross_app_ref("e3", "click"), "python3", "com.test")
    assert calls == []


def test_t5_app_focus_through_the_interpreter_gate_key_keeps_own_refs(tmp_path, monkeypatch) -> None:
    """Gap 4: app(focus "com.test") gated as ``python3`` (#1296) is still the
    snapshot's app; the requested name is part of the focus intent."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt.store.set_tier("python3", server.safety.Tier.CLICK)
    real = rt._resolve_app
    monkeypatch.setattr(rt, "_resolve_app", lambda name: (real(name)[0], "python3"))
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.test")
    assert rt._focus_intent == "python3"
    rt.drag(start_ref="e3", end_x=5, end_y=5)
    assert len(calls) == 1


def test_t6_drag_source_ref_to_other_apps_point_keeps_its_exemption(tmp_path, monkeypatch) -> None:
    """T6: the #836 route (source ref + the other app's x,y) still runs."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.drag(start_ref="e3", end_x=700, end_y=68)
    assert len(calls) == 1 and calls[0][0].title == "dnd-file-Q9.txt"


def test_t7_snapshot_of_the_focused_app_rebinds_and_acts(tmp_path, monkeypatch) -> None:
    """T7: find(A), app(focus B), find(B), act on B's new ref."""
    rt, calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.target")
    _front(rt, "com.target", 1)
    rt.desktop_snapshot("com.target")
    rt.desktop_snapshot("com.target")
    rt.drag(start_ref="e3", end_x=5, end_y=5)
    assert len(calls) == 1 and calls[0][0].title == "drop zone"


# --- Tester retest of 61da4949: false refusals -------------------------------


def test_a_ref_the_current_apps_fresh_find_returned_is_not_ambiguous(tmp_path, monkeypatch) -> None:
    """A8/V1/V2: B snapshot, then a find in A that returns e3. e3 is A's."""
    rt, _calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.target")
    rt._current = rt._note_issued(rt.driver.snapshot(None, "com.test"), {"e3"})
    assert rt._other_app_snapshot("e3") is None
    assert rt._refuse_cross_app_ref("e3", "click") is None
    # A4 stays refused: e2 was only shown by the other app's snapshot.
    assert rt._other_app_snapshot("e2") is not None
    with pytest.raises(server.ComputerUseError) as caught:
        rt._refuse_cross_app_ref("e2", "click")
    assert caught.value.detail["reason"] == "other_snapshot"


def test_a_second_find_of_the_same_app_adds_its_refs(tmp_path, monkeypatch) -> None:
    rt, _calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.target")
    snap = rt.driver.snapshot(None, "com.test")
    rt._current = rt._note_issued(snap, {"e3"})
    rt._current = rt._note_issued(snap, {"e1"})
    assert rt._issued_refs == {"e1", "e3"}


def test_window_raise_back_to_the_snapshots_app_keeps_its_refs(tmp_path, monkeypatch) -> None:
    """Tester: snapshot A, app focus terminal, window raise A, set_value A ref."""
    rt, _calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.target")
    _refused(rt, lambda: rt._refuse_cross_app_ref("e3", "typetext"), "other_app_focused")
    rt._note_window_focus("com.test")
    assert rt._refuse_cross_app_ref("e3", "typetext") is None
    # Raising another app's window refuses them again (gap 2).
    _front(rt, "com.target", 2)
    rt._note_window_focus("com.target")
    err = _refused(rt, lambda: rt._refuse_cross_app_ref("e3", "typetext"), "other_app_focused")
    assert "the text input was not sent" in str(err)


def test_window_raise_back_to_an_interpreter_hosted_app_matches_by_pid(tmp_path, monkeypatch) -> None:
    rt, _calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")  # pid 1
    rt.driver.activate_app = lambda name: None
    rt.app("focus", "com.target")
    _front(rt, "python3", 1)
    rt._note_window_focus("python3")
    assert rt._refuse_cross_app_ref("e3", "click") is None


def test_window_tool_raise_and_focus_update_the_tracked_app(tmp_path, monkeypatch) -> None:
    rt, _calls, _target = _two_app_drag_runtime(tmp_path, monkeypatch)
    rt.desktop_snapshot("com.test")
    seen = []
    monkeypatch.setattr(rt, "_note_window_focus", seen.append)
    monkeypatch.setattr(rt.driver, "window_owner", lambda wid: "com.test", raising=False)
    for verb in ("raise", "focus"):
        monkeypatch.setattr(rt.driver, f"{verb}_window", lambda wid: None, raising=False)
        monkeypatch.setattr(rt, "_window_verdict", lambda *a, **k: ("confirmed", ""), raising=False)
        try:
            rt.window(verb, 7)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"window {verb}: {exc!r}")
    assert seen == ["com.test", "com.test"]
