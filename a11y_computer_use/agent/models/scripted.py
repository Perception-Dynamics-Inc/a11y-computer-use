"""A model with no network: a list of turns, a JSON file, or a callable.

The JSON file is either a list of turn objects, one turn object, or
``{"turns": [...]}``. Each turn has the `ModelTurn` shape (``calls``,
``text``, ``raw``, ``usage``). A callable receives the current messages and
returns a `ModelTurn`; it is invoked on every `complete`, so the script can
react to tool results.

When a list or file runs out of turns, `complete` raises `ModelError`.
"""

from __future__ import annotations

import json
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
        self.seen: list[list[Message]] = []
        self.seen_tools: list[list[dict]] = []
        if path is not None:
            self._turns = _load_path(path)
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


def _load_path(path: str) -> list[TurnSource]:
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModelError(f"scripted model file not found: {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError(f"scripted model file is not JSON: {path}") from exc
    if isinstance(data, dict) and "turns" in data:
        data = data["turns"]
    elif isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ModelError(f"scripted model file must be a list of turns: {path}")
    return _coerce_turns(data)


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
