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
import sys
from importlib.metadata import version as dist_version
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as client_session
from mcp.types import ElicitResult
from PIL import Image as PILImage

from a11y_computer_use import __version__, act, capture, observe, safety, server
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
    "hover",
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
    "request_permission",
    "grant_app",
    "report_issue",
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


async def test_initialize_reports_package_version_not_the_mcp_library(mcp_server) -> None:
    """serverInfo is filled from these options. FastMCP 1.x leaves version
    unset, and the low-level server then substitutes the mcp distribution
    version (issue #14)."""
    options = mcp_server._mcp_server.create_initialization_options()
    assert options.server_name == "a11y-computer-use"
    assert options.server_version == __version__
    assert options.server_version != dist_version("mcp")


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
    # display_id 1 is the synthetic id these tests use. On a Mac it is not a
    # CGDirectDisplayID, so the bounds check needs this list or the call is
    # rejected as an unknown display before the permission gate.
    monkeypatch.setattr(
        capture, "displays",
        lambda: (Display(display_id=1, width=2880, height=1800, scale=2.0, is_main=True),),
    )
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
    assert entry["params"]["text"] == safety.REDACTED
    assert entry["params"]["chars"] == 5
    assert "hello" not in "".join(p.read_text() for p in audit_dir.glob("*.jsonl"))


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
    assert "request_permission(kind='accessibility')" in text  # the one-step grant hint


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
    monkeypatch.setattr(
        capture, "displays",
        lambda: (Display(display_id=1, width=2880, height=1800, scale=2.0, is_main=True),),
    )
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


def tmp_path_for(store) -> object:
    return store.path.parent / "audit"


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
    store.set_tier("com.other.granted", safety.Tier.READ)  # trusted, but not running
    result = await call_tool(mcp_server, "app", {"action": "list"})
    assert not result.isError
    assert json.loads(result.content[0].text) == rows_
    store.set_tier(APP, safety.Tier.READ)  # a trusted RUNNING app is preferred as the key
    assert server.Runtime(store=store, audit=safety.AuditLog(tmp_path_for(store)))._list_gate_key() == APP


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
    assert out3 == [{
        "i": 0,
        "do": "frobnicate",
        "ok": False,
        "error": "invalid_arguments: frobnicate: step 0: unknown step 'frobnicate' "
                 "— use click/hover/type/key/scroll/drag/wait_for",
    }]

    with pytest.raises(ValueError):
        rt.act_batch([])


def _act_argument_runtime():
    """Bare Runtime. Dispatch methods record calls and do no input."""
    from a11y_computer_use import server

    rt = server.Runtime.__new__(server.Runtime)
    calls: list[str] = []

    def record(name):
        def fn(*_args, **_kwargs):
            calls.append(name)
            return name
        return fn

    rt.click = record("click")
    rt.hover = record("hover")
    rt.type_text = record("type")
    rt.key = record("key")
    rt.scroll = record("scroll")
    rt.drag = record("drag")
    rt.wait_for = record("wait_for")
    return rt, calls


def _assert_rejected(rt, calls, steps, error: str) -> None:
    import json as _json

    assert _json.loads(rt.act_batch(steps)) == [{
        "i": len(steps) - 1,
        "do": steps[-1]["do"],
        "ok": False,
        "error": error,
    }]
    assert calls == []


def test_act_click_step_rejects_a_missing_target_and_a_bad_type() -> None:
    """click needs a ref or both coordinates. modifiers=42 used to TypeError
    inside tuple() after earlier steps had already run."""
    rt, calls = _act_argument_runtime()
    missing = "invalid_arguments: click: step 1: target an element ref, or both x and y coordinates"
    _assert_rejected(rt, calls, [{"do": "type", "text": "earlier"}, {"do": "click"}], missing)
    wrong = "invalid_arguments: click: step 1: 'modifiers' must be a list of modifier names"
    _assert_rejected(
        rt, calls,
        [{"do": "type", "text": "earlier"}, {"do": "click", "ref": "e1", "modifiers": 42}],
        wrong,
    )
    counted = "invalid_arguments: click: step 0: count must be 1, 2 or 3, got two"
    _assert_rejected(rt, calls, [{"do": "click", "x": 1, "y": 2, "count": "two"}], counted)
    button = "invalid_arguments: click: step 0: 5 is not a valid MouseButton"
    _assert_rejected(rt, calls, [{"do": "click", "ref": "e1", "button": 5}], button)


def test_act_hover_step_rejects_a_missing_target_and_a_bad_type() -> None:
    rt, calls = _act_argument_runtime()
    missing = "invalid_arguments: hover: step 1: target an element ref, or both x and y coordinates"
    _assert_rejected(rt, calls, [{"do": "type", "text": "earlier"}, {"do": "hover"}], missing)
    wrong = "invalid_arguments: hover: step 1: 'x' must be a finite number"
    _assert_rejected(
        rt, calls,
        [{"do": "type", "text": "earlier"}, {"do": "hover", "x": "10", "y": 20}],
        wrong,
    )


def test_act_type_step_rejects_a_missing_text_and_a_bad_type() -> None:
    import json as _json

    rt, calls = _act_argument_runtime()
    assert _json.loads(rt.act_batch([{"do": "type"}])) == [{
        "i": 0, "do": "type", "ok": False,
        "error": "invalid_arguments: type: step 0: needs a 'text'",
    }]
    assert calls == []
    missing = "invalid_arguments: type: step 1: needs a 'text'"
    _assert_rejected(rt, calls, [{"do": "key", "chord": "enter"}, {"do": "type"}], missing)
    wrong = "invalid_arguments: type: step 1: 'text' must be a string"
    _assert_rejected(
        rt, calls,
        [{"do": "key", "chord": "enter"}, {"do": "type", "text": 1}],
        wrong,
    )


