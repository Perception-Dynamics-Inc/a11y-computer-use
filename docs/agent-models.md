# Agent models

`a11y_computer_use.agent.models` is the model side of the computer-use agent. A model takes the conversation and the tool schemas and returns one `ModelTurn`: tool calls, assistant text, the raw provider response, and token usage. The agent loop (separate from this package) decides which calls to run.

HTTP providers speak to OpenAI-compatible chat completions (OpenAI, xAI, Ollama, vLLM), the Anthropic Messages API, and Gemini `generateContent`. They use `httpx` and no vendor SDK. Install them with the optional extra:

```bash
pip install 'a11y-computer-use[agent]'
```

On Linux, install the desktop backend in the same command. `gi` (PyGObject)
and `Xlib` (python-xlib) are not pulled in by `[agent]` alone:

```bash
pip install 'a11y-computer-use[agent,linux]'
sudo apt install gir1.2-atspi-2.0 at-spi2-core python3-gi
```

The core install does not depend on `httpx`. `ScriptedModel` and `CommandModel` use only the standard library.

Nothing in the test suite calls a live model. Provider tests replay recorded request and response fixtures through `httpx.MockTransport`. No API key is stored in the repo.

## Spec strings

`make_model` accepts a `Model` instance (returned unchanged) or a spec string. The provider prefix is case-insensitive. The model id is not: everything after the first colon is the model id, so Ollama tags keep their colon.

| Spec | Client | Default endpoint |
|---|---|---|
| `openai:gpt-4.1` | Chat completions | `https://api.openai.com/v1/chat/completions` |
| `xai:grok-4` | Chat completions | `https://api.x.ai/v1/chat/completions` |
| `ollama:llama3.1` | Chat completions | `http://127.0.0.1:11434/v1/chat/completions` |
| `ollama:llama3.1:8b` | Chat completions | same; the model id is `llama3.1:8b` |
| `vllm:meta-llama/Llama-3.1-8B-Instruct` | Chat completions | `http://127.0.0.1:8000/v1/chat/completions` |
| `anthropic:claude-sonnet-4-5` | Messages API | `https://api.anthropic.com/v1/messages` |
| `gemini:gemini-2.5-flash` | generateContent | `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent` |
| `command:/path/to/script.py` | Local subprocess | no network |
| `scripted:/path/to/turns.json` | Recorded turns | no network |

```python
from a11y_computer_use.agent.models import make_model

model = make_model("openai:gpt-4.1")
turn = model.complete(messages, tools, timeout=60)
```

`model.name` is the provider id (`openai`, `xai`, `ollama`, `vllm`, `anthropic`, `gemini`, `command`, `scripted`). `model.model` is the model id for the HTTP providers. `model.supports_images` is `True` for every built-in model: image blocks are forwarded, not dropped.

Point `openai:` at another OpenAI-compatible server with `OPENAI_BASE_URL` (vLLM, an OpenAI proxy, a local gateway). `ollama:` and `vllm:` are the same protocol with their own default base URL and key variable, so a process can talk to Ollama and OpenAI at the same time.

## Environment variables

The key is read when the model is constructed. A constructor `api_key` or `base_url` overrides the environment. A missing key raises `ModelError` naming the variable, before any HTTP request, when the base URL is the vendor host. Ollama, vLLM, and any spec whose base URL is not the vendor host run without a key. If a key is set, it is sent; it is never written into the URL or into `ModelError` text.

| Provider | Key variable | Base URL variable | Key required when |
|---|---|---|---|
| `openai` | `OPENAI_API_KEY` | `OPENAI_BASE_URL` | host is `api.openai.com` |
| `xai` | `XAI_API_KEY` | `XAI_BASE_URL` | host is `api.x.ai` |
| `ollama` | `OLLAMA_API_KEY` (optional) | `OLLAMA_BASE_URL`, else `OLLAMA_HOST` | never |
| `vllm` | `VLLM_API_KEY` (optional) | `VLLM_BASE_URL` | never |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` | host is `api.anthropic.com` |
| `gemini` | `GEMINI_API_KEY`, else `GOOGLE_API_KEY` | `GEMINI_BASE_URL` | host is `generativelanguage.googleapis.com` |

`OLLAMA_HOST` may be `127.0.0.1:11434` or a full URL. A missing scheme becomes `http://`, and a missing `/v1` suffix is added, because Ollama's OpenAI-compatible routes live under `/v1`. `OLLAMA_BASE_URL`, when set, is used as given (trailing slash stripped) and is not rewritten.

Base URL shape:

* OpenAI-compatible bases include the `/v1` prefix. The client posts to `{base}/chat/completions`.
* Anthropic's default base is `https://api.anthropic.com`. A base that already ends in `/v1` is posted to `{base}/messages`; otherwise to `{base}/v1/messages`.
* Gemini's default base is `https://generativelanguage.googleapis.com/v1beta`. The client posts to `{base}/models/{model}:generateContent`. Do not include `/models/...` in the base. A `models/` prefix on the model id is stripped.

