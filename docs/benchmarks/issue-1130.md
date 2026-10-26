# GTK textarea ref continuity (#1130)

## Root cause

A ref-path bisect from v0.4.76 to 80ff837 identified
52195981900e15e83d095deb977b84792fdf7b8a (#1021) as the first bad commit.
Its parent 7025e5a2 passes the same `_note_issued` → `_anchor` probe:
window/e2-scrollarea/e3-textarea becomes window/e2-textarea, retaining the
textarea's native identity. The bad commit retains the e2 scrollarea anchor
and marks the freshly issued textarea ref changed. #1113 later allowed native
controls to move between ref slots, but required identical pruned ancestry.
It therefore misses single-child scroll-wrapper collapse.

The pre-fix f7763d2 also fails the standalone GTK3 check below with
`e2 no longer identifies AXScrollArea ''; target changed` before typing.
GTK4 additionally needs its real application-scoped D-Bus object paths to be
accepted by the native identity reader. Its paths are outside the ATK-specific
`/org/a11y/atspi/` prefix; live inspection found
`/org/gtk/application/unknown/a11y/<UUID>`. No identity is synthesized.

The fix allows only a unique, previously valid native-node match with unchanged
target semantics, document identity and semantic ancestry. Only unnamed,
noninteractive, non-modal scroll ancestors without semantic identity are omitted
from this comparison. Ordinary identity checks remain exact; internal reads do
not issue refs. Replacement nodes, missing native identity, recycled rows and
changed document/named-parent cases still refuse in regression tests.

## Live verification and timings

On this Linux box: GTK 3.24.49 and GTK 4.18.6, private 1280×800 Xvfb, xfwm4,
AT-SPI2, visible-action overlays disabled. The standalone probe verifies native
GTK buffer contents separately from Runtime receipts, for library clearing,
external changes, fresh snapshots, snapshot → find, type, set_value, and the
reverse short → long transition. It runs no `*_live.py` files.

These are in-process Runtime timings, not MCP transport timings or a complete
release latency gate. Both builds use the same interpreter and fixture. Each
row is a median; seconds and head/baseline ratios follow. GTK3 has six sequences
per build; the serial GTK4 comparison has ten. Clear applies to half the sequences.

| Toolkit | Call | v0.4.76 | Fix | Ratio |
|---|---|---:|---:|---:|
| GTK3 | `snapshot_before` | 0.0377 | 0.0403 | 1.070× |
| GTK3 | `clear` | 0.4039 | 0.4208 | 1.042× |
| GTK3 | `snapshot_after` | 0.0375 | 0.0410 | 1.094× |
| GTK3 | `type` | 0.4191 | 0.4264 | 1.018× |
| GTK3 | `set_value` | 0.4082 | 0.4238 | 1.038× |
| GTK4 | `snapshot_before` | 0.0853 | 0.0987 | 1.157× |
| GTK4 | `clear` | 0.4982 | 0.5471 | 1.098× |
| GTK4 | `snapshot_after` | 0.0496 | 0.0585 | 1.180× |
| GTK4 | `type` | 0.4548 | 0.5013 | 1.102× |
| GTK4 | `set_value` | 0.4524 | 0.4813 | 1.064× |

All reported serial medians are within 1.25×. The initial GTK4 comparison ran
alongside tests: its pre-clear snapshot median was 1.269× and one type call took
1.027 s. That prompted the serial repeat; all initial samples are retained in
[the raw JSON](issue-1130-latency.json), rather than discarded.

Reproduce from this checkout, with a separate v0.4.76 checkout and the supplied
interpreter. `/tmp/915-deps` supplies Pillow missing from that interpreter:

```sh
XDG_CACHE_HOME=/tmp/ref1130-head-cache \
PYTHONPATH=/tmp/915-deps:/tmp/ship-1130-write A11Y_VISIBLE_ACTIONS=0 \
bash scripts/latency/session.sh /workspace/a11y-venv-codex/bin/python \
  scripts/check_gtk_textarea_refs.py --gtk 4 --repeats 5

XDG_CACHE_HOME=/tmp/ref1130-baseline-cache \
PYTHONPATH=/tmp/915-deps:/tmp/1130-baseline A11Y_VISIBLE_ACTIONS=0 \
bash scripts/latency/session.sh /workspace/a11y-venv-codex/bin/python \
  scripts/check_gtk_textarea_refs.py --gtk 4 --repeats 5
```

Use `--gtk 3 --repeats 3` for the GTK3 rows. Each fixture is terminated and the
AT-SPI event loop is stopped explicitly on both builds. Reported checks passed;
ATK emitted non-fatal Text-interface warnings, and GTK4 emitted a cache-path
warning during registration on both builds.

## Automated regression suite

Final serial run from the fix checkout:

```sh
env -u DISPLAY -u WAYLAND_DISPLAY XDG_CACHE_HOME=/tmp/1130-final-full-cache \
  PYTHONPATH=/tmp/915-deps:. dbus-run-session -- \
  /workspace/a11y-venv-codex/bin/python -m pytest -q -rs -p no:cacheprovider \
  --ignore-glob='*_live.py' -k 'not live' --durations=10
```

6025 passed, 25 skipped, 89 deselected, 5 warnings in 263.62s. Skips require
other native platforms/TCC/displays or Linux X11; live-named tests are excluded.
Earlier concurrent runs each encountered an AT-SPI event-thread timeout in an
unchanged synthetic test. Both tests passed unchanged when rerun individually
and in this final serial run. No limits or assertions were changed.
