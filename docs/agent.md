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

1. Observe. `Runtime.desktop_snapshot` renders the pruned tree of the tracked
   app, or the frontmost app before one has been launched or focused.
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
7. Recover. A tool error retries once with another element of the same role
   and name, then sends Escape. A repeated no-op of the same action and
   target is forced onto the next method: alternate ref, then a coordinate
   click at the element's center, then the keyboard (Return, or `type` when
   the action was entering text).
8. Stuck. If the same snapshot digest appears three times in the recent
   window, the loop emits a `stuck` event and tells the model to change
   strategy. After `max_replans` of those prompts (default 2) the run fails
   with `reason` `stuck`.
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
tree says it has no interactive elements or contains those refs. The model
can also call `crop(ref)` with optional `padding` and `scale`. Each image
block is `{"type": "image", "path": "...", "mime": "image/png"}`. The library
does not OCR or recognize the pixels. Opaque-region markers (drawn labels on
the crop) are not implemented. A runtime with no `crop` method still receives
the window screenshot.

## CLI

```bash
a11y-agent run "goal" --model scripted:turns.json --display :1 --max-steps 30 --max-time 120 --model-timeout 120 --trace-dir /tmp/trace --json
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
| `status` | `success`, `failed`, `needs_human`, or `cancelled` |
| `answer` | The `done` answer, or `""` |
| `steps` | Count of actions in `step_log` |
| `elapsed_s` | Wall clock for the run |
| `reason` | `done`, `max_steps`, `max_time`, `stuck`, `no_action`, `cancelled`, `error: ...`, or the human message |
| `conditions` | `{condition, ok, detail}` from the last evidence check |
| `needs_human` | `{kind, message, ref, window}` or null. `kind` is `login`, `captcha`, `2fa`, `payment`, or `other` |
| `trace_dir` | Directory of the trace |
| `step_log` | One object per action |

Each `step_log` entry has `index`, `action`, `target` (`ref`, `role`, `name`
from the snapshot the model acted on, before the action renumbered refs),
`args` (secrets redacted from that same pre-action element), `result`,
`error`, `verified`, `duration_s`, `skipped` (later calls in that turn that
did not run), and `turn_stop` (`failure`, `refusal`, `needs_human`, or null).

Exit codes: `0` success, `1` failed (including `stuck`, `max_steps`, `max_time`),
`2` needs_human, `3` error or cancel.

`--model-timeout` is the limit for one model call, in seconds. The default is
120. It is not the time left in `--max-time`. The loop checks the run budget
before each step and again after the observation, before it calls the model.
A model call that times out after that budget is already spent ends
`reason` `max_time` (exit 1). A model timeout while the budget remains is
`error: ModelError: ...` (exit 3).

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
| `POST` | `/runs` | `{goal, model, display, limits, allowed_domains, blocked_domains, allow_exec}` | `202 {"id"}`. `409` when that display already has an active run |
| `GET` | `/runs/{id}` | | The same fields as `a11y-agent run --json`. `status` is `running` until the run finishes. `pending_approvals` is present only while an approval is waiting |
| `GET` | `/runs/{id}/events` | | `text/event-stream`. Each event is `id`, `event` (the type), and `data` (`seq`, `type`, `data`). The stream replays, then stays open until the run finishes, then closes |
| `POST` | `/runs/{id}/cancel` | | `202 {"id", "cancel": true}` |
| `POST` | `/runs/{id}/approvals/{approval_id}` | `{"approve": true\|false}` | `200`. A second answer is `409`. No answer before `--approval-timeout` (default 60s) denies the action |
| `GET` | `/runs/{id}/trace` | | `{"trajectory", "files"}`. `trajectory` is the JSONL with UI text fenced |
| `GET` | `/runs/{id}/trace/{name}` | | One file from that directory (a screenshot, for example). Names that contain a slash or `..` are `404` |

`limits` is `{"max_steps", "max_time_s"}`. `allowed_domains` and
`blocked_domains` are lists of strings or one comma-separated string. Both
are forwarded to `Agent`. `allow_exec` defaults to false.

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

`click`, `type`, `key`, `set_value`, `select`, `scroll`, `crop`, `app`
(`launch`, `focus`, `quit`, `list`), `window` (`list`, `raise`, `focus`,
`move`, `resize`, `minimize`, `close`), `menu`, `wait`, `done`, `ask_human`.

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
- stuck method changes (alternate ref, coordinate click, keyboard) and
  failure with `reason` `stuck` after repeated screens
- stale-ref recovery that sends Escape and then completes
- trajectory redaction, screenshots, and the `--json` schema and exit codes
- `vision=True` attaching element crops and then a window screenshot, and
  `display` setting `$DISPLAY`
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
- No OCR. Element crops are `crop` and the vision hook. Opaque-region
  markers are not implemented.

`crop` is on the MCP server and in the reference loop's observation tools.
`a11y-computer-use agent` still uses the reference loop.
