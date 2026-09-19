"""CLI entry point: ``a11y_computer_use {mcp,doctor,snapshot,run-once}``.

Subcommands (PLAN.md §9, Phase 1):
    mcp       Run the MCP server over stdio.
    doctor    Print the TCC/environment diagnosis (`doctor.run_doctor`).
              Exits 0 whenever the report could be produced — failed checks
              are report content, not process failures.
    snapshot  Print a pruned a11y snapshot for an app (debugging aid).
    run-once  Execute one JSON action through the safety layer, e.g.
              ``a11y_computer_use run-once '{"tool": "key", "chord": "cmd+s"}'``.
    bench     cu-meter (``audit``) and cu-arena (``web``) numbers.
    agent     Run a task end to end with a model (`agent.run_task`): the
              reference observe -> plan -> act -> verify loop.

Exit codes: 0 success; 1 structured failure (a `schema.ComputerUseError` or
a safety refusal, rendered on stderr); 2 usage errors (argparse, malformed
JSON, unknown tool/params).

Heavy imports (pyobjc, mcp) happen inside the subcommand handlers so that
``a11y_computer_use doctor`` can still diagnose a broken install.
"""

from __future__ import annotations

import argparse
import json
import os
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
    parser = argparse.ArgumentParser(prog="a11y-computer-use",
        description=(
            "Accessibility-first computer use for AI agents on macOS, Windows, Linux, "
            "and Chromium (CDP)."
        ),
    )
    sub = parser.add_subparsers(required=True)

    mcp = sub.add_parser("mcp", help="run the MCP server over stdio")
    mcp.set_defaults(handler=_cmd_mcp)

    doctor = sub.add_parser(
        "doctor", help="diagnose OS permissions (macOS TCC), the a11y bus (Linux), and the environment"
    )
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
                    "current platform's driver (or $A11Y_COMPUTER_USE_DRIVER) against a running "
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
    bench_web.add_argument("--endpoint", help="CDP endpoint (default: $A11Y_COMPUTER_USE_CDP_ENDPOINT "
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

    bench_h2h = bench_sub.add_parser(
        "h2h", help="head-to-head: same planner, refs vs pixels, on the built-in task suite")
    bench_h2h.add_argument("--tasks", default="all",
                           help="'all' or comma-separated task ids (see --list)")
    bench_h2h.add_argument("--modes", default="refs,pixels,pixels+snap",
                           help="comma-separated subset of refs,pixels,pixels+snap")
    bench_h2h.add_argument("--provider", choices=("anthropic", "openai", "claude-cli"),
                           help="planner (default: $A11Y_COMPUTER_USE_PROVIDER, else auto)")
    bench_h2h.add_argument("--model", help="model id for the provider")
    bench_h2h.add_argument("--rounds", type=int, default=1, help="repeat the whole suite N times")
    bench_h2h.add_argument("--max-steps", type=int, help="override every task's planner-turn budget")
    bench_h2h.add_argument("--price-in", type=float, help="USD per million input tokens (cost estimate)")
    bench_h2h.add_argument("--price-out", type=float, help="USD per million output tokens")
    bench_h2h.add_argument("--endpoint", help="CDP endpoint (default: $A11Y_COMPUTER_USE_CDP_ENDPOINT "
                                              "or http://127.0.0.1:9222)")
    bench_h2h.add_argument("--out", help="directory to write h2h.md and h2h.json into")
    bench_h2h.add_argument("--json", action="store_true", help="print the JSON report instead of markdown")
    bench_h2h.add_argument("--list", action="store_true", help="list the task suite and exit")
    bench_h2h.add_argument("--render", metavar="JSON", nargs="+",
                           help="do not run; re-render one or more saved h2h.json files (merged) "
                                "with the current manifests' comparability notes")
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
            "A11Y_COMPUTER_USE_DRIVER=browser like every other command."
        ),
    )
    agent.add_argument("--task", required=True, help="what to accomplish, in plain language")
    agent.add_argument("--app", help="target app (bundle id or name; a tab id on the browser "
                                     "backend). Default: the frontmost app / bound tab")
    agent.add_argument("--provider", choices=("anthropic", "openai", "claude-cli"),
                       help="planner backend (default: $A11Y_COMPUTER_USE_PROVIDER, else the first "
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

    mission = sub.add_parser(
        "mission",
        help="run a long, multi-app task as verified phases (see docs/missions.md)",
        description="A mission file (TOML) lists phases: each is one agent run with its "
                    "own task, the apps it may drive, a step budget, and checks the runner "
                    "evaluates afterwards (files, URLs, on-screen text, notes). Failed "
                    "checks re-run the phase with the failure in the agent's notes. "
                    "Artifacts land under runs/<mission>/<timestamp>/.",
    )
    mission_sub = mission.add_subparsers(required=True)
    mission_validate = mission_sub.add_parser("validate", help="parse and check a mission file")
    mission_validate.add_argument("file", help="mission TOML file")
    mission_validate.set_defaults(handler=_cmd_mission_validate)
    mission_run = mission_sub.add_parser("run", help="run a mission")
    mission_run.add_argument("file", help="mission TOML file")
    mission_run.add_argument("--provider", choices=("anthropic", "openai", "claude-cli"),
                             help="planner backend (default: $A11Y_COMPUTER_USE_PROVIDER, else auto)")
    mission_run.add_argument("--model", help="model id for the provider")
    mission_run.add_argument("--from-phase", type=int, default=1, metavar="N",
                             help="start at phase N (1-based); earlier phases are skipped")
    mission_run.add_argument("--runs-dir", default="runs", help="where artifacts go (default: runs/)")
    mission_run.add_argument("--dry-run", action="store_true",
                             help="validate and print the phase plan without running anything")
    mission_run.add_argument("--json", action="store_true", help="print the result as JSON")
    mission_run.set_defaults(handler=_cmd_mission_run)

    return parser


def _cmd_mcp(_args: argparse.Namespace) -> int:
    from a11y_computer_use import server

    server.build_server().run(transport="stdio")
    return 0


def _cmd_bench_audit(args: argparse.Namespace) -> int:
    from pathlib import Path

    from a11y_computer_use import bench

    audit_dir = Path(args.dir) if args.dir else Path.home() / ".a11y-computer-use" / "audit"
    if not audit_dir.exists():
        print(f"no audit log at {audit_dir}; run some actions first (or pass a dir)",
              file=sys.stderr)
        return 2
    print(bench.format_report(bench.report(audit_dir)))
    return 0


def _cmd_bench_web(args: argparse.Namespace) -> int:
    from a11y_computer_use import arena
    from a11y_computer_use.drivers.browser import BrowserDriver
    from a11y_computer_use.schema import ErrorCode, ComputerUseError
    from a11y_computer_use import server

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
    from a11y_computer_use import arena, drivers, server
    from a11y_computer_use.schema import ComputerUseError, Scope

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


def _cmd_bench_h2h(args: argparse.Namespace) -> int:
    import os
    from pathlib import Path

    from a11y_computer_use import h2h, providers

    tasks = h2h.load_tasks()
    if args.list:
        for t in tasks:
            flags = "".join(f"  [{m}: not comparable]" for m, _ in t.not_comparable)
            print(f"{t.id:16} {t.title}  (max_steps={t.max_steps}){flags}")
        return 0
    if args.render:
        report = h2h.H2HReport()
        specs = {t.id: t for t in tasks}
        for path in args.render:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"cannot read {path}: {exc}", file=sys.stderr)
                return 2
            part = h2h.H2HReport.from_dict(data)
            for record in part.records:  # apply the manifests' current comparability notes
                spec = specs.get(record.task_id)
                reason = spec.comparability(record.mode) if spec else None
                record.comparable, record.note = reason is None, reason or record.note
            report.records.extend(part.records)
            if not report.meta:
                report.meta = part.meta
            elif part.meta:
                report.meta = {**part.meta, **report.meta, "merged_from": args.render}
        text = h2h.format_report(report)
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            (out / "h2h.md").write_text(text + "\n", encoding="utf-8")
            (out / "h2h.json").write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print(json.dumps(report.to_dict(), indent=2) if args.json else text)
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
    name = args.provider or os.environ.get("A11Y_COMPUTER_USE_PROVIDER")

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
    endpoint = args.endpoint or os.environ.get("A11Y_COMPUTER_USE_CDP_ENDPOINT", "http://127.0.0.1:9222")

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



