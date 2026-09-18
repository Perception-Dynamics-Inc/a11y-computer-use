# Runtime concurrency and resource limits

A `Runtime` belongs to **one agent workflow**. It holds that workflow's latest
snapshot, rendering preferences and driver connection. Calls on the same Runtime
cannot overlap: target resolution, permission checks, confirmation, input,
verification and audit run under one reentrant lock. An entire `act` batch owns
the lock, including its nested steps.

Independent agents need separate Runtimes **and isolated desktop sessions or
browser targets**. Two Runtimes controlling the same desktop still share its
pointer and focus. Two agents sharing one Runtime can overwrite each other's
snapshot between calls even though the calls execute sequentially. Neither setup
provides workflow isolation. Scale with independent workers and targets.

## Admission and backpressure

Direct Python calls fail immediately with `ComputerUseError(ErrorCode.BUSY)`
when another thread is using the Runtime. The error has `retryable: true`.
Retry after the active call finishes, or use a queue owned by your worker.

The MCP server admits and queues work asynchronously, before starting a worker
thread. Its defaults are:

| Limit | Default | Behavior |
| --- | --- | --- |
| `build_server(max_pending_calls=...)` | 32, including the active call | Excess requests return `busy` immediately. |
| `build_server(queue_timeout_s=...)` | 30 seconds | A call that waits longer returns `busy` and never executes. |
| Active Runtime operations | 1 | Other admitted calls wait without occupying worker threads. |
| `act` steps | 100 | Larger batches are rejected before any step executes. |
| `act` time budget | 60 seconds | No new step starts after the deadline; waits use the remaining budget. |
| `wait_for(timeout_s=...)` | Maximum 60 seconds | Larger finite values are clamped; negative and nonfinite values are rejected. |
| `scroll_to_find(max_scrolls=...)` | Maximum 100 scrolls | Negative, noninteger and larger values are rejected before observation. |

`max_pending_calls` must be a positive integer, and `queue_timeout_s` must be
finite and positive. These limits are per server instance. The limits apply to
execution admission; the MCP transport still has to receive and decode a request.
Enforce body-size and connection limits separately when exposing a network
transport. The supported CLI server uses stdio.

The MCP event loop remains available for tool discovery and protocol messages
while input or observation blocks. A `busy` result from admission means no input
was executed, so retrying it cannot duplicate that call's input. In contrast, a
batch can have completed earlier steps before its first failure; inspect its
receipts before retrying.

`scroll_to_find` validates that a pinned ref belongs to the requested app and
rematches its container against each fresh snapshot. Each wheel event receives
its own current permission, secure-field and focus checks and its own audit row.
Earlier completed scrolls remain visible if a later iteration fails. Permission
is also refreshed after an accepted human confirmation, so a grant revoked while
the prompt was open cannot authorize input.

## Cancellation and deadlines

Cancellation while waiting removes the request and releases its admission slot.
Once its worker starts, a call is shielded from normal AnyIO/MCP cancellation and
retains ownership until the native operation completes. Python cannot safely
interrupt an in-flight native input operation. A requester that disconnects may
miss the result even though its action completes; re-observe before retrying.

The batch deadline is checked between steps, with each `wait_for` bounded by the
remaining time. It does not forcibly stop an already running driver call, a
human confirmation prompt or the final verification snapshot. Driver timeouts
and process supervision remain necessary for hung native APIs. Process
termination and direct `asyncio.Task.cancel()` outside the normal cancellation
scopes are not a safe way to abort a desktop action.

## Cleanup and ownership

Use the Runtime as a context manager, or call `close()` in your worker's cleanup:

```python
from a11y_computer_use.server import Runtime, build_server

with Runtime(driver=driver, store=permissions, audit=audit) as runtime:
    runtime.desktop_snapshot(app="your-app")
    runtime.call_tool("type", {"text": "hello"})

    # An injected Runtime remains owned by this context manager.
    mcp = build_server(runtime=runtime, max_pending_calls=16, queue_timeout_s=10)
```

`close()` waits for the active operation, releases a driver that exposes
`close()`, drops the snapshot and is idempotent. The Runtime stays closed even if
driver cleanup raises. Further calls return `closed`; create a new Runtime to
start another workflow. The lock does not imply native driver thread affinity:
MCP continues to use AnyIO workers as before.

A server-created Runtime is closed when its server lifespan ends. A Runtime
passed to `build_server(runtime=...)` remains caller-owned, which also supports
tests or hosts that reconnect transports around one workflow. A server instance
sharing one Runtime is not a multi-tenant endpoint.

## Verification

`tests/test_runtime_concurrency.py` exercises real concurrent threads and the
MCP in-memory transport. A forced 24-call burst with four admission slots
executes exactly four calls, rejects twenty with `busy`, and still answers tool
discovery while the first call is blocked. Other tests cover queue expiry,
queued and active cancellation, batch ownership, partial batch receipts,
deadline enforcement, and cleanup during active input.
`tests/test_runtime_scroll_safety.py` covers cross-app pins, moving and missing
containers, focus changes, permission revocation and secure targets.

Run these regressions with:

```sh
python -m pytest tests/test_runtime_concurrency.py tests/test_runtime_scroll_safety.py tests/test_server.py tests/test_scaffold.py -q
```
