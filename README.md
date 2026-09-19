<p align="center">
  <img src="docs/assets/banner.png" alt="a11y-computer-use" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Perception-Dynamics-Inc/a11y-computer-use/actions/workflows/ci.yml"><img src="https://github.com/Perception-Dynamics-Inc/a11y-computer-use/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/a11y-computer-use/"><img src="https://img.shields.io/pypi/v/a11y-computer-use?label=PyPI" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0"></a>
  <a href="https://github.com/Perception-Dynamics-Inc/a11y-computer-use/releases/tag/v0.2.1"><img src="https://img.shields.io/badge/release-v0.2.1-2A8CFF" alt="Release v0.2.1"></a>
</p>

Computer use for AI agents that clicks real UI elements instead of guessed pixels. The model reads a pruned accessibility tree and says `click e14`. Works on macOS, Windows, Linux, and Chromium, through one MCP server.

> 0.2.0 adds OCR refs for apps with no accessibility tree, a mission runner for long multi-app jobs, and macOS menu and file-dialog tools. Live-verified on this release: OCR of the real screen (about 0.5 s), TextEdit menus through the accessibility menu bar, and Figma's tree through the Electron fix. The full agency mission in `docs/missions/` has not been run end to end yet.

![Driving TextEdit through accessibility refs](docs/hero-demo.gif)

## Install

```bash
uvx a11y-computer-use doctor            # try it, no install
pip install a11y-computer-use           # extras: [browser] [windows] [linux]
```

Add it to Claude Code, Claude Desktop, Cursor, or any MCP host:

```bash
claude mcp add a11y-computer-use -- uvx a11y-computer-use mcp
```

```json
{ "mcpServers": { "a11y-computer-use": { "command": "uvx", "args": ["a11y-computer-use", "mcp"] } } }
```

For a browser tab instead of the desktop, start Chrome with `--remote-debugging-port=9222` and set `A11Y_COMPUTER_USE_DRIVER=browser`.

## What the model sees

```text
[snap-7] com.apple.TextEdit (window)
  e1 window "Untitled"
    e2 textarea ="Hello" (edit,focus)
    e3 button "Save" (click)
```

Then it acts: `click(ref="e3")`, `type("hello")`, `set_value(ref="e2", value="...")`. Ref clicks go through the accessibility API and leave your pointer alone. A stale ref comes back as `stale_ref` with the nearest live candidates. Re-observing as a diff costs about 10 tokens. Apps with no accessibility tree (Telegram, canvases) get OCR refs `o1..oN` from on-device text recognition, and `click(ref="o7")` works the same way ([docs/ocr-refs.md](./docs/ocr-refs.md)).

21 tools: `desktop_snapshot`, `find`, `screen_text`, `screenshot`, `zoom`, `click`, `type`, `key`, `scroll`, `drag` (with waypoint paths for strokes), `wait_for`, `wait_until`, `act`, `set_value`, `scroll_to_find`, `notes`, `menu`, `file_dialog`, `app`, `window`, `clipboard`, plus `console` and `network` on the browser. Menus and file dialogs stay reachable even in custom-drawn apps ([docs/macos-primitives.md](./docs/macos-primitives.md)). Details: [docs/agent-loop.md](./docs/agent-loop.md) and the tool docstrings.

## Why not the alternatives

<p align="center"><img src="docs/assets/alternatives.png" alt="Comparison with other computer-use approaches" width="92%"></p>

| | Approach | Native desktop | Any model | Embeddable |
|---|---|---|---|---|
| Claude Desktop built-in | pixel loop | ✅ | ❌ | ❌ |
| browser-use, Playwright MCP | a11y tree | ❌ browser only | ✅ | ✅ |
| UI-TARS-desktop | pixel / vision | ✅ | partial | app |
| Windows-MCP, Terminator | a11y (UIA) | Windows only | ✅ | ✅ |
| **a11y-computer-use** | a11y tree + vision fallback | ✅ macOS, Linux, Windows*, Chromium | ✅ local too | ✅ MCP, CLI, Python |

