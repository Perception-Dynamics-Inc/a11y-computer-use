# a11y-agent

`a11y-agent` runs one goal against the desktop. It observes an accessibility
snapshot, asks a model for tool calls, and executes each desktop call through
`server.Runtime.call_tool`. That is the same safety layer the MCP server
uses: permission tiers, the frontmost recheck, secure-field refusal, the
confirmation gate, and the audit log. This package does not add tools to
the MCP server. `shell` and `python` are agent actions, not MCP tools.

The reference planner loop behind `a11y-computer-use agent` is unchanged. It
lives in `a11y_computer_use/agent/reference.py` and is still
`a11y_computer_use.agent.run_task`. Provider HTTP adapters for that loop stay
in `a11y_computer_use/providers.py`. See `docs/agent-loop.md`.

## Architecture

`Agent.run(goal)` and `Agent.stream(goal)` drive this loop:

1. Observe. The first observation of a window is `Runtime.desktop_snapshot`
   of the tracked app, or the frontmost app before one has been launched or
   focused: the pruned tree. Later observations of that same window are a
   diff of added, removed, and changed elements, plus the focused element
   when it did not change. Refs from the latest full snapshot stay valid for
   elements that are still there, including refs the diff does not repeat.
   A different window, or `snapshot` with `mode=full`, sends the full tree
   again. `mode=diff` repeats the changes. Text is fenced the same way as a
   full snapshot.
2. Pause if the tree shows a password field, a one-time-code field, a card
   field, or a captcha iframe. The result is `needs_human`. Nothing is typed.
3. Ask the model for one turn. A turn is a list of tool calls. The loop runs
   that list one call at a time. Each call is verified on its own. The first
   failure, refusal, or `needs_human` stops the rest of that turn. The step
   record, the `step_finished` event, and the trajectory line list `skipped`
   (the calls that did not run) and `turn_stop` (`failure`, `refusal`, or
   `needs_human`). A later turn still runs unless the stop was `needs_human`
   or a successful `done`.
4. Approve. Quit, closing a window, submit or send, and every exec action
   call `approve(action)` when a hook is set. With no hook, `auto_deny=True`
   skips them and records `approval_denied`. A refused call is not executed,
   and later calls in that turn are not executed either.
5. Execute desktop actions through `Runtime.call_tool`. `select` is
   `set_value` on the server. The agent does not reimplement click, type,
   menus, or windows. `shell` and `python` do not go through `call_tool`.
6. Verify. The loop takes a fresh snapshot. A mutating desktop action that
   leaves the snapshot digest unchanged is a no-op (`verified` false). A tool
   error is `verified` false. A no-op or a tool error stops the rest of the
   turn. `shell` and `python` are verified by exit code 0, not by the
   snapshot.
7. Recover. Escalation retries the same action, or switches method only
   when the intent is the same target and payload. A click by ref may become
   another element with the same role and name, a coordinate click on that
   element, or Return when that element is focused. That Return is the click
   the approval gate already judged. A `type` or `set_value` may be retyped
   as the same text. A key is never replaced with a different key. A `type`
   is never turned into a bare click. A `menu` is never turned into focus.
   Escape is not sent as a backtrack. A missing or stale ref stays that
   click and its error. The retry level is per action and per target: it
   includes the chord, the typed text, and the menu path, so one no-op key
   does not escalate a different key. The step result keeps the original
   error next to any escalation outcome. If no same-intent method remains,
   the loop stops escalating and reports that error.
8. Stuck. A subgoal is the work since the last verified step. `stuck_steps`
   (default 6) and `stuck_time_s` (default 120) bound one tactic of that
   subgoal. When either budget runs out, the loop forces the next strategy
   in a fixed order: the keyboard path, then the vision fallback, then a
   re-plan. Each switch is one `strategy_switch` line in `trajectory.jsonl`.
   A switch retries the same action or a same-intent method. It does not
   change a key, turn typing into a click, turn a menu into focus, or
   rewrite a click that already has `x` and `y`. When every switch has been
   used and the budget runs out again, the run stops with `status` `stuck`,
   a reason that names the subgoal and the switches, and evidence (steps
   without progress, elapsed seconds, and the last action). That stop does
   not wait for `max_time`. Separately, if the same snapshot digest appears
   three times in the recent window, the loop emits a `stuck` event and
   tells the model to change strategy. After `max_replans` of those prompts
   (default 2) the run fails with `status` `failed` and `reason` `stuck`.
9. Done. `done` must carry an answer and 1 to 3 conditions. The loop
   re-checks them on a fresh snapshot before it accepts success. A failed
   check is rejected and the model continues, until a limit stops the run.
   When the goal saves or creates a file, one condition has to be
   `file_exists`, and `contains` when the goal names the text. A window
   title is not accepted as proof the file was written. The rule is the
   wording of the goal, not a named benchmark task. `contains` reads a
   plain file as text. For `.odt` and `.ods` it reads `content.xml`; for
   `.docx`, `word/document.xml`; for `.xlsx`, `xl/sharedStrings.xml` and
   the sheet XML. Those are zip files, and the raw bytes are not the check.

`stream` yields `observation`, `plan`, `step_started`, `action`,
`step_finished`, `needs_human`, `done`, `error`, and `stuck`. `cancel()` is
safe to call from another thread; the run stops before the next action with
`status` `cancelled` and `reason` `cancelled`.

