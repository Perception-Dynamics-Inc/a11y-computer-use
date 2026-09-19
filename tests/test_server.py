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

import sys

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as client_session
from mcp.types import ElicitResult
from PIL import Image as PILImage

from a11y_computer_use import act, capture, observe, safety, server
from a11y_computer_use.schema import Bounds, ComputerUseError, Display, Element, ErrorCode, Point, Scope, Snapshot
from tests.conftest import build_synthetic_snapshot

pytestmark = pytest.mark.anyio

APP = "com.apple.TextEdit"

EXPECTED_TOOLS = {
    "desktop_snapshot",
    "find",
    "screenshot",
    "zoom",
    "screen_text",
    "click",
    "type",
    "key",
    "scroll",
    "drag",
    "wait_for",
    "act",
    "set_value",
    "scroll_to_find",
    "app",
    "window",
    "clipboard",
    "menu",
    "file_dialog",
    "notes",
    "wait_until",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _macos_driver_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module mocks the macOS native seams (``observe.snapshot``,
    ``act.click`` and friends, ``server._frontmost_bundle``), so the Runtime
    under test must be built on the macOS driver on every OS. The macOS driver
    imports without pyobjc because act/capture/observe are import-safe off
    macOS; only its native calls need pyobjc, and those are exactly what the
    fixtures replace. Tests that build a Runtime with an explicit ``driver=``
    are unaffected."""
    monkeypatch.setenv("A11Y_COMPUTER_USE_DRIVER", "macos")


@pytest.fixture
def store(tmp_path: Path) -> safety.PermissionStore:
    return safety.PermissionStore(tmp_path / "permissions.json")


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


@pytest.fixture
def mcp_server(store: safety.PermissionStore, audit_dir: Path):
    # These tests reconnect the in-memory transport between tool calls. Keep
    # the Runtime caller-owned across those individual server lifespans.
    with server.Runtime(store=store, audit=safety.AuditLog(audit_dir)) as runtime:
        yield server.build_server(runtime=runtime)


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
    assert len(listed) == len(EXPECTED_TOOLS)
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
    # the macOS driver delegates to act.click with the full explicit signature
    assert kwargs == {
        "button": server.MouseButton.LEFT,
        "count": 1,
        "modifiers": (),
        "pre_check": None,
        "dry_run": False,
    }

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


async def test_screenshot_format_jpeg_sends_a_jpeg_and_rejects_other_formats(
    mcp_server, store, monkeypatch
) -> None:
    import base64

    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    display = Display(display_id=1, width=8, height=4, scale=2.0, is_main=True)
    monkeypatch.setattr(
        capture, "screenshot",
        lambda display_id=None: capture.Screenshot(png=tiny_png(), display=display),
    )
    store.set_tier("com.test.front", safety.Tier.READ)

    result = await call_tool(mcp_server, "screenshot", {"format": "jpeg", "quality": 70})
    assert not result.isError
    image_block = result.content[1]
    assert image_block.mimeType == "image/jpeg"
    assert base64.b64decode(image_block.data)[:3] == b"\xff\xd8\xff"

    result = await call_tool(mcp_server, "screenshot", {"format": "gif"})
    assert result.isError and "format" in result.content[0].text


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


async def test_app_list_on_a_fresh_desktop_keys_on_a_trusted_running_app(mcp_server, store, monkeypatch) -> None:
    """Nothing focused: the frontmost 'app' is the desktop shell, which nobody
    grants. The list (identities only) is then gated against a running app the
    human already trusted; with no grant anywhere it still refuses."""
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "nemo-desktop")
    rows_ = [{"bundle_id": "nemo-desktop", "name": "nemo-desktop", "pid": 1, "frontmost": True},
             {"bundle_id": APP, "name": "TextEdit", "pid": 42, "frontmost": False}]
    monkeypatch.setattr(server, "_list_apps", lambda: rows_)
    result = await call_tool(mcp_server, "app", {"action": "list"})
    assert result.isError and "needs_permission" in result.content[0].text  # no grant anywhere
    store.set_tier(APP, safety.Tier.READ)
    result = await call_tool(mcp_server, "app", {"action": "list"})
    assert not result.isError
    assert json.loads(result.content[0].text) == rows_


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
    # The tier decision is under test, not the pasteboard: keep the read off
    # the real clipboard (xclip needs a DISPLAY on Linux; NSPasteboard is macOS-only).
    monkeypatch.setattr(server, "_read_clipboard", lambda: "clipboard text")
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


# --- scroll: AX reveal (cursor-free) vs synthetic wheel ------------------------


async def test_scroll_into_view_uses_ax_and_skips_the_wheel(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    revealed: list[str] = []
    monkeypatch.setattr(observe, "scroll_into_view", lambda el: revealed.append(el.ref) or True)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})

    result = await call_tool(mcp_server, "scroll", {"ref": "e2", "into_view": True})
    assert not result.isError
    assert "into view" in result.content[0].text
    assert revealed == ["e2"]  # revealed via AX
    assert mocked_driver["scroll"] == []  # no synthetic wheel => cursor never moved


async def test_scroll_delta_uses_the_wheel(mcp_server, mocked_driver, store) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})

    result = await call_tool(mcp_server, "scroll", {"ref": "e2", "dy": 3})
    assert not result.isError
    assert len(mocked_driver["scroll"]) == 1  # delta scroll -> synthetic wheel path


# --- a11y->vision auto-handoff signal (COM-12) --------------------------------


def _empty_snapshot() -> Snapshot:
    """A custom-drawn app's shell: a window with no actionable refs."""
    return Snapshot(
        snapshot_id="snap-empty",
        scope=Scope.WINDOW,
        app=APP,
        pid=1,
        created_at=0.0,
        displays=(Display(display_id=1, width=2880, height=1800, scale=2.0, is_main=True),),
        elements=(
            Element(
                ref="e1",
                role="AXWindow",
                title="Telegram",
                value=None,
                bounds=Bounds(1, 0, 0, 100, 100),
                snapshot_id="snap-empty",
            ),
        ),
    )


async def test_snapshot_with_no_refs_appends_vision_handoff(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    monkeypatch.setattr(observe, "snapshot", lambda scope, *, app: _empty_snapshot())

    result = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    assert not result.isError
    text = result.content[0].text
    assert "no interactive elements" in text  # the handoff signal fired
    assert "screenshot" in text  # points the agent at the vision path


async def test_snapshot_with_refs_has_no_handoff(mcp_server, mocked_driver, store) -> None:
    # the default synthetic snapshot has actionable refs (Save button, text area)
    store.set_tier(APP, safety.Tier.FULL)
    result = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    assert not result.isError
    assert "no interactive elements" not in result.content[0].text


def test_act_batch_dispatch_stop_and_errors() -> None:
    """Batched act: runs steps in order, stops at the first failure, and reports
    unknown steps / empty input — without needing a live driver."""
    import json as _json

    import pytest

    from a11y_computer_use import server
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    rt = server.Runtime.__new__(server.Runtime)  # bare instance; stub the dispatch targets
    calls: list = []
    rt.type_text = lambda text: (calls.append(("type", text)), f"typed {text}")[1]
    rt.key = lambda chord: (calls.append(("key", chord)), f"pressed {chord}")[1]

    out = _json.loads(rt.act_batch([{"do": "type", "text": "hi"}, {"do": "key", "chord": "enter"}]))
    assert [s["ok"] for s in out] == [True, True]
    assert calls == [("type", "hi"), ("key", "enter")]

    def boom(_text):
        raise ComputerUseError(ErrorCode.STALE_REF, "e1 no longer resolves")

    rt.type_text = boom
    out2 = _json.loads(rt.act_batch([{"do": "type", "text": "x"}, {"do": "key", "chord": "enter"}]))
    assert out2[0]["ok"] is False and len(out2) == 1  # stopped before the key step
    assert "stale_ref" in out2[0]["error"]

    out3 = _json.loads(rt.act_batch([{"do": "frobnicate"}]))
    assert out3[0]["ok"] is False and "unknown step" in out3[0]["error"]

    with pytest.raises(ValueError):
        rt.act_batch([])


def test_effect_receipt_appends_post_action_diff() -> None:
    """verify=true turns the audit-backed re-observe into an Effect Receipt: the
    single post-action snapshot diff, so the agent confirms what changed without a
    separate desktop_snapshot round-trip. Off by default (backward compat)."""
    import json as _json

    from a11y_computer_use import server
    from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot

    def snap(sid: str, extra: bool) -> Snapshot:
        els = [Element(ref="e1", role="AXButton", title="Save", value=None,
                       bounds=Bounds(0, 0, 0, 80, 30), snapshot_id=sid, clickable=True)]
        if extra:  # a new element appears — the visible effect of the action
            els.append(Element(ref="e2", role="AXStaticText", title="Saved ✓",
                               value=None, bounds=Bounds(0, 0, 40, 80, 60), snapshot_id=sid))
        return Snapshot(snapshot_id=sid, scope=Scope.WINDOW, app="com.test", pid=1,
                        created_at=0.0, displays=(Display(0, 800, 600, 1.0, True),),
                        elements=tuple(els))

    pre, post = snap("s0", False), snap("s1", True)

    rt = server.Runtime.__new__(server.Runtime)
    rt._current = pre
    rt.type_text = lambda text: f"typed {text}"
    rt.driver = type("_D", (), {"snapshot": lambda self, scope, app: post})()

    # _effect_after (the helper click() uses) returns the bare diff and advances _current.
    effect = rt._effect_after(pre)
    assert "Saved" in effect and not effect.startswith("\n")  # bare diff, callers format
    assert rt._current is post

    # act_batch(verify=True): one net diff for the whole batch, steps preserved.
    rt._current = pre
    out = _json.loads(rt.act_batch([{"do": "type", "text": "hi"}], verify=True))
    assert out["steps"][0]["ok"] is True
    assert "Saved" in out["effect"] and "effect:" not in out["effect"]  # unprefixed inside JSON

    # Default stays a bare list — existing callers unchanged.
    rt._current = pre
    assert isinstance(_json.loads(rt.act_batch([{"do": "type", "text": "hi"}])), list)

    # No prior snapshot → empty receipt, never an error.
    rt._current = None
    assert rt._effect_after(None) == ""


def test_set_value_prefers_driver_then_falls_back_and_refuses_secure() -> None:
    """set_value: one a11y op via the driver; focus+type fallback when the app
    can't set a value; secure fields refused."""
    from a11y_computer_use import server
    from a11y_computer_use.schema import Bounds, Element, ErrorCode

    rt = server.Runtime.__new__(server.Runtime)
    el = Element(ref="e1", role="AXTextField", title="Name", value="",
                 bounds=Bounds(0, 0, 0, 10, 10), snapshot_id="s")
    snap = type("S", (), {"app": "com.test"})()
    rt._anchor = lambda ref: (snap, el)
    rt._run_gated = lambda action, app, execute, **kw: execute()
    calls = {"set": [], "press": [], "type": []}

    class _D:
        set_ok = True

        def resolve_ref(self, s, ref):
            return rt._anchor(ref)[1]

        def set_value(self, e, v):
            calls["set"].append((e.ref, v))
            return _D.set_ok

        def press_element(self, e):
            calls["press"].append(e.ref)
            return True

        def type_text(self, t):
            calls["type"].append(t)

    rt.driver = _D()
    assert "set e1" in rt.set_value("e1", "Alice")
    assert calls["set"] == [("e1", "Alice")] and calls["type"] == []  # driver op, no fallback

    _D.set_ok = False
    rt.set_value("e1", "Bob")
    assert calls["press"] == ["e1"] and calls["type"] == ["Bob"]  # fell back to focus+type

    secure = dataclasses.replace(el, secure=True)
    rt._anchor = lambda ref: (snap, secure)
    with pytest.raises(ComputerUseError) as ei:
        rt.set_value("e1", "secret")
    assert ei.value.code is ErrorCode.SECURE_FIELD


def test_scroll_to_find_scrolls_until_match(monkeypatch) -> None:
    from a11y_computer_use import server
    from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot

    def mk(has_target: bool) -> Snapshot:
        els = [Element(ref="e1", role="AXScrollArea", title="", value=None,
                       bounds=Bounds(0, 0, 0, 800, 600), snapshot_id="s")]
        if has_target:
            els.append(Element(ref="e2", role="AXButton", title="Target", value=None,
                               bounds=Bounds(0, 10, 10, 80, 30), snapshot_id="s", clickable=True))
        return Snapshot(snapshot_id="s", scope=Scope.WINDOW, app="a", pid=1, created_at=0.0,
                        displays=(Display(0, 800, 600, 1.0, True),), elements=tuple(els))

    seq = [mk(False), mk(False), mk(True)]
    scrolls: list = []

    class _D:
        i = 0

        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return seq[min(_D.i, len(seq) - 1)]

        def scroll(self, target, **kw):
            scrolls.append(kw.get("dy"))
            _D.i += 1

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))

    out = rt.scroll_to_find("app", text="Target")
    assert "found after 2 scroll(s)" in out and "Target" in out
    assert scrolls == [5, 5]  # scrolled twice, found on the third observation


