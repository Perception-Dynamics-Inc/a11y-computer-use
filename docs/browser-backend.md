# Browser backend — a11y-first control of Chromium over CDP

The fourth `Driver` (`computeruse/drivers/browser.py`), and the first that is not
an OS backend: it drives a **running Chromium** through the Chrome DevTools
Protocol. It is selected explicitly — `get_driver("browser")` or
`COMPUTERUSE_DRIVER=browser computeruse serve` — never by platform, so it runs the
same on macOS, Windows, and Linux.

It is **coordinate-free by construction**: observe reads the page's accessibility
tree; act goes through the DOM. No pixel, no window focus, no pointer — which is
exactly the moat thesis (cacheable/diffable context, per-target concurrency,
deterministic replay) realized on the web surface, where most agent work happens.

## How it maps onto the shared core

Everything above the `Driver` seam is reused unchanged — the same
`observe.build_snapshot` pruning/indexing/ref engine, the same safety tiers and
JSONL audit, the same MCP surface.

| Concern            | CDP mechanism                                                                 |
|--------------------|-------------------------------------------------------------------------------|
| Observe (tree)     | `Accessibility.getFullAXTree` (roles/names/state), one call                    |
| Observe (geometry) | `DOMSnapshot.captureSnapshot` — every laid-out box in **one** call, joined by `backendDOMNodeId` |
| Roles              | `_cdp_ax._ROLE` maps ARIA/computed roles onto the canonical `AX*` vocabulary the pruner keys off; interactive roles get a synthetic `AXPress` |
| Secure fields      | `<input type=password>` detected from the DOMSnapshot attributes → `AXSecureTextField` (never typed into) |
| Activate / focus   | `DOM.resolveNode` → `Runtime.callFunctionOn` `this.click()` / `this.focus()` (a11y-first) |
| Set value          | native value setter + `input`/`change` events (React/Vue controlled inputs see it) |
| Type               | `Input.insertText` into the focused element (deterministic, not per-key)        |
| Key chords         | `Input.dispatchKeyEvent` with a CDP modifier bitmask (`_key_events`)            |
| Pixel fallback     | `Input.dispatchMouseEvent` (mouse/wheel) — the vision path, viewport-mapped     |
| Capture            | `Page.captureScreenshot` (+ `clip` for zoom), `captureBeyondViewport`           |
| Navigate           | `launch_app(url)` = `Page.navigate` + wait for `document.readyState=="complete"` (load-aware, no fixed sleep) — the browser analog of launching an app, so the existing `app` tool drives it with no new MCP surface |
| Iframes            | child frames stitched in: `getFrameTree` → per-frame `getFullAXTree` grafted under the owner `Iframe` node, geometry offset into the top document (same-process frames; cross-origin OOPIF skipped, never fatal) |
| Tabs               | page targets are modelled as apps/windows (`running_apps`/`windows`/`activate_app`) |

`stable_id` is the backend DOM node id — stable across snapshots within a page —
so `observe._match_anchor` re-resolves a ref deterministically after the DOM
shifts.

## Transport seam and testing

All wire I/O sits behind `_cdp.Transport`, so the driver logic is pure and
**100% container-testable**: `tests/test_browser.py` drives observe and act with a
scripted fake transport (no browser). One opt-in live test exercises headless
Chromium end to end and is skipped unless a CDP endpoint is reachable.

## Running it

```bash
# 1. start Chromium with a debugging port (headless=new works in CI)
google-chrome --headless=new --remote-debugging-port=9222 about:blank &

# 2. point computerUse at it
pip install computeruse[browser]          # adds websocket-client (only extra dep)
COMPUTERUSE_DRIVER=browser \
COMPUTERUSE_CDP_ENDPOINT=http://127.0.0.1:9222 \
computeruse serve
```

The endpoint defaults to `http://127.0.0.1:9222`. Target discovery uses stdlib
`urllib`; only the WebSocket needs `websocket-client`. The handshake sends no
`Origin` header (`suppress_origin`), so Chrome accepts it with **no**
`--remote-allow-origins` flag.

## Live-verified

The observe→act→verify loop is proven against real headless Chrome: a
coordinate-free `press_element` on a button fires its `onclick`
(`document.title` flips), and focus + `type_text` lands text in an input — asserted
in `test_live_observe_act_verify`.

## Measuring the moat — cu-arena

`computeruse bench web <url>` (module `computeruse/arena.py`) reports the honest
per-observation token cost of the a11y-first snapshot vs the screenshot a vision
agent would send instead — both raw numbers, no rigging: the a11y cost is the
real rendered snapshot, the image cost is the real captured frame's dimensions
run through Anthropic's published `(w·h)/750` estimate. On a small live page it
reports the a11y snapshot at roughly **5–8× cheaper per observation** than the
equivalent screenshot — and, being text, it *diffs* to near-zero on re-observe
where a screenshot pays its full image cost every step. The number is reproduced
in CI (the browser job prints it). `computeruse bench audit` aggregates the
JSONL audit log (cu-meter: per-action latency p50/p95 + tokens).
