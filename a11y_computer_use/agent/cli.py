"""``a11y-agent`` command line.

``a11y-agent run`` prints one JSON object on stdout when ``--json`` is set.
Exit codes: 0 success, 1 failed, 2 needs_human, 3 error or cancel.
``--approve-policy deny`` is the default for risky actions. ``--approve``
prompts on the terminal (stderr or the tty, never stdout).

``a11y-agent serve`` is the HTTP API. ``a11y-agent mcp`` is the goal-level
MCP server (``run_goal``, ``get_run``, ``cancel_run``, ``approve``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from a11y_computer_use.agent.actions import Action, approval_target
from a11y_computer_use.agent.result import RunResult
from a11y_computer_use.agent.trace import summarize_args
from a11y_computer_use.untrusted import fence_untrusted


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(3, f"{self.prog}: error: {message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the agent CLI. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "mcp":
        return _mcp(args)
    policy = getattr(args, "approve_policy", "deny")
    if args.approve and args.auto_deny:
        return _emit(args, _error_body("error: pass only one of --approve and --auto-deny"), 3)
    if args.approve and policy != "deny":
        return _emit(
            args,
            _error_body("error: pass only one of --approve and --approve-policy"),
            3,
        )
    if args.auto_deny and policy != "deny":
        return _emit(
            args,
            _error_body("error: pass only one of --auto-deny and --approve-policy"),
            3,
        )
    try:
        agent = build_agent(args)
        result = agent.run(args.goal)
    except Exception as exc:  # noqa: BLE001 - the process must exit 3, not traceback
        return _emit(args, _error_body(f"error: {type(exc).__name__}: {exc}"), 3)
    return _emit(args, result.to_dict(), exit_code(result))


def build_agent(args: argparse.Namespace):
    """Construct the agent a parsed ``run`` command describes."""
    from a11y_computer_use.agent.core import Agent

    policy = getattr(args, "approve_policy", "deny")
    if policy == "allow-all":
        approve = None
        auto_deny = False
    else:
        approve = _stdin_approve if args.approve else None
        auto_deny = not args.approve
    return Agent(
        model=args.model,
        display=args.display,
        max_steps=args.max_steps,
        max_time_s=args.max_time_s,
        model_timeout_s=args.model_timeout_s,
        approve=approve,
        auto_deny=auto_deny,
        approve_policy=policy,
        allow_exec=bool(getattr(args, "allow_exec", False)),
        allow_payments=bool(getattr(args, "allow_payments", False)),
        trace_dir=args.trace_dir,
        allowed_domains=args.allowed_domains,
        blocked_domains=args.blocked_domains,
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
    run.add_argument(
        "--model-timeout",
        type=float,
        default=120.0,
        dest="model_timeout_s",
        help="seconds for one model call (default 120); this is not the run budget",
    )
    run.add_argument("--trace-dir", default=None, dest="trace_dir", help="directory for trajectory.jsonl and screenshots")
    run.add_argument("--json", action="store_true", help="print the result as one JSON object on stdout")
    run.add_argument("--approve", action="store_true", help="prompt before quit, close, pay, send, delete, and exec")
    run.add_argument(
        "--auto-deny",
        action="store_true",
        help="skip quit, close, pay, send, delete, and exec (this is the default)",
    )
    run.add_argument(
        "--approve-policy",
        choices=("deny", "allow-safe", "allow-all"),
        default="deny",
        dest="approve_policy",
        help=(
            "unattended approval for risky actions: deny (default), "
            "allow-safe (quit and window close only), or allow-all"
        ),
    )
    run.add_argument(
        "--allow-exec",
        action="store_true",
        dest="allow_exec",
        help="expose shell and python tools; each call is still approved or auto-denied",
    )
    run.add_argument(
        "--allow-payments",
        action="store_true",
        dest="allow_payments",
        help=(
            "let a payment click go through approval instead of stopping as "
            "needs_human. allow-all does not include payments"
        ),
    )
    run.add_argument(
        "--allowed-domains",
        default=None,
        dest="allowed_domains",
        help="comma-separated hosts or origins the browser may be acted on or navigated to",
    )
    run.add_argument(
        "--blocked-domains",
        default=None,
        dest="blocked_domains",
        help="comma-separated hosts or origins that fail with domain_blocked (blocked wins)",
    )
    serve = sub.add_parser("serve", help="HTTP API for one agent process")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (default 8765)")
    serve.add_argument("--token", default=None, help="bearer token; required when --host is not loopback")
    serve.add_argument(
        "--approval-timeout",
        type=float,
        default=60.0,
        dest="approval_timeout",
        help="seconds to wait for an approval before denying it (default 60)",
    )
    mcp = sub.add_parser("mcp", help="goal-level MCP server on stdio")
    mcp.add_argument(
        "--approval-timeout",
        type=float,
        default=60.0,
        dest="approval_timeout",
        help="seconds to wait for an approval before denying it (default 60)",
    )
    return parser


def _serve(args: argparse.Namespace) -> int:
    from a11y_computer_use.agent.httpapi import bind_server

    try:
        httpd = bind_server(
            host=args.host,
            port=args.port,
            token=args.token,
            approval_timeout_s=args.approval_timeout,
        )
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"a11y-agent: error: {exc}\n")
        return 3
    host, port = httpd.server_address[:2]
    sys.stderr.write(f"listening on http://{host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        httpd.server_close()
    return 0


def _mcp(args: argparse.Namespace) -> int:
    from a11y_computer_use.agent.mcp_server import build_agent_mcp
    from a11y_computer_use.agent.service import RunStore

    store = RunStore(approval_timeout_s=args.approval_timeout)
    try:
        build_agent_mcp(store).run()
    except KeyboardInterrupt:
        return 0
    return 0


def render_approval_prompt(action: Action) -> str:
    """The ``--approve`` question.

    The same labelled target as the server approval event and the MCP approve
    flow: role, name, window, page URL when there is one, and the reason
    (``payment``, ``send``, ``delete``, ``quit``, or ``exec``). Role and
    reason are the loop's tokens. The name, window, URL, and argument summary
    are trimmed and wrapped with :func:`fence_untrusted`.
    """
    if action.name == "confirm":
        raw = action.args.get("prompt")
        if isinstance(raw, str) and raw.strip():
            text = raw.rstrip()
            if "[y/N]" not in text:
                text += " [y/N]"
            return text + " "
    target = approval_target(action)
    parts = [action.name]
    for key in ("role", "name", "window", "url", "reason"):
        if target.get(key):
            parts.append(f"{key}={target[key]}")
    summary = action.summary if action.summary is not None else summarize_args(action.args)
    if summary and summary != "{}":
        parts.append("args=" + fence_untrusted(summary, limit=160))
    return "Approve " + " ".join(parts) + "? [y/N] "


def _stdin_approve(action: Action) -> bool:
    _write_prompt(render_approval_prompt(action))
    answer = sys.stdin.readline()
    return answer.strip().lower() in {"y", "yes"}


def _write_prompt(prompt: str) -> None:
    """Write ``prompt`` to stderr or the tty. Never to stdout."""
    stream = _prompt_stream()
    try:
        stream.write(prompt)
        stream.flush()
    finally:
        if stream is not sys.stderr and stream is not sys.stdout:
            stream.close()


def _prompt_stream():
    """Where an approval prompt is written. Never stdout.

    A terminal stderr gets the prompt. Otherwise the controlling tty, so a
    redirected stdout (``--json``) stays a single JSON object. When neither
    is available the prompt is still written to stderr.
    """
    err = sys.stderr
    if err is not None and getattr(err, "isatty", lambda: False)():
        return err
    try:
        return open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        return err


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
