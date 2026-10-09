"""Typed result of one action: what happened, and what to try next.

The human-readable sentence stays the string value, so existing parsers that
compare or prefix-match that sentence keep working. ``outcome``, ``next``, and
``evidence`` ride alongside it. An MCP client reads them from the tool's
structured content. The agent loop reads the attributes.

``outcome`` is one of:

- ``confirmed`` — a read-back, a state change, or a still-living window/process
  showed the action landed.
- ``suspected_noop`` — the call returned and nothing observable changed.
- ``unverifiable`` — there was no state to compare.
- ``partial`` — something landed, but not the requested effect. A process that
  exits after a click is this case: the click was delivered and the app is gone,
  so the result is not ``confirmed``.
- ``refused`` — the action was not performed (permission, a hidden target, a
  disabled or secure control, a failed confirmation).

``next`` is an ordered list drawn from ``ref``, ``coordinates``, ``cdp``,
``keyboard``, and ``foreground``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Sequence

OUTCOMES = frozenset({
    "confirmed",
    "suspected_noop",
    "unverifiable",
    "partial",
    "refused",
})

STRATEGIES = ("ref", "coordinates", "cdp", "keyboard", "foreground")

# Errors where repeating the same action, or sending Escape, is the wrong move.
_NO_RETRY = frozenset({
    "permission_denied_accessibility",
    "permission_denied_screen",
    "secure_field",
    "confirmation_declined",
    "user_active",
    "busy",
    "closed",
})


class ActionResult(str):
    """A tool sentence that also carries ``outcome``, ``next``, and ``evidence``.

    It compares equal to the sentence. Attributes are not part of the text.
    """

    outcome: str
    next: tuple[str, ...]
    evidence: str

    def __new__(
        cls,
        text: str,
        *,
        outcome: str,
        next: Sequence[str] = (),
        evidence: str = "",
    ) -> "ActionResult":
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        self = str.__new__(cls, text)
        self.outcome = outcome
        self.next = clean_next(next)
        self.evidence = evidence
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "next": list(self.next),
            "evidence": self.evidence,
        }


def clean_next(items: Sequence[str] | None) -> tuple[str, ...]:
    """Keep known strategies, in the order given, without duplicates."""
    chosen: list[str] = []
    for item in items or ():
        name = str(item)
        if name in STRATEGIES and name not in chosen:
            chosen.append(name)
    return tuple(chosen)


def suggest_next(
    tool: str,
    outcome: str,
    *,
    had_ref: bool = False,
    browser: bool = False,
    process_died: bool = False,
) -> tuple[str, ...]:
    """Strategies to try after ``outcome``, most specific first.

    A confirmed action has nothing to escalate. A dead process is not retried
    at the same coordinates. A refusal that already names its own ``next``
    (a hidden document wants the tab in front) is supplied by the caller.
    """
    if outcome == "confirmed":
        return ()
    if process_died:
        return ("ref", "foreground")
    steps: list[str] = []
    if had_ref or tool in {"click", "set_value", "select", "scroll"}:
        steps.append("ref")
    steps.append("coordinates")
    if browser or tool in {"type", "set_value", "select"}:
        steps.append("cdp")
    steps.append("keyboard")
    steps.append("foreground")
    return clean_next(steps)


def next_for_error(code: str, detail: dict | None) -> tuple[str, ...]:
    """Escalation for a raised error. Empty when retrying would repeat a refusal."""
    info = detail or {}
    named = info.get("next")
    if isinstance(named, (list, tuple)) and named:
        return clean_next(named)
    reason = str(info.get("reason") or "")
    if code in _NO_RETRY or reason in _NO_RETRY:
        return ()
    if reason == "not_showing":
        return ("foreground", "ref")
    if code == "stale_ref":
        return ("ref", "coordinates", "keyboard")
    if code == "focus_changed" or reason in {"focus_changed", "not_frontmost"}:
        return ("foreground", "ref")
    if code == "element_disabled" or reason == "disabled":
        return ("ref", "keyboard")
    if reason == "text_mismatch":
        return ("keyboard", "cdp", "ref")
    return ("ref", "coordinates", "keyboard", "foreground")


def refused_result(*, text: str, code: str, message: str, detail: dict | None) -> ActionResult:
    """An action that did not run, as a result the agent and MCP client can read."""
    info = detail or {}
    evidence = info.get("evidence")
    if not isinstance(evidence, str) or not evidence:
        evidence = message or text
    outcome = info.get("outcome") or "refused"
    if outcome not in OUTCOMES:
        outcome = "refused"
    return ActionResult(
        text,
        outcome=outcome,
        next=next_for_error(code, info),
        evidence=evidence,
    )


def state_fingerprint(snap: object) -> str:
    """Digest of roles, names, values, and states. Refs and bounds are ignored.

    A fresh snapshot renumbers refs. A scroll moves bounds without editing a
    control. Neither of those, on its own, is evidence a click landed.
    """
    rows: list[str] = []
    for el in getattr(snap, "elements", ()) or ():
        rows.append("\t".join((
            str(getattr(el, "role", "")),
            str(getattr(el, "title", "")),
            "" if getattr(el, "value", None) is None else str(el.value),
            "1" if getattr(el, "enabled", True) else "0",
            "1" if getattr(el, "focused", False) else "0",
            "" if getattr(el, "checked", None) is None else str(int(bool(el.checked))),
            "1" if getattr(el, "selected", False) else "0",
            "" if getattr(el, "expanded", None) is None else str(int(bool(el.expanded))),
        )))
    raw = "\n".join(rows)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def bounds_fingerprint(snap: object) -> str:
    """Digest of element rectangles, for scroll."""
    rows: list[str] = []
    for el in getattr(snap, "elements", ()) or ():
        bounds = getattr(el, "bounds", None)
        if bounds is None:
            rows.append("")
        else:
            rows.append(f"{bounds.x},{bounds.y},{bounds.width},{bounds.height}")
    raw = "\n".join(rows)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def values_match(requested: str, actual: str) -> bool:
    """True when the read-back is the requested text, or the same number."""
    if actual == requested:
        return True
    try:
        return float(requested) == float(actual)
    except (TypeError, ValueError):
        return False


def judge(
    *,
    changed: bool | None,
    requested: str | None = None,
    readback: str | None = None,
    before_value: str | None = None,
    process_died: bool = False,
    readable: bool = True,
) -> tuple[str, str]:
    """``(outcome, evidence)`` from a real observation.

    ``process_died`` wins: a click that kills the target is not confirmed.
    A read-back that equals the request is confirmed. A read-back that moved
    but does not equal the request is partial. An unchanged readable tree is
    ``suspected_noop``.
    """
    if process_died:
        return "partial", "the target process exited after the action"
    if requested is not None:
        if readback is None or not readable:
            return "unverifiable", "the value could not be read back"
        if values_match(requested, readback):
            return "confirmed", f"read back {readback!r}"
        moved = before_value is not None and readback != before_value
        appended = (
            moved
            and before_value is not None
            and (readback.endswith(requested) or readback == f"{before_value}{requested}")
        )
        if appended:
            return "confirmed", f"read back {readback!r}"
        if moved:
            return "partial", f"read back {readback!r} after requesting {requested!r}"
        return "suspected_noop", "the value did not change"
    if not readable or changed is None:
        return "unverifiable", "the accessibility state could not be read"
    if changed:
        return "confirmed", "the accessibility state changed"
    return "suspected_noop", "the accessibility state did not change"


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` exists. A synthetic snapshot pid that was never alive is not."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if sys.platform.startswith("linux"):
        # A zombie still has a /proc entry and still accepts signal 0. The
        # process has already exited; the parent has not reaped it. A click
        # that quits the target is this case until the caller calls wait.
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return False
        end = text.rfind(")")
        if end == -1 or end + 2 >= len(text):
            return False
        return text[end + 2] not in {"Z", "X"}
    return True


def wait_until_dead(pid: int, wait_s: float) -> bool:
    """True if ``pid`` exits within ``wait_s``.

    The caller has already seen the pid alive. A handler that quits on the
    next main-loop turn is still running when the click call returns.
    """
    if not pid_alive(pid):
        return True
    deadline = time.monotonic() + max(0.0, wait_s)
    while time.monotonic() < deadline:
        time.sleep(0.02)
        if not pid_alive(pid):
            return True
    return not pid_alive(pid)