def test_act_key_step_rejects_a_missing_chord_and_a_bad_type() -> None:
    """The 0.4.34 failure was the bare KeyError text ``'chord'``."""
    import json as _json

    rt, calls = _act_argument_runtime()
    assert _json.loads(rt.act_batch([{"do": "key"}])) == [{
        "i": 0, "do": "key", "ok": False,
        "error": "invalid_arguments: key: step 0: needs a 'chord'",
    }]
    assert calls == []
    # A typo for the chord field is an unknown field, not a silent drop.
    assert _json.loads(rt.act_batch([{"do": "key", "keys": "b"}])) == [{
        "i": 0, "do": "key", "ok": False,
        "error": "invalid_arguments: key: step 0: unknown field 'keys'; expected chord, modifiers",
    }]
    assert calls == []
    wrong = "invalid_arguments: key: step 1: 'chord' must be a string"
    _assert_rejected(
        rt, calls,
        [{"do": "type", "text": "earlier"}, {"do": "key", "chord": 1}],
        wrong,
    )
    empty = "invalid_arguments: key: step 0: empty chord ''"
    _assert_rejected(rt, calls, [{"do": "key", "chord": ""}], empty)
    rt.driver = type("_D", (), {"name": "linux"})()
    unknown = _json.loads(rt.act_batch([
        {"do": "type", "text": "earlier"},
        {"do": "key", "chord": "not-a-key"},
    ]))
    assert calls == []
    assert unknown == [{
        "i": 1, "do": "key", "ok": False,
        "error": "invalid_arguments: key: step 1: unknown key 'not-a-key' in 'not-a-key'",
    }]


def test_act_key_folds_a_modifier_list_and_rejects_a_string_or_unknown_field() -> None:
    """Standalone key presses modifiers inside the chord. An act key step with
    modifiers: ["ctrl"] and chord "a" presses ctrl+a. A string modifier and any
    other field are invalid_arguments, and a later bad step runs nothing."""
    import json as _json

    rt, calls = _act_argument_runtime()
    pressed: list[str] = []
    rt.key = lambda chord: pressed.append(chord) or f"pressed {chord}"
    out = _json.loads(rt.act_batch([
        {"do": "key", "chord": "a", "modifiers": ["ctrl"]},
        {"do": "key", "chord": "a", "modifiers": ["ctrl", "shift"]},
        {"do": "key", "chord": "cmd+s", "modifiers": ["ctrl"]},
    ]))
    assert [step["ok"] for step in out] == [True, True, True]
    assert pressed == ["ctrl+a", "ctrl+shift+a", "ctrl+cmd+s"]
    assert calls == []

    pressed.clear()
    string_mod = _json.loads(rt.act_batch([
        {"do": "type", "text": "earlier"},
        {"do": "key", "chord": "a", "modifiers": "ctrl"},
    ]))
    assert pressed == [] and calls == []
    assert string_mod == [{
        "i": 1, "do": "key", "ok": False,
        "error": "invalid_arguments: key: step 1: 'modifiers' must be a list of modifier names",
    }]

    unknown_mod = _json.loads(rt.act_batch([
        {"do": "type", "text": "earlier"},
        {"do": "key", "chord": "a", "modifiers": ["super"]},
    ]))
    assert pressed == [] and calls == []
    assert "unknown modifiers" in unknown_mod[0]["error"]
    assert unknown_mod[0]["error"].startswith("invalid_arguments: key: step 1:")

    extra = _json.loads(rt.act_batch([
        {"do": "type", "text": "earlier"},
        {"do": "key", "chord": "a", "keys": "b"},
    ]))
    assert pressed == [] and calls == []
    assert extra == [{
        "i": 1, "do": "key", "ok": False,
        "error": "invalid_arguments: key: step 1: unknown field 'keys'; expected chord, modifiers",
    }]

    other = _json.loads(rt.act_batch([
        {"do": "click", "ref": "e1", "keys": "b"},
    ]))
    assert pressed == []
    assert other[0]["ok"] is False and "unknown field 'keys'" in other[0]["error"]
    assert other[0]["error"].startswith("invalid_arguments: click: step 0:")


def test_act_scroll_step_rejects_a_missing_target_and_a_bad_type() -> None:
    rt, calls = _act_argument_runtime()
    missing = "invalid_arguments: scroll: step 1: target an element ref, or both x and y coordinates"
    _assert_rejected(rt, calls, [{"do": "type", "text": "earlier"}, {"do": "scroll"}], missing)
    wrong = "invalid_arguments: scroll: step 1: 'dy' must be a finite number"
    _assert_rejected(
        rt, calls,
        [{"do": "type", "text": "earlier"}, {"do": "scroll", "ref": "e3", "dy": "down"}],
        wrong,
    )
    unit = "invalid_arguments: scroll: step 0: 5 is not a valid ScrollUnit"
    _assert_rejected(rt, calls, [{"do": "scroll", "ref": "e3", "unit": 5}], unit)


