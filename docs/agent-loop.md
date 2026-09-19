# The reference agent loop

`a11y-computer-use agent --task "..."` runs a complete observe, plan, act, verify loop with a model of your choice. It exists so a newcomer can see a11y-computer-use work end to end in one command, and so cu-arena has a planner harness that treats every observation strategy the same way. The loop lives in `a11y_computer_use/agent.py`; the planners live in `a11y_computer_use/providers.py`.

## What it does

1. Takes a full `desktop_snapshot` of the target app (a tab on the browser backend) and hands the task plus the pruned tree to the planner.
2. The planner answers with tool calls drawn from the MCP tool surface (`desktop_snapshot`, `find`, `click`, `set_value`, `act`, and the rest) plus one extra tool, `done(summary, success)`.
3. Every call runs through `server.Runtime.call_tool`, the same gated path the MCP server uses. Permission tiers, the frontmost recheck, secure-field refusal, the confirmation gate, and the JSONL audit log apply unchanged.
4. Tool results go back to the planner as tool results. Errors are results too: a refusal, a bad argument, or an unknown tool never crashes the loop. A `stale_ref` error carries a fresh snapshot in the same result, so the planner can act on current refs without an extra round trip.
5. `click` and `act` run with `verify=true` by default, so the post-action diff (the Effect Receipt) rides along with the result.
6. The loop ends when the planner calls `done`, when `--max-steps` planner turns have run, when the planner produces no tool call twice in a row, or when the provider fails.

The tool list the planner sees is derived from the same registrations `build_server` makes for the active driver (`server.tool_specs`), so browser-only tools such as `console` appear exactly when the backend serves them.

## Quickstart

```bash
# Browser backend: any Chromium with a debugging port
google-chrome --headless=new --remote-debugging-port=9222 about:blank &
A11Y_COMPUTER_USE_DRIVER=browser A11Y_COMPUTER_USE_CDP_ENDPOINT=http://127.0.0.1:9222 \
  a11y-computer-use agent --grant full --task "Type hello in the Name field and press Submit"

# macOS: the frontmost app, or --app by bundle id or name
a11y-computer-use agent --app TextEdit --grant full --task "Type a two-line greeting"

# Remote machine: the planner stays here, the server runs there (its grants, its audit log)
a11y-computer-use agent --mcp-command "ssh vm a11y-computer-use mcp" --app krita --task "Draw a circle"


# Pick a planner explicitly
a11y-computer-use agent --provider anthropic --task "..."
a11y-computer-use agent --provider openai --model gpt-5 --task "..."
a11y-computer-use agent --provider claude-cli --task "..."
```

`--grant full` stores a permission tier for the target app in the normal permission store, the same grant the MCP server would need. Without a grant the first observation returns `needs_permission` and the planner is told to stop. `--json` prints the full result, including every step and the planner token usage; the human-readable step log goes to stderr.

When the CLI runs in a terminal it answers the confirmation gate itself: a plausibly irreversible click prints the prompt and waits for `y`. Without a terminal such actions fail safe with `confirmation_declined`, exactly as under an MCP host without elicitation.

## Providers

| Provider | `--provider` | Credentials | Notes |
|---|---|---|---|
| Anthropic Messages API | `anthropic` | `ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN`; optional `ANTHROPIC_BASE_URL` | Default model `claude-opus-5`; native tool use; the conversation is append-only and context is bounded server-side with the `clear_tool_uses` context-editing strategy (falls back to plain requests if the API rejects it) |
| OpenAI-compatible chat completions | `openai` | `OPENAI_API_KEY`; `OPENAI_BASE_URL` for other servers | `--model` is required. Points at OpenAI, Ollama, vLLM, OpenRouter, or any compatible endpoint; images from `screenshot` are attached as image parts |
| Claude Code CLI | `claude-cli` | A logged-in `claude` command, no key | One stateless `claude -p` call per turn, built-in tools disabled, the model replies with one JSON action. Cannot view images, so screenshots are described rather than seen |
| Scripted | (Python only) | none | A fixed list of turns for tests and demos; used by the test suite and the live browser test |

With no `--provider`, the CLI reads `A11Y_COMPUTER_USE_PROVIDER`, then picks the first backend the environment supports: an Anthropic key, then an OpenAI key or base URL, then the `claude` command.

