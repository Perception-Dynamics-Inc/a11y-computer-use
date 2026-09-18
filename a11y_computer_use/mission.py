"""Missions: long, multi-app tasks run as verified phases.

A mission file (TOML) lists phases. Each phase is one `agent.run_task` call
with its own task text, the apps it may drive (granted for the phase and
restored afterwards), a step budget, and checks the RUNNER evaluates after the
planner calls ``done``: files that must exist, URLs that must answer, text
that must show up in an app. A failed check re-runs the phase with the failure
written into the agent's notes. The run leaves a directory of artifacts,
including a wall-clock timeline a video editor can cut from.

Example (``examples/missions/agency-demo.toml``)::

    [mission]
    name = "agency-demo"
    deadline_s = 3600

    [mission.record]
    start = "open 'screenstudio://record'"
    stop = "open 'screenstudio://stop'"

    [[phase]]
    name = "brief"
    task = "Read the newest client message in Telegram and note the brief."
    apps = ["ru.keepcoder.Telegram"]
    tier = "read"
    max_steps = 40
    retries = 1
    checks = [{ notes_contain = "brief:" }]
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from a11y_computer_use import agent, conditions, safety, server
from a11y_computer_use.providers import Provider
from a11y_computer_use.schema import ComputerUseError

#: Check kinds the runner evaluates itself, beyond `conditions.KINDS`.
RUNNER_CHECKS = ("notes_contain",)


@dataclass(slots=True)
class Phase:
    name: str
    task: str
    apps: list[str] = field(default_factory=list)
    tier: str = "full"
    max_steps: int = 60
    retries: int = 0
    checks: list[dict] = field(default_factory=list)
    check_timeout_s: float = 30.0
    on_fail_notes: str = ""
    verify: bool = True

    def validate(self, index: int) -> list[str]:
        problems: list[str] = []
        where = f"phase {index} ({self.name or 'unnamed'})"
        if not self.name or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            problems.append(f"{where}: name must be a short identifier")
        if not self.task.strip():
            problems.append(f"{where}: task is required")
        if self.tier not in {t.value for t in safety.Tier}:
            problems.append(f"{where}: tier must be one of read, click, full")
        if not isinstance(self.max_steps, int) or self.max_steps < 1 or self.max_steps > 400:
            problems.append(f"{where}: max_steps must be 1..400")
        if not isinstance(self.retries, int) or self.retries < 0 or self.retries > 10:
            problems.append(f"{where}: retries must be 0..10")
        for check in self.checks:
            if not isinstance(check, dict):
                problems.append(f"{where}: each check must be a table")
                continue
            keys = [k for k in check if k in conditions.KINDS or k in RUNNER_CHECKS]
            if len(keys) != 1:
                problems.append(f"{where}: check must have exactly one of "
                                f"{list(conditions.KINDS) + list(RUNNER_CHECKS)}: {check}")
        return problems


@dataclass(slots=True)
class Mission:
    name: str
    phases: list[Phase]
    deadline_s: float | None = None
    record_start: str | None = None
    record_stop: str | None = None
    source: Path | None = None

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.name or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            problems.append("mission.name must be a short identifier (letters, digits, - _ .)")
        if self.deadline_s is not None and self.deadline_s <= 0:
            problems.append("mission.deadline_s must be positive")
        if not self.phases:
            problems.append("a mission needs at least one [[phase]]")
        names = [p.name for p in self.phases]
        if len(set(names)) != len(names):
            problems.append("phase names must be unique")
        for i, phase in enumerate(self.phases, 1):
            problems += phase.validate(i)
        return problems


def load(path: str | Path) -> Mission:
    """Parse a mission file. Raises ValueError with every problem found."""
    path = Path(path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    head = data.get("mission", {})
    if not isinstance(head, dict):
        raise ValueError("[mission] must be a table")
    record = head.get("record", {}) if isinstance(head.get("record", {}), dict) else {}
    phases = []
    for raw in data.get("phase", []):
        if not isinstance(raw, dict):
            raise ValueError("each [[phase]] must be a table")
        known = {f for f in Phase.__dataclass_fields__}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"phase {raw.get('name', '?')}: unknown keys {unknown}")
        phases.append(Phase(**raw))
    mission = Mission(
        name=str(head.get("name", path.stem)),
        phases=phases,
        deadline_s=float(head["deadline_s"]) if "deadline_s" in head else None,
        record_start=record.get("start"),
        record_stop=record.get("stop"),
        source=path,
    )
    problems = mission.validate()
    if problems:
        raise ValueError("invalid mission:\n- " + "\n- ".join(problems))
    return mission


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PhaseResult:
    name: str
    attempts: int
    passed: bool
    checks: list[dict]
    results: list[dict]  #: `agent.AgentResult.to_dict()` per attempt
    started_at: float
    finished_at: float

    def to_dict(self) -> dict:
        return {
            "name": self.name, "attempts": self.attempts, "passed": self.passed,
            "checks": self.checks, "results": self.results,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "duration_s": round(self.finished_at - self.started_at, 2),
        }


@dataclass(slots=True)
class MissionResult:
    mission: str
    passed: bool
    stopped: str  #: completed | phase_failed | deadline
    phases: list[PhaseResult]
    run_dir: Path
    started_at: float
    finished_at: float

    def to_dict(self) -> dict:
        return {
            "mission": self.mission, "passed": self.passed, "stopped": self.stopped,
            "phases": [p.to_dict() for p in self.phases], "run_dir": str(self.run_dir),
            "started_at": self.started_at, "finished_at": self.finished_at,
            "duration_s": round(self.finished_at - self.started_at, 2),
        }


class _Timeline:
    """Append-only JSONL of wall-clock events (what a video editor cuts from)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.t0 = time.time()
        self.path.write_text("", encoding="utf-8")

    def event(self, kind: str, **fields: object) -> None:
        now = time.time()
        row = {"ts": now, "t": round(now - self.t0, 3), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")


def _run_hook(command: str | None, timeline: _Timeline, kind: str) -> None:
    if not command:
        return
    try:
        completed = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=60)
        timeline.event(kind, command=command, returncode=completed.returncode,
                       stderr=completed.stderr[-500:])
    except (OSError, subprocess.SubprocessError) as exc:
        timeline.event(kind, command=command, error=str(exc))


