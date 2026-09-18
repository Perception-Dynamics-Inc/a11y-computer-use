# Continuous integration

`.github/workflows/ci.yml` runs five jobs on every push to `main` and
`main` and on every pull request. Every job runs the full hermetic
test suite with `pytest -q -rs -p no:cacheprovider`; tests that need a platform,
a TCC grant, a display, or a Chrome DevTools endpoint skip themselves and `-rs`
prints each skip with its reason, so a green job also tells you what it did not
exercise. Each job then adds the live proof that only its runner can give.

A new push to the same branch cancels the run it supersedes (`concurrency`), and
the workflow token is read-only (`permissions: contents: read`).

## What each job proves

| Job | Runner | Hermetic | Live |
|---|---|---|---|
| `macos` | `macos-latest` | full suite | The TCC-gated macOS tests (Finder AX walk, TextEdit ref click plus typing, CGEvent post, screenshot dimensions, doctor grant probes, MCP stdio snapshot) run when the runner image holds the Accessibility and Screen Recording grants and skip when it does not. The ungranted-path tests do the reverse. Read the skip list to know which happened. |
| `windows` | `windows-latest` | full suite (`.[dev,windows,browser]`) | Driver selection resolves to `windows`; `build_server()` builds the MCP server; `tests/test_windows_live.py` drives Notepad through UI Automation: snapshot through the shared pruning engine, a11y press, SendInput typing, a `ctrl+a` chord, and the gated Runtime end to end. |
| `browser` | `ubuntu-latest` | the browser stack (`test_browser`, `test_adapters`, `test_agent`, `test_providers`, `test_h2h`, `test_arena`) over a scripted CDP transport | Headless Chrome on `:9222`: observe, act, verify, iframe stitching, console and network capture (`test_browser -k live`); cu-arena observation cost (`test_arena -k live`); a pixel click through the Anthropic adapter snapping to a ref (`test_adapters -k live`); the reference agent loop with a scripted planner (`test_agent -k live`); the head-to-head harness in refs and pixels modes with page-counted misclicks (`test_h2h -k live`); and `a11y-computer-use bench desktop --rounds 2` on the bound tab, printed. |
| `linux` | `ubuntu-latest` | full suite in a venv that sees apt's PyGObject (`--system-site-packages`), outside any X session | Driver selection resolves to `linux`; `build_server()` builds; then `tests/test_linux_live.py` and `tests/test_linux_desktop_live.py` run under Xvfb with a D-Bus session, `at-spi-bus-launcher`, and the openbox window manager. |
| `package` | `ubuntu-latest` | none | `uv build` produces the wheel and sdist, the sdist is checked to contain no brand media, `uvx --from <wheel> a11y-computer-use --help` runs the console script from an isolated environment, and a fresh venv imports the wheel's modules. |

## Why the Linux job has a window manager

Xvfb alone has no window manager: nothing owns focus, there is no active
window, and the pointer starts at (0, 0). Under those conditions a relative
pointer warp and an absolute move coincide, and a hit-test that translates
geometry the wrong way still passes because every window sits at the origin.
Three Linux coordinate-input bugs shipped that way and were only found on a Box
desktop VM (`docs/box-testbed.md`).

openbox is a small EWMH window manager. With it running on the Xvfb display,
`tests/test_linux_desktop_live.py` stops skipping and runs its ten checks:
coordinate clicks with the pointer parked off-origin land on the widget and leave
the pointer where they said they would, the act-time hit-test picks the right
window, `Runtime.click(x, y)` resolves the display through the driver, and the
apps, windows, and clipboard tools work against a live GTK3 window. Verified on
2026-09-02 on a Box (Ubuntu 24.04, the exact CI shape):

```text
WM present: True
tests/test_linux_live.py tests/test_linux_desktop_live.py: 17 passed
```

The hermetic step of the same job, run outside X on the same box, reported
`445 passed, 57 skipped, 0 failed`.

## Live versus hermetic, per platform

