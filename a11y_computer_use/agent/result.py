"""Run result and the per-step record returned by ``Agent.run``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Status = Literal["success", "failed", "needs_human"]


@dataclass(slots=True)
class StepRecord:
    """One executed action, after redaction.

    ``target`` is ``{"ref", "role", "name"}``. ``args`` never contain a
    secret. ``verified`` is true only when the post-action check passed.
    """

    index: int
    action: str
    target: dict
    args: dict
    result: str
    error: str | None
    verified: bool
    duration_s: float

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration_s"] = round(float(self.duration_s), 4)
        return data


@dataclass(slots=True)
class RunResult:
    """The final result of one goal.

    ``steps`` is the count of ``step_log`` entries. ``conditions`` is the
    evidence check that decided the run (each item is ``condition``, ``ok``,
    ``detail``). ``needs_human`` is set only when ``status`` is
    ``needs_human``.
    """

    status: Status
    answer: str
    steps: int
    elapsed_s: float
    reason: str
    conditions: list[dict] = field(default_factory=list)
    needs_human: dict | None = None
    trace_dir: str = ""
    step_log: list[StepRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "answer": self.answer,
            "steps": self.steps,
            "elapsed_s": round(float(self.elapsed_s), 3),
            "reason": self.reason,
            "conditions": list(self.conditions),
            "needs_human": self.needs_human,
            "trace_dir": self.trace_dir,
            "step_log": [step.to_dict() for step in self.step_log],
        }


__all__ = ["RunResult", "Status", "StepRecord"]
