"""``a11y-agent`` command line.

``a11y-agent run`` prints one JSON object on stdout when ``--json`` is set.
Exit codes: 0 success, 1 failed, 2 needs_human, 3 error or cancel.
``--auto-deny`` is the default. ``--approve`` is the only mode that reads a
prompt from the terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from a11y_computer_use.agent.actions import Action
from a11y_computer_use.agent.result import RunResult


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(3, f"{self.prog}: error: {message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the agent CLI. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.approve and args.auto_deny:
        return _emit(args, _error_body("error: pass only one of --approve and --auto-deny"), 3)
    try:
        agent = build_agent(args)
        result = agent.run(args.goal)
    except Exception as exc:  # noqa: BLE001 - the process must exit 3, not traceback
        return _emit(args, _error_body(f"error: {type(exc).__name__}: {exc}"), 3)
    return _emit(args, result.to_dict(), exit_code(result))


def build_agent(args: argparse.Namespace):
    """Construct the agent a parsed ``run`` command describes."""
    from a11y_computer_use.agent.core import Agent

    approve = _stdin_approve if args.approve else None
    auto_deny = not args.approve
    return Agent(
        model=args.model,
        display=args.display,
        max_steps=args.max_steps,
        max_time_s=args.max_time_s,
        approve=approve,
        auto_deny=auto_deny,
        allow_exec=bool(getattr(args, "allow_exec", False)),
        trace_dir=args.trace_dir,
    )


def exit_code(result: RunResult) -> int:
    """Map a result to the process exit code."""
    if result.reason == "cancelled" or result.reason.startswith("error:"):
        return 3
    if result.status == "success":
        return 0
    if result.status == "needs_human":
        return 2
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="a11y-agent", description="Run one computer-use goal.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    run = sub.add_parser("run", help="run a single goal and exit")
    run.add_argument("goal", help="what to accomplish")
    run.add_argument("--model", required=True, help="model spec, for example scripted:turns.json or openai:gpt-4.1")
    run.add_argument("--display", default=None, help="X display, for example :1 (also sets $DISPLAY)")
    run.add_argument("--max-steps", type=int, default=50, dest="max_steps")
    run.add_argument("--max-time", type=float, default=900.0, dest="max_time_s", help="wall-clock budget in seconds")
    run.add_argument("--trace-dir", default=None, dest="trace_dir", help="directory for trajectory.jsonl and screenshots")
    run.add_argument("--json", action="store_true", help="print the result as one JSON object on stdout")
    run.add_argument("--approve", action="store_true", help="prompt before quit, close, submit, and exec")
    run.add_argument("--auto-deny", action="store_true", help="skip quit, close, submit, and exec (this is the default)")
    run.add_argument(
        "--allow-exec",
        action="store_true",
        dest="allow_exec",
        help="expose shell and python tools; each call is still approved or auto-denied",
    )
    return parser


def _stdin_approve(action: Action) -> bool:
    rendered = action.name
    if action.args:
        rendered += " " + json.dumps(action.args, default=str)[:180]
    answer = input(f"Approve {rendered}? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _error_body(reason: str) -> dict:
    return {
        "status": "failed",
        "answer": "",
        "steps": 0,
        "elapsed_s": 0.0,
        "reason": reason,
        "conditions": [],
        "needs_human": None,
        "trace_dir": "",
        "step_log": [],
    }


def _emit(args: argparse.Namespace, payload: dict, code: int) -> int:
    if args.json:
        sys.stdout.write(json.dumps(payload) + "\n")
    else:
        answer = payload.get("answer") or ""
        summary = f"{payload.get('status')} steps={payload.get('steps')} reason={payload.get('reason')}"
        if answer:
            summary += f" answer={answer}"
        sys.stdout.write(summary + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
