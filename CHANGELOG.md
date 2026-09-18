# Changelog

All notable changes to a11y-computer-use are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). 0.1.0 is the first
tagged release; it is installed from git, and nothing is published on PyPI.

Each line describes one non-merge commit and ends with its short hash and author date (the date `git log --date=short` prints). Branch-integration merge commits carry no changes of their own and are not listed.
Within a group, lines are ordered by theme, then by date.

## [Unreleased]

- Runtime operations and batches retain exclusive ownership of their snapshot;
  MCP admission and queue waits are bounded, with `busy` and `closed` errors.
- CDP commands use serialized deadlines, strict tab binding, bounded diagnostic
  buffers, explicit disconnect errors, and release remote DOM handles.
- Permission updates are atomic across local processes; invalid policy edits
  deny actions. Audit files have configurable size/retention limits and recover
  incomplete writes.
- Windows permission updates retry transient file-sharing errors within a
  deadline and use consistent file metadata to cache unchanged policies.
- Browser password probes fail closed. Long scrolling searches recheck their
  target and permissions before input.
- Linux typing verifies the remembered editable belongs to the frontmost app
  and clears stale targets after focus-changing actions.
- Linux XTEST typing prepares the full Unicode keymap before input and paces
  keystrokes consistently to prevent the observed GTK/IBus character reordering.
  Insufficient spare keycodes now reject the text before emitting partial input.
- ASCII Box verification fails on missing live coverage, preserves reports,
  and includes a verified multiprocess browser load harness. See
  [production guidance](docs/production.md) and [concurrency contract](docs/concurrency.md).

## [0.1.0] - 2026-09-02

The first tagged release. Everything the project has shipped so far lands here, since nothing was tagged before it. Install from a clone of the tag; the package is not on PyPI.

### Added

#### Repository

- Repository created with an MIT license and a two-line README (25266b1, 2026-07-02).

#### Observe, act, and the MCP surface

- Phase-0 MVP: the a11y-first macOS framework. Observe (pruned accessibility snapshots with `e1..eN` refs), act (CGEvent input), safety (permission tiers, per-app grants, JSONL audit log), screen capture, `doctor`, the MCP server, the `a11y-computer-use` CLI, the test suite, and the demand-validation record in `docs/` (96a70b9, 2026-07-12).
- Automatic a11y to vision handoff: a full snapshot with no clickable or editable element appends a note pointing the agent at `screenshot`. Per-widget pruning caps dense containers (grids, tables, outlines) at 12 children instead of the general 24 (da96084, 2026-07-13).
- `Driver` protocol and the `a11y_computer_use/drivers/` package. The platform-free core (observe engine, safety, Runtime, MCP server) reaches native APIs only through this seam. Shipped with the macOS driver and a Windows skeleton whose methods raised `NotImplementedError` naming the native API each would use, plus `docs/windows-port.md` (81d3b19, 2026-07-13).
- `find` tool (filter a fresh snapshot by text, role, editable, or clickable), richer element states in the snapshot text, and force-enabled Chromium/Electron accessibility trees on macOS by setting `AXManualAccessibility` and `AXEnhancedUserInterface` on the app element (opt out with `A11Y_COMPUTER_USE_NO_WEB_A11Y`), with `examples/web_a11y_demo.py` (950ae21, 2026-08-29).
- Stable-id anchors: when the app exposes a stable identifier, a ref re-resolves by it before falling back to role, title, path, and bounds proximity. At HEAD the sources are `AXIdentifier` (macOS), `AutomationId` (Windows), `accessible-id` (Linux), and the backend DOM node id (browser) (ce02cb0, 2026-08-29).
- Batched `act` tool: a list of click, type, key, scroll, drag, and wait_for steps in one call. Each step is gated and audited at its own tier. The batch stops at the first failing step and does not roll back steps already executed (4bd1886, 2026-08-29).
- Diff snapshots: `desktop_snapshot(mode="diff")` returns only the added, removed, and changed elements against the previous snapshot of the same app, and falls back to the full render when there is no such snapshot (4855216, 2026-08-29).
- Self-correcting `stale_ref` errors: when a ref no longer resolves, the error detail carries up to three near-miss candidates from the live tree (70b2a3a, 2026-08-29).
- `set_value` tool: sets an editable element's value in one accessibility operation, falling back to focus plus typing when the driver cannot set it directly. Secure fields are refused before gating (ea12128, 2026-08-29).
- Set-of-Mark screenshots: `screenshot(marks=true)` draws ref labels from the latest snapshot onto the image (37b2706, 2026-08-29). As first landed, `marks_for` called a nonexistent `ScaledImage.to_scaled` and raised `AttributeError` on the live path whenever there was an element to mark; fixed in 40973f2 (see Fixed).
- `scroll_to_find` tool: scrolls a view and re-observes after each step until a text or role match appears, up to `max_scrolls` (default 6) (61f7fc0, 2026-08-29).
- Effect Receipts: `verify=true` on `click` and `act` re-snapshots the app after the action and appends the diff (0ec4c70, 2026-08-29).
- Interactive snapshot view and token budget: `desktop_snapshot(mode="interactive", budget=N, include_bounds=...)` renders the same snapshot down to input-taking and stateful elements, rows/tabs/sliders that are targets themselves, and the containers that keep them apart (roots, windows, dialogs, menus, toolbars, tab groups, titled groups), with static text folded into one `text:` line per container; refs are unchanged, so acting and re-resolving work across views. `mode="diff"` and Effect Receipts render in the view last asked for, and the interactive diff keeps static-text changes as one `~ text:` line. `budget` cuts any rendering deterministically after the header and reports the omitted element lines. Full-mode output is byte-identical to before (99b8d0b, 2026-09-02).
- `scroll_to_find` takes an optional `ref` that pins the container to scroll (d6c19d9, 2026-09-02).

