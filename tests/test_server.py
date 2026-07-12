"""Server tests: tool registry plus tool round-trips over the MCP in-memory
transport with mocked drivers.

No test here needs a TCC grant: `observe`/`act` driver calls are
monkeypatched at the module seam the server calls through, so these tests
exercise exactly the server's own responsibilities — registration, target
resolution, safety gating, audit logging, and structured-error rendering.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as client_session
from mcp.types import ElicitResult
from PIL import Image as PILImage

from computeruse import act, capture, observe, safety, server
from computeruse.schema import Bounds, ComputerUseError, Display, Element, ErrorCode
from tests.conftest import build_synthetic_snapshot

pytestmark = pytest.mark.anyio

APP = "com.apple.TextEdit"

EXPECTED_TOOLS = {
    "desktop_snapshot",
    "screenshot",
    "zoom",
    "click",
    "type",
    "key",
    "scroll",
    "drag",
    "wait_for",
    "app",
    "window",
    "clipboard",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> safety.PermissionStore:
    return safety.PermissionStore(tmp_path / "permissions.json")


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


@pytest.fixture
def mcp_server(store: safety.PermissionStore, audit_dir: Path):
    return server.build_server(store=store, audit=safety.AuditLog(audit_dir))


@pytest.fixture
def mocked_driver(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Replace the observe/act driver seams with recording fakes.

    ``observe.snapshot`` returns the synthetic tree (with ``ensure_trusted``
    and the app-to-bundle resolution stubbed, since neither the TCC grant nor
    a running TextEdit exists on test machines); ``observe.resolve_ref``
    resolves within it (no live re-observe); EVERY `act` entry point that
    posts CGEvents records its calls instead; the act-time hit-test reports
    "unknown owner" so the recheck stays neutral (its behavior has dedicated
    tests).
    """
    calls: dict[str, list] = {"click": [], "type": [], "key": [], "scroll": [], "drag": []}
    snap = build_synthetic_snapshot()
    monkeypatch.setattr(observe, "ensure_trusted", lambda: None)
    monkeypatch.setattr(observe, "snapshot", lambda scope, *, app: snap)
    monkeypatch.setattr(
        observe, "resolve_ref", lambda s, ref, *, live=None: s.element(ref)
    )
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, APP))
    monkeypatch.setattr(server, "_app_at_point", lambda point: None)
    monkeypatch.setattr(
        act, "click", lambda target, **kw: calls["click"].append((target, kw)) or []
    )
    monkeypatch.setattr(
        act, "type_text", lambda text, **kw: calls["type"].append(text) or []
    )
    monkeypatch.setattr(
        act, "key_chord", lambda chord, **kw: calls["key"].append(chord) or []
    )
    monkeypatch.setattr(
        act, "scroll", lambda target, **kw: calls["scroll"].append((target, kw)) or []
    )
    monkeypatch.setattr(
        act, "drag", lambda start, end, **kw: calls["drag"].append((start, end, kw)) or []
    )
    return calls


def tiny_png(width: int = 8, height: int = 4) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


async def call_tool(mcp_server, name: str, args: dict):
    async with client_session(mcp_server) as client:
        return await client.call_tool(name, args)


def audit_entries(audit_dir: Path) -> list[dict]:
    entries: list[dict] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        entries += [json.loads(line) for line in path.read_text().splitlines()]
    return entries


# --- registry ----------------------------------------------------------------


async def test_tool_registry_matches_plan_surface(mcp_server) -> None:
    async with client_session(mcp_server) as client:
        listed = (await client.list_tools()).tools
    assert {tool.name for tool in listed} == EXPECTED_TOOLS
    assert len(listed) == 12
    for tool in listed:
        assert tool.description, f"{tool.name} has no description"


async def test_click_description_states_ref_and_permission_contract(mcp_server) -> None:
    async with client_session(mcp_server) as client:
        listed = (await client.list_tools()).tools
    click = next(tool for tool in listed if tool.name == "click")
    assert "desktop_snapshot" in click.description  # where refs come from
    assert "needs_permission" in click.description  # the permission model


