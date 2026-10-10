"""``a11y-agent`` command line.

``a11y-agent run`` prints one JSON object on stdout when ``--json`` is set.
Exit codes: 0 success, 1 failed, 2 needs_human, 3 error or cancel.
SIGINT and SIGTERM cancel after the current step (status ``cancelled``,
exit 3). On Windows, Ctrl+C (SIGINT) and Ctrl+Break (SIGBREAK) do; SIGTERM
there is TerminateProcess and is not a cooperative cancel. Handlers are
installed before the agent is built and stay until the process exits. A
second signal calls ``os._exit(3)`` and does not print a traceback. On
Windows a console control handler does that for the second Ctrl+C or
Ctrl+Break, so the default handler cannot exit ``STATUS_CONTROL_C_EXIT``.
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
    # Before the agent, the grant check, or the run. A signal during that
    # work, or a second signal while the process is leaving, must not hit
    # the default handler (exit -2, or 0xC000013A on Windows).
    state = _arm_cancel_handlers()
    code = 3
    try:
        policy = getattr(args, "approve_policy", "deny")
        if args.approve and args.auto_deny:
            code = _emit(args, _error_body("error: pass only one of --approve and --auto-deny"), 3)
        elif args.approve and policy != "deny":
            code = _emit(
                args,
                _error_body("error: pass only one of --approve and --approve-policy"),
                3,
            )
        elif args.auto_deny and policy != "deny":
            code = _emit(
                args,
                _error_body("error: pass only one of --auto-deny and --approve-policy"),
                3,
            )
        else:
            try:
                agent = build_agent(args)
                result = _run_until_cancelled(agent, args.goal)
            except KeyboardInterrupt:
                body = _error_body("cancelled")
                body["status"] = "cancelled"
                code = _emit(args, body, 3)
            except Exception as exc:  # noqa: BLE001 - the process must exit 3, not traceback
                code = _emit(args, _error_body(f"error: {type(exc).__name__}: {exc}"), 3)
            else:
                code = _emit(args, result.to_dict(), exit_code(result))
    finally:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            _disarm_cancel_handlers(state)
        else:
            _hard_exit(code)
    return code


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


# One process-wide arm. Replacing the handler from inside it, or putting the
# default back before the process is gone, is the window where a second
# signal dies with -2 or STATUS_CONTROL_C_EXIT (0xC000013A).
_CANCEL_STATE: dict | None = None


def _cancel_state() -> dict:
    global _CANCEL_STATE
    if _CANCEL_STATE is None:
        _CANCEL_STATE = {
            "hits": [0],
            "win_events": 0,
            "agent": None,
            "previous": [],
            "win_handler": None,
            "armed": False,
        }
    return _CANCEL_STATE


def _record_cancel_hit(agent: object, hit: int) -> None:
    """Flush the hit count where a parent can see it before the next signal.

    The file is ``<trace_dir>/cancel_signal``. The write uses ``os`` calls so
    it finishes and reaches disk before ``cancel`` returns.
    """
    directory = getattr(agent, "_trace_dir", None)
    if not directory:
        return
    path = os.path.join(os.fspath(directory), "cancel_signal")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError:
        return
    try:
        os.write(fd, f"{hit}\n".encode())
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _ask_agent_cancel(agent: object) -> None:
    """Latch a cooperative cancel. A signal handler must not raise."""
    if agent is None:
        return
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


def _on_cancel_signal(agent: object, hits: list[int], signum: int, frame: object) -> None:
    """First signal cancels after the current step. The second exits 3.

    The same function stays installed. Swapping it for another handler, or
    restoring the default while this one is still the process's handler,
    lets the second signal kill the process with ``-SIGINT`` (-2) or, on
    Windows, ``STATUS_CONTROL_C_EXIT``. The second hit uses ``os._exit`` so
    finalization cannot turn the exit into that signal death. The hit is
    recorded before the cooperative cancel so a parent can send the second
    signal while this process is still inside the run.
    """
    del signum, frame
    hits[0] += 1
    _record_cancel_hit(agent, hits[0])
    if hits[0] >= 2:
        os._exit(3)
    _ask_agent_cancel(agent)


def _windows_console_event(state: dict, ctrl_type: int) -> bool:
    """Body of the Windows ``SetConsoleCtrlHandler`` callback.

    Ctrl+C is 0 and Ctrl+Break is 1. The handler registered last is called
    first, and this one is registered after Python's. The first of those
    events returns false so Python's handler still runs and the run can
    cancel. The second calls ``os._exit(3)`` and does not return, so Windows
    never applies the default handler (``STATUS_CONTROL_C_EXIT``,
    ``0xC000013A``). Close, logoff, and shutdown are not claimed.
    """
    if ctrl_type not in (0, 1):
        return False
    state["win_events"] = int(state.get("win_events", 0)) + 1
    if int(state["win_events"]) >= 2:
        os._exit(3)
    return False


def _arm_windows_console_handler(state: dict) -> None:
    """Install the console handler once. It is not removed.

    Removing it puts the default handler back. A Ctrl+Break in that window
    exits ``0xC000013A`` instead of 3.
    """
    if state.get("win_handler") is not None or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    routine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _callback(ctrl_type: int) -> int:
        return 1 if _windows_console_event(state, int(ctrl_type)) else 0

    callback = routine(_callback)
    if not kernel.SetConsoleCtrlHandler(callback, True):
        return
    # The callback is a C function pointer. Dropping it lets it be collected
    # and the next control event jumps to a dead address.
    state["win_handler"] = callback
    state["kernel32"] = kernel


def _arm_cancel_handlers() -> dict:
    """Install cancel handlers. The same ones stay up for the whole run."""
    state = _cancel_state()
    if state["armed"]:
        return state
    state["hits"][0] = 0
    state["win_events"] = 0
    state["agent"] = None

    def handler(signum: int, frame: object) -> None:
        _on_cancel_signal(state["agent"], state["hits"], signum, frame)

    for sig in _cancel_signals():
        try:
            previous = signal.signal(sig, handler)
            state["previous"].append((sig, previous))
            if hasattr(signal, "siginterrupt"):
                signal.siginterrupt(sig, True)
        except (OSError, ValueError):
            continue
    _arm_windows_console_handler(state)
    state["armed"] = True
    return state


def _disarm_cancel_handlers(state: dict) -> None:
    """Put back the handlers from before ``_arm_cancel_handlers``.

    Used when ``main`` was called inside another process (the test suite).
    A real ``a11y-agent run`` does not take this path: it ``os._exit``s with
    these handlers still installed. The Windows console handler is never
    removed.
    """
    for sig, previous in state["previous"]:
        try:
            signal.signal(sig, previous)  # type: ignore[arg-type]
        except (OSError, ValueError):
            pass
    state["previous"].clear()
    state["armed"] = False
    state["agent"] = None


def _hard_exit(code: int) -> None:
    """Exit with ``code`` without running the default signal action.

    ``os._exit`` skips stdio flush. The JSON result is written first.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - a closed stream must not block exit
            pass
    os._exit(code)


def _run_until_cancelled(agent: object, goal: str):
    """Run ``goal``. Cancel handlers are already installed.

    A signal that arrived before the agent existed is applied now, and it
    survives ``_loop`` clearing the ordinary cancel event. The first signal
    returns into the step. The trace write for that step closes, and ``run``
    returns a cancelled result. A second signal exits 3 with no traceback.
    """
    state = _cancel_state()
    state["agent"] = agent
    flag = getattr(agent, "_signal_cancel", None)
    if flag is None:
        try:
            agent._signal_cancel = threading.Event()  # type: ignore[attr-defined]
            flag = agent._signal_cancel  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a test double may refuse new attributes
            flag = None
    if state["hits"][0]:
        _ask_agent_cancel(agent)
    elif flag is not None and hasattr(flag, "is_set") and flag.is_set():
        _ask_agent_cancel(agent)
    return agent.run(goal)  # type: ignore[attr-defined]


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