#### Safety

- Confirmation gate for clicks on destructive labels (delete, trash, discard, erase, and similar) through MCP elicitation. Without an elicitation channel the click is blocked; `A11Y_COMPUTER_USE_CONFIRM=0` disables the gate (ca5adba, 2026-07-12).
- `type` probes for a focused password field on every driver: the browser evaluates `document.activeElement` through open shadow roots and same-origin iframes, Linux walks the active window's AT-SPI tree for the focused node (bounded to 400 nodes), Windows asks UIA for the focused control's `IsPassword`; UIA password edits are marked `AXSecureTextField` and their value is never read. `tests/test_safety_hardening.py` (57 tests) covers the new paths (efd9d81, 2026-09-02).

#### macOS backend

- Ref clicks activate elements through the AX API (`AXPress` and related actions), so the user's cursor does not move; synthetic mouse events remain the fallback (1f27621, 2026-07-12).
- Visual presence overlay: a blue screen-edge glow and an agent cursor in a standalone `a11y_computer_use.overlay` module with a demo (`python -m a11y_computer_use.overlay`). macOS-only; not wired into the MCP server (0b8eab0, 2026-07-13).
- Non-intrusive scroll: `scroll(ref, into_view=true)` uses `AXScrollToVisible` instead of a wheel event (e3231f0, 2026-07-13).

#### Windows backend

- `snapshot` through UI Automation into the shared pruning engine; verified in CI on `windows-latest` against Notepad (6e03c9d, 2026-07-13).
- Act loop: `type_text` via SendInput Unicode, `press_element` via the UIA Invoke, Toggle, SelectionItem, and ExpandCollapse patterns with SetFocus for editables, and `scroll_into_view` via ScrollItemPattern (8ed00dc, 2026-07-13).
- `key_chord` via virtual-key SendInput (ebb66fc, 2026-07-13).
- The full gated Runtime (permission check, recheck, audit) runs on Windows, with app identity taken from the process image name (a865706, 2026-07-13).

At HEAD the Windows driver still raises `NotImplementedError` for `resolve_ref`, coordinate `click`, `drag`, `scroll`, `wait_for`, `screenshot`, `zoom_region`, app and window enumeration, launch and activate, and clipboard read and write.

#### Linux backend