def test_act_drag_step_rejects_a_missing_end_and_a_bad_type() -> None:
    rt, calls = _act_argument_runtime()
    missing = (
        "invalid_arguments: drag: step 1: "
        "target a 'start_ref', or both 'start_x' and 'start_y'"
    )
    _assert_rejected(rt, calls, [{"do": "type", "text": "earlier"}, {"do": "drag"}], missing)
    end = (
        "invalid_arguments: drag: step 0: "
        "target an 'end_ref', or both 'end_x' and 'end_y'"
    )
    _assert_rejected(rt, calls, [{"do": "drag", "start_ref": "e1"}], end)
    wrong = "invalid_arguments: drag: step 1: 'path' must be a list of [x, y] pairs"
    _assert_rejected(
        rt, calls,
        [{"do": "type", "text": "earlier"},
         {"do": "drag", "start_ref": "e1", "end_ref": "e2", "path": 5}],
        wrong,
    )
    text_path = "invalid_arguments: drag: step 0: 'path' must be a list of [x, y] pairs"
    _assert_rejected(
        rt, calls,
        [{"do": "drag", "start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1, "path": "nope"}],
        text_path,
    )


def test_act_wait_for_step_rejects_a_missing_ref_and_a_bad_type() -> None:
    """The 0.4.34 failure was the bare KeyError text ``'ref'``. A string
    timeout used to TypeError inside math.isfinite."""
    import json as _json

    rt, calls = _act_argument_runtime()
    assert _json.loads(rt.act_batch([{"do": "wait_for"}])) == [{
        "i": 0, "do": "wait_for", "ok": False,
        "error": "invalid_arguments: wait_for: step 0: needs a 'ref'",
    }]
    assert calls == []
    missing = "invalid_arguments: wait_for: step 1: needs a 'ref'"
    _assert_rejected(rt, calls, [{"do": "type", "text": "earlier"}, {"do": "wait_for"}], missing)
    wrong = "invalid_arguments: wait_for: step 1: 'ref' must be a string"
    _assert_rejected(
        rt, calls,
        [{"do": "type", "text": "earlier"}, {"do": "wait_for", "ref": 1}],
        wrong,
    )
    timeout = "invalid_arguments: wait_for: step 1: timeout_s must be finite and nonnegative"
    _assert_rejected(
        rt, calls,
        [{"do": "type", "text": "earlier"}, {"do": "wait_for", "ref": "e7", "timeout_s": "soon"}],
        timeout,
    )


def test_act_argument_errors_run_no_step_and_skip_the_effect_snapshot() -> None:
    """A step that is not an object is rejected with the earlier steps unrun.
    verify does not re-snapshot when the batch never started."""
    import json as _json

    rt, calls = _act_argument_runtime()
    out = _json.loads(rt.act_batch([{"do": "type", "text": "earlier"}, "nope"], verify=True))
    assert out == {
        "steps": [{
            "i": 1,
            "ok": False,
            "error": "invalid_arguments: act: step 1: each step needs a 'do' field",
        }],
        "effect": "",
    }
    assert calls == []


def test_act_batch_still_runs_a_valid_step_of_each_type() -> None:
    import json as _json

    rt, calls = _act_argument_runtime()
    rt.driver = type("_D", (), {"name": "linux"})()
    steps = [
        {"do": "click", "ref": "e1"},
        {"do": "hover", "x": 1, "y": 2},
        {"do": "type", "text": "hi"},
        {"do": "key", "chord": "enter"},
        {"do": "scroll", "ref": "e1", "dy": 3, "into_view": False},
        {"do": "drag", "start_ref": "e1", "end_ref": "e2", "path": [[1, 2], [3, 4]]},
        {"do": "wait_for", "ref": "e1", "condition": "exists", "timeout_s": 1},
    ]
    out = _json.loads(rt.act_batch(steps))
    assert [step["ok"] for step in out] == [True] * len(steps)
    assert calls == ["click", "hover", "type", "key", "scroll", "drag", "wait_for"]


async def test_act_step_failure_is_a_tool_error_and_keeps_the_step_json(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    """A client that only checks isError must see a failed batch. The body
    stays the per-step JSON, including a secure_field step and any earlier
    step that did run."""
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    store.set_tier("com.test.front", safety.Tier.FULL)

    invalid = await call_tool(mcp_server, "act", {"steps": [{"do": "key"}]})
    assert invalid.isError
    invalid_body = json.loads(invalid.content[0].text)
    assert invalid_body == [{
        "i": 0, "do": "key", "ok": False,
        "error": "invalid_arguments: key: step 0: needs a 'chord'",
    }]

    ok = await call_tool(mcp_server, "act", {"steps": [{"do": "type", "text": "hi"}]})
    assert not ok.isError
    assert json.loads(ok.content[0].text)[0]["ok"] is True
    assert mocked_driver["type"] == ["hi"]

    def refuse_key(chord, **_kwargs):
        raise ComputerUseError(ErrorCode.SECURE_FIELD, "password field focused", detail={})

    monkeypatch.setattr(act, "key_chord", refuse_key)
    failed = await call_tool(mcp_server, "act", {"steps": [
        {"do": "type", "text": "hi"},
        {"do": "key", "chord": "a"},
    ]})
    assert failed.isError
    body = json.loads(failed.content[0].text)
    assert [step["ok"] for step in body] == [True, False]
    assert "secure_field" in body[1]["error"]
    assert body[0]["result"].startswith("typed")
    assert "Error executing tool" not in failed.content[0].text


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
    rt._recheck_target = lambda app, target: None  # the HID fallback's guard; hit-tests the real screen
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


def _item_window(head: int) -> Snapshot:
    """Eight rows starting at ``head``. Synthetic snapshot, not a Chrome list."""
    els = [Element(ref="box", role="AXScrollArea", title="", value=None,
                   bounds=Bounds(0, 0, 0, 800, 600), snapshot_id="s")]
    for offset, number in enumerate(range(head, head + 8)):
        els.append(Element(
            ref=f"r{number}", role="AXRow", title=f"ITEM-{number:03d}", value=None,
            bounds=Bounds(0, 20, 40 + offset * 16, 400, 16), snapshot_id="s",
        ))
    return Snapshot(snapshot_id="s", scope=Scope.WINDOW, app="a", pid=1, created_at=0.0,
                    displays=(Display(0, 800, 600, 1.0, True),), elements=tuple(els))


def _still_page(dy: int) -> ComputerUseError:
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "the rows on screen did not change after the wheel scroll",
        detail={"reason": "page_unchanged", "unit": "lines", "dx": 0, "dy": dy, "mean_abs": 0.0},
    )


def test_scroll_to_find_comes_back_after_a_still_page_past_the_target(monkeypatch) -> None:
    """Synthetic snapshots, not a live Chrome list.

    A downward step jumps forty rows and lands on ITEM-193 through ITEM-200
    without showing ITEM-180. The next wheel does not move and raises
    page_unchanged. The search then steps back one line at a time until the
    window contains ITEM-180. The default budget of 6 is extended for that
    return pass.
    """
    from a11y_computer_use import server

    scrolls: list[int] = []

    class _D:
        screen = 1

        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return _item_window(_D.screen)

        def scroll(self, target, **kw):
            dy = int(kw.get("dy") or 0)
            scrolls.append(dy)
            if dy > 0:
                if _D.screen >= 193:
                    raise _still_page(dy)
                _D.screen = min(193, _D.screen + 40)
                return
            if _D.screen <= 1:
                raise _still_page(dy)
            _D.screen = max(1, _D.screen - 4)

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))

    out = rt.scroll_to_find("app", text="ITEM-180")
    assert "ITEM-180" in out
    assert "found after 10 scroll(s)" in out
    assert "found after 0" not in out
    assert scrolls == [5, 5, 5, 5, 5, 5, -1, -1, -1, -1]
    assert -5 not in scrolls