All providers use the standard library only (`urllib`, `subprocess`), so the loop adds no dependency to the package.

### Ollama recipe

```bash
ollama serve &
ollama pull llama3.1
OPENAI_BASE_URL=http://localhost:11434/v1 \
  a11y-computer-use agent --provider openai --model llama3.1 --task "..."
```

A local model works because the observation is text with refs, not pixels. This recipe was not run here; the OpenAI provider is verified against a fake transport (see "Verification status" below).

## Token accounting

Each step records the planner's token usage for the turn that produced it (a turn with several tool calls counts its tokens once, on the first call). The loop also writes `agent_step` and `agent_run` rows to the audit log with `planner_input_tokens` and `planner_output_tokens`, and `a11y-computer-use bench audit` sums them next to the observation cost it already reports, so one report shows what the model read and what the tools cost.

`input_tokens` includes cached input. For `claude-cli` that means the Claude Code system prompt the CLI adds on every call (about 23k tokens per turn in the run below), which is why its input numbers are far higher than the observation itself.

## Bounded context

The planner always sees the newest observation in full. Older observations (the initial snapshot, `desktop_snapshot`, `find`, `screenshot`, `scroll_to_find`, `console`, `network` results, and the snapshot attached to a `stale_ref` error) collapse to a one-line placeholder once a newer one exists, and images outside the newest observation are dropped. This applies to providers that allow history edits (`openai`, `claude-cli`, `scripted`).

The Anthropic provider never edits earlier turns, because current Claude models bind later thinking blocks to the exact conversation prefix. It replays its own assistant content verbatim and asks the API to clear old tool results server-side instead.

## Embedding the loop

```python
from a11y_computer_use import agent, providers, safety, server

runtime = server.Runtime()                       # driver from A11Y_COMPUTER_USE_DRIVER or the OS
provider = providers.get_provider()              # or AnthropicProvider(), OpenAIProvider("gpt-5"), ...
app = runtime._frontmost()
runtime.store.set_tier(app, safety.Tier.FULL)   # your product decides this policy

result = agent.run_task("Type hello and press Submit", runtime, provider, app=app,
                        max_steps=20, on_step=lambda s: print(s.tool, s.ok))
print(result.success, result.summary, result.usage)
```

`examples/agent_task.py` is the runnable version. `run_task` accepts any object with `name`, `history_edits_ok`, and `plan(messages, tools, system=...)`, so a custom planner is a small class.

## Live result

Run on 2026-09-02 on this Mac against headless Chrome 152 over the browser backend, with the `claude-cli` provider (the local Claude Code CLI, no API key). The page was a data: URL with a heading, a `Name` text field, and a `Submit` button whose click sets the document title to `SUBMITTED:` plus the field value.

```
$ A11Y_COMPUTER_USE_DRIVER=browser A11Y_COMPUTER_USE_CDP_ENDPOINT=http://127.0.0.1:9333 \
    a11y-computer-use agent --provider claude-cli --grant full --max-steps 8 --json \
    --task "Type hello in the text field and press the submit button"
granted 7EF807899265B7A84509FBF92739E493 tier full
[step 1] act {"steps": [{"do": "click", "ref": "e7"}, {"do": "type", "text": "hello"},
             {"do": "click", "ref": "e8"}], "verify": true}
         -> ok: {"steps": [{"i": 0, "do": "click", "ok": true, "result": "clicked e7 (AXTextField 'Name ')"},
                           {"i": 1, "do": "type", "ok": true, "result": "typed 5 characters"},
                           {"i": 2, "do": "click", "ok": true, "result": "clicked e8 (AXButton 'Submit')"}],
                 "effect": "[snap-4 <- snap-1] +0 -0 ~2
                            ~ e1 webarea [title: ->SUBMITTED:hello, focused: False->True]
                            ~ e7 textfield [value: None->hello, focused: False->True]"}
            (17 ms, 28697 in/76 out tok)
[step 2] done {"summary": "Typed 'hello' into the Name field and clicked Submit; the page title
              changed to 'SUBMITTED:hello', confirming the submission.", "success": true}
```