- Linux backend over AT-SPI2: snapshot, ref re-resolution, press, focus, `set_value`, and typing through `EditableText`, with XTEST for coordinate input and EWMH for windowing. Live-verified in CI against a GTK3 window under Xvfb. Ships with the `[linux]` extra, `docs/linux-port.md`, and a `linux` CI job (445dd60, 2026-08-23).
- `org.a11y.Status` is switched on over D-Bus, so running Chromium/Electron apps expose their AT-SPI trees without a relaunch (53cce34, 2026-08-29).
- ARIA `xml-roles` mapping for web roles, with a single attribute fetch per node (f3c997c, 2026-08-29).
- Wayland-native screen capture through `grim`, and `capture.py` now imports cleanly off macOS (615e939, 2026-08-29).
- Structured `unsupported` error for coordinate and key injection on native Wayland, where XTEST is unavailable. The AT-SPI path (press, `set_value`, typing into a field focused through the driver) keeps working (e0d48e3, 2026-08-29).
- `doctor` gains Linux and Windows sections (display session, window manager, AT-SPI bindings and bus, XTEST availability, clipboard tool; the UI Automation import on Windows) instead of reporting macOS grants that do not exist there. AT-SPI application lookup matches by PID as well as name and ranks the active top-level frame first, so a windowless registrant with the same comm never shadows the real app. XTEST typing maps F13 to F24, named punctuation, and control characters, and binds spare keycodes for characters the layout lacks (the xdotool approach), so off-keymap Unicode round-trips. `tests/test_linux_desktop_live.py` adds ten real-desktop tests (coordinate click, scroll, drag, chords, typing, apps, windows, clipboard, the gated Runtime) that run under a window manager and skip without one (5267da2, 2026-09-02).
- `scripts/box/`: `bootstrap.sh`, `run-live.sh`, `verify-pointer.sh`, and `pointer_probe.py` stand up a Box (box.ascii.dev) Ubuntu desktop VM and run the live suites, the browser suite against a non-headless Chrome, and the pointer probe there; `docs/box-testbed.md` records the runs (fada77e, 2026-09-02; 6240b56, 2026-09-02).

#### Browser backend (CDP)

- `BrowserDriver` over the Chrome DevTools Protocol: `Accessibility.getFullAXTree` and `DOMSnapshot` fused into the shared observe engine; coordinate-free press, focus, `Input.insertText`, and `Input.dispatchKeyEvent`. Selected only by `A11Y_COMPUTER_USE_DRIVER=browser` or `get_driver("browser")`, never by platform. Adds the `[browser]` extra (websocket-client), `docs/browser-backend.md`, and the wider package description (58ec018, 2026-08-29).
- Iframe stitching: same-process frames (same-origin, about:blank, srcdoc) are grafted under their owner iframe node with composed offsets. Cross-origin out-of-process frames are skipped rather than failing the snapshot. The walk is capped at 24 frames (18b780b, 2026-08-29).
- Load-aware navigation: `launch_app(url)` runs `Page.navigate` and polls `document.readyState` until it is `complete` (63c3817, 2026-08-29).
- The full gated Runtime runs on the browser backend with tabs as apps: grants are keyed by CDP target id, `app list` and `window list` return tabs, and `app focus` rebinds to a tab (324c919, 2026-08-30).
- `console` tool, browser-only: buffered `Runtime.consoleAPICalled`, `Runtime.exceptionThrown`, and `Log.entryAdded` events returned as `{level, text}`; reading clears the buffer (a132a62, 2026-08-30).
- `network` tool, browser-only: requests joined with their response status or load failure by request id; reading clears the buffer (aa7ceb1, 2026-08-30).
- Hermetic test proving `console` and `network` are registered only on the browser driver: 16 tools on the OS drivers, 18 on the browser (fe279fa, 2026-08-30).

#### Agent loop and provider adapters

