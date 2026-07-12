"""Safety layer: per-app grants, action tiers, and the always-on audit log.

Wraps EVERY action (PLAN.md §6): the server calls `check_action` before any
driver executes and records the outcome through `AuditLog`. Slim Safety v1
(PLAN.md §4): per-app read/click/full tiers with an "ask" default for
ungranted apps, deny/allow lists, a structured NEEDS_PERMISSION decision the
MCP layer renders as an actionable error string (no GUI in the MVP), and a
day-rotated JSONL audit log that never writes secure-field content to disk.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from computeruse.schema import (
    Action,
    AppOp,
    AppVerb,
    Click,
    ClipboardOp,
    ClipboardVerb,
    Drag,
    Element,
    KeyChord,
    ObserveOp,
    Scroll,
    TypeText,
    WaitFor,
    WindowOp,
    WindowVerb,
    action_to_dict,
)

#: Placeholder written into audit entries in place of secure-field content.
REDACTED = "[REDACTED]"

#: Param names that carry injectable content and are redacted when an action
#: touches a secure field (`AuditLog.record_action` with ``secure=True``).
_SENSITIVE_PARAMS: frozenset[str] = frozenset({"text", "chord"})


class Tier(str, Enum):
    """Per-app permission tiers, least to most privileged."""

    READ = "read"  #: observe/screenshot only; no input
    CLICK = "click"  #: READ + pointer actions; no typing/keys
    FULL = "full"  #: everything


_TIER_RANK: dict[Tier, int] = {Tier.READ: 0, Tier.CLICK: 1, Tier.FULL: 2}


def required_tier(action: Action) -> Tier:
    """The minimum tier an app must hold for ``action`` to run.

    The read/click/full matrix:

    * READ — pure observation: `ObserveOp` (snapshot/screenshot/zoom),
      `WaitFor` (tree polling), LIST-verb window/app ops, clipboard reads.
      Note the clipboard is cross-app: a READ grant on the frontmost app
      exposes whatever the user last copied anywhere.
    * CLICK — pointer input and window/app manipulation: `Click`, `Drag`,
      `Scroll`, non-LIST `WindowOp`/`AppOp` verbs.
    * FULL — text/key injection: `TypeText`, `KeyChord`, and clipboard
      *writes* (the clipboard-paste fast path is a typing path; gating it
      below FULL would let a click-tier app receive injected text).
    """
    if isinstance(action, (TypeText, KeyChord)):
        return Tier.FULL
    if isinstance(action, ClipboardOp):
        return Tier.FULL if action.verb is ClipboardVerb.WRITE else Tier.READ
    if isinstance(action, (Click, Drag, Scroll)):
        return Tier.CLICK
    if isinstance(action, WindowOp):
        return Tier.READ if action.verb is WindowVerb.LIST else Tier.CLICK
    if isinstance(action, AppOp):
        return Tier.READ if action.verb is AppVerb.LIST else Tier.CLICK
    if isinstance(action, (WaitFor, ObserveOp)):
        return Tier.READ
    raise TypeError(f"not a schema.Action: {type(action).__name__}")


class PermissionStore:
    """Persistent per-app grants (bundle id -> `Tier`) plus deny/allow lists.

    Backed by a JSON config, default ``~/.computeruse/permissions.json``::

        {"apps": {"com.apple.TextEdit": {"tier": "full"}},
         "deny": ["com.example.banking"],
         "allow": []}

    Semantics:

    * Apps without an explicit grant default to **ask**: `get_tier` returns
      None and `check_action` returns a NEEDS_PERMISSION decision for the
      host to surface to the human — never a silent failure.
    * ``deny`` always wins: a denied app is blocked even if it holds a tier.
    * A non-empty ``allow`` list is a whitelist: apps not on it are denied.

    Mutations persist immediately; the file is created on first write.
    External edits are picked up on the next read (`_refresh`), so a user
    adding a runaway app to ``deny`` mid-session takes effect immediately in
    a long-lived server.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Load the grant store at ``path`` (default under ``~``); a missing
        file is an empty store."""
        self.path = path if path is not None else Path.home() / ".computeruse" / "permissions.json"
        self._load()

    def _load(self) -> None:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._stamp = self._file_stamp()
        self._tiers: dict[str, Tier] = {
            app: Tier(entry["tier"]) for app, entry in data.get("apps", {}).items()
        }
        self._deny: set[str] = set(data.get("deny", ()))
        self._allow: set[str] = set(data.get("allow", ()))

    def _file_stamp(self) -> tuple[int, int] | None:
        """(mtime_ns, size) of the config file; None when it does not exist."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _refresh(self) -> None:
        """Reload when the file changed on disk since the last load/save."""
        if self._file_stamp() != self._stamp:
            self._load()

    def get_tier(self, bundle_id: str) -> Tier | None:
        """Granted tier for ``bundle_id``; None means ungranted ("ask")."""
        self._refresh()
        return self._tiers.get(bundle_id)

    def set_tier(self, bundle_id: str, tier: Tier) -> None:
        """Record a human-approved grant and persist it."""
        self._refresh()
        self._tiers[bundle_id] = tier
        self._save()

    def revoke(self, bundle_id: str) -> None:
        """Remove any grant for ``bundle_id`` (back to the "ask" default)."""
        self._refresh()
        self._tiers.pop(bundle_id, None)
        self._save()

    def add_deny(self, bundle_id: str) -> None:
        """Put ``bundle_id`` on the deny list; beats any granted tier."""
        self._refresh()
        self._deny.add(bundle_id)
        self._save()

    def add_allow(self, bundle_id: str) -> None:
        """Put ``bundle_id`` on the allow list; a non-empty allow list
        denies every app not on it."""
        self._refresh()
        self._allow.add(bundle_id)
        self._save()

    def is_denied(self, bundle_id: str) -> bool:
        """True when the deny/allow lists block ``bundle_id`` outright."""
        self._refresh()
        if bundle_id in self._deny:
            return True
        return bool(self._allow) and bundle_id not in self._allow

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "apps": {app: {"tier": tier.value} for app, tier in sorted(self._tiers.items())},
            "deny": sorted(self._deny),
            "allow": sorted(self._allow),
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n")
        self._stamp = self._file_stamp()


