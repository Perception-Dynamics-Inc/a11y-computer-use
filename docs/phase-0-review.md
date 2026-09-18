# Phase 0 — go/no-go review (COM-12)

**Date:** 2026-07-13 · **Verdict: 🟢 GO to Phase 1** (with a sharper, evidence-revised thesis).

Phase 0's job was to kill the project cheaply if the hard parts didn't work. They work. Demand is real, the accessibility-first engine is live-proven on real apps, and the one place a11y can't reach (custom-drawn apps) is covered by a vision fallback that also works. Nothing surfaced that warrants stopping or reshaping the core bet — only sharpening it.

## Exit criteria vs. evidence

| Phase-0 exit criterion (PLAN §9) | Status | Evidence |
|---|---|---|
| ≥20 real "failed automation" stories → top-3 hero workflows | ✅ **Met** | COM-1: 30 verified stories, kill criterion PASS; heroes = Calendar / System Settings / Music |
| One hero workflow completed end-to-end **via a11y refs alone** | ✅ **Met** | COM-9: Calendar create→AX-verify→delete, self-cleaning, live, refs only |
| Per-app-category coverage table published | ✅ **Met** | See table below (native / Chromium / custom-drawn / Electron) |
| Filtered snapshot ≤ ~1k tokens on ≥7/10 apps | ⚠️ **Partial** | ~680–1,460 tok typical; dense grids blow past it (Calendar month grid ~3,220). Per-widget pruning tuning → Phase 1 |
| Rust-vs-Swift boundary decided by the 3-day kill criterion | ✅ **Met** | COM-11: stay Python (nothing fought the FFI); Rust/Swift deferred |
| Confirmation-gate UX prototyped for a host | ✅ **Met** | COM-10: MCP elicitation gate + tray-vs-CLI decision |
| Signing + notarization pipeline; TCC attribution tested | ⚠️ **Carried** | TCC attribution tested (below); signing needs an Apple Developer cert → COM-8 into Phase 1 |
| If Electron/hostile a11y is unsalvageable even with fallback, rethink | ✅ **Resolved — salvageable** | Telegram (zero a11y tree) fully driven by **vision fallback** |

Two "partials" (token budget on dense grids; signing) are engineering tasks, not viability risks. Neither is a reason to stop.

## Measured numbers

**Per-app coverage table** (live, this machine, both TCC grants active):

| App | Kind | a11y tree | Notes | Path used |
|---|---|---|---|---|
| TextEdit | native AppKit | good | text area + toolbar refs | a11y (smoke) |
| Calendar | native AppKit | **excellent** | 100+ labelled event refs | a11y (hero) |
| System Settings | native | good | full sidebar refs | a11y |
| Safari | native | good | ~680 tok snapshot | a11y |
| Chrome | Chromium | **excellent** | 199 elements, 112 clickable, editable address bar; web AX auto-enables under a trusted process | a11y |
| Telegram (keepcoder) | custom-drawn native | **none** | app exposes only 2 `AXMenuBar`s; window content invisible; `AXManualAccessibility`/`AXEnhancedUserInterface` unsupported | **vision fallback** |
| Cursor | Electron | untested | installed; `AXManualAccessibility` unlock candidate | Phase-1 spike |

**Snapshot token footprint (COM-6):** pruned window snapshots ~680–1,460 tokens on the hero apps; dense grids (Calendar month) ~3,220. A single screenshot is ~1,100–1,600 tokens — so **snapshots are comparable to one screenshot, not 10× smaller.**

**TCC findings:** Accessibility and Screen Recording both work and both attach to the **responsible process** (here, Terminal), not the Python child; each needs a **relaunch after granting** (Screen Recording stricter). `doctor` correctly detects both, names the responsible host app, and emits Settings deep links. `AXManualAccessibility` is Chromium/Electron-specific (unsupported on custom-drawn native apps like Telegram); Chrome's web AX tree auto-enables when a trusted assistive process is present.

## The thesis, revised by evidence

1. **Wedge is reliability + any-model + non-intrusiveness + auditability — NOT token savings.** Snapshots ≈ one screenshot. The real wins: exact clickable refs (no coordinate hallucination), works with cheap non-vision/local models, deterministic + fully audited, and — proven this phase — **ref actions activate via the AX API without moving the user's pointer** (COM-55), a property a pixel-loop agent structurally cannot have.
2. **The product is a hybrid, and the hybrid is real.** a11y-first where a tree exists (fast, deterministic, verifiable, cursor-free); **vision fallback** (screenshot → coordinate click) everywhere else. Both are live-proven. This makes "control any app" true today — the screenshot tools already shipped; the vision loop is just the host agent reading the PNG.
3. **Stay Python for Phase 1** (COM-11). The working MVP is the strongest evidence against a premature Rust rewrite.
4. **Governance settled:** Apache-2.0 (COM-4); name kept as "a11y-computer-use" with a tagline-led SEO strategy (COM-3).

## Confirmed / revised Phase-1 scope

**Confirmed:** harden the macOS driver (observe/act/window/clipboard/wait_for), the tree-pruning engine, supervised worker isolation, safety v1, and the launch assets (hero gif, benchmark, beta cohort) — all in **Python**.

**Revised / added by Phase-0 findings:**
- **Vision fallback promoted from "Electron handoff" to a first-class, always-available path** with an explicit **a11y→vision auto-handoff signal** (when a snapshot yields ~0 interactive elements, tell the agent to use screenshot+coordinates). Telegram proved this is the real coverage story, not just Electron.
- **Per-widget pruning tuning** to bring dense grids (Calendar) toward the token budget — a named deliverable, not an afterthought.
- **Visual presence overlay** (COM-55 follow-on) — the blue agent-cursor + screen-edge glow — is now core UX (agent must be visible precisely because it doesn't move the real cursor). Needs main-thread Cocoa integration into the server.
- **Signing/notarization from Python** (`py2app`/PyInstaller) — COM-8 carries in.
- **Deprioritize** the Rust/Swift rewrite and the SCKit spike until a measured need appears.

**Bottom line:** the risky half of the project is de-risked and live-proven. Proceed to Phase 1.