Same planner, 13 browser tasks, one round: refs finished 13/13 with 0 misclicks for $7.04; screenshot coordinates finished 7/13 with 27 misclicks for $11.74 ([full results](./docs/benchmarks/h2h-2026-09-02.md)). Run it yourself with `a11y-computer-use bench h2h`.

## Platforms

<p align="center"><img src="docs/assets/backends.png" alt="Four drivers under one core" width="92%"></p>

| | Observe | Ref actions | Type, keys | Coordinates, screenshot | Verified |
|---|---|---|---|---|---|
| macOS (AX) | ✅ | ✅ | ✅ | ✅ | live on a granted Mac |
| Linux (AT-SPI2) | ✅ | ✅ | ✅ | ✅ X11, ❌ Wayland | CI + real desktop VM |
| Browser (CDP) | ✅ | ✅ | ✅ | ✅ | CI, headless Chrome |
| Windows (UIA)* | ✅ | ◐ press only | ✅ | ❌ | CI, Notepad |

\*Windows is partial: ref re-resolution, capture, and coordinate input are not implemented yet. Exact gates per platform: [docs/ci.md](./docs/ci.md), [docs/windows-port.md](./docs/windows-port.md), [docs/linux-port.md](./docs/linux-port.md).

## Safety

<p align="center"><img src="docs/assets/safety-gate.png" alt="Permission tier, confirmation gate, same-window recheck, execute, audit log" width="92%"></p>

- Per-app grants (`read`, `click`, `full`) in `~/.a11y-computer-use/permissions.json`. No tool can grant itself access.
- Clicks on destructive labels ask the host to confirm. No confirmation channel means the click is blocked.
- Password fields are never read, typed into, or clicked. Every action is checked against the frontmost window right before it fires.
- Everything is logged to `~/.a11y-computer-use/audit/` as JSONL, with secrets redacted.

Report security issues privately: [SECURITY.md](./SECURITY.md).

## Embed it

```python
from a11y_computer_use import safety, server

store = safety.PermissionStore()
store.set_tier("com.apple.TextEdit", safety.Tier.FULL)
rt = server.Runtime(store=store)
print(rt.desktop_snapshot("com.apple.TextEdit", mode="interactive"))
rt.click(ref="e3", verify=True)
```

Existing Anthropic or OpenAI computer-use loops can run through it unchanged via `a11y_computer_use.adapters` ([docs/provider-adapters.md](./docs/provider-adapters.md)). A reference agent loop ships as `a11y-computer-use agent --task "..."` and works with Anthropic, OpenAI-compatible endpoints (Ollama included), or the Claude Code CLI.

Long jobs across several apps run as missions: phases with their own app grants, step budgets, runner-side checks (a file exists, a URL answers, text is on screen), retries, and a wall-clock timeline for video cuts.

```bash
a11y-computer-use mission run examples/missions/agency-demo.toml --provider claude-cli
```

Format and checks: [docs/missions.md](./docs/missions.md). The example mission is the design-agency reel described in [docs/missions/agency-demo.md](./docs/missions/agency-demo.md).

## Docs

[Agent loop](./docs/agent-loop.md) · [Missions](./docs/missions.md) · [OCR refs](./docs/ocr-refs.md) · [macOS menus and dialogs](./docs/macos-primitives.md) · [Adapters](./docs/provider-adapters.md) · [Observation cost](./docs/observation-cost.md) · [Benchmark](./docs/benchmark.md) · [Browser backend](./docs/browser-backend.md) · [Linux](./docs/linux-port.md) · [Windows](./docs/windows-port.md) · [Real-desktop test bed](./docs/box-testbed.md) · [CI](./docs/ci.md) · [Decision records](./docs/decisions/) · [Changelog](./CHANGELOG.md) · [Contributing](./CONTRIBUTING.md)

Apache-2.0. Copyright 2026 Perception Dynamics, Inc.