class Verdict(str, Enum):
    """Outcome of one `check_action` gate. Values are wire-stable: they
    appear in MCP tool results and the JSONL audit log."""

    ALLOW = "allow"
    DENY = "deny"  #: deny/allow lists, or the granted tier is too low
    NEEDS_PERMISSION = "needs_permission"  #: ungranted app — ask the human


@dataclass(frozen=True, slots=True)
class Decision:
    """Structured result of gating one action against one app.

    The server surfaces non-ALLOW decisions to the host; ``reason`` is the
    actionable human-readable rendering (the MCP layer has no GUI in MVP).

    Attributes:
        verdict: Allow, deny, or ask-the-human.
        app: Bundle id the action was gated against.
        required: Minimum tier the action needs (`required_tier`).
        granted: The app's granted tier; None when ungranted.
        reason: One actionable sentence explaining the verdict.
    """

    verdict: Verdict
    app: str
    required: Tier
    granted: Tier | None
    reason: str

    @property
    def allowed(self) -> bool:
        """Whether the driver may execute the action."""
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> dict[str, object]:
        """Wire form used in MCP tool results and the audit log."""
        return {
            "verdict": self.verdict.value,
            "app": self.app,
            "required": self.required.value,
            "granted": self.granted.value if self.granted is not None else None,
            "reason": self.reason,
        }


