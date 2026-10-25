# Issue #1095 modal latency validation

Measured on Linux X11/Xvfb 1280×800, xfwm4, AT-SPI 2.56.2, LibreOffice 25.2.3.2 GTK3,
Chrome 155.0.8059.84 and Electron 41.7.1, with A11Y_VISIBLE_ACTIONS=0. Main before is
`e09b18e` (after #1093 and #1099). The wheel hash matches the issue's 0.4.76 wheel.
The patched source hash and all samples are in [the raw report](issue-1095-latency.json).

Each completed row uses two warm-ups followed by seven interleaved samples per build;
no completed timing sample was removed. Pre-fix and patched runs had different CPU
pairs and different host load, so both paired wheel medians are shown. Relative ratios
are the comparison; cross-run absolute deltas alone do not isolate the fix.

| Operation | Wheel (before run) | Main before | Before ratio | Wheel (after run) | Patched | After ratio |
|---|---:|---:|---:|---:|---:|---:|
| LibreOffice modal find | 1.182 s | 2.360 s | 1.997× | 1.674 s | 1.909 s | 1.141× |
| LibreOffice modal ref click | 1.385 s | 2.680 s | 1.935× | 1.921 s | 2.310 s | 1.202× |
| Chrome no-modal key | 0.468 s | 0.563 s | 1.205× | 0.486 s | 0.593 s | 1.222× |
| Chrome no-modal ref click | — | — | — | 2.755 s | 2.796 s | 1.015× |
| Chrome no-modal ref type | — | — | — | 3.114 s | 3.370 s | 1.082× |

The pre-fix run stopped at Chrome ref click setup with an app-registration failure;
its click/type distributions are absent, not estimated. Both required LibreOffice
before/after comparisons completed.

The first patched run aborted during an R13-click warm-up: the call lasted 382.5 s
and honestly returned unverifiable. Its cause is not established (a separate 45 s
session wait also lasted 396 s). The raw failure is retained. The unfinished Chrome
rows were rerun in a fresh private desktop without changing thresholds or assertions.
All completed patched row medians pass the 1.25× gate. LibreOffice ref clicks retain
partial outcomes with selected-row evidence; Chrome samples require confirmed effects.

The native-worker profiles isolate overhead from the caller's wait. These are adapter
requests (including cache hits) and dispatched getter/mutator callbacks, not D-Bus
packet counts; direct native interface calls are not included in that callback count.
Two build_snapshot calls are the same two window trees in all builds, not extra app reads.

| Build/action | Adapter requests | Dispatched methods | find_root | build_snapshot | Window lists | App-child reads | Recursive modal discovery |
|---|---:|---:|---:|---:|---:|---:|---:|
| wheel find | 20288 | 11439 | 1 | 2 | 1 | 4 | 0 |
| wheel click | 20751 | 11852 | 4 | 2 | 1 | 12 | 0 |
| before find | 40425 | 12696 | 1 | 2 | 1 | 6 | 0 |
| before click | 42697 | 13827 | 4 | 2 | 1 | 14 | 1 |
| after find | 29770 | 12678 | 1 | 2 | 1 | 6 | 0 |
| after click | 32042 | 13809 | 4 | 2 | 1 | 14 | 1 |

For LibreOffice find, pre-fix outside_modals consumed 1.604 s in the native-worker profile;
the patched traversal consumed 0.291 s. One recursive modal discovery per ref
click was already present; the larger regression was repeated modal ancestry/classification
and ctypes layout construction. The fix reuses immutable layouts, traversal-local modal
metadata/classification and complete action discovery for subsequent point checks. Point-only
results never authorize a broader ref check. Event/mutation invalidation remains intact.

Safety checks: #1013 background click refused 3/3; #1084 implicit typing refused 3/3
for both native and ARIA page modals while address-bar focus/type remained usable;
#1097 Electron parent click/type/set_value each refused 3/3, parent unchanged, inside
modal typing confirmed. These are standalone native diagnostics, not pytest live files.

Full suite excluding *_live.py: 5974 passed, 56 skipped, 6 warnings (286.50 s).
Related suites: 2174 passed, 2 macOS-only skips (186.61 s). Final focused modal/identity/
gate regressions: 113 passed, 1482 deselected. Remaining skips are missing macOS frameworks/
grants, display-dependent off-platform checks, unconfigured live CDP and absent Inkscape.
Warnings are existing Pillow/AT-SPI deprecations. No CI or off-platform live claim is made.
The supplied interpreter lacked Pillow; /tmp/latency-deps supplied the existing dependency.

## Commands

```bash
PYTHONPATH=$PWD CHROME=/opt/google/chrome/chrome FIREFOX=/tmp/firefox/firefox \
  bash scripts/latency/session.sh /workspace/a11y-venv-codex/bin/python \
  scripts/latency/run.py --baseline /tmp/latency-wheel-venv/bin/python \
  --head /workspace/a11y-venv-codex/bin/python \
  --rows R12-find R12-click R13-key R13-click R13-type --output /tmp/gate1095-after.json
# Fresh unfinished Chrome rows after the aborted warm-up:
# same command with --rows R13-click R13-type --output /tmp/gate1095-chrome-after.json
# Pre-fix: copy this PR's scripts/latency into a checkout of e09b18e;
# run from that checkout with taskset -c 2,3 before session.sh.
PYTHONPATH=$PWD bash scripts/latency/session.sh /workspace/a11y-venv-codex/bin/python \
  scripts/latency/profile_modal.py --output /tmp/profile1095-final.json
# Repeat with PYTHONPATH=/tmp/ship1095-before, and with empty PYTHONPATH + wheel Python.
PYTHONPATH=$PWD:/tmp/latency-deps bash scripts/latency/session.sh \
  /workspace/a11y-venv-codex/bin/python -m pytest -q -rs -p no:cacheprovider --ignore-glob='*_live.py'
PYTHONPATH=$PWD:/tmp/latency-deps DISPLAY=:194 XDG_CACHE_HOME=/tmp/safety1095-cache \
  dbus-run-session -- /workspace/a11y-venv-codex/bin/python scripts/check_chromium_page_modals.py
PYTHONPATH=$PWD:/tmp/latency-deps bash scripts/latency/session.sh /workspace/a11y-venv-codex/bin/python \
  scripts/latency/check_electron_modal.py --electron /tmp/1039-electron/electron
```
