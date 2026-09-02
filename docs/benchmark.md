# cu-arena head-to-head: refs versus pixels, same planner, same tasks

`computeruse bench web` measures what one observation costs. `computeruse bench h2h`
measures what a whole task costs and whether it gets done. The same planner model
runs a fixed suite of browser tasks twice (three times if you include the hybrid),
and the harness scores every run the same way:

| mode | what the planner sees | how actions execute |
|---|---|---|
| `refs` | the pruned accessibility snapshot with element refs (the reference agent loop, `computeruse.agent.run_task`) | `click(ref)`, `set_value(ref)`, `type`, `key`, `act`, through the gated Runtime with Effect Receipts |
| `pixels` | a PNG screenshot only, plus a fresh screenshot after every action | pixel coordinates executed by the Anthropic computer-use adapter as raw coordinate clicks (the incumbent loop) |
| `pixels+snap` | the same screenshot-only loop | the same coordinates, but a click that lands inside a known accessibility element executes as a ref click (`snap_to_refs`, what a pixel client gets for free from `computeruse.adapters`) |

The pixel planner never sees the accessibility tree. The refs planner never sees a
screenshot unless it asks for one. Both get the same instruction, the same planner,
the same planner-turn budget, and a freshly loaded page. The harness cannot make
the accessibility path win; it can only count.

The implementation is `computeruse/h2h.py`. The task suite is
`computeruse/arena_tasks/` (shipped inside the package).

## What is measured

Per task, mode and round:

| metric | definition |
|---|---|
| done | the task's JavaScript success predicate, evaluated over CDP after the run, is truthy |
| turns | planner calls (one `plan()` per turn; a turn may carry several tool calls) |
| actions | executed page actions: click, type, key, scroll, drag, set_value, act (refs) or left_click, double_click, type, key, scroll (pixels). Observations, waits and `done` are not actions |
| misclicks | clicks recorded by the fixture whose nearest element with an id is not in the task's `allowed_targets`. A click that hit nothing with an id is recorded by tag name and counts. Counted by the page's own instrumentation (`_cu.js`), so it is independent of the loop that produced the click |
| wasted | actions after which the page state digest did not change (`__cuState()`: title, form values, body text, scroll positions, hash) |
| tokens in / out | the planner's usage as the provider reported it, summed over turns. Input counts cached and uncached tokens alike |
| cost (reported) | the provider's own per-call cost when it reports one. The Claude Code CLI reports `total_cost_usd` at list price; the HTTP APIs report tokens only |
| cost (est.) | tokens multiplied by `--price-in` / `--price-out` (USD per million), shown only when you pass prices |
| wall | seconds from page load to the end of the run |

Aggregates per mode: completion rate, mean turns, total actions, misclicks,
wasted actions, tokens, cost, wall time.

## The task suite

Twelve deterministic single-page fixtures. Each has a manifest (`<id>.json`) with
the instruction the planner receives, the success predicate, the allowed click
targets and the turn budget. Pages are deliberately plain: system font, high
contrast, every target at least 24x24 CSS px, no decoration that would handicap a
screenshot planner or help an accessibility planner.

| id | probes |
|---|---|
| `form_fill` | two text fields and a submit button |
| `dropdown` | a native `select` with 12 options |
| `checkboxes` | two specific checkboxes among eight with similar labels |
| `menu` | a click-to-open menu bar with a nested submenu (File, Export, PDF) |
| `long_list` | a scrolling list of 111 cities, target near the end |
| `iframe_field` | a text field and button inside a same-origin iframe |
| `similar_buttons` | Archive among Delete, Unarchive, Restore, Duplicate, Move |
| `modal_confirm` | an in-page confirmation dialog (not `window.confirm`, which blocks CDP) |
| `tabs` | switch to a tab whose panel was `display: none`, then act inside it |
| `copy_value` | read a value from the page and type it into a field |
| `table_row` | tick one row's checkbox in a table, then act |
| `search_filter` | type a filter, then act on a result that appeared |