def check_action(action: Action, target_app: str, *, store: PermissionStore | None = None) -> Decision:
    """Gate ``action`` against ``target_app``'s grant; called before EVERY act.

    Returns a `Decision` instead of raising: DENY and NEEDS_PERMISSION are
    states the model/human react to, not driver failures. The caller records
    the decision via `AuditLog.record_action` and, at act() time, combines it
    with a same-window recheck between decision and injection (PLAN.md §6 —
    toasts and overlays can race the click; the server implements this via
    its frontmost/hit-test recheck and raises `ErrorCode.FOCUS_CHANGED`).

    Args:
        action: The action to gate (any member of `schema.Action`).
        target_app: Bundle id of the app the action lands on.
        store: Grant store; defaults to the one at the standard config path.
    """
    if store is None:
        store = PermissionStore()
    required = required_tier(action)
    kind = type(action).__name__.lower()
    granted = store.get_tier(target_app)
    if store.is_denied(target_app):
        return Decision(
            verdict=Verdict.DENY,
            app=target_app,
            required=required,
            granted=granted,
            reason=f"{target_app} is on the deny list; no actions are permitted",
        )
    if granted is None:
        return Decision(
            verdict=Verdict.NEEDS_PERMISSION,
            app=target_app,
            required=required,
            granted=None,
            reason=(
                f"{target_app} has no permission grant; ask the user to approve "
                f"tier '{required.value}' (or higher) for this app, then retry"
            ),
        )
    if _TIER_RANK[granted] < _TIER_RANK[required]:
        return Decision(
            verdict=Verdict.DENY,
            app=target_app,
            required=required,
            granted=granted,
            reason=(
                f"{kind} requires tier '{required.value}' but {target_app} "
                f"is granted '{granted.value}'"
            ),
        )
    return Decision(
        verdict=Verdict.ALLOW,
        app=target_app,
        required=required,
        granted=granted,
        reason=f"{kind} permitted at tier '{granted.value}'",
    )


#: Substrings (matched case-insensitively against a click target's label) that
#: flag a plausibly irreversible action. Deliberately conservative — a
#: false-negative just means "no extra prompt", but a false-positive nags the
#: user on a safe click. Word-ish, clearly-destructive verbs only; "send",
#: "remove", "reset" are intentionally excluded to avoid over-triggering.
_DESTRUCTIVE_LABEL_SUBSTRINGS: tuple[str, ...] = (
    "delete",
    "move to trash",
    "empty trash",
    "trash",
    "discard",
    "erase",
    "uninstall",
    "permanently",
    "wipe",
    "don't save",
    "don’t save",  # curly apostrophe — the label AppKit actually renders
)


def confirmation_prompt(action: Action, target_app: str) -> str | None:
    """One human-readable confirmation question, or None if none is warranted.

    The tier gate answers "is this app allowed to click?"; this answers the
    orthogonal "should a human explicitly okay *this* click first?" for
    plausibly irreversible actions (PLAN.md §8 confirmation gates). MVP scope:
    a label heuristic on click targets — the destructive buttons users fear an
    agent misfiring on (Delete, Move to Trash, Discard...). Only ref-resolved
    `Click`s carry a label; coordinate clicks and every non-click action return
    None (nothing to key the heuristic on).

    Returns:
        A confirmation question to route to the host (e.g. via MCP
        elicitation), or None when the action needs no extra confirmation.
    """
    if not isinstance(action, Click) or not isinstance(action.target, Element):
        return None
    title = action.target.title.strip()
    lowered = title.lower()
    match = next((kw for kw in _DESTRUCTIVE_LABEL_SUBSTRINGS if kw in lowered), None)
    if match is None:
        return None
    return (
        f'Confirm a potentially irreversible action: click "{title}" in '
        f"{target_app}? (matched “{match}”)"
    )


