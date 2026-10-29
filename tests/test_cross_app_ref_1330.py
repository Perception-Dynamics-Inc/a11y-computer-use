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
