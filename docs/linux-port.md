# Porting computerUse to Linux

**Goal:** the same one reusable computerUse, the third `Driver` behind the platform-free core. Everything OS-specific lives in `drivers/linux.py` (+ its `_atspi` / `_linux_input` / `_linux_system` helpers); the schema, tree-pruning engine, safety layer, and MCP server are **shared and untouched**. Porting was "implement `drivers/linux.py`", never "touch the core."

**Status:** implemented; partially live-verified in CI. `tests/test_linux_live.py` (5 tests, `5 passed` on run 33436980587 at 3b331ba, `ubuntu-latest` under Xvfb + `dbus-run-session` + `at-spi-bus-launcher`) covers the driver name, an AT-SPI snapshot of a real GTK3 window, an a11y button press via `do_action`, a11y text entry via `EditableText`, and the `org.a11y.Status` flip. Coordinate click/drag/scroll, key chords, capture, EWMH windowing, clipboard, `wait_for`, and `set_value` are implemented but have no live test; the Wayland matrix below reflects a manual headless-sway run and is not CI-tested. `tests/test_linux_synthetic.py` (runs on any OS, no bus) covers the role vocabulary, accessor, chord parsing, and error paths; `tests/test_linux_live.py` (Linux-only, self-skips without a reachable bus) launches a real GTK3 window (`cuatestapp`), snapshots it through the shared pruning engine, focuses its entry via AT-SPI **without moving the cursor**, and types via AT-SPI `EditableText.insert_text` (XTEST keystrokes are only the fallback when no editable was focused through the driver). A `linux` CI job (`ubuntu-latest`) runs the bus-free tests on every push and the live AT-SPI run under Xvfb + a session/a11y bus (`xvfb-run` + `dbus-run-session` + `at-spi-bus-launcher --launch-immediately`). The live step self-skips if the a11y bus is unreachable, but on the current head it runs and passes (run 33436980587 on 3b331ba: `5 passed`), mirroring how `windows-latest` verifies the Windows backend.

## What's already shared (no per-OS work)

- `schema.py`: the canonical Element/Snapshot/Action contract. Identical on every OS.
- `observe.build_snapshot` and the pruning engine: walks any `TreeAccessor` and prunes/indexes it. The Linux `ATSPIAccessor` feeds the *same* engine; `_match_anchor` (ref re-resolution) is reused verbatim.
- `safety.py` (tiers, gates, audit log, confirmation classifier) and the MCP server with its full 16-tool surface (`console` and `network` are added only on the browser backend). Platform-free.

## The role-vocabulary trick

Same move as the Windows adapter (`_uia.py`): `_atspi.py` maps AT-SPI role names onto the **same AX role vocabulary** the pruning engine keys off: `"push button"` → `AXButton`, `"entry"` → `AXTextField`, `"password text"` → `AXSecureTextField`, `"frame"`/`"dialog"` → `AXWindow`, and so on (the `_ROLE` table). The mapping is keyed by the human role-name string from `get_role_name()` rather than the numeric `Atspi.Role` enum, because the strings are stable across atspi2 versions and bindings. AT-SPI action names map the same way: `"click"`/`"press"`/`"activate"`/`"do default"`/… → `AXPress`, `"select"`/`"pick"` → `AXPick`, so the engine's `_PRESS_ACTIONS` interactivity test fires unchanged. Result: a Linux tree prunes/indexes through the identical engine as macOS and Windows, with zero engine changes.

Coordinates need no projection either: `AtspiComponent.get_extents(SCREEN)` returns physical pixels with a top-left origin, so `primary_geometry()` (Xlib screen size, `$COMPUTERUSE_SCREEN` fallback) uses `scale=1.0` and the engine's point→pixel projection is an identity, the same as the Windows adapter.

## Primitive mapping (Driver → AT-SPI2 / X11)

