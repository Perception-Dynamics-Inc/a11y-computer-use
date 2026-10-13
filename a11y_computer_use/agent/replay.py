"""Record a successful agent run and replay it without a model.

A skill file stores the goal, one step per action, and a locator for each
target: role, accessible name, and, when a snapshot is supplied, ``stable_id``
and the role path. The recorded ref is a hint. ``x`` and ``y`` are not stored
as a click target. Replay resolves the locator on a fresh snapshot, checks
that a still-present ref still names that control, and sends the live ref.
A stored point is never passed to ``click`` or ``scroll``.

A step whose locator is missing, ambiguous, reused, disabled, opaque, or
whose check fails is a divergence. The model then runs from the current
screen, and a successful continuation replaces the skill from that step
forward. A clean replay does not call the model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from a11y_computer_use.agent.actions import POINTER_CLICKS, Action, pointer_to_click
from a11y_computer_use.agent.core import (
    _by_ref,
    _exact_controls,
    _exact_name,
    _is_opaque_role,
    _is_verified,
    _roles_match,
    blocking_human,
    check_conditions,
    snapshot_digest,
    to_runtime_call,
)
from a11y_computer_use.agent.result import RunResult, StepRecord
from a11y_computer_use.schema import ComputerUseError, Snapshot

SKILL_VERSION = 1

#: Actions whose target is a control. Replay binds a live ref or stops.
_ELEMENT_ACTIONS = frozenset({
    *POINTER_CLICKS,
    "type",
    "set_value",
    "select",
    "crop",
    "expand",
    "choose_file",
})

#: No durable locator, or the agent loop executes them outside ``call_tool``.
#: Replay stops and hands the step to the model.
_MODEL_ACTIONS = frozenset({"ask_human", "shell", "python", "expand", "choose_file"})

_POINT_KEYS = ("x", "y")


@dataclass(frozen=True)
class Divergence:
    """The first skill step replay would not execute."""

    index: int
    reason: str


def skill_from_run(
    goal: str,
    result: RunResult,
    *,
    snapshots: Sequence[Snapshot | None] | None = None,
) -> dict:
    """A version-1 skill for a successful ``result``.

    ``snapshots`` is optional, one entry per ``step_log`` item, taken while
    that step's ref was still valid. A snapshot adds ``stable_id`` and
    ``path`` to the locator. Coordinates on a click or scroll are omitted.
    """
    if result.status != "success":
        raise ValueError("only a successful run can be saved as a skill")
    steps = []
    for index, step in enumerate(result.step_log):
        snap = None
        if snapshots is not None and index < len(snapshots):
            snap = snapshots[index]
        steps.append(_step_from_record(step, snap))
    return {"version": SKILL_VERSION, "goal": goal, "steps": steps}


def save_skill(path: str | Path, skill: Mapping) -> None:
    """Write ``skill`` as JSON. The file is replaced only after it is complete."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(skill), indent=2, ensure_ascii=False) + "\n"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)