# --- round-trip: snapshot -> click through gate, driver, and audit -----------


async def test_snapshot_then_click_round_trip(
    mcp_server, mocked_driver, store, audit_dir
) -> None:
    store.set_tier(APP, safety.Tier.FULL)

    result = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    assert not result.isError
    text = result.content[0].text
    assert "[snap-test-1]" in text
    assert 'e2 button "Save" (click)' in text

    result = await call_tool(mcp_server, "click", {"ref": "e2"})
    assert not result.isError
    assert "clicked e2" in result.content[0].text

    (target, kwargs), = mocked_driver["click"]
    assert isinstance(target, Element)
    assert target.ref == "e2"
    assert kwargs == {"button": server.MouseButton.LEFT, "count": 1, "modifiers": ()}

    snap_entry, click_entry = audit_entries(audit_dir)  # observation is audited too
    assert snap_entry["action"] == "observeop"
    assert snap_entry["params"]["verb"] == "snapshot"
    assert snap_entry["app"] == APP
    assert snap_entry["result"] == "ok"
    assert click_entry["action"] == "click"
    assert click_entry["app"] == APP
    assert click_entry["result"] == "ok"
    assert click_entry["decision"]["verdict"] == "allow"


async def test_snapshot_without_grant_needs_permission_and_is_audited(
    mcp_server, mocked_driver, audit_dir
) -> None:
    # READ is a *tier*, not a freebie: an ungranted (or denied) app's tree —
    # text-field values included — must never reach the model silently.
    result = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    assert result.isError
    text = result.content[0].text
    assert "needs_permission" in text
    assert APP in text

    entry, = audit_entries(audit_dir)
    assert entry["action"] == "observeop"
    assert entry["result"] == "needs_permission"


