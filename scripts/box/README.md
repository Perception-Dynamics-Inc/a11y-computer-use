# Box test bed

A [Box](https://box.ascii.dev) is a persistent Ubuntu VM with a real desktop
session (Budgie on Xorg on the current image, streamed at 1920x1080), SSH, and
Chrome preinstalled. It is the cheapest way we have found to test computerUse's
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
cd /path/to/computerUse
tar czf - --exclude='./.venv' --exclude='./.git' --exclude='./scratch' \
    --exclude='./.pytest_cache' --exclude='./.remember' --exclude='./.claude' \
    --exclude='./docs/assets' --exclude='./*.txt' --exclude='__pycache__' . \
  | box ssh <id> 'mkdir -p ~/computerUse && tar xzf - -C ~/computerUse'
box scp scripts/box/bootstrap.sh <id>:/tmp/bootstrap.sh   # or rely on the synced copy
```

## 3. Bootstrap (on the box)

```bash
box ssh <id> 'bash ~/computerUse/scripts/box/bootstrap.sh'
box ssh <id> 'INSTALL_VSCODE=1 bash ~/computerUse/scripts/box/bootstrap.sh'   # add VS Code for the Electron probe
```

Installs the AT-SPI2 stack (`at-spi2-core gir1.2-atspi-2.0 gir1.2-gtk-3.0
python3-gi`), `xdotool` and `x11-utils`, creates a `--system-site-packages`
venv so apt's PyGObject is importable, installs the package with the `dev` and
`browser` extras, and turns on `toolkit-accessibility` for the desktop session.

## 4. Run the verification (on the box)

```bash
box ssh <id> 'bash ~/computerUse/scripts/box/run-live.sh'
```

`run-live.sh` discovers the desktop session environment (DISPLAY, XAUTHORITY,
the session D-Bus address) from a live session process, then runs:

| Step | What it proves |
|---|---|
| hermetic tests | the platform-free core and the driver seam on the box's Python |
| `tests/test_linux_live.py` | AT-SPI2 observe, a11y press, a11y text entry against a real GTK3 window under Budgie's Mutter |
| `tests/test_browser.py -k live` | the CDP backend against a real, non-headless Chrome window |
| `tests/test_arena.py -k live` and `computeruse bench web` | the a11y-vs-screenshot observation cost on a 1920x1080 display |

Use `box exec <id> --timeout 600 -- bash -c '...'` instead of `box ssh` for
anything that launches GUI apps: `box ssh` waits for every child that inherits
its stdout, so a stray GTK window keeps the session open until it times out.
Redirect launched apps to `/dev/null` or use `--detach`.

## 4b. Verify coordinate input under a real pointer (on the box)

```bash
box exec <id> --timeout 900 -- bash -c 'bash ~/computerUse/scripts/box/verify-pointer.sh'
box exec <id> --timeout 900 -- bash -c 'XVFB=1 bash ~/computerUse/scripts/box/verify-pointer.sh'   # also run the CI shape
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
box snapshot <id> computeruse-linux-testbed   # named snapshot, reusable with: box new --from computeruse-linux-testbed
box stop <id>                                 # snapshots and pauses billing
```

## What the box cannot test

macOS (TCC, AXUIElement) and Windows (UIA). Those stay on the GitHub-hosted
runners. The image is X11, so it also cannot exercise the Wayland-only paths
(libei / RemoteDesktop portal); `xdg-desktop-portal-gtk` is installed but no
RemoteDesktop backend is.
