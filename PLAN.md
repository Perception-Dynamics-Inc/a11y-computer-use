# a11y-computer-use — Project Plan

> **Status (2026-09):** this plan is a dated design record. The front-matter below (v0.2, 2026-07-02) and the section 9 outcomes (through 2026-07-13) predate the Windows observe/act loop (later on 2026-07-13), the Linux/AT-SPI2 backend (2026-08-23) and the browser/CDP backend (2026-08-29), and have not been revised for them. For the current state see [README.md](./README.md) and [CHANGELOG.md](./CHANGELOG.md); for the newer backends see [docs/windows-port.md](./docs/windows-port.md), [docs/linux-port.md](./docs/linux-port.md) and [docs/browser-backend.md](./docs/browser-backend.md). Where this file says "~12 tools" (section 8), the server registers 16, plus `console` and `network` on the browser backend; where it calls Windows a "mapped skeleton" (section 6), `drivers/windows.py` now implements snapshot, press, scroll-into-view, set_value, typing and key chords, with its remaining 15 methods still raising `NotImplementedError`.

**Status:** v0.2 — revised after adversarial review (3 independent critiques) · **Date:** 2026-07-02
**Vision (from README):** an open-source, **embeddable computer-use SDK for AI-platform builders** on macOS & Windows — integrate native desktop control into your product instead of building your own. The host app owns the identity (signing, entitlements, OS permissions); a11y-computer-use is the layer, not the end-user product.

All landscape facts were pulled from the GitHub API and official vendor docs on 2026-07-02 and independently fact-checked. Sources linked inline.

---

## 1. TL;DR