class _Grants:
    """Grant a phase's apps and restore the previous grants afterwards."""

    def __init__(self, store: safety.PermissionStore) -> None:
        self.store = store
        self._previous: dict[str, safety.Tier | None] = {}

    def grant(self, apps: list[str], tier: str) -> None:
        for app in apps:
            if app not in self._previous:
                self._previous[app] = self.store.get_tier(app)
            self.store.set_tier(app, safety.Tier(tier))

    def restore(self) -> None:
        for app, previous in self._previous.items():
            if previous is None:
                self.store.revoke(app)
            else:
                self.store.set_tier(app, previous)
        self._previous.clear()


def evaluate_check(runtime: server.Runtime, check: dict, *, timeout_s: float) -> dict:
    """Runner-side check: notes_contain, or any `conditions` kind (ungated: the
    runner is on the human's side of the gate; it grants the apps itself)."""
    if "notes_contain" in check:
        needle = str(check["notes_contain"]).lower()
        text = runtime.notes_store.render().lower()
        ok = needle in text
        return {"check": check, "passed": ok,
                "detail": "found in notes" if ok else "not in notes"}
    try:
        outcome = runtime._checker().wait(check, timeout_s=timeout_s,
                                          poll_s=float(check.get("poll_s", 2.0)))
        return {"check": check, "passed": True, "detail": outcome["matched"],
                "waited_s": outcome["waited_s"]}
    except ComputerUseError as exc:
        return {"check": check, "passed": False, "detail": server.error_text(exc)}
    except ValueError as exc:
        return {"check": check, "passed": False, "detail": f"invalid check: {exc}"}


def _phase_task(mission: Mission, phase: Phase, index: int, previous: list[PhaseResult]) -> str:
    lines = [f"Mission: {mission.name}. Phase {index} of {len(mission.phases)}: {phase.name}.",
             phase.task.strip()]
    if previous:
        done = ", ".join(f"{p.name} ({'passed' if p.passed else 'failed'})" for p in previous)
        lines.append(f"Earlier phases: {done}. Their facts are in your notes.")
    if phase.checks:
        lines.append("This phase is complete only when: "
                     + "; ".join(json.dumps(c) for c in phase.checks)
                     + ". Verify before calling done.")
    if phase.apps:
        lines.append(f"Apps you may drive in this phase: {', '.join(phase.apps)}.")
    return "\n".join(lines)


