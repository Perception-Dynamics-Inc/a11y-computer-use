"""A model that is a local command.

Each `complete` runs the command once. The process reads one JSON object from
stdin::

    {"messages": [Message, ...], "tools": [schema, ...]}

and writes one `ModelTurn` JSON object to stdout::

    {"calls": [{"name": "click", "args": {"ref": "e1"}, "id": "1"}], "text": "", "usage": {}}

Logs go to stderr. A non-zero exit, a timeout, a missing executable, or
stdout that is not one JSON object raises `ModelError`. A single existing
``.py`` file is run with the current Python interpreter so ``command:script.py``
works without a shebang.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence

from a11y_computer_use.agent.models.base import (
    Message,
    ModelError,
    ModelTurn,
    message_to_dict,
    model_turn_from_dict,
)

__all__ = ["CommandModel", "command_argv"]


def command_argv(command: str | Sequence[str]) -> list[str]:
    """Argv for a `CommandModel` command string or sequence."""
    if not isinstance(command, str):
        argv = [str(part) for part in command]
    elif os.path.isfile(command):
        argv = [command]
    else:
        argv = shlex.split(command, posix=(os.name != "nt"))
    if not argv or not argv[0]:
        raise ModelError("command model is missing a command")
    if len(argv) == 1 and argv[0].endswith(".py") and os.path.isfile(argv[0]):
        return [sys.executable, argv[0]]
    return argv


class CommandModel:
    """Run a subprocess that speaks the model JSON protocol on stdin/stdout."""

    name = "command"
    supports_images = True

    def __init__(self, command: str | Sequence[str], *, timeout: float = 30.0) -> None:
        self.argv = command_argv(command)
        self.model = self.argv[0]
        self.timeout = timeout

    def complete(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        timeout: float | None = None,
    ) -> ModelTurn:
        limit = self.timeout if timeout is None else timeout
        if limit <= 0:
            raise ModelError(f"command model timeout must be positive, got {limit}")
        try:
            payload = json.dumps(
                {"messages": [message_to_dict(message) for message in messages], "tools": tools},
                ensure_ascii=False,
            )
        except (TypeError, ValueError) as exc:
            raise ModelError(f"command model request is not JSON-serializable: {exc}") from exc
        try:
            proc = subprocess.run(
                self.argv,
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelError(f"command model timed out after {limit}s: {self.argv[0]}") from exc
        except OSError as exc:
            raise ModelError(f"command model failed to start {self.argv[0]!r}: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ModelError(f"command model exited {proc.returncode}: {detail[:2000]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stdout or proc.stderr or "").strip()
            raise ModelError(f"command model wrote non-JSON stdout: {detail[:500]!r}") from exc
        return model_turn_from_dict(data)