def _grant_target(runtime: "server.Runtime", app: str | None) -> str:
    """The app id ``--grant`` should key on.

    A running app resolves to its id through the driver. An app that is NOT
    running yet is still a valid target (the loop can `app launch` it), so a
    bundle id is granted as given and a display name is looked up among the
    installed applications; only an identifier that matches nothing raises.
    """
    from a11y_computer_use import server
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    target = app if app is not None else runtime._frontmost()
    try:
        _running, resolved = runtime._resolve_app(target)
        return resolved
    except ComputerUseError as exc:
        if exc.code is not ErrorCode.APP_NOT_FOUND or app is None:
            raise
    installed = server._installed_bundle_id(target)
    if installed:
        return installed
    raise ComputerUseError(
        ErrorCode.APP_NOT_FOUND,
        f"no running or installed application matches {target!r}; pass its bundle id",
        detail={"app": target},
    )

def _cmd_agent(args: argparse.Namespace) -> int:
    from a11y_computer_use import agent, providers, safety, server
    from a11y_computer_use.schema import ComputerUseError

    try:
        provider = providers.get_provider(args.provider, model=args.model)
    except providers.ProviderError as exc:
        print(f"provider: {exc}", file=sys.stderr)
        return 2
    runtime = server.Runtime()
    app = args.app
    if args.grant:
        try:
            target = _grant_target(runtime, app)
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


