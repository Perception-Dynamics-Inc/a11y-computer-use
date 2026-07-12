"""CLI entry point: ``computeruse {mcp,doctor,snapshot,run-once}``.

Subcommands (PLAN.md §9, Phase 1):
    mcp       Run the MCP server over stdio.
    doctor    Print the TCC/environment diagnosis (`doctor.run_doctor`).
              Exits 0 whenever the report could be produced — failed checks
              are report content, not process failures.
    snapshot  Print a pruned a11y snapshot for an app (debugging aid).
    run-once  Execute one JSON action through the safety layer, e.g.
              ``computeruse run-once '{"tool": "key", "chord": "cmd+s"}'``.

Exit codes: 0 success; 1 structured failure (a `schema.ComputerUseError` or
a safety refusal, rendered on stderr); 2 usage errors (argparse, malformed
JSON, unknown tool/params).

Heavy imports (pyobjc, mcp) happen inside the subcommand handlers so that
``computeruse doctor`` can still diagnose a broken install.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` (defaults to ``sys.argv[1:]``) and dispatch a subcommand.

    Returns:
        Process exit code (0 on success).
    """
    args = _build_parser().parse_args(argv)
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="computeruse",
        description="Accessibility-first computer-use framework for macOS.",
    )
    sub = parser.add_subparsers(required=True)

    mcp = sub.add_parser("mcp", help="run the MCP server over stdio")
    mcp.set_defaults(handler=_cmd_mcp)

    doctor = sub.add_parser("doctor", help="diagnose TCC grants and environment")
    doctor.set_defaults(handler=_cmd_doctor)

    snapshot = sub.add_parser("snapshot", help="print a pruned a11y snapshot for an app")
    snapshot.add_argument(
        "--app", help="bundle id or display name (default: the frontmost app)"
    )
    snapshot.add_argument(
        "--scope", choices=("window", "app"), default="window",
        help="frontmost window only, or all windows (default: window)",
    )
    snapshot.set_defaults(handler=_cmd_snapshot)

    run_once = sub.add_parser(
        "run-once",
        help="execute one action through the safety layer",
        description=(
            "Execute one JSON action, e.g. '{\"tool\": \"click\", \"x\": 100, "
            "\"y\": 200}'. The object needs a \"tool\" key (click, type, key, "
            "scroll, drag, app, window, clipboard); the remaining keys are "
            "that tool's parameters. Element refs (and wait_for) need a live "
            "snapshot epoch, which a one-shot process does not have — "
            "click/scroll/drag take x/y coordinate targets only."
        ),
    )
    run_once.add_argument("action", help="JSON object with a 'tool' key plus parameters")
    run_once.set_defaults(handler=_cmd_run_once)

    return parser


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from computeruse import server

    server.build_server().run(transport="stdio")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from computeruse import doctor

    print(doctor.render_text(doctor.run_doctor()))
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    from computeruse import observe, safety, server
    from computeruse.schema import ComputerUseError, Scope

    app = args.app
    if app is None:
        app, _pid = safety.frontmost_app()
        if app is None:
            print("no frontmost application detected; pass --app NAME", file=sys.stderr)
            return 2
    try:
        snap = observe.snapshot(Scope(args.scope), app=app)
    except ComputerUseError as exc:
        print(server.error_text(exc), file=sys.stderr)
        return 1
    print(observe.render_text(snap))
    return 0


def _cmd_run_once(args: argparse.Namespace) -> int:
    from computeruse import server
    from computeruse.schema import ComputerUseError

    try:
        payload = json.loads(args.action)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON action: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict) or not isinstance(payload.get("tool"), str):
        print(
            'action must be a JSON object with a "tool" key, '
            'e.g. \'{"tool": "key", "chord": "cmd+s"}\'',
            file=sys.stderr,
        )
        return 2
    tool = payload.pop("tool")
    try:
        result = server.Runtime().dispatch(tool, payload)
    except ComputerUseError as exc:
        print(server.error_text(exc), file=sys.stderr)
        return 1
    except server.ActionRefused as exc:
        print(server.refusal_text(exc.decision), file=sys.stderr)
        return 1
    except (TypeError, ValueError) as exc:
        print(f"invalid action: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0
