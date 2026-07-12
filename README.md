# computerUse

**The accessibility-first computer-use framework for macOS — give any LLM native control of your Mac through the accessibility tree, not guessed pixel coordinates.**

> ⚠️ **Status: Phase-0 spike (v0.0.1).** macOS-only, early, and evolving fast. The core loop is built and live-proven (see [Proof it works](#proof-it-works)); the public benchmark, Windows support, and PyPI release are on the roadmap, not shipped. The project name is a working title — a rename is planned before launch.

---

## Why this exists

Every mainstream "computer use" agent — Anthropic's, OpenAI's, and the open-source clones — drives your machine the same way: **screenshot → the model guesses (x, y) pixel coordinates → click → screenshot again.** Coordinate regression is the single biggest source of error: desktop grounding accuracy on the ScreenSpot-Pro benchmark is still only ~40–46%. It also *requires* a vision model on every single step.

The browser world already solved this a better way. Tools like [Playwright MCP](https://github.com/microsoft/playwright-mcp) serialize the page's **accessibility tree**, hand the model a list of real elements with stable refs, and let it say `click e14` instead of `click (512, 663)`. Deterministic. Cheap. Works with non-vision models.

**Nobody has built that for the native macOS desktop** — and the marquee desktop projects that tried mostly died or pivoted to cloud in the last year. `computerUse` is that missing layer: the same accessibility-tree-first approach, for the apps actually on your Mac.

## The wedge

*Native Mac control for any LLM via the accessibility tree — **exact clickable element refs instead of guessed pixel coordinates**, so:*

- **Fewer misclicks** — the model targets a real UI element (`click e14`), not a coordinate it regressed from a screenshot.
- **Any model works** — a11y refs are plain text, so cheap non-vision models and **local Ollama models** can drive the desktop; vision is a fallback, not a requirement.
- **It doesn't fight you for the mouse** — ref actions execute through the accessibility API (`AXPress` / set `AXValue`), so the agent can click and type **without moving your physical cursor or stealing keyboard focus.** A pixel-loop agent cannot do this — it hijacks your mouse on every step.
- **Deterministic & auditable** — every action resolves through a structured element ref and is written to an always-on JSONL audit log. You can see exactly what the agent did and why.

Honest note: this is **not** a token-savings pitch. Phase-0 measurement found pruned window snapshots run ~680–1,460 tokens — about the same as one screenshot, not 10× smaller. The win is *reliability + model choice + non-intrusiveness + auditability*, and we intend to prove it with a published head-to-head benchmark, not adjectives.

## Why not the alternatives?

| | Approach | macOS native? | Model-agnostic? | Embeddable? |
|---|---|---|---|---|
| **Claude Desktop built-in** | pixel loop | ✅ | ❌ Anthropic-only | ❌ closed app, not a library |
| **browser-use / Playwright MCP** | a11y tree | ❌ browser only | ✅ | ✅ |
| **UI-TARS-desktop** | pixel/vision | ✅ | partial | app, pivoted to agent stack |
| **Windows-MCP / Terminator** | a11y (UIA) | ❌ Windows | ✅ | ✅ |
| **computerUse** | **a11y tree (AX)** | ✅ | ✅ any LLM / local | ✅ MCP + CLI |

We deliberately **don't** build another browser agent (use Playwright MCP / browser-use), VM sandbox infra (integrate E2B / cua / Docker), or a foundation model.

---

## Quickstart

**Requirements:** macOS (developed on Sequoia+), Python 3.11+. macOS control needs two one-time system permission grants — `doctor` walks you through them.

```bash
# 1. Clone and install (uv recommended; pip works too)
git clone <repo-url> computerUse && cd computerUse
uv venv && uv pip install -e .          # or: python3 -m venv .venv && .venv/bin/pip install -e .

# 2. Diagnose & grant permissions (the honest "5-minute TCC dance")
computeruse doctor
#   → tells you exactly which app (Terminal, your IDE, Claude Desktop) needs
#     Accessibility + Screen Recording, and opens the right Settings pane.
#     Grants attach to the *responsible* process — relaunch that app after granting.

# 3. See a real app's accessibility tree (pruned + indexed with refs)
computeruse snapshot --app TextEdit

# 4. Run a single action through the safety layer
computeruse run-once '{"tool": "key", "chord": "cmd+s"}'
```

### Use it from an agent (MCP)

`computerUse` ships as an [MCP](https://modelcontextprotocol.io) server over stdio, so any MCP host (Claude Code, Claude Desktop, Cursor, your own loop) can drive the desktop.

```bash
# Add to Claude Code (run from the repo root):
claude mcp add computeruse -- "$(pwd)/.venv/bin/computeruse" mcp

# Or run the server directly:
computeruse mcp
```

---

## The tool surface (12 tools)

Designed as the front door — the same canonical schema the CLI and MCP server share:

| Observe | Act | Manage |
|---|---|---|
| `desktop_snapshot` — pruned a11y tree + refs, scoped to window/app | `click` (ref \| coord, button, modifiers) | `app` — list / launch / focus |
| `screenshot` | `type` · `key` · `scroll` · `drag` | `window` — list / raise / move / resize |
| `zoom` — full-res region crop | `wait_for` — actionability primitive (ref/condition) | `clipboard` — read / write |

Element refs (`e14`) are **snapshot-scoped**: `act()` re-resolves them against the live tree via anchored attributes (role, title, path, bounds) and returns a structured `stale_ref` error prompting a re-observe — the [Playwright MCP snapshot-epoch design](https://playwright.dev/mcp/snapshots), adapted for the messier world of native AX trees.

## Safety model

Safety wraps **every** action — but it's table stakes done well, never a speed bump on the quickstart (sane defaults, one prompt):

- **Per-app grants** with **read / click / full** tiers, stored in `~/.computeruse/permissions.json`.
- **Frontmost-window hit-test** at act-time, with a same-window recheck between the permission decision and input injection (a structured `focus_changed` error if a toast or overlay races the click).
- **Secure-field handoff** — password fields (`IsSecureEventInputEnabled` / secure AX roles) surface a structured `secure_field` state instead of being typed into.
- **Always-on JSONL audit log** at `~/.computeruse/audit/` with secure-field redaction — every action is inspectable after the fact.

## How computer use actually works

The model never touches your machine. It only emits structured actions; the **client** (this framework) executes them and feeds back what it observes:

```
┌─> observe (pruned a11y tree + refs) ──> model picks an action
│                                              │
│   execute on real machine   <── {tool:"click", ref:"e14"}
│   (AXPress / CGEvent)                         │
└── re-observe ─────────────────────────────────┘   repeat until done
```

The full write-up — provider contracts, the three grounding approaches, per-OS execution layers, and the architecture — lives in **[PLAN.md](./PLAN.md)**. Demand validation (30 verified "I tried to automate my Mac and it failed" stories) is in **[docs/demand-validation.md](./docs/demand-validation.md)**.

## Proof it works

- **Live end-to-end:** an agent opened TextEdit, found the text area as a structured element, clicked it, typed, and verified the result by reading the AX tree back — with the action in the audit log.
- **Rich refs on real apps:** Calendar exposes 102 labeled clickable refs; System Settings its full sidebar — all as plain-text refs a non-vision model can target.
- **190 tests** (unit + live e2e smoke + a real-subprocess MCP handshake).

## Roadmap

- **Phase 0 (now)** — demand validation ✅, hostile-app AX spike (Electron/Slack/VS Code), signing + notarization, confirmation-gate UX, Rust-vs-Swift boundary decision.
- **Phase 1** — hardened macOS driver, tree-pruning engine, vision fallback on Electron, launch assets (hero demo, published benchmark, beta cohort).
- **Phase 2** — Windows (UIA), provider executor adapters (Anthropic `computer_20251124`, OpenAI `computer`), local/Ollama planner-grounder split.
- **Phase 3** — teach & replay (audit logs double as demonstrations).

See [PLAN.md §9](./PLAN.md) for the full roadmap and exit criteria.

## License

**[Apache-2.0](./LICENSE)** (with [NOTICE](./NOTICE)). The explicit patent grant matters for a project that injects input and reads UI trees, and it's the license the credible OSS peers use (Chrome DevTools MCP, Playwright MCP). Governance intent: no CLA-to-relicense trap, roadmap in the open, signed releases.

## Contributing

Early days — issues and discussion welcome. Please read [PLAN.md](./PLAN.md) first; it's the source of truth for architecture and scope (including the explicit anti-goals).
