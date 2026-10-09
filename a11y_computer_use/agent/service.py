"""In-process runs shared by the HTTP server and the agent MCP server.

One run is active per display. A second start on that display raises
`DisplayBusy`. Risky actions block in the approve hook until
``POST .../approvals/{id}`` or the MCP ``approve`` tool answers, or until
``approval_timeout_s`` elapses, which denies the action.
"""

from __future__ import annotations

import copy
import inspect
import json
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from a11y_computer_use.agent.actions import approval_target
from a11y_computer_use.agent.result import RunResult
from a11y_computer_use.agent.trace import redact_args, summarize_args
from a11y_computer_use.agent.ui_text import fence_ui
from a11y_computer_use.untrusted import fence_untrusted


class DisplayBusy(Exception):
    """Another run is still active on this display."""

    def __init__(self, display: str, run_id: str) -> None:
        super().__init__(display)
        self.display = display
        self.run_id = run_id


def display_key(display: str | None) -> str:
    """Key for the one-active-run rule. Blank and omitted share one slot."""
    text = "" if display is None else str(display).strip()
    return text or "default"


def fence_trajectory(text: str) -> str:
    """Return JSONL with UI strings fenced.

    Observation text, element names, tool results, and condition details are
    wrapped. Model response text is left as the model wrote it.
    """
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        if isinstance(item, dict):
            for key in ("observation", "result", "error", "answer", "window", "title"):
                if isinstance(item.get(key), str) and item[key]:
                    item[key] = fence_ui(item[key])
            if isinstance(item.get("target"), dict):
                item["target"] = _fence_target(item["target"])
            if "conditions" in item:
                item["conditions"] = _fence_conditions(item["conditions"])
        lines.append(json.dumps(item, ensure_ascii=False))
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _fence_text(value: object) -> object:
    if isinstance(value, str) and value:
        return fence_ui(value)
    return value


def _fence_target(target: object) -> object:
    if not isinstance(target, dict):
        return target
    copied = dict(target)
    if "name" in copied:
        copied["name"] = _fence_text(copied.get("name"))
    return copied


def _fence_conditions(items: object) -> object:
    if not isinstance(items, list):
        return items
    fenced = []
    for item in items:
        if not isinstance(item, dict):
            fenced.append(item)
            continue
        copied = dict(item)
        if "detail" in copied:
            copied["detail"] = _fence_text(copied.get("detail"))
        fenced.append(copied)
    return fenced


def _fence_steps(steps: object) -> object:
    if not isinstance(steps, list):
        return steps
    fenced = []
    for step in steps:
        if not isinstance(step, dict):
            fenced.append(step)
            continue
        copied = dict(step)
        if "target" in copied:
            copied["target"] = _fence_target(copied.get("target"))
        if "result" in copied:
            copied["result"] = _fence_text(copied.get("result"))
        if "error" in copied:
            copied["error"] = _fence_text(copied.get("error"))
        fenced.append(copied)
    return fenced


def _fence_payload(kind: str, data: dict) -> dict:
    """Fence UI strings in one event payload. The input dict is not mutated."""
    payload = copy.deepcopy(data)
    if kind == "observation" and "text" in payload:
        payload["text"] = _fence_text(payload.get("text"))
    if kind == "needs_human":
        for key in ("message", "window", "url", "name"):
            if key in payload:
                payload[key] = _fence_text(payload.get(key))
    if kind == "action" and "target" in payload:
        payload["target"] = _fence_target(payload.get("target"))
    if kind == "step_finished":
        for key in ("result", "error"):
            if key in payload:
                payload[key] = _fence_text(payload.get(key))
    if kind == "done":
        if "answer" in payload:
            payload["answer"] = _fence_text(payload.get("answer"))
        if "conditions" in payload:
            payload["conditions"] = _fence_conditions(payload.get("conditions"))
    if kind == "error" and payload.get("reason") not in _STATUS_REASONS:
        payload["reason"] = _fence_text(payload.get("reason"))
    return payload


_STATUS_REASONS = frozenset({
    "done",
    "max_steps",
    "max_time",
    "stuck",
    "no_action",
    "cancelled",
})