Every page loads `_cu.js`, which records clicks and input events into
`window.__cu` and exposes `__cuState()`. The iframe fixture forwards its events
to the parent with `postMessage` so clicks inside the frame are counted too.

## Running it

You need a Chromium with remote debugging and a planner.

```bash
# 1. a private headless Chrome
google-chrome --headless=new --remote-debugging-port=9666 --window-size=1280,800 about:blank &
export COMPUTERUSE_CDP_ENDPOINT=http://127.0.0.1:9666

# 2. the suite, all modes, with the local Claude Code CLI as planner (no API key)
computeruse bench h2h --provider claude-cli --out docs/benchmarks/latest

# a subset, two modes, three rounds, with an API planner and prices for the estimate
computeruse bench h2h --tasks form_fill,menu --modes refs,pixels --rounds 3 \
  --provider anthropic --model claude-sonnet-5 --price-in 3 --price-out 15

computeruse bench h2h --list        # the suite
computeruse bench h2h --json        # machine-readable report on stdout
```

`--out DIR` writes `h2h.md` and `h2h.json`. The exit code is 0 when every mode
completed at least one task, 1 otherwise, 2 for usage errors.

Providers: `anthropic` (ANTHROPIC_API_KEY), `openai` (OPENAI_API_KEY, or
OPENAI_BASE_URL for Ollama, vLLM, OpenRouter, with `--model`), `claude-cli` (the
local `claude` command). For the pixel modes the CLI provider writes each
screenshot to a temporary PNG and enables only the CLI's Read tool so the model
can look at it; that read is part of the reported tokens and cost. The CLI runs
with `--strict-mcp-config`, so none of your configured MCP servers load into the
planner's context.

The benchmark uses its own permission store and audit log under a temporary
directory (the `workdir` in the JSON meta). It never touches `~/.computeruse`.

## Adding a task

1. Write `computeruse/arena_tasks/<id>.html`: include `<link rel="stylesheet"
   href="_style.css">` and `<script src="_cu.js"></script>` in the head, give every
   legitimate target an `id`, and make the success condition observable (set
   `document.title` or a status element).
2. Write `<id>.json` with `id`, `title`, `page`, `instruction`, `success` (a
   JavaScript expression), `allowed_targets` (ids a correct solution may click,
   including labels that toggle a control) and `max_steps`.
3. Run `pytest tests/test_h2h.py`: the manifest validator checks that every
   allowed target is an id in the page (or its iframe pages), that the page is
   instrumented, and that the success expression is at least syntactically
   balanced. The live test checks that every fixture loads and its predicate is
   false before anyone acts.

If the page has an iframe, the frame must forward clicks to the parent (see
`iframe_field_inner.html`) or its misclicks are invisible.

## What it does not measure

- Real websites. The fixtures are small and plain on purpose; production pages
  have denser layouts, more text and slower loads, which affect both modes.
- Desktop applications. The head-to-head runs on the browser backend so it is
  reproducible in a container. Desktop observation cost is measured separately
  (`computeruse bench desktop`).
- Cross-origin iframes. The browser backend skips out-of-process frames; the
  suite only uses a same-origin frame.
- Statistical significance. One round per task is a single sample per cell. Use
  `--rounds` for more.
- Planner quality in general. Results depend on the model; run the same suite
  with the model you plan to ship.
- Token accounting is what the provider reports. For the Claude Code CLI that
  includes the CLI's own system prompt on every stateless call, for both modes.

## Reading a result honestly

A mode can lose. Report the per-task rows, not only the aggregate, and keep the
failed-run list: a planner that misreads a screenshot and a planner that picks
the wrong ref are both failures the harness records the same way. The dated
results under `docs/benchmarks/` state the planner, model, browser build, machine
and every caveat of that run.
