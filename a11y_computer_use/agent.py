"""Reference agent loop: observe -> plan -> act -> verify, end to end.

``a11y_computer_use agent --task "..."`` is the one-command demonstration of the
whole stack. A planner (any `a11y_computer_use.providers.Provider`) reads the
pruned accessibility snapshot, picks tools by element ref, and the loop runs
each call through the gated `server.Runtime`, so permission tiers, the
frontmost recheck, secure-field refusal, the confirmation gate, Effect
Receipts, and the JSONL audit log all apply exactly as they do under the MCP
server. The same loop is the planner harness cu-arena uses to compare
observation strategies.

The loop keeps the conversation small: the planner always sees the latest
observation in full, and earlier observations are replaced with one-line
placeholders once a newer one exists (for providers whose API allows history
edits; the Anthropic provider bounds context server-side instead).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from a11y_computer_use import server
from a11y_computer_use.providers import PlannerTurn, Provider, ProviderError, ToolCall, Usage
from a11y_computer_use.schema import ComputerUseError, ErrorCode

#: Tools whose result is a fresh observation the planner acts on next.
OBSERVATION_TOOLS = frozenset(
    {"desktop_snapshot", "find", "screenshot", "zoom", "screen_text", "scroll_to_find",
     "console", "network"}
)

#: The terminal tool the loop adds to the MCP surface.
DONE_TOOL = {
    "name": "done",
    "description": (
        "Finish the task. Call this exactly once, when the task is complete or "
        "cannot be completed. success=true only when an observation confirmed the "
        "outcome; summary is one or two sentences for the human. Call it alone, in "
        "its own turn, after reading the results of every other call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "What was done, or why it stopped."},
            "success": {"type": "boolean", "description": "Whether the task was completed."},
        },
        "required": ["summary", "success"],
    },
}

_EXCERPT_CHARS = 600


@dataclass(slots=True)
class Step:
    """One executed tool call (or the terminal ``done``)."""

    index: int
    tool: str
    params: dict
    ok: bool
    result: str  #: excerpt of the tool result the planner saw
    error_code: str | None
    duration_ms: float
    usage: Usage  #: planner tokens of the turn that produced this call (first call only)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["usage"] = {"input_tokens": self.usage.input_tokens, "output_tokens": self.usage.output_tokens}
        return d


@dataclass(slots=True)
class AgentResult:
    task: str
    app: str | None
    provider: str
    model: str | None
    success: bool
    summary: str
    stopped: str  #: done | max_steps | provider_error | no_action | deadline
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    wall_time_s: float = 0.0
    audit_dir: str = ""
    compactions: int = 0  #: how many times the history was compacted (long tasks)

    def to_dict(self) -> dict:
        return {
            "task": self.task, "app": self.app, "provider": self.provider, "model": self.model,
            "success": self.success, "summary": self.summary, "stopped": self.stopped,
            "steps": [s.to_dict() for s in self.steps],
            "usage": {"input_tokens": self.usage.input_tokens,
                      "output_tokens": self.usage.output_tokens},
            "wall_time_s": round(self.wall_time_s, 2),
            "audit_dir": self.audit_dir,
            "compactions": self.compactions,
        }


def tool_specs(runtime: server.Runtime) -> list[dict]:
    """The planner's tool list: the MCP surface for ``runtime``'s driver plus ``done``."""
    return server.tool_specs(runtime) + [DONE_TOOL]


def notes_block(runtime: server.Runtime) -> str:
    """The agent's notes, rendered for the system prompt (empty when there are none)."""
    store = getattr(runtime, "notes_store", None)
    if store is None or not store.all():
        return ""
    return "\nYour notes so far (facts you recorded with the notes tool):\n" + store.render()


