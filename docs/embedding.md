# Embed it: Python API, adapters, agent loop, voice, missions

> Moved here from the top-level README on 2026-10-17. The README is now a short landing page.

## Embed it

```python
from a11y_computer_use import safety, server

store = safety.PermissionStore()
store.set_tier("com.apple.TextEdit", safety.Tier.FULL)
rt = server.Runtime(store=store)
print(rt.desktop_snapshot("com.apple.TextEdit", mode="interactive"))
rt.click(ref="e3", verify=True)
```

Existing Anthropic or OpenAI computer-use loops can run through it unchanged via `a11y_computer_use.adapters` ([docs/provider-adapters.md](./provider-adapters.md)). A reference agent loop ships as `a11y-computer-use agent --task "..."` and works with Anthropic, OpenAI-compatible endpoints (Ollama included), or the Claude Code CLI. `a11y-agent` runs one goal from an accessibility snapshot; the loop, the scripted model, and the exit codes are in [docs/agent.md](./agent.md).

Spoken commands skip the planner: `a11y-computer-use voice` matches each utterance to one fixed skill and runs it through the same safety gate, with a per-command timeline ([docs/voice.md](./voice.md)). `agent --mcp-command "ssh vm a11y-computer-use mcp"` keeps the planner local and runs every action on a remote machine under its own grants.

Long jobs across several apps run as missions: phases with their own app grants, step budgets, runner-side checks (a file exists, a URL answers, text is on screen), retries, and a wall-clock timeline for video cuts.

```bash
a11y-computer-use mission run examples/missions/agency-demo.toml --provider claude-cli
```

Format and checks: [docs/missions.md](./missions.md). The example mission is the design-agency reel described in [docs/missions/agency-demo.md](./missions/agency-demo.md).