def test_scroll_to_find_stops_when_both_directions_stay_still(monkeypatch) -> None:
    """Synthetic snapshots, not a live Chrome list.

    The target is not on screen. The first wheel and the one-line return
    both report page_unchanged. That second still page is the error the
    caller sees. The search does not keep scrolling.
    """
    from a11y_computer_use import server

    scrolls: list[int] = []

    class _D:
        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return _item_window(193)

        def scroll(self, target, **kw):
            dy = int(kw.get("dy") or 0)
            scrolls.append(dy)
            raise _still_page(dy)

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))

    with pytest.raises(ComputerUseError) as error:
        rt.scroll_to_find("app", text="ITEM-180")
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert error.value.detail["reason"] == "page_unchanged"
    assert error.value.detail["dy"] == -1
    assert scrolls == [5, -1]


def test_scroll_to_find_does_not_turn_around_on_rows_stale(monkeypatch) -> None:
    """A rows_stale scroll is not a still page. The search does not reverse."""
    from a11y_computer_use import server

    scrolls: list[int] = []

    class _D:
        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return _item_window(1)

        def scroll(self, target, **kw):
            scrolls.append(int(kw.get("dy") or 0))
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "the list moved on screen but the snapshot would still show the old rows",
                detail={"reason": "rows_stale", "unit": "lines", "dy": 5},
            )

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))

    with pytest.raises(ComputerUseError) as error:
        rt.scroll_to_find("app", text="ITEM-180")
    assert error.value.detail["reason"] == "rows_stale"
    assert scrolls == [5]


def test_scroll_to_find_returns_a_target_already_on_screen(monkeypatch) -> None:
    """The target is in the first snapshot, so no wheel is sent."""
    from a11y_computer_use import server

    scrolls: list[int] = []

    class _D:
        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return _item_window(177)

        def scroll(self, target, **kw):
            scrolls.append(int(kw.get("dy") or 0))
            raise _still_page(int(kw.get("dy") or 0))

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "com.a"))

    out = rt.scroll_to_find("app", text="ITEM-180")
    assert "found after 0 scroll(s)" in out and "ITEM-180" in out
    assert scrolls == []


def test_scroll_to_find_matches_text_past_the_value_clip(monkeypatch) -> None:
    """Text already in the field, past the 200-character value cap, is a hit
    on the first observation. It is not scrolled out of view."""
    from tests.fixtures.trees import GEOMETRY, DictAccessor, ax

    token = "line-15-token"
    prefix = "line 01 " + ("." * 272)
    tree = ax(
        "AXWindow", title="Mousepad", at=(0.0, 0.0), size=(800.0, 600.0),
        children=[ax("AXTextArea", value=prefix + token, at=(10.0, 10.0), size=(700.0, 500.0))],
    )
    snap = observe.build_snapshot(
        tree, DictAccessor(), scope=Scope.WINDOW, app="mousepad", pid=1, geometry=GEOMETRY,
    )
    scrolls: list = []

    class _D:
        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return snap

        def scroll(self, target, **kw):
            scrolls.append(kw)

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = _D()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kwargs: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, "_running_app", lambda a: (None, "mousepad"))

    out = rt.scroll_to_find("mousepad", text=token)
    assert "found after 0 scroll(s)" in out and "1 match" in out
    assert scrolls == []


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


def test_scroll_anchor_wheels_the_document_not_the_tab_strip() -> None:
    """A document-scroll list is taller than the window, so it is the largest
    element. 0.4.19 dropped it (area >= 0.9 of the largest element) and the
    tab strip, in the same tier, won. The wheel belongs on the document, or
    on the list when the document group is not in the tree. Synthetic bounds,
    not a live Chrome window."""
    from a11y_computer_use import server
    from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot

    def el(ref, role, x, y, w, h, parent=None):
        return Element(ref=ref, role=role, title="", value=None,
                       bounds=Bounds(0, x, y, w, h), snapshot_id="s", parent=parent)

    def snap(*els):
        return Snapshot(snapshot_id="s", scope=Scope.WINDOW, app="chrome", pid=1, created_at=0.0,
                        displays=(Display(0, 1280, 800, 1.0, True),), elements=tuple(els))

    window = el("e1", "AXWindow", 0, 0, 1280, 800)
    tabs = el("e2", "AXTabGroup", 0, 0, 1280, 40, parent="e1")
    document = el("e3", "AXGroup", 0, 88, 1280, 680, parent="e1")
    listing = el("e4", "AXList", 40, 120, 1100, 6400, parent="e3")
    assert server._scroll_anchor(snap(window, tabs, document, listing)).ref == "e3"
    # The document group collapsed away: the list is the content, not the strip.
    bare = el("e4", "AXList", 40, 120, 1100, 6400, parent="e1")
    assert server._scroll_anchor(snap(window, tabs, bare)).ref == "e4"
    # A fixed-height overflow list stays the anchor when a tab strip is present,
    # including when the tab group is a large notebook rather than a thin strip.
    overflow = el("e5", "AXList", 24, 101, 362, 382, parent="e3")
    assert server._scroll_anchor(snap(window, tabs, document, overflow)).ref == "e5"
    notebook = el("e6", "AXTabGroup", 0, 60, 1280, 700, parent="e1")
    assert server._scroll_anchor(snap(window, notebook, overflow)).ref == "e5"
    # No list at all: a notebook-sized tab group is still a place to wheel.
    assert server._scroll_anchor(snap(window, notebook)).ref == "e6"


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