# --- interactive view + budget through the tool surface -----------------------------


async def test_snapshot_interactive_mode_via_mcp(mcp_server, mocked_driver, store) -> None:
    store.set_tier(APP, safety.Tier.READ)
    result = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit", "mode": "interactive"})
    assert not result.isError
    text = result.content[0].text
    assert text.splitlines()[0].endswith("(window) interactive")
    # same refs as the full view; the click flag is implied on buttons
    assert 'e2 button "Save"' in text and 'e2 button "Save" (click)' not in text
    assert 'e3 textarea "Document body" ="hello" (click,edit,focus)' in text
    full = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    assert 'e2 button "Save" (click)' in full.content[0].text


async def test_snapshot_budget_truncates_via_mcp(mcp_server, mocked_driver, store) -> None:
    store.set_tier(APP, safety.Tier.READ)
    result = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit", "budget": 12})
    assert not result.isError
    assert "truncated at ~12 tokens" in result.content[0].text


async def test_snapshot_rejects_bad_mode_and_budget(mcp_server, mocked_driver, store) -> None:
    store.set_tier(APP, safety.Tier.READ)
    bad_mode = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit", "mode": "compact"})
    assert bad_mode.isError and "mode must be" in bad_mode.content[0].text
    bad_budget = await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit", "budget": 0})
    assert bad_budget.isError and "budget" in bad_budget.content[0].text