def _fence_result(body: dict) -> dict:
    """Fence UI strings in a CLI-shaped result. Keys stay the CLI set."""
    copied = dict(body)
    if "answer" in copied:
        copied["answer"] = _fence_text(copied.get("answer"))
    reason = copied.get("reason")
    if isinstance(reason, str) and reason and reason not in _STATUS_REASONS:
        copied["reason"] = fence_ui(reason)
    human = copied.get("needs_human")
    if isinstance(human, dict):
        fenced = dict(human)
        for key in ("message", "window", "url", "name"):
            if key in fenced:
                fenced[key] = _fence_text(fenced.get(key))
        copied["needs_human"] = fenced
    if "step_log" in copied:
        copied["step_log"] = _fence_steps(copied["step_log"])
    if "conditions" in copied:
        copied["conditions"] = _fence_conditions(copied["conditions"])
    return copied


@dataclass
class RunRecord:
    """One goal, its event log, and any approvals waiting on a client."""

    id: str
    display_key: str
    trace_dir: Path
    started: float
    cond: threading.Condition = field(default_factory=threading.Condition)
    events: list[dict] = field(default_factory=list)
    approvals: dict[str, dict] = field(default_factory=dict)
    result: dict | None = None
    done: bool = False
    cancel_requested: bool = False
    agent: object | None = None
    seq: int = 0

    def append(self, kind: str, data: dict) -> dict:
        """Record one event and wake SSE listeners."""
        payload = _fence_payload(kind, data)
        with self.cond:
            self.seq += 1
            event = {"seq": self.seq, "type": kind, "data": payload}
            self.events.append(event)
            self.cond.notify_all()
        return event

    def finish(self, result: dict) -> None:
        with self.cond:
            self.result = _fence_result(result)
            self.done = True
            self.cond.notify_all()
        for item in list(self.approvals.values()):
            item["ready"].set()

    def pending_approvals(self) -> list[dict]:
        waiting = []
        for item in self.approvals.values():
            if item.get("answer") is None and not item["ready"].is_set():
                waiting.append({
                    "approval_id": item["id"],
                    **item["public"],
                })
        return waiting