# --- one-step grants and agent reports --------------------------------------------


async def test_grant_app_records_the_grant_only_when_the_user_accepts(mcp_server, store, monkeypatch) -> None:
    monkeypatch.setattr(server, "_installed_bundle_id", lambda ident: "com.example.Paint" if ident == "Paint" else None)
    async with client_session(mcp_server, elicitation_callback=_decline_elicitation) as client:
        result = await client.call_tool("grant_app", {"app": "Paint", "tier": "full"})
    assert "not granted: the user declined com.example.Paint" in result.content[0].text
    assert store.get_tier("com.example.Paint") is None

    async with client_session(mcp_server, elicitation_callback=_accept_elicitation) as client:
        result = await client.call_tool("grant_app", {"app": "Paint", "tier": "full"})
    assert result.content[0].text.startswith("granted: com.example.Paint at tier 'full'")
    assert store.get_tier("com.example.Paint") is safety.Tier.FULL

    async with client_session(mcp_server, elicitation_callback=_accept_elicitation) as client:
        result = await client.call_tool("grant_app", {"app": "Paint", "tier": "click"})
    assert result.content[0].text == "already granted: com.example.Paint at tier 'full'"

    result = await call_tool(mcp_server, "grant_app", {"app": "Paint", "tier": "sudo"})
    assert result.isError and "tier must be" in result.content[0].text


async def test_grant_app_without_a_host_dialog_asks_through_a_native_one(mcp_server, store, monkeypatch) -> None:
    from a11y_computer_use import onboarding

    shown = []
    monkeypatch.setattr(onboarding, "native_confirm", lambda title, msg, **kw: shown.append(title + " " + msg) or True)
    result = await call_tool(mcp_server, "grant_app", {"app": "com.example.Other", "tier": "click"})
    assert result.content[0].text == "granted: com.example.Other at tier 'click' (user confirmed in the native dialog)"
    assert store.get_tier("com.example.Other") is safety.Tier.CLICK
    assert "com.example.Other" in shown[0] and "tier 'click'" in shown[0]

    monkeypatch.setattr(onboarding, "native_confirm", lambda title, msg, **kw: False)
    result = await call_tool(mcp_server, "grant_app", {"app": "com.example.Third", "tier": "click"})
    assert "declined com.example.Third at 'click' in the native dialog" in result.content[0].text
    assert store.get_tier("com.example.Third") is None

    monkeypatch.setattr(onboarding, "native_confirm", lambda title, msg, **kw: None)
    result = await call_tool(mcp_server, "grant_app", {"app": "com.example.Fourth", "tier": "click"})
    assert "a11y-computer-use grant com.example.Fourth click" in result.content[0].text
    assert store.get_tier("com.example.Fourth") is None


async def test_needs_permission_refusal_names_the_grant_step(mcp_server, monkeypatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.test.front")
    result = await call_tool(mcp_server, "screenshot", {})
    assert result.isError
    text = result.content[0].text
    assert "needs_permission: com.test.front" in text and "grant_app(app='com.test.front', tier='read')" in text
    assert "a11y-computer-use grant com.test.front read" in text


async def test_report_issue_without_gh_returns_a_prefilled_link(mcp_server, audit_dir, monkeypatch) -> None:
    from a11y_computer_use import onboarding, reporting

    monkeypatch.setattr(reporting.shutil, "which", lambda name: None)
    monkeypatch.setattr(onboarding, "native_confirm", lambda *a, **kw: None)
    result = await call_tool(mcp_server, "report_issue", {
        "kind": "bug", "title": "click crashed", "body": "internal_error: click crashed: KeyError", "tool": "click"})
    text = result.content[0].text
    assert text.startswith("not posted: no signed-in GitHub CLI") and reporting.ISSUES_URL + "/new?" in text
    assert audit_entries(audit_dir)[-1]["action"] == "report_issue"

    result = await call_tool(mcp_server, "report_issue", {"kind": "rant", "title": "x", "body": "y"})
    assert result.isError and "kind must be one of" in result.content[0].text


async def test_request_permission_is_a_no_op_off_macos(mcp_server, monkeypatch) -> None:
    from a11y_computer_use import onboarding

    monkeypatch.setattr(onboarding.sys, "platform", "linux")
    result = await call_tool(mcp_server, "request_permission", {"kind": "accessibility"})
    assert json.loads(result.content[0].text)["needed"] is False


async def test_unknown_key_chord_is_invalid_arguments_not_an_internal_error(mcp_server, audit_dir) -> None:
    """key('ctrl+notakey') names the unknown key. It is not an internal crash."""
    result = await call_tool(mcp_server, "key", {"chord": "ctrl+notakey"})
    assert result.isError
    text = result.content[0].text
    # The MCP client prefixes "Error executing tool <name>: ".
    assert "invalid_arguments: key:" in text
    assert "unknown key 'notakey'" in text
    assert "internal_error" not in text
    assert "report_issue" not in text
    assert audit_entries(audit_dir) == []  # rejected before the permission gate


class _HoverDriver:
    """Records hover and click. ``name`` selects the Linux runtime path."""

    def __init__(self, name: str = "linux") -> None:
        self.name = name
        self.hovered: list = []
        self.clicked: list = []

    def hover(self, target, **kwargs) -> None:
        self.hovered.append(target)

    def click(self, target, **kwargs) -> None:
        self.clicked.append(target)

    def main_display_id(self) -> int:
        return 0

    def resolve_ref(self, snap, ref, *, live=None):
        return snap.element(ref)


def _hover_runtime(tmp_path, driver: _HoverDriver) -> server.Runtime:
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("editor", safety.Tier.CLICK)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


def test_linux_hover_tool_act_step_and_run_once_move_without_a_click(tmp_path, monkeypatch) -> None:
    """Fake driver only. Not a live tooltip. The three names that rejected hover
    call the existing Runtime.hover and do not click."""
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "editor")
    monkeypatch.setattr(server, "_app_at_point", lambda point: None)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    driver = _HoverDriver()
    rt = _hover_runtime(tmp_path, driver)
    rt._current = build_synthetic_snapshot(app="editor")

    assert rt.call_tool("hover", {"x": 10, "y": 20}) == "hovered (10, 20) on display 0"
    assert rt.dispatch("hover", {"x": 30, "y": 40}) == "hovered (30, 40) on display 0"
    steps = json.loads(rt.act_batch([{"do": "hover", "ref": "e2"}]))
    assert steps == [{"i": 0, "do": "hover", "ok": True, "result": "hovered e2 (AXButton 'Save')"}]
    assert [target.ref if isinstance(target, Element) else (target.x, target.y) for target in driver.hovered] == [
        (10, 20),
        (30, 40),
        "e2",
    ]
    assert driver.clicked == []


