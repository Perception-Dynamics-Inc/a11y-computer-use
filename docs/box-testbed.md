# Box test bed findings

First real-desktop Linux run of computerUse on a [Box](https://box.ascii.dev)
VM, on 2026-09-02. The value of this over the Xvfb CI job is a real window
manager with real pointer and focus semantics, a real (non-headless) Chrome, and
an Electron app. That combination surfaced two coordinate-input bugs that Xvfb
hides. See [scripts/box/README.md](../scripts/box/README.md) for how to
reproduce.

## The machine

| Property | Value |
|---|---|
| OS | Ubuntu 24.04.4 LTS, kernel 6.8.0 |
| CPU / RAM | 4 vCPU, 7.8 GB |
| Session type | x11 (`XDG_SESSION_TYPE=x11`) |
| Display server | Xorg on `:0`, 1920x1080 |
| Desktop / WM | Budgie (`budgie-wm`, which is Mutter), `XDG_CURRENT_DESKTOP=Budgie:GNOME` |
| Desktop streaming | Sunshine plus a Moonlight web server (this is why the pointer rests at a screen corner when idle) |
| a11y bus | at-spi2-core 2.52, `org.a11y.Bus` up; `toolkit-accessibility` starts off and is turned on per session |
| Chrome | Google Chrome 151 (reports as Chromium 151.0.7922.108) |
| Python | 3.12.3, apt `python3-gi` 3.48.2, Atspi typelib present |
| Preinstalled | Docker, Chrome, GitHub CLI, Node, Rust, Go; VS Code installed on demand from the .deb |

The session is X11, so this box cannot exercise the Wayland-only input path
(libei / RemoteDesktop portal). `xdg-desktop-portal-gtk` is present but no
RemoteDesktop portal backend is, and `org.freedesktop.portal.Desktop` exposes no
RemoteDesktop interface. `libei1` and `libeis1` are installable from apt if a
Wayland image is used later. Verifying the Wayland raw-input path still needs a
Wayland desktop, not this image.

## What passed

Hermetic suite (no display), on the box's own Python:

```
83 passed, 8 skipped
```
(`tests/test_drivers.py tests/test_observe.py tests/test_linux_synthetic.py tests/test_browser.py`)

Live AT-SPI2 backend under the real Budgie session:

```
5 passed
```
(`tests/test_linux_live.py`: snapshot of a GTK3 window, a11y button press via
`do_action`, a11y text entry via `EditableText`)

Live browser backend against a real non-headless Chrome window:

```
4 passed, 28 deselected
```
(`tests/test_browser.py -k live`: observe, act, verify, iframe stitching)

cu-arena against the same real Chrome, two live measurements:

```
cu-arena  observations=2  elements=11  frame=945x920px
  a11y (text)           176 tok  (88/obs, 349 chars)
  screenshot (img)     2318 tok  (1159/obs, ~wxh/750)
  -> a11y-first is 13.2x cheaper per observation.
  re-observe (a11y diff) 10 tok avg vs 1159 tok/screenshot
```

```
computeruse bench web https://example.com --rounds 3
  a11y (text)           285 tok  (95/obs)
  screenshot (img)     3477 tok  (1159/obs)
  -> a11y-first is 12.2x cheaper per observation.
  re-observe (a11y diff) 10 tok avg vs 1159 tok/screenshot
```

The screenshot per-observation cost here (1159 tokens) is higher than the
headless CI number because the frame is a real 1920x1080 desktop, so the ratio
is larger than CI reports. State it as "roughly 12 to 13 times cheaper per
observation on this display", not a single fixed number.

Manual end-to-end checks through the driver and the gated `Runtime`:

- a11y press of a button by ref set the app's status label and left the pointer
  where it was (no cursor movement), as designed.
- Screenshot through the driver returned a 1920x1080 PNG.
- `Runtime.type_text` and `Runtime.key("ctrl+a")` worked through the gate after
  granting the app the full tier.
- XTEST keyboard typing into a focused GTK entry landed: the text appeared in the
  entry and propagated to the label on Save. The vision-fallback keyboard path
  works on a real desktop.

## Electron (VS Code) accessibility

VS Code 1.135 exposes its accessibility tree over AT-SPI on this desktop, and it
is snapshot-able through the shared pruning engine:

| Launch | Elements in the focused window | Clickable |
|---|---|---|
| `code --force-renderer-accessibility` | 14 | 5 |
| `code` (no flag) | 9 | 3 |

Both cases exposed the real window and its controls (Minimize, Maximize, Close)
under `AXWindow` / `AXButton` / `AXGroup`. The forced flag adds renderer
(web-content) nodes. So Electron is observable on Linux, and our driver's
`org.a11y.Status` forcing plus `--force-renderer-accessibility` improves
coverage. This is the Electron datapoint the macOS notes were missing.

Chrome's own browser UI (the toolbar and address bar, not a page) returned zero
elements over AT-SPI on this build. Chrome's window chrome is not exposed to
AT-SPI here; drive pages through the CDP browser backend instead, which is what
it is for.

## Coordinate-input bugs found (all invisible to Xvfb CI)

### 1. The Linux coordinate click uses a relative pointer warp as if absolute

`computeruse/drivers/_linux_input.py` positions the pointer with
`display.warp_pointer(x, y)` in `click`, `drag`, and `scroll`. Measured on the
box: with the pointer at (810, 394), `warp_pointer(500, 500)` moved it to
(1310, 894), that is by +500/+500. python-xlib's `warp_pointer` is relative to
the current pointer position, not absolute. So a coordinate click at (810, 354)
left the pointer at the screen corner and the click landed on the wrong widget,
which then broke the follow-on typing because focus never moved to the target.

Xvfb hides this because the pointer starts at (0, 0), where relative and absolute
are the same. `xdotool mousemove 810 354` and XTEST `MotionNotify(x, y)` both
positioned the pointer correctly to the absolute coordinates on the same box.

Recommended fix: position the pointer with an absolute motion. Replace
`d.warp_pointer(x, y)` with `xtest.fake_input(d, X.MotionNotify, x=int(x), y=int(y))`
(the module already imports XTEST for buttons and keys, and this was verified to
move the pointer to absolute coordinates on the box), or use
`d.screen().root.warp_pointer(x, y)` which warps relative to the root origin.
Apply to `click`, `drag`, and `scroll`. Add a live coordinate-click assertion so
a real-desktop run (Box) would catch a regression that Xvfb cannot.

### 2. Runtime.click with x/y and no display_id crashes off macOS

`Runtime._target` in `computeruse/server.py` fills a missing `display_id` with
`int(Quartz.CGMainDisplayID())` unconditionally. On Linux this raises
`NameError: name 'Quartz' is not defined` (Quartz is imported only on macOS), so
`Runtime.click(x=.., y=..)` without an explicit `display_id` fails on Linux and
would fail on Windows. Passing `display_id=0` avoids the crash and instead went
through the normal target-app recheck.

Recommended fix: resolve the default display through the driver rather than
Quartz, for example `self.driver.screenshot`'s display or a
`driver.main_display_id()` seam, so the coordinate path is cross-platform. Until
then, callers on Linux must pass `display_id` explicitly.

## Trial usage

31.4 minutes of the 25 trial hours were used for the full run above (create,
install, hermetic plus live plus browser plus arena plus the manual probes).
Box id `bx_fh2cm8n2`, one `default` box, well within the 2-hour TTL.

## Fixes verified

Second run, later the same day, on a fresh box created from the saved template
(`box new --from computeruse-linux-testbed --ttl 3600`, box `bx_ngdszs3k`, same
Budgie on Xorg image). The fixes below were synced from the fix branch and
verified with `scripts/box/verify-pointer.sh` (which runs
`scripts/box/pointer_probe.py`, then the live pytest suite under the real window
manager, then, with `XVFB=1`, the same suite in the CI shape: Xvfb plus a fresh
session bus and a11y bus, no window manager).

What changed:

1. `computeruse/drivers/_linux_input.py`: `click`, `drag`, and `scroll` position
   the pointer with an absolute XTEST `MotionNotify` (`_move`) instead of the
   relative `Display.warp_pointer`.
2. `computeruse/server.py`: `Runtime._target` takes the default display from the
   new `Driver.main_display_id()` seam (macOS returns `CGMainDisplayID`; the
   Linux, Windows, and browser drivers return 0) instead of calling Quartz.
3. A third bug, visible only once the first two were fixed:
   `computeruse/drivers/_linux_system.py` `_geometry_on_root` translated the
   root origin into window coordinates (`win.translate_coords(root, 0, 0)`),
   which is the negated window position. Every window not at (0, 0) therefore
   failed the act-time hit-test and the full-screen desktop window
   (`nemo-desktop` here) won it, so `Runtime.click(x, y)` refused with
   `focus_changed` ("the app under the target point is now nemo-desktop"). It
   now translates the window origin into root coordinates
   (`root.translate_coords(win, 0, 0)`), which also fixes the bounds the
   `window list` tool reports on Linux. Xvfb without a window manager places
   every window at (0, 0), where both forms agree, which is why CI never saw it.

Probe output (pointer parked at (800, 400) by `xdotool`, then moved by the probe
to (508, 204) before each click; the button center was (358, 84)):

```
PASS driver.click left the pointer at (358, 84) == (358, 84)
PASS driver.click landed on the button (entry values ['SAVED'])
PASS entry cleared via set_value
frontmost app id (gating key): 'python'
Runtime.click -> clicked (358, 84) on display 0
PASS Runtime.click without display_id returned a click receipt: 'clicked (358, 84) on display 0'
PASS Runtime.click left the pointer at (358, 84) == (358, 84)
PASS Runtime.click landed on the button (entry values ['SAVED'])
```

pytest under the real window manager (the two new tests are
`test_linux_coordinate_click_lands_with_pointer_away_from_origin` and
`test_linux_runtime_click_without_display_id`; both park the pointer off-origin
first, so they fail against the old relative warp even under Xvfb):

```
tests/test_linux_synthetic.py tests/test_linux_system_synthetic.py: 14 passed
tests/test_linux_live.py: 7 passed
```

The same live suite in the CI shape (`xvfb-run` + `dbus-run-session` +
`at-spi-bus-launcher`, no window manager):

```
tests/test_linux_live.py: 7 passed
```

Before the fixes, on the same image, the probe's `Runtime.click` step failed
with `focus_changed` and the live suite reported `1 failed, 6 passed` in both
shapes. The macOS suite on the development machine reports `523 passed, 28
skipped` with the fixes (the Linux live tests skip there).

Trial usage for this run: about 10 minutes of box time (create from template,
sync, two verification passes, stop); 42.5 minutes of the 25 trial hours are
used in total. The old box `bx_fh2cm8n2` was deleted afterwards (its box-scoped
token had appeared in a command output); `bx_ngdszs3k` is stopped and carries
the `computeruse-linux-testbed` template, which is what `box new --from` restores.

## Whole suite and keyboard input on the desktop

A later run on a box created from the `computeruse-linux-testbed` template
(after the fixes above) ran the complete test suite inside the desktop session,
not just the driver seam:

```
461 passed, 41 skipped, 0 failed
```

The skips are the macOS TCC live tests, the Windows UIA live test, and the
desktop-live tests when the step runs headless. `computeruse doctor` reported 8
of 8 on the box:

```
[ OK ] display_session   XDG_SESSION_TYPE=x11 DISPLAY=:0
[ OK ] window_manager    EWMH window manager: Mutter(Budgie)
[ OK ] atspi_bindings    gi + Atspi 2.0 typelib import cleanly
[ OK ] a11y_bus          org.a11y.Bus reachable; 15 application(s) on the desktop
[ OK ] coordinate_input  XTEST available (python-xlib)
[ OK ] clipboard_tool    xclip on PATH
[ OK ] python_version    Python 3.12.3
[ OK ] mcp_import        mcp 1.29.1 imports cleanly
```

`tests/test_linux_desktop_live.py` drives a real GTK3 window under the window
manager and covers the input surface a user has (10 tests, all passing on the
box): a coordinate click lands at the absolute target and triggers the button
there; XTEST typing into the focused field round-trips punctuation, symbols,
and characters the keymap lacks (`"Hello, World! 42 <a/b> ünïcödé 日本"` comes
back exactly); key chords work, including `ctrl+a` then replace, `end`,
`backspace`, and chords on punctuation keys (`ctrl+/`, `ctrl+minus`, `alt+.`,
`shift+tab`, `f13`); the scroll wheel changes a spin button and a drag moves a
slider; app list, window list, activate, frontmost, and the clipboard
round-trip (non-ASCII included); and the gated Runtime path that `run-once`
and the MCP tools use (`click(x, y)` with no display id, `key`, `type`,
`desktop_snapshot`) passes through the safety tiers.

That run surfaced a fourth desktop-only bug, also fixed: XTEST typing dropped
characters not on the active keymap (accented Latin, CJK) and control
characters (newline, tab). The fix binds spare keycodes to the missing keysyms
for the duration of the operation, the way xdotool does, and maps control
characters to their key keysyms; chords now accept punctuation names, F13 to
F24, and shift-level keys (`computeruse/drivers/_linux_input.py`, commit
5267da2). The same pass gave `doctor` real Linux and Windows checks instead of
reporting macOS grants that do not exist there, and made `computeruse snapshot`
resolve its backend through the driver seam.

## Recommended CI follow-up

Add a manual or nightly "real desktop" job (self-hosted, or a Box created from
the saved snapshot via the Box API and a `BOX_API_KEY` secret) that runs
`scripts/box/run-live.sh` and `scripts/box/verify-pointer.sh`. The two new live
tests already unmask the pointer bug under Xvfb, but the hit-test bug (windows
not at the origin) still needs a window manager: either add a lightweight one to
the Xvfb job (`openbox` or `xfwm4`) or keep the real-desktop run.