def run(
    mission: Mission,
    runtime: server.Runtime,
    provider: Provider,
    *,
    runs_dir: str | Path = "runs",
    from_phase: int = 1,
    on_step=None,
    confirm=None,
    now: float | None = None,
) -> MissionResult:
    """Run every phase in order, verifying each with its checks.

    Grants the phase's apps at the phase's tier for the duration of the phase
    (and restores the previous grants), runs the agent loop, evaluates the
    checks, retries with the failure noted, and stops at the first phase that
    still fails or when the mission deadline passes.
    """
    started = time.time() if now is None else now
    stamp = datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(runs_dir) / mission.name / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    timeline = _Timeline(run_dir / "timeline.jsonl")
    timeline.event("mission_start", mission=mission.name, source=str(mission.source))
    _run_hook(mission.record_start, timeline, "record_start")
    grants = _Grants(runtime.store)
    results: list[PhaseResult] = []
    stopped, passed = "completed", True
    mono0 = time.monotonic()

    def remaining() -> float | None:
        if mission.deadline_s is None:
            return None
        return mission.deadline_s - (time.monotonic() - mono0)

    def step_hook(phase_name: str):
        def hook(step: agent.Step) -> None:
            timeline.event("step", phase=phase_name, index=step.index, tool=step.tool,
                           ok=step.ok, error=step.error_code, duration_ms=round(step.duration_ms, 1))
            if on_step is not None:
                on_step(phase_name, step)
        return hook

    try:
        for index, phase in enumerate(mission.phases, 1):
            if index < from_phase:
                continue
            left = remaining()
            if left is not None and left <= 0:
                stopped, passed = "deadline", False
                break
            phase_started = time.time()
            timeline.event("phase_start", phase=phase.name, index=index)
            attempts, phase_passed = 0, False
            attempt_results: list[dict] = []
            check_rows: list[dict] = []
            grants.grant(phase.apps, phase.tier)
            try:
                while attempts <= phase.retries and not phase_passed:
                    attempts += 1
                    left = remaining()
                    if left is not None and left <= 0:
                        stopped = "deadline"
                        break
                    task = _phase_task(mission, phase, index, results)
                    result = agent.run_task(
                        task, runtime, provider,
                        app=phase.apps[0] if phase.apps else None,
                        max_steps=phase.max_steps, verify=phase.verify,
                        on_step=step_hook(phase.name), confirm=confirm,
                        deadline_s=left,
                    )
                    attempt_results.append(result.to_dict())
                    timeline.event("phase_attempt", phase=phase.name, attempt=attempts,
                                   success=result.success, stopped=result.stopped,
                                   steps=len(result.steps))
                    check_rows = [evaluate_check(runtime, c, timeout_s=phase.check_timeout_s)
                                  for c in phase.checks]
                    for row in check_rows:
                        timeline.event("check", phase=phase.name, attempt=attempts, **row)
                    phase_passed = all(r["passed"] for r in check_rows) and (
                        result.success or bool(phase.checks)
                    )
                    if not phase_passed and attempts <= phase.retries:
                        failed = [r for r in check_rows if not r["passed"]]
                        why = "; ".join(f"{json.dumps(r['check'])}: {r['detail']}" for r in failed) \
                            or f"the planner stopped with {result.stopped}: {result.summary}"
                        runtime.notes_store.add(
                            f"phase {phase.name} attempt {attempts} failed: {why}. "
                            + (phase.on_fail_notes or ""), source="runner")
            finally:
                grants.restore()
            phase_result = PhaseResult(name=phase.name, attempts=attempts, passed=phase_passed,
                                       checks=check_rows, results=attempt_results,
                                       started_at=phase_started, finished_at=time.time())
            results.append(phase_result)
            (run_dir / f"phase-{index:02d}-{phase.name}.json").write_text(
                json.dumps(phase_result.to_dict(), indent=1), encoding="utf-8")
            timeline.event("phase_end", phase=phase.name, passed=phase_passed, attempts=attempts)
            if not phase_passed:
                passed = False
                if stopped == "completed":
                    stopped = "phase_failed"
                break
    finally:
        _run_hook(mission.record_stop, timeline, "record_stop")
        (run_dir / "notes.json").write_text(
            json.dumps(runtime.notes_store.all(), indent=1), encoding="utf-8")
    if stopped == "deadline":
        passed = False
    mission_result = MissionResult(mission=mission.name, passed=passed, stopped=stopped,
                                   phases=results, run_dir=run_dir, started_at=started,
                                   finished_at=time.time())
    (run_dir / "result.json").write_text(json.dumps(mission_result.to_dict(), indent=1),
                                         encoding="utf-8")
    timeline.event("mission_end", passed=passed, stopped=stopped)
    return mission_result


__all__ = ["Mission", "MissionResult", "Phase", "PhaseResult", "RUNNER_CHECKS",
           "evaluate_check", "load", "run"]