def test_hover_onto_another_apps_point_does_not_move_the_pointer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "editor")
    monkeypatch.setattr(server, "_app_at_point", lambda point: "com.other.overlay")
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    driver = _HoverDriver()
    rt = _hover_runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as error:
        rt.call_tool("hover", {"x": 10, "y": 20})
    assert error.value.code is ErrorCode.FOCUS_CHANGED
    assert driver.hovered == []
    assert driver.clicked == []


def test_hover_off_linux_is_unsupported_and_does_not_move(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "editor")
    monkeypatch.setattr(server, "_app_at_point", lambda point: None)
    driver = _HoverDriver(name="macos")
    rt = _hover_runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as error:
        rt.call_tool("hover", {"x": 10, "y": 20})
    assert error.value.code is ErrorCode.UNSUPPORTED
    assert "Linux" in error.value.message
    assert driver.hovered == []
    with pytest.raises(ComputerUseError):
        rt.dispatch("hover", {"x": 1, "y": 2})
    steps = json.loads(rt.act_batch([{"do": "hover", "x": 1, "y": 2}]))
    assert steps[0]["ok"] is False and "unsupported" in steps[0]["error"]
    assert driver.hovered == []


async def test_mcp_hover_on_linux_moves_and_off_linux_is_unsupported(
    store, audit_dir, monkeypatch
) -> None:
    """In-memory MCP transport. The driver is a fake, not a live pointer."""
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "editor")
    monkeypatch.setattr(server, "_app_at_point", lambda point: None)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    store.set_tier("editor", safety.Tier.CLICK)
    linux = _HoverDriver()
    with server.Runtime(store=store, audit=safety.AuditLog(audit_dir), driver=linux) as runtime:
        mcp = server.build_server(runtime=runtime)
        listed = await _tool_names(mcp)
        assert "hover" in listed
        result = await call_tool(mcp, "hover", {"x": 15, "y": 25})
    assert not result.isError
    assert result.content[0].text == "hovered (15, 25) on display 0"
    assert len(linux.hovered) == 1 and linux.clicked == []

    mac = _HoverDriver(name="macos")
    with server.Runtime(store=store, audit=safety.AuditLog(audit_dir), driver=mac) as runtime:
        result = await call_tool(server.build_server(runtime=runtime), "hover", {"x": 1, "y": 2})
    assert result.isError
    assert "unsupported" in result.content[0].text
    assert mac.hovered == []


async def _tool_names(mcp_server) -> set[str]:
    async with client_session(mcp_server) as client:
        listed = (await client.list_tools()).tools
    return {tool.name for tool in listed}


def test_linux_runtime_rejects_an_unknown_chord_before_input(tmp_path) -> None:
    pressed: list[str] = []

    class _D:
        name = "linux"
        resolves_apps = False

        def key_chord(self, chord, **kw):
            pressed.append(chord)

        def frontmost_app(self):
            return ("editor", 1)

        def main_display_id(self):
            return 0

    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("editor", safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    with pytest.raises(ValueError, match="unknown key 'notakey'"):
        rt.key("ctrl+notakey")
    assert pressed == []
    assert list((tmp_path / "audit").glob("*.jsonl")) == []


def test_windows_validate_chord_names_an_unknown_key_without_sendinput() -> None:
    from a11y_computer_use.drivers import _win_input

    with pytest.raises(ValueError, match="unknown key 'notakey'"):
        _win_input.validate_chord("ctrl+notakey")
    _win_input.validate_chord("ctrl+a")


async def test_a_crash_inside_a_tool_reads_as_internal_error_with_the_report_hint(mcp_server, monkeypatch) -> None:
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: (_ for _ in ()).throw(KeyError("boom")))
    result = await call_tool(mcp_server, "screenshot", {})
    assert result.isError
    assert "internal_error: screenshot crashed: KeyError" in result.content[0].text
    assert "report_issue(kind='bug', tool='screenshot'" in result.content[0].text



