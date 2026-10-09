"""Typed actions the model may request, and the JSON schemas it is shown.

A model turn may contain several calls (``ModelTurn.calls``). M1 executes
that list one action at a time, and each action is verified on its own.
There is no ``exec`` action in this list. ``ReservedPermission.EXEC`` is the
name M2 will use for shell and Python; it stays off, behind ``approve``, and
audited. Adding it later does not rename the actions below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from a11y_computer_use.agent.models.base import ToolCall

#: Action names M1 will execute. ``done`` and ``ask_human`` end or pause the run.
ACTION_NAMES = frozenset({
    "click",
    "type",
    "key",
    "set_value",
    "select",
    "scroll",
    "app",
    "window",
    "menu",
    "wait",
    "done",
    "ask_human",
})

_SUBMIT_WORDS = (
    "submit",
    "send",
    "pay now",
    "purchase",
    "place order",
    "buy now",
    "confirm purchase",
    "confirm payment",
)


class ReservedPermission(str, Enum):
    """Permissions reserved for a later milestone.

    ``exec`` is shell and Python execution. M1 does not grant it and does not
    expose a tool that would use it. A future executor must leave it off by
    default, refuse it unless ``approve`` returns true, and write the command
    to the audit log before it runs.
    """

    EXEC = "exec"


@dataclass(frozen=True, slots=True)
class Action:
    """One tool call the loop can execute."""

    name: str
    args: dict
    id: str | None = None

    @classmethod
    def from_call(cls, call: ToolCall) -> Action:
        args = call.args if isinstance(call.args, dict) else {}
        return cls(name=str(call.name), args=dict(args), id=call.id)


def tool_schemas() -> list[dict]:
    """JSON-schema function definitions passed to ``Model.complete``."""
    return [_schema(name, description, properties, required) for name, description, properties, required in _SPECS]


def validate_action(action: Action) -> str | None:
    """Return an error string when ``action`` cannot be executed, else None."""
    if action.name not in ACTION_NAMES:
        return f"unknown action {action.name!r}"
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
    if action.name == "menu" and not (args.get("path") or args.get("action") == "list"):
        return "menu requires path"
    if action.name == "done":
        return _done_error(args)
    if action.name == "ask_human":
        if not args.get("message"):
            return "ask_human requires message"
        kind = str(args.get("kind") or "other")
        if kind not in {"login", "captcha", "2fa", "payment", "other"}:
            return f"ask_human kind {kind!r} is not one of login, captcha, 2fa, payment, other"
    if action.name == "click" and not args.get("ref") and (args.get("x") is None or args.get("y") is None):
        return "click requires ref or x and y"
    return None


def risk_reason(action: Action, label: str | None) -> str | None:
    """Why this action needs ``approve``, or None when it does not.

    Typing into a password, OTP, or card field is not approved: the loop
    pauses with ``needs_human`` and does not type it.
    """
    verb = str(action.args.get("action") or "").lower()
    if action.name == "app" and verb == "quit":
        return "app quit"
    if action.name == "window" and verb == "close":
        return "closing a window"
    text = (label or "").casefold()
    if action.name in {"click", "menu"} and any(word in text for word in _SUBMIT_WORDS):
        return f"submitting {label}"
    if action.name == "menu" and any(word in text for word in ("quit", "exit", "close window", "log out", "sign out")):
        return f"menu {label}"
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


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_OBJ = {"type": "object"}

_SPECS: list[tuple[str, str, dict[str, Any], list[str]]] = [
    (
        "click",
        "Click an element by accessibility ref, or at x,y when a ref click cannot land.",
        {"ref": _STR, "button": _STR, "count": _INT, "x": _INT, "y": _INT, "display_id": _INT},
        [],
    ),
    (
        "type",
        "Type text into the focused element. Never use this for passwords, OTP, or card numbers.",
        {"text": _STR, "app": _STR},
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
        "Press a menu item by path, for example File > Save As.",
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
        "done",
        "Finish the goal. conditions is 1 to 3 checks the loop re-runs against a fresh "
        "snapshot or the filesystem before it accepts success. Each condition is one of "
        '{"element": {"role", "name"}}, {"value": {"ref" or "name", "equals"}}, '
        '{"window_title_contains": text}, {"file_exists": path, "contains"?: text}.',
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


__all__ = [
    "ACTION_NAMES",
    "Action",
    "ReservedPermission",
    "risk_reason",
    "tool_schemas",
    "validate_action",
]