| Driver primitive | Linux (implemented) | macOS / Windows analog |
|---|---|---|
| `ensure_trusted` | probe `Atspi.get_desktop(0)`; raise a structured `PERMISSION_DENIED_ACCESSIBILITY` (bindings missing or **a11y bus unreachable**) with the exact enable-accessibility hints | TCC Accessibility grant / (Windows: integrity, no grant) |
| `snapshot` | `_atspi.find_root` (desktop-child name-substring match; `Scope.WINDOW` prefers the `ACTIVE` top-level frame) → **shared** `observe.build_snapshot` over `ATSPIAccessor`. Per node: `get_role_name`/`get_name`/`get_description`, `AtspiComponent.get_extents(SCREEN)`, Text/Value ifaces for the value, `StateSet` (`ENABLED`/`SENSITIVE`, `FOCUSED`), `AtspiAction` names | `AXUIElement` multi-attribute reads / UIA `CacheRequest` |
| `resolve_ref` | fresh `snapshot` + the shared `observe._match_anchor`; structured `STALE_REF` when the anchor no longer resolves | the *same* `_match_anchor` on all three OSes |
| `press_element` | `AtspiAction.do_action` on the first activating action (the a11y-first payoff: no pointer movement); an editable field with no action falls back to `AtspiComponent.grab_focus`; secure fields are refused | `AXUIElementPerformAction(AXPress)` / `InvokePattern.Invoke` |
| `scroll_into_view` | `AtspiComponent.scroll_to(ScrollType.ANYWHERE)` | `AXScrollToVisible` / `ScrollItemPattern.ScrollIntoView` |
| `click` | XTEST via python-xlib: absolute XTEST `MotionNotify` + `ButtonPress/Release`; modifier-clicks bracket with keysym press/release (`held()`) | `CGEvent` mouse / `SendInput(MOUSEINPUT)` |
| `drag` | XTEST: absolute motion → `ButtonPress` → absolute motion → `ButtonRelease` | `CGEvent` drag / `SendInput` |
| `scroll` | XTEST wheel = X buttons **4/5** (vertical) and **6/7** (horizontal); one button tap per notch | `CGEventScrollWheel` / `MOUSEEVENTF_WHEEL` |
| `type_text` | AT-SPI `EditableText.insert_text` on the remembered editable after verifying its owner matches the frontmost app. No widget focus is required, but missing/mismatched app ownership returns `focus_changed`; use explicit `set_value` without a detectable frontmost app. Coordinate/key/app/window changes clear the remembered target. Otherwise XTEST uses a prepared Unicode keymap and paced keystrokes. | `CGEventKeyboardSetUnicodeString` / `KEYEVENTF_UNICODE` |
| `key_chord` | XTEST via python-xlib: chord → X keysyms (`keysymdef.h` table) → keycodes → modifier `KeyPress`es, key press/release, modifier `KeyRelease`s; `validate_chord` fails fast on dry-run. (Real widget focus is needed for chords to land, so a full desktop session, not headless, is where they apply.) | `_US_KEYCODES` / VK codes via `SendInput` |
| `wait_for` | platform-free poll of the Runtime-supplied checker (re-resolution goes through `resolve_ref`); structured `TIMEOUT` | identical loop on all three OSes |
| `screenshot` | native Wayland (`WAYLAND_DISPLAY` set, no `DISPLAY`): `grim -` first; X11/XWayland: PIL `ImageGrab.grab(xdisplay=$DISPLAY)` to PNG, with grim as the last resort; structured `PERMISSION_DENIED_SCREEN` carrying X11 and Wayland hints when neither path works (headless without Xvfb) | `CGWindowListCreateImage` / DXGI Desktop Duplication (Windows: not implemented yet) |
| `zoom_region` | PIL crop of the full-screen grab | same approach on macOS |
| `frontmost_app` | EWMH `_NET_ACTIVE_WINDOW` → `_NET_WM_PID` → **`/proc/<pid>/comm`** | `NSWorkspace` frontmost / `GetForegroundWindow` |
| `app_at_point` | `_NET_CLIENT_LIST_STACKING` walked topmost-first, geometry hit-test → comm name (the act-time gating recheck) | `CGWindowList` / `WindowFromPoint` |
| `running_apps` | distinct comm names of EWMH-managed windows, with pid + frontmost flag | `NSWorkspace` / `EnumWindows` |
| `launch_app` | `subprocess.Popen([identifier])`, falling back to `gtk-launch` / `xdg-open` | `open` / `ShellExecute` |
| `activate_app` | EWMH `_NET_ACTIVE_WINDOW` `ClientMessage` to the root window (python-xlib); resolves a comm/title substring to the owning comm | `activateWithOptions` / `SetForegroundWindow` |
| `windows` | `_NET_CLIENT_LIST_STACKING` (else `_NET_CLIENT_LIST`) + `translate_coords` for root-relative bounds | `CGWindowList` / `EnumWindows` |
| `read_clipboard` / `write_clipboard` | shell out to `xclip` / `xsel` / `wl-clipboard` (first available) | `NSPasteboard` / `OpenClipboard` |