def system_prompt(runtime: server.Runtime, app: str | None) -> str:
    return (
        "You control a computer through a11y-computer-use tools. Work in a loop: read the latest "
        "observation, call one or more tools, read their results, repeat.\n"
        "Observations are pruned accessibility trees, one line per element: "
        "eN role \"title\" =\"value\" (flags). Act on element refs (click ref='e14', "
        "set_value ref='e3'). Refs are valid only against the latest snapshot or find result.\n"
        "After an action, check the effect block or call desktop_snapshot with mode='diff'. "
        "A stale_ref error means the UI changed; a fresh snapshot is attached to the error, "
        "use its refs. needs_permission or deny means the human must grant the app first: "
        "do not retry, call done with success=false. secure_field means a password field: "
        "stop and report.\n"
        f"The target app is {app!r} on the {runtime.driver.name} backend; pass it as the app "
        "argument where a tool takes one. Prefer refs over x/y coordinates; use screenshot "
        "only when the tree exposes no interactive elements. Use act to batch several known "
        "steps into one call.\n"
        "When the task is complete, or cannot be completed, call done with a one-sentence "
        "summary and success true or false. Never claim success without evidence from an "
        "observation. Call done by itself in its own turn, never alongside other tool calls.\n"
        "In long tasks, record facts you will need later (file paths, URLs, ids) with "
        "notes(action='add', text=...): older parts of this conversation may be compacted, "
        "your notes are shown to you on every turn. Use wait_until for renders, downloads, "
        "uploads, and deploys instead of taking repeated snapshots."
        + notes_block(runtime)
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text_of(blocks: list[dict]) -> str:
    return "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")


def _image_block(png: bytes) -> dict:
    return {"type": "image", "media_type": "image/png",
            "data": base64.b64encode(bytes(png)).decode("ascii")}


def _blocks_for(raw: object) -> list[dict]:
    """Tool results as neutral content blocks: text, or text plus an image."""
    if isinstance(raw, tuple) and len(raw) == 2 and hasattr(raw[1], "png"):
        text, image = raw
        return [{"type": "text", "text": str(text)}, _image_block(image.png)]
    if isinstance(raw, (bytes, bytearray)):
        return [_image_block(raw)]
    return [{"type": "text", "text": str(raw)}]


def _observe(runtime: server.Runtime, app: str) -> tuple[str, bool]:
    """A full snapshot of ``app`` through the gate, or the structured error text."""
    try:
        return str(runtime.call_tool("desktop_snapshot", {"app": app})), True
    except ComputerUseError as exc:
        return server.error_text(exc), False
    except server.ActionRefused as exc:
        return server.refusal_text(exc.decision), False


def _execute(runtime: server.Runtime, app: str, call: ToolCall, *, verify: bool,
             confirm, index: int, usage: Usage) -> tuple[Step, dict]:
    """Run one planner tool call through the Runtime; never raises to the planner.

    Returns the `Step` record and the tool_result block for the history.
    """
    params = dict(call.arguments)
    if verify and call.name in ("click", "act") and "verify" not in params:
        params["verify"] = True  # Effect Receipt: the post-action diff rides along
    started = time.perf_counter()
    ok, code, observation = True, None, call.name in OBSERVATION_TOOLS
    try:
        blocks = _blocks_for(runtime.call_tool(call.name, params, confirm=confirm))
    except ComputerUseError as exc:
        ok, code = False, exc.code.value
        text = server.error_text(exc)
        if exc.code is ErrorCode.STALE_REF:  # self-correct: attach the fresh tree
            fresh, _ = _observe(runtime, app)
            text += f"\n\nre-observed {app}; these refs are current:\n{fresh}"
            observation = True
        blocks = [{"type": "text", "text": text}]
    except server.ActionRefused as exc:
        ok, code = False, exc.decision.verdict.value
        blocks = [{"type": "text", "text": server.refusal_text(exc.decision)}]
    except (TypeError, ValueError, KeyError) as exc:
        ok = False
        code = "unknown_tool" if "unknown tool" in str(exc) else "invalid_arguments"
        blocks = [{"type": "text", "text": f"{code}: {call.name}: {exc}"}]
    duration_ms = (time.perf_counter() - started) * 1000.0
    step = Step(index=index, tool=call.name, params=params, ok=ok,
                result=_text_of(blocks)[:_EXCERPT_CHARS], error_code=code,
                duration_ms=duration_ms, usage=usage)
    result = {"type": "tool_result", "tool_use_id": call.id, "name": call.name,
              "content": blocks, "is_error": not ok}
    if observation:
        result["observation"] = True
    return step, result