`file_exists` conditions go through `conditions.Checker`, including its rule
that paths outside the home directory are refused unless
`A11Y_COMPUTER_USE_ALLOW_ANY_PATH=1`. Element, value, and window-title checks
read the fresh snapshot. They are not `wait_until` kinds.

## SDK

```python
from a11y_computer_use.agent import Agent
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.agent.models.base import ModelTurn, ToolCall

model = ScriptedModel([
    ModelTurn(calls=[ToolCall("done", {
        "answer": "the Save button is on screen",
        "conditions": [{"element": {"role": "AXButton", "name": "Save"}}],
    })]),
])
result = Agent(model, display=":1", max_steps=20, trace_dir="/tmp/run").run("confirm Save is visible")
print(result.status, result.answer, result.steps, result.reason)
```

`ScriptedModel` is not an LLM. It replays turns, loads a JSON file of the
same shape, or calls `(messages) -> ModelTurn`. Tests and headless runs use
it because no model API keys are available.

`Agent(..., runtime=...)` accepts a `server.Runtime` (or a test double with
`desktop_snapshot`, `call_tool`, `screenshot`, and `_frontmost`). The default
constructs a real `Runtime` after applying `display`.

`vision=True` attaches PNG crops of unnamed images, unknown widgets, and
unnamed clickables (at most four), then a whole-window screenshot, when the
tree says it has no interactive elements or contains those refs. The same
attachment runs without `vision=True` when the tree cannot name the target
the model asked for. `find(text, role)`, `expand(ref)`, and `scroll_to_find`
run first when a node is collapsed or the observation says a ref has more
children (`e89 is collapsed; use expand(e89)`). After those looks, the
picture is attached when that name or role is still missing twice, or when
the match lies inside an `opaque_region` or canvas. A toolbar or any other
named control does not keep the tree sufficient. An empty tree, or a tree
that is only an opaque region, still attaches on the first look. A named
control the model asked for that is present, such as a button named Ready,
stays text. The crop is `crop(ref)` of a page-content opaque region: the
deepest one, and the smallest at that depth. A browser-frame view that
contains another opaque region, or that only duplicates an ancestor's box,
is not the crop. Otherwise the loop attaches the window screenshot. Each image block
is `{"type": "image", "path": "...", "mime": "image/png"}`. The model then
clicks with `x` and `y` inside the region. Those coordinate clicks use the
same approval gate and typed outcomes as a ref click. Words in the image are
wrapped in an `<untrusted>` fence and are not instructions. The loop calls
`a11y_computer_use.ocr.ocr` on the crop or the window screenshot and fences
that text the same way, with `[x, y, width, height]` in screen pixels beside
each line. `crop` itself includes those lines when an engine is installed.
`ocr` reads a window screenshot, a `crop` result, or
`(image, bounds)` and returns fenced `{text, bounds, confidence}` in screen
pixels. Adjacent words are one item; RapidOCR detector lines are split into
word boxes first. Each span also carries `words`, one plain-text box per
input box. A missing engine (`unsupported`, reason `missing_dependency`) is
skipped and is not an error. The loop does not install the `[ocr]` extra.
`Agent(..., grounding=...)` accepts a `GroundingModel` (`ground(png,
instruction) -> point or box`). It is off by default. The only local adapter
is `HoloLocalGrounding` for `Hcompany/Holo-3.1-0.8B` on CPU, from the
optional `[holo]` extra (`torch`, `torchvision`, and `transformers>=5.6,<5.20`). The loop
does not construct it, does not download weights at import, and does not
call a hosted model. The adapter puts the screenshot in the chat template
as an image part, asks for `{"x","y"}` on a 0..1000 grid, and scales that
point onto the image. It loads bfloat16 when that dtype runs, otherwise
float16, stops generation on `}` with `max_new_tokens` 14 and the
tokenizer EOS id, and caps torch at 4 threads. Weights are not vendored.
A `+cpu` torch and the default PyPI torchvision wheel are different builds.
Importing that wheel raises `RuntimeError: operator torchvision::nms does not exist`.
The `[holo]` extra lists `torch`, `torchvision`, and `transformers`, and pip
cannot choose a wheel index from that extra. Install torch and torchvision
from the same index before the extra. For a CPU torch:

```bash
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
```

`HoloLocalGrounding.ground` turns that import or load failure into
`GroundingUnavailable` and includes this command. It does not download a
CUDA wheel.

When the model clicks a ref that is not in the snapshot, the loop resolves
that click only from the model's own call, before the payment, send, and
confirm gates, so those gates see the resolved target:

- An explicit `label`, with no `ref` and no `x`/`y`, matches an enabled
  control whose title is that exact name (case-insensitive, internal
  whitespace collapsed). One match becomes that ref. Zero matches, or more
  than one, is an error and nothing is clicked. A shorter or longer title
  is not a match.
- A missing `ref` matches an enabled control with the same role and the
  same exact name that ref had the last time it was in a snapshot. One
  match becomes that ref, and the step records `resolved_from` plus the
  same sentence on the tool result. Zero matches, or more than one, is a
  `stale_ref` error. A ref that was never seen is left as that click so
  the runtime can report `stale_ref`. The goal, the turn text, and a
  negative clause are not names.

