# Confirmation-gate UX — prototype + decision (COM-10)

**Status:** Phase-0 spike, prototyped and tested. **Date:** 2026-07-12.

## The question

The tier system (`read`/`click`/`full`) answers *"is this app allowed to receive input?"* It does **not** answer the orthogonal question: *"this specific click looks irreversible (Delete, Move to Trash, Erase…) — should a human explicitly okay it first?"* And when the MCP server runs headlessly inside a host, **who renders that prompt?** (docs/decisions/plan-2026-07.md §8.) This is load-bearing for the safety story, so Phase 0 settles it with a working prototype rather than a paragraph.

## Decision

**1. The confirmation channel is MCP elicitation — no separate tray/menubar app for the MVP.**

When an action is flagged as plausibly irreversible, the server issues an [MCP elicitation](https://modelcontextprotocol.io) request (`ctx.elicit`) and the **host** (Claude Code, Claude Desktop, any elicitation-capable client) renders the yes/no. The prompt appears in the surface the user is already looking at; there is no second always-running process to sign, notarize, keep alive, or wire over IPC.

**2. Fail-safe when there is no channel.** If nothing can render the prompt (an MCP client that doesn't advertise elicitation, or an in-process `Runtime.click(...)` call with no `confirm` callback), the irreversible action is **blocked** with a structured `confirmation_declined` error, never fired unconfirmed. The CLI `run-once` one-shot never reaches the gate: it accepts only x/y coordinate targets, and coordinate clicks carry no label for the classifier. Escape hatch: `A11Y_COMPUTER_USE_CONFIRM=0` disables the gate wholesale for automation that has accepted the risk.

**3. Revisit a tray/menubar helper only when earned** — specifically if we ship a non-MCP embedding (direct SDK use in someone's own agent loop) that has no host to render prompts, or if real hosts turn out to render elicitation poorly. Until then a tray app is cost (a second signed process, its own TCC surface, cross-host inconsistency) without a matching benefit for a 1–2 person team.

### Why not a tray app now (the rejected alternative)

| | MCP elicitation | Dedicated tray/menubar app |
|---|---|---|
| Extra process to ship/sign/notarize | none | yes (second TCC + signing surface) |
| Renders where the user is looking | ✅ in the host | ❌ separate UI, context-switch |
| Works across hosts uniformly | ✅ (spec-level) | ❌ needs per-host glue anyway |
| IPC / lifecycle complexity | none | server↔tray channel, keep-alive |
| Fits a 1–2 person MVP | ✅ | ✗ |

## How it works (prototype)

1. **Classifier.** `safety.confirmation_prompt(action, app)` returns a prompt string when a `Click` targets an element whose label matches a conservative destructive-verb set (`delete`, `move to trash`, `empty trash`, `trash`, `discard`, `erase`, `uninstall`, `permanently`, `wipe`, `don't save`, and the same phrase with the curly apostrophe U+2019 that AppKit renders), else `None`. Deliberately conservative: a miss just means "no extra prompt"; a false positive nags on a safe click. `send`/`remove`/`reset` are intentionally excluded.
2. **Gate.** `Runtime._run_gated` runs the check after the tier decision and **before** the same-window recheck (so the frontmost re-check stays closest to injection, since a slow human prompt can let focus drift). No prompt → proceed. Prompt + confirmer returns True → proceed. Prompt + declined/no-confirmer → raise `ErrorCode.CONFIRMATION_DECLINED`, audited.
3. **Transport bridge.** The MCP `click` and `act` (batched) tools build a sync confirmer that bridges the worker thread back to the event loop (`anyio.from_thread.run`) to call `ctx.elicit`. Elicitation unsupported → the confirmer returns False → fail-safe block.

## Evidence (`tests/test_server.py`, `tests/test_safety.py`)

Driven end-to-end over the real MCP in-memory transport with client elicitation callbacks:

- `test_destructive_click_proceeds_when_confirmed` — accept → the click executes.
- `test_destructive_click_blocked_when_declined` — decline → `confirmation_declined`, driver never called, audited.
- `test_destructive_click_fails_safe_without_elicitation` — no elicitation channel → blocked (fail-safe), not fired.
- `test_destructive_click_gate_can_be_disabled` — `A11Y_COMPUTER_USE_CONFIRM=0` → proceeds without a prompt.
- `test_safe_click_never_triggers_confirmation` — a "Save" click never elicits.
- `safety` unit tests cover the classifier (destructive vs safe labels, case-insensitivity, coordinate/non-click actions return `None`).

## Prototype limitations / follow-ups (Phase 1)

- **Trigger is a label heuristic**, not a taxonomy — clear-cut destructive buttons only. A real policy would also consider app-specific context (Mail "Send", Finder "Empty Trash…"), keyboard destructive shortcuts (`cmd+delete`), and an allowlist of pre-approved actions per app so confirmation isn't asked twice.
- **Click-only.** `key`/`type` paths (e.g. a destructive keyboard shortcut) are not yet classified.
- **Binary yes/no.** No "always allow this button in this app for this session" memory yet — that belongs with the Phase-1 safety-policy store.