class RunStore:
    """Start and inspect agent runs. Safe to call from several threads."""

    def __init__(
        self,
        *,
        approval_timeout_s: float = 60.0,
        runtime_factory: Callable[[], object] | None = None,
        model_factory: Callable[[str], object] | None = None,
        trace_root: str | Path | None = None,
    ) -> None:
        if approval_timeout_s <= 0:
            raise ValueError("approval_timeout_s must be positive")
        self.approval_timeout_s = float(approval_timeout_s)
        self.runtime_factory = runtime_factory
        self.model_factory = model_factory
        self.trace_root = None if trace_root is None else Path(trace_root)
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def start(self, body: dict) -> RunRecord:
        """Validate ``body`` and start a run. Raises `DisplayBusy` or ValueError."""
        goal, spec, display, limits, allow_exec, allow_payments, domains, blocked = _parse_run_body(body)
        key = display_key(display)
        run_id = uuid.uuid4().hex
        trace_dir = self._trace_dir(run_id)
        record = RunRecord(
            id=run_id,
            display_key=key,
            trace_dir=trace_dir,
            started=time.perf_counter(),
        )
        with self._lock:
            for existing in self._runs.values():
                if existing.display_key == key and not existing.done:
                    raise DisplayBusy(key, existing.id)
            self._runs[run_id] = record
        worker = threading.Thread(
            target=self._worker,
            args=(record, goal, spec, display, limits, allow_exec, allow_payments, domains, blocked),
            name=f"a11y-agent-{run_id[:8]}",
            daemon=True,
        )
        worker.start()
        return record

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def view(self, record: RunRecord) -> dict:
        """CLI ``--json`` object once finished, plus pending approvals while running."""
        if record.result is not None:
            return dict(record.result)
        steps: list[dict] = []
        agent = record.agent
        if agent is not None:
            raw = getattr(agent, "_steps", None)
            if raw:
                steps = [step.to_dict() for step in list(raw)]
        body = _fence_result({
            "status": "running",
            "answer": "",
            "steps": len(steps),
            "elapsed_s": round(time.perf_counter() - record.started, 3),
            "reason": "",
            "conditions": [],
            "needs_human": None,
            "trace_dir": str(record.trace_dir),
            "step_log": steps,
        })
        pending = record.pending_approvals()
        if pending:
            body["pending_approvals"] = pending
        return body

    def cancel(self, run_id: str) -> RunRecord | None:
        record = self.get(run_id)
        if record is None:
            return None
        record.cancel_requested = True
        agent = record.agent
        cancel = getattr(agent, "cancel", None)
        if callable(cancel):
            cancel()
        for item in list(record.approvals.values()):
            item["ready"].set()
        return record

    def resolve_approval(self, run_id: str, approval_id: str, approve: bool) -> str:
        """Record a decision. Returns ``ok``, ``missing``, or ``answered``."""
        record = self.get(run_id)
        if record is None:
            return "missing"
        item = record.approvals.get(approval_id)
        if item is None:
            return "missing"
        with record.cond:
            if item.get("answer") is not None:
                return "answered"
            item["answer"] = bool(approve)
            item["ready"].set()
            return "ok"

    def trajectory(self, record: RunRecord) -> str:
        path = record.trace_dir / "trajectory.jsonl"
        if not path.is_file():
            return ""
        return fence_trajectory(path.read_text(encoding="utf-8"))

    def trace_files(self, record: RunRecord) -> list[str]:
        if not record.trace_dir.is_dir():
            return []
        names = [
            path.name
            for path in sorted(record.trace_dir.iterdir())
            if path.is_file()
        ]
        return names

    def trace_file(self, record: RunRecord, name: str) -> Path | None:
        if not _safe_name(name):
            return None
        path = record.trace_dir / name
        if not path.is_file():
            return None
        return path

    def _trace_dir(self, run_id: str) -> Path:
        if self.trace_root is not None:
            path = self.trace_root / run_id
            path.mkdir(parents=True, exist_ok=True)
            return path
        return Path(tempfile.mkdtemp(prefix=f"a11y-agent-{run_id[:8]}-"))

    def _worker(
        self,
        record: RunRecord,
        goal: str,
        spec: str,
        display: str | None,
        limits: dict,
        allow_exec: bool,
        allow_payments: bool,
        domains: list[str] | None,
        blocked: list[str] | None,
    ) -> None:
        try:
            agent = self._make_agent(
                record, spec, display, limits, allow_exec, allow_payments, domains, blocked,
            )
            record.agent = agent
            if record.cancel_requested:
                agent.cancel()

            def on_event(event: object) -> None:
                # run() clears a cancel that arrived before the loop started.
                if record.cancel_requested:
                    agent.cancel()
                kind = getattr(event, "type", None)
                data = getattr(event, "data", None)
                if not isinstance(kind, str) or not isinstance(data, dict):
                    return
                record.append(kind, data)

            agent.on_event = on_event
            if record.cancel_requested:
                agent.cancel()
            result = agent.run(goal)
            record.finish(result.to_dict())
        except Exception as exc:  # noqa: BLE001 - the HTTP client must get a result
            record.finish(_failed(record, exc))

    def _make_agent(
        self,
        record: RunRecord,
        spec: str,
        display: str | None,
        limits: dict,
        allow_exec: bool,
        allow_payments: bool,
        domains: list[str] | None,
        blocked: list[str] | None,
    ):
        from a11y_computer_use.agent.core import Agent

        model: object = spec
        if self.model_factory is not None:
            model = self.model_factory(spec)
        kwargs: dict = {
            "display": display,
            "max_steps": limits["max_steps"],
            "max_time_s": limits["max_time_s"],
            "model_timeout_s": limits["model_timeout_s"],
            "approve": lambda action, record=record: self._approve(record, action),
            "auto_deny": False,
            "allow_exec": allow_exec,
            "allow_payments": allow_payments,
            "trace_dir": record.trace_dir,
        }
        if self.runtime_factory is not None:
            kwargs["runtime"] = self.runtime_factory()
        params = inspect.signature(Agent.__init__).parameters
        if domains is not None and "allowed_domains" in params:
            kwargs["allowed_domains"] = domains
        if blocked is not None and "blocked_domains" in params:
            kwargs["blocked_domains"] = blocked
        return Agent(model, **kwargs)

    def _approve(self, record: RunRecord, action: object) -> bool:
        if record.cancel_requested:
            return False
        approval_id = uuid.uuid4().hex
        public = approval_public(action)
        ready = threading.Event()
        item = {
            "id": approval_id,
            "name": public["name"],
            "args": public["args"],
            "public": public,
            "answer": None,
            "ready": ready,
        }
        record.approvals[approval_id] = item
        record.append("approval_required", {"approval_id": approval_id, **public})
        ready.wait(timeout=self.approval_timeout_s)
        with record.cond:
            if item.get("answer") is None:
                item["answer"] = False
            approved = item.get("answer") is True
        if record.cancel_requested:
            return False
        return approved


