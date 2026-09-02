# Observation cost: what a snapshot costs, and how to pay less

computerUse's claim is not "the accessibility tree is always smaller than a
screenshot". It is that the tree is *text*, so it can be cut down to what the
agent needs and, after the first look, re-observed as a diff. This page records
what the estimator counts, the measured numbers on real pages, and which of the
four ways of observing (`full`, `interactive`, `find`, `diff`) to use when.

Every number below was produced by `computeruse bench`, which is the same code
path as the MCP tools, on 2026-09-02 against headless Chrome 152 with
`--remote-debugging-port=9555`. Rerun the commands to reproduce them; pages
change, so expect the dense-page numbers to move.

## What the estimator counts

`computeruse/arena.py` prices both sides of one observation of the same UI state:

- The a11y side is the rendered snapshot text divided by 4 characters per token,
  rounded up. This is the basis cu-meter already uses for `tokens_est` in the
  audit log, so arena, audit and this page speak the same units.
- The screenshot side is the frame the driver captured at the same moment,
  priced with Anthropic's published estimate of width times height over 750,
  in CSS pixels (physical pixels divided by the backing scale).
- A re-observe is the rendered `diff` of two consecutive snapshots. A
  screenshot has no diff form, so its re-observe cost is another full frame.

When the backend cannot capture (no Screen Recording grant on macOS, a backend
without capture) the report prints the a11y numbers and says the screenshot was
unavailable. It never substitutes a zero and claims a ratio.

## Snapshot views

`desktop_snapshot(app, scope, mode, budget, include_bounds)` accepts three modes.

`full` is the whole pruned tree, one line per element, geometry on the root
only. It is the right first observation when the agent needs to read content.

`interactive` renders the same snapshot down to what the agent can act on:
elements that take input (clickable, editable, including disabled ones), carry
state (checked, expanded, selected, focused), or have a role an agent selects
(rows, tabs, sliders), plus the containers needed to keep them apart (the root,
windows, dialogs, menus, toolbars, tab groups, titled groups). Everything else
folds into one `text:` line per kept container, capped at 96 characters with a
count of the rest. Rows and cells that merely hold links are layout and fold
too. The refs are the snapshot's own refs, so `click`, `set_value`, `wait_for`
and `act` resolve them exactly as they resolve full-mode refs; an agent can
switch views mid-task without a stale ref. The `click` flag is left implicit on
roles that are pressable by definition (buttons, links, menu items, checkboxes,
radios, tabs); other flags still print. Geometry is omitted unless
`include_bounds=true`.

`diff` returns only what changed since the previous snapshot of the same app,
rendered in the view the agent last asked for. In the interactive view, changes
to static text fold into one `~ text:` line, so a status label flipping to
"Saved" still shows, and added or removed static elements are reported as
counts. Effect Receipts (`click(verify=true)`, `act(verify=true)`) use the same
view.

`budget=N` caps any of the three at about N tokens. The header always survives,
lines are kept in order until the next one would overflow, and a final marker
says how many element lines were omitted. The cut is deterministic for a given
snapshot.

`find(app, text=..., role=...)` is the fourth way: it snapshots and returns only
matching elements with their refs and bounds. It is the cheapest observation
when the agent already knows what it is looking for.

## Measured

Commands (a Chrome with `--remote-debugging-port=9555` was already running):

```bash
export COMPUTERUSE_CDP_ENDPOINT=http://127.0.0.1:9555
computeruse bench web https://example.com --rounds 3
computeruse bench web https://example.com --rounds 3 --mode interactive
computeruse bench web https://news.ycombinator.com --rounds 3
COMPUTERUSE_DRIVER=browser computeruse bench desktop --rounds 3   # every view of the bound tab
```

| Page | Elements | Screenshot | Full view | Interactive view | Re-observe diff |
|---|---|---|---|---|---|
| example.com (756x469) | 7 | 473 tok | 95 tok (5.0x cheaper) | 72 tok (6.6x cheaper) | 10 tok |
| news.ycombinator.com (804x1214) | 330 | 1,301 tok | 4,509 tok (3.5x more expensive) | 758 tok, 84 element lines (1.7x cheaper) | 10 tok |

