"""cu-arena head-to-head: the same planner, refs versus pixels, on a fixed task suite.

``computeruse bench web`` measures what one observation costs. This module
measures what a whole task costs, and whether it gets done: the same planner
model runs every task in the suite under `computeruse/arena_tasks/` through

* ``refs``         the reference agent loop (`computeruse.agent.run_task`):
                   pruned accessibility snapshots, element refs, Effect Receipts;
* ``pixels``       a screenshot-only loop: the planner sees a PNG and emits
                   pixel coordinates, executed as raw coordinate clicks through
                   the Anthropic computer-use adapter (the incumbent loop);
* ``pixels+snap``  the same screenshot-only loop, but coordinate clicks that land
                   on a known accessibility element are executed as ref clicks
                   (the hybrid a pixel client gets from `computeruse.adapters`).

Per (task, mode, round) the harness records completion (a JavaScript success
predicate evaluated over CDP), planner turns, executed actions, misclicks
(clicks whose nearest element with an id is not one of the task's allowed
targets, counted by the fixture's own instrumentation), wasted actions
(actions after which the page state digest did not change), planner tokens,
the cost the provider itself reported when it reports one, and wall time.

Honest by construction: both loops get the same instruction, the same planner,
the same step budget, and a fresh page. The pixel planner never sees the
accessibility tree; the refs planner never sees a screenshot unless it asks for
one. Nothing here can make the accessibility path win; it can only measure.
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import statistics
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from computeruse import agent, safety, server
from computeruse.adapters.anthropic_computer import AnthropicComputerAdapter
from computeruse.agent import DONE_TOOL, AgentResult, Step
from computeruse.providers import PlannerTurn, Provider, ProviderError, Usage
from computeruse.schema import ComputerUseError

TASKS_DIR = Path(__file__).parent / "arena_tasks"

MODES = ("refs", "pixels", "pixels+snap")

#: Tools whose execution is an action on the page (counted as actions and
#: checked for wasted effect). Observations, waits and ``done`` are not.
ACTION_TOOLS = frozenset({
    "click", "type", "key", "scroll", "drag", "set_value", "act",
    "left_click", "double_click",
})

_EXCERPT = 600


# ---------------------------------------------------------------------------
# Task suite
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """One benchmark task: a fixture page plus how to judge a run of it."""

    id: str
    title: str
    page: str
    instruction: str
    success: str  #: JavaScript expression, truthy when the task is complete
    allowed_targets: tuple[str, ...]  #: element ids a correct solution may click
    max_steps: int
    notes: str = ""
    directory: Path = TASKS_DIR  #: where ``page`` (and its assets) live
    #: mode -> reason a run in that mode is NOT a fair comparison (for example a
    #: native ``select`` popup that headless Chrome never paints into a
    #: screenshot). Such runs are still executed and reported, but flagged, and
    #: the aggregate shows completion with and without them.
    not_comparable: tuple[tuple[str, str], ...] = ()

    @property
    def html_path(self) -> Path:
        return self.directory / self.page

    def comparability(self, mode: str) -> str | None:
        """The reason ``mode`` is not comparable on this task, or None when it is."""
        for m, reason in self.not_comparable:
            if m == mode:
                return reason
        return None


def load_tasks(ids: list[str] | None = None, *, directory: Path = TASKS_DIR) -> list[TaskSpec]:
    """Every ``*.json`` manifest under ``directory`` (sorted by id), or the
    subset named in ``ids`` (in that order). Unknown ids raise ``KeyError``."""
    specs: dict[str, TaskSpec] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):  # editor swap files, AppleDouble sidecars
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        spec = TaskSpec(
            id=str(data["id"]), title=str(data.get("title", data["id"])), page=str(data["page"]),
            instruction=str(data["instruction"]), success=str(data["success"]),
            allowed_targets=tuple(str(t) for t in data.get("allowed_targets", [])),
            max_steps=int(data.get("max_steps", 8)), notes=str(data.get("notes", "")),
            directory=directory,
            not_comparable=tuple((str(m), str(r)) for m, r in
                                 (data.get("not_comparable") or {}).items()),
        )
        specs[spec.id] = spec
    if ids is None:
        return list(specs.values())
    return [specs[i] for i in ids]


def validate_task(spec: TaskSpec) -> list[str]:
    """Static checks a manifest must pass; returns the list of problems (empty when valid)."""
    problems: list[str] = []
    if not spec.html_path.exists():
        return [f"{spec.id}: page {spec.page} does not exist"]
    html = spec.html_path.read_text(encoding="utf-8")
    if "_cu.js" not in html and "window.__cu" not in html:
        problems.append(f"{spec.id}: page is not instrumented (no _cu.js / window.__cu)")
    # Same-origin iframe pages are part of the fixture: their ids are targets too,
    # and they must forward their clicks to the parent's instrumentation.
    combined = html
    for src in re.findall(r"""<iframe[^>]+src=["']([^"']+)["']""", html):
        inner = spec.html_path.parent / src
        if not inner.exists():
            problems.append(f"{spec.id}: iframe page {src} does not exist")
            continue
        inner_html = inner.read_text(encoding="utf-8")
        if "cu-click" not in inner_html:
            problems.append(f"{spec.id}: iframe page {src} does not forward clicks (cu-click)")
        combined += inner_html
    ids = set(re.findall(r"""\bid=["']([^"']+)["']""", combined))
    for target in spec.allowed_targets:
        if target not in ids:
            problems.append(f"{spec.id}: allowed target {target!r} is not an id in {spec.page}")
    if not spec.success.strip():
        problems.append(f"{spec.id}: empty success expression")
    if spec.success.count("(") != spec.success.count(")"):
        problems.append(f"{spec.id}: unbalanced parentheses in success expression")
    if spec.max_steps < 1:
        problems.append(f"{spec.id}: max_steps must be positive")
    if not spec.instruction.strip():
        problems.append(f"{spec.id}: empty instruction")
    for mode, reason in spec.not_comparable:
        if mode not in MODES:
            problems.append(f"{spec.id}: not_comparable names unknown mode {mode!r}")
        if not reason.strip():
            problems.append(f"{spec.id}: not_comparable[{mode!r}] needs a reason")
    return problems


# ---------------------------------------------------------------------------
# Fixture server
# ---------------------------------------------------------------------------


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:  # noqa: D401 - stdlib signature
        pass


class FixtureServer:
    """Serves the task pages over plain HTTP on 127.0.0.1 from a daemon thread.

    The pages use relative links (``_cu.js``, ``_style.css``, the iframe page),
    which ``data:`` URLs cannot resolve; a real origin also gives the iframe a
    same-origin parent so its instrumentation can reach the top page.
    """

    def __init__(self, directory: Path = TASKS_DIR) -> None:
        handler = functools.partial(_QuietHandler, directory=str(directory))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def url_for(self, page: str) -> str:
        return f"{self.base_url}/{page}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FixtureServer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Provider wrapper: counts planner turns, sums usage
# ---------------------------------------------------------------------------


class CountingProvider:
    """Wraps a `Provider` to count ``plan()`` calls (planner turns) and sum usage."""

    def __init__(self, inner: Provider) -> None:
        self.inner = inner
        self.name = inner.name
        self.history_edits_ok = inner.history_edits_ok
        self.turns = 0
        self.usage = Usage()

    @property
    def model(self) -> str | None:
        return getattr(self.inner, "reported_model", None) or getattr(self.inner, "model", None)

    def plan(self, messages: list[dict], tools: list[dict], *, system: str) -> PlannerTurn:
        self.turns += 1
        turn = self.inner.plan(messages, tools, system=system)
        self.usage = self.usage + turn.usage
        return turn


# ---------------------------------------------------------------------------
# Pixel loop
# ---------------------------------------------------------------------------

_POINT = {"type": "object",
          "properties": {"x": {"type": "integer", "description": "pixel column in the screenshot"},
                         "y": {"type": "integer", "description": "pixel row in the screenshot"}},
          "required": ["x", "y"]}

PIXEL_TOOLS: list[dict] = [
    {"name": "screenshot", "description": "Take a fresh screenshot of the page.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "left_click", "description": "Left-click at screenshot pixel coordinates.",
     "input_schema": _POINT},
    {"name": "double_click", "description": "Double-click at screenshot pixel coordinates.",
     "input_schema": _POINT},
    {"name": "type", "description": "Type text into the focused field (click it first).",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                      "required": ["text"]}},
    {"name": "key", "description": "Press a key or chord, xdotool style: Return, Tab, Down, ctrl+a.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                      "required": ["text"]}},
    {"name": "scroll", "description": "Scroll the wheel at a point: direction up or down, amount in lines.",
     "input_schema": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "direction": {"type": "string", "enum": ["up", "down"]},
         "amount": {"type": "integer", "description": "wheel lines (default 3)"}},
         "required": ["x", "y", "direction"]}},
    DONE_TOOL,
]


def pixel_system_prompt(width: int, height: int) -> str:
    return (
        "You control a web page by looking at screenshots and acting on pixel coordinates. "
        f"Screenshots are {width}x{height} pixels; (0, 0) is the top-left corner. Every action "
        "result comes back with a fresh screenshot of the page after the action.\n"
        "Click precisely on the centre of the control you mean. To enter text, click the field "
        "first, then call type. Use key for Return, Tab, arrows and shortcuts. Use scroll to "
        "reveal content that is cut off. Look at the newest screenshot before every decision.\n"
        "When the task is complete, or cannot be completed, call done with a one-sentence summary "
        "and success true or false. Never claim success unless the screenshot shows the outcome."
    )


def _image_block(png: bytes) -> dict:
    import base64

    return {"type": "image", "media_type": "image/png",
            "data": base64.b64encode(bytes(png)).decode("ascii")}


def _pixel_action(adapter: AnthropicComputerAdapter, name: str, args: dict):
    """One pixel tool call -> the Anthropic adapter's action input."""
    if name == "screenshot":
        return adapter.handle({"action": "screenshot"})
    if name in ("left_click", "double_click"):
        return adapter.handle({"action": name, "coordinate": [int(args["x"]), int(args["y"])]})
    if name == "type":
        return adapter.handle({"action": "type", "text": str(args.get("text", ""))})
    if name == "key":
        return adapter.handle({"action": "key", "text": str(args.get("text", ""))})
    if name == "scroll":
        return adapter.handle({
            "action": "scroll", "coordinate": [int(args["x"]), int(args["y"])],
            "scroll_direction": str(args.get("direction", "down")),
            "scroll_amount": int(args.get("amount", 3) or 3),
        })
    return None


