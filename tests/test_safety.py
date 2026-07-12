"""Safety-layer tests: tier matrix, deny/allow lists, the NEEDS_PERMISSION
path, config round-trip, and audit logging with secure-field redaction.

Everything here is filesystem + in-memory only — no TCC-gated calls. The
`frontmost_app` tests use NSWorkspace, which needs no TCC grant, plus a
simulated AppKit-missing environment for the graceful-degradation path.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from computeruse.safety import (
    REDACTED,
    AuditLog,
    PermissionStore,
    Tier,
    Verdict,
    check_action,
    confirmation_prompt,
    frontmost_app,
    required_tier,
)
from computeruse.schema import (
    AppOp,
    AppVerb,
    Bounds,
    Click,
    ClipboardOp,
    ClipboardVerb,
    Drag,
    Element,
    KeyChord,
    ObserveOp,
    ObserveVerb,
    Point,
    Scroll,
    TypeText,
    WaitFor,
    WindowOp,
    WindowVerb,
)
from tests.conftest import build_synthetic_snapshot

APP = "com.apple.TextEdit"
POINT = Point(display_id=1, x=300, y=170)

OBSERVE = ObserveOp(verb=ObserveVerb.SNAPSHOT, app=APP)
CLICK = Click(target=POINT)
TYPE = TypeText(text="hello")
KEY = KeyChord(chord="cmd+s")
PASTE = ClipboardOp(verb=ClipboardVerb.WRITE, text="hello")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox HOME so default config/audit paths land in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# required_tier classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "required"),
    [
        (OBSERVE, Tier.READ),
        (ObserveOp(verb=ObserveVerb.SCREENSHOT), Tier.READ),
        (ObserveOp(verb=ObserveVerb.ZOOM), Tier.READ),
        (WaitFor(target=build_synthetic_snapshot().element("e2")), Tier.READ),
        (WindowOp(verb=WindowVerb.LIST), Tier.READ),
        (AppOp(verb=AppVerb.LIST), Tier.READ),
        (ClipboardOp(verb=ClipboardVerb.READ), Tier.READ),
        (CLICK, Tier.CLICK),
        (Drag(start=POINT, end=Point(1, 500, 500)), Tier.CLICK),
        (Scroll(target=POINT, dy=3), Tier.CLICK),
        (WindowOp(verb=WindowVerb.RAISE, window_id=7), Tier.CLICK),
        (AppOp(verb=AppVerb.FOCUS, app=APP), Tier.CLICK),
        (TYPE, Tier.FULL),
        (KEY, Tier.FULL),
        (PASTE, Tier.FULL),  # clipboard-paste is a typing path
    ],
)
def test_required_tier(action, required: Tier) -> None:
    assert required_tier(action) is required


def test_required_tier_rejects_non_actions() -> None:
    with pytest.raises(TypeError):
        required_tier(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# check_action: tier matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("granted", "action", "allowed"),
    [
        # read tier: observe-level only
        (Tier.READ, OBSERVE, True),
        (Tier.READ, CLICK, False),
        (Tier.READ, TYPE, False),
        (Tier.READ, KEY, False),
        # click tier: + pointer, still no text/key injection
        (Tier.CLICK, OBSERVE, True),
        (Tier.CLICK, CLICK, True),
        (Tier.CLICK, TYPE, False),
        (Tier.CLICK, KEY, False),
        (Tier.CLICK, PASTE, False),
        # full tier: everything
        (Tier.FULL, OBSERVE, True),
        (Tier.FULL, CLICK, True),
        (Tier.FULL, TYPE, True),
        (Tier.FULL, KEY, True),
        (Tier.FULL, PASTE, True),
    ],
)
def test_tier_matrix(home: Path, granted: Tier, action, allowed: bool) -> None:
    store = PermissionStore()
    store.set_tier(APP, granted)
    decision = check_action(action, APP, store=store)
    assert decision.allowed is allowed
    assert decision.app == APP
    assert decision.granted is granted
    if not allowed:
        assert decision.verdict is Verdict.DENY
        # the reason names both sides of the tier gap
        assert decision.required.value in decision.reason
        assert granted.value in decision.reason


def test_unknown_app_needs_permission(home: Path) -> None:
    decision = check_action(CLICK, "com.example.unknown", store=PermissionStore())
    assert decision.verdict is Verdict.NEEDS_PERMISSION
    assert not decision.allowed
    assert decision.granted is None
    assert decision.required is Tier.CLICK
    # actionable for the host: names the app and the tier to approve
    assert "com.example.unknown" in decision.reason
    assert "click" in decision.reason
    assert decision.to_dict() == {
        "verdict": "needs_permission",
        "app": "com.example.unknown",
        "required": "click",
        "granted": None,
        "reason": decision.reason,
    }


def test_denylist_beats_granted_tier(home: Path) -> None:
    store = PermissionStore()
    store.set_tier(APP, Tier.FULL)
    store.add_deny(APP)
    assert store.is_denied(APP)
    decision = check_action(OBSERVE, APP, store=store)
    assert decision.verdict is Verdict.DENY
    assert "deny list" in decision.reason


def test_nonempty_allowlist_is_a_whitelist(home: Path) -> None:
    store = PermissionStore()
    store.add_allow(APP)
    store.set_tier("com.other.app", Tier.FULL)
    assert not store.is_denied(APP)
    assert store.is_denied("com.other.app")
    assert check_action(CLICK, "com.other.app", store=store).verdict is Verdict.DENY


def test_check_action_default_store_reads_home_config(home: Path) -> None:
    PermissionStore().set_tier(APP, Tier.FULL)
    assert check_action(TYPE, APP).allowed


# ---------------------------------------------------------------------------
# PermissionStore persistence
# ---------------------------------------------------------------------------


def test_config_round_trip(home: Path) -> None:
    store = PermissionStore()
    assert store.path == home / ".computeruse" / "permissions.json"
    assert not store.path.exists(), "file is created on first write, not load"
    assert store.get_tier(APP) is None, "ungranted apps default to ask"

    store.set_tier(APP, Tier.CLICK)
    store.add_deny("com.example.banking")
    store.add_allow(APP)
    data = json.loads(store.path.read_text())
    assert data == {
        "apps": {APP: {"tier": "click"}},
        "deny": ["com.example.banking"],
        "allow": [APP],
    }

    reloaded = PermissionStore()
    assert reloaded.get_tier(APP) is Tier.CLICK
    assert reloaded.is_denied("com.example.banking")
    assert not reloaded.is_denied(APP)

    reloaded.revoke(APP)
    assert PermissionStore().get_tier(APP) is None


def test_store_honors_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "perms.json"
    PermissionStore(path).set_tier(APP, Tier.READ)
    assert PermissionStore(path).get_tier(APP) is Tier.READ


def test_store_picks_up_external_edits(tmp_path: Path) -> None:
    """A user editing permissions.json mid-session (revoke, deny) must take
    effect on the next check in an already-running server."""
    path = tmp_path / "perms.json"
    store = PermissionStore(path)
    store.set_tier(APP, Tier.FULL)
    assert not store.is_denied(APP)

    external = json.loads(path.read_text())
    external["deny"] = [APP]
    del external["apps"][APP]
    path.write_text(json.dumps(external))

    assert store.is_denied(APP), "external deny must apply without a reload"
    assert store.get_tier(APP) is None, "external revoke must apply too"

    path.unlink()
    assert not store.is_denied(APP), "a deleted config is an empty store"


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------


def test_audit_entry_written_and_shaped(home: Path) -> None:
    store = PermissionStore()
    store.set_tier(APP, Tier.FULL)
    decision = check_action(TYPE, APP, store=store)

    log = AuditLog()
    path = log.record_action(TYPE, app=APP, decision=decision, result="ok")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert path == home / ".computeruse" / "audit" / f"{today}.jsonl"

    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["app"] == APP
    assert entry["action"] == "typetext"
    assert entry["params"]["text"] == "hello", "non-secure entries stay replayable"
    assert entry["decision"] == decision.to_dict()
    assert entry["result"] == "ok"
    assert isinstance(entry["ts"], float)


def test_audit_appends_within_a_day(home: Path) -> None:
    log = AuditLog()
    first = log.record({"result": "ok"})
    second = log.record({"result": "ok"})
    assert first == second
    assert len(first.read_text().splitlines()) == 2


def test_audit_rotates_by_utc_day(tmp_path: Path) -> None:
    clock = {"ts": 1_752_300_000.0}
    log = AuditLog(tmp_path / "audit", now=lambda: clock["ts"])
    first = log.record({"result": "ok"})
    clock["ts"] += 86_400.0
    second = log.record({"result": "ok"})
    assert first != second, "a new UTC day means a new file"
    for ts, path in ((1_752_300_000.0, first), (1_752_386_400.0, second)):
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        assert path.name == f"{day}.jsonl"
        assert len(path.read_text().splitlines()) == 1


@pytest.mark.parametrize(
    ("action", "param"),
    [
        (TypeText(text="hunter2-secret"), "text"),
        (KeyChord(chord="hunter2-secret"), "chord"),
        (ClipboardOp(verb=ClipboardVerb.WRITE, text="hunter2-secret"), "text"),
    ],
)
def test_audit_redacts_secure_field_content(home: Path, action, param: str) -> None:
    store = PermissionStore()
    store.set_tier(APP, Tier.FULL)
    decision = check_action(action, APP, store=store)

    path = AuditLog().record_action(
        action, app=APP, decision=decision, result="secure_field", secure=True
    )
    raw = path.read_text()
    assert "hunter2-secret" not in raw, "secrets must never reach disk"
    entry = json.loads(raw.splitlines()[-1])
    assert entry["params"][param] == REDACTED
    assert entry["result"] == "secure_field"


def test_audit_always_redacts_clipboard_write_text(home: Path) -> None:
    """Clipboard writes are how an agent stages a secret before pasting; the
    text never reaches disk even when nobody flagged the action secure."""
    action = ClipboardOp(verb=ClipboardVerb.WRITE, text="hunter2-secret")
    store = PermissionStore()
    store.set_tier(APP, Tier.FULL)
    decision = check_action(action, APP, store=store)

    path = AuditLog().record_action(action, app=APP, decision=decision, result="ok")
    raw = path.read_text()
    assert "hunter2-secret" not in raw
    assert json.loads(raw.splitlines()[-1])["params"]["text"] == REDACTED


def test_audit_redacts_element_values_inside_targets(home: Path) -> None:
    """A clicked field may hold a revealed secret in `value`; the audit entry
    keeps the re-resolution anchor (role/title/path/bounds), never the value."""
    field = build_synthetic_snapshot().element("e3")  # value="hello"
    action = Click(target=field)
    store = PermissionStore()
    store.set_tier(APP, Tier.FULL)
    decision = check_action(action, APP, store=store)

    path = AuditLog().record_action(action, app=APP, decision=decision, result="ok")
    entry = json.loads(path.read_text().splitlines()[-1])
    assert entry["params"]["target"]["value"] == REDACTED
    assert entry["params"]["target"]["role"] == "AXTextArea", "anchor fields survive"
    assert entry["params"]["target"]["title"] == "Document body"


# ---------------------------------------------------------------------------
# Frontmost-app hit-test helper
# ---------------------------------------------------------------------------


def test_frontmost_app_shape() -> None:
    # NSWorkspace needs no TCC grant, so this runs even on ungranted machines.
    bundle_id, pid = frontmost_app()
    assert bundle_id is None or isinstance(bundle_id, str)
    assert pid is None or isinstance(pid, int)
    if sys.platform == "darwin" and pid is not None:
        assert pid > 0


def test_frontmost_app_degrades_without_appkit(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes `from AppKit import ...` raise
    # ImportError — the non-macOS / missing-pyobjc path, structured not fatal.
    monkeypatch.setitem(sys.modules, "AppKit", None)
    assert frontmost_app() == (None, None)


# ---------------------------------------------------------------------------
# confirmation_prompt — the irreversible-action heuristic (COM-10)
# ---------------------------------------------------------------------------


def _clickable(title: str) -> Element:
    return Element(
        ref="e2",
        role="AXButton",
        title=title,
        value=None,
        bounds=Bounds(1, 10, 10, 100, 40),
        snapshot_id="snap-x",
        clickable=True,
    )


@pytest.mark.parametrize(
    "label",
    [
        "Delete",
        "Delete Message",
        "Move to Trash",
        "Empty Trash",
        "Discard Changes",
        "Erase Disk…",
        "Uninstall",
        "Don't Save",
        "Permanently Erase",
        "WIPE DEVICE",  # case-insensitive
    ],
)
def test_confirmation_prompt_flags_destructive_labels(label: str) -> None:
    prompt = confirmation_prompt(Click(target=_clickable(label)), APP)
    assert prompt is not None
    assert label in prompt and APP in prompt


@pytest.mark.parametrize(
    "label",
    ["Save", "OK", "Cancel", "Reply", "Send", "Add", "Remove Filter", "Reset Zoom", ""],
)
def test_confirmation_prompt_ignores_safe_labels(label: str) -> None:
    # "Send"/"Remove"/"Reset" are intentionally NOT in the destructive set to
    # avoid nagging on common safe buttons.
    assert confirmation_prompt(Click(target=_clickable(label)), APP) is None


def test_confirmation_prompt_ignores_coordinate_clicks() -> None:
    # A raw point has no label to key the heuristic on.
    assert confirmation_prompt(CLICK, APP) is None


def test_confirmation_prompt_ignores_non_click_actions() -> None:
    assert confirmation_prompt(TYPE, APP) is None
    assert confirmation_prompt(KEY, APP) is None
    assert confirmation_prompt(OBSERVE, APP) is None