- `a11y_computer_use.adapters`: Anthropic (`computer_toolset_20260801` members and the legacy `computer_20251124`/`computer_20250124` action shape, all 17 members, `tool_definition` plus `beta_header`) and OpenAI (GA `{type: computer}` and `computer_use_preview`, the nine actions, `handle_call` running an actions array and building `computer_call_output`) computer-use executors that run provider-native pixel actions through the gated Runtime, with snap-to-ref (smallest actionable element under the point, size cap, `stale_ref` fallback), an optional Set-of-Mark, and a `Result` type that never raises to the host. Adds `docs/provider-adapters.md`, `examples/anthropic_computer_use.py`, `examples/openai_computer_use.py`, and `tests/test_adapters.py` (81 hermetic tests on a recording fake driver plus a scripted-CDP browser run; the live test skips without an endpoint and passed on headless Chrome per the commit message) (d913093, 2026-09-02).
- `a11y-computer-use agent --task`: the reference observe, plan, act, verify loop through the gated Runtime, with pluggable planners in `a11y_computer_use/providers.py` (Anthropic Messages API, OpenAI-compatible chat completions via `OPENAI_BASE_URL` for Ollama/vLLM, the Claude Code CLI with no key, and a scripted provider; stdlib only). The planner sees the real MCP tool schemas (`server.tool_specs`) plus a `done` tool; `stale_ref` errors carry a fresh snapshot; older observations collapse to placeholders. Adds `server.Runtime.call_tool` (the full surface by name; `run-once` dispatch unchanged), `build_server(runtime=...)`, planner token sums in `bench audit`, `docs/agent-loop.md`, `examples/agent_task.py`, 43 hermetic tests, and one opt-in live browser test; live-verified by the author with `claude-cli` on headless Chrome, not in CI (70dcb15, 2026-09-02).

#### Benchmarks and telemetry

- cu-meter: per-action metrics (`duration_ms`, `result_chars`, `tokens_est` at 4 chars per token) written to the audit log, plus `a11y_computer_use/bench.py` to aggregate them (6941bf0, 2026-08-29).
- cu-arena: measures the a11y snapshot cost (chars/4) against the screenshot cost (width*height/750) for the same UI state on the same driver. Adds the `a11y-computer-use bench` CLI with `bench audit` (cu-meter report) and `bench web URL`, and a live step in the browser CI job (6df365e, 2026-08-29).
- cu-arena also scores the re-observe diff cost for every round after the first (9d8d4ae, 2026-08-29).
- `a11y-computer-use bench desktop [--app] [--scope] [--rounds] [--json]` costs every snapshot view (full, interactive) of a running app against the screenshot captured at the same moment, plus the re-observe diff per view, with the same estimator as `bench web`; `bench web` gains `--mode` and `--json`; `snapshot` gains `--mode`, `--budget`, and `--bounds`. A capture the backend cannot deliver is reported as such, never zeroed into a ratio. `docs/observation-cost.md` records the method and the live numbers (example.com 95/72 vs 473 tokens for full/interactive vs screenshot; Hacker News 4,509/758 vs 1,301; re-observe diff 10 tokens on both; Finder not measured, no Accessibility grant in that shell) (910a530, 2026-09-02).
- cu-arena head-to-head: `a11y-computer-use bench h2h` runs a browser task suite (12 tasks at first, 13 with `dropdown_custom`) (`a11y_computer_use/arena_tasks/`, instrumented pages that record clicks, inputs, and a state digest) through three loops with the same planner: the reference agent loop on accessibility refs, a screenshot-only coordinate loop executed by the Anthropic computer-use adapter, and the same loop with snap-to-ref. It scores completion, planner turns, actions, misclicks (counted by the page), wasted actions, tokens, reported and estimated cost, and wall time, writes markdown and JSON reports, and serves the fixtures over local HTTP so iframes work. `ClaudeCLIProvider` gains `view_images` (the newest screenshot written to a temporary PNG, Read tool only), runs with `--strict-mcp-config`, and reports the CLI's per-call cost in `Usage.cost_usd`. `docs/benchmark.md` describes the method (6ee97b9, 2026-09-02).
- Head-to-head comparability: task manifests can flag a mode as not comparable (the native `select` popup is not painted in headless Chrome), the report shows completion rates with and without flagged tasks, `--render` re-renders saved `h2h.json` files with the current manifests, and the `dropdown_custom` task (a DOM-rendered listbox) gives the dropdown a fair three-way comparison (7078709, 2026-09-02).
- The head-to-head state digest counts focus changes, so a click that only focuses a field is no longer scored as wasted (9e27bdc, 2026-09-02).
- Both head-to-head loops receive the same approving confirmation callback, so the destructive-label gate cannot decide the comparison (a ref click on a button titled Delete trips it; a coordinate click carries no title) (7618d05, 2026-09-02).
- The live `scroll_to_find` check gives the loop enough scroll steps to reach a list item 3,200 px down (d11d407, 2026-09-02).
- `docs/benchmarks/h2h-2026-09-02.md` and `.json`: the first dated head-to-head result. One planner (`claude-fable-5-1` through the Claude Code CLI), 13 tasks, one round: refs 13/13 with 0 misclicks at $7.04 reported cost; pixels 7/13 with 27 misclicks at $11.74; pixels+snap 6/13 with 31 misclicks at $12.07; on the 12 comparable tasks 12/12 vs 7/12; pixels won `search_filter`. Superseded pre-fix rows are kept alongside (35ac0f6, 2026-09-02).