def test_diff_and_effect_receipt_follow_the_last_view(tmp_path) -> None:
    """mode='diff' and Effect Receipts render in the view the agent last asked
    for, so a cheap-view agent keeps getting cheap re-observations."""

    def snap(sid: str, status: str) -> Snapshot:
        els = (
            Element(ref="e1", role="AXWindow", title="W", value=None,
                    bounds=Bounds(0, 0, 0, 800, 600), snapshot_id=sid, path=("AXWindow",)),
            Element(ref="e2", role="AXButton", title="Save", value=None,
                    bounds=Bounds(0, 10, 10, 80, 30), snapshot_id=sid, parent="e1",
                    path=("AXWindow", "AXButton"), clickable=True),
            Element(ref="e3", role="AXStaticText", title="status", value=status,
                    bounds=Bounds(0, 10, 50, 200, 20), snapshot_id=sid, parent="e1",
                    path=("AXWindow", "AXStaticText")),
        )
        return Snapshot(snapshot_id=sid, scope=Scope.WINDOW, app="com.test", pid=1,
                        created_at=0.0, displays=(Display(0, 800, 600, 1.0, True),), elements=els)

    snaps = iter([snap("s0", "idle"), snap("s1", "saving"), snap("s2", "saved"),
                  snap("s3", "saved"), snap("s4", "done")])
    driver = type("_D", (), {
        "resolves_apps": True, "name": "fake",
        "ensure_trusted": lambda self: None,
        "frontmost_app": lambda self: ("com.test", 1),
        "snapshot": lambda self, scope, app: next(snaps),
    })()
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("com.test", safety.Tier.READ)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)

    first = rt.desktop_snapshot("com.test", mode="interactive")
    assert first.splitlines()[0].endswith("interactive") and 'text: "status: idle"' in first
    delta = rt.desktop_snapshot("com.test", mode="diff")  # rendered in the interactive view
    assert "~ text: e3 idle→saving" in delta and "statictext" not in delta
    receipt = rt._effect_after(rt._current)  # Effect Receipts follow the same view
    assert "~ text: e3 saving→saved" in receipt

    rt.desktop_snapshot("com.test", mode="full")  # switching back changes the diff rendering
    assert "~ e3 statictext [value: saved→done]" in rt.desktop_snapshot("com.test", mode="diff")