def run_pixel_task(
    task: str,
    runtime: server.Runtime,
    provider: Provider,
    *,
    app: str,
    snap_to_refs: bool,
    max_steps: int = 25,
    on_step: Callable[[Step], None] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> AgentResult:
    """The screenshot-and-coordinates loop, the incumbent computer-use shape.

    The planner receives the instruction plus a screenshot, may call only the
    `PIXEL_TOOLS`, and every action is executed by the Anthropic computer-use
    adapter as the provider's own action shape would be. ``snap_to_refs``
    selects the pure-coordinate incumbent (False) or the hybrid (True).
    ``confirm`` is the Runtime's confirmation-gate callback for plausibly
    irreversible clicks (the harness auto-approves it, see `Harness`).
    """
    started = time.perf_counter()
    adapter = AnthropicComputerAdapter(runtime, app=app, snap_to_refs=snap_to_refs, confirm=confirm)
    shot = adapter.handle({"action": "screenshot"})
    if not shot.ok or shot.png is None:
        return AgentResult(task=task, app=app, provider=provider.name,
                           model=getattr(provider, "model", None), success=False,
                           summary=f"initial screenshot failed: {shot.text}", stopped="provider_error",
                           wall_time_s=time.perf_counter() - started,
                           audit_dir=str(runtime.audit.dir_path))
    width, height = adapter.screen.width, adapter.screen.height  # type: ignore[union-attr]
    system = pixel_system_prompt(width, height)
    messages: list[dict] = [{"role": "user", "content": [
        {"type": "text", "text": f"Task: {task}"},
        {"type": "text", "text": "Current screenshot of the page:"},
        {**_image_block(shot.png), "observation": True},
    ]}]
    steps: list[Step] = []
    usage = Usage()
    stopped, success, summary = "max_steps", False, ""
    nudges = 0
    for _turn in range(max_steps):
        try:
            turn = provider.plan(messages, PIXEL_TOOLS, system=system)
        except ProviderError as exc:
            stopped, summary = "provider_error", f"planner error: {exc}"
            break
        usage = usage + turn.usage
        messages.append(turn.assistant_message())
        if not turn.tool_calls:
            nudges += 1
            if nudges >= 2 or turn.stop_reason == "refusal":
                stopped, summary = "no_action", turn.text or "the planner produced no tool call"
                break
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "Reply with a tool call, or call done."}]})
            continue
        results: list[dict] = []
        finished = False
        for i, call in enumerate(turn.tool_calls):
            step_usage = turn.usage if i == 0 else Usage()
            if call.name == "done":
                success = bool(call.arguments.get("success", False))
                summary = str(call.arguments.get("summary", "")).strip()
                step = Step(index=len(steps) + 1, tool="done", params=dict(call.arguments), ok=True,
                            result=summary, error_code=None, duration_ms=0.0, usage=step_usage)
                steps.append(step)
                if on_step is not None:
                    on_step(step)
                finished, stopped = True, "done"
                break
            t0 = time.perf_counter()
            result = _pixel_action(adapter, call.name, dict(call.arguments))
            if result is None:
                text, ok, code, png = f"unknown_tool: {call.name}", False, "unknown_tool", None
            else:
                text, ok, code, png = result.text, result.ok, result.error, result.png
                if result.snapped_ref:
                    text += f" [snapped to {result.snapped_ref}]"
            blocks: list[dict] = [{"type": "text", "text": text or "OK"}]
            if call.name != "screenshot":  # every action comes back with the new frame
                after = adapter.handle({"action": "screenshot"})
                if after.ok and after.png is not None:
                    png = after.png
            if png is not None:
                blocks.append(_image_block(png))
            duration_ms = (time.perf_counter() - t0) * 1000.0
            step = Step(index=len(steps) + 1, tool=call.name, params=dict(call.arguments), ok=ok,
                        result=(text or "")[:_EXCERPT], error_code=code, duration_ms=duration_ms,
                        usage=step_usage)
            steps.append(step)
            if on_step is not None:
                on_step(step)
            results.append({"type": "tool_result", "tool_use_id": call.id, "name": call.name,
                            "content": blocks, "is_error": not ok, "observation": True})
        if finished:
            break
        messages.append({"role": "user", "content": results})
        if provider.history_edits_ok:
            agent._bound_history(messages)
    if stopped == "max_steps" and not summary:
        summary = f"stopped after {max_steps} planner turns without a done call"
    return AgentResult(task=task, app=app, provider=provider.name,
                       model=getattr(provider, "model", None), success=success, summary=summary,
                       stopped=stopped, steps=steps, usage=usage,
                       wall_time_s=time.perf_counter() - started,
                       audit_dir=str(runtime.audit.dir_path))