### Changed

- License changed from MIT to Apache-2.0; `NOTICE` and the PyPI license classifier added (bf2355a, 2026-07-12).
- `server.py` no longer imports pyobjc at module import, so `build_server()` runs on Windows; the Windows CI job gained a build smoke step (cbf7904, 2026-07-13).
- Simplification pass over the browser driver, arena, and Runtime: duplicate code removed and fewer CDP round-trips per operation (2bdce2d, 2026-09-01).
- One shared `observe.rematch_ref` handles ref re-resolution for the macOS, Linux, and browser drivers, and the `app`, `window`, and `clipboard` tools route through the driver. `window raise` still calls `NSRunningApplication` directly and is macOS-only code (bcce4fa, 2026-09-01).
- `a11y-computer-use snapshot` resolves its backend through `drivers.get_driver()` like `run-once` and `mcp`, so it works on every OS and honours `A11Y_COMPUTER_USE_DRIVER`; `doctor` help text names the per-platform checks (5f7766a, 2026-09-02).
- `window raise` runs through the new `Driver.window_owner` / `raise_window` seam: macOS behaviour unchanged, Linux raises by X window id via `_NET_ACTIVE_WINDOW`, browser and Windows answer a structured `unsupported` (efd9d81, 2026-09-02).
- `scroll_to_find` anchors its wheel on the largest list, table, or outline container below the window rather than the window itself; the browser backend exposes an overflow list as `AXList`, so the previous anchor scrolled the page (d6c19d9, 2026-09-02).

### Fixed
- Agent loop: planner transport failures (timeouts, connection resets, truncated bodies) end the run with `stopped=provider_error` and an audit row instead of an unhandled exception; `done(success)` is validated (string booleans flagged as `invalid_done_arguments`, anything else fails the run); a `done` issued alongside other tool calls is deferred until their results are seen (b8b297f, 2026-09-02).
- Adapters: coordinate clicks snap to a ref only for a plain left click, never against a stale snapshot when the refresh fails, and never inside a populated field or a slider; OpenAI `pending_safety_checks` block execution until acknowledged; screenshot coordinate mapping is normalised to the display size, and the browser driver captures a CSS-sized bitmap on HiDPI tabs (b8b297f, 2026-09-02).
- Observe: the snapshot depth cap now bounds the tree walk itself, so deep wrapper chains and cyclic fan-out are read in bounded time (b8b297f, 2026-09-02).
- Linux: `pids_matching` matches the window owner's comm only; AltGr-only keysyms are typed through a spare keycode; the AT-SPI focus probe asks the Collection interface first and `type` refuses when the probe cannot decide (b8b297f, 2026-09-02).