async def test_click_without_grant_returns_needs_permission(
    mcp_server, mocked_driver, audit_dir, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    result = await call_tool(mcp_server, "click", {"x": 10, "y": 20, "display_id": 1})
    assert result.isError
    text = result.content[0].text
    assert "needs_permission" in text
    assert "com.test.front" in text
    assert mocked_driver["click"] == []  # never reached the driver

    entry, = audit_entries(audit_dir)
    assert entry["result"] == "needs_permission"
    assert entry["decision"]["verdict"] == "needs_permission"


async def test_type_at_click_tier_is_denied(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    store.set_tier("com.test.front", safety.Tier.CLICK)
    result = await call_tool(mcp_server, "type", {"text": "hello"})
    assert result.isError
    assert "deny" in result.content[0].text
    assert mocked_driver["type"] == []


async def test_type_gated_against_frontmost_app(
    mcp_server, mocked_driver, store, audit_dir, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    store.set_tier("com.test.front", safety.Tier.FULL)
    result = await call_tool(mcp_server, "type", {"text": "hello"})
    assert not result.isError
    assert result.content[0].text == "typed 5 characters"
    assert mocked_driver["type"] == ["hello"]
    entry, = audit_entries(audit_dir)
    assert entry["app"] == "com.test.front"
    assert entry["params"]["text"] == "hello"  # non-secure entries keep params


async def test_secure_field_driver_error_is_audited_and_redacted(
    mcp_server, store, audit_dir, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    store.set_tier("com.test.front", safety.Tier.FULL)

    def secure_type(text: str, **kw):
        raise ComputerUseError(
            ErrorCode.SECURE_FIELD, "secure event input is active", detail={}
        )

    monkeypatch.setattr(act, "type_text", secure_type)
    result = await call_tool(mcp_server, "type", {"text": "hunter2"})
    assert result.isError
    assert "secure_field" in result.content[0].text

    entry, = audit_entries(audit_dir)
    assert entry["result"] == "secure_field"
    assert entry["params"]["text"] == safety.REDACTED  # the secret never hits disk


async def test_wait_for_at_read_tier(mcp_server, mocked_driver, store) -> None:
    store.set_tier(APP, safety.Tier.READ)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    result = await call_tool(
        mcp_server, "wait_for", {"ref": "e2", "condition": "actionable", "timeout_s": 1.0}
    )
    assert not result.isError
    assert result.content[0].text == "e2 actionable: satisfied"


# --- structured errors --------------------------------------------------------


async def test_permission_error_surfaces_code_and_doctor_hint(
    mcp_server, monkeypatch
) -> None:
    def denied():
        raise ComputerUseError(
            ErrorCode.PERMISSION_DENIED_ACCESSIBILITY,
            "Accessibility permission is missing for this process",
        )

    # The missing-TCC error precedes app resolution and per-app gating: on an
    # ungranted machine the actionable answer is the doctor hint.
    monkeypatch.setattr(observe, "ensure_trusted", denied)
    result = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    assert result.isError
    text = result.content[0].text
    assert "permission_denied_accessibility" in text
    assert "doctor" in text


async def test_ref_before_any_snapshot_is_stale(mcp_server, mocked_driver) -> None:
    result = await call_tool(mcp_server, "click", {"ref": "e2"})
    assert result.isError
    text = result.content[0].text
    assert "stale_ref" in text
    assert "desktop_snapshot" in text


async def test_unknown_ref_in_current_snapshot_is_stale(
    mcp_server, mocked_driver, store
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    result = await call_tool(mcp_server, "click", {"ref": "e99"})
    assert result.isError
    text = result.content[0].text
    assert "stale_ref" in text
    assert "snapshot-scoped" in text


async def test_click_requires_ref_or_coordinates(mcp_server, mocked_driver) -> None:
    result = await call_tool(mcp_server, "click", {})
    assert result.isError
    assert "ref" in result.content[0].text


async def test_click_rejects_unknown_modifier_before_gating(
    mcp_server, mocked_driver, audit_dir
) -> None:
    result = await call_tool(
        mcp_server, "click", {"x": 1, "y": 1, "display_id": 1, "modifiers": ["super"]}
    )
    assert result.isError
    assert "unknown modifiers" in result.content[0].text
    assert audit_entries(audit_dir) == []  # fail-fast validation, not a gate event


# --- AX activation vs synthetic mouse (the non-intrusive-cursor contract) -------


async def test_click_prefers_ax_activation_and_skips_the_mouse(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    pressed: list[str] = []
    monkeypatch.setattr(observe, "press_element", lambda el: pressed.append(el.ref) or True)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})

    result = await call_tool(mcp_server, "click", {"ref": "e2"})
    assert not result.isError
    assert pressed == ["e2"]  # activated through the AX API
    assert mocked_driver["click"] == []  # no synthetic mouse events => cursor never moved


async def test_click_falls_back_to_mouse_when_ax_declines(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    monkeypatch.setattr(observe, "press_element", lambda el: False)  # e.g. no live handle
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})

    result = await call_tool(mcp_server, "click", {"ref": "e2"})
    assert not result.isError
    assert len(mocked_driver["click"]) == 1  # fell back to a synthetic mouse click


async def test_modified_and_multiclicks_never_use_ax(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    attempted: list[str] = []
    monkeypatch.setattr(observe, "press_element", lambda el: attempted.append(el.ref) or True)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})

    await call_tool(mcp_server, "click", {"ref": "e2", "button": "right"})
    await call_tool(mcp_server, "click", {"ref": "e2", "count": 2})
    await call_tool(mcp_server, "click", {"ref": "e2", "modifiers": ["cmd"]})

    assert attempted == []  # AX activation is only for plain left single-clicks
    assert len(mocked_driver["click"]) == 3  # right/double/modified all synthesize mouse events


# --- same-window recheck (decision -> injection race) ---------------------------


async def test_type_aborts_when_frontmost_changes_before_injection(
    mcp_server, mocked_driver, store, audit_dir, monkeypatch
) -> None:
    front_values = iter(["com.test.front", "com.other.bank"])
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: next(front_values))
    store.set_tier("com.test.front", safety.Tier.FULL)

    result = await call_tool(mcp_server, "type", {"text": "hello"})
    assert result.isError
    assert "focus_changed" in result.content[0].text
    assert mocked_driver["type"] == []  # aborted before any CGEvent

    entry, = audit_entries(audit_dir)
    assert entry["result"] == "focus_changed"


async def test_click_aborts_when_another_app_covers_the_target(
    mcp_server, mocked_driver, store, audit_dir, monkeypatch
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    monkeypatch.setattr(server, "_app_at_point", lambda point: "com.other.overlay")

    result = await call_tool(mcp_server, "click", {"ref": "e2"})
    assert result.isError
    assert "focus_changed" in result.content[0].text
    assert mocked_driver["click"] == []

    click_entry = audit_entries(audit_dir)[-1]
    assert click_entry["action"] == "click"
    assert click_entry["result"] == "focus_changed"


# --- screenshot / zoom over the wire ---------------------------------------------


async def test_screenshot_round_trip_returns_text_and_image(
    mcp_server, store, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    display = Display(display_id=1, width=8, height=4, scale=2.0, is_main=True)
    monkeypatch.setattr(
        capture,
        "screenshot",
        lambda display_id=None: capture.Screenshot(png=tiny_png(), display=display),
    )
    store.set_tier("com.test.front", safety.Tier.READ)

    result = await call_tool(mcp_server, "screenshot", {})
    assert not result.isError
    text_block, image_block = result.content
    assert "physical px" in text_block.text
    assert image_block.type == "image"
    assert image_block.mimeType == "image/png"


async def test_screenshot_without_grant_needs_permission(
    mcp_server, audit_dir, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    called = []
    monkeypatch.setattr(capture, "screenshot", lambda display_id=None: called.append(1))

    result = await call_tool(mcp_server, "screenshot", {})
    assert result.isError
    assert "needs_permission" in result.content[0].text
    assert called == []  # pixels never captured

    entry, = audit_entries(audit_dir)
    assert entry["action"] == "observeop"
    assert entry["params"]["verb"] == "screenshot"
    assert entry["result"] == "needs_permission"


async def test_zoom_round_trip_returns_text_and_image(
    mcp_server, store, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    monkeypatch.setattr(capture, "zoom_region", lambda region: tiny_png())
    store.set_tier("com.test.front", safety.Tier.READ)

    result = await call_tool(
        mcp_server, "zoom", {"display_id": 1, "x": 0, "y": 0, "width": 8, "height": 4}
    )
    assert not result.isError
    text_block, image_block = result.content
    assert "zoom of display 1" in text_block.text
    assert image_block.type == "image"


# --- app / window / clipboard adapters -----------------------------------------


async def test_app_list_returns_json_rows(mcp_server, store, monkeypatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    monkeypatch.setattr(
        server,
        "_list_apps",
        lambda: [{"bundle_id": APP, "name": "TextEdit", "pid": 42, "frontmost": True}],
    )
    store.set_tier("com.test.front", safety.Tier.READ)
    result = await call_tool(mcp_server, "app", {"action": "list"})
    assert not result.isError
    rows = json.loads(result.content[0].text)
    assert rows == [{"bundle_id": APP, "name": "TextEdit", "pid": 42, "frontmost": True}]


async def test_window_list_bounds_are_display_qualified_physical_pixels(
    mcp_server, store, monkeypatch
) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    monkeypatch.setattr(
        server,
        "_list_windows",
        lambda: [
            {
                "window_id": 7,
                "app": "TextEdit",
                "pid": 42,
                "title": "Untitled",
                "bounds": {"X": 100, "Y": 50, "Width": 800, "Height": 600},
            }
        ],
    )
    # 2x display: 100pt/50pt global points -> 200px/100px display-local.
    monkeypatch.setattr(
        observe,
        "project_global_rect",
        lambda position, size: Bounds(1, int(position[0] * 2), int(position[1] * 2), int(size[0] * 2), int(size[1] * 2)),
    )
    store.set_tier("com.test.front", safety.Tier.READ)

    result = await call_tool(mcp_server, "window", {"action": "list"})
    assert not result.isError
    row, = json.loads(result.content[0].text)
    assert row["bounds"] == {"display_id": 1, "x": 200, "y": 100, "width": 1600, "height": 1200}


async def test_clipboard_write_needs_full_tier(mcp_server, store, monkeypatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    store.set_tier("com.test.front", safety.Tier.READ)

    read = await call_tool(mcp_server, "clipboard", {"action": "read"})
    write = await call_tool(mcp_server, "clipboard", {"action": "write", "text": "hi"})

    assert not read.isError  # READ tier suffices for clipboard read
    assert write.isError
    assert "deny" in write.content[0].text


# --- confirmation gate: irreversible clicks over MCP elicitation (COM-10) ------


def _use_destructive_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point observe at a snapshot whose e2 is a "Delete" button.

    Overrides mocked_driver's snapshot/resolve_ref (later setattr wins), so the
    click on e2 trips `safety.confirmation_prompt`.
    """
    base = build_synthetic_snapshot()
    elements = tuple(
        dataclasses.replace(el, title="Delete") if el.ref == "e2" else el
        for el in base.elements
    )
    snap = dataclasses.replace(base, elements=elements)
    monkeypatch.setattr(observe, "snapshot", lambda scope, *, app: snap)
    monkeypatch.setattr(observe, "resolve_ref", lambda s, ref, *, live=None: s.element(ref))


async def _accept_elicitation(context, params):
    return ElicitResult(action="accept", content={})


async def _decline_elicitation(context, params):
    return ElicitResult(action="decline")


async def _snapshot_then_click_e2(mcp_server, elicitation_callback=None):
    """One session: desktop_snapshot (sets the epoch) then click e2."""
    async with client_session(
        mcp_server, elicitation_callback=elicitation_callback
    ) as client:
        await client.call_tool("desktop_snapshot", {"app": "TextEdit"})
        return await client.call_tool("click", {"ref": "e2"})


async def test_destructive_click_proceeds_when_confirmed(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    _use_destructive_snapshot(monkeypatch)
    store.set_tier(APP, safety.Tier.FULL)

    result = await _snapshot_then_click_e2(mcp_server, _accept_elicitation)
    assert not result.isError
    assert len(mocked_driver["click"]) == 1  # confirmed -> executed


async def test_destructive_click_blocked_when_declined(
    mcp_server, mocked_driver, store, audit_dir, monkeypatch
) -> None:
    _use_destructive_snapshot(monkeypatch)
    store.set_tier(APP, safety.Tier.FULL)

    result = await _snapshot_then_click_e2(mcp_server, _decline_elicitation)
    assert result.isError
    assert "confirmation_declined" in result.content[0].text
    assert mocked_driver["click"] == []  # never executed
    assert audit_entries(audit_dir)[-1]["result"] == "confirmation_declined"


async def test_destructive_click_fails_safe_without_elicitation(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    _use_destructive_snapshot(monkeypatch)
    store.set_tier(APP, safety.Tier.FULL)

    # No elicitation callback => the host cannot prompt => fail-safe block.
    result = await _snapshot_then_click_e2(mcp_server, None)
    assert result.isError
    assert "confirmation_declined" in result.content[0].text
    assert mocked_driver["click"] == []


async def test_destructive_click_gate_can_be_disabled(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    _use_destructive_snapshot(monkeypatch)
    monkeypatch.setattr(server, "CONFIRMATION_GATE", False)
    store.set_tier(APP, safety.Tier.FULL)

    result = await _snapshot_then_click_e2(mcp_server, None)  # no channel, but gate off
    assert not result.isError
    assert len(mocked_driver["click"]) == 1  # proceeds without confirmation


async def test_safe_click_never_triggers_confirmation(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    # Default synthetic e2 is "Save" (non-destructive): no elicitation even
    # though the host offers no channel.
    store.set_tier(APP, safety.Tier.FULL)
    result = await _snapshot_then_click_e2(mcp_server, None)
    assert not result.isError
    assert len(mocked_driver["click"]) == 1