def load_skill(path: str | Path) -> dict:
    """Read a version-1 skill file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != SKILL_VERSION:
        raise ValueError("skill file is not version 1")
    if not isinstance(data.get("goal"), str) or not isinstance(data.get("steps"), list):
        raise ValueError("skill file needs a goal and a list of steps")
    return data


def run_with_skill(agent: object, goal: str, path: str | Path) -> RunResult:
    """Replay ``path`` when it is this goal, otherwise run the model and save.

    A clean replay returns without calling the model. Any divergence calls
    ``agent.run`` on the current screen. When that run succeeds, the skill
    keeps the steps that replayed and replaces the rest with the model's
    steps. A failed continuation leaves the file as it was. A file for a
    different goal is not replayed and not overwritten.
    """
    target = Path(path)
    skill: dict | None = None
    if target.is_file():
        skill = load_skill(target)
        if skill.get("goal") == goal:
            outcome = replay_skill(agent, skill, goal=goal)
            if outcome.divergence is None and outcome.result is not None:
                return outcome.result
            divergence = outcome.divergence or Divergence(0, "replay did not finish")
            resumed = agent.run(_resume_goal(goal, divergence))  # type: ignore[attr-defined]
            if resumed.status == "success":
                fresh = skill_from_run(goal, resumed)
                merged = {
                    "version": SKILL_VERSION,
                    "goal": goal,
                    "steps": list(skill["steps"][: divergence.index]) + list(fresh["steps"]),
                }
                save_skill(target, merged)
            return resumed
        skill = None
    result = agent.run(goal)  # type: ignore[attr-defined]
    if result.status == "success" and skill is None and not _other_goal(target, goal):
        save_skill(target, skill_from_run(goal, result))
    return result


def replay_skill(agent: object, skill: Mapping, *, goal: str) -> "ReplayOutcome":
    """Execute ``skill`` against ``agent``'s runtime. The model is not called."""
    runtime = getattr(agent, "runtime", None) or getattr(agent, "_runtime", None)
    if runtime is None:
        return ReplayOutcome(None, Divergence(0, "agent has no runtime"))
    if skill.get("goal") != goal:
        return ReplayOutcome(None, Divergence(0, "skill goal does not match"))
    steps = skill.get("steps")
    if not isinstance(steps, list) or not steps:
        return ReplayOutcome(None, Divergence(0, "skill has no steps"))
    app = _frontmost(runtime)
    started = time.perf_counter()
    log: list[StepRecord] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return ReplayOutcome(None, Divergence(index, "step is not an object"))
        try:
            _text, snap = _observe(runtime, app)
        except Exception as exc:  # noqa: BLE001 - the model takes the screen from here
            return ReplayOutcome(None, Divergence(index, f"snapshot failed: {exc}"))
        if snap is None:
            return ReplayOutcome(None, Divergence(index, "snapshot has no element tree"))
        blocked = blocking_human(snap)
        if blocked is not None:
            kind = str(blocked.get("kind") or "needs_human")
            return ReplayOutcome(None, Divergence(index, f"needs_human: {kind}"))
        action_name = str(step.get("action") or "")
        if action_name == "done":
            return _finish_done(step, snap, goal, log, started)
        reason = _refuse_reason(step)
        if reason is not None:
            return ReplayOutcome(None, Divergence(index, reason))
        if action_name in _ELEMENT_ACTIONS or _scroll_needs_target(step):
            resolved = _resolve(snap, step.get("locator"))
            if isinstance(resolved, str):
                return ReplayOutcome(None, Divergence(index, resolved))
            element = resolved
        else:
            element = None
        before = snapshot_digest(snap, "")
        try:
            tool, params = _runtime_params(step, element, app)
        except (ValueError, KeyError) as exc:
            return ReplayOutcome(None, Divergence(index, str(exc)))
        if tool in {"click", "scroll", "hover", "drag"} and (
            "x" in params or "y" in params
        ):
            return ReplayOutcome(None, Divergence(index, "refusing stored coordinates"))
        started_at = time.perf_counter()
        try:
            raw = _call(runtime, tool, params)
            error = None
        except ComputerUseError as exc:
            return ReplayOutcome(None, Divergence(index, exc.message))
        except Exception as exc:  # noqa: BLE001 - a tool failure is a divergence
            return ReplayOutcome(None, Divergence(index, f"{type(exc).__name__}: {exc}"))
        try:
            _after_text, after_snap = _observe(runtime, app)
        except Exception as exc:  # noqa: BLE001
            return ReplayOutcome(None, Divergence(index, f"snapshot failed: {exc}"))
        if after_snap is None:
            return ReplayOutcome(None, Divergence(index, "snapshot has no element tree"))
        after = snapshot_digest(after_snap, "")
        action = _action_for(step, element)
        if not _is_verified(action, before, after, error):
            return ReplayOutcome(
                None, Divergence(index, "check failed: the snapshot did not change"),
            )
        log.append(StepRecord(
            index=len(log) + 1,
            action=action.name,
            target=_target(element),
            args=_public_step_args(step, element),
            result=raw if isinstance(raw, str) else tool,
            error=None,
            verified=True,
            duration_s=time.perf_counter() - started_at,
        ))
    return ReplayOutcome(None, Divergence(len(steps), "skill ended without a done check"))


@dataclass
class ReplayOutcome:
    """A finished replay, or the step that stopped it."""

    result: RunResult | None
    divergence: Divergence | None


def _other_goal(path: Path, goal: str) -> bool:
    if not path.is_file():
        return False
    try:
        saved = load_skill(path)
    except ValueError:
        return True
    return saved.get("goal") != goal


def _resume_goal(goal: str, divergence: Divergence) -> str:
    return (
        f"{goal}\n\n"
        f"A saved skill replayed the earlier steps and diverged at step {divergence.index} "
        f"({divergence.reason}). Those earlier steps were already executed and checked. "
        "Continue from the current screen. Do not repeat a step whose effect is already done."
    )