def frontmost_app() -> tuple[str | None, int | None]:
    """(bundle id, pid) of the frontmost application, or (None, None).

    The act()-time hit-test anchor (PLAN.md §6): callers decide against this
    value and MUST re-read it immediately before injection — the frontmost
    app can change between decision and injection (same-window recheck).
    Uses NSWorkspace, which needs no TCC grant, so this degrades gracefully
    (returns (None, None)) only when AppKit itself is unavailable.
    """
    try:
        from AppKit import NSWorkspace
    except ImportError:  # non-macOS, or pyobjc missing
        return None, None
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None, None
    bundle_id = app.bundleIdentifier()
    return (str(bundle_id) if bundle_id is not None else None), int(app.processIdentifier())


def _redact(params: dict[str, object]) -> dict[str, object]:
    """Replace injectable-content params with `REDACTED`, keeping structure."""
    return {
        key: REDACTED if key in _SENSITIVE_PARAMS and value is not None else value
        for key, value in params.items()
    }


def _redact_target_values(params: dict[str, object]) -> dict[str, object]:
    """Strip element ``value``s from serialized targets, keeping the anchor.

    A click/wait target can carry a revealed password or SSN in its
    ``value``; the audit log only needs the re-resolution anchor
    (role/title/path/bounds), so target values never reach disk.
    """
    redacted = dict(params)
    for key in ("target", "start", "end"):
        target = redacted.get(key)
        if isinstance(target, dict) and target.get("value") is not None:
            redacted[key] = {**target, "value": REDACTED}
    return redacted


class AuditLog:
    """Always-on JSONL audit log: one file per UTC day, one line per action.

    Files live at ``<dir>/YYYY-MM-DD.jsonl`` (default ``~/.computeruse/audit``);
    rotation is implicit — every entry is appended to the file named for the
    current UTC day. The trajectory format doubles as a demonstration
    recording (PLAN.md §9 Phase 3: teach & replay), so non-secure entries keep
    full action params; secure-field content is always redacted.
    """

    def __init__(self, dir_path: Path | None = None, *, now: Callable[[], float] = time.time) -> None:
        """Log into ``dir_path`` (default under ``~``); ``now`` is injectable
        for rotation tests."""
        self.dir_path = dir_path if dir_path is not None else Path.home() / ".computeruse" / "audit"
        self._now = now

    def record(self, event: Mapping[str, object]) -> Path:
        """Append one event (already serialized, e.g. via
        `schema.action_to_dict`) as a JSON line.

        Adds ``ts`` (epoch seconds) when absent; the target file is named for
        that timestamp's UTC day. Returns the path written, for callers that
        surface "logged to ..." breadcrumbs.
        """
        entry = dict(event)
        entry.setdefault("ts", self._now())
        day = datetime.fromtimestamp(float(entry["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
        path = self.dir_path / f"{day}.jsonl"
        self.dir_path.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return path

    def record_action(
        self,
        action: Action,
        *,
        app: str,
        decision: Decision,
        result: str,
        secure: bool = False,
    ) -> Path:
        """Record one gated action attempt in the standard entry shape.

        Entry fields: ``ts``, ``app``, ``action`` (kind), ``params``,
        ``decision`` (`Decision.to_dict`), ``result`` (e.g. ``"ok"`` or an
        `schema.ErrorCode` value). Callers MUST pass ``secure=True`` whenever
        the target/focused element is a secure field or secure event input is
        active; every injectable-content param is then replaced with
        `REDACTED` so secrets never reach disk. Two redactions are
        unconditional: clipboard-write text (the standard staging path for a
        secret about to be pasted) and element ``value``s inside serialized
        targets (a clicked field may hold a revealed secret).
        """
        payload = action_to_dict(action)
        kind = payload.pop("kind")
        if secure or isinstance(action, ClipboardOp):
            payload = _redact(payload)
        payload = _redact_target_values(payload)
        return self.record(
            {
                "ts": self._now(),
                "app": app,
                "action": kind,
                "params": payload,
                "decision": decision.to_dict(),
                "result": result,
            }
        )
