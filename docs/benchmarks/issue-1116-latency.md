# Issue #1116: scoped Chrome key latency

Browser confirmation already queries every descendant dialog/window role before
Return. Modal guarding then independently walks browser structure and queries
the page again. The fix reuses the complete role-query candidates under the
existing subscribed boundary-event epoch. Modal classification and native
application-sibling checks still run. Query failure or incomplete role coverage
cannot seed an absence result; missing subscriptions, epoch changes, native
mutations and another window prevent reuse. A fresh complete query supersedes
older derived discoveries, and candidate lists are copied before extension.

## Native release-gate measurement

```bash
FIREFOX=/tmp/firefox/firefox PYTHONPATH="$PWD" \
  bash scripts/latency/session.sh /tmp/latency-head-venv/bin/python \
  scripts/latency/run.py --baseline /tmp/latency-wheel-venv/bin/python \
  --head /tmp/latency-head-venv/bin/python --rows R13-key \
  --output /tmp/1116-final-gate.json
```

The baseline environment contains the fixed 0.4.76 wheel. Two discarded warmups
per build, then seven interleaved samples per build, on CPUs 0 and 1:

| Row | 0.4.76 median | Patched median | Ratio | Result |
| --- | ---: | ---: | ---: | --- |
| R13-key | 0.474032 s | 0.514973 s | 1.086367 | pass |

Every measured key had a confirmed outcome and the fixture's form submission
was independently checked. The same invocation passed R9 ref continuity and
R10's three modal background refusals plus address-bar shortcut check. No timing
thresholds, retry rules or process-exit verification waits changed.

[Raw samples, outcomes, versions and source hash](issue-1116-latency.json).
The report's commit is the base commit because the patch was uncommitted during
measurement; its AT-SPI source hash matches the patched source in this PR.
Other latency rows were not rerun. No `*_live.py` files were run.

## Revision investigation

Inspected and sampled the parent of the two cited merges (`2bcec2f`), the
pointer-focus merge (`c3122a3`, #1108), and the modal-cache merge (`41349bc`,
#1105). Three isolated Runtime Return diagnostics per revision had nonmonotonic
results, so they do not establish either merge as the sole cause. Both sides
retain the redundant discovery addressed here. The seven-sample fixed-baseline
MCP gate above is the acceptance evidence, rather than those exploratory
Runtime measurements.