def _step_from_record(step: StepRecord, snap: Snapshot | None) -> dict:
    args = dict(step.args)
    point_dropped = any(key in args for key in _POINT_KEYS)
    if step.action != "window":
        for key in ("x", "y", "display_id"):
            args.pop(key, None)
    target = dict(step.target or {})
    role = str(target.get("role") or "")
    name = str(target.get("name") or "")
    ref = str(target.get("ref") or "")
    args.pop("ref", None)
    locator = None
    if role or name or ref:
        locator = {"role": role, "name": name}
        if ref:
            locator["ref"] = ref
        element = _by_ref(snap, ref) if snap is not None and ref else None
        if element is not None:
            if element.stable_id:
                locator["stable_id"] = element.stable_id
            if element.path:
                locator["path"] = list(element.path)
    recorded = {
        "action": step.action,
        "args": args,
        "locator": locator,
        "check": _check_spec(step),
    }
    if point_dropped and step.action != "window":
        recorded["point_dropped"] = True
    if not step.verified:
        recorded["replayable"] = False
    return recorded


def _check_spec(step: StepRecord) -> dict:
    if step.action == "done":
        conditions = step.args.get("conditions") if isinstance(step.args, dict) else None
        return {"conditions": list(conditions or [])}
    if step.action in {"wait", "crop", "find", "expand", "scroll_to_find"}:
        return {"snapshot_changed": False}
    verb = str(step.args.get("action") or "") if isinstance(step.args, dict) else ""
    if step.action in {"app", "window", "menu"} and verb in {"list", "state"}:
        return {"snapshot_changed": False}
    return {"snapshot_changed": True}


def _finish_done(step: dict, snap: Snapshot, goal: str, log: list[StepRecord], started: float) -> ReplayOutcome:
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    conditions = args.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return ReplayOutcome(None, Divergence(len(log), "done has no conditions"))
    checked = check_conditions(conditions, snap, goal)
    if not checked or not all(item.get("ok") for item in checked):
        detail = "; ".join(str(item.get("detail") or "") for item in checked if not item.get("ok"))
        return ReplayOutcome(None, Divergence(len(log), f"done check failed: {detail}"))
    answer = str(args.get("answer") or "")
    log.append(StepRecord(
        index=len(log) + 1,
        action="done",
        target={"ref": None, "role": None, "name": None},
        args={"answer": answer, "conditions": conditions},
        result=answer,
        error=None,
        verified=True,
        duration_s=0.0,
    ))
    result = RunResult(
        status="success",
        answer=answer,
        steps=len(log),
        elapsed_s=time.perf_counter() - started,
        reason="replay",
        conditions=checked,
        step_log=log,
    )
    return ReplayOutcome(result, None)


def _refuse_reason(step: dict) -> str | None:
    action = str(step.get("action") or "")
    if step.get("replayable") is False:
        return "recorded step was not verified"
    if action in _MODEL_ACTIONS:
        return f"{action} is handed to the model"
    if _redacted(step.get("args")):
        return "recorded text was redacted"
    if action in _ELEMENT_ACTIONS and not _locator_usable(step.get("locator"), action):
        return "no durable locator; refusing stored coordinates"
    if step.get("point_dropped") and not _locator_usable(step.get("locator"), action):
        return "no durable locator; refusing stored coordinates"
    if _is_opaque_role(_locator_role(step.get("locator"))):
        return "opaque target has no durable control; refusing stored coordinates"
    return None


def _locator_role(locator: object) -> str:
    if not isinstance(locator, dict):
        return ""
    return str(locator.get("role") or "")


def _locator_usable(locator: object, action: str) -> bool:
    if not isinstance(locator, dict):
        return False
    if _is_opaque_role(str(locator.get("role") or "")):
        return False
    if locator.get("stable_id"):
        return True
    if action in _ELEMENT_ACTIONS:
        return bool(str(locator.get("role") or "").strip() and str(locator.get("name") or "").strip())
    return bool(str(locator.get("name") or "").strip() or locator.get("stable_id"))


def _scroll_needs_target(step: dict) -> bool:
    if str(step.get("action") or "") != "scroll":
        return False
    if step.get("point_dropped"):
        return True
    return isinstance(step.get("locator"), dict)


