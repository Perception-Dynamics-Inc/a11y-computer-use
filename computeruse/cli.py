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
    snapshot.add_argument(
        "--mode", choices=("full", "interactive"), default="full",
        help="'full' prints the whole pruned tree; 'interactive' prints only actionable "
             "elements plus their containers, static text folded into one line each "
             "(same refs, far fewer tokens). Default: full",
    )
    snapshot.add_argument(
        "--budget", type=int, metavar="TOKENS",
        help="cap the output at about TOKENS tokens (4 chars each); the tail is replaced "
             "by a marker counting the omitted elements",
    )
    snapshot.add_argument(
        "--bounds", action="store_true",
        help="print geometry on every element line (default: roots only in full mode, "
             "none in interactive mode)",
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

    bench = sub.add_parser(
        "bench",
        help="cu-meter/cu-arena: the token + latency numbers behind the moat",
        description="'audit' aggregates the JSONL audit log (per-action latency + "
                    "tokens). 'web' runs the a11y-vs-vision observation-cost benchmark "
                    "on the browser backend (needs a running Chromium; see "
                    "docs/browser-backend.md). 'desktop' runs the same benchmark on the "
                    "current platform's driver (or $COMPUTERUSE_DRIVER) against a running "
                    "app, costing every snapshot view against one screenshot (see "
                    "docs/observation-cost.md).",
    )
    bench_sub = bench.add_subparsers(required=True)
    bench_audit = bench_sub.add_parser("audit", help="aggregate the audit log (cu-meter)")
    bench_audit.add_argument("dir", nargs="?", help="audit dir (default: the SDK's audit log dir)")
    bench_audit.set_defaults(handler=_cmd_bench_audit)
    bench_web = bench_sub.add_parser("web", help="a11y-vs-vision observation cost (cu-arena)")
    bench_web.add_argument("url", help="URL to observe (e.g. https://example.com)")
    bench_web.add_argument("--rounds", type=int, default=3, help="observations to measure")
    bench_web.add_argument("--endpoint", help="CDP endpoint (default: $COMPUTERUSE_CDP_ENDPOINT "
                           "or http://127.0.0.1:9222)")
    bench_web.add_argument("--mode", choices=("full", "interactive"), default="full",
                           help="snapshot view to cost (default: full)")
    bench_web.add_argument("--json", action="store_true", help="print the report as JSON")
    bench_web.set_defaults(handler=_cmd_bench_web)
    bench_desktop = bench_sub.add_parser(
        "desktop", help="a11y-vs-vision observation cost of a running app, every view (cu-arena)",
    )
    bench_desktop.add_argument("--app", help="bundle id / app name / tab id to observe "
                               "(default: the driver's frontmost app)")
    bench_desktop.add_argument("--scope", choices=("window", "app"), default="window",
                               help="frontmost window only, or all windows (default: window)")
    bench_desktop.add_argument("--rounds", type=int, default=3, help="observations to measure")
    bench_desktop.add_argument("--json", action="store_true", help="print the report as JSON")
    bench_desktop.set_defaults(handler=_cmd_bench_desktop)

    return parser


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from computeruse import server

    server.build_server().run(transport="stdio")
    return 0


def _cmd_bench_audit(args: argparse.Namespace) -> int:
    from pathlib import Path

    from computeruse import bench

    audit_dir = Path(args.dir) if args.dir else Path.home() / ".computeruse" / "audit"
    if not audit_dir.exists():
        print(f"no audit log at {audit_dir}; run some actions first (or pass a dir)",
              file=sys.stderr)
        return 2
    print(bench.format_report(bench.report(audit_dir)))
    return 0


def _cmd_bench_web(args: argparse.Namespace) -> int:
    from computeruse import arena
    from computeruse.drivers.browser import BrowserDriver
    from computeruse.schema import ComputerUseError
    from computeruse import server

    driver = BrowserDriver(endpoint=args.endpoint)
    try:
        report = arena.run_web_task(driver, args.url, rounds=args.rounds, mode=args.mode)
    except ComputerUseError as exc:
        print(server.error_text(exc), file=sys.stderr)
        return 1
    finally:
        driver.close()
    print(json.dumps(report.to_dict(), indent=2) if args.json else arena.format_report(report))
    return 0


def _cmd_bench_desktop(args: argparse.Namespace) -> int:
    from computeruse import arena, drivers, server
    from computeruse.schema import ComputerUseError, Scope

    driver = drivers.get_driver()
    try:
        driver.ensure_trusted()
        report = arena.run_desktop_task(
            driver, args.app, rounds=args.rounds, scope=Scope(args.scope)
        )
    except ComputerUseError as exc:
        print(server.error_text(exc), file=sys.stderr)
        return 1
    finally:
        close = getattr(driver, "close", None)
        if callable(close):
            close()
    print(json.dumps(report.to_dict(), indent=2) if args.json else arena.format_desktop_report(report))
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
    print(observe.render_text(snap, mode=args.mode, budget=args.budget, include_bounds=args.bounds))
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


if __name__ == "__main__":  # `python -m computeruse.cli ...` alongside the console script
    raise SystemExit(main())