**App identity** on Linux is the process **comm name** (`/proc/<pid>/comm`, exe-basename fallback), the analog of a macOS bundle id / Windows exe for permission-keying and the safety layer's app gating.

## Linux-specific concerns (no macOS analog)

- Accessibility must be ENABLED; it is the key runtime requirement. This is the exact opposite of the Grok-desktop default, which ships with a11y OFF and drives everything by vision+coordinates. computerUse's a11y-first path needs the AT-SPI2 registry reachable, which means: `at-spi2-core` installed and running (the `org.a11y.Bus` name present on the session bus), toolkits bridging into it (`gsettings set org.gnome.desktop.interface toolkit-accessibility true`; for non-GNOME sessions export `GTK_MODULES=gail:atk-bridge`), and Chromium/Electron apps launched with `--force-renderer-accessibility` (they otherwise expose an empty tree). `ensure_trusted` (`computeruse/drivers/linux.py`) checks only the first of these: it best-effort sets `org.a11y.Status.IsEnabled`/`ScreenReaderEnabled` true on the session bus (`_atspi.enable_a11y_status`, result not checked; opt out with `COMPUTERUSE_NO_WEB_A11Y=1`) and then probes `Atspi.get_desktop(0)`; if the bindings fail to import or the desktop is unreachable it raises a structured `permission_denied_accessibility` error whose hint lists the apt, gsettings, and `--force-renderer-accessibility` steps. It does not verify that toolkits are bridged (gsettings / `GTK_MODULES`) or that Chromium/Electron apps were launched with `--force-renderer-accessibility`; an app whose tree is empty for those reasons still yields an empty snapshot, not an error. Turning an accessibility-OFF Linux desktop into an accessibility-FIRST one is the whole point of this backend.
- No batch read. AT-SPI is a D-Bus protocol with **no batch/prefetch**: every property and every `get_child_at_index` is its own cross-process round-trip (the weakness it has vs UIA's `CacheRequest` and AX's multi-attribute reads). Two mitigations in `_atspi.py`: every read is defensive (`_safe`/`_call_first`: a flaky read degrades to a sane default, never crashes the walk), and children fetched per node are capped at `_MAX_CHILDREN_FETCH` (250); the engine only walks the first `_MAX_WALK_CHILDREN` (200) anyway, so fetching a virtualized 10k-row list one round-trip at a time would be ruinous for nothing. A subtree cache/diff is the perf follow-up.
- PyGObject interface-method collision. Reading a text field's contents must use the explicit interface class form `Atspi.Text.get_text(acc, 0, -1)`. The instance form `acc.get_text_iface().get_text(a, b)` resolves to `Atspi.Accessible.get_text` (a 1-arg method) and raises `TypeError`, which, swallowed by the defensive readers, silently blanks every field value. (`get_character_count` and the EditableText/Action methods don't collide.) `_value_text` uses the explicit form.
- X11 assumptions. Input generation is XTEST-backed, window facts are EWMH, and capture is PIL's X11 grab: all X paths, which also cover XWayland windows. On a pure-Wayland session the observe/press path (pure AT-SPI, bus-only) still works and capture goes through `grim` (see the Wayland section below); the coordinate fallback and the EWMH window probes degrade. Clipboard already has the Wayland fallback (`wl-clipboard`); raw input is the follow-up (libei / RemoteDesktop portal). Headless hosts run under Xvfb (that's the live-test setup).
- Import safety. `gi`/`Atspi`/`Xlib` are imported lazily inside methods, so `computeruse.drivers` stays import-safe on macOS and Windows, the same discipline as the pyobjc and uiautomation gating.