A ref that is already in the tree is not rewritten. A click with `x` and
`y` and no `ref` is that coordinate click. It is not moved onto a tree
element, an OCR word, or a point from Holo. `double_click`, `right_click`,
`middle_click`, `triple_click`, and `left_click` use the same rules. The
name sets the button and count (`double_click` is a left click with count
2, `right_click` is the right button). An `x`/`y` on any of them stays on
that point. `scroll` at `x`/`y` stays that scroll. A name the loop does not
know is an error and is not turned into a click on a word from the goal.
`click` (and each of those gestures) with both a `ref` and `x` or `y` is
`invalid_arguments` and is not sent. Worker threads
(`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and
`torch.set_num_threads` when torch is already imported) are capped at 4
before fallback OCR and before a grounding suggestion. A pool already set
lower is not raised. Torch is not imported by this step. A model with
`supports_images` false stops as `needs_human` with `kind` `unsupported`
instead of continuing without the picture. A runtime with no `crop` method
still receives the window screenshot.

Before a coordinate click is sent, the loop looks again. It crops a 640 by
400 screen-pixel rectangle centred on the proposed point, clipped to the
image, and shows that crop at 2× (an unclipped crop is a 1280 by 800 PNG
whose screen bounds stay 640 by 400). A magenta crosshair marks the point.
The verifier accepts it or returns another point in the crop. A reply that
is not a point counts as accept. The click that is sent is still the
model's `x` and `y`. Look-again does not move that point, does not snap it
to a ref, and does not snap it to an OCR word. When the verifier's point is
more than 40 screen pixels from the proposed point, the loop records
`disagree` and looks again once at the full screenshot, at 1×. That second
look may only confirm the first point. A verifier point within 8 screen
pixels confirms it (`accept`): `refined` on that line is the model's point,
and `distance` is how far the verifier was. The click is not moved onto the
verifier's pixel. A second look farther than 8 screen pixels is
`unconfirmed`. That result is not a new point, and the click stays the
model's `x` and `y`. Adopting the farther point on a dense grid scored
16/24. A distance of 40 on the crop does not open the full-screen look.
When `grounding` is set, that local model is the
verifier (`HoloLocalGrounding` or a test `ScriptedGrounding`). Otherwise the
verifier is one short image turn on the main model, with no tools. The check
does not call a hosted or paid grounding service. `Agent(..., look_again=False)`, or
`A11Y_COMPUTER_USE_LOOK_AGAIN=0` when the argument is omitted, skips it.
A model with `supports_images` false and no grounding model skips it too,
and the trajectory records `skipped` `no_image_support`. A screenshot that
cannot be decoded is the same kind of skip (`no_screenshot`); the original
click still goes through the approval gate. The recorded snap names a tree
element or a word box under the verifier's point when one is there. It is
not applied. RapidOCR line boxes are not snap targets: only `word_results`
is used, and a span that still contains whitespace is split by character
count. A missing OCR engine does not fail the click. RapidOCR's ONNX
intra-op and inter-op thread counts are 4, and `OMP_NUM_THREADS`,
`MKL_NUM_THREADS`, and `OPENBLAS_NUM_THREADS` are capped at 4. The marked
crop, the decision (`accept`, `refine`, `unavailable`, `skipped`,
`disagree`, or `unconfirmed`), the distance, and the snap are lines in
`trajectory.jsonl` (`kind` `look_again`) and on the step. A disagreement
records the crop, then a second line for the full-screen look. The step
keeps `accept` when that look is within 8 screen pixels and `unconfirmed`
when it is farther. The click is the model's point either way.
The coordinate click already passed the approval gate, the payment stop,
and typed outcomes as the model's point. One crop check per coordinate
click, plus the full-screen look only when that check disagrees by more
than 40 pixels.

## CLI

```bash
a11y-agent run "goal" --model scripted:turns.json --display :1 --max-steps 30 --max-time 120 --stuck-steps 6 --stuck-time 120 --model-timeout 120 --trace-dir /tmp/trace --json
```

`--approve-policy deny` is the default: quit, close, send, delete, and
exec are skipped with no prompt. `--approve-policy allow-safe` runs quit and
window close and still skips send, delete, and exec.
`--approve-policy allow-all` runs those actions. A payment does not follow
that policy. A button or link named Pay, Place order, Buy, Purchase,
Checkout, Confirm payment, Complete order, and the like, or any button or
link on a checkout or payment page, stops the run as `needs_human` with
`kind` `payment`. `--allow-payments` is the opt-out: the click then goes
through `approve` (or `auto_deny`, or `allow-all`) instead of that stop.
`allow-all` alone does not submit a payment. A checkbox or toggle is never
a send action, including one labelled "Send usage statistics". `--approve`
prompts on stderr when it is a terminal, otherwise on the controlling tty,
and reads the answer from stdin. The prompt is never written to stdout, so
`--json` stays one JSON object. The prompt names the action, the target's
role, accessible name, window title, the page URL when the target is in a
browser, the reason (`payment`, `send`, `delete`, `quit`, or `exec`), and a
short JSON summary of the arguments. A card number or other secret in that
summary is `[REDACTED]` before the prompt is written. The name, window
title, URL, and summary are shown to the person as `untrusted:"..."` (or
`untrusted suspicious:"..."` when the text reads like an instruction).
Quotes, backslashes, newlines, and other control and format characters are
escaped, and a page-supplied `<untrusted` opener is escaped, so the page
cannot close the quote or insert another line. Role and reason are the
loop's tokens and are not marked untrusted. The `Action` passed to `approve` carries
the same fields unfenced (`role`, `target_name`, `window`, `url`,
`summary`, `reason`, `reason_kind`), trimmed, so a callback can read them.
The HTTP `approval_required` event and the agent MCP `pending_approvals`
entry (what `approve` answers) keep that page text inside `fence`
(`<untrusted nonce=…>`), which is what a model is shown. The desktop
confirmation question (MCP elicitation) uses the same quoted form. The
`confirmation_declined` detail carries the unfenced role, name, window, and
summary. Do not combine `--approve` with `--auto-deny`
or with a policy other than `deny`. `--allow-exec` exposes `shell` and
`python`. It is off by default, and it does not bypass the approval policy.

`--json` writes exactly one JSON object to stdout:

| Field | Meaning |
|---|---|
| `status` | `success`, `failed`, `stuck`, `timeout`, `needs_human`, or `cancelled` |
| `answer` | The `done` answer, or `""` |
| `steps` | Count of actions in `step_log` |
| `elapsed_s` | Wall clock for the run |
| `reason` | `done`, `max_steps`, `max_time`, `stuck`, `stuck: subgoal ...`, `no_action`, `cancelled`, `error: ...`, or the human message |
| `conditions` | `{condition, ok, detail}` from the last evidence check |
| `needs_human` | `{kind, message, ref, window}` or null. `kind` is `login`, `captcha`, `2fa`, `payment`, or `other` |
| `trace_dir` | Directory of the trace |
| `step_log` | One object per action |
| `evidence` | For `status` `stuck`, `{subgoal, switches, steps_without_progress, elapsed_s, last_action}`. For `status` `timeout`, `{limit_s, elapsed_s, where, action, abandoned}`. Otherwise null |

Each `step_log` entry has `index`, `action`, `target` (`ref`, `role`, `name`
from the snapshot the model acted on, before the action renumbered refs),
`args` (secrets redacted from that same pre-action element), `result`,
`error`, `verified`, `duration_s`, `skipped` (later calls in that turn that
did not run), and `turn_stop` (`failure`, `refusal`, `needs_human`, or null).

Exit codes: `0` success, `1` failed, `stuck`, or `timeout` (including `max_steps`, `max_time`),
`2` needs_human, `3` error or cancel.

SIGINT and SIGTERM during `a11y-agent run` cancel the run after the current
step. The cancel flag is checked again immediately before the next step is
recorded, so a signal that arrives after that step was chosen does not start
it. On Windows, Ctrl+C (`SIGINT`) and Ctrl+Break (`SIGBREAK`) do the same;
`SIGTERM` there ends the process without a Python handler, so it is not a
cooperative cancel. The handlers are installed before the agent is built and
stay installed until the process exits, including through shutdown. A signal
that arrives inside a blocking call (`sleep`, `select`, or a subprocess) still
ends the run as `cancelled` when that call raises `InterruptedError` or
`KeyboardInterrupt`. The trace write for the step is closed, and `--json`
prints one object with `status` `cancelled` and `reason` `cancelled`. The
process exits 3. A second cancel signal calls `os._exit(3)` and does not
print a traceback. On Windows a `SetConsoleCtrlHandler` callback does that
for the second Ctrl+C or Ctrl+Break, so the default console handler does not
terminate the process with `STATUS_CONTROL_C_EXIT` (`0xC000013A`).

`--model-timeout` is the limit for one model call, in seconds. The default is
120. It is not the time left in `--max-time`. The loop checks the run budget
before each step and again after the observation, before it calls the model.
A model call that times out after that budget is already spent ends
`reason` `max_time` with `status` `failed` (exit 1). A model timeout while the
budget remains is `error: ModelError: ...` (exit 3). The model call is not
cut short by `--max-time`; it keeps `--model-timeout`.

`--max-time` is a hard deadline for a driver or tool call. Each snapshot,
screenshot, crop, and tool call runs on a worker. When the budget is spent
and that call is still blocked, the loop abandons it and stops. It does not
wait for Chrome, AT-SPI, or any other driver to return. The result is
`status` `timeout`, `reason` `max_time`, and `evidence` with `limit_s`,
`elapsed_s`, `where` (`tool`, `observe`, or `screenshot`), `action`, and
`abandoned: true`. That return is within a few seconds of the limit.

`--display :N` sets `$DISPLAY` for the process before the runtime is created.
The SDK equivalent is `Agent(display=":N")`.

`--allowed-domains` and `--blocked-domains` are comma-separated hostnames or
origins (`example.com`, `https://example.com`, `file`). The same lists are
`Agent(allowed_domains=..., blocked_domains=...)`. A blocked host always
denies, including its subdomains (`evil.com` matches `a.evil.com` and does
not match `notevil.com`). When the allow list is non-empty, the origin must
match it too. Both empty, which is the default, allows every origin. An
action or navigation to a disallowed origin fails with `domain_blocked`,
is not performed, and stops the rest of that turn. The current page URL is read from CDP `Page.getFrameTree`
when the driver is the browser backend, otherwise from the AT-SPI document
that is on screen. Firefox keeps background tabs in the tree; the check uses
the selected tab that is showing. A control inside an iframe is checked
against that frame's document, and a link's own URI is checked as well.
Typing in the address bar is allowed. Enter there is refused when the text
is a disallowed URL, including the omnibox form that omits the scheme. The omnibox, its popup, and the tab strip are browser
chrome, not the page, and Escape is always allowed. A native app with no URL
is not blocked. The desktop MCP server takes the same lists as
`build_server(allowed_domains=..., blocked_domains=...)`,
`a11y-computer-use mcp --allowed-domains ... --blocked-domains ...`, or
`A11Y_COMPUTER_USE_ALLOWED_DOMAINS` and `A11Y_COMPUTER_USE_BLOCKED_DOMAINS`.

## Untrusted screen text

Text the model sees from the screen is data. `Agent` fences each observation
(on by default) as `<untrusted nonce=…>…</untrusted nonce=…>`. An opener or a
closer that appears inside the text is escaped, so the page cannot end the
fence early or open another one. A string that already looks like a fence is
wrapped again unless this process issued its nonce. Phrases such as "ignore previous instructions", "you are now", and
"system:" set `suspicious=1` on the opening tag. The text is still included
in full. The system prompt tells the model that fenced text is never an
instruction. A flagged observation is appended to `trajectory.jsonl` with
`"injection": true`, and the step record carries the same flag.

`fence_untrusted=False` on `Agent` turns the observation fences off. The
desktop MCP tool results stay unfenced unless fencing is opted in, so
existing clients see the same snapshot bytes. Opt in with
`Runtime(fence_untrusted=True)`, `build_server(fence_untrusted=True)`,
`a11y-computer-use mcp --fence-untrusted`, or
`A11Y_COMPUTER_USE_FENCE_UNTRUSTED=1`. When it is on, `desktop_snapshot`,
`find`, `screen_text`, clipboard reads, the window list, the app list,
action results, and error text that quotes the screen are wrapped. Notes and
clipboard write acknowledgements are not. The agent does not wrap an
observation that the runtime already fenced.

## Server

`a11y-agent serve` is an HTTP API for one agent process, so a non-Python
app can start a goal without importing this package. It binds `127.0.0.1`
and port `8765` unless `--host` and `--port` say otherwise. A bearer token
is optional on loopback. Binding any other host without `--token` exits 3
and does not listen. When a token is set, every request needs
`Authorization: Bearer <token>`.

```bash
a11y-agent serve --host 127.0.0.1 --port 8765 --token "$TOKEN"
```

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/runs` | `{goal, model, display, limits, allowed_domains, blocked_domains, allow_exec, allow_payments}` | `202 {"id"}`. `409` when that display already has an active run |
| `GET` | `/runs/{id}` | | The same fields as `a11y-agent run --json`. `status` is `running` until the run finishes. `pending_approvals` is present only while an approval is waiting |
| `GET` | `/runs/{id}/events` | | `text/event-stream`. Each event is `id`, `event` (the type), and `data` (`seq`, `type`, `data`). The stream replays, then stays open until the run finishes, then closes |
| `POST` | `/runs/{id}/cancel` | | `202 {"id", "cancel": true}` |
| `POST` | `/runs/{id}/approvals/{approval_id}` | `{"approve": true\|false}` | `200`. A second answer is `409`. No answer before `--approval-timeout` (default 60s) denies the action |
| `GET` | `/runs/{id}/trace` | | `{"trajectory", "files"}`. `trajectory` is the JSONL with UI text fenced |
| `GET` | `/runs/{id}/trace/{name}` | | One file from that directory (a screenshot, for example). Names that contain a slash or `..` are `404` |

`limits` is `{"max_steps", "max_time_s", "model_timeout_s"}`. `allowed_domains` and
`blocked_domains` are lists of strings or one comma-separated string. Both
are forwarded to `Agent`. `allow_exec` defaults to false. A field that is
not in that body, including a key inside `limits`, is `400` with
`invalid_arguments` and does not start a run. A cancel body with any field,
or an approve body with any field other than `approve`, is the same error
and does not cancel or answer.

One run may be active per display. An omitted display shares the `default`
slot. A second `POST /runs` for that display returns `409` with `run_id` of
the run that still holds it.

Quit, close, send, delete, and exec pause the run. The server emits an
`approval_required` event and waits. The event and each `pending_approvals`
item carry `approval_id`, the action `name`, `args` with secrets redacted,
`reason` (`payment`, `send`, `delete`, `quit`, or `exec`), and `target`.
`target` is `role`, `name`, `window`, `url` when the page has one, and
`reason`. The name, window, URL, and argument `summary` are trimmed and
fenced. Role and reason are not. `POST /runs` accepts `allow_payments`
(default false). While it is false, a payment click finishes the run as
`needs_human` and does not emit `approval_required`. The agent MCP
`run_goal` takes the same flag, and `get_run`'s `pending_approvals` is the
object `approve` answers. `approve: false`, a timeout, or a cancel denies
the action. The agent is constructed with an approve hook, so the CLI
`--auto-deny` default does not apply to these runs: a risky action waits
instead of being skipped immediately.

Every UI or page string is passed through `a11y_computer_use.untrusted.fence`
once before it leaves the server. That includes observations, window titles,
element names, action results, errors that quote the screen, condition
details, `needs_human` message and window, answers, and the trajectory JSONL.
A fence this process already issued is returned as that fence, so a step
result the agent wrapped is not wrapped again. A page-supplied
`<untrusted nonce=...>` block is not returned as-is: openers and closers are
escaped and the whole string is wrapped again, so a page cannot plant a fence
and leave a line outside it. A phrase such as "ignore previous
instructions" is marked `suspicious=1` and is not removed. Screenshot bytes
are not wrapped. Status tokens such as `done` and `cancelled` are not fenced.

```bash
TOKEN=devtoken
curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"goal":"Save the note","model":"scripted:turns.json","display":":1"}' \
  http://127.0.0.1:8765/runs