def test_scroll_anchor_prefers_a_scroll_container_below_the_window() -> None:
    """The browser backend exposes an overflow <ul> as AXList (CDP has no
    scroll-area role). Wheeling over the window scrolls nothing there, so the
    anchor must be the list, not the webarea; AXScrollArea still wins when present."""
    from a11y_computer_use import server
    from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot

    def el(ref, role, x, y, w, h):
        return Element(ref=ref, role=role, title="", value=None,
                       bounds=Bounds(0, x, y, w, h), snapshot_id="s")

    def snap(*els):
        return Snapshot(snapshot_id="s", scope=Scope.WINDOW, app="a", pid=1, created_at=0.0,
                        displays=(Display(0, 1280, 713, 1.0, True),), elements=tuple(els))

    web = el("e1", "AXWebArea", 0, 0, 1280, 713)
    wrapper = el("e2", "AXGroup", 0, 0, 1280, 700)       # page-sized wrapper: not a target
    lst = el("e3", "AXList", 24, 101, 362, 382)          # the overflow list
    small_group = el("e4", "AXGroup", 24, 500, 300, 40)
    assert server._scroll_anchor(snap(web, wrapper, lst, small_group)).ref == "e3"
    # no list-like container: the largest group that is not the window
    assert server._scroll_anchor(snap(web, wrapper, small_group)).ref == "e4"
    # nothing but the window: the window
    assert server._scroll_anchor(snap(web)).ref == "e1"
    # an explicit scroll area always wins, whatever its size
    area = el("e5", "AXScrollArea", 0, 0, 1280, 713)
    assert server._scroll_anchor(snap(web, area, lst)).ref == "e5"
    assert server._scroll_anchor(snap()) is None