def _bound_history(messages: list[dict]) -> None:
    """Keep only the newest observation in full.

    Older observation blocks (initial snapshot, observation-tool results, the
    re-observe attached to a stale_ref error) collapse to a one-line
    placeholder, and images outside the newest observation are dropped, so the
    planner's context stops growing with every look at the screen.
    """
    observations = [b for m in messages if m["role"] == "user"
                    for b in m["content"] if b.get("observation")]
    for block in observations[:-1]:
        if block.get("elided"):
            continue
        if block.get("type") == "tool_result":
            chars = len(_text_of(block.get("content", [])))
            block["content"] = [{"type": "text",
                                 "text": f"[{block.get('name', 'observation')} result elided: "
                                         f"{chars} chars, superseded by a newer observation]"}]
        else:
            chars = len(str(block.get("text", "")))
            block["text"] = f"[earlier observation elided: {chars} chars, superseded]"
        block["elided"] = True
    newest = observations[-1] if observations else None
    for message in messages:
        if message["role"] != "user":
            continue
        for block in message["content"]:
            if block is newest or block.get("type") != "tool_result":
                continue
            if any(b.get("type") == "image" for b in block.get("content", [])):
                block["content"] = [b for b in block["content"] if b.get("type") != "image"]
                block["content"].append({"type": "text", "text": "[image dropped: superseded]"})


def _estimate_tokens(messages: list[dict]) -> int:
    """Cheap size estimate of the history (chars / 4; images count a fixed 1,000)."""
    total = 0
    for message in messages:
        for block in message.get("content", []):
            if block.get("type") == "image":
                total += 1000
            elif block.get("type") == "tool_result":
                for inner in block.get("content", []):
                    total += 1000 if inner.get("type") == "image" else len(str(inner.get("text", ""))) // 4
            else:
                total += len(str(block.get("text", ""))) // 4
            if block.get("type") == "tool_use":
                total += len(str(block.get("input", ""))) // 4
    return total


def _first_line(text: str, limit: int = 160) -> str:
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line[:limit]


def compact_history(messages: list[dict], task: str) -> list[dict]:
    """Collapse everything but the latest exchange into one deterministic
    "so far" block, keeping the newest observation in full.

    The planner keeps: the task, one line per earlier tool call (tool, params
    excerpt, first line of its result), the newest observation text, and the
    last assistant turn with its tool results (tool_use ids must stay paired).
    Notes are not in the history at all (the system prompt carries them), so
    nothing the agent recorded is lost.
    """
    if len(messages) < 4:
        return messages
    # Locate the last assistant message; everything from it on is kept verbatim.
    last_assistant = max(i for i, m in enumerate(messages) if m["role"] == "assistant")
    head, tail = messages[:last_assistant], messages[last_assistant:]
    calls: dict[str, dict] = {}
    lines: list[str] = []
    newest_observation = ""
    for message in head:
        for block in message.get("content", []):
            if message["role"] == "assistant" and block.get("type") == "tool_use":
                calls[block.get("id", "")] = block
                lines.append(f"- {block.get('name')} {json.dumps(block.get('input', {}))[:120]}")
            elif block.get("type") == "tool_result":
                text = _text_of(block.get("content", []))
                if block.get("observation") and not block.get("elided"):
                    newest_observation = text
                status = "error" if block.get("is_error") else "ok"
                if lines:
                    lines[-1] += f" -> {status}: {_first_line(text)}"
            elif block.get("observation") and not block.get("elided"):
                newest_observation = str(block.get("text", ""))
    # The tail's observation, if any, supersedes the head's.
    for message in tail:
        for block in message.get("content", []):
            if block.get("type") == "tool_result" and block.get("observation") and not block.get("elided"):
                newest_observation = ""
    summary = "\n".join(lines) if lines else "(no earlier actions)"
    content: list[dict] = [
        {"type": "text", "text": f"Task: {task}"},
        {"type": "text", "text": "Earlier in this task (compacted history, oldest first):\n" + summary},
    ]
    if newest_observation:
        content.append({"type": "text", "text": f"Latest observation before the last turn:\n{newest_observation}",
                        "observation": True})
    return [{"role": "user", "content": content}, *tail]


