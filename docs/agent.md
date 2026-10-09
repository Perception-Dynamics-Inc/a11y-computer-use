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

`stream` yields `observation`, `plan`, `step_started`, `action`,
`step_finished`, `needs_human`, `done`, `error`, and `stuck`. `cancel()` is
safe to call from another thread; the run stops before the next action with
`reason` `cancelled`.

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

`vision=True` attaches a whole-window screenshot to the observation when the
tree says it has no interactive elements, or when it contains an unnamed
image or unnamed clickable. The image block is
`{"type": "image", "path": "...", "mime": "image/png"}`. Element crops and
opaque-region markers are not implemented. The library does not OCR.

## CLI

```bash
a11y-agent run "goal" --model scripted:turns.json --display :1 --max-steps 30 --max-time 120 --trace-dir /tmp/trace --json
```

`--auto-deny` is the default: quit, close, submit, and exec are skipped with
no prompt. `--approve` prompts on stdin and is the only interactive mode. Do
not pass both. `--allow-exec` exposes `shell` and `python`. It is off by
default, and it does not bypass `--auto-deny` or the approve hook.

`--json` writes exactly one JSON object to stdout:

| Field | Meaning |
|---|---|
| `status` | `success`, `failed`, or `needs_human` |
| `answer` | The `done` answer, or `""` |
| `steps` | Count of actions in `step_log` |
| `elapsed_s` | Wall clock for the run |
| `reason` | `done`, `max_steps`, `max_time`, `stuck`, `no_action`, `cancelled`, `error: ...`, or the human message |
| `conditions` | `{condition, ok, detail}` from the last evidence check |
| `needs_human` | `{kind, message, ref, window}` or null. `kind` is `login`, `captcha`, `2fa`, `payment`, or `other` |
| `trace_dir` | Directory of the trace |
| `step_log` | One object per action |

Each `step_log` entry has `index`, `action`, `target` (`ref`, `role`, `name`),
`args` (secrets redacted), `result`, `error`, `verified`, `duration_s`,
`skipped` (later calls in that turn that did not run), and `turn_stop`
(`failure`, `refusal`, `needs_human`, or null).

Exit codes: `0` success, `1` failed (including `stuck`, `max_steps`, `max_time`),
`2` needs_human, `3` error or cancel.

`--display :N` sets `$DISPLAY` for the process before the runtime is created.
The SDK equivalent is `Agent(display=":N")`.

`--allowed-domains` and `--blocked-domains` are comma-separated hostnames or
origins (`example.com`, `https://example.com`, `file`). The same lists are
`Agent(allowed_domains=..., blocked_domains=...)`. A blocked host always
denies, including its subdomains (`evil.com` matches `a.evil.com` and does
not match `notevil.com`). When the allow list is non-empty, the origin must
match it too. Both empty, which is the default, allows every origin. An
action or navigation to a disallowed origin fails with `domain_blocked` and
is not performed. The current page URL is read from CDP `Page.getFrameTree`
when the driver is the browser backend, otherwise from the AT-SPI document
URL. A link's own URI is checked the same way. A native app with no URL is
not blocked. The MCP server takes the same lists as
`build_server(allowed_domains=..., blocked_domains=...)`,
`a11y-computer-use mcp --allowed-domains ... --blocked-domains ...`, or
`A11Y_COMPUTER_USE_ALLOWED_DOMAINS` and `A11Y_COMPUTER_USE_BLOCKED_DOMAINS`.

## Untrusted screen text

Text the model sees from the screen is data. `Agent` fences each observation
(on by default) as `<untrusted nonce=…>…</untrusted nonce=…>`. A closing tag
that appears inside the text is escaped, so the page cannot end the fence
early. Phrases such as "ignore previous instructions", "you are now", and
"system:" set `suspicious=1` on the opening tag. The text is still included
in full. The system prompt tells the model that fenced text is never an
instruction. A flagged observation is appended to `trajectory.jsonl` with
`"injection": true`, and the step record carries the same flag.

`fence_untrusted=False` on `Agent` turns the observation fences off. The MCP
tool results stay unfenced unless fencing is opted in, so existing clients
see the same snapshot bytes. Opt in with
`Runtime(fence_untrusted=True)`, `build_server(fence_untrusted=True)`,
`a11y-computer-use mcp --fence-untrusted`, or
`A11Y_COMPUTER_USE_FENCE_UNTRUSTED=1`. When it is on, `desktop_snapshot`,
`find`, `screen_text`, and clipboard reads are wrapped. Notes and clipboard
write acknowledgements are not. The agent does not wrap an observation that
the runtime already fenced.

## Actions

`click`, `type`, `key`, `set_value`, `select`, `scroll`, `app`
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
- `{"file_exists": "~/note.txt", "contains": "hello"}` (`contains` is optional)

`ask_human` takes `kind` and `message`. The agent also pauses on its own when
the snapshot contains a password field, an OTP or 2FA field, a card field, or
a captcha iframe. It does not guess credentials or submit payments.

## Approval and secrets

Risky actions are app quit, window close, menu items whose label is quit or
exit, clicks or menu items whose label is submit, send, or pay, and every
`shell` or `python` call. Typing into a password, OTP, or card field is not
sent to `approve`. The run stops with `needs_human` and the characters are
not typed.

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
(`pip install 'a11y-computer-use[agent]'`). This package's tests do not call
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
- approve deny and the auto-deny default
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
- `vision=True` attaching a window screenshot, and `display` setting
  `$DISPLAY`
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

Live on Linux, under Xvfb, with `ScriptedModel` (`tests/test_agent_live.py`):

- GTK save, wrong-step recovery, stuck Ping, Chrome form, needs_human pages,
  and CLI `--json` exit codes
- one GTK turn that sets the note, the path, and the format, then saves
- exec with `allow_exec` writing a file, and the same command denied by the
  approve hook

Not in this change:

- No HTTP server and no MCP tools for `shell` or `python`. The existing MCP
  tool list is unchanged.
- No live call to OpenAI, Anthropic, Gemini, xAI, Ollama, or a command
  provider. Those clients are in `a11y_computer_use.agent.models` and are
  tested with recorded HTTP fixtures, not from this loop. The system prompt
  tells a real model that fenced text is data; the scripted tests do not
  measure whether an LLM would obey it.
- No OCR, no element-crop vision, no opaque-region markers.

`a11y-computer-use agent` still uses the reference loop.
