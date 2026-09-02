"""Run one task end to end with the reference agent loop, in process.

The loop is the same one behind ``computeruse agent``. Embedding it means you
pick the planner (any `computeruse.providers.Provider`) and keep the gated
Runtime, so permission tiers, the confirmation gate, Effect Receipts, and the
audit log apply exactly as they do under the MCP server.

    # macOS / Windows / Linux: the frontmost app, planned by the local claude CLI
    python examples/agent_task.py "Open the File menu and read the first item"

    # Browser backend: any Chromium with --remote-debugging-port=9222
    COMPUTERUSE_DRIVER=browser python examples/agent_task.py "Fill Name with Alice and press Go"

    # Other planners
    ANTHROPIC_API_KEY=... COMPUTERUSE_PROVIDER=anthropic python examples/agent_task.py "..."
    OPENAI_BASE_URL=http://localhost:11434/v1 COMPUTERUSE_PROVIDER=openai python examples/agent_task.py "..." llama3.1

The target app needs a permission grant first (``full`` lets the planner type):
``computeruse agent --grant full --task ...`` does it from the CLI, or call
``runtime.store.set_tier(app_id, safety.Tier.FULL)`` as below.
"""

from __future__ import annotations

import sys

from computeruse import agent, providers, safety, server


def main(task: str, model: str | None = None) -> int:
    runtime = server.Runtime()  # driver from COMPUTERUSE_DRIVER or the OS
    provider = providers.get_provider(model=model)  # anthropic | openai | claude-cli

    app = runtime._frontmost()  # bundle id, process name, or the bound tab id
    runtime.store.set_tier(app, safety.Tier.FULL)  # your product decides this policy

    def on_step(step: agent.Step) -> None:
        status = "ok" if step.ok else step.error_code
        print(f"[{step.index}] {step.tool} {step.params} -> {status}", file=sys.stderr)

    result = agent.run_task(task, runtime, provider, app=app, max_steps=20, on_step=on_step)
    print(("success" if result.success else f"stopped ({result.stopped})") + ": " + result.summary)
    print(f"{len(result.steps)} steps, planner tokens in={result.usage.input_tokens} "
          f"out={result.usage.output_tokens}, audit log at {result.audit_dir}")
    return 0 if result.success else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
