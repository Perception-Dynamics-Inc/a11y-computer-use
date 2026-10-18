"""Run result and the per-step record returned by ``Agent.run``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

StepStatus = Literal["confirmed", "dispatched", "unverifiable", "no_op", "failed", "skipped"]


def step_status(
    verified: bool, error: str | None, marker: str | None = None, skipped: bool = False,
) -> StepStatus:
    """Normalize existing evidence without performing any observation.

    ``marker`` is the action result's outcome. Skipping takes precedence,
    then errors, then explicit outcomes, then the legacy verification check.
    """
    if skipped:
        return "skipped"
    if error:
        return "failed"
    if marker in {"partial", "refused"}:
        return "failed"
    if marker == "dispatched":
        return "dispatched"
    if marker == "suspected_noop":
        return "no_op"
    if marker == "unverifiable":
        return "unverifiable"
    if marker == "confirmed" or verified:
        return "confirmed"
    return "no_op"


Status = Literal["success", "failed", "stuck", "timeout", "needs_human", "cancelled"]


@dataclass(slots=True)
class StepRecord:
    """One executed or skipped action, after redaction.

    ``target`` is ``{"ref", "role", "name"}``. ``args`` never contain a
    secret. ``verified`` is true only when the post-action check passed.
    ``skipped`` lists later calls in the same turn that did not run.
    ``turn_stop`` is ``failure``, ``refusal``, ``needs_human``, ``screen_change``, or None.
    ``status`` normalizes existing evidence; it does not change verification.
    """

    index: int
    action: str
    target: dict
    args: dict
    result: str
    error: str | None
    verified: bool
    duration_s: float
    skipped: list = field(default_factory=list)
    turn_stop: str | None = None
    fingerprint: dict[str, str] | None = None
    status: StepStatus | None = None

    def __post_init__(self) -> None:
        if self.status is None:
            self.status = step_status(self.verified, self.error)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration_s"] = round(float(self.duration_s), 4)
        return data


@dataclass(slots=True)
class RunResult:
    """The final result of one goal.

    ``skipped_steps`` holds unexecuted calls, indexed separately, so they do
    not change execution budgets or the historical ``step_log`` ordering.
    ``steps`` is the count of ``step_log`` entries. ``conditions`` is the
    evidence check that decided the run (each item is ``condition``, ``ok``,
    ``detail``).     ``needs_human`` is set only when ``status`` is
    ``needs_human``. ``evidence`` is set when ``status`` is ``stuck`` or
    ``timeout``.
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
    evidence: dict | None = None
    skipped_steps: list[StepRecord] = field(default_factory=list)

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
            "evidence": self.evidence,
            **({"skipped_steps": [step.to_dict() for step in self.skipped_steps]}
               if self.skipped_steps else {}),
        }


__all__ = ["RunResult", "Status", "StepRecord", "StepStatus", "step_status"]