def approval_public(action: object) -> dict:
    """The labelled target shared by the SSE event and ``pending_approvals``.

    ``name`` stays the action name. ``target`` is role, accessible name,
    window, page URL, and reason. Name, window, URL, and the argument summary
    are trimmed and fenced. Role and reason are loop tokens.
    """
    args, _secrets = redact_args(dict(getattr(action, "args", {}) or {}))
    target = approval_target(action)  # type: ignore[arg-type]
    body: dict = {
        "name": str(getattr(action, "name", "")),
        "args": args,
        "target": target,
    }
    if target.get("reason"):
        body["reason"] = target["reason"]
    summary = getattr(action, "summary", None)
    if not isinstance(summary, str) or not summary:
        summary = summarize_args(args)
    if summary and summary != "{}":
        fenced = fence_untrusted(summary, limit=160)
        if fenced:
            body["summary"] = fenced
    return body


def _parse_run_body(body: dict) -> tuple[str, str, str | None, dict, bool, bool, list[str] | None, list[str] | None]:
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    goal = body.get("goal")
    model = body.get("model")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model is required")
    display = body.get("display")
    if display is not None and not isinstance(display, str):
        raise ValueError("display must be a string")
    limits = body.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    max_steps = limits.get("max_steps", 50)
    max_time_s = limits.get("max_time_s", 900)
    model_timeout_s = limits.get("model_timeout_s", 120)
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
        raise ValueError("limits.max_steps must be a positive integer")
    if isinstance(max_time_s, bool) or not isinstance(max_time_s, (int, float)) or float(max_time_s) <= 0:
        raise ValueError("limits.max_time_s must be a positive number")
    if (
        isinstance(model_timeout_s, bool)
        or not isinstance(model_timeout_s, (int, float))
        or float(model_timeout_s) <= 0
    ):
        raise ValueError("limits.model_timeout_s must be a positive number")
    if "allow_exec" in body and not isinstance(body.get("allow_exec"), bool):
        raise ValueError("allow_exec must be a boolean")
    if "allow_payments" in body and not isinstance(body.get("allow_payments"), bool):
        raise ValueError("allow_payments must be a boolean")
    allow_exec = bool(body.get("allow_exec", False))
    allow_payments = bool(body.get("allow_payments", False))
    domains = _domains(body.get("allowed_domains", None), "allowed_domains")
    blocked = _domains(body.get("blocked_domains", None), "blocked_domains")
    return (
        goal,
        model,
        display,
        {
            "max_steps": int(max_steps),
            "max_time_s": float(max_time_s),
            "model_timeout_s": float(model_timeout_s),
        },
        allow_exec,
        allow_payments,
        domains,
        blocked,
    )


def _domains(value: object, field: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        return parts
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return [item.strip() for item in value]
    raise ValueError(f"{field} must be a list of strings or a comma-separated string")


def _failed(record: RunRecord, exc: BaseException) -> dict:
    return {
        "status": "failed",
        "answer": "",
        "steps": 0,
        "elapsed_s": round(time.perf_counter() - record.started, 3),
        "reason": f"error: {type(exc).__name__}: {exc}",
        "conditions": [],
        "needs_human": None,
        "trace_dir": str(record.trace_dir),
        "step_log": [],
    }


def _safe_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    if "/" in name or "\\" in name:
        return False
    return Path(name).name == name


def running_result(result: RunResult) -> dict:
    """Present for callers that already have a `RunResult`."""
    return _fence_result(result.to_dict())


__all__ = [
    "DisplayBusy",
    "RunRecord",
    "RunStore",
    "display_key",
    "fence_trajectory",
]