def _audit(runtime: server.Runtime, app: str, action: str, params: dict, result: str,
           duration_ms: float, usage: Usage) -> None:
    """cu-meter row for the planner side: `a11y_computer_use bench audit` sums
    ``planner_*_tokens`` next to the per-action metrics the gate records."""
    runtime.audit.record({
        "app": app, "action": action, "params": params, "result": result,
        "metrics": {"duration_ms": round(duration_ms, 1),
                    "planner_input_tokens": usage.input_tokens,
                    "planner_output_tokens": usage.output_tokens},
    })


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def run_task(
    task: str,
    runtime: server.Runtime,
    provider: Provider,
    *,
    app: str | None = None,
    max_steps: int = 25,
    verify: bool = True,
    on_step: Callable[[Step], None] | None = None,
    confirm: Callable[[str], bool] | None = None,
    context_budget: int | None = 60_000,
    deadline_s: float | None = None,
) -> AgentResult:
    """Drive ``task`` to completion (or a bounded stop) with ``provider`` as the planner.

    Args:
        task: What to accomplish, in plain language.
        runtime: The gated Runtime to act through (its driver decides the backend).
        provider: The planner.
        app: Target app (bundle id or name; a tab id on the browser backend).
            Defaults to the frontmost app / bound tab.
        max_steps: Maximum planner turns. Each turn may run several tool calls.
            Missions use hundreds; the history is compacted to stay bounded.
        verify: Ask for Effect Receipts on click/act so the planner sees the
            post-action diff without a separate observation.
        on_step: Called after every executed step (progress logging).
        confirm: Human-confirmation callback for plausibly irreversible actions;
            without one they fail safe (``confirmation_declined``).
        context_budget: Approximate token size of the history above which older
            turns are compacted into a summary (providers that allow history
            edits only). None disables compaction.
        deadline_s: Wall-clock budget for the whole run; the loop stops with
            ``stopped="deadline"`` once it is exceeded between turns.
    """
    started = time.perf_counter()
    if app is None:
        app = runtime._frontmost()
    else:
        try:
            app = runtime._resolve_app(app)[1]
        except ComputerUseError:
            pass  # keep the identifier; the first snapshot reports app_not_found
    tools = tool_specs(runtime)
    model = getattr(provider, "model", None)

    steps: list[Step] = []
    usage = Usage()
    stopped, success, summary = "max_steps", False, ""
    nudges = 0
    compactions = 0

    observation, _ok = _observe(runtime, app)
    messages: list[dict] = [{"role": "user", "content": [
        {"type": "text", "text": f"Task: {task}"},
        {"type": "text", "text": f"Current observation of {app}:\n{observation}", "observation": True},
    ]}]

    for _turn in range(max_steps):
        if deadline_s is not None and time.perf_counter() - started >= deadline_s:
            stopped, summary = "deadline", f"stopped at the {deadline_s:g}s deadline without a done call"
            break
        system = system_prompt(runtime, app)  # notes change between turns
        try:
            turn: PlannerTurn = provider.plan(messages, tools, system=system)
        except ProviderError as exc:
            stopped, summary = "provider_error", f"planner error: {exc}"
            break
        except Exception as exc:  # a transport bug must not lose the agent_run audit row
            stopped, summary = "provider_error", f"planner error: {type(exc).__name__}: {exc}"
            break
        usage = usage + turn.usage
        messages.append(turn.assistant_message())

        if not turn.tool_calls:
            if turn.stop_reason == "refusal":
                stopped, summary = "provider_error", turn.text
                break
            nudges += 1
            if nudges >= 2:
                stopped, summary = "no_action", turn.text or "the planner produced no tool call"
                break
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "Reply with a tool call, or call done."}]})
            continue

        results: list[dict] = []
        finished = False
        charged = False  # a turn's planner tokens are counted once, on its first recorded row
        sole = len(turn.tool_calls) == 1
        for call in turn.tool_calls:
            step_usage = Usage() if charged else turn.usage
            if call.name == "done" and not sole:
                # Planned before the other calls' results existed: there can be no
                # evidence for it. Run the others, return their results, ask again.
                results.append({"type": "tool_result", "tool_use_id": call.id, "name": "done",
                                "is_error": True, "content": [{"type": "text", "text": (
                                    "done_not_sole: done must be the only call in its turn. Read the "
                                    "results of the other calls, then call done by itself.")}]})
                _audit(runtime, app, "agent_step", {"step": len(steps) + 1, "tool": "done",
                                                    "provider": provider.name},
                       "done_not_sole", 0.0, step_usage)
                charged = True
                continue
            if call.name == "done":
                # The schema says boolean; a string "true"/"false" (OpenAI-compatible
                # local models, the CLI's free-text JSON) is honoured as intent but
                # flagged, anything else is a failed run. Never truthiness: "false" is
                # a non-empty string.
                raw = call.arguments.get("success")
                if isinstance(raw, bool):
                    success, done_error = raw, None
                elif isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
                    success, done_error = raw.strip().lower() == "true", "invalid_done_arguments"
                else:
                    success, done_error = False, "invalid_done_arguments"
                summary = str(call.arguments.get("summary", "")).strip()
                step = Step(index=len(steps) + 1, tool="done", params=dict(call.arguments),
                            ok=done_error is None, result=summary, error_code=done_error,
                            duration_ms=0.0, usage=step_usage)
                steps.append(step)
                _audit(runtime, app, "agent_step", {"step": step.index, "tool": "done",
                                                    "provider": provider.name},
                       "ok" if step.ok else done_error, 0.0, step_usage)
                if on_step is not None:
                    on_step(step)
                finished, stopped = True, "done"
                break
            step, result = _execute(runtime, app, call, verify=verify, confirm=confirm,
                                    index=len(steps) + 1, usage=step_usage)
            charged = True
            steps.append(step)
            _audit(runtime, app, "agent_step", {"step": step.index, "tool": step.tool,
                                                "provider": provider.name},
                   "ok" if step.ok else (step.error_code or "error"), step.duration_ms, step_usage)
            if on_step is not None:
                on_step(step)
            results.append(result)
        if finished:
            break
        messages.append({"role": "user", "content": results})
        if provider.history_edits_ok:
            _bound_history(messages)
            if context_budget is not None and _estimate_tokens(messages) > context_budget:
                messages = compact_history(messages, task)
                compactions += 1

    if stopped == "max_steps" and not summary:
        summary = f"stopped after {max_steps} planner turns without a done call"
    wall = time.perf_counter() - started
    _audit(runtime, app, "agent_run",
           {"task": task, "provider": provider.name, "model": model, "steps": len(steps),
            "stopped": stopped, "success": success, "compactions": compactions},
           "ok" if success else stopped, wall * 1000.0, usage)
    return AgentResult(task=task, app=app, provider=provider.name, model=model,
                       success=success, summary=summary, stopped=stopped, steps=steps,
                       usage=usage, wall_time_s=wall, audit_dir=str(runtime.audit.dir_path),
                       compactions=compactions)


__all__ = ["AgentResult", "DONE_TOOL", "OBSERVATION_TOOLS", "Step", "compact_history",
           "notes_block", "run_task", "system_prompt", "tool_specs"]