def _resolve(snap: Snapshot, locator: object):
    """The live element for ``locator``, or a reason string.

    A ref that still carries the recorded role and name wins. A ref that now
    names something else is not clicked. ``stable_id`` wins over the name
    when it still agrees with the role and name. More than one match stops
    the replay.
    """
    if not isinstance(locator, dict):
        return "no durable locator; refusing stored coordinates"
    role = str(locator.get("role") or "")
    name = str(locator.get("name") or "")
    stable = str(locator.get("stable_id") or "")
    recorded_ref = str(locator.get("ref") or "")
    path = locator.get("path")
    if _is_opaque_role(role):
        return "opaque target has no durable control; refusing stored coordinates"
    if stable:
        hits = [element for element in snap.elements if element.stable_id == stable]
        if len(hits) > 1:
            return "stable_id matched more than one element"
        if len(hits) == 1:
            element = hits[0]
            if role and not _roles_match(element.role, role):
                return "stable_id role changed"
            if name and _exact_name(element.title) != _exact_name(name):
                return "stable_id name changed"
            if not element.enabled or _is_opaque_role(element.role):
                return "target is not an enabled control"
            return element
    if not role or not name:
        return "no durable locator; refusing stored coordinates"
    found = _exact_controls(snap, name=name, role=role)
    if recorded_ref:
        live = _by_ref(snap, recorded_ref)
        if live is not None and _same_control(live, role, name):
            if any(element.ref == recorded_ref for element in found):
                return live
            return "recorded ref is no longer that control"
        if live is not None:
            found = [element for element in found if element.ref != recorded_ref]
    if isinstance(path, list) and path:
        pathed = [element for element in found if list(element.path) == list(path)]
        if len(pathed) == 1:
            return pathed[0]
        if not pathed:
            return "locator path no longer matches"
        return "locator matched more than one element"
    if len(found) == 1:
        return found[0]
    if not found:
        return "no element matches the locator"
    return "locator matched more than one element"


def _same_control(element, role: str, name: str) -> bool:
    if role and not _roles_match(element.role, role):
        return False
    if name and _exact_name(element.title) != _exact_name(name):
        return False
    return bool(role or name)


def _runtime_params(step: dict, element, app: str | None) -> tuple[str, dict]:
    action = _action_for(step, element)
    tool, params = to_runtime_call(action, app)
    if tool in {"click", "scroll", "hover", "drag"}:
        params.pop("x", None)
        params.pop("y", None)
        params.pop("display_id", None)
    return tool, params


def _action_for(step: dict, element) -> Action:
    name = str(step.get("action") or "")
    args = dict(step.get("args") or {})
    # A window move keeps its frame. A click or scroll must not keep a point.
    if name != "window":
        for key in ("x", "y", "display_id", "label"):
            args.pop(key, None)
    args.pop("ref", None)
    if element is not None:
        args["ref"] = element.ref
    action = pointer_to_click(Action(name=name, args=args))
    if action.name in POINTER_CLICKS or action.name == "click":
        click_args = {"ref": args.get("ref")}
        if action.args.get("button"):
            click_args["button"] = action.args["button"]
        if action.args.get("count"):
            click_args["count"] = action.args["count"]
        return Action(name="click", args=click_args)
    return action


def _public_step_args(step: dict, element) -> dict:
    args = dict(step.get("args") or {})
    for key in ("x", "y", "display_id"):
        args.pop(key, None)
    if element is not None:
        args["ref"] = element.ref
    return args


def _target(element) -> dict:
    if element is None:
        return {"ref": None, "role": None, "name": None}
    return {"ref": element.ref, "role": element.role, "name": element.title}


def _redacted(args: object) -> bool:
    if not isinstance(args, dict):
        return False
    for value in args.values():
        if isinstance(value, str) and "[REDACTED]" in value:
            return True
    return False


def _frontmost(runtime: object) -> str:
    fn = getattr(runtime, "_frontmost", None)
    if callable(fn):
        try:
            name = fn()
        except Exception:  # noqa: BLE001 - a test double may not name an app
            name = None
        if isinstance(name, str) and name.strip():
            return name
    return "frontmost"


def _observe(runtime: object, app: str) -> tuple[object, Snapshot | None]:
    raw = runtime.desktop_snapshot(app, mode="full")  # type: ignore[attr-defined]
    current = getattr(runtime, "_current", None)
    if isinstance(current, Snapshot):
        return raw, current
    if isinstance(raw, Snapshot):
        return "", raw
    return raw, None


def _call(runtime: object, tool: str, params: dict):
    confirm = None
    return runtime.call_tool(tool, params, confirm=confirm)  # type: ignore[attr-defined]


__all__ = [
    "Divergence",
    "ReplayOutcome",
    "SKILL_VERSION",
    "load_skill",
    "replay_skill",
    "run_with_skill",
    "save_skill",
    "skill_from_run",
]