def _cmd_mission_validate(args: argparse.Namespace) -> int:
    from a11y_computer_use import mission as mission_mod

    try:
        mission = mission_mod.load(args.file)
    except (OSError, ValueError) as exc:
        print(f"mission: {exc}", file=sys.stderr)
        return 2
    print(f"{mission.name}: {len(mission.phases)} phases"
          + (f", deadline {mission.deadline_s:g}s" if mission.deadline_s else ""))
    for i, phase in enumerate(mission.phases, 1):
        print(f"  {i}. {phase.name}: tier {phase.tier}, {phase.max_steps} steps, "
              f"{phase.retries} retries, {len(phase.checks)} checks, apps {phase.apps or '-'}")
    return 0


def _cmd_mission_run(args: argparse.Namespace) -> int:
    from a11y_computer_use import agent, mission as mission_mod, providers, server

    try:
        mission = mission_mod.load(args.file)
    except (OSError, ValueError) as exc:
        print(f"mission: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        return _cmd_mission_validate(args)
    try:
        provider = providers.get_provider(args.provider, model=args.model)
    except providers.ProviderError as exc:
        print(f"provider: {exc}", file=sys.stderr)
        return 2
    runtime = server.Runtime()

    def on_step(phase: str, step: agent.Step) -> None:
        status = "ok" if step.ok else step.error_code
        first_line = step.result.splitlines()[0] if step.result else ""
        print(f"[{phase} step {step.index}] {step.tool} {json.dumps(step.params)[:120]} -> "
              f"{status}: {first_line[:120]} ({step.duration_ms:.0f} ms)", file=sys.stderr)

    confirm = None
    if sys.stdin.isatty():
        def confirm(prompt: str) -> bool:
            return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")

    result = mission_mod.run(mission, runtime, provider, runs_dir=args.runs_dir,
                             from_phase=args.from_phase, on_step=on_step, confirm=confirm)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        for phase in result.phases:
            print(f"{phase.name}: {'passed' if phase.passed else 'failed'} "
                  f"after {phase.attempts} attempt(s), {phase.finished_at - phase.started_at:.0f}s")
        print(f"{'completed' if result.passed else result.stopped}: artifacts in {result.run_dir}")
    return 0 if result.passed else 1


def _cmd_doctor(_args: argparse.Namespace) -> int:
    from a11y_computer_use import doctor

    print(doctor.render_text(doctor.run_doctor()))
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    from a11y_computer_use import observe, server
    from a11y_computer_use.drivers import get_driver
    from a11y_computer_use.schema import ComputerUseError, Scope

    # Through the platform Driver seam, so `a11y_computer_use snapshot` works on
    # macOS (AX), Windows (UIA), Linux (AT-SPI2) and the browser alike. The
    # macOS driver delegates to observe.snapshot / safety.frontmost_app.
    driver = get_driver()
    app = args.app
    if app is None:
        app, _pid = driver.frontmost_app()
        if app is None:
            print("no frontmost application detected; pass --app NAME", file=sys.stderr)
            return 2
    try:
        snap = driver.snapshot(Scope(args.scope), app)
    except ComputerUseError as exc:
        print(server.error_text(exc), file=sys.stderr)
        return 1
    print(observe.render_text(snap, mode=args.mode, budget=args.budget, include_bounds=args.bounds))
    return 0


def _cmd_run_once(args: argparse.Namespace) -> int:
    from a11y_computer_use import server
    from a11y_computer_use.schema import ComputerUseError

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


if __name__ == "__main__":  # `python -m a11y_computer_use.cli ...` alongside the console script
    raise SystemExit(main())
