# Box test bed

A [Box](https://box.ascii.dev) is a persistent Ubuntu VM with a real desktop
session (Budgie on Xorg on the current image, streamed at 1920x1080), SSH, and
Chrome preinstalled. It is the cheapest way we have found to test a11y-computer-use's
Linux backend under a real window manager, which the Xvfb-based CI job cannot do:
focus-dependent XTEST input, non-headless Chrome, and Electron apps.

Findings from the first run are in [docs/box-testbed.md](../../docs/box-testbed.md).

## Trial limits (as of 2026-09)

| Limit | Value |
|---|---|
| Machine time | 25 hours total on the trial |
| Concurrent boxes | 2 |
| TTL per box | 2 hours maximum, auto-stop required |
| Sizes | `small` or `default` (4 vCPU, 8 GB) |
| Starts | 5 per minute, 25 per hour, 75 per day |

Stopped boxes cost nothing and keep their disk. The trial converts to the paid
plan after 7 days unless cancelled in the dashboard.

## 1. Create a box

```bash
box new --ttl 7200 --type default --json      # prints the id, e.g. bx_fh2cm8n2
box limits                                    # hours used so far
```

## 2. Sync the working tree (no .git, no credentials, no assets)

```bash
cd /path/to/a11y-computer-use
COPYFILE_DISABLE=1 tar czf - --exclude='./.venv' --exclude='./.git' --exclude='./scratch' \
    --exclude='./.pytest_cache' --exclude='./.remember' --exclude='./.claude' \
    --exclude='./docs/assets' --exclude='./*.txt' --exclude='__pycache__' . \
  | box ssh <id> 'mkdir -p ~/a11y-computer-use && tar xzf - -C ~/a11y-computer-use'
box scp scripts/box/bootstrap.sh <id>:/tmp/bootstrap.sh   # or rely on the synced copy
# COPYFILE_DISABLE=1 stops macOS tar from adding AppleDouble ._* sidecar files to the archive.
```

## 3. Bootstrap (on the box)

```bash
box ssh <id> 'bash ~/a11y-computer-use/scripts/box/bootstrap.sh'
box ssh <id> 'INSTALL_VSCODE=1 bash ~/a11y-computer-use/scripts/box/bootstrap.sh'   # add VS Code for the Electron probe
```

Installs the AT-SPI2 stack (`at-spi2-core gir1.2-atspi-2.0 gir1.2-gtk-3.0
python3-gi`), `xdotool` and `x11-utils`, creates a `--system-site-packages`
venv so apt's PyGObject is importable, installs the package with the `dev` and
`browser` extras, and turns on `toolkit-accessibility` for the desktop session.

## 4. Run the verification (on the box)

```bash
box ssh <id> 'bash ~/a11y-computer-use/scripts/box/run-live.sh'
```

`run-live.sh` discovers the desktop session environment (DISPLAY, XAUTHORITY,
the session D-Bus address) from a live session process, then runs:

| Step | What it proves |
|---|---|
| hermetic tests | the platform-free core and the driver seam on the box's Python |
| `tests/test_linux_live.py` | AT-SPI2 observe, a11y press, a11y text entry against a real GTK3 window under Budgie's Mutter |
| `tests/test_browser.py -k live` | the CDP backend against a real, non-headless Chrome window |
| `tests/test_arena.py -k live` and `a11y-computer-use bench web` | the a11y-vs-screenshot observation cost on a 1920x1080 display |

Use `box exec <id> --timeout 600 -- '...'` instead of `box ssh` for
anything that launches GUI apps: `box ssh` waits for every child that inherits
its stdout, so a stray GTK window keeps the session open until it times out.
Redirect launched apps to `/dev/null` or use `--detach`.

## 4a. Drive the box from a planner on your machine

The agent can run its planner here and every tool on the box: the box runs
`a11y-computer-use mcp` inside its desktop session and the agent speaks MCP
to it over `box ssh`'s stdin/stdout.

```bash
# once, on the box: grants are per machine and cannot be set remotely.
# Linux grant keys are process comm names (15 chars): krita, gedit, chrome, gnome-terminal-.
box ssh <id> 'cd ~/a11y-computer-use && .venv/bin/python -c "
from a11y_computer_use.safety import PermissionStore, Tier
s = PermissionStore()
for app in [\"krita\", \"gedit\", \"chrome\", \"gnome-terminal-\"]: s.set_tier(app, Tier.FULL)"'

# from your machine
a11y-computer-use agent --provider claude-cli --app gedit --task "..." \
  --mcp-command "box ssh <id> bash /home/user/a11y-computer-use/scripts/box/mcp-session.sh"
```

`mcp-session.sh` sources `session-env.sh` (DISPLAY, XAUTHORITY and the
session D-Bus address, read from a running `budgie-wm`), turns on the Qt and
GTK accessibility bridges, and execs the server; the handshake takes about
4 s and lists the same 21 tools as a local server. `app launch` takes
`name=`, and `app list` is gated on the frontmost app, so on an empty desktop
it refuses until something is focused.

Known limits of this image (2026-09-20): Krita 5.2.2 never registers on the
AT-SPI bus, and a bare PyQt5 window logs `qt.accessibility.atspi: Error in
contacting registry: Not connected to D-Bus server` even with
`AT_SPI_BUS_ADDRESS` exported, so Qt apps have no a11y tree here and are
driven by screenshot and coordinates (GTK apps such as gedit are fine).
Linux has no OCR refs (`screen_text` is macOS-only). A full-desktop
screenshot crosses the `box ssh` link at roughly 170 KB/s, which is why the
remote runtime asks for JPEG.

## 4b. Verify coordinate input under a real pointer (on the box)

```bash
box exec <id> --timeout 600 -- 'bash ~/a11y-computer-use/scripts/box/verify-pointer.sh'
box exec <id> --timeout 600 -- 'XVFB=1 bash ~/a11y-computer-use/scripts/box/verify-pointer.sh'   # also run the CI shape
```

`verify-pointer.sh` (with `pointer_probe.py`) parks the pointer away from the
origin, then proves that a coordinate `click` puts the pointer exactly at the
requested screen coordinates and lands on the widget there, and that
`Runtime.click(x, y)` with no `display_id` resolves the display through the
driver. This is the run that found the three Linux pointer bugs recorded in
[docs/box-testbed.md](../../docs/box-testbed.md); Xvfb hid all of them because
every window and the pointer start at (0, 0) there.

## 5. Save a template and stop

```bash
box snapshot <id> a11y-computer-use-linux-testbed   # named snapshot, reusable with: box new --from a11y_computer_use-linux-testbed
box stop <id>                                 # snapshots and pauses billing
```

## What the box cannot test

macOS (TCC, AXUIElement) and Windows (UIA). Those stay on the GitHub-hosted
runners. The image is X11, so it also cannot exercise the Wayland-only paths
(libei / RemoteDesktop portal); `xdg-desktop-portal-gtk` is installed but no
RemoteDesktop backend is.

## Concurrent workflows and release gates

The current `run-live.sh` fails on test errors and rejects missing/skipped
mandatory Linux and browser live tests. It saves full logs and JUnit XML under
`artifacts/` (override with `REPORT_DIR`). Browser verification includes the
adapters, reference agent loop and head-to-head fixtures. Only the Chrome
process launched by the script is cleaned up; an existing CDP endpoint is reused.

For long runs, use detached execution: the CLI/API response may time out while
a foreground command is still running. Use the returned pid to inspect status.
Pass the complete shell command as one argument (the CLI joins arguments):

```bash
box exec <id> --detach -- 'REPO=/home/user/a11y-computer-use LOAD_WORKERS=4 LOAD_ITERATIONS=100 bash /home/user/a11y-computer-use/scripts/box/run-live.sh'
box exec <id> --status <pid>
```

`load_browser.py` creates separate worker processes and tabs, submits forms
through the gated Runtime, and verifies every resulting value. Reports contain
completion/error counts, throughput, latency percentiles and worker peak RSS.
Use 1, 4 and 8 workers to find a suitable concurrency level on your box; then
repeat with your real pages. See [production.md](../../docs/production.md) for
worker isolation, overload/cancellation behavior and audit retention settings.
