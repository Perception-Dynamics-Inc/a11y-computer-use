# Production hardening verification — 2026-09-04

Changes are on `codex/production-hardening`, based on `43b1d10`. The complete
machine-readable results and source/wheel checksums are in
[production-2026-09-04.json](production-2026-09-04.json).

## Changes that matter for concurrent workflows

- A Runtime owns its complete operation or batch. MCP admission and queue waits
  are bounded; overload returns `busy`, and cancelled queued work never executes.
- Browser connections retain their explicit tab, use bounded diagnostics,
  release DOM handles, and report timeouts/disconnects without replaying input.
- Permission writes are atomic across local processes. Invalid policy denies
  actions. Audit logs rotate with bounded records and retention, and repair
  interrupted writes.
- Scrolling and delayed confirmations recheck grants. Linux rejects stale
  editable targets belonging to another app; browser failed password probes
  refuse typing.
- Linux XTEST prebinds Unicode before typing and paces every character. An
  exhausted keymap now rejects the input before partially typing it.
- Remote validation preserves logs, fails on missing live coverage, and includes
  a verified multiprocess browser load test. CI includes that load smoke.

## Validation

| Environment | Result |
|---|---|
| Local macOS, Python 3.13 | 765 passed, 42 platform/grant skips |
| ASCII Box Linux, Python 3.12 | 691 passed, 32 platform skips |
| Mandatory real Budgie/Xorg desktop suite | 18 passed, no skips |
| Mandatory browser/agent/adapter/arena suite | 11 passed, no skips |
| Unicode typing regression after fix | 60 entries across 12 independent test runs, all passed |
| Packaging | Wheel and source archive built; wheel installed into a separate venv on a second box |

The installed wheel's Python source checksum matches the local checkout and
primary box. The Linux typing bug was observed in 2 of 12 baseline runs before
the fix. Regression tests also prove prebinding, no partial input on capacity
failure, and pacing; four fail against the original implementation.

GitHub CI configuration was checked locally; the workflow was not dispatched
in this session. Native Windows and macOS live paths and native Wayland were
not exercised by these Box runs. The remaining platform gates are documented
in the README and per-platform guides.

## Browser capacity measurements

Both boxes expose 4 CPUs and use the default 8 GB machine type. Each worker
owns a process, Runtime, tab, permission file and audit directory. Every
iteration observes a local form, sets its input, clicks by ref, and verifies a
unique `applied:` result that only the button handler can produce.

| Environment | Workers | Verified workflows | Workflows/second | p95 per workflow |
|---|---:|---:|---:|---:|
| Desktop | 1 | 250 | 26.72 | 50.38 ms |
| Desktop | 4 | 1,000 | 78.32 | 82.17 ms |
| Desktop | 8 | 2,000 | 94.77 | 139.93 ms |
| Desktop, extended run | 8 | 8,000 | 99.41 | 137.40 ms |
| Installed wheel, second box | 4 | 1,000 | 40.09 | 124.27 ms |

**12,250/12,250 workflows passed, with zero errors.** The extended 8-worker run
lasted 80.48 seconds. Its p99 was 156.02 ms and the largest Python
worker peak RSS was 30.38 MiB; this excludes Chromium and system memory.

These are small local-form execution measurements. LLM latency, complex pages,
network delays, large documents, and multi-hour workloads need their own
capacity tests. Four workers gave lower latency than eight on this desktop;
eight gave higher throughput. Separate tenants require separate browser
profiles or boxes. Each native desktop needs a single workflow owner.

## Saved boxes and reproduction

- `bx_4c4s3kdb`: source checkout and `.venv` under
  `/home/user/computerUse-hardening`; template `computeruse-hardened-2026-09-04`.
- `bx_pguvxg3x`: same checkout plus installed wheel in `.release-venv`;
  template `computeruse-wheel-2026-09-04`.

```bash
box new --from computeruse-hardened-2026-09-04 --no-env --ttl 3600
box exec <id> --detach -- 'REPO=/home/user/computerUse-hardening LOAD_WORKERS=8 LOAD_ITERATIONS=1000 bash /home/user/computerUse-hardening/scripts/box/run-live.sh'
box exec <id> --status <pid>
```

Raw logs, JUnit XML, load JSON and before/after typing evidence are saved in
`artifacts/production-validation` in the local checkout and under `artifacts`
on the boxes. See [production guidance](../production.md) for queue limits,
worker ownership, timeout handling, audit durability/retention, and cleanup.
The package version remains 0.1.0; this is an unreleased build identified by
its checksum, and has not been published to PyPI.