The world does not need another screenshot-loop browser agent — that market is won ([browser-use](https://github.com/browser-use/browser-use) ~102k stars, [Playwright MCP](https://github.com/microsoft/playwright-mcp) ~34.6k, [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) ~45k). What nobody owns is **native desktop control**: the marquee open-source desktop projects died or pivoted in the last 12 months, and the accessibility-tree-first desktop projects are tiny and fragmented — the macOS niche is effectively vacant.

**The wedge (one claim, with receipts):** *native Mac control for any LLM via the accessibility tree — exact clickable element refs instead of guessed pixel coordinates, so fewer misclicks, cheap non-vision/local models work, and every action is deterministic and auditable; benchmarked head-to-head in public.* Everything else (safety layer, Windows, adapters) supports that claim; none of it replaces it. (Phase-0 measurement, COM-6: pruned snapshots run ~680–1,460 tokens — about the same as one screenshot, not 10× less — so the wedge is reliability + any-model, **not** token savings.)

**The shape:** macOS-first MVP shipped as an **embeddable library + MCP server + CLI** (integrators embed it and sign *their own* app — we ship no certificate), then Windows as the second launch beat. A11y-tree-first, vision/pixel fallback. Apache-2.0, rug-pull-proof governance.

**Two things to do before writing more code:** (1) validate demand with real failed-automation stories (§5) — ✅ done (COM-1); (2) ~~pick a distinct name~~ — **decided (COM-3, 2026-07-12): keep "a11y-computer-use"** (owner's call); mitigate the un-Googleable/branding-collision risk with a distinct SEO tagline (§10).

---

## 2. Landscape: what already exists (verified 2026-07-02)

### The winners (browser)

| Project | Stars | What it is | License |
|---|---|---|---|
| [browser-use](https://github.com/browser-use/browser-use) | ~102k | De-facto standard browser agent. Hybrid DOM + screenshots, model-agnostic, Python. $17M seed, cloud product. | MIT |
| [Chrome DevTools MCP](https://github.com/ChromeDevTools/chrome-devtools-mcp) | ~45k | Official Google MCP server driving real Chrome via CDP. Exploded since Sept 2025. | Apache-2.0 |
| [vercel-labs/agent-browser](https://github.com/vercel-labs/agent-browser) | ~38k | Browser-automation CLI for agents. | Apache-2.0 |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | ~34.6k | Official Microsoft MCP server; accessibility-tree snapshots with element refs, no pixels by default. 68 tools. | Apache-2.0 |
| [Stagehand](https://github.com/browserbase/stagehand) | ~23.3k | `act()`/`extract()`/`observe()` on Playwright; gradient from deterministic code to full agency. | MIT |
| [Skyvern](https://github.com/Skyvern-AI/skyvern) | ~22.1k | Vision+DOM RPA-style browser workflows. | AGPL-3.0 |

### Desktop (the contested, half-abandoned space)

| Project | Stars | Status |
|---|---|---|
| [UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop) | ~37.5k | Active, but **pivoted** to a general "multimodal AI agent stack" (Agent TARS); vision/pixel-based; top-requested features (Ollama, Linux) unshipped |
| [trycua/cua](https://github.com/trycua/cua) | ~19.3k | Active, YC-backed — but **VM-first** (sandboxed desktops via Lume/cloud), now repositioned around "Cua Drivers"; not host-native control |
| [Agent S](https://github.com/simular-ai/Agent-S) | ~12k | Research cadence; screenshots + pixel actions; Agent S3 (Dec 2025, best-of-N) beat the ~72% human baseline on OSWorld |
| [Bytebot](https://github.com/bytebot-ai/bytebot) | ~11.1k | **Archived Mar 2026** (company went closed-source cloud) |
| [self-operating-computer](https://github.com/OthersideAI/self-operating-computer) | ~10.2k | **Dormant** since Sept 2025 |
| [microsoft/UFO](https://github.com/microsoft/UFO) | ~9.2k | Active Microsoft research framework for Windows (UIA-based); research-oriented, not a pluggable library |
| Open Interpreter | ~64k | **Pivoted** entirely away from computer use (now "a lightweight coding agent for open models") |

### Native a11y-first desktop control — the actual gap

| Project | Stars | Notes |
|---|---|---|
| [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) | ~6.3k | Windows UIA-based MCP server, 0.2–0.5 s/action. The largest in this niche — still 16× smaller than browser-use. |
| [Terminator](https://github.com/mediar-ai/terminator) | ~1.5k | "Playwright for Windows": Rust core, UIA selectors not pixels, Python/TS bindings + MCP. Validates the architecture; small mindshare; weak on macOS. |
| [MacOS-MCP](https://github.com/CursorTouch/MacOS-MCP) | ~0.1k | macOS sibling of Windows-MCP. Barely started. |
| [macOS26/Agent](https://github.com/macOS26/Agent), mac-use, etc. | <1k | Fragmented cluster of young macOS AX-tree projects; none has escaped hobby scale. |

**Sober read:** Windows already has incumbents (Windows-MCP, Terminator, UFO — and Microsoft's MXC/Agent Workspace may commoditize that layer). The genuinely vacant, ownable niche is **accessibility-first macOS control**. That's where we launch.

### Also in the ecosystem

- **Grounding models** (screenshot → coordinates): [OmniParser](https://github.com/microsoft/OmniParser) (~25k, maintenance mode), [UI-TARS](https://github.com/bytedance/UI-TARS) (open weights: UI-TARS-1.5-**7B only**), [OpenCUA](https://github.com/xlang-ai/OpenCUA), Qwen3-VL, UI-Venus, Holo3 — a fully local, $0-inference stack is now genuinely viable.
- **Sandboxes**: Anthropic's [computer-use-demo](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo) Docker template, [E2B Desktop](https://github.com/e2b-dev/desktop) (Firecracker microVMs, powers Manus), cua's Lume (macOS VMs; Apple licensing caps at 2 VMs/host), Scrapybara (company pivoted; product maintained).
- **Benchmarks**: [OSWorld-Verified](https://xlang.ai/blog/osworld-verified) is the standard (369 Ubuntu tasks; frontier models ~73–85%, mostly provider-self-reported); [ScreenSpot-Pro](https://arxiv.org/abs/2504.07981) shows desktop grounding is still weak (~40–46% SOTA).

### Lessons from the graveyard

Three of seven marquee desktop OSS projects exited open source within ~12 months (Bytebot archived, self-operating-computer dormant, Open Interpreter pivoted), typically when the backing company moved to a paid cloud. The community has noticed: **license + governance credibility ("won't rug-pull") is now a real selection criterion.** Loudest unmet demands in issue trackers: local/Ollama model support (top-reacted issues on UI-TARS-desktop: 15 and 8 reactions), Linux support (14), and install/plumbing reliability. A cautionary counter-read the plan takes seriously: the graveyard may also mean demand in this niche is thinner than it looks — which is why §5 (demand validation) comes before code.

---

## 3. How computer use actually works

### The loop

Every computer-use system — Anthropic's, OpenAI's, Google's, and all OSS clones — is the same client-side loop:

```
┌─> capture screenshot ──> send to vision LLM with tool definitions
│                              │
│                              v
│   execute action  <── model emits action JSON:
│   on real machine     {action: "left_click", coordinate: [512, 663]}
│                              │
└── new screenshot ────────────┘   repeat until model stops calling tools
```

The **model** only emits structured actions — it never touches the machine. The **client** (your framework) does everything else: execute the action, capture the screen, feed it back. Anthropic's docs are explicit: *"Your application must explicitly run the computer use tool; Claude cannot run it directly."*

### The provider contracts (what an executor must implement)

- **Anthropic** — [`computer_20251124`](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) (beta header `computer-use-2025-11-24`; Claude Sonnet 5 / Opus 4.8–4.5): schema-less tool baked into the model. Actions: `screenshot`, `left_click`, `type`, `key`, `mouse_move`, `scroll`, `left_click_drag`, `right/middle/double/triple_click`, `left_mouse_down/up`, `hold_key`, `wait`, plus `zoom` (full-res region crop — solves tiny-text illegibility). Coordinates are pixel positions in the screenshot you sent; `display_width_px`/`height_px` must match the image exactly or clicks land offset. Oversized screenshots are rejected (Sonnet 5 / Opus 4.8: 2576 px long edge; older: 1568 px) — you downscale and rescale coordinates (Retina = 2×). Anthropic pairs it with `bash` and `text_editor` tools so the model uses the GUI only when a GUI is required.
- **OpenAI** — Responses API [`computer` tool](https://developers.openai.com/api/docs/guides/tools-computer-use) (gpt-5.x; `computer-use-preview` is legacy): `computer_call` items in, `computer_call_output` + screenshot back. Notable protocol difference: **`pending_safety_checks`** (e.g. `malicious_instructions` detected on screen) must be explicitly acknowledged by a human before the loop continues.
- **Cost/latency reality**: each screenshot ≈ 1,000–1,800 tokens; each action = a full model round-trip (real tasks: 20–35 steps, tens of minutes, spiky $0.05–$1+ per task). This is why structured/a11y approaches that skip screenshots win on cost — and why cost governance belongs in the loop.

### The three grounding approaches

1. **Pure vision** (Claude, OpenAI CUA, UI-TARS): model regresses (x, y) from pixels. Works on *anything* (Citrix, games, custom canvas), but coordinate regression is the dominant error source — desktop grounding SOTA on ScreenSpot-Pro is still only ~46%.
2. **Accessibility tree / DOM** (Playwright MCP, Windows-MCP, Terminator): serialize the UI tree, model targets element refs (`click e14`). Deterministic, cheap, works with non-vision models — but only where a faithful tree exists, and *desktop* trees are larger and messier than browser snapshots (see §6: pruning engine).
3. **Hybrid / set-of-marks** (browser-use, OmniParser): detect elements, overlay numbered boxes, model picks a number. The hybrids are winning on reliability in the browser world.

**The design conclusion:** structured tree first, vision fallback for what the tree can't see — with the fallback treated as core engineering, not an afterthought, because the apps people most want automated (Electron apps) are exactly where trees are worst.

### Execution layers per OS

| OS | Input | Capture | UI tree |
|---|---|---|---|
| macOS | `CGEvent`/Quartz | ScreenCaptureKit | `AXUIElement`. Requires TCC grants: Accessibility + Screen Recording (re-confirmed monthly on Sequoia+); grants attach to the signed, *responsible* process. Retina 2× scaling. |
| Windows | `SendInput` | DXGI duplication (+ WGC fallback) | UI Automation (UIA). UIPI silently drops input into elevated windows; UAC prompts live on the uncapturable Secure Desktop. |
| Browsers | CDP / Playwright | CDP | DOM + AX tree |

---

## 4. Positioning & wedge

**Launch claim (falsifiable, benchmarked):** *"Native Mac control for any LLM — exact element refs instead of pixel guessing: higher completion rates, fewer misclicks, and it works with cheap non-vision and local models. Here are the head-to-head numbers."* The launch asset is a published benchmark: the same N real native-app tasks run by (a) the Anthropic pixel-loop reference and (b) this framework — completion rate, misclicks/retries, steps, dollars, wall-clock (tokens reported honestly: per-step context is comparable to a screenshot, ~0.7–1.5k; the dollar win comes from cheaper non-vision models and fewer retries, not smaller prompts). That table is the README header, the blog post, and the Show HN.

*Phase-0 measured reality (COM-6, 2026-07-12):* real pruned window snapshots on the hero apps came in at ~680–1,460 tokens (Calendar's month grid: ~3,220 — dense grids need per-widget pruning tuning in Phase 1). One screenshot costs ~1,100–1,600 tokens. So "fraction of the token cost" is dead as the headline; refs-not-coordinates, any-model, deterministic+auditable is the wedge.

**Who it's for:** teams **building AI platforms** who need native desktop control as a *capability in their product* — they embed a11y-computer-use (their app, their signing identity, their branding) instead of building and maintaining a computer-use stack themselves. We are the layer, not the end-user product — infrastructure, in the Stripe/Twilio sense.

**Why us and not the built-in?** (the question every platform builder will ask): Claude Desktop's computer use is Anthropic-only, closed, **app-not-library**, and un-scriptable — you can't embed it in *your* product. We are **model-agnostic** (any MCP host, any provider loop, local models via Ollama), **embeddable** (in-process library or MCP subprocess, any language; the host owns signing/permissions), and **scriptable/auditable** (trajectory logs, deterministic refs). Same answer applies to Microsoft's MXC/Agent Workspace on Windows later.

Supporting differentiators (in service of the wedge, not co-equal to it):

1. **Local/open-weight models as a first-class citizen** — a11y refs work with cheap non-vision models; planner/grounder split for when vision is needed. The loudest unmet community demand and the r/LocalLLaMA launch hook.
2. **Pluggable everywhere** — MCP server + CLI in one binary first; SDK and provider executor adapters (Anthropic `computer_20251124`, OpenAI `computer`) after traction.
3. **Safety as disciplined hygiene, not the headline** — per-app allow/deny, read/click/full tiers, always-on audit log, secure-field human-handoff. One great docs page. (Adversarial review verdict: individual OSS adopters choose "works in 60 seconds," enterprises don't adopt 2-person repos — so safety is table stakes done well, not the wedge. It must never make the quickstart slower: sane defaults, one prompt.)

What we deliberately do **not** build: another browser agent (point users at Playwright MCP/browser-use), VM/sandbox infrastructure (integrate E2B/cua/Docker instead), our own foundation model, or an eval-harness-in-CI product (cut until users ask twice).

**README honesty:** "one of the first open-source ComputerUse repos" isn't accurate in 2026 — and doesn't need to be. "The accessibility-first computer-use framework for macOS (Windows next)" is accurate and stronger.

---

## 5. Validate demand before code (Week 0)

The gap analysis proves the niche is *empty*, not that it's *wanted* — and the graveyard cuts both ways. Before the spike:

- Collect **20 concrete "I tried to automate X native app and failed" stories** from UI-TARS-desktop/cua/Windows-MCP issue trackers, Discord servers, r/LocalLLaMA, HN threads.
- Pick the **top 3 recurring workflows**; they replace synthetic tasks as the Phase 0/1 exit criteria and become the hero demo and benchmark tasks.
- If 20 stories can't be found, that is the answer — reshape or kill the project cheaply now.
- Recruit 10–20 of these people as the beta cohort who run the "5-minute stranger test" *before* launch.

---

## 6. Architecture

```
┌────────────────────────────────────────────────────────────┐
│                     Consumers                              │
│  Claude Code/Desktop · Cursor · LangGraph · CrewAI ·       │
│  Vercel AI SDK · your own agent loop                       │
└──────┬──────────────┬───────────────┬─────────────────────┘
       │ MCP          │ CLI (same     │ later: SDK + executor
       │              │  binary)      │ adapters (Anthropic/OpenAI)
       v              v               v
┌────────────────────────────────────────────────────────────┐
│  Adapter layer                                             │
├────────────────────────────────────────────────────────────┤
│  Safety layer (wraps EVERY action)                         │
│  per-app grants · tiers (read/click/full) · confirmation   │
│  gates · secure-field handoff · always-on JSONL audit log  │
├────────────────────────────────────────────────────────────┤
│  Core: one canonical action/observation schema             │
│  + tree pruning engine + ref lifecycle + budgets           │
├────────────────────────────────────────────────────────────┤
│  Supervised driver workers (timeout-killable)              │
│  macOS: AXUIElement · CGEvent · ScreenCaptureKit (Swift    │
│  shim) — Phase 1.   Windows: UIA · SendInput · DXGI — Ph 2 │
└────────────────────────────────────────────────────────────┘
```

### Canonical schema (the thing adapters can't paper over later)

Observation:
- `observe(scope=display|app|window|element)` → pruned, indexed a11y tree + screenshot + display metadata. **Every coordinate is display-qualified physical pixels** (mixed Retina/1× and, later, Windows negative virtual-desktop coordinates make a single global space a bug factory).
- `zoom(region)` full-res crop; first-class states for `screen_locked`, `secure_desktop`, `drm_region`.

Actions:
- `act(click|double|right|drag|scroll, ref|coordinate)` — element refs first, coordinates always available. Scroll spec'd as element-targeted with line/pixel deltas.
- `type(text)` — three-path spec: clipboard-paste fast path for long text, Unicode-event path for short text, layout-aware keycode resolution (UCKeyTranslate / MapVirtualKeyEx) for chords. IME/dead-key composition acknowledged as out of scope for naive per-char injection.
- `key(chord)`, `wait_for(ref|condition, timeout)` — the actionability primitive Playwright proved is the single biggest reliability lever.
- Window verbs: enumerate/raise/move/resize/minimize, spaces/virtual desktops. Clipboard read/write. App verbs: list/launch/focus/quit.

### Contracts that must be specified in Phase 0 (from the technical review)

- **Ref lifecycle:** macOS `AXUIElementRef`s are live objects with no stable serializable ID; trees mutate between observe() and act() (an agent round-trip is seconds). Refs are **snapshot-scoped**; `act()` re-resolves via anchored attributes (role, title, path, bounds proximity) and returns a structured `stale_ref` error prompting re-observe. Steal Playwright MCP's snapshot-epoch design; test on Electron apps that rebuild trees on route change.
- **Tree pruning engine is a named core deliverable.** The "~200–400 tokens/snapshot" figure is Playwright's *filtered browser* number; a raw desktop tree (window + menu bar + system chrome) is thousands of nodes. Nobody ships a reusable desktop pruning heuristic — building one *is* the product. Budget target: filtered snapshot ≤ ~1k tokens on the 10 test apps, measured in Phase 0.
- **Hostile-app reality:** Electron apps expose near-empty AX trees until `AXManualAccessibility` is set (with real renderer perf cost — measure it); virtualized lists (Excel grids, Slack channel lists) don't materialize offscreen items; Java needs Access Bridge; Qt mislabels roles. The tree→vision fallback handoff and per-app policy is an explicit Phase 1 deliverable with its own exit criterion — reliability reputation is won on Slack and VS Code, not Finder.
- **Supervised workers:** AX calls on a hung app block forever unless `AXUIElementSetMessagingTimeout` is set; UIA is COM with no per-call timeout (MTA client, deadlock-prone event callbacks; use CacheRequest batching). Drivers run as timeout-killable supervised workers so one frozen app can't take down the server mid-task.
- **Windows integrity semantics (Phase 2):** detect target-window integrity level before `act()` and return a structured `elevation_blocked` error instead of UIPI's silent no-op; locked-workstation/RDP/secure-desktop detection in `observe()`; games/anti-cheat declared out of scope (synthetic input carries `LLHF_INJECTED`).
- **Permission enforcement mechanism:** frontmost-window hit-test (CGWindowList / WindowFromPoint) at `act()` time with a same-window recheck between decision and injection (toasts and overlays can race the click).
- **Secure input fields:** detect `IsSecureEventInputEnabled` / password AX roles → structured `secure_field` state → safety layer converts to human-handoff gate. (Password fields blind event taps anyway; turning that into a feature is a safety story competitors lack.)

### Language: stay Python for Phase 1 (COM-11 decision, 2026-07-13)

**Revised by Phase-0 evidence.** The MVP is Python + PyObjC and is live-verified end-to-end, capture included (`CGWindowListCreateImage` + `screencapture` fallback — **ScreenCaptureKit not needed**). Every "hard" primitive bridged through PyObjC without fighting the FFI, so the pre-committed Rust-core + Swift-shim rewrite is **deferred, not adopted**: harden the Python core for Phase 1, ship a signed binary via `py2app`/PyInstaller (COM-8), and keep the accessor/schema seams clean so a compiled core is a later *incremental* port, not a rewrite. If a real need appears (measured perf bottleneck, zero-dependency distribution, or `CGWindowListCreateImage` removal + streaming capture), *then* draw the boundary — **Rust** for schema/pruning/safety/MCP (+ the Phase-2 Windows/UIA driver via `windows-rs`), a **Swift static lib** for ScreenCaptureKit's async-Swift-first API — and re-run the 3-day-per-primitive kill criterion against the Python reference. Full analysis: [docs/language-boundary.md](./docs/language-boundary.md).

### Platform seam: one `Driver` protocol per OS (cross-platform by construction)

**Shipped 2026-07-13.** Everything OS-specific — walking the a11y tree, synthesizing input, capturing pixels, enumerating windows — lives behind the `Driver` protocol (`a11y_computer_use/drivers/`). The schema, pruning engine, safety layer, and MCP server are platform-free and **shared across OSes**. macOS is implemented (`AXUIElement`/`CGEvent`/Quartz, live-verified through the seam); Windows is a **mapped skeleton** (`IUIAutomation`/`SendInput`/DXGI, unverified) — every method names the native API it will use. `get_driver()` selects by OS and the Runtime routes every platform op through it, so **adding an OS is "implement `Driver`", never "touch the core".** Port guide: [docs/windows-port.md](./docs/windows-port.md).

---

## 7. Distribution & trust (launch blockers, not polish)

**Audience reframe (2026-07-13): a11y-computer-use is an embeddable SDK for AI-platform builders, not an end-user app.** We don't ship a signed consumer binary — integrators embed us in *their* app and sign it with *their* Developer ID. That flips the signing burden off us and onto a party who already has it, and it's the correct model (Playwright/browser-use don't ship their own signing identity either). Our job is to be cleanly embeddable and to document the host's checklist.

- **macOS TCC reality — inherited from the host.** Accessibility + Screen Recording grants key off code-signing identity and are attributed to the *responsible process* = **the integrator's app**. a11y-computer-use runs under that identity (in-process, or as a child the host spawns) and inherits its grants; **we need no certificate of our own**. The integrator: signs with their Developer ID, requests the permissions, and — for the subprocess model — ensures TCC responsibility resolves to their signed app (embed in-process, or ship the helper signed with their Team ID at a stable path). Hardened-runtime **library validation** is the host's to satisfy (embed in-process / sign bundled components with their Team ID / `disable-library-validation`). `doctor` detects, from inside the host, which app holds the grant and what's missing.
- **Windows (Phase 2) — same inheritance.** No TCC, but Authenticode/SmartScreen reputation and UIPI/elevation are governed by the **host app's** signature and integrity level; the integrator signs with their EV cert. We ship no Windows certificate. (An unsigned input-injecting/screen-reading binary is a textbook malware signature — which is exactly why the identity must be the integrator's trusted app, not ours.)
- **Distribution is developer-first:** PyPI/`uvx` for Python hosts (in-process import or `a11y-computer-use mcp` subprocess), then language bindings / a C-ABI core so non-Python hosts (Swift, Electron, Go, Rust agents) embed in-process cleanly. No consumer `.app`, no our-side notarization.
- **Integration docs are a launch deliverable** — the host checklist (sign, entitle, request TCC, library validation, responsible-process for the subprocess model), with `doctor` as the in-process verifier. This replaces the old "signed helper we ship" plan.

## 8. MCP tool surface v1 (~12 tools — the front door, designed not implied)

`desktop_snapshot` (pruned tree + refs, scoped) · `screenshot` · `zoom` · `click` (ref|coord, button, modifiers) · `type` · `key` · `scroll` · `drag` · `wait_for` · `app` (list/launch/focus) · `window` (list/raise/move/resize) · `clipboard` (read/write). Confirmation-gate UX per host prototyped in Phase 0 (at minimum: Claude Code elicitation flow; tray-app question resolved then).

---

## 9. Roadmap (revised for a 1–2 person team)

### Phase 0 — Demand + hostile-app spike (2–3 weeks)
- §5 demand validation (20 stories → top-3 workflows).
- macOS spike against **hostile apps**: Slack, VS Code, Discord, Chrome, one Java app — AX coverage with/without `AXManualAccessibility` (+ perf cost), a virtualized list, pruned-snapshot token counts per app, ref re-resolution on tree rebuilds.
- Signing + notarization pipeline; TCC attribution tested from Terminal, Claude Desktop, and an IDE.
- Throwaway MCP server (~8 tools) driven from Claude Code; confirmation-gate UX prototyped.
- **Exit criteria (numeric):** per-app-category coverage table published; filtered snapshots ≤ ~1k tokens on ≥7/10 apps; one top-3 workflow completed end-to-end via refs alone; Rust-vs-Swift-shim boundary decided by the 3-day kill criterion. **If a11y coverage on Electron apps is unsalvageable even with fallback, rethink before Phase 1.**
- **✅ OUTCOME (COM-12, 2026-07-13): GO to Phase 1.** Hero workflow live via refs alone (Calendar); coverage table published (native/Chromium excellent; custom-drawn Telegram = zero a11y → **vision fallback proven**); language decided (Python); confirmation gate prototyped. Partials carried to Phase 1: dense-grid token budget (Calendar ~3,220 tok needs per-widget pruning) and signing (needs cert). Snapshot tokens ~680–1,460 (≈ one screenshot, **not** 10× less). Full memo + measured numbers + coverage table: [docs/phase-0-review.md](./docs/phase-0-review.md).

### Phase 1 — macOS-only MVP + launch (8–12 weeks, honest estimate)
- macOS driver hardened: observe/act/window/clipboard/wait_for, tree pruning engine, vision fallback handoff on Electron (own exit criterion), supervised worker isolation.
- One binary: MCP server + CLI (`mcp`, `doctor`, `snapshot`, `run-once`). Python SDK only if it doesn't slip the date.
- Safety v1 (slim): per-app allow/deny, read/click/full tiers, always-on JSONL audit log, secure-field handoff. Blocklists/injection heuristics → Phase 2.
- **Launch assets are exit criteria:** hero demo gif (split-screen: pixel loop vs us, live token/cost/step counter, on a real §5 workflow), benchmark blog post with reproducible numbers, README comparison table (§2 published), 10–20 beta users pass the 5-minute stranger test (including the guided TCC step).

### Phase 2 — Windows + adapters + local models (the second launch beat)
- Windows driver (UIA, integrity-level semantics, signing/AV workstream) — headline: "now the same API on Windows."
- Anthropic `computer_20251124` and OpenAI `computer` executor adapters (contract tests against recorded fixtures).
- Planner/grounder split with Ollama-served open grounding models; per-task budgets + loop detection.
- Framework shims where thin (LangChain tool, Vercel AI SDK example, CrewAI tool).

### Phase 3 — Earned, not promised
- **Teach & replay** (record demonstrations → deterministic replay → agent fallback on drift): trajectory format from day one is designed so audit logs double as demonstrations, making this cheap when demand justifies it.
- Eval harness only if users ask twice. Linux only with a committed co-maintainer.

## 10. Launch plan

1. **Name — decided: keep "a11y-computer-use"** (COM-3, 2026-07-12, owner's call). The known downside stands (un-Googleable; collides with Anthropic's `computer-use` MCP; reads as a fan clone), so the mitigation is mandatory: **lead every public surface with a distinct tagline** — "a11y-computer-use — the accessibility-first computer-use framework for macOS" — so search and disambiguation ride on the tagline, not the bare name. (A vetted rename shortlist — axreach / axweave / axwright / axgrove / treewright, all registry-free — is parked on COM-3 if we ever reconsider.)
2. Benchmark blog post → **Show HN** ("Show HN: X — give any LLM native control of your Mac via the accessibility tree, with head-to-head token numbers") → **r/LocalLLaMA** post leading with Ollama/local-model support.
3. Submit to every MCP registry the week of launch (registry.modelcontextprotocol.io, Smithery, PulseMCP, mcp.so, Cursor/Cline directories) — free, high-intent distribution.
4. `examples/` gallery with 5 copy-paste recipes against apps people feel (Mail, Spotify, Slack, System Settings, Finder).
5. README: hero gif at top, comparison table ("why not Windows-MCP / Terminator / cua / UI-TARS / Claude Desktop built-in?") — the first HN comment will ask exactly this; answer it preemptively.

## 11. License, governance & sustainability

- **Apache-2.0** (patent grant matters; avoid AGPL — it verifiably suppresses adoption here). ✅ **DONE (COM-4, 2026-07-12): the repo LICENSE is now Apache-2.0** (+ NOTICE, PyPI classifier), switched while the cost was still near-zero — relicensing later would have invited exactly the suspicion we're preempting.
- Governance note at launch: no CLA-to-relicense trap, roadmap in the open, signed releases.
- **Sustainability honesty beats a governance promise** (the graveyard projects all had implicit promises too): state the funding reality — e.g. GitHub Sponsors + a clearly-scoped future hosted service that never gates the core, or an explicit "this is a 12-month bet; here's the fork-friendly exit (permissive license, no proprietary deps, documented release process)."

## 12. Risks

| Risk | Mitigation |
|---|---|
| The gap is a graveyard, not a market — demand may be thin | §5 demand validation gates everything; hero workflows come from real failed-automation stories, not synthesis |
| A11y trees are poor exactly where users want automation (Electron) | Vision fallback is a Phase 1 deliverable with its own exit criterion; Phase 0 measures hostile apps first; per-app policy engine |
| Token-cost claim doesn't survive desktop tree sizes | **RESOLVED (Phase 0, COM-6): it doesn't.** Snapshots ≈ one screenshot (~0.7–1.5k tok; dense grids worse). Wedge re-framed to refs-not-coordinates / any-model / deterministic+auditable; §4 updated; benchmark still published, honest token column included |
| TCC/signing friction kills the quickstart | Signed stable-path helper + IPC design; notarization CI from Phase 0; `doctor` names the responsible host app |
| Scope explosion for a 1–2 person team | macOS-only Phase 1; Windows/adapters/SDK gated on traction; anti-goals enforced |
| Provider contract churn (twice in 18 months) | Adapter layer is the only thing that changes; contract tests on recorded fixtures |
| Prompt injection unsolved (vendors: possibly permanent) | Tiers + gates + secure-field handoff; claim "governed," never "safe" |
| Sherlocking (Claude Desktop, MXC/Agent Workspace) | Stay the model-agnostic, embeddable, scriptable layer — the built-ins are closed products; publish the "why not the built-in?" answer |
| Windows distribution = malware-pattern flags | Phase 2 signing/AV workstream, budgeted before the Windows launch |

## 13. Key sources

- [Anthropic computer use tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) · [reference implementation](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo) · [best-practices post](https://claude.com/blog/best-practices-for-computer-and-browser-use-with-claude) (incl. Teach Mode) · [prompt-injection defenses](https://www.anthropic.com/research/prompt-injection-defenses)
- [OpenAI computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use) · [Operator system card](https://openai.com/index/operator-system-card/)
- [OSWorld-Verified](https://xlang.ai/blog/osworld-verified) · [ScreenSpot-Pro](https://arxiv.org/abs/2504.07981) · [UI-TARS](https://arxiv.org/abs/2501.12326) / [UI-TARS-2](https://arxiv.org/abs/2509.02544)
- [Claude Desktop computer use docs](https://code.claude.com/docs/en/desktop) (permission-tier model) · [Windows agentic security](https://learn.microsoft.com/en-us/windows/security/book/operating-system-agentic-security) · [MXC](https://blogs.windows.com/windowsdeveloper/2026/06/02/windows-platform-security-for-ai-agents/)
- Architecture prior art: [Terminator](https://github.com/mediar-ai/terminator) (Rust+UIA+bindings+MCP) · [Playwright MCP snapshot design](https://playwright.dev/mcp/snapshots) · [trycua/cua](https://github.com/trycua/cua) (adapter/model-routing patterns) · [browser-use/workflow-use](https://github.com/browser-use/workflow-use) (record-replay pattern)