Auth headers: `Authorization: Bearer` for OpenAI-compatible APIs, `x-api-key` plus `anthropic-version: 2023-06-01` for Anthropic, `x-goog-api-key` for Gemini.

## Messages

```python
from a11y_computer_use.agent.models import Message, ToolCall, ModelTurn, assistant_message

Message(role="system", content="You drive the computer by element ref.")
Message(role="user", content="Click Save.")
Message(role="assistant", content=[
    {"type": "text", "text": "Clicking Save."},
    {"type": "tool_call", "id": "call_1", "name": "click", "args": {"ref": "e2"}},
])
Message(role="tool", content="clicked e2", tool_call_id="call_1", name="click")
```

`content` is a string or a list of blocks. A string is one text block.

| Block | Fields |
|---|---|
| text | `{"type": "text", "text": "..."}` |
| image | `{"type": "image", "path": "/tmp/screen.png"}` or `{"type": "image", "b64": "...", "mime": "image/png"}` |
| tool call | `{"type": "tool_call", "name": "click", "args": {...}, "id": "call_1"}` |

`b64` wins when both `b64` and `path` are set. `mime` overrides the suffix guess (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`; default `image/png`). A missing image file raises `ModelError` and no request is sent. `args` may also be spelled `input` (Anthropic) on the way in; providers always emit `args` on `ToolCall`.

`assistant_message(turn)` builds the assistant `Message` to append before the next call. Gemini attaches `thoughtSignature` to function-call parts. The helper copies each one onto the matching tool-call block as `thought_signature`, and the Gemini provider sends it back on that part. Dropping it makes the next Gemini request fail validation. Other providers ignore the field.

Tool results are a separate `role="tool"` message, not a block inside the user message. `tool_call_id` is required. Gemini also requires `name` (the function that was called) because `functionResponse` carries the name. OpenAI and Anthropic send `name` when it is set.

What goes on the wire:

* **OpenAI-compatible.** System, user, and assistant messages keep those roles. Tool calls become `tool_calls` with `function.arguments` as a JSON string. Tool results are `role: "tool"`. That role is text-only, so an image on a tool result is a following user message (`image_url` data URL) after the tool message. User images are `image_url` data URLs on the user message.
* **Anthropic.** System text is the top-level `system` string, not a message. Assistant tool calls are `tool_use` blocks (`input` is the object). Tool results are `tool_result` blocks on a user message, grouped when several tool messages are consecutive. Images, including images inside a tool result, are base64 `source` blocks. `max_tokens` defaults to 4096.
* **Gemini.** System text is `systemInstruction`. Assistant messages use role `model`. Tool calls are `functionCall` parts (`args` is the object, `id` is kept). Tool results are `functionResponse` parts (`response` is `{"result": "<text>"}`); images are sibling `inlineData` parts. Consecutive user-role parts, including several tool results, are merged into one content item.

`tools` are JSON-schema function definitions, the shape `tool_schemas()` produces:

```python
{
    "name": "click",
    "description": "Click an element ref from the latest snapshot.",
    "parameters": {
        "type": "object",
        "properties": {"ref": {"type": "string"}},
        "required": ["ref"],
    },
}
```

`input_schema` is accepted as an alias of `parameters`. An OpenAI wrapper `{"type": "function", "function": {...}}` is accepted too. Each provider rewrites the list into its own tool declaration (`function` + `parameters`, Anthropic `input_schema`, Gemini `functionDeclarations`).

`ModelTurn.usage` is `{"input_tokens": int, "output_tokens": int}` when the provider reported usage, otherwise `None`. `ModelTurn.raw` is the decoded response body (or the command's `raw` object).

## Retries, timeouts, and errors

`complete(..., timeout=None)` uses a 120 second HTTP timeout. Pass `timeout` to override it for that call. The same argument is the subprocess timeout for `CommandModel` (default 30 seconds) and must be positive. The agent loop passes its own per-call limit (`--model-timeout`, default 120 seconds), not the seconds left in the run budget.

HTTP 408, 429, and every 5xx status are retried, as are timeouts (`httpx.TimeoutException`). Other connection errors and 4xx statuses (except 408) are not retried. The client makes up to 3 attempts. Between attempts it sleeps 0.25s, then 0.5s, capped at 8s. A numeric `Retry-After` header replaces that delay, still capped at 8s. The final failure is `ModelError`. `ModelError.status` is the HTTP status when there was a response, and `None` for a timeout, a missing key, a bad body, or a local command. The response text is included, truncated. The API key is not.

A 200 body that is not a JSON object, an empty `choices` / `candidates` list, a Gemini `promptFeedback.blockReason`, or tool-call arguments that are not a JSON object also raise `ModelError`.

## Scripted models

`ScriptedModel` is the offline stand-in used by tests and demos.

```python
from a11y_computer_use.agent.models import ModelTurn, ScriptedModel, ToolCall

model = ScriptedModel([
    ModelTurn(calls=[ToolCall("click", {"ref": "e2"}, "c1")], text="Clicking."),
    ModelTurn(calls=[ToolCall("done", {"answer": "Saved."}, "d1")], text="done"),
])
```

A list entry may be a `ModelTurn`, a dict of that shape, or a callable `(messages) -> ModelTurn`. A single callable passed as the source is invoked on every `complete`, so the script can read tool results and answer differently:

```python
def react(messages):
    if any(message.role == "tool" for message in messages):
        return ModelTurn(calls=[ToolCall("done", {"answer": "ok"}, "d1")], text="done")
    return ModelTurn(calls=[ToolCall("click", {"ref": "e1"}, "c1")])

model = ScriptedModel(react)
```

`make_model("scripted:/path/turns.json")` loads a file. The file is a list of turn objects, one turn object, or `{"turns": [...]}`. Each object has `calls` (`name`, `args`, optional `id`), optional `text`, optional `usage`, and optional `raw`. When a list or a file runs out, `complete` raises `ModelError` (`scripted model has no turns left`) instead of inventing a final answer. `model.seen` is the message list from each call; `model.seen_tools` is the tool list.

Image blocks are not rewritten. A script sees the same `path` or `b64` the caller passed.

## Command models

`CommandModel` runs a local program. Each `complete` is one process. Stdin is one JSON object:

```json
{"messages": [{"role": "user", "content": "Click Save."}], "tools": []}
```

Messages use the neutral shape above (`role`, `content`, and `tool_call_id` / `name` when set). Image blocks are passed through; the command opens the file if it wants pixels. Stdout is one `ModelTurn` object. Logs go to stderr. A non-zero exit, a timeout, a missing executable, or stdout that is not one JSON object raises `ModelError` with the exit code and the stderr text.

```python
import json, sys
req = json.load(sys.stdin)
json.dump({
    "text": "clicking",
    "calls": [{"name": "click", "args": {"ref": "e2"}, "id": "1"}],
    "usage": {"input_tokens": 10, "output_tokens": 4},
}, sys.stdout)
```

`make_model("command:/path/to/script.py")` runs a single existing `.py` file with the current Python interpreter, so the script does not need a shebang. Any other command is split with `shlex` (Windows rules on Windows) unless the string is itself an existing file, in which case it is executed as that one argv entry. `CommandModel([sys.executable, script])` passes argv through unchanged.

```python
model = make_model("command:/path/to/script.py")
turn = model.complete(messages, tools, timeout=10)
```

## A custom model

Implement the `Model` protocol. `make_model` returns an instance unchanged.

```python
from a11y_computer_use.agent.models import Message, ModelTurn, ToolCall

class EchoModel:
    name = "echo"
    supports_images = False

    def complete(self, messages: list[Message], tools: list[dict], *, timeout: float | None = None) -> ModelTurn:
        text = messages[-1].content if messages else ""
        return ModelTurn(calls=[ToolCall("done", {"answer": str(text)}, "echo-1")], text=str(text))
```

`supports_images` tells the loop whether to attach screenshots. Set it to `False` only when the model cannot accept image blocks; the HTTP providers, `ScriptedModel`, and `CommandModel` all set it to `True`.

Raise `ModelError` for anything the caller should see (auth, a down process, a bad reply). Do not raise the provider SDK's exception type; there is no SDK.

## What the tests check

`tests/test_agent_models.py` and `tests/test_agent_models_http.py` are hermetic.

* Scripted models: a list, a dict, a JSON file (list, `{"turns": ...}`, and one object), a reactive callable, image blocks left unchanged, and exhaustion.
* Command models: a fake Python script on stdin/stdout, two tool calls, an image block passed through, timeout, non-zero exit, non-JSON stdout, and a missing executable.
* Each HTTP provider: the recorded request for a tool-call round trip (previous call plus tool result), a response with two tool calls, a user message with `b64` and `path` images, a tool result that contains an image, and error mapping (400 and 401 are not retried, 429 and 502 are retried with backoff, timeouts are retried, `Retry-After` is honored, a missing key raises before a request).
* Gemini: `thoughtSignature` from the recorded response is sent back on the next request.
* `make_model` for xAI, Ollama, vLLM, Anthropic, and Gemini posts at the endpoint above. Those calls use `MockTransport`. They do not reach the network.

The fixtures live in `tests/fixtures/agent_models/{openai,anthropic,gemini}.json`. They are hand-written wire samples, not transcripts of a live account.