# ---------------------------------------------------------------------------
# Records, scoring, aggregation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunRecord:
    task_id: str
    mode: str
    round: int
    success: bool
    stopped: str
    turns: int
    actions: int
    misclicks: int
    wasted: int
    input_tokens: int
    output_tokens: int
    cost_reported_usd: float
    wall_s: float
    summary: str
    model: str | None = None
    error: str | None = None
    #: False when the task manifest marks this mode as not a fair comparison;
    #: ``note`` carries the reason. The run still happened and is reported.
    comparable: bool = True
    note: str = ""
    steps: list[dict] = field(default_factory=list)
    clicks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RunRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def count_misclicks(clicks: list[dict], allowed: tuple[str, ...] | list[str]) -> int:
    """Clicks whose recorded target is not an allowed id. A click that hit no
    id-bearing element is recorded by tag name and counts."""
    allowed_set = set(allowed)
    return sum(1 for c in clicks if str(c.get("target", "")) not in allowed_set)


def estimate_cost(input_tokens: int, output_tokens: int,
                  price_in: float | None, price_out: float | None) -> float | None:
    """USD from per-million-token prices; None when no prices were given."""
    if price_in is None or price_out is None:
        return None
    return input_tokens * price_in / 1e6 + output_tokens * price_out / 1e6


