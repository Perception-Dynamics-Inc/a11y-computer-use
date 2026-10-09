# a11y-agent

`a11y-agent` runs one goal against the desktop. It observes an accessibility
snapshot, asks a model for tool calls, and executes each call through
`server.Runtime.call_tool`. That is the same safety layer the MCP server
uses: permission tiers, the frontmost recheck, secure-field refusal, the
confirmation gate, and the audit log. This package does not add tools to
the MCP server.

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
3. Ask the model for one turn. A turn is a list of tool calls. M1 runs that
   list one call at a time.
4. Approve. Quit, closing a window, and submit or send actions call
   `approve(action)` when a hook is set. With no hook, `auto_deny=True` skips
   them and records `approval_denied`. The skipped call is not executed.
5. Execute through `Runtime.call_tool`. `select` is `set_value` on the
   server. The agent does not reimplement click, type, menus, or windows.
6. Verify. The loop takes a fresh snapshot. A mutating action that leaves the
   snapshot digest unchanged is a no-op (`verified` false). A tool error is
   `verified` false.
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

`--auto-deny` is the default: quit, close, and submit are skipped with no
prompt. `--approve` prompts on stdin and is the only interactive mode. Do not
pass both.

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
`args` (secrets redacted), `result`, `error`, `verified`, and `duration_s`.

Exit codes: `0` success, `1` failed (including `stuck`, `max_steps`, `max_time`),
`2` needs_human, `3` error or cancel.

`--display :N` sets `$DISPLAY` for the process before the runtime is created.
The SDK equivalent is `Agent(display=":N")`.

## Actions

`click`, `type`, `key`, `set_value`, `select`, `scroll`, `app`
(`launch`, `focus`, `quit`, `list`), `window` (`list`, `raise`, `focus`,
`move`, `resize`, `minimize`, `close`), `menu`, `wait`, `done`, `ask_human`.

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
exit, and clicks or menu items whose label is submit, send, or pay. Typing
into a password, OTP, or card field is not sent to `approve`. The run stops
with `needs_human` and the characters are not typed.

The existing permission store still applies. An ungranted app returns
`needs_permission` from the runtime; this package does not grant tiers by
itself.

## Traces

Each run writes a directory (the `--trace-dir` you pass, or a fresh temp
directory):

- `trajectory.jsonl` — one object per step: the observation text sent toward
  the model (truncated with `…[truncated N chars]` when it is huge), model
  name, request metadata, response text and tool calls, the action that ran,
  result, error, `verified`, recovery notes, and a screenshot path.
- `steps.jsonl` — the same records as `step_log`.
- `step-NNNN.png` — a screenshot when `Runtime.screenshot` returns PNG bytes.
  A capture failure leaves the path null and does not fail the run.
- `observe-NNNN.png` — the whole-window shot attached when `vision=True`.

Card-number-shaped strings and secret argument names are stored as
`[REDACTED]`.

## Models

`Model.complete(messages, tools, timeout=...)` returns a `ModelTurn` of
`ToolCall`s plus optional text. `tools` is `agent.actions.tool_schemas()`.
After each turn the loop appends `assistant_message(turn)`, so the next
request replays the tool calls the model just made.

`make_model` lives in `a11y_computer_use.agent.models`. Specs, endpoints, and
environment variables are in `docs/agent-models.md`. A bad spec or a missing
key raises `ModelError`. HTTP clients are an optional extra
(`pip install 'a11y-computer-use[agent]'`). This package's tests do not call
them: they construct `ScriptedModel` directly. No API key is stored in the
repo.

## Forward compatibility

M2 should not need to rename the types in this package.

- A model turn is already a list (`ModelTurn.calls`). M1 executes the list
  in order, and each call has its own verification, recovery, and step
  record. A later milestone can schedule several calls from one turn without
  changing the schema the model sees.
- `ReservedPermission.EXEC` (`"exec"`) is the permission for shell and
  Python. It is off. There is no exec tool in `tool_schemas()`, and the loop
  will not run a shell. When that tool exists it must stay off by default,
  call `approve` before it runs, and write the invocation to the audit log.
  `read` / `click` / `full` on the existing permission store are unchanged.

## What is tested

Hermetic, on every OS, with `ScriptedModel` only (`tests/test_agent_core.py`,
`tests/test_agent_core_cli.py`):

- event order, one-at-a-time calls, `max_steps`, `max_time`, `cancel`
- approve deny and the auto-deny default
- needs_human for a password field, an OTP field, a card field, a captcha
  iframe, and `ask_human`
- evidence-checked done, including a rejected condition and `file_exists`
- stuck method changes (alternate ref, coordinate click, keyboard) and
  failure with `reason` `stuck` after repeated screens
- stale-ref recovery that sends Escape and then completes
- trajectory redaction, screenshots, and the `--json` schema and exit codes
- `vision=True` attaching a window screenshot, and `display` setting
  `$DISPLAY`

Not in this change:

- No live GTK or Chrome agent run. That end-to-end test is a separate change.
- No live call to OpenAI, Anthropic, Gemini, xAI, Ollama, or a command
  provider. Those clients are in `a11y_computer_use.agent.models` and are
  tested with recorded HTTP fixtures, not from this loop.
- No OCR, no element-crop vision, no opaque-region markers.
- No shell or Python execution.

The existing MCP tool list is untouched. `a11y-computer-use agent` still uses
the reference loop.
