"""Pure screen-change checks over snapshots already collected by the loop."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from a11y_computer_use.agent.actions import Action
from a11y_computer_use.schema import Element, Snapshot

_COMPONENTS = frozenset({"app", "window", "url", "focus", "tree"})
_LOCAL = frozenset({"focus", "tree"})
# Editors mark unsaved documents in the window title (Mousepad/gedit "*doc",
# VS Code "● doc", "doc (modified)"). An edit's own dirty marker is not a
# screen change; any other title change still is (#787).
_DIRTY_EDGE = "*●•"
_DIRTY_SUFFIX = re.compile(r"\s*(?:[-–—]\s*modified|\(modified\)|\[modified\])$", re.IGNORECASE)


@dataclass(frozen=True)
class ScreenFingerprint:
    app: tuple
    window: tuple
    url: str | None
    focus: tuple
    tree: str

    @classmethod
    def from_snapshot(cls, snap: Snapshot | None, digest: str) -> ScreenFingerprint:
        """Reuse the caller's snapshot_digest; never query a driver."""
        if snap is None:
            return cls((), (), None, (), digest)
        roots = [el for el in snap.elements if el.role in {"AXWindow", "AXDialog", "AXSheet"}]
        if not roots:
            roots = [el for el in snap.elements if el.parent is None]
        windows = tuple((el.ref, el.role, el.title) for el in roots)
        focus = tuple((el.ref, el.role, el.title) for el in snap.elements if el.focused)
        return cls((snap.app, snap.pid), windows, snap.url, focus, digest)

    def hashed(self) -> dict[str, str]:
        """Trace-safe components: no window titles, focus labels, or URLs."""
        return {
            key: hashlib.sha256(json.dumps(getattr(self, key), ensure_ascii=False).encode()).hexdigest()
            for key in sorted(_COMPONENTS)
        }


def normalize_title(title: str | None) -> str | None:
    """Drop leading/trailing unsaved-document markers from a window title."""
    if not title:
        return title
    text = title.strip()
    while True:
        stripped = _DIRTY_SUFFIX.sub("", text).strip(_DIRTY_EDGE).strip()
        if stripped == text:
            return text
        text = stripped


def _window_identity(windows: tuple) -> tuple:
    return tuple((ref, role, normalize_title(title)) for ref, role, title in windows)


def diff(before: ScreenFingerprint, after: ScreenFingerprint) -> set[str]:
    changed = {key for key in _COMPONENTS if getattr(before, key) != getattr(after, key)}
    if "window" in changed and _window_identity(before.window) == _window_identity(after.window):
        changed.discard("window")
    return changed


def expected_changes(action: Action, target: Element | None = None) -> frozenset[str]:
    """Classify effects, without executing, resolving, or rewriting the action."""
    name, args = action.name, action.args
    if name in {"app", "window", "scroll", "choose_file", "scroll_to_find"}:
        if args.get("action") not in {"list", "state"}:
            return _COMPONENTS
    if name == "menu" and args.get("action", "press") not in {"list", "state"}:
        return _COMPONENTS
    if name == "key":
        parts = set(str(args.get("chord", "")).lower().replace(" ", "").split("+"))
        navigation = {
            "return", "enter", "kp_enter", "escape", "esc", "f5",
            "alt", "option", "super", "win", "meta", "cmd", "command",
        }
        if parts & navigation or (parts & {"ctrl", "control"} and parts & set("loswtnqr")):
            return _COMPONENTS
    if name == "click":
        local_roles = {
            "AXTextField", "AXTextArea", "AXComboBox", "AXCheckBox",
            "AXRadioButton", "AXSlider", "AXIncrementor",
        }
        if args.get("x") is not None or target is None or target.role not in local_roles:
            return _COMPONENTS
    return _LOCAL


def unexpected_changes(
    before: ScreenFingerprint, after: ScreenFingerprint, action: Action,
    target: Element | None, snap: Snapshot | None,
) -> set[str]:
    changed = diff(before, after)
    expected = expected_changes(action, target)
    unexpected = changed - expected
    # Text/value actions may focus their target (or a child), but must not
    # silently continue after focus jumps to an unrelated control. Unscoped
    # chords such as ctrl+f may legitimately focus a different in-window field.
    if "focus" in changed and expected == _LOCAL and target is not None and snap is not None:
        by_ref = {el.ref: el for el in snap.elements}
        for ref, _role, _title in after.focus:
            seen = set()
            while ref in by_ref and ref not in seen and ref != target.ref:
                seen.add(ref)
                ref = by_ref[ref].parent
            if ref != target.ref:
                unexpected.add("focus")
        if before.focus and not after.focus:
            unexpected.add("focus")
    return unexpected