def test_scroll_to_find_ref_pins_the_element_to_wheel_over(monkeypatch) -> None:
    from a11y_computer_use import server
    from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot

    def mk(has_target: bool) -> Snapshot:
        els = [Element(ref="e1", role="AXWebArea", title="", value=None,
                       bounds=Bounds(0, 0, 0, 1280, 713), snapshot_id="s"),
               Element(ref="e6", role="AXList", title="", value=None,
                       bounds=Bounds(0, 24, 101, 362, 382), snapshot_id="s")]
        if has_target:
            els.append(Element(ref="e9", role="AXButton", title="Reykjavik", value=None,
                               bounds=Bounds(0, 30, 300, 300, 40), snapshot_id="s", clickable=True))
        return Snapshot(snapshot_id="s", scope=Scope.WINDOW, app="a", pid=1, created_at=0.0,
                        displays=(Display(0, 1280, 713, 1.0, True),), elements=tuple(els))

    seq = [mk(False), mk(True)]
    anchors: list = []

    class _D:
        i = 0

        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return seq[min(_D.i, len(seq) - 1)]

        def scroll(self, target, **kw):
            anchors.append(target.ref)
            _D.i += 1

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._current = mk(False)
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    rt.driver.resolve_ref = lambda old, ref, *, live: observe.rematch_ref(old, ref, live)
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))

    out = rt.scroll_to_find("app", text="Reykjavik", ref="e6")
    assert "found after 1 scroll(s)" in out and anchors == ["e6"]
    with pytest.raises(server.ComputerUseError):  # a ref the current snapshot never issued
        rt.scroll_to_find("app", text="Reykjavik", ref="e999")


# --- drag path (strokes) -----------------------------------------------------


def test_drag_path_reaches_the_driver_as_waypoints_on_the_start_display(tmp_path) -> None:
    from a11y_computer_use.schema import Point

    calls: list = []

    class _D:
        resolves_apps = True
        name = "fake"
        def ensure_trusted(self): return None
        def frontmost_app(self): return ("com.test", 1)
        def main_display_id(self): return 7
        def drag(self, start, end, *, button=None, path=(), pre_check=None, dry_run=False):
            calls.append((start, end, tuple(path)))

    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("com.test", safety.Tier.CLICK)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    out = rt.drag(start_x=10, start_y=10, end_x=90, end_y=90, path=[[30, 60], [60, 30]])
    assert "via 2 waypoints" in out
    start, end, path = calls[0]
    assert path == (Point(7, 30, 60), Point(7, 60, 30))
    assert start.display_id == 7 and end.display_id == 7


def test_drag_path_rejects_bad_waypoints(tmp_path) -> None:
    class _D:
        resolves_apps = True
        name = "fake"
        def ensure_trusted(self): return None
        def frontmost_app(self): return ("com.test", 1)
        def main_display_id(self): return 0
        def drag(self, *a, **k): raise AssertionError("must not be called")

    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("com.test", safety.Tier.CLICK)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    with pytest.raises(ValueError, match=r"path\[0\]"):
        rt.drag(start_x=1, start_y=1, end_x=2, end_y=2, path=[[1]])
    with pytest.raises(ValueError, match="256"):
        rt.drag(start_x=1, start_y=1, end_x=2, end_y=2, path=[[1, 1]] * 257)


