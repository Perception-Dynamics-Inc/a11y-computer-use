"""#1312: a deny on an interpreter-hosted app's requested name beats a python3 grant.

Hermetic: no display, no AT-SPI. ``_running_app`` stands in for the Linux
lookup, which returns the requested name before the window maps and the
interpreter (``python3``) after.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.schema import ObserveOp, ObserveVerb


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr(server.sys, "platform", "linux")
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("python3", safety.Tier.FULL)
    store.set_tier("cuicon1296", safety.Tier.FULL)
    store.add_deny("cuicon1296")
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.store = store
    runtime._current = None
    runtime._resolves_apps = lambda: False
    return runtime


def _allowed(runtime, key: str) -> safety.Decision:
    return safety.check_action(ObserveOp(verb=ObserveVerb.SNAPSHOT, app=key), key, store=runtime.store)


def _phase(monkeypatch, key: str) -> None:
    monkeypatch.setattr(server, "_running_app", lambda ident: (None, key))


def test_deny_before_the_window_maps(rt, monkeypatch) -> None:
    _phase(monkeypatch, "cuicon1296")  # only the AT-SPI name is known yet
    key = rt._resolve_app("cuicon1296")[1]
    decision = _allowed(rt, key)
    assert not decision.allowed and "deny list" in decision.reason


def test_deny_after_the_window_maps_beats_the_python3_grant(rt, monkeypatch) -> None:
    _phase(monkeypatch, "python3")  # the mapped window resolves to its interpreter
    key = rt._resolve_app("cuicon1296")[1]
    assert key == "cuicon1296"
    decision = _allowed(rt, key)
    assert not decision.allowed and "cuicon1296 is on the deny list" in decision.reason


def test_without_the_deny_the_python3_key_is_kept(rt, monkeypatch) -> None:
    rt.store = safety.PermissionStore(rt.store.path.with_name("nodeny.json"))
    rt.store.set_tier("python3", safety.Tier.FULL)
    _phase(monkeypatch, "python3")
    assert rt._resolve_app("cuicon1296")[1] == "python3"


def test_refs_of_a_python3_labelled_snapshot_gate_on_the_denied_name(rt) -> None:
    """A deny that lands after the snapshot still stops its ref actions."""
    rt._current = SimpleNamespace(app="python3", snapshot_id="snap-1")
    rt._requested_names = {"snap-1": "cuicon1296"}
    assert rt._gate_app("python3") == "cuicon1296"
    assert not _allowed(rt, rt._gate_app("python3")).allowed
    rt._current = SimpleNamespace(app="python3", snapshot_id="snap-2")  # another app's snapshot
    assert rt._gate_app("python3") == "python3"


def test_other_platforms_are_unchanged(rt, monkeypatch) -> None:
    monkeypatch.setattr(server.sys, "platform", "darwin")
    _phase(monkeypatch, "python3")
    assert rt._resolve_app("cuicon1296")[1] == "python3"
