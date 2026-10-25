# Safety-probe latency budget

Implements the CTO latency budget spec (2026-10-25), with a fixed 0.4.76
baseline, not a moving main baseline. `latency-bench.yml` is advisory on PRs;
`release.yml` depends on its reusable job before publishing.

```bash
docker build -f scripts/latency/Dockerfile -t a11y-latency .
mkdir -p latency-results
docker run --rm --network none --shm-size=2g --cpuset-cpus=0,1 \
  -v "$PWD/latency-results:/results" a11y-latency
```

On an already provisioned Linux box, use two venvs with system GI bindings:

```bash
CHROME=/opt/google/chrome/chrome FIREFOX=/tmp/firefox/firefox \
  bash scripts/latency/session.sh /path/to/head/bin/python scripts/latency/run.py \
  --baseline /path/to/wheel-0.4.76/bin/python --head /path/to/head/bin/python \
  --output /tmp/latency-results.json
```

The starter gate covers Chrome top/iframe checkbox ref clicks (R1), Firefox
coordinate click on Wikipedia's First ascent header (R2), GTK3 20,000-row
first snapshot plus server RSS (R5), Mousepad Help→About from fresh refs (R7),
and an 18-point pointer-box drag (R8). R9 checks title-change/fresh-ref
continuity. R10 requires background input behind an open aria-modal to be
refused three times. Correctness checks run against head only: 0.4.76 is known
to permit that unsafe modal input.

Every sample starts a fresh app, HOME, profile and MCP stdio server. Actions
use MCP; R2 times the CLI process as in #1067. Snapshot/find preparation and
post-action effect checks are outside action timers. R5 deliberately measures
the first snapshot in a fresh server and app, including during warm-up.
There are two discarded warm-ups per build, followed by seven interleaved
baseline/head samples. JSON preserves every sample, median, min, max and IQR,
app/native versions and commit. A median ratio above 1.25 fails; ratios in
(1.25, 1.5] get one fresh seven-sample retry and fail if both runs exceed 1.25.
RSS must also remain within 1.25. R7 has no waiver. No absolute-time floor is
used to waive a relative regression; the spec's proposed floor is uncalibrated.

The real Wikipedia and Python documentation HTML is frozen as gzip files.
The gate serves fixtures over loopback with external networking disabled.
Wikipedia's online ResourceLoader is unavailable offline, so
`offline-sort.js` attaches a local sorter to the **real table/header** and
changes the title only after its row order actually changes. It does not
replace the page with a synthetic tree. Source URLs, retrieval date and hashes
are in `fixtures/sources.json`; the source pages retain their attribution.
Python type/key, Firefox ref click and Chrome large-table timings (R3/R4/R6)
remain phase 2, as specified. R11 applies to the separate context-read proposal,
which this change does not introduce.

Keep the baseline fixed until a release passes the full gate. A replacement
baseline must be equal or faster in every row. Updating app pins or promoting
the PR job to required needs a calibration report; two weeks without false
failures is the spec's promotion target. This repository cannot configure
GitHub branch-protection settings through a workflow.

The Wikipedia stylesheet is frozen alongside its HTML. The loopback server
sends a CSP that blocks external requests, including on a provisioned box where
Docker's network isolation is unavailable. The R2 effect check reads the native
window title set by the sorter after a changed row order; it does not require an
unrelated second full-page snapshot from the old baseline after DOM reordering.

For a separate five-sample Runtime modal diagnostic and a Return-key profile:

```bash
bash scripts/latency/session.sh /path/to/head/bin/python \
  scripts/latency/modal_probe.py --profile-return /tmp/return.prof
```

Use `--unsafe-baseline` with the 0.4.76 wheel. To measure pre-fix main with the
same fixture, run the script with `PYTHONPATH` pointing to that checkout. This
diagnostic reports every sample and verifies each click's effect; its timings
are separate from the seven-sample MCP/CLI gate.

R1 uses the exact minimal `top.html` / `ifr.html` / `inner.html` checkbox shape
from [#1061](https://github.com/Perception-Dynamics-Inc/a11y-computer-use/issues/1061),
including its onchange title evidence. It is separate from the modal/drag fixture.

R2 invokes the installed package through `run_once.py` for both builds. The
launcher preserves the real CLI arguments, exit status, and exceptions, then
quits and joins its AT-SPI event thread before releasing the native cache.
This cleanup stays inside the subprocess timer. The 0.4.76 wheel otherwise
leaves a daemon loop running through Python finalization: a reproduced CI core
showed its libatspi/ctypes callback crashing in `PyGILState_Ensure` while the
main thread was already in libc `exit`. Child failures retain stdout/stderr
and remain fatal; the separate native-title check still verifies the sort.
