"""Compound scrolling must retain the same safety checks as a single action."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from a11y_computer_use import observe, safety, server
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, Scope, Snapshot
from tests.conftest import build_synthetic_snapshot


def snapshot(*, app: str = "test-app", x: int = 10, secure: bool = False) -> Snapshot:
    template = build_synthetic_snapshot()
    element = Element(
        ref="e1", role="AXScrollArea", title="List", value=None,
        bounds=Bounds(0, x, 10, 200, 300), snapshot_id=f"s-{x}", secure=secure,
    )
    return dataclasses.replace(template, snapshot_id=f"s-{x}", app=app, elements=(element,))


class ScrollDriver:
    resolves_apps = True

    def __init__(self) -> None:
        self.frontmost = "test-app"
        self.snapshots = [snapshot()]
        self.scrolls: list[Element] = []
        self.resolutions: list[Snapshot] = []
        self.after_scroll = lambda: None

    def ensure_trusted(self) -> None:
        pass

    def frontmost_app(self) -> tuple[str, int]:
        return self.frontmost, 1

    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        return self.snapshots[min(len(self.scrolls), len(self.snapshots) - 1)]

    def resolve_ref(self, original: Snapshot, ref: str, *, live: Snapshot) -> Element:
        self.resolutions.append(live)
        return observe.rematch_ref(original, ref, live)

    def scroll(self, target: Element, *, dy: int) -> None:
        self.scrolls.append(target)
        self.after_scroll()


@pytest.fixture
def runtime(tmp_path: Path):
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("test-app", safety.Tier.FULL)
    with server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=ScrollDriver(),
    ) as rt:
        rt._current = snapshot()
        yield rt


def audit_rows(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for path in (tmp_path / "audit").glob("*.jsonl")
            for line in path.read_text().splitlines()]


def test_pinned_scroll_ref_cannot_borrow_another_apps_grant(runtime, tmp_path) -> None:
    runtime._current = snapshot(app="ungranted-app")
    with pytest.raises(ComputerUseError) as refused:
        runtime.scroll_to_find("test-app", text="absent", ref="e1")
    assert refused.value.code is ErrorCode.STALE_REF
    assert refused.value.detail["ref_app"] == "ungranted-app"
    assert runtime.driver.scrolls == []
    assert runtime.driver.resolutions == []
    assert audit_rows(tmp_path)[-1]["result"] == "stale_ref"


def test_each_scroll_resolves_the_pinned_container_against_fresh_geometry(runtime) -> None:
    runtime.driver.snapshots = [snapshot(x=20), snapshot(x=30), snapshot(x=40)]
    assert "not found" in runtime.scroll_to_find("test-app", text="absent", ref="e1", max_scrolls=2)
    assert [element.bounds.x for element in runtime.driver.scrolls] == [20, 30]
    assert runtime.driver.resolutions == runtime.driver.snapshots[:2]


def test_missing_pinned_container_never_scrolls_at_old_coordinates(runtime) -> None:
    runtime.driver.snapshots = [dataclasses.replace(snapshot(), elements=())]
    with pytest.raises(ComputerUseError) as refused:
        runtime.scroll_to_find("test-app", text="absent", ref="e1")
    assert refused.value.code is ErrorCode.STALE_REF
    assert runtime.driver.scrolls == []


def test_scroll_rechecks_focus_after_each_observation(runtime, tmp_path) -> None:
    runtime.driver.after_scroll = lambda: setattr(runtime.driver, "frontmost", "other-app")
    with pytest.raises(ComputerUseError) as refused:
        runtime.scroll_to_find("test-app", text="absent", max_scrolls=2)
    assert refused.value.code is ErrorCode.FOCUS_CHANGED
    assert len(runtime.driver.scrolls) == 1
    scroll_rows = [row for row in audit_rows(tmp_path) if row["action"] == "scroll"]
    assert [row["result"] for row in scroll_rows] == ["ok", "focus_changed"]


def test_scroll_stops_when_permission_is_revoked_mid_search(runtime, tmp_path) -> None:
    runtime.driver.after_scroll = lambda: runtime.store.set_tier("test-app", safety.Tier.READ)
    with pytest.raises(server.ActionRefused):
        runtime.scroll_to_find("test-app", text="absent", max_scrolls=2)
    assert len(runtime.driver.scrolls) == 1
    scroll_rows = [row for row in audit_rows(tmp_path) if row["action"] == "scroll"]
    assert [row["result"] for row in scroll_rows] == ["ok", "deny"]


@pytest.mark.parametrize("pinned", [False, True])
def test_secure_scroll_target_is_refused_before_pointer_input(runtime, pinned: bool) -> None:
    runtime.driver.snapshots = [snapshot(secure=True)]
    with pytest.raises(ComputerUseError) as refused:
        runtime.scroll_to_find("test-app", text="absent", ref="e1" if pinned else None)
    assert refused.value.code is ErrorCode.SECURE_FIELD
    assert runtime.driver.scrolls == []


def test_permission_revoked_during_confirmation_prevents_injection(runtime, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server, "CONFIRMATION_GATE", True)
    clicked = []
    monkeypatch.setattr(safety, "confirmation_prompt", lambda action, app: "Confirm?")

    def confirm(_prompt: str) -> bool:
        runtime.store.set_tier("test-app", safety.Tier.READ)
        return True

    action = server.Click(target=snapshot().elements[0])
    with pytest.raises(server.ActionRefused):
        runtime._run_gated(action, "test-app", lambda: clicked.append(True), confirm=confirm)
    assert clicked == []
    assert [row["result"] for row in audit_rows(tmp_path)] == ["deny"]