# {"id":"..."}
curl -N -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/runs/RUN_ID/events
```

`examples/agent_server_curl.sh` is that exchange. `examples/agent_client.js`
is a Node client with no npm packages: it `POST`s a run and prints each SSE
event. Neither script is run by tests or CI.

`a11y-agent mcp` is a second MCP server, on stdio, named `a11y-agent`. Its
tools are `run_goal`, `get_run`, `cancel_run`, and `approve`. They call the
same run store as the HTTP server. A busy display raises a tool error whose
text starts with `409`. This does not add or remove tools on
`a11y-computer-use mcp`.

## Actions

`click`, `type`, `key`, `set_value`, `select`, `scroll`, `crop`, `find`,
`expand`, `scroll_to_find`, `choose_file`, `snapshot`, `app`
(`launch`, `focus`, `quit`, `list`), `window` (`list`, `raise`, `focus`,
`move`, `resize`, `minimize`, `close`), `menu`, `wait`, `done`, `ask_human`.

`snapshot` takes optional `mode`. `full` (the default) redraws the current
window and keeps the refs of elements that are still there. `diff` repeats
the changes since the last observation the model was shown. A different
window is always the full tree.

`find` takes `text` and/or `role` (optional `editable`, `clickable`, `scope`)
and calls the runtime `find` on the tracked app. It searches the whole
document tree, not only the on-screen or budgeted nodes: on Linux a match
inside a table that starts below the viewport is scrolled into the snapshot,
and the ref is that cell rather than only the table that contains it.
`scroll_to_find` takes `text` and/or `role` and scrolls until that element
is on screen. `expand` takes `ref`. A collapsed node, or a menu or tree, is
pressed first. The result is the next contiguous page of that node's own
children, including children that do not fit on screen. The following
`expand` of that node resumes at the exact index where the previous page
ended, so a later listed name cannot skip the hole in front of it. Use these
before a coordinate click when the observation names a collapsed node or
`… N more` children.

`choose_file` takes an absolute `path` and an optional `action` of `open`
(the default) or `save`. The file chooser is already open (GTK, Qt, or
Chrome). The macro presses Ctrl+L, types that path once, and presses Return.
When the location entry is in the tree and the typed text is not the path,
one `set_value` writes it. Return is not pressed until the entry reads the
path back. The call succeeds only when the dialog has closed and the file
name shows outside the dialog. A dialog that stays open, a dialog that
closes without the file, and a check that cannot be read are failures, not
success. `open` requires the file to exist. `save` requires a file name and
the file need not exist yet. In a file chooser, always use `choose_file`;
never navigate folders. This is an agent action. It does not call the
desktop `file_dialog` tool, which stays unsupported on Linux.

`shell` and `python` are added to the model's tool list only when the agent
is constructed with `allow_exec=True` or the CLI is passed `--allow-exec`.
`shell` takes `command`. `python` takes `code` and runs it with a fresh
`python -I -c`. Both accept `cwd` and `timeout_s` (default 30 seconds, capped
at 120). Stored stdout and stderr are capped at 4000 characters and marked
`…[truncated N chars]`. A timeout kills the process group, sets
`exit_code` to null, and stops the rest of the turn.

`done` conditions, one to three of:

- `{"element": {"role": "AXButton", "name": "Save"}}`
- `{"value": {"ref": "e3", "equals": "hello"}}` or `{"value": {"name": "Note", "equals": "hello"}}`
- `{"window_title_contains": "Notes"}`
- `{"file_exists": "~/note.txt", "contains": "hello"}` (`contains` is optional; `.odt`, `.ods`, `.docx`, and `.xlsx` match the document text inside the zip)

`ask_human` takes `kind` and `message`. The agent also pauses on its own when
the snapshot contains a password field, an OTP or 2FA field, a card field, or
a captcha iframe. It does not guess credentials or submit payments.

## Approval and secrets

Risky actions are app quit, window close, menu items whose label is quit,
exit, log out, or sign out, clicks or menu items whose label pays, sends a
message, or deletes, and every `shell` or `python` call. An ordinary form
submit, a save, or a button such as Update cart is not risky: the loop runs
it. Typing into a password, OTP, or card field is not sent to `approve`.
The run stops with `needs_human` and the characters are not typed.

When no application is focused, the observation is a desktop overview: open
windows, running apps, and how to launch or focus one. It is not a permission
error, and permission errors do not tell the model to ask the user.

Exec has two gates. `allow_exec` defaults to false: the tools are omitted
from the schema, a call the model emits anyway is not run, and the audit
line says `exec_disabled`. When exec is enabled, the call still goes through
`approve`. A hook that returns false is `denied`. No hook and `auto_deny`
(the headless default) is `auto_denied`. The command runs only when the hook
returns true, or when `auto_deny` is false. `read` / `click` / `full` on the
desktop permission store are unchanged, and exec is not one of those tiers.

The existing permission store still applies. An ungranted app returns
`needs_permission` from the runtime; this package does not grant tiers by
itself.

## Traces

Each run writes a directory (the `--trace-dir` you pass, or a fresh temp
directory):

- `trajectory.jsonl` — one object per step: the observation text sent toward
  the model (truncated with `…[truncated N chars]` when it is huge), model
  name, request metadata, response text and tool calls, the action that ran,
  result, error, `verified`, recovery notes, a screenshot path, `skipped`,
  and `turn_stop`.
- `steps.jsonl` — the same records as `step_log`.
- `events.jsonl` — one line when a step starts (`kind` `step_started`,
  `index`, `action`), flushed before that step's tool call blocks.
  `steps.jsonl` is still written only when the step finishes.
- `exec-audit.jsonl` — one JSON line per exec attempt, appended and never
  rewritten. Each line has `timestamp`, `command`, `cwd`, `exit_code`,
  `output` (already truncated), `truncated`, `approval` (`approved`,
  `denied`, `auto_denied`, `exec_disabled`, or `rejected`), `error`, and
  `action` (`shell` or `python`). Denied and rejected attempts are logged
  with a null exit code and are not started. Approved attempts are logged
  when the process exits or times out.
- `step-NNNN.png` — a screenshot when `Runtime.screenshot` returns PNG bytes.
  A capture failure leaves the path null and does not fail the run.
- `observe-NNNN.png` — the whole-window shot attached when `vision=True`.
- `crop-<ref>-NNNN.png` — a vision crop of an unnamed or opaque ref.
- `crop-action-NNNN-<ref>.png` — the PNG returned when the model calls `crop`.

Card-number-shaped strings and secret argument names are stored as
`[REDACTED]`.

## Models

`Model.complete(messages, tools, timeout=...)` returns a `ModelTurn` of
`ToolCall`s plus optional text. `tools` is
`agent.actions.tool_schemas(allow_exec=...)`. The default call omits `shell`
and `python`.
After each turn the loop appends `assistant_message(turn)`, so the next
request replays the tool calls the model just made.

`make_model` lives in `a11y_computer_use.agent.models`. Specs, endpoints, and
environment variables are in `docs/agent-models.md`. A bad spec or a missing
key raises `ModelError`. HTTP clients are an optional extra
(`pip install 'a11y-computer-use[agent]'`). On Linux the desktop backend also
needs PyGObject, which the `linux` extra names. python-xlib is already a
core dependency on Linux. The AT-SPI typelib is a system package:

```bash
pip install 'a11y-computer-use[agent,linux]'
sudo apt install gir1.2-atspi-2.0 at-spi2-core python3-gi
```

`a11y-agent` checks those imports before the first model call when the
driver is Linux. A missing binding is status `failed`, reason `error: ...`,
exit code 3, not `needs_human`. This package's tests do not call
them: they construct `ScriptedModel` directly. No API key is stored in the
repo.

## Forward compatibility

Later milestones should not need to rename the types in this package.

- `ModelTurn.calls` is the list of tool calls for one turn. The loop runs
  them in order, stops at the first failure, refusal, or `needs_human`, and
  records which calls ran.
- `ReservedPermission.EXEC` (`"exec"`) is the permission for `shell` and
  `python`. It stays off unless `allow_exec=True`. Enabling it does not add
  tools to the MCP server.

## What is tested

Hermetic, on every OS, with `ScriptedModel` only (`tests/test_agent_core.py`,
`tests/test_agent_core_cli.py`, `tests/test_agent_exec.py`):

- event order, a successful multi-action turn, `max_steps`, `max_time`,
  `cancel`
- stop-on-failure, stop-on-refusal, and a no-op that does not run the later
  calls in the turn
- approve deny, `--approve-policy`, and the auto-deny default
- a desktop overview when nothing is focused, and a plain Submit that runs
- exec permission gating, audit-log fields, `exec_disabled`, `auto_denied`,
  hook denial, argument rejection, timeout, nonzero exit, and the output cap
- `--allow-exec` reaching `Agent`
- needs_human for a password field, an OTP field, a card field, a captcha
  iframe, and `ask_human`
- evidence-checked done, including a rejected condition and `file_exists`
- stuck method changes stay on the same intent (alternate ref, coordinate
  click of that element, Return only for a focused click) and failure with
  `reason` `stuck` after repeated screens
- a subgoal stuck budget switches keyboard, then vision, then re-plan, records
  each switch in the trace, keeps an explicit `x`/`y` click, and stops with
  `status` `stuck` before `max_time`
- a tool call that never returns is abandoned at `max_time`. The run ends
  with `status` `timeout`, `reason` `max_time`, and evidence, within a second
  of the limit (`tests/test_agent_core.py`)
- a missing ref stays a click error and does not send Escape or Return
- a no-op key does not turn the next chord into Return, a failed `type` does
  not become a click, and a failed `menu` does not become focus
- trajectory redaction, screenshots, and the `--json` schema and exit codes
- `vision=True` attaching element crops and then a window screenshot, and
  `display` setting `$DISPLAY`
- the Holo parser scaling a 0..1000 `{"x","y"}` reply onto image pixels,
  the chat template and the processor both receiving the image, and torch
  threads staying at most 4 (`tests/test_grounding.py`). A processor load
  that fails because torchvision is missing names that package. An import
  or a processor load that raises `RuntimeError: operator torchvision::nms
  does not exist` is `GroundingUnavailable` and names the CPU index
  (`pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision`).
  Those tests mock the stack. They do not download weights.
- click resolution (`tests/test_vision_order.py`): a missing ref is not
  chosen from the goal, including "Do not click Archive"; an explicit
  `x`/`y` stays on that point, including a `double_click` that must not
  move onto a control named Upload; `right_click`, `middle_click`,
  `triple_click`, `left_click`, and `scroll` at `x`/`y` stay on that point;
  `ref` together with `x`/`y` is `invalid_arguments`; a missing ref resolves
  only to the same role and exact name it last had; an ambiguous or inexact
  name is an error. Word boxes, the 200 by 40 snap helper, and the thread
  cap of 4 are still checked. No weights are downloaded. The 11 October
  2026 figures of 42 of 47 and 29 of 47 are not a measurement of this resolver.
- `crop` returning an image block on the tool message, and a failed crop
  staying a crop (no coordinate click, no Escape)
- untrusted fences: wrapping, escaping a forged closing tag, and the
  injection flag (`tests/test_untrusted.py`, `tests/test_agent_core.py`)
- domain allow and block rules, including a scripted run that sees injection
  text and does not perform the injected action
- `domain_blocked` before a navigation or a click is sent

Live, Linux only, under Xvfb (`tests/test_linux_live.py`): a local Chrome
page whose text contains an injection string and a link to
`https://blocked.example/`. The scripted agent fences that text and completes
the legitimate goal. Clicking the link and launching the blocked URL both
return `domain_blocked`. That test uses `ScriptedModel`, not a live LLM.
The document URL on that page comes from AT-SPI. The same file crops a red
GTK button named Swatch and checks the PNG size and dominant color.

