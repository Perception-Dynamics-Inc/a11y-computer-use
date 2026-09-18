# Browser backend: accessibility-first control of Chromium over CDP

The fourth `Driver` (`a11y_computer_use/drivers/browser.py`), and the first that is not
an OS backend: it drives a **running Chromium** through the Chrome DevTools
Protocol. It is selected explicitly (`get_driver("browser")` or
`A11Y_COMPUTER_USE_DRIVER=browser a11y-computer-use mcp`), never by platform, so it runs the
same on macOS, Windows, and Linux.

The ref-based path is coordinate-free: observe reads the page's accessibility
tree (`Accessibility.getFullAXTree` plus `DOMSnapshot.captureSnapshot`);
`press_element` and `focus` call `this.click()` / `this.focus()` on the resolved
DOM node, `set_value` uses the native value setter, and typing and key chords go
through `Input.insertText` / `Input.dispatchKeyEvent`. None of that moves a
pointer or needs a focused window. Coordinate `click`, `drag`, and `scroll` also
exist as the vision fallback (`Input.dispatchMouseEvent`, viewport-mapped; see
the Pixel fallback row below); they are implemented but have no test coverage
(README feature table), and a coordinate `click` is not refused on a secure
field. Snapshot text is diffable (`desktop_snapshot(mode="diff")`) and every gated action is
written to the JSONL audit log. Not yet: replay tooling does not exist (the audit
format is kept replay-compatible, nothing more), and one driver instance binds
one page target at a time (`_bind` rebinds on tab switch); driving several tabs
concurrently is untested.

## How it maps onto the shared core

Everything above the `Driver` seam is reused unchanged: the same
`observe.build_snapshot` pruning/indexing/ref engine, the same safety tiers and
JSONL audit, the same MCP surface.

| Concern            | CDP mechanism                                                                 |
|--------------------|-------------------------------------------------------------------------------|
| Observe (tree)     | `Accessibility.getFullAXTree` (roles/names/state), one call                    |
| Observe (geometry) | `DOMSnapshot.captureSnapshot`: every laid-out box in **one** call, joined by `backendDOMNodeId` |
| Roles              | `_cdp_ax._ROLE` maps ARIA/computed roles onto the canonical `AX*` vocabulary the pruner keys off; interactive roles get a synthetic `AXPress` |
| Secure fields      | `<input type=password>` detected from the DOMSnapshot attributes → `AXSecureTextField`: the pruner never emits its value, and `press_element`/`set_value` refuse it. `type_text` has no focused-password probe on this backend and a coordinate `click` is not refused on a secure element, so text typed while a password field holds focus lands in it (see SECURITY.md item 3; only macOS probes before typing). |
| Activate / focus   | `DOM.resolveNode` → `Runtime.callFunctionOn` `this.click()` / `this.focus()` (a11y-first) |
| Set value          | native prototype value setter + bubbling `input`/`change` events (`_SET_VALUE_FN`, `drivers/browser.py`). Intended so framework-controlled inputs (React/Vue) observe the change; verified live only on a plain `<input>` by read-back of `.value` (`tests/test_agent.py::test_live_browser_agent_loop_fills_and_clicks_by_ref`), and hermetically only that the sent function contains `dispatchEvent` (`tests/test_browser.py::test_browser_type_and_set_value_emit_expected_cdp`). No test asserts a listener fires, and none runs against React or Vue. |
| Type               | `Input.insertText` into the focused element: one CDP call carrying the whole string, no per-key `dispatchKeyEvent`s (`type_text`, `browser.py:292-299`; asserted hermetically in `tests/test_browser.py:335-341`) |
| Key chords         | `Input.dispatchKeyEvent` with a CDP modifier bitmask (`_key_events`)            |
| Pixel fallback     | `Input.dispatchMouseEvent` (mouse/wheel), the vision path, viewport-mapped     |
| Capture            | `Page.captureScreenshot` (+ `clip` for zoom), `captureBeyondViewport`           |
| Navigate           | `launch_app(url)` = `Page.navigate` + wait for `document.readyState=="complete"` (load-aware, no fixed sleep). The browser analog of launching an app, so the existing `app` tool drives it with no new MCP surface |
| Iframes            | child frames stitched in: `getFrameTree` → per-frame `getFullAXTree` grafted under the owner `Iframe` node, geometry offset into the top document (same-process frames; cross-origin OOPIF skipped, never fatal) |
| Tabs               | page targets are modelled as apps/windows (`running_apps`/`windows`/`activate_app`) |
| Console            | `console` tool (browser-only, tier `read`): console output + uncaught JS exceptions from `Runtime.consoleAPICalled`/`exceptionThrown`/`Log.entryAdded`. This is how the agent verifies an action worked, which a screenshot cannot show |
| Network            | `network` tool (browser-only, tier `read`): completed request outcomes (status codes + failures) from `Network.responseReceived`/`loadingFailed`, joined by `requestId` ("did that POST return 200?"); the session event buffer is bounded so busy pages can't grow it |

