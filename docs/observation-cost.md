# Observation cost: what a snapshot costs, and how to pay less

a11y-computer-use's claim is not "the accessibility tree is always smaller than a
screenshot". It is that the tree is *text*, so it can be cut down to what the
agent needs and, after the first look, re-observed as a diff. This page records
what the estimator counts, the measured numbers on real pages, and which of the
four ways of observing (`full`, `interactive`, `find`, `diff`) to use when.

Every number below was produced by `a11y-computer-use bench`, which is the same code
path as the MCP tools, on 2026-09-02 against headless Chrome 152 with
`--remote-debugging-port=9555`. Rerun the commands to reproduce them; pages
change, so expect the dense-page numbers to move.

## What the estimator counts

`a11y_computer_use/arena.py` prices both sides of one observation of the same UI state:

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
export A11Y_COMPUTER_USE_CDP_ENDPOINT=http://127.0.0.1:9555
a11y-computer-use bench web https://example.com --rounds 3
a11y-computer-use bench web https://example.com --rounds 3 --mode interactive
a11y-computer-use bench web https://news.ycombinator.com --rounds 3
A11Y_COMPUTER_USE_DRIVER=browser a11y-computer-use bench desktop --rounds 3   # every view of the bound tab
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

`a11y-computer-use bench desktop --app Finder` on this machine returned
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
under most rows. The engine now counts depth over kept ancestors only:
`_prune_inner` reads a node's children before recursing, so it knows whether a
wrapper will collapse and carries the exact pruned depth down the walk; the cap
fires during the walk, which is what bounds its cost on deep wrapper soup and
on cyclic trees with fan-out. A separate `_MAX_RAW_DEPTH` backstops the one
shape the cap cannot see, a fan-out-1 cycle of collapsing wrappers.

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


Groups containing only named leaf radio buttons, such as GTK colour chooser
palettes, use the existing 96-child sheet allowance instead of the ordinary
24-child cap. This keeps all 45 standard swatches available to snapshots,
`find`, and `scroll_to_find` without scrolling an already visible palette.
Mixed groups retain the ordinary cap; larger palettes still show an overflow
marker, and the raw traversal and snapshot token budgets still apply.

## Linux Chromium ref-click reads (#1019)

A ref click still uses a fresh window snapshot for resolution and a second
snapshot when whole-tree effect verification is necessary. List discovery
uses Chromium's native Collection role query, with the existing bounded walk
as a fallback when the query is unavailable or fails. The structural getter
cache is still invalidated between discovery and the snapshot walk, so a
same-count replacement or timer-added child can appear in the effect diff.

Issuing row identity is attached to the refs retained after pruning, including
offscreen matches. Each ref's actual native ancestors still participate;
pruning a layout wrapper does not remove it from row identity. Snapshot-local
ancestry and semantic parts are shared. Nodes outside rows do not need row
names or index attributes. Internal resolution probes bind only the requested target and its ancestors;
outcome-only probes do not issue row identities. Act-time checks still invalidate native and getter
caches and compare the target and its ancestors before input, including after
window preparation. Modal/occlusion policy and effect verification are
unchanged.

`scripts/box/profile_chrome_ref_click.py` compares checkouts in one Chrome
session against a page with twenty form controls, including popovers, details,
select, datalist, date and color inputs. It prints per-click native invocation
counts/times, discovery/identity step times, snapshot times, outcomes and
medians. Native getter invocations can themselves hit libatspi's cache; these
are not claimed to be D-Bus message counts. The script verifies an independent
counter read after each confirmed click. Run it outside pytest under private
Xvfb, a window manager and D-Bus, and interleave revisions to expose host drift.

Child ownership is shared before toolkit/visibility checks. Recreated GI
wrappers must report the same native parent bus/path, and the child must be
on that parent's bus; unknown identity or an embedded application's different
bus keeps the ordinary lookup. This sharing lasts only for the current getter
cache operation and is invalidated by mutations.

Measured on 2026-10-24 with Python 3.13.5, Chrome 155.0.8059.84 and
Xvfb 1280×800/Xfwm4: two interleaved batches of six clicks per revision in
one Chrome session. All 36 clicks confirmed and passed the independent
counter check. Baseline is source tag v0.4.76 (`5db4a7d8`); main is
`fd41931e`. These are instrumented native Linux measurements, not CDP.

| Per-click median | v0.4.76 | main | Fix |
|---|---:|---:|---:|
| Total time (s) | 1.493 | 1.753 | 1.281 |
| Resolution snapshot (s) | 0.551 | 0.665 | 0.430 |
| Verification snapshot (s) | 0.547 | 0.688 | 0.433 |
| Native application lookups | 407 | 411 | 33 |
| Application lookup time (s) | 0.305 | 0.295 | 0.011 |
| Native child-count reads | 388 | 584 | 424 |
| Native indexed-child reads | 623 | 973 | 643 |
| Row-identity calls | 0 | 382 | 1 |
| Row-identity time (s) | 0 | 0.129 | 0.001 |
| List-discovery time (s) | 0.084 | 0.078 | 0.002 |

Step times overlap and are not additive. Each revision still used two full
snapshots for this plain-button effect. Batch medians were 1.522/1.490 s for
v0.4.76, 1.753/1.752 s for main and 1.257/1.281 s for the fix. This reduces
the shared read overhead in #983; it does not add composite-state-only
verification for that issue.

Measurement command, with the baseline and main archives extracted at the
paths shown (the interpreter also needs the Linux dependencies):

```bash
PYTHONPATH=/tmp/ship1019-deps XDG_CACHE_HOME=/tmp/cache1019 \
GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0 \
dbus-run-session -- xvfb-run -a -s '-screen 0 1280x800x24' bash -c '
  xfwm4 >/tmp/w1019shipping.log 2>&1 &
  /workspace/a11y-venv-codex/bin/python scripts/box/profile_chrome_ref_click.py \
    /tmp/ship1019-baseline /tmp/ship1019-main /tmp/ship-1019 \
    /tmp/ship1019-baseline /tmp/ship1019-main /tmp/ship-1019 --runs 6
'
```
