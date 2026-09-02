"""CLI entry point: ``computeruse {mcp,doctor,snapshot,run-once}``.

Subcommands (PLAN.md §9, Phase 1):
    mcp       Run the MCP server over stdio.
    doctor    Print the TCC/environment diagnosis (`doctor.run_doctor`).
              Exits 0 whenever the report could be produced — failed checks
              are report content, not process failures.
    snapshot  Print a pruned a11y snapshot for an app (debugging aid).
    run-once  Execute one JSON action through the safety layer, e.g.
              ``computeruse run-once '{"tool": "key", "chord": "cmd+s"}'``.
    bench     cu-meter (``audit``) and cu-arena (``web``) numbers.
    agent     Run a task end to end with a model (`agent.run_task`): the
              reference observe -> plan -> act -> verify loop.

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

    bench = sub.add_parser(
        "bench",
        help="cu-meter/cu-arena: the token + latency numbers behind the moat",
        description="'audit' aggregates the JSONL audit log (per-action latency + "
                    "tokens). 'web' runs the a11y-vs-vision observation-cost benchmark "
                    "on the browser backend (needs a running Chromium; see "
                    "docs/browser-backend.md).",
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
    bench_web.set_defaults(handler=_cmd_bench_web)
    bench_h2h = bench_sub.add_parser(
        "h2h", help="head-to-head: same planner, refs vs pixels, on the built-in task suite")
    bench_h2h.add_argument("--tasks", default="all",
                           help="'all' or comma-separated task ids (see --list)")
    bench_h2h.add_argument("--modes", default="refs,pixels,pixels+snap",
                           help="comma-separated subset of refs,pixels,pixels+snap")
    bench_h2h.add_argument("--provider", choices=("anthropic", "openai", "claude-cli"),
                           help="planner (default: $COMPUTERUSE_PROVIDER, else auto)")
    bench_h2h.add_argument("--model", help="model id for the provider")
    bench_h2h.add_argument("--rounds", type=int, default=1, help="repeat the whole suite N times")
    bench_h2h.add_argument("--max-steps", type=int, help="override every task's planner-turn budget")
    bench_h2h.add_argument("--price-in", type=float, help="USD per million input tokens (cost estimate)")
    bench_h2h.add_argument("--price-out", type=float, help="USD per million output tokens")
    bench_h2h.add_argument("--endpoint", help="CDP endpoint (default: $COMPUTERUSE_CDP_ENDPOINT "
                                              "or http://127.0.0.1:9222)")
    bench_h2h.add_argument("--out", help="directory to write h2h.md and h2h.json into")
    bench_h2h.add_argument("--json", action="store_true", help="print the JSON report instead of markdown")
    bench_h2h.add_argument("--list", action="store_true", help="list the task suite and exit")
    bench_h2h.set_defaults(handler=_cmd_bench_h2h)

    agent = sub.add_parser(
        "agent",
        help="run a task end to end with a model (the reference agent loop)",
        description=(
            "Observe -> plan -> act -> verify until the model calls done. The planner "
            "sees the pruned accessibility snapshot and acts on element refs; every "
            "action goes through the same gated Runtime as the MCP server (tiers, "
            "recheck, confirmation gate, Effect Receipts, audit). Provider: anthropic "
            "(ANTHROPIC_API_KEY), openai (OPENAI_API_KEY, OPENAI_BASE_URL for Ollama/"
            "vLLM), or claude-cli (the local `claude` command, no key). Honours "
            "COMPUTERUSE_DRIVER=browser like every other command."
        ),
    )
    agent.add_argument("--task", required=True, help="what to accomplish, in plain language")
    agent.add_argument("--app", help="target app (bundle id or name; a tab id on the browser "
                                     "backend). Default: the frontmost app / bound tab")
    agent.add_argument("--provider", choices=("anthropic", "openai", "claude-cli"),
                       help="planner backend (default: $COMPUTERUSE_PROVIDER, else the first "
                            "one the environment supports)")
    agent.add_argument("--model", help="model id for the provider (required for openai)")
    agent.add_argument("--max-steps", type=int, default=25, help="maximum planner turns (default 25)")
    agent.add_argument("--no-verify", action="store_true",
                       help="do not request Effect Receipts on click/act")
    agent.add_argument("--grant", choices=("read", "click", "full"),
                       help="grant the target app this permission tier before running "
                            "(persisted in the permission store, like any grant)")
    agent.add_argument("--json", action="store_true", help="print the result as JSON")
    agent.set_defaults(handler=_cmd_agent)

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
        report = arena.run_web_task(driver, args.url, rounds=args.rounds)
    except ComputerUseError as exc:
        print(server.error_text(exc), file=sys.stderr)
        return 1
    finally:
        driver.close()
    print(arena.format_report(report))
    return 0


def _cmd_bench_h2h(args: argparse.Namespace) -> int:
    import os
    from pathlib import Path

    from computeruse import h2h, providers

    tasks = h2h.load_tasks()
    if args.list:
        for t in tasks:
            print(f"{t.id:16} {t.title}  (max_steps={t.max_steps})")
        return 0
    if args.tasks != "all":
        try:
            tasks = h2h.load_tasks([t.strip() for t in args.tasks.split(",") if t.strip()])
        except KeyError as exc:
            print(f"unknown task {exc}; --list shows the suite", file=sys.stderr)
            return 2
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    bad = [m for m in modes if m not in h2h.MODES]
    if bad:
        print(f"unknown mode(s) {bad}; expected a subset of {list(h2h.MODES)}", file=sys.stderr)
        return 2
    name = args.provider or os.environ.get("COMPUTERUSE_PROVIDER")

    def factory(mode: str):
        if name == "claude-cli" or (name is None and not os.environ.get("ANTHROPIC_API_KEY")
                                    and not os.environ.get("OPENAI_API_KEY")):
            return providers.ClaudeCLIProvider(args.model, view_images=(mode != "refs"))
        return providers.get_provider(name, model=args.model)

    try:
        factory(modes[0])  # fail early on a misconfigured provider
    except providers.ProviderError as exc:
        print(f"provider: {exc}", file=sys.stderr)
        return 2
    endpoint = args.endpoint or os.environ.get("COMPUTERUSE_CDP_ENDPOINT", "http://127.0.0.1:9222")

    def on_event(kind: str, payload: dict) -> None:
        if kind == "start":
            print(f"== {payload['task']} / {payload['mode']} (round {payload['round']})", file=sys.stderr)
        elif kind == "step":
            s = payload["step"]
            first = s.result.splitlines()[0] if s.result else ""
            print(f"   [{s.index}] {s.tool} {json.dumps(s.params)[:100]} -> "
                  f"{'ok' if s.ok else s.error_code}: {first[:100]}", file=sys.stderr)
        elif kind == "run":
            r = payload["record"]
            print(f"   => {'done' if r.success else 'NOT done'} turns={r.turns} misclicks={r.misclicks} "
                  f"wasted={r.wasted} tokens={r.input_tokens}/{r.output_tokens} wall={r.wall_s:.0f}s",
                  file=sys.stderr)

    report = h2h.run_h2h(endpoint, factory, tasks=tasks, modes=modes, rounds=args.rounds,
                         max_steps=args.max_steps, price_in=args.price_in, price_out=args.price_out,
                         on_event=on_event, meta={"planner": name or "auto"})
    text = h2h.format_report(report)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "h2h.md").write_text(text + "\n", encoding="utf-8")
        (out / "h2h.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(f"wrote {out / 'h2h.md'} and {out / 'h2h.json'}", file=sys.stderr)
    print(json.dumps(report.to_dict(), indent=2) if args.json else text)
    agg = report.aggregate()
    return 0 if agg and all(v["completed"] > 0 for v in agg.values()) else 1


def _cmd_agent(args: argparse.Namespace) -> int:
    from computeruse import agent, providers, safety, server
    from computeruse.schema import ComputerUseError

    try:
        provider = providers.get_provider(args.provider, model=args.model)
    except providers.ProviderError as exc:
        print(f"provider: {exc}", file=sys.stderr)
        return 2
    runtime = server.Runtime()
    app = args.app
    if args.grant:
        try:
            target = app if app is not None else runtime._frontmost()
            _running, target = runtime._resolve_app(target)
        except ComputerUseError as exc:
            print(server.error_text(exc), file=sys.stderr)
            return 1
        runtime.store.set_tier(target, safety.Tier(args.grant))
        print(f"granted {target} tier {args.grant}", file=sys.stderr)

    def on_step(step: agent.Step) -> None:
        status = "ok" if step.ok else step.error_code
        tokens = f"{step.usage.input_tokens} in/{step.usage.output_tokens} out tok" if step.usage.total else ""
        first_line = step.result.splitlines()[0] if step.result else ""
        print(f"[step {step.index}] {step.tool} {json.dumps(step.params)[:160]} -> {status}: "
              f"{first_line[:160]} ({step.duration_ms:.0f} ms{', ' + tokens if tokens else ''})",
              file=sys.stderr)

    confirm = None
    if sys.stdin.isatty():  # a human at the terminal can approve irreversible actions
        def confirm(prompt: str) -> bool:
            answer = input(f"{prompt} [y/N] ")
            return answer.strip().lower() in ("y", "yes")

    result = agent.run_task(args.task, runtime, provider, app=app, max_steps=args.max_steps,
                            verify=not args.no_verify, on_step=on_step, confirm=confirm)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        verdict = "success" if result.success else f"not completed ({result.stopped})"
        print(f"{verdict}: {result.summary}")
        print(f"steps={len(result.steps)} planner tokens in={result.usage.input_tokens} "
              f"out={result.usage.output_tokens} wall={result.wall_time_s:.1f}s "
              f"audit={result.audit_dir}")
    return 0 if result.success else 1


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


if __name__ == "__main__":  # `python -m computeruse.cli ...` alongside the console script
    raise SystemExit(main())
