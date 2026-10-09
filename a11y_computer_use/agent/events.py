"""Typed events yielded by ``Agent.stream`` and passed to ``on_event``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EventType = Literal[
    "plan",
    "step_started",
    "action",
    "observation",
    "step_finished",
    "needs_human",
    "done",
    "error",
    "stuck",
]


@dataclass(frozen=True, slots=True)
class Event:
    """One loop event.

    ``type`` is the wire name. ``data`` is JSON-serialisable context for that
    event (the observation text, the tool call, the stuck digest, and so on).
    """

    type: EventType
    data: dict = field(default_factory=dict)


__all__ = ["Event", "EventType"]