| Area | Hermetic (every job) | Live (where) |
|---|---|---|
| Pruning engine, refs, stable ids, diff, interactive view, budget | `test_observe`, `test_linux_synthetic` | macOS (TCC), Windows (Notepad), Linux (GTK3), browser (Chrome) |
| Safety: tiers, rechecks, confirmation gate, audit, redaction | `test_safety`, `test_server` (macOS driver seams mocked, forced with `A11Y_COMPUTER_USE_DRIVER=macos`) | macOS live smoke when granted |
| CGEvent executor | `test_act` (skips at collection off macOS) | macOS when granted |
| Windows UIA | none | `windows` job |
| Linux AT-SPI2 and XTEST | `test_linux_synthetic`, `test_linux_system_synthetic` (fake Xlib and fake Atspi) | `linux` job under openbox |
| Browser CDP | `test_browser` (scripted transport) | `browser` job |
| Provider adapters | `test_adapters` (fake driver, scripted transport) | `browser` job, Anthropic adapter only |
| Agent loop and planners | `test_agent`, `test_providers` (fake urlopen, scripted planner) | `browser` job with the scripted planner; `claude-cli` runs are manual (`docs/agent-loop.md`) |
| Head-to-head harness | `test_h2h` | `browser` job with the scripted planner; model runs are manual (`docs/benchmark.md`) |
| Packaging | none | `package` job |

Not covered by any job: a real Wayland session (the Linux job is X11), a real
desktop Linux window manager other than openbox, the Anthropic and OpenAI
provider endpoints, and the OS drivers under the provider adapters.

## The Box nightly

The GitHub runners cannot give Linux a real desktop session or macOS a
guaranteed TCC grant. `scripts/box/run-live.sh` and `scripts/box/verify-pointer.sh`
run the Linux live suites, the browser suite against a non-headless Chrome, and
the pointer probe on a Box VM (`docs/box-testbed.md`, `scripts/box/README.md`).
Run them by hand after changes to `a11y_computer_use/drivers/linux.py` or
`_linux_input.py`, or wire them into a nightly job with a `BOX_API_KEY` secret
once the trial question is settled.

## Reproducing a job locally

macOS (the `macos` job):

```bash
pip install -e ".[dev,browser]"
pytest -q -rs -p no:cacheprovider
```

Browser (the `browser` job), from any OS with Chrome installed:

```bash
pip install -e ".[dev,browser]"
google-chrome --headless=new --remote-debugging-port=9222 --window-size=1280,800 about:blank &
export A11Y_COMPUTER_USE_CDP_ENDPOINT=http://127.0.0.1:9222
pytest tests/test_browser.py tests/test_adapters.py tests/test_agent.py tests/test_h2h.py tests/test_arena.py -k live -q -rs
A11Y_COMPUTER_USE_DRIVER=browser a11y-computer-use bench desktop --rounds 2
```

Linux (the `linux` job), on Ubuntu 24.04:

```bash
sudo apt-get install -y --no-install-recommends at-spi2-core gir1.2-atspi-2.0 gir1.2-gtk-3.0 python3-gi \
  xvfb dbus dbus-x11 openbox xdotool x11-utils xclip
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev,browser]" python-xlib
.venv/bin/pytest -q -rs -p no:cacheprovider                      # hermetic, outside X
GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0 \
xvfb-run -a -s "-screen 0 1280x800x24" dbus-run-session -- bash -c '
  ( /usr/libexec/at-spi-bus-launcher --launch-immediately >/dev/null 2>&1 & )
  ( openbox >/dev/null 2>&1 & )
  sleep 2
  timeout -k 5 300 .venv/bin/pytest tests/test_linux_live.py tests/test_linux_desktop_live.py -q -rs
'
```

Package (the `package` job):

```bash
uv build
uvx --from "$(ls dist/*.whl)" a11y-computer-use --help
```

Windows: `pip install -e ".[dev,windows,browser]"` then `pytest -q -rs`; the live
UIA tests need a desktop session with Notepad available.

## Conventions the tests rely on

- A test that needs a platform uses `pytest.mark.skipif(sys.platform ...)`; a
  module that imports a platform-only dependency at import time uses
  `pytest.importorskip` (`tests/test_act.py`, `tests/test_overlay.py`).
- TCC-gated macOS tests use `HAS_AX` and `HAS_SCREEN` from `tests/conftest.py`,
  probed through ctypes so importing the tests never triggers a permission prompt.
- CDP-gated tests read `A11Y_COMPUTER_USE_CDP_ENDPOINT` and skip without it.
- Display-gated Linux tests skip without `DISPLAY` or `WAYLAND_DISPLAY`, and the
  real-desktop tests additionally require an EWMH window manager and a reachable
  AT-SPI bus.
- `tests/test_server.py` mocks the macOS native seams, so it forces
  `A11Y_COMPUTER_USE_DRIVER=macos`; the macOS driver imports on every OS because
  `act`, `capture`, and `observe` are import-safe without pyobjc.
