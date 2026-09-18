# Running concurrent agent workflows

a11y-computer-use is the execution component inside a worker. Scale with isolated
workers; keep each observe → act → verify sequence on the same Runtime. A
Runtime retains one current snapshot and its refs. Serializing tool calls
protects an operation or batch, but does not make two independent agents share
that snapshot safely.

## Worker ownership and isolation

Use one process and Runtime per active workflow. Browser jobs bind an explicit
CDP target id. Jobs belonging to the same trusted account can use separate
tabs in one Chrome process. Different tenants need separate browser profiles
and preferably separate boxes: tabs share cookies, storage and browser
privileges. PermissionStore is an action policy, not a tenant boundary.

Native desktop workflows need exclusive ownership of their desktop session.
Do not run multiple agents that type into the same display. Separate Linux
sessions or boxes provide that ownership. Keep the opt-in AT-SPI event thread
disabled unless you have separately validated it for your workload.

Use accessibility refs and `set_value` for bulk text. Implicit Linux typing
verifies that a remembered editable belongs to the current frontmost app;
without detectable ownership (for example, Xvfb without a window manager),
use explicit `set_value` instead. Linux's XTEST fallback
paces keystrokes at 12 ms per character (about 83 characters/second before
server latency) to accommodate asynchronous input methods. It reserves all
needed Unicode keycodes before typing and refuses text that exceeds the spare
keymap capacity. This fallback is slower than a direct accessibility text write
and should be verified against the target application.

The process supervising workers should own the durable job queue, cap active
workers, apply a job deadline, and restart a stuck worker. The MCP server is
stdio; keep CDP on loopback or behind an authenticated tunnel. Do not expose
CDP as a public service.

## Backpressure and cancellation

MCP admits 32 calls per Runtime by default, including the active one. Waiting
calls consume no execution thread and expire after 30 seconds. Saturation or
queue expiry returns `busy`. Configure these limits with
`build_server(max_pending_calls=..., queue_timeout_s=...)`.

Direct concurrent Runtime calls fail immediately with `busy`; the caller may
retry later with backoff. Queued MCP cancellation removes the call before it
executes. Once native execution starts, cancellation cannot undo it: ownership
is retained until that operation finishes. Use the supervisor's process-level
deadline for a permanently hung native library.

A batch permits at most 100 steps and stops starting steps after 60 seconds;
scrolling searches permit at most 100 scrolls.
Wait timeouts must be finite and nonnegative and are capped at 60 seconds.
These are individual limits, not a whole-job deadline.

CDP commands are never automatically retried. A timeout or disconnect after
sending input may have applied the action. Inspect `outcome_unknown`, reconnect
if needed, then observe the page before deciding whether to repeat it. A lost
explicit tab fails with `app_not_found`; it never selects another tab. A failed
password-focus probe refuses typing.

Use `with Runtime(...) as runtime:` or call `close()` in `finally`. Closing
waits for the active operation, clears the snapshot, and releases the driver's
connection. A closed Runtime returns `closed`. A server closes a Runtime it
created; injected Runtimes remain the caller's responsibility.

## Persistent state

Permission updates use a lock plus atomic file replacement so concurrent
writers merge grants and interrupted writes preserve the prior file. A
malformed or unreadable policy denies actions until it is repaired. Store
permission files and audit directories on a local filesystem; cross-host
locking on a network filesystem is not supported.

Audit defaults are 16 MiB per file, at most 32 files per directory, and 64 KiB
per record. Files rotate by UTC day and size; oldest files are pruned.
Oversized records become explicit summaries. Newly written logs retain at most approximately 512 MiB per worker;
pre-existing oversized logs follow normal oldest-file eviction and should be
archived explicitly during migration. Export records before rotation if your
retention policy requires more history. `AuditLog` accepts
`max_file_bytes`, `max_files`, `max_record_bytes`, `lock_timeout`, and `sync`.
Set `sync=True` for an fsync after each record when that durability is required;
measure the throughput cost on your filesystem. Default writes reach the OS
but do not promise power-loss durability. An audit write failure after input
also has an uncertain outcome: re-observe before retrying.

Browser diagnostic buffers are bounded. They retain recent events and may
drop older events during a burst; use external telemetry for a complete
network trace. DOM object handles are released after use to avoid retaining
page objects during long workflows.

## Repeatable validation on ASCII Box

The saved Linux testbed has a real Budgie/Xorg desktop. On that box, from the
checkout with an installed `.venv`:

```bash
REPO="$PWD" LOAD_WORKERS=4 LOAD_ITERATIONS=100 bash scripts/box/run-live.sh
```

The script preserves pytest logs and JUnit XML under `artifacts/`, fails when
tests fail, and rejects skipped or missing mandatory Linux/browser coverage.
It starts a dedicated Chrome profile if the requested CDP port is absent and
cleans up only the Chrome process it owns. Set `REPORT_DIR` for a fixed output
location. The full suite may legitimately skip macOS/Windows-only tests on
Linux; the dedicated live gates may not skip.

Run a longer browser load separately against a running endpoint:

```bash
python scripts/box/load_browser.py --workers 8 --iterations 100 \
  --endpoint http://127.0.0.1:9222 --output artifacts/load-8.json
```

Every worker uses a separate process, tab, permission file and audit directory.
Every iteration observes, fills an input, presses a button by ref, and verifies
the rendered result. The report includes throughput, latency percentiles,
per-process peak RSS, errors and completion counts. The page is local and small:
these numbers measure the execution layer, not LLM latency, arbitrary websites,
whole-browser memory, or production capacity. Size your pool using your own
pages, long-running jobs and peak traffic.

CI includes a four-worker browser load smoke alongside existing platform tests.
Windows remains partial, and native Wayland input remains unsupported. This
hardening pass does not change those platform limits.

The [2026-09-04 validation report](benchmarks/production-2026-09-04.md) records
the two-box results, measured concurrency levels, build checksums and templates.
