"""Typed actions the model may request, and the JSON schemas it is shown.

A model turn may contain several calls (``ModelTurn.calls``). The loop runs
that list one action at a time and stops the rest of the turn at the first
failure, refusal, or ``needs_human``. ``shell`` and ``python`` are exec
actions. They stay out of ``tool_schemas()`` unless ``allow_exec`` is true,
and they are not MCP tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

from a11y_computer_use.agent.models.base import ToolCall

#: Desktop actions. ``done`` and ``ask_human`` end or pause the run.
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
    "crop",
    "done",
    "ask_human",
})

#: Shell and Python. Hidden, rejected, and unaudited as runnable until
#: ``allow_exec`` is set. They are not added to the MCP server.
EXEC_ACTION_NAMES = frozenset({"shell", "python"})

_SEND_WORDS = ("send",)
_PAYMENT_WORDS = (
    "pay now",
    "purchase",
    "place order",
    "buy now",
    "confirm purchase",
    "confirm payment",
)
_DELETE_WORDS = (
    "delete",
    "move to trash",
    "empty trash",
    "trash",
)


class ReservedPermission(str, Enum):
    """Permission names that are not tiers in the desktop permission store.

    ``exec`` is shell and Python. It is off unless an ``Agent`` is built with
    ``allow_exec=True``. Each call still goes through ``approve`` (headless
    ``auto_deny`` refuses it) and is appended to the exec audit log.
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
            return "exec is not allowed"
        return _exec_error(action)
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
    if action.name == "crop" and not args.get("ref"):
        return "crop requires ref"
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
    if action.name in {"click", "menu"} and any(word in text for word in _PAYMENT_WORDS):
        return f"paying {label}"
    if action.name in {"click", "menu"} and any(word in text for word in _SEND_WORDS):
        return f"sending {label}"
    if action.name in {"click", "menu"} and any(word in text for word in _DELETE_WORDS):
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
        "crop",
        "Return a PNG of one element's on-screen bounds. Use it for an unnamed "
        "image, a canvas, or any opaque control. padding (0..512) and scale "
        "(greater than 0, at most 8) are optional. The library does not read the pixels.",
        {"ref": _STR, "padding": _INT, "scale": _NUM},
        ["ref"],
    ),
    (
        "done",
        "Finish the goal. conditions is 1 to 3 checks the loop re-runs against a fresh "
        "snapshot or the filesystem before it accepts success. Each condition is one of "
        '{"element": {"role", "name"}}, {"value": {"ref" or "name", "equals"}}, '
        '{"window_title_contains": text}, {"file_exists": path, "contains"?: text}. '
        "When the goal saves or creates a file, include file_exists, and contains "
        "when the text is known. A window title alone does not prove a file was written.",
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
    "Action",
    "ReservedPermission",
    "risk_reason",
    "tool_schemas",
    "validate_action",
]