- Linux CI live AT-SPI2 step no longer exits with code 137 from a `pkill` self-match (dd79f5f, 2026-08-28).
- Headless Chrome launch in the browser CI job: added `--disable-dev-shm-usage`, a wait for `/json/version`, and diagnostics on failure. The CI run for the previous commit had failed at this step with exit code 7 (3b331ba, 2026-09-01).
- Set-of-Mark: `marks_for` (`a11y_computer_use/marks.py`) mapped element bounds through a nonexistent `ScaledImage.to_scaled`, so `screenshot(marks=true)` raised `AttributeError` on any real `ScaledImage` with an element to mark; it now maps through `ScaledImage.from_source`, and the test fake in `tests/test_marks.py` uses the real method name. Covered by that unit test with a stub only, not by a live screenshot test (40973f2, 2026-09-02).
- CLI description, package and server module docstrings, and the MCP `_INSTRUCTIONS` no longer describe a macOS-only 12-tool server; they describe the cross-platform surface without a hard-coded count. The adapter examples printed `Result.error` and `Result.text` together, which rendered `app_not_found: app_not_found: ...`; they print the text alone, with a test pinning that the code appears exactly once (b4ece8a, 2026-09-02).
- `Runtime.click(x, y)` with no `display_id` filled it from `Quartz.CGMainDisplayID()` unconditionally, so it raised `NameError` on Linux and would have on Windows (found on a real Ubuntu desktop, `docs/box-testbed.md`). The `Driver` protocol gains `main_display_id()`: macOS returns `CGMainDisplayID`, the Linux, Windows, and browser drivers return 0 (the id their `primary_geometry` stamps on snapshots and screenshots). Hermetic tests cover the seam and every backend (838165d, 2026-09-02).
- Linux coordinate input: `_linux_input.click`/`drag`/`scroll` positioned the pointer with `Display.warp_pointer(x, y)`, which X treats as a move relative to the current pointer, so clicks landed at pointer + (x, y); they now queue an absolute XTEST `MotionNotify` before the button events. `_linux_system._geometry_on_root` translated the root origin into window coordinates, so the act-time hit-test never matched a window away from the origin and every `Runtime.click` ended in `focus_changed`; it now translates the window origin into root coordinates, which also fixes window-list bounds. Both bugs were invisible to the Xvfb CI job (every window and the pointer sit at 0,0 there) and were found on a real Budgie/Xorg desktop (`docs/box-testbed.md`). Adds fake-Xlib synthetic tests (`tests/test_linux_synthetic.py`, `tests/test_linux_system_synthetic.py`), two live tests that park the pointer off-origin before a coordinate click, and `scripts/box/verify-pointer.sh` with `pointer_probe.py` (6240b56, 2026-09-02).
- Pre-gate refusals are audited: a ref that fails to resolve (`stale_ref`) and `set_value` on a secure field (`secure_field`) now write an audit row with `decision: null`, with the ref and role only and never the value (efd9d81, 2026-09-02).
- Pointer actions refuse secure fields on every driver: a resolved secure element, or a raw point inside one in the latest snapshot, for `click`, `drag` (either endpoint), and wheel `scroll`. Previously a ref click on a password field fell back to a raw pointer click on the browser and Linux backends (efd9d81, 2026-09-02).
- Browser wheel scroll direction was inverted: `drivers/browser.py` negated `dy` the way the macOS driver does, but CDP already uses the tool contract's sign, so every scroll down at the top of a list was a no-op. Found by the head-to-head `long_list` task, which all three modes failed before the fix (b7cd8db, 2026-09-02; test pinned in b31f477, 2026-09-02).
- `a11y_computer_use.act` imports on Linux and Windows (the CGEvent tables are built only when Quartz imported), so the full suite collects and passes off macOS: `tests/test_server.py` builds its Runtime on the macOS driver seam it mocks, doctor assertions are platform-aware, Linux live tests skip without a display, and `h2h.load_tasks` ignores dot-prefixed sidecar files such as AppleDouble `._*.json` (00d2ea2, 2026-09-02).
- The suite passes on Windows runners: `doctor`'s first check is `uiautomation_import` there, and an autouse conftest shim makes `Path.home()` honour `HOME` on win32 so tests that redirect `HOME` to a temp dir no longer read and write the runner's real `~/.a11y-computer-use` (0c5eba7, 2026-09-02).
- `scripts/box/bootstrap.sh` installs `dbus-x11`; without `dbus-launch` the `org.a11y.Status` flip cannot autolaunch a session bus after a box resume (9ff0167, 2026-09-02).

### Performance

- Linux: XTEST events are batched and flushed once per operation (51dbcbc, 2026-08-29).
- Linux: the value probe is skipped on roles that carry no value, saving D-Bus round-trips (3f50039, 2026-08-29).
- Linux: opt-in AT-SPI event thread (`A11Y_COMPUTER_USE_ATSPI_EVENTS=1`) that trusts libatspi's read cache. The commit message reports about 1.8x on its own measurement; that figure is not reproduced in CI (3371405, 2026-08-29).
- Linux: event-driven `wait_for` on the a11y event thread when the opt-in is set (bfe469e, 2026-08-29).
- Observe: the prune depth cap is counted over kept ancestors rather than raw wrapper nodes (`_prune_inner` carries a lower bound, `_enforce_depth` applies the exact cap afterwards, `_MAX_RAW_DEPTH` keeps the old protection against pathological trees), so deep collapsed wrapper chains no longer stop the walk one level short. Commit-reported numbers on news.ycombinator.com, same frame: `… 1 more` markers 209 -> 1, elements 330 -> 435 (actionable 79 -> 103), full view 4,511 -> 4,539 tokens, interactive 759 -> 1,089; not reproduced in CI. Existing fixtures unchanged; two synthetic tests added; `docs/observation-cost.md` corrected (a9e42d9, 2026-09-02).

