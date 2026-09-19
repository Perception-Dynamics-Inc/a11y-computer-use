"""Safety layer: per-app grants, action tiers, and the always-on audit log.

Wraps EVERY action (PLAN.md §6): the server calls `check_action` before any
driver executes and records the outcome through `AuditLog`. Slim Safety v1
(PLAN.md §4): per-app read/click/full tiers with an "ask" default for
ungranted apps, deny/allow lists, a structured NEEDS_PERMISSION decision the
MCP layer renders as an actionable error string (no GUI in the MVP), and a
day-rotated JSONL audit log that never writes secure-field content to disk.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from a11y_computer_use.schema import (
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
    FileDialogOp,
    MenuOp,
    MenuVerb,
    WebMcpOp,
    WebMcpVerb,
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


def _sync_directory(path: Path) -> None:
    """Persist directory changes on POSIX; Windows has no directory fsync."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_file(source: Path, target: Path, *, timeout: float = 1.0) -> None:
    """Replace atomically, allowing brief Windows reader/sharing conflicts.

    Ordinary Windows file readers can temporarily deny replacement. Keep the
    old file intact while retrying; persistent access errors still propagate
    after a bounded wait, and other filesystem errors fail immediately.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.01, remaining))


@contextmanager
def _file_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Serialize local processes using a stable sidecar file, with a deadline.

    The sidecar must never be removed: replacing a locked inode would allow
    another writer to acquire a different lock for the same data file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")

            def acquire() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def release() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire() -> None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release() -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock: {path}") from exc
                time.sleep(0.01)
        yield
    finally:
        try:
            if acquired:
                release()
        finally:
            os.close(fd)


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
        if action.verb is AppVerb.LIST:
            return Tier.READ
        # Quit sends cmd+q, a key injection, so it sits with the typing tier.
        return Tier.FULL if action.verb is AppVerb.QUIT else Tier.CLICK
    if isinstance(action, MenuOp):
        # list and state observe; press and close act on the menu bar.
        return Tier.READ if action.verb in (MenuVerb.LIST, MenuVerb.STATE) else Tier.CLICK
    if isinstance(action, FileDialogOp):
        return Tier.FULL  # it types a path and a filename
    if isinstance(action, WebMcpOp):
        # Listing observes. Calling is an activation like a click, unless the
        # tool takes free text or names a payment / submission, which is the
        # typing tier (see `webmcp_sensitive`).
        if action.verb is WebMcpVerb.LIST:
            return Tier.READ
        return Tier.FULL if action.sensitive else Tier.CLICK
    if isinstance(action, (WaitFor, ObserveOp)):
        return Tier.READ
    raise TypeError(f"not a schema.Action: {type(action).__name__}")


#: Tool-name words that mark a WebMCP call as payment or submission: the call
#: sends something out of the page, so it is gated at FULL like typing.
_WEBMCP_SENSITIVE_WORDS: frozenset[str] = frozenset({
    "submit", "send", "post", "publish", "pay", "payment", "purchase", "buy",
    "checkout", "order", "book", "reserve", "transfer", "donate", "subscribe",
    "signup", "register", "login", "signin", "message", "email", "reply",
    "comment", "upload", "delete", "remove",
})


def _schema_takes_free_text(schema: object) -> bool:
    """True when a JSON schema has a string property with no enum, or a
    string that is itself unconstrained: an argument the model composes."""
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "string" and not schema.get("enum") and not schema.get("const"):
        return True
    props = schema.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if _schema_takes_free_text(prop):
                return True
    items = schema.get("items")
    if isinstance(items, dict) and _schema_takes_free_text(items):
        return True
    for key in ("anyOf", "oneOf", "allOf"):
        alts = schema.get(key)
        if isinstance(alts, list) and any(_schema_takes_free_text(a) for a in alts):
            return True
    return False


def webmcp_sensitive(name: str, input_schema: object) -> bool:
    """Whether calling WebMCP tool ``name`` needs the FULL tier.

    The rule: FULL when the tool's input schema has a free-text string
    argument (no enum: the model composes text, which is text entry) or when
    the name contains a payment or submission word (`_WEBMCP_SENSITIVE_WORDS`,
    matched on the name split at underscores, dashes, dots, and camelCase);
    otherwise CLICK, an activation with fixed choices. Unknown schemas
    (None) are treated as free text, the conservative side.
    """
    if input_schema is None or _schema_takes_free_text(input_schema):
        return True
    words = _name_words(name)
    return any(w in _WEBMCP_SENSITIVE_WORDS for w in words)


def _name_words(name: str) -> list[str]:
    out: list[str] = []
    word = ""
    for ch in name:
        if ch.isalnum():
            if word and ch.isupper() and not word[-1].isupper():
                out.append(word.lower())
                word = ""
            word += ch
        elif word:
            out.append(word.lower())
            word = ""
    if word:
        out.append(word.lower())
    return out


class PermissionStore:
    """Persistent per-app grants (bundle id -> `Tier`) plus deny/allow lists.

    Backed by a JSON config, default ``~/.a11y-computer-use/permissions.json``::

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
    Updates merge under a local-process lock and replace the file atomically.
    Invalid or unreadable config denies every action until repaired.
    External edits are picked up on the next read (`_refresh`), so a user
    adding a runaway app to ``deny`` mid-session takes effect immediately in
    a long-lived server.
    """

    def __init__(self, path: Path | None = None) -> None:
        """Load the grant store at ``path`` (default under ``~``); a missing
        file is an empty store."""
        self.path = path if path is not None else Path.home() / ".a11y-computer-use" / "permissions.json"
        self._lock = threading.RLock()
        self._load_error: str | None = None
        self._error_digest: bytes | None = None
        self._load()

    def _load(self) -> None:
        """Publish a complete valid policy, or an empty, denied policy.

        Read and stamp the same open file so an atomic replacement cannot
        mark old permissions as current. Invalid edits never retain grants.
        """
        self._tiers: dict[str, Tier] = {}
        self._deny: set[str] = set()
        self._allow: set[str] = set()
        self._stamp: tuple[int, ...] | None = None
        self._load_error = None
        before: tuple[int, ...] | None = None
        try:
            try:
                with self.path.open(encoding="utf-8") as fh:
                    before = self._stat_stamp(os.fstat(fh.fileno()))
                    data = json.load(fh)
                    stamp = self._stat_stamp(os.fstat(fh.fileno()))
                    if before != stamp:
                        raise ValueError("permission configuration changed during the read")
            except FileNotFoundError:
                data, stamp = {}, None
            if not isinstance(data, dict) or not isinstance(data.get("apps", {}), dict):
                raise ValueError("expected a JSON object with an apps object")
            tiers: dict[str, Tier] = {}
            for app, entry in data.get("apps", {}).items():
                self._validate_app(app)
                if not isinstance(entry, dict) or "tier" not in entry:
                    raise ValueError("each app must contain a tier")
                tiers[app] = Tier(entry["tier"])
            lists: dict[str, set[str]] = {}
            for name in ("deny", "allow"):
                values = data.get(name, [])
                if not isinstance(values, list):
                    raise ValueError(f"{name} must be a list of app identifiers")
                for app in values:
                    self._validate_app(app)
                lists[name] = set(values)
            self._tiers, self._deny, self._allow = tiers, lists["deny"], lists["allow"]
            self._stamp = stamp
        except (OSError, ValueError, TypeError, RecursionError):
            self._stamp = before
            self._load_error = "permission configuration is invalid or unreadable"
            # While the file is broken, metadata alone cannot be trusted to
            # notice a repair: a same-size rewrite within one filesystem
            # timestamp tick (common on Windows) keeps the stamp identical.
            # Remember the bytes, so _refresh reloads on any content change
            # and still never re-parses unchanged invalid content.
            self._error_digest = self._content_digest()

    @staticmethod
    def _validate_app(bundle_id: str) -> None:
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            raise ValueError("app identifier must be a non-empty string")

    @staticmethod
    def _stat_stamp(stat: os.stat_result) -> tuple[int, ...]:
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)

    def _content_digest(self) -> bytes | None:
        """SHA-256 of the file bytes; None for an absent or unreadable file."""
        try:
            return hashlib.sha256(self.path.read_bytes()).digest()
        except OSError:
            return None

    def _file_stamp(self) -> tuple[int, ...] | None:
        """Use the same metadata API as loading; None only for an absent file.

        On Windows/Python 3.12, stat reports creation time as ctime while
        fstat reports change time. Comparing those makes unchanged policies
        reload on every action, including repeatedly parsing invalid JSON.
        """
        try:
            with self.path.open("rb") as fh:
                return self._stat_stamp(os.fstat(fh.fileno()))
        except FileNotFoundError:
            return None

    def _refresh(self) -> None:
        """Reload when the file changed on disk since the last load/save."""
        try:
            changed = self._file_stamp() != self._stamp
        except OSError:
            changed = True
        if not changed and self._load_error is not None:
            changed = self._content_digest() != self._error_digest
        if changed:
            self._load()

    def policy(self, bundle_id: str) -> tuple[Tier | None, bool, str | None]:
        """Return grant, denial, and config error from one coherent policy."""
        with self._lock:
            self._refresh()
            denied = self._load_error is not None or bundle_id in self._deny or (
                bool(self._allow) and bundle_id not in self._allow
            )
            return self._tiers.get(bundle_id), denied, self._load_error

    def get_tier(self, bundle_id: str) -> Tier | None:
        """Granted tier for ``bundle_id``; None means ungranted ("ask")."""
        return self.policy(bundle_id)[0]

    def set_tier(self, bundle_id: str, tier: Tier) -> None:
        """Record a human-approved grant and persist it."""
        self._validate_app(bundle_id)
        tier = Tier(tier)

        def grant() -> None:
            self._tiers[bundle_id] = tier

        self._mutate(grant)

    def revoke(self, bundle_id: str) -> None:
        """Remove any grant for ``bundle_id`` (back to the "ask" default)."""
        self._validate_app(bundle_id)

        def remove() -> None:
            self._tiers.pop(bundle_id, None)

        self._mutate(remove)

    def add_deny(self, bundle_id: str) -> None:
        """Put ``bundle_id`` on the deny list; beats any granted tier."""
        self._validate_app(bundle_id)
        self._mutate(lambda: self._deny.add(bundle_id))

    def add_allow(self, bundle_id: str) -> None:
        """Put ``bundle_id`` on the allow list; a non-empty allow list
        denies every app not on it."""
        self._validate_app(bundle_id)
        self._mutate(lambda: self._allow.add(bundle_id))

    def is_denied(self, bundle_id: str) -> bool:
        """True when the deny/allow lists block ``bundle_id`` outright."""
        return self.policy(bundle_id)[1]

    def _mutate(self, update: Callable[[], None]) -> None:
        """Merge with the latest disk state while excluding other writers."""
        with self._lock, _file_lock(self.path.with_name(self.path.name + ".lock")):
            self._load()
            if self._load_error is not None:
                raise ValueError(f"{self._load_error}; repair {self.path} before changing grants")
            try:
                update()
                self._save()
            except BaseException:
                self._load()  # a failed save must not leave an unpersisted grant active
                raise

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "apps": {app: {"tier": tier.value} for app, tier in sorted(self._tiers.items())},
            "deny": sorted(self._deny),
            "allow": sorted(self._allow),
        }
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
                stamp = self._stat_stamp(os.fstat(fh.fileno()))
            _replace_file(temporary, self.path)
            _sync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        self._stamp = stamp


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
    granted, denied, config_error = store.policy(target_app)
    if denied:
        return Decision(
            verdict=Verdict.DENY,
            app=target_app,
            required=required,
            granted=granted,
            reason=(
                f"{config_error}; repair {store.path} before retrying"
                if config_error else f"{target_app} is on the deny list; no actions are permitted"
            ),
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
    if isinstance(action, MenuOp) and action.verb is MenuVerb.PRESS:
        # The last path component is the label the user would read.
        title = action.path.split(">")[-1].strip()
        lowered = title.lower()
        match = next((kw for kw in _DESTRUCTIVE_LABEL_SUBSTRINGS if kw in lowered), None)
        if match is None:
            return None
        return (
            f'Confirm a potentially irreversible action: choose the menu item "{title}" in '
            f"{target_app}? (matched \u201c{match}\u201d)"
        )
    if isinstance(action, WebMcpOp) and action.verb is WebMcpVerb.CALL:
        # A tool named delete_order or remove_item is the page's own word for
        # an irreversible step; the same keyword rule as a button label.
        name = action.name or ""
        lowered = " ".join(_name_words(name))
        match = next((kw for kw in _DESTRUCTIVE_LABEL_SUBSTRINGS if kw in lowered), None)
        if match is None:
            return None
        return (
            f'Confirm a potentially irreversible action: call the WebMCP tool "{name}" in '
            f"{target_app}? (matched \u201c{match}\u201d)"
        )
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


def refresh_workspace() -> None:
    """Let NSWorkspace catch up with LaunchServices before it is read.

    `NSWorkspace.runningApplications` and `frontmostApplication` are updated by
    notifications on the main thread's run loop. A process that never spins
    that loop (a CLI, an MCP server on asyncio) keeps reading the list it
    fetched first: an app launched a second ago is "not running", the app
    that just came to the front is not "frontmost", and every wait for either
    burns its whole timeout. One non-blocking pass of the loop (deadline in
    the past) delivers the pending updates: measured at 0.01 ms when nothing
    is pending and under 1 ms right after a launch. No-op off the main
    thread (the loop there carries no AppKit notifications) and off macOS.
    """
    if sys.platform != "darwin" or threading.current_thread() is not threading.main_thread():
        return
    try:
        from Foundation import NSDate, NSRunLoop
    except ImportError:
        return
    NSRunLoop.currentRunLoop().runMode_beforeDate_("kCFRunLoopDefaultMode", NSDate.distantPast())


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
    refresh_workspace()
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
    """Always-on JSONL audit log with bounded files and serialized appends.

    Files live at ``<dir>/YYYY-MM-DD.jsonl`` (default ``~/.a11y-computer-use/audit``);
    full daily files rotate to ``YYYY-MM-DD.<timestamp>-<id>.jsonl``. Defaults
    rotate new writes at 16 MiB, retain at most 32 files, and cap individual
    records at 64 KiB. Older files are deleted by normal count retention.
    Existing oversized logs are preserved until that eviction; the 512 MiB
    bound applies after those legacy files have been archived or evicted.
    Oversized records become explicit summaries, never silently broken JSON.
    Ordinary non-secure entries keep replayable params; secure content is
    redacted. Local processes sharing this directory serialize rotation and
    appends through a sidecar lock. Use separate directories per worker/tenant
    when isolation is required. Network filesystems are not supported.
    """

    def __init__(
        self,
        dir_path: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
        max_file_bytes: int = 16 * 1024 * 1024,
        max_files: int = 32,
        max_record_bytes: int = 64 * 1024,
        sync: bool = False,
        lock_timeout: float = 10.0,
    ) -> None:
        """Log into ``dir_path`` (default under ``~``); ``now`` is injectable
        for rotation tests. ``sync=True`` fsyncs each record for crash durability
        at a throughput cost; by default writes reach the OS before return.
        ``max_files=None`` is deliberately unsupported: retention is bounded.
        """
        for name, value in (("max_file_bytes", max_file_bytes), ("max_files", max_files),
                            ("max_record_bytes", max_record_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_record_bytes < 1024 or max_record_bytes > max_file_bytes:
            raise ValueError("max_record_bytes must be at least 1024 and at most max_file_bytes")
        if not 0 < lock_timeout < float("inf"):
            raise ValueError("lock_timeout must be positive and finite")
        self.dir_path = dir_path if dir_path is not None else Path.home() / ".a11y-computer-use" / "audit"
        self._now = now
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_record_bytes = max_record_bytes
        self.sync = sync
        self.lock_timeout = lock_timeout
        self._lock = threading.Lock()
        self._needs_prune = True

    def _encode(self, entry: dict[str, object]) -> bytes:
        """Encode a JSON line, or a bounded summary if a caller supplied huge params."""
        data = (json.dumps(entry, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        if len(data) <= self.max_record_bytes:
            return data
        original_bytes = len(data)
        # Keep verdict and outcome so summaries remain useful to audit/bench
        # consumers. Escaped Unicode is checked against the byte cap below.
        def label(value: object) -> str:
            return str(value)[:64]

        decision = entry.get("decision")
        summary = {
            "ts": entry["ts"],
            "app": label(entry.get("app", "")),
            "action": label(entry.get("action", "")),
            "result": label(entry.get("result", "")),
            "decision": {"verdict": label(decision.get("verdict", ""))}
            if isinstance(decision, dict) else None,
            "params": {"_truncated": True, "_original_bytes": original_bytes},
        }
        data = (json.dumps(summary, ensure_ascii=True) + "\n").encode("utf-8")
        if len(data) > self.max_record_bytes:
            # Pathological escaped identifiers must not defeat the byte cap.
            data = (json.dumps({"ts": entry["ts"], "audit_truncated": True,
                                "original_bytes": original_bytes}) + "\n").encode("utf-8")
        return data

    def _prune(self, current: Path) -> None:
        """Keep only our newest files, never deleting unrelated JSONL data."""
        files = [
            path for path in self.dir_path.glob("*.jsonl")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:\.\d+-[0-9a-f]{8})?\.jsonl", path.name)
            and path != current
        ]
        ordered = sorted(((path, path.stat()) for path in files),
                         key=lambda item: (item[1].st_mtime_ns, item[0].name), reverse=True)
        for path, _stat in ordered[self.max_files - 1:]:
            path.unlink()

    @staticmethod
    def _repair_tail(fd: int) -> int:
        """Discard an interrupted final record; return the intact file size.

        Our records always end with a newline. Scan backward in bounded chunks
        only when a killed writer left a partial line, preserving earlier rows.
        """
        size = os.fstat(fd).st_size
        if not size:
            return 0
        os.lseek(fd, size - 1, os.SEEK_SET)
        if os.read(fd, 1) == b"\n":
            return size
        end = size
        while end:
            start = max(0, end - 64 * 1024)
            os.lseek(fd, start, os.SEEK_SET)
            chunk = os.read(fd, end - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                size = start + newline + 1
                os.ftruncate(fd, size)
                return size
            end = start
        os.ftruncate(fd, 0)
        return 0

    def record(self, event: Mapping[str, object]) -> Path:
        """Append one event (already serialized, e.g. via
        `schema.action_to_dict`) as a JSON line.

        Adds ``ts`` (epoch seconds) when absent; the target file is named for
        that timestamp's UTC day. Returns the path written, for callers that
        surface "logged to ..." breadcrumbs.
        """
        entry = dict(event)
        entry.setdefault("ts", self._now())
        entry["ts"] = float(entry["ts"])
        day = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        path = self.dir_path / f"{day}.jsonl"
        data = self._encode(entry)
        with self._lock, _file_lock(self.dir_path / ".audit.lock", timeout=self.lock_timeout):
            flags = os.O_CREAT | os.O_RDWR | os.O_APPEND
            fd = os.open(path, flags, 0o600)
            try:
                size = self._repair_tail(fd)
                if size and size + len(data) > self.max_file_bytes:
                    os.close(fd)
                    fd = -1
                    archive = self.dir_path / f"{day}.{time.time_ns()}-{uuid.uuid4().hex[:8]}.jsonl"
                    path.replace(archive)
                    fd = os.open(path, flags, 0o600)
                    size = 0
                try:
                    remaining = memoryview(data)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("audit write made no progress")
                        remaining = remaining[written:]
                    if self.sync:
                        os.fsync(fd)
                except BaseException:
                    # A failed write must not poison the following successful
                    # row. Process death is handled by _repair_tail next time.
                    try:
                        os.ftruncate(fd, size)
                    except OSError:
                        pass
                    raise
            finally:
                if fd >= 0:
                    os.close(fd)
            if size == 0 or self._needs_prune:
                self._prune(path)
                self._needs_prune = False
                if self.sync:
                    _sync_directory(self.dir_path)
        return path

    def record_action(
        self,
        action: Action,
        *,
        app: str,
        decision: Decision,
        result: str,
        secure: bool = False,
        metrics: "Mapping[str, object] | None" = None,
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
        if isinstance(action, WebMcpOp) and payload.get("arguments") is not None:
            payload["arguments"] = REDACTED  # free-form content handed to the page
        payload = _redact_target_values(payload)
        entry: dict[str, object] = {
            "ts": self._now(),
            "app": app,
            "action": kind,
            "params": payload,
            "decision": decision.to_dict(),
            "result": result,
        }
        if metrics:  # cu-meter: duration_ms, result_chars, tokens_est (see server._run_gated)
            entry["metrics"] = dict(metrics)
        return self.record(entry)

    def record_failure(
        self, kind: str, *, app: str, params: Mapping[str, object], result: str
    ) -> Path:
        """Record an attempt that failed before an `Action` could be built.

        A ref that no longer resolves has no live target to serialize, so the
        gate never runs and `record_action` cannot be used. The entry keeps the
        standard shape (``ts``, ``app``, ``action``, ``params``, ``decision``,
        ``result``) with ``decision`` set to None (no verdict was reached) and
        ``result`` set to the `schema.ErrorCode` value. ``params`` must carry
        no injectable content: callers pass the ref, the snapshot epoch, and the
        failure reason, never text.
        """
        entry: dict[str, object] = {
            "ts": self._now(),
            "app": app,
            "action": kind,
            "params": dict(params),
            "decision": None,
            "result": result,
        }
        return self.record(entry)