# --- verified activation ------------------------------------------------------


def test_activate_verifies_frontmost_and_escalates(monkeypatch) -> None:
    """activateWithOptions_ is advisory; _activate must confirm or raise."""
    if sys.platform != "darwin":
        pytest.skip("macOS activation path")
    monkeypatch.setattr(server, "_ACTIVATE_WAIT_S", 0.05)
    monkeypatch.setattr(server, "_top_window_pid", lambda: None)
    calls: list[str] = []

    class _Running:
        def bundleIdentifier(self): return "com.test.app"
        def processIdentifier(self): return 4242
        def activateWithOptions_(self, opts): calls.append("activate"); return True

    # 1. happy path: frontmost right after activation
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.app")
    server._activate(_Running())
    assert calls == ["activate"]

    # 2. never frontmost: escalates through open -b and AX, then raises focus_changed
    calls.clear()
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.other.app")
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: calls.append("open") or None)

    class _AX:
        def AXUIElementCreateApplication(self, pid): calls.append(f"ax-app-{pid}"); return "app"
        def AXUIElementSetAttributeValue(self, el, name, value): calls.append(f"set-{name}"); return 0
        def AXUIElementCopyAttributeValue(self, el, name, _): return (0, "win")
        def AXUIElementPerformAction(self, el, action): calls.append(f"perform-{action}"); return 0

    monkeypatch.setattr(server.observe, "_appservices", lambda: _AX())
    with pytest.raises(ComputerUseError) as info:
        server._activate(_Running())
    assert info.value.code is ErrorCode.FOCUS_CHANGED
    assert calls == ["activate", "open", "ax-app-4242", "set-AXFrontmost", "perform-AXRaise"]


def test_activate_accepts_the_window_stack_when_nsworkspace_lags(monkeypatch) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS activation path")
    monkeypatch.setattr(server, "_ACTIVATE_WAIT_S", 0.05)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.other.app")
    monkeypatch.setattr(server, "_top_window_pid", lambda: 4242)

    class _Running:
        def bundleIdentifier(self): return "com.test.app"
        def processIdentifier(self): return 4242
        def activateWithOptions_(self, opts): return True

    server._activate(_Running())  # no raise: the target owns the top window


def test_app_launch_gate_keys_by_the_installed_bundle_id(tmp_path, monkeypatch) -> None:
    """A grant for the bundle id must cover `app launch <display name>` before the app runs."""
    launched: list[str] = []

    class _D:
        resolves_apps = False
        name = "fake"
        def ensure_trusted(self): return None
        def frontmost_app(self): return ("com.front", 1)
        def main_display_id(self): return 0
        def launch_app(self, ident): launched.append(ident)

    monkeypatch.setattr(server, "_installed_bundle_id", lambda ident: "org.krita" if ident == "Krita" else None)
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("org.krita", safety.Tier.CLICK)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    monkeypatch.setattr(rt, "_resolve_app", lambda ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "x")))
    monkeypatch.setattr(rt, "_wait_first_window", lambda name, wait: "Krita")
    assert "launched Krita" in rt.app("launch", "Krita")
    assert launched == ["Krita"]