def test_running_app_by_bundle_asks_launchservices_directly(monkeypatch) -> None:
    """The NSWorkspace list lags in a process whose main run loop is the asyncio
    loop; a direct bundle-id query still sees the app (Calculator, 2026-10-01)."""
    import sys as _sys
    import types

    class Running:
        def __init__(self, policy): self._p = policy
        def activationPolicy(self): return self._p

    calls = []
    fake_appkit = types.SimpleNamespace(NSRunningApplication=types.SimpleNamespace(
        runningApplicationsWithBundleIdentifier_=lambda bid: calls.append(bid) or
        ([Running(2), Running(0)] if bid == "com.apple.calculator" else [])))
    monkeypatch.setitem(_sys.modules, "AppKit", fake_appkit)
    monkeypatch.setattr(server, "_installed_bundle_id", lambda ident: "com.apple.calculator" if ident == "Calculator" else (ident if "." in ident else None))
    got = server._running_app_by_bundle("Calculator")
    assert got is not None and got[1] == "com.apple.calculator" and got[0].activationPolicy() == 0  # helper skipped
    assert server._running_app_by_bundle("com.example.none") is None
    assert server._running_app_by_bundle("Nothing") is None and calls == ["com.apple.calculator", "com.example.none"]


# --- background input: keystrokes addressed to a process, no activation ------------


class _RunningPid:
    def __init__(self, pid=4242): self._pid = pid
    def bundleIdentifier(self): return APP
    def processIdentifier(self): return self._pid


class _BgDriver:
    """A driver that can address keyboard input to a process."""
    resolves_apps = False
    name = "fake-bg"
    background_input = True

    def __init__(self):
        self.typed: list[tuple[str, int | None]] = []
        self.chords: list[tuple[str, int | None]] = []
        self.launched: list[tuple[str, bool]] = []
    def ensure_trusted(self): return None
    def frontmost_app(self): return ("com.other.front", 1)  # the user is elsewhere
    def main_display_id(self): return 0
    def type_text(self, text, *, pre_check=None, dry_run=False, pid=None): self.typed.append((text, pid))
    def key_chord(self, chord, *, pre_check=None, dry_run=False, pid=None): self.chords.append((chord, pid))
    def launch_app(self, ident, *, activate=True): self.launched.append((ident, activate))


@pytest.fixture
def bg(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_running_app", lambda ident: (_RunningPid(), APP))
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "com.other.front")
    store = safety.PermissionStore(tmp_path / "p.json")
    driver = _BgDriver()
    with server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver) as rt:
        yield server.build_server(runtime=rt), rt, driver, store


async def test_type_with_app_is_addressed_to_the_process_and_gated_against_it(bg) -> None:
    srv, rt, driver, store = bg
    store.set_tier(APP, safety.Tier.FULL)
    result = await call_tool(srv, "type", {"text": "hi", "app": APP})
    assert not result.isError, result.content[0].text
    assert "addressed to its process; nothing was activated" in result.content[0].text
    assert driver.typed == [("hi", 4242)]
    result = await call_tool(srv, "key", {"chord": "cmd+s", "app": APP})
    assert not result.isError and driver.chords == [("cmd+s", 4242)]


async def test_type_with_app_needs_the_app_grant_not_the_frontmost_one(bg) -> None:
    srv, rt, driver, store = bg
    store.set_tier("com.other.front", safety.Tier.FULL)  # the frontmost app's grant must not count
    result = await call_tool(srv, "type", {"text": "hi", "app": APP})
    assert result.isError and "needs_permission: " + APP in result.content[0].text
    assert driver.typed == []


async def test_background_mode_addresses_the_observed_app_by_default(bg, monkeypatch) -> None:
    srv, rt, driver, store = bg
    monkeypatch.setattr(server, "FOCUS_MODE", "background")
    store.set_tier(APP, safety.Tier.FULL)
    rt._current = dataclasses.replace(build_synthetic_snapshot(), app=APP)
    result = await call_tool(srv, "type", {"text": "x"})
    assert not result.isError, result.content[0].text
    assert driver.typed == [("x", 4242)]
    result = await call_tool(srv, "app", {"action": "launch", "name": APP})
    assert driver.launched and driver.launched[-1][1] is False  # background mode: open -g


async def test_launch_activate_flag_reaches_the_driver(bg, monkeypatch) -> None:
    srv, rt, driver, store = bg
    store.set_tier(APP, safety.Tier.CLICK)
    monkeypatch.setattr(rt, "_wait_first_window", lambda name, wait: "Untitled")
    await call_tool(srv, "app", {"action": "launch", "name": APP, "activate": False})
    await call_tool(srv, "app", {"action": "launch", "name": APP})
    assert [a for _, a in driver.launched] == [False, True]


async def test_type_with_app_is_unsupported_off_background_drivers(bg) -> None:
    srv, rt, driver, store = bg
    driver.background_input = False
    store.set_tier(APP, safety.Tier.FULL)
    result = await call_tool(srv, "type", {"text": "hi", "app": APP})
    assert result.isError and "unsupported" in result.content[0].text


def test_launch_without_activation_uses_open_g(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stderr": ""})())
    server._launch_app("com.apple.TextEdit", activate=False)
    server._launch_app("com.apple.TextEdit")
    assert calls[0] == ["/usr/bin/open", "-g", "-b", "com.apple.TextEdit"]
    assert calls[1] == ["/usr/bin/open", "-b", "com.apple.TextEdit"]


def test_launch_wait_sees_a_window_on_another_space(tmp_path, monkeypatch) -> None:
    """The on-screen window list stops at the current Space; an app that opened
    its window on another desktop must still count as launched."""
    class _D:
        resolves_apps = False
        name = "fake"
        def ensure_trusted(self): return None
        def frontmost_app(self): return ("com.front", 1)
        def main_display_id(self): return 0
        def windows(self): return []  # nothing on this Space

    monkeypatch.setattr(server, "_running_app", lambda ident: (_RunningPid(31), APP))
    monkeypatch.setattr(server, "_window_titles_all_spaces", lambda pid: ["Untitled 2.rtf"] if pid == 31 else [])
    store = safety.PermissionStore(tmp_path / "p.json")
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    assert rt._wait_first_window(APP, 0.5) == "Untitled 2.rtf"
    monkeypatch.setattr(server, "_window_titles_all_spaces", lambda pid: [])
    assert rt._wait_first_window(APP, 0.3) is None


# --- the human has the keyboard -----------------------------------------------------


async def test_focus_and_hid_input_are_refused_while_the_user_is_active(bg, monkeypatch) -> None:
    srv, rt, driver, store = bg
    store.set_tier(APP, safety.Tier.FULL)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: 0.3)
    driver.activate_app = lambda name: (_ for _ in ()).throw(AssertionError("must not activate"))
    result = await call_tool(srv, "app", {"action": "focus", "name": APP})
    assert result.isError and "user_active" in result.content[0].text and "0.3 s ago" in result.content[0].text
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: APP)  # the user is inside the target app
    result = await call_tool(srv, "type", {"text": "hi", "app": APP})
    assert result.isError and "user_active" in result.content[0].text
    assert driver.typed == []