Those two rows predate the depth-cap fix described below. The same commands on
the fixed engine, run the same day on a second headless Chrome (port 9777,
default window, so the frame and the screenshot price differ from the rows
above):

| Page | Elements | Screenshot | Full view | Interactive view | Re-observe diff |
|---|---|---|---|---|---|
| example.com (1280x513) | 7 | 876 tok | 95 tok (9.2x cheaper) | 72 tok (12.2x cheaper) | 10 tok |
| news.ycombinator.com (1280x1214), before the fix | 330 | 2,072 tok | 4,511 tok (0.5x) | 759 tok | 10 tok |
| news.ycombinator.com (1280x1214), after the fix | 435 | 2,072 tok | 4,539 tok (0.5x) | 1,089 tok (1.9x cheaper) | 10 tok |

Two readings of that table.

On a simple page both views beat the screenshot by a wide margin, and the diff
makes every later observation nearly free. On a dense, link-heavy page the full
tree costs more than the screenshot: 102 links plus the table cells and static
text around them render to 18k characters. The interactive view brings the same
page under the screenshot's cost by folding the static elements into text lines
and dropping the layout rows and cells, while keeping every link addressable by
ref. The diff is 10 tokens either way, which is the number the moat actually
rests on: an agent that observes once and then re-observes by diff pays the
screenshot price zero times.

### Desktop

`computeruse bench desktop --app Finder` on this machine returned
`permission_denied_accessibility`: the shell that ran it (Ghostty) does not hold
the Accessibility grant, and the screenshot side needs Screen Recording. The
command runs on any Mac that has both grants; until then the desktop side has
no measured number on this page. The earlier Phase 0 figure for a pruned desktop
window snapshot (roughly 680 to 1,460 tokens for the test apps, which is about
one screenshot) was measured in `full` view only; `bench desktop` is how that
figure gets a companion `interactive` number.

`tests/test_arena.py` carries an opt-in live test for Finder that self-skips
without the grants, and one for the browser backend that runs in CI.

### The depth cap fix, and what the markers were hiding

Before the fix, 209 of the 540 lines of the Hacker News full render were
`… 1 more` markers, about 5,000 characters or 1,240 tokens. They came from the
depth cap in `observe.build_snapshot`: `MAX_DEPTH` counted raw tree depth,
including the generic wrappers that later collapse into their only child, and
the page's DOM is deeper than 12 levels, so the walk stopped one level short
under most rows. The engine now counts depth over kept ancestors only
(`_prune_inner` carries a lower bound while walking, `_enforce_depth` applies
the exact cap afterwards, and a separate `_MAX_RAW_DEPTH` still bounds
pathological trees).

The measurement corrected the expectation this page used to state. The markers
were not removable cost: they were hiding real nodes. On the same page and
frame, the fix took the marker count from 209 lines to 1 (the remaining one is
the fan-out cap on the story table, `… 68 more`), and the full view stayed at
4,511 to 4,539 tokens, because 1,240 tokens of markers became about 1,270
tokens of content: 24 more links and 93 more text nodes that the agent could
previously neither read nor click. Actionable elements went from 79 to 103. The
interactive view rose from 759 to 1,089 tokens for the same reason: it now
lists 24 more targets. The full view of a dense page still costs more than one
screenshot; the ways to pay less remain `interactive`, `find`, `diff`, and
`budget`.

## Which view to use

- First look at an app or page: `interactive`. Switch to `full` when the task
  needs the text (reading an article, checking a value in a table).
- Known target: `find` with `text` or `role`. It is cheaper than either view and
  returns refs the agent can act on.
- After an action: `diff`, or `verify=true` on the action itself. Read
  `(no change)` as "the action had no visible effect".
- Long or virtualized lists: `scroll_to_find`. The `… N more` markers in either
  view mean the walk was capped, not that the rows do not exist.
- A hard ceiling on context: `budget`. The trailing marker tells the agent how
  much it did not see.