def test_launch_wait_recognises_truncated_and_vendor_prefixed_linux_comm_names(tmp_path, monkeypatch) -> None:
    """On the Box, `app launch gnome-terminal` and `app launch google-chrome` both
    opened a window at once yet reported "no window appeared within 60s": the
    window rows carry the process comm ("gnome-terminal-", cut at 15 bytes;
    "chrome", the vendor prefix dropped), which never equalled the launched name."""
    monkeypatch.setattr(server.sys, "platform", "linux")
    rows = [{"window_id": 1, "app": "gnome-terminal-", "title": "Terminal", "pid": 5},
            {"window_id": 2, "app": "chrome", "title": "New Tab - Chromium", "pid": 6}]

    class _D:
        resolves_apps = False
        name = "fake"
        def ensure_trusted(self): return None
        def frontmost_app(self): return ("gedit", 1)
        def main_display_id(self): return 0
        def windows(self): return rows

    rt = server.Runtime(store=safety.PermissionStore(tmp_path / "p.json"),
                        audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    monkeypatch.setattr(server, "_running_app", lambda ident: (None, ident))  # unresolved: echoed back
    assert rt._wait_first_window("gnome-terminal", 0.5) == "Terminal"
    assert rt._wait_first_window("google-chrome", 0.5) == "New Tab - Chromium"
    assert rt._wait_first_window("krita", 0.3) is None  # still nothing for an app with no window
    assert server._launched_as("chrome", "chromium-browser") is False  # no vendor prefix relation
    monkeypatch.setattr(server.sys, "platform", "darwin")
    assert server._launched_as("com.apple.textedit", "com.apple.textedit.helper") is False  # bundle ids stay exact


# --- observations of a named app gate against that app ---------------------------


class _WinDriver:
    resolves_apps = False
    name = "fake"
    def ensure_trusted(self): return None
    def frontmost_app(self): return ("com.owner.terminal", 1)
    def main_display_id(self): return 0
    def windows(self):
        return [{"window_id": 1, "app": "Krita", "bundle": "org.krita", "pid": 9,
                 "bounds": {"display_id": 0, "x": 100, "y": 100, "width": 800, "height": 600}},
                {"window_id": 2, "app": "Terminal", "bundle": "com.owner.terminal", "pid": 1,
                 "bounds": {"display_id": 0, "x": 0, "y": 0, "width": 400, "height": 300}}]
    def screenshot(self, display_id=None):
        img = PILImage.new("RGB", (1000, 800), "black"); buf = io.BytesIO(); img.save(buf, "PNG")
        return capture.Screenshot(png=buf.getvalue(), display=Display(0, 1000, 800, 1.0, True))


def _granted_runtime(tmp_path, monkeypatch):
    from a11y_computer_use import ocr
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("org.krita", safety.Tier.READ)          # the target, granted
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_WinDriver(),
                        ocr_engine=ocr.FakeOcr([ocr.TextBox("Brush", 120, 120, 60, 20, 1.0)]))
    monkeypatch.setattr(rt, "_resolve_app", lambda ident: (object(), "org.krita"))
    return rt


def test_screen_text_of_a_named_app_is_gated_against_that_app(tmp_path, monkeypatch) -> None:
    rt = _granted_runtime(tmp_path, monkeypatch)
    out = rt.screen_text(app="org.krita")                  # the owner's terminal is frontmost and ungranted
    assert "o1" in out and "Brush" in out and "not frontmost" in out
    with pytest.raises(server.ActionRefused):
        rt.screen_text()                                    # whole display: still gated on the frontmost app


def test_window_list_for_a_named_app_is_gated_against_it_and_filtered(tmp_path, monkeypatch) -> None:
    rt = _granted_runtime(tmp_path, monkeypatch)
    rows = json.loads(rt.window("list", app="org.krita"))
    assert [r["window_id"] for r in rows] == [1]
    with pytest.raises(server.ActionRefused):
        rt.window("list")


def test_auto_ocr_escalation_runs_when_cropped_even_if_not_frontmost(tmp_path, monkeypatch) -> None:
    rt = _granted_runtime(tmp_path, monkeypatch)
    note = rt._auto_ocr_note("org.krita", None)
    assert "Brush" in note and "cropped" in note


# --- name resolution prefers the dock app over same-named helpers -------------


class _RunningApp:
    def __init__(self, bundle, name, policy=0, pid=1):
        self._bundle, self._name, self._policy, self._pid = bundle, name, policy, pid

    def bundleIdentifier(self):
        return self._bundle

    def localizedName(self):
        return self._name

    def activationPolicy(self):
        return self._policy

    def processIdentifier(self):
        return self._pid


def test_match_running_app_prefers_bundle_then_regular_app_over_helper() -> None:
    widget = _RunningApp("com.apple.Notes.WidgetExtension", "Notes", policy=2, pid=10)
    notes = _RunningApp("com.apple.Notes", "Notes", policy=0, pid=11)
    apps = [widget, notes]
    assert server._match_running_app(apps, "Notes") == (notes, "com.apple.Notes")
    assert server._match_running_app(apps, "com.apple.notes.widgetextension") == (
        widget, "com.apple.Notes.WidgetExtension")
    assert server._match_running_app([widget], "Notes") is None  # a faceless helper is not "Notes"
    accessory = _RunningApp("com.example.bar", "Bar", policy=1)
    assert server._match_running_app([accessory], "bar") == (accessory, "com.example.bar")
    assert server._match_running_app(apps, "TextEdit") is None
    unnamed = _RunningApp(None, "Loose", policy=0)
    assert server._match_running_app([unnamed], "loose") == (unnamed, "")