Live, headless Chromium when a CDP endpoint is up
(`tests/test_browser.py::test_live_cdp_reads_document_url_and_link_href`):
`document_url` is the `file:` page from `Page.getFrameTree`, and
`element_url` on the link is `https://blocked.example/phish`. The browser
CI job starts that endpoint. Hermetic runs with nothing listening on the
default port skip the test.

Live on Linux, under Xvfb, with `ScriptedModel` (`tests/test_agent_live.py`):

- GTK save, wrong-step recovery, stuck Ping, Chrome form, needs_human pages,
  and CLI `--json` exit codes
- one GTK turn that sets the note, the path, and the format, then saves
- exec with `allow_exec` writing a file, and the same command denied by the
  approve hook
- an empty desktop's first observation, Files/Terminal grant aliases, a
  Submit button that is not sent for approval, a Pay now button named on the
  approve callback when `--allow-payments` is set, a checkout page that
  stops as `needs_human` payment by default, and a number field with a
  minimum and no maximum

Hermetic, on every OS, with `ScriptedModel` and `FakeRuntime`
(`tests/test_agent_http.py`, `tests/test_agent_mcp.py`):

- SSE event order and sequence numbers, and a finished `GET /runs/{id}` with
  the CLI JSON fields and no `pending_approvals`
