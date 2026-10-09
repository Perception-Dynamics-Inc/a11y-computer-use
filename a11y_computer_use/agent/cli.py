"""``a11y-agent`` command line.

``a11y-agent run`` prints one JSON object on stdout when ``--json`` is set.
Exit codes: 0 success, 1 failed, 2 needs_human, 3 error or cancel.
SIGINT and SIGTERM cancel after the current step (status ``cancelled``,
exit 3). On Windows, Ctrl+C (SIGINT) and Ctrl+Break (SIGBREAK) do; SIGTERM
there is TerminateProcess and is not a cooperative cancel. A second signal
exits 3 immediately and does not print a traceback.
``--approve-policy deny`` is the default for risky actions. ``--approve``
prompts on the terminal (stderr or the tty, never stdout).

``a11y-agent serve`` is the HTTP API. ``a11y-agent mcp`` is the goal-level
MCP server (``run_goal``, ``get_run``, ``cancel_run``, ``approve``).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Sequence

from a11y_computer_use.agent.actions import Action, risk_category
from a11y_computer_use.agent.result import RunResult
from a11y_computer_use.agent.trace import summarize_args
from a11y_computer_use.untrusted import render_untrusted


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
        result = _run_until_cancelled(agent, args.goal)
    except KeyboardInterrupt:
        body = _error_body("cancelled")
        body["status"] = "cancelled"
        return _emit(args, body, 3)
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


def _cancel_signals() -> list[int]:
    """Signals that cancel ``a11y-agent run`` on this operating system.

    POSIX delivers SIGINT (Ctrl+C) and SIGTERM to a Python handler. Windows
    delivers SIGINT (Ctrl+C) and SIGBREAK (Ctrl+Break). ``os.kill(SIGTERM)``
    on Windows calls TerminateProcess and never enters the handler, so it is
    not installed there.
    """
    names = ("SIGINT", "SIGBREAK") if sys.platform == "win32" else ("SIGINT", "SIGTERM")
    found: list[int] = []
    for name in names:
        value = getattr(signal, name, None)
        if isinstance(value, int):
            found.append(value)
    return found


def _on_cancel_signal(agent: object, hits: list[int], signum: int, frame: object) -> None:
    """First signal cancels after the current step. The second exits 3.

    The handler is re-armed before it does anything else. A second SIGINT
    that arrives while the first is still tripped can leave the process on
    the default handler; that path is an uncaught KeyboardInterrupt and the
    process dies with status ``-SIGINT`` (-2) and a traceback. Re-arming
    keeps this function installed. The second hit uses ``os._exit`` so
    finalization cannot turn the exit into that signal death.
    """
    del frame
    try:
        signal.signal(signum, lambda sig, frm: _on_cancel_signal(agent, hits, sig, frm))
    except (OSError, ValueError):
        pass
    hits[0] += 1
    flag = getattr(agent, "_signal_cancel", None)
    if flag is not None and hasattr(flag, "set"):
        try:
            flag.set()
        except Exception:  # noqa: BLE001 - a signal handler must not raise
            pass
    cancel = getattr(agent, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:  # noqa: BLE001 - a signal handler must not raise
            pass
    if hits[0] >= 2:
        os._exit(3)


def _run_until_cancelled(agent: object, goal: str):
    """Run ``goal``. The platform's cancel signals stop it after this step.

    Handlers are installed before ``run``, so a signal during startup is
    recorded on ``agent._signal_cancel`` and still cancels after ``_loop``
    clears the ordinary cancel event. The first signal returns into the
    step. The trace write for that step closes, and ``run`` returns a
    cancelled result. A second signal exits 3 with no traceback.
    """
    hits = [0]
    saved: list[tuple[int, object]] = []
    flag = getattr(agent, "_signal_cancel", None)
    if flag is None:
        try:
            agent._signal_cancel = threading.Event()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a test double may refuse new attributes
            pass

    def handler(signum: int, frame: object) -> None:
        _on_cancel_signal(agent, hits, signum, frame)

    for sig in _cancel_signals():
        try:
            saved.append((sig, signal.getsignal(sig)))
            signal.signal(sig, handler)
            if hasattr(signal, "siginterrupt"):
                signal.siginterrupt(sig, True)
        except (OSError, ValueError):
            continue
    try:
        return agent.run(goal)  # type: ignore[attr-defined]
    finally:
        for sig, previous in saved:
            try:
                signal.signal(sig, previous)  # type: ignore[arg-type]
            except (OSError, ValueError):
                pass


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
    """The ``--approve`` question a person reads.

    Role, name, window, page URL when there is one, and the reason
    (``payment``, ``send``, ``delete``, ``quit``, or ``exec``). Role and
    reason are the loop's tokens. The name, window, URL, and argument summary
    are quoted with :func:`render_untrusted`. The HTTP and MCP approval
    payloads still use :func:`approval_target`, which keeps ``fence`` tags for
    a model.
    """
    if action.name == "confirm":
        raw = action.args.get("prompt")
        if isinstance(raw, str) and raw.strip():
            text = raw.rstrip()
            if "[y/N]" not in text:
                text += " [y/N]"
            return text + " "
    parts = [action.name]
    if action.role:
        parts.append(f"role={action.role}")
    for key, value, limit in (
        ("name", action.target_name, 80),
        ("window", action.window, 80),
        ("url", action.url, 160),
    ):
        if isinstance(value, str) and value:
            shown = render_untrusted(value, limit=limit)
            if shown:
                parts.append(f"{key}={shown}")
    kind = action.reason_kind or risk_category(action.reason)
    if kind:
        parts.append(f"reason={kind}")
    summary = action.summary if action.summary is not None else summarize_args(action.args)
    if summary and summary != "{}":
        shown = render_untrusted(summary, limit=160)
        if shown:
            parts.append("args=" + shown)
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