def aggregate(records: list[RunRecord], *, price_in: float | None = None,
              price_out: float | None = None) -> dict[str, dict]:
    """Per-mode totals and means over ``records``."""
    out: dict[str, dict] = {}
    for mode in sorted({r.mode for r in records}, key=lambda m: MODES.index(m) if m in MODES else 99):
        rows = [r for r in records if r.mode == mode]
        completed = sum(1 for r in rows if r.success)
        tin = sum(r.input_tokens for r in rows)
        tout = sum(r.output_tokens for r in rows)
        reported = sum(r.cost_reported_usd for r in rows)
        # "comparable" excludes every task that ANY mode flags as not comparable,
        # so the with/without rates are computed over the same task set per mode.
        flagged_tasks = {r.task_id for r in records if not r.comparable}
        comp = [r for r in rows if r.task_id not in flagged_tasks]
        comp_done = sum(1 for r in comp if r.success)
        out[mode] = {
            "runs": len(rows),
            "completed": completed,
            "completion_rate": completed / len(rows) if rows else 0.0,
            "comparable_runs": len(comp),
            "comparable_completed": comp_done,
            "comparable_rate": comp_done / len(comp) if comp else 0.0,
            "excluded_tasks": sorted(flagged_tasks),
            "mean_turns": statistics.fmean(r.turns for r in rows) if rows else 0.0,
            "mean_turns_completed": (statistics.fmean(r.turns for r in rows if r.success)
                                     if completed else 0.0),
            "actions": sum(r.actions for r in rows),
            "misclicks": sum(r.misclicks for r in rows),
            "wasted": sum(r.wasted for r in rows),
            "input_tokens": tin,
            "output_tokens": tout,
            "cost_reported_usd": reported if reported else None,
            "cost_estimated_usd": estimate_cost(tin, tout, price_in, price_out),
            "wall_s": sum(r.wall_s for r in rows),
        }
    return out