- approval deny, a second answer returning 409, and a timeout that denies
- cancel during `model.complete`, before the click runs
- bearer auth, and refusing to bind `0.0.0.0` without a token (CLI exit 3)
- 409 when a display already has an active run, and a second display allowed
- UI text fenced on the event stream, the result, and the trajectory,
  including `suspicious=1` for an injection phrase
- a `scripted:` file with no model factory, and `allowed_domains` forwarded
  to `Agent`
- agent MCP tools exactly `run_goal`, `get_run`, `cancel_run`, `approve`,
  disjoint from the desktop MCP tool list

Live on Linux, under Xvfb, with `ScriptedModel` (`tests/test_agent_live.py`):

- the HTTP server started in-process, a `POST /runs` against the GTK fixture,
  the SSE stream followed until `done`, and the saved file matching the note

Not in this change:

- No MCP tools for `shell` or `python`. The desktop MCP tool list is unchanged.
- No live call to OpenAI, Anthropic, Gemini, xAI, Ollama, or a command
  provider. Those clients are in `a11y_computer_use.agent.models` and are
  tested with recorded HTTP fixtures, not from this loop. The system prompt
  tells a real model that fenced text is data; the scripted tests do not
  measure whether an LLM would obey it.
- The loop calls `a11y_computer_use.ocr.ocr` and skips a missing engine.
  Tests pass a stub. Element crops are `crop` and the vision fallback.
  Words in an attached image, OCR lines, and a grounding suggestion are
  fenced as untrusted screen data. Opaque-region markers drawn on the crop
  are not implemented. No hosted grounding API is called.
  `HoloLocalGrounding` loads only when the caller constructs it. The
  `[holo]` extra is not installed by the test run.
  Look-again is the later check described above: a 640 by 400 crop at 2×
  before a coordinate click is sent. It verifies or records a different
  point. It does not replace the model's `x`/`y` with another element.

`crop` is on the MCP server and in the reference loop's observation tools.
`a11y-computer-use agent` still uses the reference loop.