| Measure | Value |
|---|---|
| Planner turns | 2 (one `act` batch of three steps, then `done`) |
| Wall time | 17.9 s, almost all of it in the two `claude -p` calls |
| Planner tokens | 57,793 in (including the CLI's cached system prompt), 149 out |
| Coordinates used | none; the planner acted on refs e7 and e8 |
| Evidence | the Effect Receipt showed the title change and the field value; a CDP `Runtime.evaluate` afterwards read `document.title == "SUBMITTED:hello"` and the field value `hello` |

The planner chose the batched `act` tool on its own, which is the round-trip-saving path the tool description recommends.

## Verification status

- Live on headless Chrome, this machine: the `claude-cli` run above, and the opt-in test `tests/test_agent.py::test_live_browser_agent_loop_fills_and_clicks_by_ref` (scripted planner, `set_value` then `click`, checked through CDP). The same test runs in the browser CI job whenever the endpoint is up.
- Hermetic, every OS: the loop against a fake driver and a scripted planner (`tests/test_agent.py`), and the Anthropic, OpenAI, and Claude CLI providers against fake transports (`tests/test_providers.py`), pinning the request bodies sent and the responses parsed.
- Not run here: a real Anthropic or OpenAI endpoint, and the Ollama recipe. Their providers are transport-tested only.

## Limits

- `--max-steps` bounds planner turns, not tool calls; a turn may carry several calls on the Anthropic and OpenAI providers, while `claude-cli` emits one action per turn.
- The `claude-cli` provider cannot see images. Prefer refs; `screenshot` results reach it as text only.
- HTTP providers retry connection errors and retryable statuses three times with backoff; anything else ends the run with `stopped == "provider_error"`.
- A model refusal (`stop_reason == "refusal"` on Anthropic) ends the run the same way, with the refusal category in the summary.
- The loop does not compact the conversation into a summary when it grows past the bounded observations; long tasks should be split.

## Live runs on macOS

Synthetic input does not reset the idle timer, so a long run on an unattended Mac ends with a locked screen and every capture failing. `a11y-computer-use agent` and `mission run` therefore hold `caffeinate -dimsu` for their duration (`A11Y_COMPUTER_USE_KEEP_AWAKE=0` opts out) and refuse to start while the screen is locked or no display is active, with a structured `unsupported` message instead of a traceback. Turn display sleep off or leave the keep-awake default on, and do not use the machine while a run drives it: the same-window recheck refuses clicks under whatever window you bring to the front. Observations of a granted app (`desktop_snapshot`, `screen_text(app=X)`, `window list app=X`, and the automatic OCR escalation) are gated against that app, so an ungranted terminal in front does not block them; the result says when the app is not frontmost. `app launch` waits `A11Y_COMPUTER_USE_LAUNCH_WAIT_S` seconds (default 60) for the first window.

## Refs after a live reorder

A ref is issued for an element with a title. When the ref is used later, the
element is re-resolved against a fresh tree, and the title is binding: the ref
follows the element wherever it moved, and it never resolves onto whatever
element now occupies its old position. This closes the failure the incident
gauntlet exposed, where a virtualized list refreshed and reordered between the
observation and the click, and the click landed on the row that had slid into
the slot.

What the planner sees when the titled element is gone from the tree:

```text
stale_ref: e105 (AXRow 'email-router production') is no longer in the tree under
that title; the AXRow at that position is now 'config-sync production'. The
list may have reordered: use find(text=...) or scroll_to_find to locate it
again rather than clicking the slot
detail: {"reason": "title_changed", "candidates": [..., {"ref": "e105",
"title": "config-sync production", "at_old_position": true}]}
```

Rules, in order:

- A developer-assigned stable id still wins for controls that may relabel
  (a button going from "Submit" to "Sending") when no other live element
  carries the old label. For rows, cells, items, links, and static text the
  label is binding even against a matching id, because virtualized lists hand
  out slot-based ids that outlive the row's content.
- Titles compare case- and whitespace-insensitively; a title truncated with an
  ellipsis matches a live title that starts with the same stem of at least
  three characters.
- Text-like elements without a title (static text, headings) are bound by
  their value the same way.
- Untitled elements keep the positional ladder: same path, then nearest bounds
  within 400 px.

After a `title_changed` result, call `find(text=...)` or `scroll_to_find` and
act on the ref it returns.