`stable_id` is the backend DOM node id (stable across snapshots within a page,
not across navigations). `BrowserDriver.resolve_ref` delegates to the shared
`observe.rematch_ref`, whose matcher tries an exact (`stable_id`, role) match
first, so a ref survives relayout and label changes while its DOM node persists.
When that node is gone, the shared title/path/bounds ladder applies and may raise
`stale_ref` (`reason: not_found` or `ambiguous`). The stable-id path is
unit-tested on synthetic elements in `tests/test_observe.py`
(`test_stable_id_survives_title_and_bounds_drift`); no browser test drives
`resolve_ref` across a real DOM shift.

## Transport seam and testing

All wire I/O sits behind `_cdp.Transport`, so the driver logic can be exercised
in a container with a scripted fake transport (no browser): `tests/test_browser.py`
covers snapshot and observe (geometry, roles, stable ids), accessibility-first
press and focus, secure-field refusal, `set_value`, typing, key chords, navigate,
screenshot, same-process iframe stitching, a skipped cross-origin frame,
console, network, and the app/window/clipboard tools through the Runtime. Not
covered by any test: coordinate `click`/`drag`/`scroll`
(`Input.dispatchMouseEvent`) and the 24-frame stitching cap (`_MAX_FRAMES`).
Four opt-in live tests
(`test_live_observe_act_verify`, `test_live_iframe_content_is_observable_and_actionable`,
`test_live_console_captures_logs_and_exceptions`,
`test_live_network_reports_status_and_failures`), plus
`tests/test_arena.py::test_live_arena_measures_real_costs`, exercise headless
Chromium end to end. They skip unless a CDP endpoint is reachable
(`A11Y_COMPUTER_USE_CDP_ENDPOINT`, default `http://127.0.0.1:9222`); the `browser` CI job
runs them against headless Chrome.

## Running it

```bash
# 1. start Chromium with a debugging port (headless=new works in CI)
google-chrome --headless=new --remote-debugging-port=9222 about:blank &

# 2. point a11y-computer-use at it
pip install -e '.[browser]'               # from a clone (not on PyPI yet); adds websocket-client (only extra dep)
A11Y_COMPUTER_USE_DRIVER=browser \
A11Y_COMPUTER_USE_CDP_ENDPOINT=http://127.0.0.1:9222 \
a11y-computer-use mcp
```

The endpoint defaults to `http://127.0.0.1:9222`. Target discovery uses stdlib
`urllib`; only the WebSocket needs `websocket-client`. The handshake sends no
`Origin` header (`suppress_origin`), so Chrome accepts it with **no**
`--remote-allow-origins` flag.

## Live-verified

The observe→act→verify loop is proven against real headless Chrome: a
coordinate-free `press_element` on a button fires its `onclick`
(`document.title` flips), and focus + `type_text` lands text in an input, asserted
in `test_live_observe_act_verify`.

## Measuring the moat: cu-arena

`a11y-computer-use bench web <url> [--mode full|interactive] [--json]` (module `a11y_computer_use/arena.py`) reports the honest
per-observation token cost of the a11y-first snapshot vs the screenshot a vision
agent would send instead. Both raw numbers, no rigging: the a11y cost is the
real rendered snapshot, the image cost is the real captured frame's dimensions
run through Anthropic's published `w*h/750` estimate. On the small live test page
the CI browser job reproduces **5.2x cheaper per observation** (88 vs 454 tokens on
an 11-element page in a 780x437 CSS-px frame, run 33436980587), and, being text,
the a11y side *diffs* to about 10 tokens on an unchanged re-observe where a
screenshot pays its full image cost every step. Both figures are estimates
(chars/4 and w*h/750, not tokenizer counts). The a11y cost grows with the element
count while the screenshot cost is fixed by the frame, so the ratio depends on the
page; the diff advantage does not. The browser CI job prints the numbers on every
run. `a11y-computer-use bench audit` aggregates the JSONL audit log (cu-meter:
per-action latency p50/p95 + tokens). `a11y-computer-use bench desktop` runs the same
per-view measurement on the current platform driver (or `A11Y_COMPUTER_USE_DRIVER=browser`
for the bound tab); method and numbers in docs/observation-cost.md. `a11y-computer-use
bench h2h` is the task-level head-to-head on this backend (docs/benchmark.md).