## Wayland

Checked once by hand on 2026-08-29 under a headless **sway** compositor
(WLR_BACKENDS=headless, no X; the only record is the commit messages of 615e939
and e0d48e3): snapshot, a11y press, `EditableText` typing, and a `grim`
screenshot worked, and `key_chord` returned `unsupported`. No test or CI step in
this repo reproduces that run (the Linux CI job is Xvfb-only,
`.github/workflows/ci.yml`), so treat the matrix below as implemented-by-code
with a single manual check behind it, matching the ◐ rows in README.md.

| Capability | Wayland | how |
|---|---|---|
| snapshot / find / diff | ◐ manual check 2026-08-29 (snapshot only); `find` and diff share the path, no test | AT-SPI over D-Bus (display-agnostic) |
| press / invoke, set_value | ◐ historical manual press check 2026-08-29; explicit `set_value` remains the supported text path without detected frontmost ownership | AT-SPI `do_action` / `EditableText`, no coordinates |
| implicit type (focused) | refuses with `focus_changed` if frontmost ownership cannot be verified | The earlier manual typing result predates the ownership guard. |
| screenshot / zoom | ◐ manual check 2026-08-29 (`grim` screenshot); zoom not covered, no test | `grim` (wlroots ext-image-copy-capture); PIL X11 grab off |
| org.a11y.Status force-enable | ◐ implemented; not covered by the manual check, no test | D-Bus session bus |
| click(x,y) / drag / wheel scroll / key_chord | ⛔ `unsupported` | XTEST is X11-only → structured `ErrorCode.UNSUPPORTED` with a hint to use ref-based actions; libei/RemoteDesktop-portal input is the session-gated follow-up |

◐ = implemented; checked by hand once, not asserted by any test or CI job.

Because AT-SPI is D-Bus rather than X11, the ref-based path does not depend on
XTEST; that is why it can work on Wayland where coordinate injection does not.
`_on_wayland()` (WAYLAND_DISPLAY set, no DISPLAY) gates the X-only input paths;
`_grab_png` probes grim before PIL. Coordinate input on Wayland needs libei plus
the `org.freedesktop.portal.RemoteDesktop` portal; this is not implemented yet
(the driver returns `unsupported` with a hint). Build it on a real Wayland
session, since portal input consent is not headless-scriptable.

## Packaging

```
pip install -e '.[linux]'        # from a clone; computeruse is not on PyPI yet
sudo apt install gir1.2-atspi-2.0 at-spi2-core
```

The `[linux]` extra pulls **PyGObject** (the `gi.repository.Atspi` client, for observe + XTEST event generation) and **python-xlib** (pure Python: EWMH windowing, no build deps), both gated `sys_platform == 'linux'`; **pillow** (capture) is already a core dependency. The apt packages provide the AT-SPI2 typelib and the accessibility bus itself. For clipboard support install one of `xclip`, `xsel`, or `wl-clipboard`; headless boxes add `xvfb`.

In CI the shared core tests (`tests/test_drivers.py`, `tests/test_observe.py`, `tests/test_linux_synthetic.py`) and the MCP `build_server()` smoke run on `ubuntu-latest`, and `tests/test_linux_live.py` exercises the driver name, an AT-SPI snapshot of the GTK app, an a11y button press with an observable effect, a11y typing via `EditableText`, and the `org.a11y.Status` flip against a live bus. Coordinate click/drag/scroll, key chords, capture, EWMH windowing, and clipboard are implemented but have no live test yet. The `Driver` contract is a shared signature protocol (`computeruse/drivers/base.py`), and `tests/test_drivers.py` checks only that `LinuxDriver` satisfies it structurally, so a green Linux job shows that the platform-free engine passes on `ubuntu-latest` and that the five live-tested paths (driver name, AT-SPI snapshot, accessibility press, accessibility typing, `org.a11y.Status` flip) work against a GTK3 window; it is not a behavioural comparison with the macOS driver, which no test performs.