@dataclass(slots=True)
class H2HReport:
    records: list[RunRecord] = field(default_factory=list)
    meta: dict = field(default_factory=dict)  #: planner, model, browser, date, prices...

    def aggregate(self) -> dict[str, dict]:
        return aggregate(self.records, price_in=self.meta.get("price_in"),
                         price_out=self.meta.get("price_out"))

    def to_dict(self) -> dict:
        return {"meta": self.meta, "aggregate": self.aggregate(),
                "records": [r.to_dict() for r in self.records]}

    @classmethod
    def from_dict(cls, data: dict) -> "H2HReport":
        """Rebuild a report from a saved ``h2h.json`` (re-rendering, merging runs)."""
        return cls(records=[RunRecord.from_dict(r) for r in data.get("records", [])],
                   meta=dict(data.get("meta") or {}))


def _fmt_cost(value: float | None) -> str:
    return "" if value is None else f"${value:.3f}"


def format_report(report: H2HReport) -> str:
    """Markdown: run metadata, one aggregate row per mode, one row per task and mode."""
    lines: list[str] = []
    meta = report.meta
    lines.append("## cu-arena head-to-head")
    lines.append("")
    for key in ("date", "planner", "model", "browser", "tasks", "rounds", "max_steps", "machine"):
        if meta.get(key) not in (None, ""):
            lines.append(f"- {key}: {meta[key]}")
    if meta.get("price_in") is not None:
        lines.append(f"- prices: ${meta['price_in']}/M input, ${meta['price_out']}/M output "
                     "(estimated cost column)")
    lines.append("")
    agg = report.aggregate()
    if not agg:
        lines.append("no runs")
        return "\n".join(lines)
    has_reported = any(v["cost_reported_usd"] is not None for v in agg.values())
    has_est = any(v["cost_estimated_usd"] is not None for v in agg.values())
    excluded = next(iter(agg.values()))["excluded_tasks"]
    head = ["mode", "completed", "rate"]
    if excluded:
        head.append("rate (comparable tasks)")
    head += ["mean turns", "actions", "misclicks", "wasted", "tokens in", "tokens out"]
    if has_reported:
        head.append("cost (reported)")
    if has_est:
        head.append("cost (est.)")
    head.append("wall")
    lines.append("| " + " | ".join(head) + " |")
    lines.append("|" + "---|" * len(head))
    for mode, v in agg.items():
        row = [mode, f"{v['completed']}/{v['runs']}", f"{v['completion_rate'] * 100:.0f}%"]
        if excluded:
            row.append(f"{v['comparable_completed']}/{v['comparable_runs']} "
                       f"({v['comparable_rate'] * 100:.0f}%)")
        row += [f"{v['mean_turns']:.1f}", str(v["actions"]), str(v["misclicks"]), str(v["wasted"]),
                f"{v['input_tokens']:,}", f"{v['output_tokens']:,}"]
        if has_reported:
            row.append(_fmt_cost(v["cost_reported_usd"]))
        if has_est:
            row.append(_fmt_cost(v["cost_estimated_usd"]))
        row.append(f"{v['wall_s']:.0f}s")
        lines.append("| " + " | ".join(row) + " |")
    if excluded:
        lines.append("")
        lines.append(f"The \"comparable tasks\" rate leaves out {', '.join(excluded)} for every mode, "
                     "because at least one mode is flagged as not comparable on that task "
                     "(see the notes column).")
    lines.append("")
    lines.append("| task | mode | round | done | stopped | turns | actions | misclicks | wasted | tokens in/out | wall | note |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report.records:
        note = r.note if r.comparable else f"not comparable: {r.note}"
        lines.append(
            f"| {r.task_id} | {r.mode} | {r.round} | {'yes' if r.success else 'no'} | {r.stopped} | "
            f"{r.turns} | {r.actions} | {r.misclicks} | {r.wasted} | "
            f"{r.input_tokens:,}/{r.output_tokens:,} | {r.wall_s:.0f}s | {note} |")
    failures = [r for r in report.records if not r.success]
    if failures:
        lines.append("")
        lines.append("Failed runs:")
        for r in failures:
            why = r.error or r.summary or r.stopped
            flag = " [not comparable]" if not r.comparable else ""
            lines.append(f"- {r.task_id} / {r.mode} (round {r.round}){flag}: {why[:300]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


def _evaluate(sess, expression: str):
    result = sess.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    return (result or {}).get("result", {}).get("value")


def browser_version(endpoint: str) -> str:
    """The ``Browser`` field of ``{endpoint}/json/version`` (empty on failure)."""
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/json/version", timeout=5) as resp:
            return str(json.load(resp).get("Browser", ""))
    except Exception:
        return ""


def auto_confirm(prompt: str) -> bool:
    """The benchmark's confirmation-gate answer: always yes.

    The Runtime asks before plausibly irreversible clicks (a button titled
    "Delete", for example). A ref click carries the element title, so the
    classifier fires on it; a raw coordinate click carries no title, so it does
    not. Without an approving callback the refs loop would be blocked on such a
    task while the pixel loop sails through, which is not a comparison of
    observation strategies. The fixtures are throwaway pages, so approving is
    safe here; a real agent should route the prompt to a human.
    """
    return True


class Harness:
    """Runs tasks against one browser tab and scores each run.

    Args:
        driver: A connected `BrowserDriver` (the tab it binds is the arena).
        provider_factory: Builds a fresh planner for a given mode. The pixel
            modes need a planner that can view images.
        workdir: Where the permission store and audit log for the runs live
            (a temporary directory by default), so a benchmark never touches
            the user's own ``~/.computeruse``.
        on_event: Progress callback ``(kind, payload)``.
        confirm: Confirmation-gate callback handed to both loops; defaults to
            `auto_confirm` (see its docstring for why).
    """

    def __init__(self, driver, provider_factory: Callable[[str], Provider], *,
                 workdir: Path | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 confirm: Callable[[str], bool] | None = auto_confirm) -> None:
        self.driver = driver
        self.provider_factory = provider_factory
        self.workdir = workdir or Path(tempfile.mkdtemp(prefix="cu-h2h-"))
        self.on_event = on_event or (lambda kind, payload: None)
        self.confirm = confirm
        self.store = safety.PermissionStore(self.workdir / "permissions.json")
        self.audit = safety.AuditLog(self.workdir / "audit")

    def _runtime(self) -> tuple[server.Runtime, str]:
        runtime = server.Runtime(store=self.store, audit=self.audit, driver=self.driver)
        tab = self.driver.frontmost_app()[0]
        self.store.set_tier(tab, safety.Tier.FULL)
        return runtime, tab

    def run_one(self, spec: TaskSpec, mode: str, url: str, *, round_no: int = 1,
                max_steps: int | None = None) -> RunRecord:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
        budget = max_steps or spec.max_steps
        self.driver.navigate(url)
        sess = self.driver._connect()
        runtime, tab = self._runtime()
        provider = CountingProvider(self.provider_factory(mode))
        state = {"prev": _evaluate(sess, "window.__cuState ? window.__cuState() : ''"),
                 "wasted": 0, "actions": 0}

        def on_step(step: Step) -> None:
            if step.tool in ACTION_TOOLS:
                state["actions"] += 1
                now = _evaluate(sess, "window.__cuState ? window.__cuState() : ''")
                if now == state["prev"]:
                    state["wasted"] += 1
                state["prev"] = now
            self.on_event("step", {"task": spec.id, "mode": mode, "step": step})

        started = time.perf_counter()
        error: str | None = None
        try:
            if mode == "refs":
                result = agent.run_task(spec.instruction, runtime, provider, app=tab,
                                        max_steps=budget, verify=True, on_step=on_step,
                                        confirm=self.confirm)
            else:
                result = run_pixel_task(spec.instruction, runtime, provider, app=tab,
                                        snap_to_refs=(mode == "pixels+snap"),
                                        max_steps=budget, on_step=on_step, confirm=self.confirm)
        except (ComputerUseError, server.ActionRefused, ProviderError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            result = AgentResult(task=spec.instruction, app=tab, provider=provider.name,
                                 model=provider.model, success=False, summary=error,
                                 stopped="harness_error", wall_time_s=time.perf_counter() - started)
        try:
            success = bool(_evaluate(sess, f"!!({spec.success})"))
        except ComputerUseError as exc:
            success, error = False, f"success expression failed: {exc}"
        clicks_raw = _evaluate(sess, "JSON.stringify(window.__cu ? window.__cu.clicks : [])") or "[]"
        try:
            clicks = json.loads(clicks_raw)
        except json.JSONDecodeError:
            clicks = []
        reason = spec.comparability(mode)
        record = RunRecord(
            task_id=spec.id, mode=mode, round=round_no, success=success,
            stopped=result.stopped, turns=provider.turns, actions=state["actions"],
            misclicks=count_misclicks(clicks, spec.allowed_targets), wasted=state["wasted"],
            input_tokens=provider.usage.input_tokens, output_tokens=provider.usage.output_tokens,
            cost_reported_usd=provider.usage.cost_usd, wall_s=result.wall_time_s,
            summary=result.summary, model=provider.model, error=error,
            comparable=reason is None, note=reason or "",
            steps=[s.to_dict() for s in result.steps], clicks=clicks,
        )
        self.on_event("run", {"record": record})
        return record

    def run(self, tasks: list[TaskSpec], modes: list[str] = list(MODES), *, rounds: int = 1,
            max_steps: int | None = None, server_dir: Path = TASKS_DIR) -> H2HReport:
        report = H2HReport()
        with FixtureServer(server_dir) as fixtures:
            for round_no in range(1, max(1, rounds) + 1):
                for spec in tasks:
                    for mode in modes:
                        self.on_event("start", {"task": spec.id, "mode": mode, "round": round_no})
                        report.records.append(
                            self.run_one(spec, mode, fixtures.url_for(spec.page),
                                         round_no=round_no, max_steps=max_steps))
        return report


def run_h2h(endpoint: str, provider_factory: Callable[[str], Provider], *,
            tasks: list[TaskSpec] | None = None, modes: list[str] = list(MODES), rounds: int = 1,
            max_steps: int | None = None, price_in: float | None = None,
            price_out: float | None = None, workdir: Path | None = None,
            on_event: Callable[[str, dict], None] | None = None, meta: dict | None = None) -> H2HReport:
    """Run the suite on the browser at ``endpoint`` and return the scored report."""
    import datetime as _dt

    from computeruse.drivers.browser import BrowserDriver

    tasks = tasks if tasks is not None else load_tasks()
    driver = BrowserDriver(endpoint=endpoint)
    harness = Harness(driver, provider_factory, workdir=workdir, on_event=on_event)
    try:
        report = harness.run(tasks, modes, rounds=rounds, max_steps=max_steps)
    finally:
        driver.close()
    models = sorted({r.model for r in report.records if r.model})
    report.meta = {
        "date": _dt.date.today().isoformat(),
        "browser": browser_version(endpoint),
        "tasks": ", ".join(t.id for t in tasks),
        "modes": list(modes),
        "rounds": rounds,
        "max_steps": max_steps or "per task",
        "model": ", ".join(models),
        "price_in": price_in,
        "price_out": price_out,
        "workdir": str(harness.workdir),
        **(meta or {}),
    }
    return report


__all__ = [
    "ACTION_TOOLS", "MODES", "PIXEL_TOOLS", "TASKS_DIR", "CountingProvider", "FixtureServer",
    "H2HReport", "Harness", "RunRecord", "TaskSpec", "aggregate", "auto_confirm",
    "browser_version", "count_misclicks", "estimate_cost", "format_report", "load_tasks",
    "pixel_system_prompt", "run_h2h", "run_pixel_task", "validate_task",
]
