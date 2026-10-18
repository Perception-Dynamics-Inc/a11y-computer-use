"""Typed actions the model may request, and the JSON schemas it is shown.

A model turn may contain several calls (``ModelTurn.calls``). The loop runs
that list one action at a time and stops the rest of the turn at the first
failure, refusal, or ``needs_human``. ``shell`` and ``python`` are exec
actions. They stay out of ``tool_schemas()`` unless ``allow_exec`` is true,
and they are not MCP tools.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from a11y_computer_use.agent.choose_file import chooser_argument_error
from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.schema import unknown_fields_message

#: Pointer gestures. Each one is a click. The name sets button and count.
#: An explicit ``x,y`` is that point and is not moved onto another element.
POINTER_CLICKS: dict[str, dict[str, str | int]] = {
    "click": {},
    "left_click": {"button": "left", "count": 1},
    "double_click": {"button": "left", "count": 2},
    "triple_click": {"button": "left", "count": 3},
    "right_click": {"button": "right", "count": 1},
    "middle_click": {"button": "middle", "count": 1},
}

#: Desktop actions. ``done`` and ``ask_human`` end or pause the run.
ACTION_NAMES = frozenset({
    *POINTER_CLICKS,
    "type",
    "key",
    "set_value",
    "select",
    "scroll",
    "app",
    "window",
    "menu",
    "wait",
    "crop",
    "find",
    "expand",
    "scroll_to_find",
    "choose_file",
    "snapshot",
    "done",
    "ask_human",
})

#: Shell and Python. Hidden, rejected, and unaudited as runnable until
#: ``allow_exec`` is set. They are not added to the MCP server.
EXEC_ACTION_NAMES = frozenset({"shell", "python"})

#: Refusal for shell/python when exec is off. It is final for the session and
#: names GUI routes instead of a setting the user could change (#624).
EXEC_DISABLED_ERROR = (
    "exec is not allowed. This refusal is final for this session. "
    "Use GUI routes such as a file manager, text editor, or app menus; "
    "do not ask the user to enable exec or a terminal."
)

#: ``ask_human`` refused when exec is off: it would hand the run to a person
#: to enable exec, open a terminal, or run a command (#624).
_EXEC_REQUEST_RE = re.compile(
    r"\b(?:enable|enabling|turn(?:ing)?\s+on|allow|allowing|grant|granting|unlock|switch\s+on)\b"
    r"[^.?!\n]{0,60}?\b(?:exec|execution|terminals?|shell|command[\s-]+line)\b"
    r"|\b(?:run|execute|type|paste|enter)\b[^.?!\n]{0,40}?\b(?:commands?|in\s+(?:a|the)\s+"
    r"(?:terminal|shell|console))\b"
    r"|\bopen\b[^.?!\n]{0,20}?\b(?:a|the)\s+(?:terminal|shell|console)\b",
    re.IGNORECASE,
)

_SEND_WORDS = ("send",)
# Buttons and links. A checkbox or toggle is never one of these, even when
# its label contains "send" ("Send usage statistics").
_PAYMENT_LABEL = re.compile(
    r"\b(?:"
    r"pay(?:ment|\s+now|\s+with)?"
    r"|place\s+(?:your\s+)?order"
    r"|buy(?:\s+now)?"
    r"|purchase"
    r"|check\s*out"
    r"|confirm\s+payment"
    r"|complete\s+order"
    r"|confirm\s+(?:purchase|order)"
    r")\b",
    re.IGNORECASE,
)
# A path segment or host label, not a substring of another word.
_PAYMENT_URL = re.compile(
    r"(?:^|[/?#&=])(?:checkout|check-out|payment|billing|purchase)(?:$|[/?#&=.])",
    re.IGNORECASE,
)
_PAYMENT_WINDOW = re.compile(r"\b(?:checkout|payment|billing)\b", re.IGNORECASE)
_PAYMENT_FIELD = (
    "card number",
    "credit card",
    "debit card",
    "cvv",
    "cvc",
    "payment card",
    "cardholder",
)
_DELETE_WORDS = (
    "delete",
    "move to trash",
    "empty trash",
    "trash",
)
_TOGGLE_ROLES = frozenset({
    "checkbox",
    "toggle",
    "switch",
    "togglebutton",
    "checkbutton",
})
_TEXT_ROLES = frozenset({
    "statictext",
    "text",
    "textfield",
    "textarea",
    "image",
    "group",
    "window",
    "webarea",
})


class ReservedPermission(str, Enum):
    """Permission names that are not tiers in the desktop permission store.

    ``exec`` is shell and Python. It is off unless an ``Agent`` is built with
    ``allow_exec=True``. Each call still goes through ``approve`` (headless
    ``auto_deny`` refuses it) and is appended to the exec audit log.
    """

    EXEC = "exec"


@dataclass(frozen=True, slots=True)
class Action:
    """One tool call the loop can execute.

    ``role``, ``target_name``, ``window``, ``url``, ``summary``, ``reason``,
    and ``reason_kind`` are set on the copy passed to ``approve``. They are
    empty on the action the model requested. ``target_name`` is the control's
    accessible name. ``url`` is the page URL when the target is in a browser.
    ``summary`` is a short JSON summary of ``args`` with secrets removed.
    ``reason`` is ``risk_reason`` (for example ``paying Pay now``).
    ``reason_kind`` is ``payment``, ``send``, ``delete``, ``quit``, or ``exec``.
    """

    name: str
    args: dict
    id: str | None = None
    role: str | None = None
    target_name: str | None = None
    window: str | None = None
    url: str | None = None
    summary: str | None = None
    reason: str | None = None
    reason_kind: str | None = None

    @classmethod
    def from_call(cls, call: ToolCall) -> Action:
        args = call.args if isinstance(call.args, dict) else {}
        return cls(name=str(call.name), args=dict(args), id=call.id)

    def for_approval(
        self,
        *,
        role: str | None,
        target_name: str | None,
        window: str | None,
        summary: str | None,
        reason: str | None,
        url: str | None = None,
        reason_kind: str | None = None,
    ) -> Action:
        """Copy this action with the fields a person needs in order to approve it."""
        return replace(
            self,
            role=role,
            target_name=target_name,
            window=window,
            url=url,
            summary=summary,
            reason=reason,
            reason_kind=reason_kind if reason_kind is not None else risk_category(reason),
        )


def tool_schemas(*, allow_exec: bool = False) -> list[dict]:
    """JSON-schema function definitions passed to ``Model.complete``.

    ``shell`` and ``python`` are included only when ``allow_exec`` is true.
    The default list is the desktop actions the MCP server already knows,
    plus ``done`` and ``ask_human``, which belong to the agent loop.
    """
    specs = list(_SPECS)
    if allow_exec:
        specs.extend(_EXEC_SPECS)
    return [_schema(name, description, properties, required) for name, description, properties, required in specs]


def validate_action(action: Action, *, allow_exec: bool = False) -> str | None:
    """Return an error string when ``action`` cannot be executed, else None."""
    if action.name in EXEC_ACTION_NAMES:
        if not allow_exec:
            return EXEC_DISABLED_ERROR
        unknown = _unknown_argument(action)
        if unknown is not None:
            return unknown
        return _exec_error(action)
    if action.name not in ACTION_NAMES:
        return f"unknown action {action.name!r}"
    unknown = _unknown_argument(action)
    if unknown is not None:
        return unknown
    args = action.args
    if action.name == "type" and "text" not in args:
        return "type requires text"
    if action.name == "key" and not args.get("chord"):
        return "key requires chord"
    if action.name in {"set_value", "select"} and (not args.get("ref") or "value" not in args):
        return f"{action.name} requires ref and value"
    if action.name == "app" and not args.get("action"):
        return "app requires action"
    if action.name == "window" and not args.get("action"):
        return "window requires action"
    if action.name == "menu" and not (args.get("path") or args.get("action") in {"list", "close"}):
        # list and close act on the menu bar or the open menu; only press needs a path.
        return "menu press requires path"
    if action.name == "done":
        return _done_error(args)
    if action.name == "ask_human":
        if not args.get("message"):
            return "ask_human requires message"
        kind = str(args.get("kind") or "other")
        if kind not in {"login", "captcha", "2fa", "payment", "other"}:
            return f"ask_human kind {kind!r} is not one of login, captcha, 2fa, payment, other"
        if not allow_exec and _EXEC_REQUEST_RE.search(str(args.get("message") or "")):
            return (
                "ask_human cannot request exec, a terminal, or a command run: exec is off "
                "and this is final for this session. Do the task with GUI routes such as "
                "a file manager, text editor, app menus, or file:// in the browser."
            )
    if action.name in POINTER_CLICKS:
        point_error = _click_target_error(args, name=action.name)
        if point_error is not None:
            return point_error
    if action.name == "crop" and not args.get("ref"):
        return "crop requires ref"
    if action.name == "find" and not _find_filter(args):
        return "find requires text, role, editable, or clickable"
    if action.name == "expand" and not args.get("ref"):
        return "expand requires ref"
    if action.name == "scroll_to_find" and not (args.get("text") or args.get("role")):
        return "scroll_to_find requires text or role"
    if action.name == "scroll_to_find" and args.get("direction") not in (None, "down", "up"):
        return "scroll_to_find direction must be down or up"
    if action.name == "choose_file":
        return chooser_argument_error(args)
    if action.name == "snapshot" and args.get("mode") not in (None, "full", "diff"):
        return "snapshot mode must be full or diff"
    return None


def pointer_to_click(action: Action) -> Action:
    """Map a pointer gesture onto ``click``, keeping the point and the gesture.

    ``double_click`` is a left click with count 2. ``right_click`` is the
    right button. The gesture name sets button and count; arguments on the
    call do not override that. An explicit ``x,y`` stays on the action.
    ``click`` itself is returned unchanged.
    """
    gesture = POINTER_CLICKS.get(action.name)
    if gesture is None or action.name == "click":
        return action
    args = {
        key: value
        for key, value in action.args.items()
        if key not in {"button", "count"}
    }
    args.update(gesture)
    return replace(action, name="click", args=args)


def _click_target_error(args: dict, *, name: str = "click") -> str | None:
    """Reject a click that names two targets, or names none.

    A ref together with ``x`` or ``y`` is ``invalid_arguments``: the call
    must not silently drop the point. ``label`` is the only other target,
    and it stands alone. A point needs both coordinates. ``name`` is the
    gesture the model called (``click``, ``double_click``, ``right_click``,
    and the other pointer names).
    """
    ref = str(args.get("ref") or "").strip()
    label = str(args.get("label") or "").strip()
    has_x = args.get("x") is not None
    has_y = args.get("y") is not None
    if ref and (has_x or has_y):
        return (
            f"invalid_arguments: {name}: ref and x/y are both set; "
            "pass a ref or a point, not both"
        )
    if label and (ref or has_x or has_y):
        return (
            f"invalid_arguments: {name}: label cannot be combined with ref or x/y"
        )
    if has_x != has_y:
        return f"{name} requires both x and y"
    if not ref and not label and not (has_x and has_y):
        return f"{name} requires ref or x and y"
    return None


def _role_key(role: str | None) -> str:
    if not role:
        return ""
    return "".join(str(role).casefold().split()).removeprefix("ax")


def is_toggle_control(role: str | None) -> bool:
    """Checkboxes, switches, and toggles. These are never a send action."""
    key = _role_key(role)
    return key in _TOGGLE_ROLES or "checkbox" in key


def _is_button_or_link(role: str | None) -> bool:
    """A clickable control a person could use to submit a payment.

    An unknown role still counts, so a payment label is not dropped when the
    tree omitted the role. Checkboxes, toggles, and static text do not.
    """
    if is_toggle_control(role):
        return False
    key = _role_key(role)
    if not key:
        return True
    if key in _TEXT_ROLES:
        return False
    return True


def looks_like_payment_form(
    *,
    url: str | None = None,
    window: str | None = None,
    texts: object = (),
) -> bool:
    """True when the page is a checkout or payment form.

    ``url`` matches a checkout, payment, billing, or purchase segment.
    ``window`` matches those words in the title. ``texts`` are accessible
    names and placeholders; a card field marks the form even when the button
    itself is labelled Submit.
    """
    if isinstance(url, str) and _PAYMENT_URL.search(url):
        return True
    if isinstance(window, str) and _PAYMENT_WINDOW.search(window):
        return True
    for text in texts:  # type: ignore[union-attr]
        blob = str(text or "").casefold()
        if any(token in blob for token in _PAYMENT_FIELD):
            return True
    return False


def risk_category(reason: str | None) -> str | None:
    """``payment``, ``send``, ``delete``, ``quit``, or ``exec`` for ``reason``."""
    if not reason:
        return None
    text = reason.casefold()
    if text.startswith("paying"):
        return "payment"
    if text.startswith("sending"):
        return "send"
    if text.startswith("deleting"):
        return "delete"
    if text.startswith("app quit") or text.startswith("closing") or text.startswith("menu "):
        return "quit"
    if text.startswith("running"):
        return "exec"
    return None


def approval_target(action: Action) -> dict[str, str]:
    """Role, name, window, URL, and reason for a model or an API client.

    ``role`` and ``reason`` are the loop's own tokens (``payment``, ``send``,
    ``delete``, ``quit``, ``exec``). The name, window, and page URL are page
    text: each is trimmed and wrapped with ``fence``. The CLI prompt a person
    reads uses ``render_untrusted`` instead of these tags.
    """
    from a11y_computer_use.untrusted import fence_untrusted

    target: dict[str, str] = {}
    if action.role:
        target["role"] = action.role
    if action.target_name:
        fenced = fence_untrusted(action.target_name)
        if fenced:
            target["name"] = fenced
    if action.window:
        fenced = fence_untrusted(action.window)
        if fenced:
            target["window"] = fenced
    if action.url:
        fenced = fence_untrusted(action.url, limit=160)
        if fenced:
            target["url"] = fenced
    kind = action.reason_kind or risk_category(action.reason)
    if kind:
        target["reason"] = kind
    return target


def _deletes(action: Action, text: str, role: str | None) -> bool:
    """The shared full-path judgement (`safety.judge_command`) with the harness words.

    ``text`` is the casefolded label, a whole menu path such as
    "table > delete > rows" for menu items. A click or expand on a submenu
    opener (role ``menu``) only opens it, so the leaf under it is judged
    instead (#847). A ``menu`` call names a leaf path and carries no role.
    """
    from a11y_computer_use.safety import judge_command

    components = tuple(part for part in text.split(" > ") if part)
    opener_role = None if action.name == "menu" else role
    return judge_command(components, role=opener_role, words=_DELETE_WORDS) is not None


def risk_reason(
    action: Action,
    label: str | None,
    *,
    role: str | None = None,
    url: str | None = None,
    window: str | None = None,
    payment_form: bool = False,
) -> str | None:
    """Why this action needs ``approve``, or None when it does not.

    Typing into a password, OTP, or card field is not approved: the loop
    pauses with ``needs_human`` and does not type it. A payment-like button
    or link, or a button or link on a payment form, is ``paying``. A
    checkbox or toggle is never ``sending``, including a usage-stats checkbox
    whose label contains "send". ``url``, ``window``, and ``payment_form``
    are accepted so callers can pass the page context; the form flag is what
    marks a generic submit on a checkout page.
    """
    del url, window
    verb = str(action.args.get("action") or "").lower()
    if action.name == "app" and verb == "quit":
        return "app quit"
    if action.name == "window" and verb == "close":
        return "closing a window"
    text = (label or "").casefold()
    if action.name in {"click", "menu", "expand"} and not is_toggle_control(role):
        named = bool(label) and _PAYMENT_LABEL.search(label) is not None and _is_button_or_link(role)
        on_form = payment_form and _is_button_or_link(role)
        if named or on_form:
            return f"paying {label or 'payment'}"
    if action.name in {"click", "menu", "expand"} and not is_toggle_control(role) and any(word in text for word in _SEND_WORDS):
        return f"sending {label}"
    if action.name in {"click", "menu", "expand"} and _deletes(action, text, role):
        return f"deleting {label}"
    if action.name == "menu" and any(word in text for word in ("quit", "exit", "close window", "log out", "sign out")):
        return f"menu {label}"
    if action.name == "shell":
        return "running a shell command"
    if action.name == "python":
        return "running python"
    return None


def _exec_error(action: Action) -> str | None:
    if action.name == "shell" and not str(action.args.get("command") or "").strip():
        return "shell requires command"
    if action.name == "python" and not str(action.args.get("code") or "").strip():
        return "python requires code"
    if "timeout_s" in action.args and action.args.get("timeout_s") is not None:
        try:
            timeout = float(action.args["timeout_s"])
        except (TypeError, ValueError):
            return "timeout_s must be a positive number"
        if timeout <= 0:
            return "timeout_s must be a positive number"
    cwd = action.args.get("cwd")
    if cwd is not None and not os.path.isdir(str(cwd)):
        return "cwd is not a directory"
    return None


def _done_error(args: dict) -> str | None:
    if "answer" not in args:
        return "done requires answer"
    conditions = args.get("conditions")
    if isinstance(conditions, str):
        return "done conditions must be a list of 1 to 3 objects"
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 3:
        return "done requires 1 to 3 conditions"
    if not all(isinstance(item, dict) for item in conditions):
        return "done conditions must be objects"
    return None


def _find_filter(args: dict) -> bool:
    """True when find names at least one filter the runtime accepts."""
    if args.get("text") or args.get("role"):
        return True
    return args.get("editable") is not None or args.get("clickable") is not None


def _declared_fields(name: str) -> set[str] | None:
    for spec_name, _description, properties, _required in (*_SPECS, *_EXEC_SPECS):
        if spec_name == name:
            return set(properties)
    return None


def _unknown_argument(action: Action) -> str | None:
    """An undeclared top-level argument. None when every key is in the schema.

    ``_source_ref`` is not a model argument. Recovery adds it after this
    check, on a click the runtime maps without that key.
    """
    allowed = _declared_fields(action.name)
    if allowed is None:
        return None
    message = unknown_fields_message(action.args, allowed)
    if message is None:
        return None
    return f"invalid_arguments: {action.name}: {message}"


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_OBJ = {"type": "object"}

_CLICK_PROPERTIES = {
    "ref": _STR,
    "label": _STR,
    "button": _STR,
    "count": _INT,
    "x": _INT,
    "y": _INT,
    "display_id": _INT,
}
_CLICK_TARGET_RULES = (
    "Pass only one target: a ref, an exact label, or x and y. "
    "A ref together with x or y is invalid_arguments. "
    "A missing ref is resolved only from that ref's last-known role and name, "
    "never from the goal. An x,y point is not moved onto a different element. "
    "scroll at x,y is that scroll, not a click on a word from the goal."
)

_SPECS: list[tuple[str, str, dict[str, Any], list[str]]] = [
    (
        "click",
        "Click an element by accessibility ref, by an exact label, or at x,y. "
        + _CLICK_TARGET_RULES
        + " double_click, right_click, middle_click, triple_click, and left_click "
        "are the same rules; the name sets the button and count.",
        _CLICK_PROPERTIES,
        [],
    ),
    (
        "left_click",
        "Left-click (left button, count 1). " + _CLICK_TARGET_RULES
        + " button and count on this call are ignored; the name sets them.",
        _CLICK_PROPERTIES,
        [],
    ),
    (
        "double_click",
        "Double-click (left button, count 2). " + _CLICK_TARGET_RULES
        + " button and count on this call are ignored; the name sets them.",
        _CLICK_PROPERTIES,
        [],
    ),
    (
        "triple_click",
        "Triple-click (left button, count 3). " + _CLICK_TARGET_RULES
        + " button and count on this call are ignored; the name sets them.",
        _CLICK_PROPERTIES,
        [],
    ),
    (
        "right_click",
        "Right-click (right button, count 1). " + _CLICK_TARGET_RULES
        + " button and count on this call are ignored; the name sets them.",
        _CLICK_PROPERTIES,
        [],
    ),
    (
        "middle_click",
        "Middle-click (middle button, count 1). " + _CLICK_TARGET_RULES
        + " button and count on this call are ignored; the name sets them.",
        _CLICK_PROPERTIES,
        [],
    ),
    (
        "type",
        "Type text. With ref, focus that element, confirm the focus landed, and type only "
        "there; if focus cannot be confirmed, nothing is typed. Without ref, type into "
        "the focused element. An undeclared argument is invalid_arguments and no text "
        "is sent. Never use this for passwords, OTP, or card numbers.",
        {"text": _STR, "app": _STR, "ref": _STR},
        ["text"],
    ),
    (
        "key",
        "Press a key chord such as Return, ctrl+s, or alt+f.",
        {"chord": _STR, "app": _STR},
        ["chord"],
    ),
    (
        "set_value",
        "Set an editable element's value through the accessibility API.",
        {"ref": _STR, "value": _STR},
        ["ref", "value"],
    ),
    (
        "select",
        "Choose an option in a combo box, list, or popup. The value is the option's visible name.",
        {"ref": _STR, "value": _STR},
        ["ref", "value"],
    ),
    (
        "scroll",
        "Scroll at a ref or a point. dy is lines; negative scrolls down.",
        {"ref": _STR, "dx": _INT, "dy": _INT, "x": _INT, "y": _INT, "display_id": _INT},
        [],
    ),
    (
        "app",
        "Launch, focus, or quit an application. quit needs approval.",
        {"action": {"type": "string", "enum": ["launch", "focus", "quit", "list"]}, "name": _STR},
        ["action"],
    ),
    (
        "window",
        "List, raise, move, resize, minimize, or close a window. close needs approval.",
        {
            "action": {
                "type": "string",
                "enum": ["list", "raise", "focus", "move", "resize", "minimize", "close"],
            },
            "window_id": _INT,
            "app": _STR,
            "x": _INT,
            "y": _INT,
            "width": _INT,
            "height": _INT,
        },
        ["action"],
    ),
    (
        "menu",
        "Press a menu item by path, for example File > Save As. "
        "action close dismisses the open menu and needs no path.",
        {"app": _STR, "path": _STR, "action": {"type": "string", "enum": ["press", "list", "close"]}},
        [],
    ),
    (
        "wait",
        "Wait until a condition holds, or for a number of seconds.",
        {"seconds": _NUM, "condition": _OBJ, "timeout_s": _NUM},
        [],
    ),
    (
        "crop",
        "Return a PNG of one element's on-screen bounds. Use it for an unnamed "
        "image, a canvas, or any opaque control. padding (0..512) and scale "
        "(greater than 0, at most 8) are optional. When an OCR engine is "
        "installed, the result lists each word with [x, y, width, height] in "
        "screen pixels, fenced as untrusted. A missing engine says no text was read.",
        {"ref": _STR, "padding": _INT, "scale": _NUM},
        ["ref"],
    ),
    (
        "find",
        "Search the tracked app by name and/or role. text is a case-insensitive "
        "substring of the title or value. role is a substring of the accessibility "
        "role. Use this before a pixel click when the target may be folded or "
        "scrolled out of view.",
        {
            "text": _STR,
            "role": _STR,
            "editable": {"type": "boolean"},
            "clickable": {"type": "boolean"},
            "scope": _STR,
        },
        [],
    ),
    (
        "expand",
        "Open a collapsed menu, tree, or section and list that subtree. Use "
        "expand(ref) when the observation says the node is collapsed or has "
        "more children. Prefer this over a coordinate click.",
        {"ref": _STR},
        ["ref"],
    ),
    (
        "scroll_to_find",
        "Scroll until an element matching text and/or role is on screen, then "
        "return its ref. Use this when the target is below the fold, before "
        "falling back to pixels. direction is down or up.",
        {
            "text": _STR,
            "role": _STR,
            "direction": _STR,
            "max_scrolls": _INT,
            "ref": _STR,
        },
        [],
    ),
    (
        "choose_file",
        "In an open GTK, Qt, or Chrome file chooser, choose this absolute path. "
        "Always use choose_file; never navigate folders. Presses Ctrl+L, types the "
        "path once, and presses Return. When the location entry is visible and the "
        "typed text is not the path, one set_value writes the path. Return is not "
        "pressed until that entry reads the path back. Succeeds only when the dialog "
        "has closed and the file name is shown outside the dialog. If that cannot be "
        "verified, the result is a failure. action is open (default) or save. open "
        "requires the file to exist. This does not call file_dialog.",
        {
            "path": _STR,
            "action": {"type": "string", "enum": ["open", "save"]},
        },
        ["path"],
    ),
    (
        "snapshot",
        "Send the accessibility tree again. mode full (the default) redraws the "
        "current window and keeps the refs of elements that are still there. "
        "mode diff repeats the changes since the last observation you were shown. "
        "A different window is always the full tree.",
        {"mode": {"type": "string", "enum": ["full", "diff"]}},
        [],
    ),
    (
        "done",
        "Finish the goal. conditions is 1 to 3 checks the loop re-runs against a fresh "
        "snapshot or the filesystem before it accepts success. Each condition is one of "
        '{"element": {"role", "name"}}, {"value": {"ref" or "name", "equals"}}, '
        '{"window_title_contains": text}, {"dir_exists": path}, '
        '{"file_exists": path, "contains"?: text}. '
        "For folder creation include dir_exists for the folder path. "
        "When the goal saves or creates a file, include file_exists, and contains "
        "for the text the file should hold. When the goal says to replace or "
        "remove text, contains is the new text, not the text being replaced or "
        "removed; a file that still has the old text is rejected. contains treats "
        "a newline, CR, CRLF, U+2028, U+2029, and U+FFFC as the same break, and "
        "ignores one leading UTF-8 BOM. A plain-text file that starts with that "
        "mark still passes, and the evidence includes bom_present: true, unless "
        "the goal forbids a byte-order mark. contains on .odt, .ods, .docx, and "
        ".xlsx reads the document text inside the zip, not the raw bytes, and a "
        "paragraph end or a line break in that text is a newline. A window title "
        "alone does not prove a file was written.",
        {
            "answer": _STR,
            "conditions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": _OBJ,
            },
        },
        ["answer", "conditions"],
    ),
    (
        "ask_human",
        "Stop and ask a person. kind is login, captcha, 2fa, payment, or other. Do not guess secrets.",
        {
            "kind": {"type": "string", "enum": ["login", "captcha", "2fa", "payment", "other"]},
            "message": _STR,
            "ref": _STR,
        },
        ["kind", "message"],
    ),
]


_EXEC_SPECS: list[tuple[str, str, dict[str, Any], list[str]]] = [
    (
        "shell",
        "Run a shell command on this machine. Requires exec permission and approval. "
        "command is the shell string. cwd and timeout_s are optional. Output is truncated.",
        {"command": _STR, "cwd": _STR, "timeout_s": _NUM},
        ["command"],
    ),
    (
        "python",
        "Run Python source in a fresh interpreter. Requires exec permission and approval. "
        "code is the source. cwd and timeout_s are optional. Output is truncated.",
        {"code": _STR, "cwd": _STR, "timeout_s": _NUM},
        ["code"],
    ),
]


__all__ = [
    "ACTION_NAMES",
    "EXEC_ACTION_NAMES",
    "POINTER_CLICKS",
    "pointer_to_click",
    "Action",
    "ReservedPermission",
    "approval_target",
    "is_toggle_control",
    "looks_like_payment_form",
    "risk_category",
    "risk_reason",
    "tool_schemas",
    "validate_action",
]
