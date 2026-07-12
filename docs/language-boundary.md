# Language & the Rust/Swift boundary — decision (COM-11)

**Status:** Phase-0 spike deliverable. **Date:** 2026-07-13.
**Decision: stay Python for Phase 1. Defer the Rust core + Swift/ScreenCaptureKit shim until a measured need forces it.** Keep the accessor/schema boundary clean so a compiled core is swappable later.

---

## What COM-11 originally asked

PLAN.md §6 *pre-committed* to a hybrid: a **Rust core** (schema, safety, pruning, MCP server) plus a **small Swift static library** for ScreenCaptureKit (SCKit) and TCC-adjacent code, linked into one binary. The stated rationale: SCKit is an async Swift-first API that is "genuinely painful through `objc2`." The ticket's ACs — "SCKit capture working from the chosen boundary" and "build produces one binary" — assume that rewrite is happening.

The Phase-0 MVP is evidence that changes the question from *"where's the Rust/Swift boundary?"* to *"do we need Rust at all yet?"*

## The evidence: the Python MVP already does all of it

The MVP is Python + PyObjC and is **live-verified end-to-end** (see COM-9, COM-5): observe/act/safety/audit, the 12-tool MCP server, the CLI, the a11y hero workflow on Calendar, and the **vision fallback on Telegram** — including screen capture.

Crucially for this ticket:

- **Capture already works, and without SCKit.** `capture.py` uses `CGWindowListCreateImage` in-process, with a `/usr/sbin/screencapture -x` out-of-process fallback. It captured the full display for the Telegram vision demo with the Screen Recording grant. **ScreenCaptureKit is not used and is not needed for the current tool surface** (`screenshot`, `zoom`). SCKit only becomes necessary for *streaming* capture or if Apple removes `CGWindowListCreateImage` (deprecated on macOS 14+, but still functional, and the `screencapture` fallback is independent of it).
- Every "hard" macOS primitive the plan worried about is **already working through PyObjC**: `AXUIElement` walking with `AXUIElementSetMessagingTimeout`, `CGEvent` input, `AXUIElementPerformAction`, `NSPasteboard`, TCC probes (`AXIsProcessTrusted`, `CGPreflightScreenCaptureAccess`), display topology, and Cocoa overlay windows. None of them "fought the FFI for >3 days" — PyObjC bridges them directly.

So the kill criterion (a primitive fighting the FFI for >3 days moves to Swift) has, in effect, **already been run in Python — and nothing fought.** The pain the Rust plan anticipated (SCKit through `objc2`) is a pain we don't currently have, because we don't use SCKit and we're not in Rust.

## Re-examining the rewrite rationale

| Original reason for Rust+Swift | Holds up against the Python MVP? |
|---|---|
| **Performance** | Not yet demonstrated as a problem. The bottleneck is the *model round-trip per action* (seconds), not AX-tree walking (tens of ms). A faster core saves microseconds against a multi-second loop. Premature. |
| **Single signed binary** (§7 distribution) | Achievable in Python: `py2app`/PyInstaller → a signed, notarized `.app`/binary; or `uvx`/`pipx` for the dev audience. The single-binary *goal* does not require Rust. |
| **No Python runtime dependency** | Real, but only matters for the eventual broad-distribution build, not for the Phase-1 MVP + beta cohort (developers who have Python). |
| **SCKit is painful through objc2** | Circular — that pain only exists *because* of choosing Rust. In Python, SCKit (if ever needed) is a normal PyObjC import. |
| **Terminator validates Rust+UIA for Windows** | True and relevant **for the Windows driver (Phase 2)** — not a reason to rewrite the working macOS core now. |

## Decision & boundary

**Phase 1: stay Python.** Harden the existing package; ship a signed/notarized app (COM-8) via `py2app`/PyInstaller. This keeps iteration fast while the product surface is still moving.

**Keep the boundary swap-ready.** The architecture already isolates the platform-specific code behind seams — `observe.build_snapshot` is platform-free and drives a `TreeAccessor`; the schema is a pure data contract; `act`/`capture`/`safety` are separable. A future Rust core would reimplement the pruning/schema/MCP layers and call platform primitives; that boundary is **already drawn in the module structure**, so a later port is incremental, not a rewrite-from-zero.

**If/when a compiled core is justified** (a real perf bottleneck is measured, or broad zero-dependency distribution is prioritized, or `CGWindowListCreateImage` is removed and streaming capture is needed):
- **Rust:** schema, pruning engine, ref lifecycle, safety engine, MCP server, budgets. (And the Windows/UIA driver — `windows-rs` — where Terminator already validated it.)
- **Swift static lib (linked into the one binary):** ScreenCaptureKit capture and any TCC-adjacent / modern-async-Cocoa code that is genuinely worse through Rust FFI. This is the point where SCKit's async-Swift-first shape earns a Swift shim rather than `objc2` pain.
- Re-run the 3-day kill criterion *per primitive at that time*, with the Python implementation as the reference behavior.

## Disposition of the original ACs

- ~~"SCKit capture working from the chosen boundary"~~ → **Reframed.** SCKit isn't needed yet; capture works via `CGWindowListCreateImage` + `screencapture`. The SCKit-from-Swift spike is deferred with the rest of the Rust decision.
- "Build produces one binary" → **Carried to COM-8** (signing/notarization), achievable from Python (`py2app`/PyInstaller), no Rust required.

**Net:** the pre-committed rewrite was the right *contingency*, but the Phase-0 evidence says don't spend Phase 1 on it. Ship the working Python core, keep the seams clean, and let a measured need — not a pre-commitment — trigger the port. Feeds the COM-12 go/no-go.