async def test_addressed_input_into_another_app_is_fine_while_the_user_is_active(bg, monkeypatch) -> None:
    srv, rt, driver, store = bg
    store.set_tier(APP, safety.Tier.FULL)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: 0.3)
    result = await call_tool(srv, "type", {"text": "hi", "app": APP})  # user is in com.other.front
    assert not result.isError, result.content[0].text
    assert driver.typed == [("hi", 4242)]


async def test_idle_user_does_not_block_focus(bg, monkeypatch) -> None:
    srv, rt, driver, store = bg
    store.set_tier(APP, safety.Tier.CLICK)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: 12.0)
    activated = []
    driver.activate_app = lambda name: activated.append(name)
    monkeypatch.setattr(rt, "_wait_frontmost", lambda bundle, wait, pid=None: True)
    result = await call_tool(srv, "app", {"action": "focus", "name": APP})
    assert not result.isError and activated == [APP]


def test_seconds_since_user_input_ignores_our_own_hid_posts(monkeypatch) -> None:
    import types

    from a11y_computer_use import act

    monkeypatch.setattr(safety, "seconds_since_user_input", safety._real_seconds_since_user_input)
    monkeypatch.setattr(safety.sys, "platform", "darwin")
    fake_quartz = types.SimpleNamespace(
        kCGEventSourceStateHIDSystemState=1, kCGAnyInputEventType=~0,
        CGEventSourceSecondsSinceLastEventType=lambda state, kind: 0.2)
    monkeypatch.setitem(safety.sys.modules, "Quartz", fake_quartz)
    monkeypatch.setattr(act, "LAST_HID_POST_MONOTONIC", 0.0)
    assert safety.seconds_since_user_input() == 0.2  # a real hardware event
    monkeypatch.setattr(act, "LAST_HID_POST_MONOTONIC", safety.time.monotonic() - 0.2)
    assert safety.seconds_since_user_input() is None  # that event was ours


# --- issue 11: a covered window is not a visible one ---------------------------------


async def test_ax_ref_click_works_under_an_overlay_but_hid_click_does_not(
    mcp_server, mocked_driver, store, monkeypatch
) -> None:
    store.set_tier(APP, safety.Tier.FULL)
    await call_tool(mcp_server, "desktop_snapshot", {"app": "TextEdit"})
    monkeypatch.setattr(server, "_app_at_point", lambda point: "com.openai.codex")  # floating window on top
    monkeypatch.setattr(observe, "press_element", lambda element: True)  # the AXPress reaches the element
    result = await call_tool(mcp_server, "click", {"ref": "e2"})
    assert not result.isError, result.content[0].text
    assert mocked_driver["click"] == []  # no synthesized mouse event was needed
    monkeypatch.setattr(observe, "press_element", lambda element: False)  # no AX action: synthesized click
    result = await call_tool(mcp_server, "click", {"ref": "e2"})
    assert result.isError and "focus_changed" in result.content[0].text  # the HID path keeps its guard
    assert mocked_driver["click"] == []


async def test_focus_reports_a_window_covered_by_another_app(bg, monkeypatch) -> None:
    srv, rt, driver, store = bg
    store.set_tier(APP, safety.Tier.CLICK)
    driver.activate_app = lambda name: None
    driver.windows = lambda: [{"app": APP, "pid": 4242, "title": "Untitled", "bounds": {"display_id": 1, "x": 100, "y": 100, "width": 800, "height": 600}}]
    monkeypatch.setattr(rt, "_wait_frontmost", lambda bundle, wait, pid=None: True)
    monkeypatch.setattr(server, "_app_at_point", lambda point: "com.openai.codex" if (point.x, point.y) == (500, 400) else APP)
    result = await call_tool(srv, "app", {"action": "focus", "name": APP})
    text = result.content[0].text
    assert text.startswith(f"focused {APP}, but its window is covered by com.openai.codex")
    monkeypatch.setattr(server, "_app_at_point", lambda point: APP)
    result = await call_tool(srv, "app", {"action": "focus", "name": APP})
    assert result.content[0].text == f"focused {APP}"


async def test_window_list_for_an_app_includes_its_windows_on_other_spaces(bg, monkeypatch) -> None:
    """Issue #13: the snapshot read a Chrome window while window list said []."""
    srv, rt, driver, store = bg
    store.set_tier(APP, safety.Tier.READ)
    driver.windows = lambda: []  # nothing of the app on this Space
    monkeypatch.setattr(rt, "_resolve_app", lambda ident: (None, APP))
    monkeypatch.setattr(server, "_windows_all_spaces",
                        lambda bundle, with_titles=False: [(105, Bounds(1, 0, 160, 3024, 1804), "X Library")])
    result = await call_tool(srv, "window", {"action": "list", "app": APP})
    rows = json.loads(result.content[0].text)
    assert rows == [{"window_id": 105, "app": APP, "title": "X Library", "on_screen": False,
                     "bounds": {"display_id": 1, "x": 0, "y": 160, "width": 3024, "height": 1804}}]

