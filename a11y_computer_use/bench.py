"""cu-meter — aggregate the JSONL audit log into a benchmark report.

Every gated action records metrics (``duration_ms``, and for text results
``result_chars`` + ``tokens_est``; see ``server.Runtime._run_gated``). This turns
the audit the SDK already writes into the numbers behind the moat — per-action
latency (p50/p95), tokens/task, and full-vs-diff snapshot savings — with no
external harness. Pure + platform-free.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from statistics import median


def _percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def load_entries(audit_dir: Path | str) -> list[dict]:
    """All audit rows across the dir's ``*.jsonl`` files (bad lines skipped)."""
    entries: list[dict] = []
    for path in sorted(Path(audit_dir).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def report(audit_dir: Path | str) -> dict:
    """Aggregate audit metrics into a report dict.

    ``observe_tokens_est`` sums the token footprint of observation results
    (snapshot/find/diff/screenshot) — the number a screenshot loop can't shrink
    and diff-mode does. ``by_action`` gives per-verb count + latency p50/p95 +
    token total.
    """
    entries = load_entries(audit_dir)
    per_action: dict[str, dict] = {}
    total_tokens = 0
    observe_tokens = 0
    planner_in = 0
    planner_out = 0
    agent_runs = 0
    durations_all: list[float] = []
    for e in entries:
        m = e.get("metrics") or {}
        act = str(e.get("action", "?"))
        a = per_action.setdefault(act, {"count": 0, "durations": [], "tokens": 0})
        a["count"] += 1
        dur = m.get("duration_ms")
        if isinstance(dur, (int, float)):
            a["durations"].append(float(dur))
            durations_all.append(float(dur))
        toks = int(m.get("tokens_est", 0) or 0)
        a["tokens"] += toks
        total_tokens += toks
        if act == "observeop":
            observe_tokens += toks
        # The agent loop (a11y_computer_use agent) records the planner's own token
        # usage per step; summed here so one report covers observation cost
        # (tokens_est) and planner cost side by side.
        if act == "agent_step":
            planner_in += int(m.get("planner_input_tokens", 0) or 0)
            planner_out += int(m.get("planner_output_tokens", 0) or 0)
        elif act == "agent_run":
            agent_runs += 1
    by_action = {
        act: {
            "count": a["count"],
            "p50_ms": round(median(a["durations"]), 1) if a["durations"] else None,
            "p95_ms": round(_percentile(a["durations"], 0.95), 1) if a["durations"] else None,
            "tokens_est": a["tokens"],
        }
        for act, a in sorted(per_action.items())
    }
    return {
        "total_actions": len(entries),
        "total_tokens_est": total_tokens,
        "observe_tokens_est": observe_tokens,
        "p50_ms": round(median(durations_all), 1) if durations_all else None,
        "p95_ms": round(_percentile(durations_all, 0.95), 1) if durations_all else None,
        "by_action": by_action,
        "agent_runs": agent_runs,
        "planner_input_tokens": planner_in,
        "planner_output_tokens": planner_out,
    }


def format_report(rep: dict) -> str:
    """A compact human-readable rendering of `report`."""
    lines = [
        f"actions={rep['total_actions']}  tokens_est={rep['total_tokens_est']}  "
        f"observe_tokens={rep['observe_tokens_est']}  latency p50={rep['p50_ms']}ms "
        f"p95={rep['p95_ms']}ms",
    ]
    if rep.get("agent_runs"):
        lines.append(
            f"agent runs={rep['agent_runs']}  planner tokens in={rep['planner_input_tokens']} "
            f"out={rep['planner_output_tokens']}"
        )
    for act, a in rep["by_action"].items():
        lines.append(f"  {act:<14} n={a['count']:<4} p50={a['p50_ms']}ms p95={a['p95_ms']}ms "
                     f"tokens={a['tokens_est']}")
    return "\n".join(lines)