### CI

- First GitHub Actions workflow: cross-platform install plus a Windows validation job on `windows-latest` (driver selection smoke, core tests) next to the macOS job (2a02599, 2026-07-13).
- The Linux job (apt AT-SPI2, GTK, and Xvfb packages; live GTK3 test under `xvfb-run` and `dbus-run-session`) arrived with 445dd60, and the live cu-arena step with 6df365e; both are listed under Added.
- Every runner runs the full hermetic suite with `pytest -q -rs`; the Linux live step runs under the openbox window manager so the real-desktop coordinate tests execute in CI; the browser job adds live adapter, agent-loop, head-to-head, and `bench desktop` steps; a `package` job builds with uv, runs the console script through uvx, and checks that the sdist excludes brand media (`[tool.hatch.build.targets.sdist]`, 4.6 MB to 454 KB); `concurrency` cancels superseded runs and the token is read-only. `docs/ci.md` describes each job (eace75c, 2026-09-02).

### Docs

- PLAN.md reframed after the COM-6 token measurements: the wedge is refs, not token savings (0af7c04, 2026-07-12).
- Launch-ready README with the a11y-first positioning (6a34ee3, 2026-07-12).
- COM-3: the name `a11y-computer-use` is kept (445f0b2, 2026-07-12).
- COM-11: language boundary decision, stay Python for Phase 1 and defer Rust/Swift; `docs/language-boundary.md` (c86cdb4, 2026-07-13).
- COM-12: Phase 0 go/no-go review with verdict GO to Phase 1; `docs/phase-0-review.md` (d07630e, 2026-07-13).
- README and PLAN reframed around embedding into AI-platform products, with the host app owning signing and TCC grants (23a7856, 2026-07-13).
- `examples/`: in-process Python embed, Node MCP-subprocess embed, and a README (6e97131, 2026-07-13).
- Hero demo GIF (`docs/hero-demo.gif`) and a measured-results section in the README (bad6136, 2026-07-13).
- `docs/linux-port.md`: Wayland support matrix (a11y and capture native; coordinate input gated) (c7ebfa7, 2026-08-29).
- `a11y_computer_use/drivers/linux.py`: the `_grab_wayland` docstring now says the grim capture path was exercised manually under headless sway (2026-08-29) and has no automated test, instead of "verified live" (42491a7, 2026-09-02).
- README rewritten from an evidence-cited fact check of the code (four drivers, the 16+2 tool surface, a platform matrix with the exact gates, measured numbers with provenance, embedding shapes, safety model, architecture), plus CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, CITATION.cff, issue forms, a PR template, dependabot, CODEOWNERS, `.editorconfig`, a docs index, brand assets under `docs/assets/`, pyproject metadata, and corrections to stale statements in the existing docs and CI comments (b59cc09, 2026-09-02).
- `docs/box-testbed.md`: the real-desktop Linux run, the four bugs Xvfb hid, the whole-suite desktop run, `doctor` 8 of 8, and the keyboard round-trip; `scripts/box/README.md` syncs with `COPYFILE_DISABLE=1` so macOS tar ships no AppleDouble sidecars (fada77e, 2f9fa73, 8e431e3, 2026-09-02).

Dates are author dates as printed by `git log --date=short` (for one rebased commit, 445dd60, the committer date is 2026-08-25), not release dates. v0.1.0 is the first tag; nothing is published on PyPI.

[Unreleased]: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/compare/v0.1.0...a11y-computer-use-mvp
[0.1.0]: https://github.com/Perception-Dynamics-Inc/a11y-computer-use/releases/tag/v0.1.0
