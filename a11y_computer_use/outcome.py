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
import math
import os
import re
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
    if code == "focus_lost" or reason == "focus_lost":
        # Do not offer keyboard: that types into whatever is focused now.
        return ("ref", "cdp")
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


_WINDOW_ROLES = frozenset({"AXWindow", "AXDialog", "AXSheet", "AXDrawer"})
# A titled group, web area, or scroll area above the target is the page.
# The window is the page when the control has no narrower titled ancestor.
_PAGE_ROLES = frozenset({"AXGroup", "AXWebArea", "AXScrollArea"})
# Browser chrome. Its focus, selection, and status text move on their own.
_CHROME_UI_ROLES = frozenset({
    "AXToolbar", "AXTabGroup", "AXMenuBar", "AXMenu", "AXScrollBar",
})


def state_fingerprint(snap: object) -> str:
    """Digest of roles, names, values, and states. Refs and bounds are ignored.

    A fresh snapshot renumbers refs. A scroll moves bounds without editing a
    control. Neither of those, on its own, is evidence a click landed.
    Action judgment uses `relevant_state_changed` instead: this digest also
    moves when Chrome churns a toolbar or status node the click did not touch.
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


def _snap_elements(snap: object) -> tuple:
    return tuple(getattr(snap, "elements", ()) or ())


def _match_control(element: object, snap: object) -> object | None:
    """The same control in ``snap``. Refs are not stable across reads."""
    elements = _snap_elements(snap)
    role = getattr(element, "role", "")
    title = getattr(element, "title", "")
    path = tuple(getattr(element, "path", ()) or ())
    same_path = [
        el for el in elements
        if el.role == role and el.title == title and tuple(el.path or ()) == path
    ]
    if len(same_path) == 1:
        return same_path[0]
    same_name = [el for el in elements if el.role == role and el.title == title]
    if len(same_name) == 1:
        return same_name[0]
    stable = getattr(element, "stable_id", None)
    if stable:
        for el in elements:
            if el.stable_id == stable:
                return el
    if same_path:
        return same_path[0]
    return same_name[0] if same_name else None


def _relevant_target_state(element: object) -> tuple:
    """Focus, value, checked, and expanded. The rest of the target is not proof."""
    return (
        bool(getattr(element, "focused", False)),
        getattr(element, "value", None),
        getattr(element, "checked", None),
        getattr(element, "expanded", None),
    )


def _by_ref(snap: object) -> dict:
    return {
        el.ref: el for el in _snap_elements(snap) if getattr(el, "ref", None)
    }


def _page_root(snap: object, target: object | None) -> object | None:
    """The document that holds ``target``, or its window when it has none."""
    if target is None:
        return None
    by_ref = _by_ref(snap)
    current = by_ref.get(getattr(target, "parent", None))
    titled = None
    window = None
    seen: set[str] = set()
    while current is not None:
        ref = getattr(current, "ref", "")
        if ref in seen:
            break
        seen.add(ref)
        role = getattr(current, "role", "")
        if role in _WINDOW_ROLES:
            window = current
            break
        if titled is None and getattr(current, "title", "") and role in _PAGE_ROLES:
            titled = current
        parent = getattr(current, "parent", None)
        current = by_ref.get(parent) if parent else None
    return titled or window


def _subtree(snap: object, root: object) -> list:
    children: dict[str, list] = {}
    for el in _snap_elements(snap):
        parent = getattr(el, "parent", None)
        if parent:
            children.setdefault(parent, []).append(el)
    out = [root]
    stack = list(children.get(getattr(root, "ref", None), []))
    seen = {getattr(root, "ref", None)}
    while stack:
        el = stack.pop()
        ref = getattr(el, "ref", None)
        if ref in seen:
            continue
        seen.add(ref)
        out.append(el)
        stack.extend(children.get(ref, []))
    return out


def _chrome_ui(element: object) -> bool:
    return any(part in _CHROME_UI_ROLES for part in (getattr(element, "path", ()) or ()))


def _page_row(element: object) -> tuple:
    """Content that means the page changed. Focus and enabled are not included."""
    return (
        getattr(element, "role", ""),
        getattr(element, "title", ""),
        getattr(element, "value", None),
        getattr(element, "checked", None),
        getattr(element, "expanded", None),
        tuple(getattr(element, "path", ()) or ()),
    )


def _sort_key(row: tuple) -> tuple:
    """A key ``sorted`` can compare. Values mix None, bools, and strings."""
    key = []
    for part in row:
        if part is None:
            key.append((0, ""))
        elif isinstance(part, bool):
            key.append((1, part))
        elif isinstance(part, tuple):
            key.append((3, part))
        else:
            key.append((2, str(part)))
    return tuple(key)


def _page_rows(snap: object, target: object | None) -> tuple:
    root = _page_root(snap, target)
    if root is None:
        elements = [el for el in _snap_elements(snap) if not _chrome_ui(el)]
    else:
        elements = _subtree(snap, root)
    return tuple(sorted((_page_row(el) for el in elements), key=_sort_key))


def _window_rows(snap: object) -> tuple:
    rows = [
        (el.role, el.title)
        for el in _snap_elements(snap)
        if getattr(el, "role", "") in _WINDOW_ROLES
    ]
    return tuple(sorted(rows))


def relevant_state_changed(before: object, after: object, target: object | None = None) -> bool:
    """Whether ``target`` or its window or page changed between two snapshots.

    The target's focus, value, checked, and expanded count. So does a window
    title (or a window appearing) and the page that holds the target: its
    titles, values, checked state, and expanded state. Focus, selection, and
    enabled bits elsewhere do not, and neither does Chrome's toolbar, tab
    strip, or status text when the click was inside the page.
    """
    if target is not None:
        before_target = _match_control(target, before)
        after_target = _match_control(target, after)
        if before_target is None or after_target is None:
            return True
        if _relevant_target_state(before_target) != _relevant_target_state(after_target):
            return True
    else:
        before_target = None
        after_target = None
    if _window_rows(before) != _window_rows(after):
        return True
    return _page_rows(before, before_target) != _page_rows(after, after_target)


# Text the user is editing, plus a spreadsheet cell. A tab strip or toolbar
# around that text is not part of this record: Mousepad's document lives
# under a tab group, which the page fingerprint skips.
_KEY_TEXT_ROLES = frozenset({
    "AXTextArea", "AXTextField", "AXSearchField", "AXComboBox", "AXCell",
})
_KEY_SELECTION_ROLES = frozenset({"AXCell", "AXRow", "AXOutlineRow"})


def key_focus_rows(snap: object) -> tuple:
    """Focused text and the selected cell or row.

    The page fingerprint omits tab groups and toolbars, and it does not
    include which cell is selected. A key is confirmed from this record
    when that fingerprint stays still.
    """
    rows = []
    for el in _snap_elements(snap):
        role = getattr(el, "role", "")
        title = getattr(el, "title", "") or ""
        value = getattr(el, "value", None)
        if bool(getattr(el, "focused", False)) and role in _KEY_TEXT_ROLES:
            rows.append(("text", role, title, value))
        if bool(getattr(el, "selected", False)) and role in _KEY_SELECTION_ROLES:
            rows.append(("selection", role, title, value))
    return tuple(sorted(rows, key=_sort_key))


def _key_text_rows(rows: tuple) -> tuple:
    return tuple(row for row in rows if row[0] == "text" and row[1] != "AXCell")


def _key_cell_rows(rows: tuple) -> tuple:
    return tuple(row for row in rows if row[1] == "AXCell" or row[0] == "selection")


def key_focus_evidence(
    before_live: tuple | None,
    after_live: tuple | None,
    before_rows: tuple,
    after_rows: tuple,
) -> str | None:
    """Why a key changed the focused text, caret, selection, or cell.

    ``None`` when those records match. A live record is ``(text, caret,
    selection start, selection end, cell address)``. An unread live record
    is not a change.
    """
    if before_rows != after_rows:
        if _key_text_rows(before_rows) != _key_text_rows(after_rows):
            return "the focused text changed"
        if _key_cell_rows(before_rows) != _key_cell_rows(after_rows):
            return "the focused cell changed"
        return "the focused text changed"
    if not _live_known(before_live) or not _live_known(after_live) or before_live == after_live:
        return None
    assert before_live is not None and after_live is not None
    b_text, b_caret, b_start, b_end, b_cell = before_live
    a_text, a_caret, a_start, a_end, a_cell = after_live
    if b_text != a_text:
        return "the focused text changed"
    if b_cell != a_cell:
        return "the focused cell changed"
    if (b_start, b_end) != (a_start, a_end):
        # A caret move keeps an empty selection on the caret. A range that
        # grows, shrinks, or collapses is the selection itself.
        if b_start == b_end and a_start == a_end:
            return "the caret moved"
        return "the selection changed"
    if b_caret != a_caret:
        return "the caret moved"
    return None


def _live_known(record: tuple | None) -> bool:
    return record is not None and any(part is not None for part in record)


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


def display_numbers_match(shown: str, wanted: str) -> bool:
    """True when both sides are the same number after Calc's display rewrite.

    ``14.60`` matches ``14.6``, ``1e3`` matches ``1000``, and ``1,200`` matches
    ``1200``. A formula is not a number, so ``=E2+31`` does not match ``42``.
    A formula cut at a colon does not match the rest of the formula.
    """
    if shown is None or wanted is None:
        return False
    try:
        return _numbers_close(_parse_display_number(shown), _parse_display_number(wanted))
    except (TypeError, ValueError):
        return False


def _parse_display_number(value: str) -> float:
    """``1.50``, ``1e3``, ``1,200``, and ``1,5``. A formula is not a number."""
    text = str(value).strip().replace("\u00a0", "").replace("\u202f", "")
    if not text or text.startswith("="):
        raise ValueError(text)
    if any(char.isalpha() and char not in "eE" for char in text):
        raise ValueError(text)
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        pieces = text.split(",")
        head = pieces[0].lstrip("+-")
        if head.isdigit() and all(len(piece) == 3 and piece.isdigit() for piece in pieces[1:]):
            text = "".join(pieces)
        elif len(pieces) == 2 and pieces[1].isdigit() and len(pieces[1]) != 3:
            text = pieces[0] + "." + pieces[1]
        else:
            raise ValueError(text)
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return number


def _numbers_close(got: float, wanted: float) -> bool:
    return abs(float(got) - float(wanted)) <= 1e-6 * max(1.0, abs(wanted))


# Line and paragraph separators. ``\r\n`` is one break. A repeated break
# stays repeated, and a space is not a break. U+000B VT, U+000C FF,
# U+0085 NEL, U+2028, U+2029, and U+FFFC are the same break as ``\n``.
_PARAGRAPH_BREAK = re.compile(r"\r\n|[\n\r\u000b\u000c\u0085\u2028\u2029\ufffc]")


def normalize_paragraph_breaks(text: str) -> str:
    """Map every paragraph or line separator onto ``\\n``, one break each."""
    return _PARAGRAPH_BREAK.sub("\n", str(text).replace("\u00a0", " "))


def paragraph_breaks_match(got: str | None, wanted: str | None) -> bool:
    """True when the only difference is which paragraph separator was used.

    Newline, CR, CRLF, VT, FF, NEL, U+2028, U+2029, and U+FFFC are the same
    break. ``\\r\\n`` is one break, not two. ``a\\n\\nb`` is not ``a\\nb``,
    ``a b`` is not ``a\\nb``, and ``ab`` is not ``a\\nb``.
    """
    if got is None or wanted is None:
        return False
    return normalize_paragraph_breaks(got) == normalize_paragraph_breaks(wanted)


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
    """Whether ``pid`` is still running.

    A process that has exited but has not been reaped counts as dead. On
    Linux that is a zombie in ``/proc``. On macOS it is a ``Z`` state, and
    signal 0 still succeeds until ``wait``. On Windows ``os.kill(pid, 0)``
    is ``CTRL_C_EVENT`` and would interrupt the caller, so the exit code is
    read with ``GetExitCodeProcess`` instead. A synthetic snapshot pid that
    was never alive is not a crash.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if sys.platform.startswith("linux"):
        return _linux_pid_alive(pid)
    if sys.platform == "darwin":
        return not _darwin_zombie(pid)
    return True


def _linux_pid_alive(pid: int) -> bool:
    """False for a zombie or a pid that disappeared between the signal and the read."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return False
    end = text.rfind(")")
    if end == -1 or end + 2 >= len(text):
        return False
    return text[end + 2] not in {"Z", "X"}


def _darwin_zombie(pid: int) -> bool:
    """True when ``ps`` reports a zombie. Signal 0 cannot see that state."""
    import subprocess

    try:
        completed = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    state = completed.stdout.strip()
    if completed.returncode != 0 or not state:
        return True
    return state.startswith("Z")


def _windows_pid_alive(pid: int) -> bool:
    """True while the process has not exited.

    An unreaped process still has a handle. ``GetExitCodeProcess`` returns
    ``STILL_ACTIVE`` only while it is running.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        if ctypes.get_last_error() == error_access_denied:
            return True
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


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
