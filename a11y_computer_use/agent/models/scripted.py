"""A model with no network: a list of turns, a JSON file, or a callable.

The JSON file is either a list of turn objects, one turn object, or
``{"turns": [...]}``. Each turn has the `ModelTurn` shape (``calls``,
``text``, ``raw``, ``usage``). A callable receives the current messages and
returns a `ModelTurn`; it is invoked on every `complete`, so the script can
react to tool results.

When a list or file runs out of turns, `complete` raises `ModelError`.

A turns object may also set ``hold_before_turn`` (1-based), ``hold_file``,
and optional ``hold_timeout_s`` (default 60). Before returning that turn,
``complete`` waits until ``hold_file`` contains an integer >= 1. The CLI
cancel handler writes that file. A test uses the wait so the signal is sent
and recorded before the next step can start, including when the previous
step did not block.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from a11y_computer_use.agent.models.base import (
    Message,
    ModelError,
    ModelTurn,
    model_turn_from_dict,
)

__all__ = ["ScriptedModel"]

TurnSource = ModelTurn | Callable[[list[Message]], ModelTurn]


class ScriptedModel:
    """Replay turns, or compute one from the messages."""

    name = "scripted"
    supports_images = True

    def __init__(
        self,
        source: Sequence[TurnSource] | Callable[[list[Message]], ModelTurn] | None = None,
        *,
        path: str | None = None,
    ) -> None:
        if path is not None and source is not None:
            raise ModelError("ScriptedModel takes a turn list, a callable, or a path, not more than one")
        self._fn: Callable[[list[Message]], ModelTurn] | None = None
        self._turns: list[TurnSource] = []
        self._index = 0
        self._hold: tuple[int, str, float] | None = None
        self.seen: list[list[Message]] = []
        self.seen_tools: list[list[dict]] = []
        if path is not None:
            self._turns, self._hold = _load_path(path)
        elif callable(source):
            self._fn = source
        elif isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
            self._turns = _coerce_turns(source)
        else:
            raise ModelError("ScriptedModel needs a turn list, a callable, or a path")

    def complete(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        timeout: float | None = None,
    ) -> ModelTurn:
        del timeout
        self.seen.append(list(messages))
        self.seen_tools.append(list(tools))
        if self._fn is not None:
            turn = self._fn(messages)
            if not isinstance(turn, ModelTurn):
                raise ModelError(
                    f"scripted callable must return ModelTurn, got {type(turn).__name__}"
                )
            return turn
        if self._index >= len(self._turns):
            raise ModelError("scripted model has no turns left")
        # Block before this turn exists. The next step cannot start until the
        # cancel record is on disk, so a late signal cannot land after it.
        self._await_recorded_cancel(self._index + 1)
        item = self._turns[self._index]
        self._index += 1
        if callable(item):
            turn = item(messages)
            if not isinstance(turn, ModelTurn):
                raise ModelError(
                    f"scripted callable must return ModelTurn, got {type(turn).__name__}"
                )
            return turn
        return item

    def _await_recorded_cancel(self, turn_number: int) -> None:
        """Wait until ``hold_file`` records a cancel, when this turn is held."""
        if self._hold is None:
            return
        before, path, timeout = self._hold
        if turn_number < before:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if text:
                try:
                    seen = int(text)
                except ValueError:
                    seen = 0
                if seen >= 1:
                    return
            time.sleep(0.02)
        raise ModelError(
            f"scripted model held turn {turn_number} until {path} recorded a cancel"
        )


def _load_path(path: str) -> tuple[list[TurnSource], tuple[int, str, float] | None]:
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelError(f"scripted model file not found: {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError(f"scripted model file is not JSON: {path}") from exc
    hold: tuple[int, str, float] | None = None
    if isinstance(data, dict):
        if "hold_before_turn" in data or "hold_file" in data:
            hold = _parse_hold(data)
        if "turns" in data:
            data = data["turns"]
        elif hold is not None:
            raise ModelError(
                f"scripted model file with hold_before_turn must contain turns: {path}"
            )
        else:
            data = [data]
    if not isinstance(data, list):
        raise ModelError(f"scripted model file must be a list of turns: {path}")
    return _coerce_turns(data), hold


def _parse_hold(data: dict) -> tuple[int, str, float]:
    """``hold_before_turn`` plus the cancel-record path the test waits on."""
    before = data.get("hold_before_turn")
    hold_file = data.get("hold_file")
    timeout = data.get("hold_timeout_s", 60)
    if isinstance(before, bool) or not isinstance(before, int) or before < 1:
        raise ModelError("hold_before_turn must be an integer >= 1")
    if not isinstance(hold_file, str) or not hold_file:
        raise ModelError("hold_file must be the path of the cancel record")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or float(timeout) <= 0:
        raise ModelError("hold_timeout_s must be a positive number")
    return before, hold_file, float(timeout)


def _coerce_turns(source: Sequence[object]) -> list[TurnSource]:
    turns: list[TurnSource] = []
    for item in source:
        if isinstance(item, ModelTurn) or callable(item):
            turns.append(item)
        elif isinstance(item, dict):
            turns.append(model_turn_from_dict(item))
        else:
            raise ModelError(
                f"scripted turn must be a ModelTurn, a callable, or a JSON object, got {type(item).__name__}"
            )
    return turns
