# Porting a11y-computer-use to Linux

**Goal:** the same one reusable a11y-computer-use, the third `Driver` behind the platform-free core. Everything OS-specific lives in `drivers/linux.py` (+ its `_atspi` / `_linux_input` / `_linux_system` helpers); the schema, tree-pruning engine, safety layer, and MCP server are **shared and untouched**. Porting was "implement `drivers/linux.py`", never "touch the core."

**Status:** implemented; partially live-verified in CI. `tests/test_linux_live.py` (5 tests, `5 passed` on run 33436980587 at 3b331ba, `ubuntu-latest` under Xvfb + `dbus-run-session` + `at-spi-bus-launcher`) covers the driver name, an AT-SPI snapshot of a real GTK3 window, an a11y button press via `do_action`, a11y text entry via `EditableText`, and the `org.a11y.Status` flip. Coordinate click/drag/scroll, key chords, capture, `wait_for`, and `set_value` are implemented. Clipboard bytes (CR, CRLF, NUL, non-text, and invalid UTF-8) and the EWMH window verbs are live-checked under openbox by `tests/test_linux_desktop_live.py` in the Linux CI job. The Wayland matrix below reflects a manual headless-sway run and is not CI-tested. `tests/test_linux_synthetic.py` (runs on any OS, no bus) covers the role vocabulary, accessor, chord parsing, and error paths; `tests/test_linux_live.py` (Linux-only, self-skips without a reachable bus) launches a real GTK3 window (`cuatestapp`), snapshots it through the shared pruning engine, focuses its entry via AT-SPI **without moving the cursor**, and types via AT-SPI `EditableText.insert_text` (XTEST keystrokes are only the fallback when no editable was focused through the driver). A `linux` CI job (`ubuntu-latest`) runs the bus-free tests on every push and the live AT-SPI run under Xvfb + a session/a11y bus (`xvfb-run` + `dbus-run-session` + `at-spi-bus-launcher --launch-immediately`). The live step self-skips if the a11y bus is unreachable, but on the current head it runs and passes (run 33436980587 on 3b331ba: `5 passed`), mirroring how `windows-latest` verifies the Windows backend.

Xournal++ zoom sliders owned by `xournalpp` or `Xournal++` accept `150%`, `150 %`, and out-of-range bare numbers as percents when the current label and Value match the application's offset natural-log scale. Conversion uses `log(percent / 100 - 0.20753)` without step snapping. In-range bare numbers remain raw values. Percent writes require both the raw Value and the displayed percent to confirm; builds that change only the adjustment return `text_mismatch` without substituting clicks or keys. This path is covered by hermetic tests; live Xournal++ verification is pending.

Claws Mail's Setup Wizard notebook pages with empty names can use their leading
native page-title label as a tab name. The fallback is limited to that application's
Setup Wizard window and does not use later field captions. Tabs without a native
allocation remain hidden, without bounds or click actions; use the wizard's
Next/Previous buttons to change pages. This naming fallback is hermetic-tested;
live Claws Mail verification is pending.

Large Calc sheets group visible cells into tables named `Visible cells (synthetic
viewport band)` to retain the viewport through snapshot pruning. These groups
are snapshot-only, not native sheets or nested AT-SPI tables. The real sheet
retains its native name, and cells retain their native handles and measured
bounds. Short visible cell lists remain flat. This labeling is covered by
hermetic snapshot and find tests; live Calc verification is pending.

## What's already shared (no per-OS work)

- `schema.py`: the canonical Element/Snapshot/Action contract. Identical on every OS.
- `observe.build_snapshot` and the pruning engine: walks any `TreeAccessor` and prunes/indexes it. The Linux `ATSPIAccessor` feeds the *same* engine; `_match_anchor` (ref re-resolution) is reused verbatim.
- `safety.py` (tiers, gates, audit log, confirmation classifier) and the MCP server. `build_server` registers 27 tools on the Linux, macOS, and Windows drivers (`desktop_snapshot`, `find`, `screenshot`, `zoom`, `crop`, `screen_text`, `click`, `hover`, `type`, `key`, `scroll`, `drag`, `wait_for`, `act`, `set_value`, `scroll_to_find`, `app`, `window`, `clipboard`, `menu`, `file_dialog`, `files`, `notes`, `wait_until`, `request_permission`, `grant_app`, `report_issue`). The browser driver adds `console`, `network`, and `webmcp` when the driver has those methods. Platform-free.

## Files in opaque file-manager views

Thunar 4.20 exposes named cells in List View, but its Icon and Compact views
can expose only an unnamed `opaque_region`. The Linux `files` helper supports
named file tasks without clicking that surface. Configure the MCP server's
environment with `A11Y_COMPUTER_USE_FILE_ROOTS=/absolute/task/root` (multiple
roots are separated by `:`). The helper is disabled by default. This setting
grants direct filesystem access inside those roots independently of app grants.

Use `files(action="list", path="/absolute/task/root")` to discover names, then
`mkdir`, `rename`, or `move` with absolute paths. For example:

```python
files(action="mkdir", path="/absolute/task/root/archive")
files(action="rename", path="/absolute/task/root/alpha.txt",
      destination="/absolute/task/root/final.txt")
files(action="move", path="/absolute/task/root/final.txt",
      destination="/absolute/task/root/archive/final.txt")
```

The CLI can call the same helper with `run-once` and a JSON object containing
`"tool": "files"`. Paths must stay inside the configured roots; roots themselves
cannot be renamed or moved. Parents must exist. Symlink traversal, overwrites,
special-file moves, and cross-filesystem moves are refused. `rename` keeps the
same parent; `move` takes the exact destination path including its name. Listing
is limited to 200 entries and returns an error rather than a partial list.
Every attempt is audit logged before mutation, and success requires filesystem
verification. These operations change files directly; they do not select items
in Thunar. D3's live agent acceptance rate has not been measured by these tests.

## The role-vocabulary trick

Same move as the Windows adapter (`_uia.py`): `_atspi.py` maps AT-SPI role names onto the **same AX role vocabulary** the pruning engine keys off: `"push button"` → `AXButton`, `"entry"` → `AXTextField`, `"password text"` → `AXSecureTextField`, `"frame"`/`"dialog"` → `AXWindow`, and so on (the `_ROLE` table). The mapping is keyed by the human role-name string from `get_role_name()` rather than the numeric `Atspi.Role` enum, because the strings are stable across atspi2 versions and bindings. AT-SPI action names map the same way: `"click"`/`"press"`/`"activate"`/`"do default"`/… → `AXPress`, `"select"`/`"pick"` → `AXPick`, so the engine's `_PRESS_ACTIONS` interactivity test fires unchanged. `"internal frame"` maps to `AXGroup` explicitly. A Linux tree prunes and indexes through the same engine as macOS and Windows. Two Chromium cases are exceptions, and both are gated on a mark macOS and Windows nodes do not carry (`RawNode.atspi_web`): a zero-size wrapper under a web document, and a nested iframe's depth budget. See [Chromium pages](#chromium-pages) below.

Coordinates need no projection either: `AtspiComponent.get_extents(SCREEN)` returns physical pixels with a top-left origin, so `primary_geometry()` (Xlib screen size, `$A11Y_COMPUTER_USE_SCREEN` fallback) uses `scale=1.0` and the engine's point→pixel projection is an identity, the same as the Windows adapter.

AT-SPI `heading` maps to `AXHeading`, so `find(role="heading")` includes headings from Firefox and WebKitGTK. Snapshots preserve a positive integer `level` object attribute as `heading_level` and print `(level N)` beside the heading. Missing or invalid levels stay unknown; ordinary static text remains `AXStaticText`. Chromium’s heading wrapper around a single same-named button retains its existing static-text label ref and verified click behavior. This mapping and text matching are covered by synthetic tests; Firefox and Yelp live runs were not performed for this change.

AT-SPI `level bar` maps to `AXProgressIndicator` and shows its current Value with the reported minimum and maximum when the range is valid. Numeric range reads require a Value interface: a progress-role widget without one keeps its accessible child labels and has no numeric value. This behavior is covered by synthetic tests; GTK4/Baobab live runs were not performed for this change.

## Chromium pages

A Chrome document can hide a whole form under one zero-height section. On the Figma login page the raw AT-SPI tree is `document web` "Login | Figma" (1279 by 812) → `section` with height 0 (1279 by 0) → `section` (1279 by 812) → `section` → `form` → entry "EMAIL", password text "PASSWORD", push buttons "Continue with Google" and "Log in", and links. The snapshot used to print only `group "Login | Figma"` with no children. The pruner dropped any node whose bounds were zero-size, and the zero-height section took the painted form with it. Chrome does expose those children.

Under a `document web`, a `document frame`, or an `internal frame`, a zero-width or zero-height non-interactive wrapper is treated like GTK's negative-extent hollow node: it is kept only when a descendant with a real box survives, and its rect is the union of those descendants. On the live login page that wrapper is `section#react-page` at 1271 by 0 with AT-SPI actions `click` and `showContextMenu`. `click` maps to AXPress. `clickAncestor` and `showContextMenu` do not: a context menu is not activation, and clickAncestor would mark every container that holds a click listener. An unnamed, valueless, non-focusable zero-size wrapper is still hollow, and it is not a clickable target, so a ref click does not call `do_action` on it. A focusable node, a named node, or a real control (push button, link, check box, entry) with a zero box still drops. A zero-size node outside that web content, and a positive rect that lies off every display, still drop their subtrees. macOS and Windows nodes do not set the mark, so their snapshots and token budgets are unchanged. The snapshot read passes the zero size through (`_extents(..., keep_zero=True)`). Hit-testing and scrolling still treat a zero box as no box. `file_dialog` stays unsupported on Linux.

A cross-origin captcha iframe is in the tree and used to be cut by `MAX_DEPTH` (12 kept levels). On `https://2captcha.com/demo/recaptcha-v2` the raw tree is `internal frame` "reCAPTCHA" → `document web` "reCAPTCHA" → several `section`s → check box "I'm not a robot" (focusable, showing, about 29 by 29). The 0.4.32 snapshot showed the reCAPTCHA groups and then `… 2 more`. The checkbox never appeared. A nested `internal frame` restarts the kept-depth budget. A `document web` that is already inside a page restarts too, once it is deeper than the first level, so the document directly under a frame that just restarted does not spend a second restart. Controls in that frame are walked. The walk stays capped: at most four restarts per snapshot (four iframes), the raw-depth backstop of 64, and at most 64 extra reads scanning for a frame once a web subtree is already past the cap. That scan opens the frame and does not pull ordinary deep nodes into the snapshot. A ref click on the checkbox uses the existing `AtspiAction.do_action` path (`press_element` → `do_press`). No new tool is added.

hCaptcha and reCAPTCHA image-challenge tiles carry no accessible names. They are image content and stay unsolvable without vision. FunCAPTCHA / Arkose draws on a canvas the accessibility tree does not describe. This backend does not solve those tiles, and `file_dialog` stays unsupported. `ocr.ocr` (the optional `[ocr]` extra, or the system `tesseract` binary) reads text from a window screenshot or an element crop for a vision fallback; it is not wired into captcha solving. The "I'm not a robot" checkbox is a named check box; the image grid is not.

A Chrome `<input type=date>`, `type=time`, or `type=month` is a `date editor` whose segments are spin buttons. The text interface is empty. `valuetext` is the segment ("03", "17", "1994", "AM", "March"). An unset segment's `valuetext` is "0", and that is not shown. The Value interface is not the segment: `set_current_value` can leave it at 0.0, and a filled date can publish one float across the segments (the year read back as 171994.0 in #146). `set_value` on a segment types that segment and succeeds when `valuetext` matches. "03" matches "3" and, on a month input, "March". A mismatch reports that displayed text, not the float. `week` and `datetime-local` use the same editor. The address bar (`Address and search bar`) is polled after `type` until the typed URL is visible or the field settles on a different string. A settled rewrite is not `text_mismatch`; the outcome judge reads the string the bar shows. The find bar (`Find`) that already contains exactly the typed query is a match, including when the bar reopens with that query selected.

Closing that bar leaves the page scrolled to the match. Chrome then reports the content root (a viewport-sized section under the active tab's document) at a negative Y, so the box misses the screen, while the text now on screen keeps real coordinates under it. A snapshot walks that root and keeps those descendants. The group is no longer an empty `group "<title>" (focus)`. A control above the viewport, and the rest of the page below the fold, still drop.

The find bar and an omnibox popup also stick in the AT-SPI client cache. A snapshot taken while the bar is open remembers that Chrome process. Typing in the find field does too, which is what ``scroll_to_find`` does: it opens the bar, types, and closes it before the next snapshot, so that snapshot would not otherwise see the find field. The next snapshot, including one after the bar closes and one after Reload, drops that cache and walks the frame that holds the selected tab rather than a leftover find-in-page or omnibox frame. A page-window snapshot that still has a cached find-in-page or omnibox frame beside it drops that cache even when the process was not remembered yet. Reload does not reuse the cached document. The address bar by itself does not remember the process.

For Chrome/Electron processes that advertise a DevTools port, the Linux form overlay uses the optional `[browser]` extra to read and write matching DOM controls. Without `websocket-client`, snapshots and form writes retain the native AT-SPI path, including selection and numeric value writes. Native verification still determines success. Other CDP connection, script, and read-back failures remain errors; they do not retry a potentially completed write through AT-SPI. Selecting the standalone browser backend still requires the extra.

## Primitive mapping (Driver → AT-SPI2 / X11)

| Driver primitive | Linux (implemented) | macOS / Windows analog |
|---|---|---|
| `ensure_trusted` | probe `Atspi.get_desktop(0)`; raise a structured `PERMISSION_DENIED_ACCESSIBILITY` (bindings missing or **a11y bus unreachable**) with the exact enable-accessibility hints | TCC Accessibility grant / (Windows: integrity, no grant) |
| `snapshot` | `_atspi.find_root` (desktop-child name-substring match; `Scope.WINDOW` prefers the `ACTIVE` top-level frame, then includes every other showing top-level window — frame, dialog, alert, file chooser, color chooser — active first) → **shared** `observe.build_snapshot` over `ATSPIAccessor`, merged when more than one window is showing. Per node: `get_role_name`/`get_name`/`get_description`, `AtspiComponent.get_extents(SCREEN)`, Text/Value ifaces for the value, `StateSet` (`ENABLED`/`SENSITIVE`, `FOCUSED`), `AtspiAction` names | `AXUIElement` multi-attribute reads / UIA `CacheRequest` |
| `resolve_ref` | fresh `snapshot` + the shared `observe._match_anchor`; structured `STALE_REF` when the anchor no longer resolves | the *same* `_match_anchor` on all three OSes |
| `press_element` | `AtspiAction.do_action` on the first activating action (the a11y-first payoff: no pointer movement). A control whose top-level window is not ACTIVE is raised with `_NET_ACTIVE_WINDOW` first, unless that X window is already the EWMH active window. An editable field with no action falls back to `AtspiComponent.grab_focus`; secure fields are refused | `AXUIElementPerformAction(AXPress)` / `InvokePattern.Invoke` |
| `scroll_into_view` | `AtspiComponent.scroll_to(ScrollType.ANYWHERE)` | `AXScrollToVisible` / `ScrollItemPattern.ScrollIntoView` |
| `click` | XTEST via python-xlib: absolute XTEST `MotionNotify` + `ButtonPress/Release`; modifier-clicks bracket with keysym press/release (`held()`). Multi-clicks sync each intermediate press/release pair and pause 30 ms before the next pair inside the same call, so applications such as Engrampa receive distinct clicks. The gap is covered by a fake-clock XTEST regression; Engrampa was not live-tested on this branch. An on-screen `AXButton` ref uses this path instead of `DoAction`: at-spi2-atk runs the action before `dbus_connection_dispatch` returns, and `Gtk.Dialog.run()` from that handler holds the dispatch lock, so the next key waits forever | `CGEvent` mouse / `SendInput(MOUSEINPUT)` |
| `hover` | XTEST absolute `MotionNotify` plus a flush, and no button. Computer-use `mouse_move` / `move` and the MCP `hover` tool, an `act` step `{"do":"hover"}`, and `run-once` call the same `Runtime.hover`: the pointer goes to an x,y point or a ref and a tooltip or a hover-opened menu can see the motion. A point owned by another app is `focus_changed` and the pointer is not moved. Click, right-click, double-click, and drag keep their own sequences. The tool registration is covered by synthetic tests; not a live tooltip run | macOS `kCGEventMouseMoved` |
| `drag` | XTEST: absolute motion → `ButtonPress` → absolute motion → `ButtonRelease` | `CGEvent` drag / `SendInput` |
| `scroll` | `unit=lines` on a target that is not a Chromium list: XTEST wheel buttons **4/5** (vertical) and **6/7** (horizontal), one notch per unit. A Chromium list whose own box sits fully on the screen, or a list inside a shorter overflow ancestor, does not start with that wheel. One line is one content row (`_ROWS_PER_LINE` is 1 in `drivers/_atspi.py`), and the step is not larger than the rows already on screen. The driver writes that list's vertical AT-SPI scroll bar by that distance. When the list has no such bar, it calls AT-SPI `scroll_to` (`TOP_EDGE`) on a row that many lines from the on-screen head. When the bar write or `scroll_to` does not move the painted rows, a left click lands on the vertical track (the lower track to go down, the upper track to go up). A wheel is sent only when those do not move the list. A step is a success when the pixels in the list box change and the on-screen head leaves the previous row. A head that moved by about the requested lines is that step even when the grab is at or under the uniform-row floor (0.4): the bar write stays, and no track click or wheel follows. A page-sized jump is not that step. `page_unchanged` is the case where the head stays and the grab stays at or under 1. A content-height list whose parent is the document, and a document group, keep the wheel. A document group is not a list. XTEST notches do not move a Chrome document, so a line step uses a CDP `scrollBy` of about 40 pixels per line when that window's process was started with `--remote-debugging-port`, and keeps the step only when `scrollTop` changes. A window with no port keeps the wheel. A Chromium document's tree trails that wheel, so the call waits until the on-screen names change, at most 0.9s, before the next snapshot. `scroll_to_find` on a page of plain elements anchors on the titled document rather than a larger empty Chrome panel. `snapshot` lists every on-screen row of a Chromium list; 16 is the hit-test sample count (`_MAX_ROW_SAMPLES`), not a cap on that walk. A list longer than `_MAX_CHILDREN_FETCH` (250) is not read from child 0: the on-screen walk starts at the first child whose box reaches the viewport, so a painted row past that index is still listed. Rows the accessibility tree does not expose are not added. `unit=pixels`: AT-SPI `Value.set_current_value` on the nearest scroll bar by that delta (GTK scrolled windows expose the value in pixels); the write is read back. A missing bar uses a CDP `scrollTop` write when that window's process was started with `--remote-debugging-port`, and keeps the step only when `scrollTop` changes. Otherwise it sends XTEST wheel notches at the element box (about one per 80 pixels) and keeps the step only when a grab of that box changes. A step that cannot be checked, or that does not move, is `unsupported`. Native Wayland rejects the wheel fallback; the bar path and a CDP scroll do not need XTEST. The line-scroll description is the control flow in `LinuxDriver.scroll` and `_atspi.py`. The tests use synthetic trees, not a live Chrome list. Pixel scroll is covered by synthetic tests, not a live Mousepad run | macOS `CGEventScrollWheel` line vs pixel units / Windows `MOUSEEVENTF_WHEEL` (wheel not implemented) |
| `type_text` | Optional `ref` on the `type` tool resolves that element (a replaced process is `stale_ref` / `app_restarted`), focuses it, and types only after keyboard focus reads back on that accessible. Unconfirmed focus is `focus_lost` and sends no keystrokes and no insert. The outcome read-back is that element. Without `ref`: AT-SPI `EditableText.insert_text` on the remembered editable after verifying its owner matches the frontmost app. No widget focus is required, but missing/mismatched app ownership returns `focus_changed`; use explicit `set_value` without a detectable frontmost app. Coordinate/key/app/window changes clear the remembered target. Otherwise XTEST uses a prepared Unicode keymap and paced keystrokes. On X11, a Chromium single-line entry treats one trailing LF or CRLF as Enter: the payload is typed and verified in that entry before Enter is sent once. Failed payload verification withholds Enter. Focused entry discovery uses native focused states inside a focused container, including PDF toolbars; multiline fields keep their existing behavior. This path has synthetic regression coverage; no live verification is claimed. Chrome's GTK Open File (and Save) dialog is an X window with no AT-SPI location entry, so the page focus stays empty after the keys land. That read-back copies the focused location entry with select-all, collapses the selection so the chooser's Open button is not left covered, and restores the previous clipboard. The type is confirmed from that entry, not reported as `text_mismatch` with an empty page. | `CGEventKeyboardSetUnicodeString` / `KEYEVENTF_UNICODE` |
| `set_value` | An element that is not editable (the snapshot flag is false and the role is not a text field, text area, search field, or secure field) raises an error that the element is not editable. That call sends no keystrokes, clicks, or focus changes. An editable element is written with AT-SPI EditableText. Success is `Text.get_text(0, -1)` equaling the new string, the same read a snapshot uses. GTK `set_text_contents` replaces. A Chromium web field whose `set_text_contents` appends is selected and deleted, then written again. If that snapshot text remains, or the field has no EditableText, X11 focuses the field, sends ctrl+a and BackSpace, and types the new string, and that is success only when the snapshot read equals the new string. A field whose text cannot be read at all is trusted when `set_text_contents` returned true. A Chromium contenteditable with no textbox role is an AT-SPI section whose tag is `div` or `span`. `STATE_EDITABLE` on that section marks the group editable in the snapshot (`edit`) even when EditableText is absent, and `set_value` is allowed. With no EditableText the write uses the empty-contenteditable clear (focus, ctrl+a, BackSpace, Delete) and then types; success is the readable text, and one trailing newline is the `<br>`. That section is covered by a live Chrome test. Firefox's own roleless contenteditable is the same section shape and also has EditableText, so it stays editable. The edit flag uses the node's own `STATE_EDITABLE`, not EditableText support. A failed or refused write restores the previous caret and text selection, including on the document. A paragraph, panel, document, or `<select>` is not an entry: Firefox can report `STATE_EDITABLE` or EditableText on those nodes, and neither sets the edit flag. `set_value` on one raises not-editable before any select-all. An Impress or Draw paragraph is the exception when it has both `STATE_EDITABLE` and EditableText: the snapshot marks it `edit`, and `set_value` replaces it through EditableText with no select-all. A browser paragraph stays refused. A live Draw page covers a rectangle and an ellipse. A `<select>` is still written as a combo. A live Firefox test covers the paragraph, the select, a real input, and a contenteditable. The GTK replace stays on synthetic trees. | macOS `AXValue` / UIA `ValuePattern.SetValue` |
| `key_chord` | XTEST via python-xlib: chord → X keysyms (`keysymdef.h` table) → keycodes → modifier `KeyPress`es, key press/release, modifier `KeyRelease`s; `validate_chord` fails fast on dry-run. (Real widget focus is needed for chords to land, so a full desktop session, not headless, is where they apply.) A bare Return, Enter, or kp_enter while the active window is a GTK file chooser (Open File, Save File, Save As, and the other titles in `_FILE_CHOOSER_TITLES`) does not send the key. The Open or Save button's AT-SPI action runs instead, after `scroll_to` and, when the button box is missing or outside the screen, a resize or move that places the window inside the display. The chooser window then has to be gone and the opener's accessibility text or another window title has to show the file. A dialog that stays open is `unsupported` / `file_chooser_unchanged`. One that closes without the file is `unsupported` / `file_not_chosen`. A named browser's chooser with no accessibility button, including Chrome's GTK Open File dialog, is not sent the key either. The window is resized and moved inside the display, then Open is clicked inset from the bottom-right. Success requires the dialog to close and the page's file input to show the file. A page that still says "No file chosen", including when the address bar or a window title contains the filename, is `unsupported` / `file_not_chosen`. A dialog that stays open is `file_chooser_unchanged`. Any other chooser with no accessibility button still sends the key. `file_dialog` stays unsupported. | `_US_KEYCODES` / VK codes via `SendInput` |
| `wait_for` | `exists` and `gone` re-resolve through `resolve_ref` and return only when that checker does. `actionable` also reads the accessible: it has to be showing, on the primary screen, enabled or sensitive, stable (the same box on two reads at least 0.05s apart), and the process that owns the top window at its center, with the hit on that node or a descendant. The AT-SPI listener (state-changed, children-changed, bounds-changed, focus) starts on a Linux display and wakes the wait; otherwise the poll is 0.1s and never longer than the time left. While that listener is up, a snapshot drops the application's cached children before the walk, so a Chromium page that never entered the cache is still read from the bus. A timeout is `ErrorCode.TIMEOUT` and names the check that failed (`visible`, `enabled`, `stable`, `receiving_events`, or `actionable`). A sample that fails a check is not success. | identical loop on macOS and Windows |
| `screenshot` | native Wayland (`WAYLAND_DISPLAY` set, no `DISPLAY`): `grim -` first; X11/XWayland: PIL `ImageGrab.grab(xdisplay=$DISPLAY)` to PNG, with grim as the last resort; structured `PERMISSION_DENIED_SCREEN` carrying X11 and Wayland hints when neither path works (headless without Xvfb) | `CGWindowListCreateImage` / DXGI Desktop Duplication (Windows: not implemented yet) |
| `zoom_region` | PIL crop of the full-screen grab | same approach on macOS |
| `frontmost_app` | EWMH `_NET_ACTIVE_WINDOW` → `_NET_WM_PID` → **`/proc/<pid>/comm`** | `NSWorkspace` frontmost / `GetForegroundWindow` |
| `app_at_point` | `_NET_CLIENT_LIST_STACKING` walked topmost-first, geometry hit-test → comm name, or the WM_CLASS instance when the window has no pid (the act-time gating recheck). A coordinate click on a focused window with no pid gates on that class instead of `unknown` | `CGWindowList` / `WindowFromPoint` |
| `running_apps` | distinct comm names of EWMH-managed windows, with pid + frontmost flag | `NSWorkspace` / `EnumWindows` |
| `launch_app` | executable on `PATH` via `shutil.which` + `Popen`, case-insensitive (`Mousepad` starts `mousepad`); else a desktop file by id, then by `Name=` or `GenericName=` (`Text Editor`), via `gtk-launch` (its `Exec` line when `gtk-launch` is absent). A name that is none of these raises `APP_NOT_FOUND` immediately. The grant does not change which program is looked up | `open` / `ShellExecute` |
| `activate_app` | EWMH `_NET_ACTIVE_WINDOW` `ClientMessage` to the root window (python-xlib); resolves a comm/title substring to the owning comm. A launcher name (`google-chrome`) and the display name ("Google Chrome") resolve to the process comm (`chrome`); `grant` stores that same id. Synthetic only | `activateWithOptions` / `SetForegroundWindow` |
| `windows` / `raise_window` / `focus_window` / `minimize_window` / `maximize_window` / `move_window` / `resize_window` / `close_window` | EWMH client list. The app id is `/proc/<pid>/comm`, or the WM_CLASS instance when the window has no pid. `window list` reports that missing pid as null. An unfiltered list includes those mapped windows, with WM_CLASS and bounds, whether or not another window is focused. A window that is iconic or `_NET_WM_STATE_HIDDEN` is `on_screen: false` with `bounds: null`. raise and focus uniconify a minimized window (`WM_CHANGE_STATE` NormalState and `_NET_WM_STATE` remove `_NET_WM_STATE_HIDDEN`) before `_NET_ACTIVE_WINDOW`. minimize sends ICCCM `WM_CHANGE_STATE` (iconic) and `_NET_WM_STATE_HIDDEN`. maximize adds both maximized atoms. move and resize uniconify the same way, then send `_NET_MOVERESIZE_WINDOW`. close sends `_NET_CLOSE_WINDOW`. Native Wayland returns `unsupported` and names the operation. Hermetic fakes plus a live openbox run in `tests/test_linux_desktop_live.py` | `CGWindowList` / `EnumWindows`. macOS raise activates the owning app. The other verbs are `unsupported` and name the platform |
| `read_clipboard` / `write_clipboard` | `xclip`, else `xsel`, else `wl-paste` / `wl-copy`. The read is the text target's bytes decoded as UTF-8, so CR and CRLF are unchanged. Zero bytes on a text target that was read successfully is `""`. No tool, non-text targets, invalid UTF-8, and no clipboard owner are `unsupported` (`missing_clipboard_tool`, `clipboard_not_text`, `clipboard_invalid_utf8`, `clipboard_no_owner`) and do not return `""`. A missing tool on write is that same unsupported error, not an internal error. Hermetic mocks, and a live xclip run under the Linux CI job | `NSPasteboard` / `OpenClipboard` |
| `menu_items` / `menu_press` / `menu_state` / `menu_close` | AT-SPI menu bar. A GTK bar entry is role `menu`; its rows are direct children or one popup child. Titles use the shared matcher (case, ellipsis, unique prefix). List does not press when the rows are already present; a menu with no rows yet is opened and then closed. Press opens each level and activates the leaf with `do_action`. A leaf that can open a modal dialog is invoked on the application's private socket with a short timeout, so `Gtk.Dialog.run()` does not hold the shared accessibility connection; the driver then waits for the dialog window on the connection used for reads. A shortcut is the accelerator field of the AT-SPI binding (`<Primary>n` is `ctrl+n`; a bare `F11`, `F1`, or `Delete` is that key). A mnemonic with no accelerator (`a;<Alt>v:a;`, `f;<Alt>f;`, `_F;;`, or `<Alt>f;<Alt>f;`) has shortcut null and is listed as `mnemonic`, the underlined letter. `checked` is true or false for a GTK check or radio item. A Qt item whose role stays `menu item` is false when `CHECKABLE` is set and the action is off, and true when `CHECKED` is set. Qt 6.4 publishes `CHECKED` and not `CHECKABLE`, so an off item on that bridge stays null and an on item is true. A plain item is null. Open means `SELECTED` on the entry or `SHOWING` on its popup — the bar label's own `SHOWING` is not an open menu. Close sends Escape and is an error if the popup is still showing; pressing the bar entry again does not end GTK menu tracking. Hermetic on a fake tree (`tests/test_linux_menus.py`). A live PyQt6 window lists Tools → Option X, the press toggles the action, and the next list is true | `AXMenuBar` walk (macOS). Windows: not implemented |
| `file_dialog` | **unsupported**. GTK and xdg-desktop-portal file choosers are not driven. The tool result is `unsupported: file_dialog is not supported on Linux: GTK and portal file choosers are not driven by this tool`, with a hint to open the chooser and fill the location or name field from the snapshot (GTK 3: Ctrl+L focuses the location bar). Return in that chooser activates Open or Save through the button's accessibility action and checks the result; it does not make this tool drive the dialog. The Linux Runtime skips the frontmost recheck for this tool only, so a named app that is in the background, and a granted name that is not the frontmost app, get that unsupported error rather than `focus_changed`. The call does not open a chooser. The grant is still checked. Type, key, click, and `file_dialog` on the other drivers keep the frontmost recheck | `NSOpenPanel` go-to-folder (macOS). Windows: not implemented |

**App identity** on Linux is the process **comm name** (`/proc/<pid>/comm`, exe-basename fallback), the analog of a macOS bundle id / Windows exe for permission-keying and the safety layer's app gating.

HTML `section` nodes tagged `div`, `pre`, `code`, `blockquote`, `address`,
or `figcaption` expose their own AT-SPI Text as snapshot values, including
when Gecko or WebKitGTK provides no static-text children. This makes those
words available to `find` and snapshot-based `scroll_to_find`. Sections containing
only embedded controls stay valueless, and protected text stays private.
This mapping is covered by synthetic tests; Firefox/Yelp live verification
has not been performed for this change.

## Linux-specific concerns (no macOS analog)

- Qt on X11 joins the accessibility tree through the X root-window property `AT_SPI_BUS` (type `STRING`, 8-bit) or through `org.a11y.Bus` on the session bus. `at-spi-bus-launcher` writes that property when it starts. Tester launched a PyQt6 window with `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` before the property existed, and the window never appeared in the tree. The live Qt fixture reads `org.a11y.Bus.GetAddress` and writes that address onto the root before the process starts. It does not export `AT_SPI_BUS_ADDRESS`: on Qt 6.4 that variable makes the bridge emit its enable signal before the slot is connected, and the application never registers. Set the root property, and leave the variable unset, when you launch Qt outside the fixture.
- A Qt table and a Qt tree publish `table cell`, `table column header`, and `table row header`. Those map to `AXCell`, `AXColumn`, and `AXRow`. A table or outline with at most 24 children keeps every header and cell, so a 4×3 grid plus its headers is not cut at 12. A grid with more children than that still keeps 12. A live PyQt6 window finds `R3C2` by role `cell` and clicks it, then shows the Tree tab, where `Fruits` is a collapsed cell.
- A ref click on a file-manager side pane, or on a tree or list row that opens a folder, uses that row's own activation. A Thunar shortcut cell's `activate` action and Return leave the folder where it was; the click is a pointer click at the row. A tree or list row whose own action is `activate` uses that action. Return is sent only when the row has no box and no activate action. The click is `confirmed` only when the location entry or the path bar changed. A row that was only selected is `partial`. A file row inside the folder view's tab still selects and does not open. A GTK tree with no location bar still selects through the parent's Selection interface.
- A ref click on a Qt table cell is a pointer click at the cell's center. `Toggle` and `Selection.select_child` add the cell and leave `currentItem` where it was, so a later Delete or key would hit every selected cell and the old current cell. The click is a success only when that cell is the only selected cell and it is focused, which is the current cell. The outcome is `confirmed` when the accessibility state changed to that, and `refused` with `selection_unchanged` when the click did not. A tree item stays on `Toggle`, which selects the row and does not expand it.
- Accessibility must be ENABLED; it is the key runtime requirement. This is the exact opposite of the Grok-desktop default, which ships with a11y OFF and drives everything by vision+coordinates. a11y-computer-use's a11y-first path needs the AT-SPI2 registry reachable, which means: `at-spi2-core` installed and running (the `org.a11y.Bus` name present on the session bus), toolkits bridging into it (`gsettings set org.gnome.desktop.interface toolkit-accessibility true`; for non-GNOME sessions export `GTK_MODULES=gail:atk-bridge`), and Chromium/Electron apps launched with `--force-renderer-accessibility` (they otherwise expose an empty tree). `ensure_trusted` (`a11y_computer_use/drivers/linux.py`) checks only the first of these: it best-effort sets `org.a11y.Status.IsEnabled`/`ScreenReaderEnabled` true on the session bus (`_atspi.enable_a11y_status`, result not checked; opt out with `A11Y_COMPUTER_USE_NO_WEB_A11Y=1`) and then probes `Atspi.get_desktop(0)`; if the bindings fail to import or the desktop is unreachable it raises a structured `permission_denied_accessibility` error whose hint lists the apt, gsettings, and `--force-renderer-accessibility` steps. It does not verify that toolkits are bridged (gsettings / `GTK_MODULES`) or that Chromium/Electron apps were launched with `--force-renderer-accessibility`; an app whose tree is empty for those reasons still yields an empty snapshot, not an error. Turning an accessibility-OFF Linux desktop into an accessibility-FIRST one is the whole point of this backend.
- A running app that appears in app/window lists but has no AT-SPI root returns `unsupported` with `reason: no_accessibility_bridge` from app-scoped snapshots, find, and menu listing. A window first listed after the registration eligibility check remains `app_not_found` for that observation so the next call can retry registration. Snapshots preserve the remaining existing 15 s startup grace for an unambiguous newly started window owner, so a GTK4 window may register its tree after it maps. A distinct GTK program name may identify that owner through WM_CLASS when the process runs under an interpreter; a window title alone does not establish ownership. Older non-LibreOffice apps get one short registration retry; LibreOffice retains its 15 s registration grace period. This also covers an app connected to the accessibility bus without exporting a root. The hint suggests screenshots with explicit coordinates, enabling the bridge, or retrying if the app is still starting. A registered app with an empty tree remains a valid snapshot; an app absent from both AT-SPI and the lists remains `app_not_found`.
- VLC Qt AT-SPI registration can be intermittent. When VLC has no root, snapshots probe again after one short registration interval instead of waiting out process startup grace, then return `no_accessibility_bridge`. Enable Qt accessibility with `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` and restart VLC. If the bridge is still absent, use screenshots and explicit coordinates rather than repeatedly retrying tree queries. A registered VLC tree continues through normal observation.
- A frozen application does not answer AT-SPI. libatspi waits 300ms per call, and a walk of a stopped process (Mousepad under `SIGSTOP`) used to take about 19s and then succeed with an empty tree plus the custom-drawn `screen_text` note. `screen_text` has no engine on Linux. Snapshot now bounds that wait per application. A read that returns, however slow, is a live app: Firefox, Chrome, and LibreOffice cross 300ms on startup and on large documents. `app_not_responding` is raised only with evidence of a hang. The process state in `/proc/<pid>/stat` is `T` or `t` (stopped) and a query went unanswered, which fails on that first miss so a `SIGSTOP` stays inside one D-Bus timeout. The pid is the window owner when that comm matches, or the one stopped process whose command line contains the AT-SPI program name when the comm is different (`cuatestapp` on `python`). Otherwise the process is still scheduled, and the error is raised only when a cheap ping (`get_name` / `get_process_id`, retried, on a longer timeout than the walk) gets no reply across several seconds of consecutive misses. A process younger than the registration wait is not failed for those misses; the snapshot wait retries it. The note is not added. `Scope.DISPLAY` skips an application that does not answer and still returns the others. After the process continues, the next snapshot is the tree again. Ref re-resolution is unchanged: a missing anchor is still `stale_ref`, and a node that is only scrolled off or not showing keeps the code it had.
- No batch read. AT-SPI is a D-Bus protocol with **no batch/prefetch**: every property and every `get_child_at_index` is its own cross-process round-trip (the weakness it has vs UIA's `CacheRequest` and AX's multi-attribute reads). Two mitigations in `_atspi.py`: every read is defensive (`_safe`/`_call_first`: a flaky read degrades to a sane default, never crashes the walk), and children fetched per node are capped at `_MAX_CHILDREN_FETCH` (250); the engine only walks the first `_MAX_WALK_CHILDREN` (200) anyway, so fetching a virtualized 10k-row list one round-trip at a time would be ruinous for nothing. A Chromium list is the exception in `ATSPIAccessor.children`: it returns the on-screen named rows, and when the child count is above that cap the walk starts at the first child whose box reaches the viewport. Rows the tree does not expose are not added. A subtree cache/diff is the perf follow-up.
- PyGObject interface-method collision. Reading a text field's contents must use the explicit interface class form `Atspi.Text.get_text(acc, 0, -1)`. The instance form `acc.get_text_iface().get_text(a, b)` resolves to `Atspi.Accessible.get_text` (a 1-arg method) and raises `TypeError`, which, swallowed by the defensive readers, silently blanks every field value. (`get_character_count` and the EditableText/Action methods don't collide.) `_value_text` uses the explicit form.
- X11 assumptions. Input generation is XTEST-backed, window facts are EWMH, and capture is PIL's X11 grab: all X paths, which also cover XWayland windows. On a pure-Wayland session the observe/press path (pure AT-SPI, bus-only) still works and capture goes through `grim` (see the Wayland section below); the coordinate fallback and the EWMH window probes degrade. Clipboard already has the Wayland fallback (`wl-clipboard`); raw input is the follow-up (libei / RemoteDesktop portal). Headless hosts run under Xvfb (that's the live-test setup).
- Import safety. `gi`/`Atspi`/`Xlib` are imported lazily inside methods, so `a11y_computer_use.drivers` stays import-safe on macOS and Windows, the same discipline as the pyobjc and uiautomation gating.

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
| press / invoke, set_value | ◐ historical manual press check 2026-08-29; explicit `set_value` remains the supported text path without detected frontmost ownership. A non-editable element is an error and sends no input. On an editable element, `set_value` succeeds when `Text.get_text(0, -1)` equals the new string (the snapshot read). A field whose text cannot be read at all is trusted when `set_text_contents` returned true. GTK `set_text_contents` already replaces. A web field whose snapshot text disagrees is selected and deleted; if that text remains, or the field has no EditableText, X11 focuses the field, sends ctrl+a and BackSpace, and types the new string. That key path needs `DISPLAY` (native Wayland returns failure instead of success when the AT-SPI write did not stick). A contenteditable section with no textbox role is a live Chrome test: `STATE_EDITABLE` marks it editable, and `set_value` clears with ctrl+a, BackSpace, and Delete before typing. A Firefox paragraph or select is not that section: it is not marked edit, and `set_value` refuses it before select-all. An Impress or Draw paragraph with `STATE_EDITABLE` and EditableText is marked edit and replaced through EditableText, with no select-all. A Firefox contenteditable section stays editable. The other web-field key path stays synthetic. | AT-SPI `do_action` / `EditableText`, no coordinates; X11 chords when the snapshot text is not the new string |
| implicit type (focused) | refuses with `focus_changed` if frontmost ownership cannot be verified | The earlier manual typing result predates the ownership guard. |
| screenshot / zoom | ◐ manual check 2026-08-29 (`grim` screenshot); zoom not covered, no test | `grim` (wlroots ext-image-copy-capture); PIL X11 grab off |
| org.a11y.Status force-enable | ◐ implemented; not covered by the manual check, no test | D-Bus session bus |
| click(x,y) / drag / `scroll` `unit=lines` / key_chord | ⛔ `unsupported` | XTEST is X11-only → structured `ErrorCode.UNSUPPORTED` with a hint to use ref-based actions; libei/RemoteDesktop-portal input is the session-gated follow-up |
| `scroll` `unit=pixels` | implemented when a scroll bar is exposed, or when Chrome advertises a DevTools port. The X11 wheel fallback is not used on Wayland. Not part of the 2026-08-29 sway check; synthetic tests only | AT-SPI `Value` on the scroll bar, else CDP `scrollTop`. No XTEST on that path |

◐ = implemented; checked by hand once, not asserted by any test or CI job.

Because AT-SPI is D-Bus rather than X11, the ref-based path does not depend on
XTEST; that is why it can work on Wayland where coordinate injection does not.
`_on_wayland()` (WAYLAND_DISPLAY set, no DISPLAY) gates the X-only input paths;
`_grab_png` probes grim before PIL. Coordinate input on Wayland needs libei plus
the `org.freedesktop.portal.RemoteDesktop` portal; this is not implemented yet
(the driver returns `unsupported` with a hint). Build it on a real Wayland
session, since portal input consent is not headless-scriptable.

## Packaging

PyPI publishes the package (0.1.1 through 0.4.4, the current release). Install the Linux extra from PyPI:

```bash
pip install 'a11y-computer-use[linux]'
```

An agent that drives the Linux desktop needs the agent extra as well. The
two extras install together; there is no separate `[agent,linux]` package
name:

```bash
pip install 'a11y-computer-use[agent,linux]'
```

That command compiles PyGObject and pycairo in an isolated venv. PyGObject is sdist-only and pycairo has no Linux wheel, so the build needs `pkg-config` plus the cairo and girepository headers. Without those headers, pip fails while compiling one of the two.

The install that does not compile them uses the distro GI packages and a venv that can import those modules (`--system-site-packages`):

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-atspi-2.0
python3 -m venv --system-site-packages .venv
.venv/bin/pip install 'a11y-computer-use[agent,linux]'
```

`python3-gi` is PyGObject, `python3-gi-cairo` is the cairo override those bindings import, and `gir1.2-atspi-2.0` is the AT-SPI2 typelib. An isolated venv still works when the build dependencies later in this section are installed first.

`a11y-agent` refuses to start when `gi` or `Xlib` cannot be imported. The
process exits 3 with status `failed` and an install message. It does not hand
that ImportError to the model as a permission observation.

From a clone, the same extra is an editable install:

```bash
pip install -e '.[linux]'
```

**python-xlib** is a core dependency on Linux (`sys_platform == 'linux'`), so `pip install a11y-computer-use` can list X11 windows. If that import still fails, `app list`, `window list`, `key` with `app`, and an app-scoped `find` return `unsupported` with `reason` `missing_dependency` and the hint `pip install python-xlib`. They do not return an empty list as `confirmed`, and they do not wait 15s and report LibreOffice's gtk3 bridge as missing. The `[linux]` extra pulls **PyGObject** (the `gi.repository.Atspi` client, for observe) and repeats **python-xlib**, both gated `sys_platform == 'linux'`; **pillow** (capture) is already a core dependency. The package's own 0.4.4 artifacts on PyPI are a pure-Python wheel (`py3-none-any`) and an sdist, so installing `a11y-computer-use` itself does not compile anything. PyGObject 3.58.0 is sdist-only on PyPI. pycairo, which that sdist builds against, publishes Windows wheels and an sdist and no Linux wheel (1.29.1, checked on PyPI 2026-10-02), so an isolated venv that installs the extra compiles both. This repository does not publish Linux wheels for PyGObject or pycairo.

Runtime packages on Debian and Ubuntu are the AT-SPI2 typelib and the accessibility bus:

```bash
sudo apt install gir1.2-atspi-2.0 at-spi2-core
```

A source build into an isolated venv, when those wheels are absent, also needs headers and pkg-config. This is the alternative to the `--system-site-packages` install above. Issue #14 reports that on a fresh Debian 13 / Python 3.13 venv, `pip install 'a11y-computer-use[linux]'` exited 1 while building pycairo until the packages below were installed (missing pkg-config and cairo headers, then "Python dependency not found"). `python3.13-dev` matches that interpreter; another Python needs its own `python3.X-dev` as well as `python3-dev`. `libgirepository-2.0-dev` is the Debian 13 name, and the name in [PyGObject's current Debian/Ubuntu pip instructions](https://pygobject.readthedocs.io/en/latest/getting_started.html). The runtime packages above stay required either way. This list is the one from that report; a fresh Debian image was not rebuilt for the doc edit.

```bash
sudo apt install pkg-config libcairo2-dev python3-dev python3.13-dev \
    gobject-introspection libgirepository-2.0-dev \
    gir1.2-atspi-2.0 at-spi2-core
```

PyGObject's Debian/Ubuntu instructions also install `gcc`. The Debian 13 report did not install a compiler separately.

For clipboard support install one of `xclip`, `xsel`, or `wl-clipboard`; headless boxes add `xvfb`.

In CI the shared core tests (`tests/test_drivers.py`, `tests/test_observe.py`, `tests/test_linux_synthetic.py`) and the MCP `build_server()` smoke run on `ubuntu-latest`, and `tests/test_linux_live.py` exercises the driver name, an AT-SPI snapshot of the GTK app, an a11y button press with an observable effect, a11y typing via `EditableText`, and the `org.a11y.Status` flip against a live bus. Coordinate click/drag/scroll, key chords, capture, EWMH windowing, and clipboard are implemented but have no live test yet. The `Driver` contract is a shared signature protocol (`a11y_computer_use/drivers/base.py`), and `tests/test_drivers.py` checks only that `LinuxDriver` satisfies it structurally, so a green Linux job shows that the platform-free engine passes on `ubuntu-latest` and that the five live-tested paths (driver name, AT-SPI snapshot, accessibility press, accessibility typing, `org.a11y.Status` flip) work against a GTK3 window; it is not a behavioural comparison with the macOS driver, which no test performs.

Audacity's wxGTK toolbar tools may be childless AT-SPI panels with the command
name in the description. The Linux driver recognizes a native toolbar ancestor
or an immediate sibling grip described as `Audacity … Toolbar` and exposes the
command panels as buttons, keeping the grip a group, so
`find(role="button", text="Play")` includes the transport control. A childless,
unnamed track view remains an `opaque_region`, labelled `Audacity track / waveform`;
Mute, Solo, track names, Gain, and Pan painted inside it are not separate controls
in that tree. Use Audacity's `Tracks > Mute/Unmute` menus for mute operations.
The unnamed panel in the Time toolbar is labelled `Audacity Time clock` and stays
opaque, including the wxGTK layout with the toolbar description on a sibling
grip: its displayed reading is not an accessible value. When Audacity publishes
real children or names, the driver preserves them.

## Meld file comparisons

Meld 3.22.3 (GTK 3) exposes the file-diff chunk-action gutter between text
panes as a single unnamed `opaque_region`. Its painted per-chunk merge arrows
are not separate accessible controls or AT-SPI actions. Targeting a specific
gutter arrow by ref is unsupported; the gutter ref identifies the whole strip,
and a reported click on it does not verify that a change was merged. Do not
infer arrow locations from the strip's bounds.

For a merge operation, explicitly select the intended change with the named
`Go to the next change` or `Go to the previous change` header-bar buttons, then
use `Push current change to the left`, `Push current change to the right`, or
`Delete change` as appropriate. Take a fresh snapshot and check the destination
pane's text to verify the result. These are separate requested operations:
an explicit gutter-arrow click must not be silently replaced with navigation
or a header-bar action. If that arrow cannot be addressed, report the limitation.

Okular's Thumbnails sidebar can expose its painted thumbnail strip as an
empty AT-SPI filler, without page icons or page-label text. In the reported
Sidebar structure (a Thumbnails label above a filler beside a scrollbar),
the Linux driver labels that opaque region `Okular thumbnails` and includes
a navigation note. `find` cannot select a thumbnail by page number in this
tree. Use the page-number field, `Go to Page…`, or the Find bar instead.
Native names and per-page children, when exposed, retain the normal role and
label handling; the driver does not synthesize thumbnail targets or bounds.
This fallback is covered by synthetic tests; a live Okular run has not been
verified for this change.

Okular may expose its PDF page viewport as an unnamed, childless filler without
an AT-SPI Text interface. In the document pane, the Linux driver labels this
opaque surface `Okular document / page view` and includes a limitation note.
`find` and `scroll_to_find` cannot read page tokens from that surface. Explicitly
use Okular's Ctrl+F Find bar or page-number control and verify the resulting UI;
these tools do not automatically invoke in-app search. Accessible names and
page text supplied by the application remain unchanged.

### Opaque document search
`scroll_to_find` refuses in-app Find before sending keys or query text when the chosen container contains an `opaque_region`, such as AbiWord’s document drawing area. It returns `unsupported` with reason `opaque_find_unverifiable`: the accessibility tree cannot verify a match, and native Find could leave document text selected for replacement by a later `type`. This refusal does not open Find or change the document selection.

## Kate document text search
Kate/KTextEditor exposes the entire document through AT-SPI Text. Snapshots
omit Kate textarea buffer values because the editor bounds do not establish
character visibility. `find` and `scroll_to_find` cannot verify individual
document text matches; even visible buffer text is excluded until character
visibility can be established. The editor ref remains available, with a hint
to use in-document navigation. Single-line search fields keep their values.
Kate skips the generic Qt viewport-text probe: that discarded read disrupts
subsequent accessibility traversal on Kate 25.04.3. Native status controls such
as `1:1`, `INSERT`, and `UTF-8` remain available to snapshots and `find`.
This preservation was checked with a real Kate 25.04.3 under Xvfb/D-Bus for #646.
Automatic `scroll_to_find` selects this editor view ahead of sibling breadcrumb
lists. Text search returns `unsupported` with reason
`document_text_visibility_unavailable` before wheel or Find input because the
accessibility tree cannot confirm a visible match. An explicit container `ref`
still selects that container. Editor selection and refusal were checked against
Kate 25.04.3 under an isolated Xvfb/D-Bus session for #647.

The buffer suppression is covered by synthetic tests; no live Kate run
was performed for #477.

GTK text views (`text` / `editable text`) require AT-SPI `EDITABLE` to be offered as editable fields. EditableText alone is insufficient: read-only GtkTextView widgets expose it too. Snapshot and `find editable=true` exclude those views from editable matches, and `set_value` checks the live state and refuses with `not_editable` before replacing text or sending keys. Explicit `type` remains a keystroke action.

Kate's unnamed, childless filler directly left of a multiline editor is labelled
`Kate editor gutter` when native bounds establish the shared edge and height.
It remains opaque: AT-SPI does not expose individual line numbers, so this label
supports finding the gutter, not targeting a particular line. Missing or ambiguous
geometry leaves it unnamed. This recognition is covered by synthetic tests;
Kate has not been live-verified for this change.

GTK grid buttons with empty or duplicated accessible names use the nearest
sibling label to their left with overlapping vertical extents. Explicit
`labelled-by` relations take precedence. Missing geometry or equally near
labels leave the raw name unchanged. The original nonempty button name (for
example `Sans Bold`) is retained as its value when no value is exposed. This
lets snapshots and `find` identify font and colour controls by their row label;
button handles, actions, and measured bounds are unchanged. Covered by hermetic
galculator-shaped regression tests; no local live verification.


### Masked GTK 4 entries

GTK 4 PasswordEntry wrappers and their password text children are classified as
secure. Ref-based `type` and `set_value` refuse an already observed secure field
before live ref resolution, so a rebuilt child cannot turn the refusal into
`stale_ref`. The refusal is audited with the ref and role, without the input.

Password roles, password/protected/invisible states, visibility-off, and
invisible-character attributes classify an entry as `securetextfield`. Its
value is omitted, and type/set_value are refused without echoing the input.

GTK 4 can omit those signals and expose the plaintext buffer through bounded
`GetText` even when the entry is masked. For GTK 4 single-line entries the driver
checks `Text.GetStringAtOffset(0, LINE)`, which the GTK 4.18 probe showed returns
the rendered text: plain text for visible entries, bullets for masked entries.
A nonempty, unmasked layout string matching a private buffer read permits
snapshot/find readback. Custom glyphs that differ from the buffer, masking
strings and missing layout replies require human handoff. An empty rendered
line with a zero character count and no masking signal is unknown rather than
secure: ref-based type/set_value may write it once using private boolean
verification. The action result and audit input are redacted even if the entry
subsequently proves visible. The set_value write carries its private success
signal through caret restoration and the driver to the Runtime receipt, even
if the earlier visibility probe missed it; failed private verification includes
no input excerpts. A fresh snapshot classifies the rendered line:
bullets become secure, matching plain text permits readback. GTK 3 and multiline
editors keep their existing text path. State and toolkit attributes are read once
within each security probe; the layout call is limited to GTK single-line
entry/text roles outside GTK 3.

## Qt document searches

Qt multiline editors (including FeatherPad) expose the entire document through
AT-SPI Text. Native multiline states or attributes select this path; ordinary
Qt line edits retain their text without requesting document viewport ranges.
Snapshot values and text searches use `Text.get_bounded_ranges`
inside the measured editor bounds, falling back to native point offsets and
verified character rectangles when Qt omits bounded ranges. If that query
fails, times out, or returns malformed ranges and point offsets are unavailable
(as on Qt 5.15), a raw Text prefix capped at 4000 characters is read. Failed
explicit text reads retry `getText(0,-1)` only when the native character count
fits that cap, then try 256-character chunks within the same cap. Each
character's native rectangle is checked against the viewport. Unverified and
off-screen characters remain excluded from search. Each scroll re-reads
these ranges, so an off-screen match becomes searchable when it enters the
viewport. When neither interface establishes visibility, the document value is
omitted and its hint recommends in-document Find or line navigation. `find` and
`scroll_to_find` cannot confirm document text through the whole buffer in that
case. This path is covered by synthetic tests; FeatherPad has not been live-
verified for this change.

### Execution permission and launchers

With `allow_exec=False` (the default), terminal launches and input require an
explicit execution grant, independently of a FULL app grant. The same launch
gate refuses known run dialogs/app finders, script interpreters, shells, and
generic launch wrappers (including `exo-open`, `gtk-launch`, and `xdg-open`).
It checks command strings before application resolution, executable symlinks,
desktop-file Exec programs, and resolved launch arguments. Each refusal returns
`exec_disabled` and writes an audit entry. Input to known app finders is also
refused; a Python process hosting an ordinary GUI editor is not itself a
terminal input target. These name-based checks are not a process sandbox.

## gucharmap Character Table

The reported GTK 3 Character Table exposes an unnamed drawing area with no
AT-SPI children. The Linux driver labels it `gucharmap Character Table` and
explains that its ref identifies the whole opaque table. It does not publish
character cells, names, codepoints, or inferred glyph positions. `find` and
`scroll_to_find` cannot target the painted characters; scrolling may change
selection. A click on the table cannot confirm a requested character.

Use `Search > Find` or the `Go` menu only as separate requested operations,
then verify the selected codepoint and name in the statusbar or Character
Details. A statusbar match describes the current selection, not a clickable
character cell. Native drawing-area names, descriptions, and children take
precedence over this fallback label.

## Gnumeric name-box navigation

Ref-based activation of Gnumeric's `AXImage` named `go-jump` is refused with an
`unsupported` error because this name-box control has been reported to crash
Gnumeric (#492). The guard covers accessibility activation and element pointer
clicks. It does not substitute keyboard input or claim cell-address navigation
succeeded. Coordinate-only clicks do not identify an accessible control and are
not covered by this guard.

GTK 4 multiline text values include only complete lines whose AT-SPI text range
fits inside the widget's WINDOW-coordinate rectangle. This avoids whole-buffer
matches in `find` and `scroll_to_find` when SCREEN text coordinates and
point-to-offset queries are unsupported. When range extents are unavailable,
each character must have measured WINDOW extents inside the widget. Missing
geometry leaves text unsearchable. GTK versions without either extent API
retain labeled buffer contents for read-back, excluded from the search index.
Partially clipped or horizontally overflowing lines are conservatively excluded.
This does not change full-buffer write verification.

GTK 4 tree-expander buttons exposing `listitem.toggle-expand` (or the
state-appropriate `listitem.expand` / `listitem.collapse`) use that native action
for a ref click. The driver verifies a change in the live `EXPANDED` state;
a successful action acknowledgement or focus moving to another row is
insufficient. A failed expansion returns an error without a pointer fallback,
since these controls can report bounds that do not match their painted rows.

Editable web combobox inputs (ARIA autocomplete and HTML datalist) retain their combobox role and expose `edit` when their native node has `tag=input`, Text, and `EDITABLE`, without `READ_ONLY`. `set_value` uses the verified text replacement path, and `type(ref=...)` uses the focus-confirmed text path; values are not restricted to popup options. Select-style combos retain option selection. This distinction is hermetic-tested; native Chrome verification remains a CI/live check.

## Qt Quick checkable clicks

Qt Quick checkboxes, switches, and radio buttons can expose `Toggle`, `Press`,
and `SetFocus`: `Toggle` changes state without user signals, while `Press` does
nothing. Ref clicks on Qt checkable roles with that action pair use one X11
pointer click at the visible center of the reported bounds and require a stable
checked-state change. Missing or off-screen bounds, native Wayland, and an
unchanged checked state return an error; no AT-SPI Toggle follows the click.
This path is covered by hermetic tests; a live QML run has not been performed.

Qt Quick sliders and spin buttons can report a working AT-SPI Value interface
with broken minimum/maximum getters returning 0..0. These controls expose their
finite current value and accept numeric `set_value` with unknown bounds; success
requires the requested value to read back. A clamped or unchanged value is an
error. Qt spin buttons use the numeric role and writer rather than Text. Value
writes do not guarantee Qt Quick user-interaction signals such as `moved` or
`valueModified`. Other invalid bounds and non-numeric Qt roles remain excluded.


X11 wheel line input syncs each notch and waits 120 ms between successive
notches (including across axes), so Qt Quick does not receive one queued burst.
Line units describe wheel input, not a guaranteed pixel or row distance.
This pacing is covered by synthetic event-order tests; Qt Quick movement has
not been live-verified for this change.

## Qt Quick combo selection

For non-editable Qt Quick combos, `set_value` ignores disabled, non-focusable
Text display children as write targets. It opens the combo with one click at
its measured center, then polls native popup readiness within the existing
menu deadline. Each probe runs on the AT-SPI thread while waits leave that
thread free to process events. Options may belong to a newly shown menu in
that native frame or to the combobox descendants. It selects the exact option
only after rows appear. Success requires the display Text to match and the popup
to close. Missing geometry, ambiguous popups, and uncommitted selections return
errors. Editable Qt entries retain their text-write path. This behavior is
hermetic-tested; native Qt Quick verification remains a CI/live check.

Qt Quick Controls 2 menu-bar entries can expose a named `menu bar` directly
under the real menu bar with no native actions. The menu walker recognizes
these as submenus and clicks their measured native bounds on X11. It requires
a showing popup in the same window before continuing; missing bounds or an
unchanged popup returns a refusal. Listing an unopened submenu opens it and
closes it again. This path is covered by synthetic menu tests; native Qt Quick
verification remains dependent on Linux CI.

Chromium document-text `find` reveals use one native AT-SPI `TOP_LEFT`
request. PDF viewers can update extents asynchronously; if the match is
still outside the snapshot, it is returned as off-screen with no click
bounds rather than reported missing. No table-row scrollbar position is
inferred for a document-text match. This fallback has synthetic regression
coverage; native PDF scrolling still requires live verification.

Unnamed GTK single-line text fields can use a sibling label when its measured
box lies to the left and fully within the field's row. This fallback requires
one label and no other text editor on that row; missing or ambiguous geometry
leaves the name empty. Existing names, descriptions, and explicit label
relations take precedence. It changes naming only and retains the native entry
handle and bounds (including Claws Mail's Setup Wizard fields).

Chrome HTML top-layer popover panels with a reported `ispopup` mode (`auto`,
`manual`, or `hint`) and native `SHOWING` state are exposed as `AXPopover`.
Ref clicks and crops refuse with `not_visible` / `covered` when another
popover's measured bounds contain the ref's center. Controls inside their own
popover remain eligible; dismiss the covering popover before retrying a covered
ref. This check uses the accessibility tree because Chrome's accessible
hit test can return the underlying control.


Claws Mail's GTK folder and message lists can expose only childless panels
inside scroll panes in the main split-pane layout. These opaque regions are
labelled `Claws Mail folder / message list` with a note that folder rows and
message subjects are unavailable to `find` and `scroll_to_find`. The bridge
provides no list-type identifier, so the label does not distinguish the two
panes by size or position. Native names and bounds are preserved; this does
not expose row refs or enable selecting a folder or message by name.

Buttons exposing native AT-SPI IS_DEFAULT state (nick `is-default`) carry a `default` snapshot flag.
Submit queries (`submit` or `submit form`) also match these buttons, and button
role searches rank them first. No default is inferred from labels or geometry;
the marker does not change click behavior or safety checks.

Thunar toolbar location entries use confirmed focus, select-all and real typed
input for `set_value`, with text read-back. This updates the path model that
AT-SPI EditableText alone leaves stale. `set_value` does not navigate: send
`key("Return", app="thunar")` separately and verify the changed location and
window title. Without X11 keyboard input, this replacement is refused.

### Chromium PDF thumbnails

Ref clicks on Chromium buttons named `Thumbnail for page N` are refused with
`unsupported` / `pdf_thumbnail_unverified`. The native accessibility action can
acknowledge a no-op, and page navigation is not verified by this path. This
restriction also applies to fully visible thumbnails. Use a screenshot to choose
a coordinate inside the visible thumbnail and check the viewer's page number
after clicking. No scrolling or alternate action is substituted for the ref click.

### Thunar location-entry failures

After Return or KP_Enter in a focused Thunar toolbar location textfield, the
runtime checks the post-action snapshot. A `dialog-error-symbolic` image child
or an unchanged window title returns `suspected_noop` with `location not opened`
and guidance to check the path or create missing folders from the nearest
existing parent. An unchanged title alone does not prove that an error icon
exists, so the result names the observed evidence. Successful navigation keeps
the usual key result: a changed window title naming the requested folder,
together with a matching live location entry, overrides even a newly appearing
error icon. Text read-back from `set_value` does not establish that a
folder exists.

Showing HTML popover panels reporting AT-SPI `ispopup=auto|manual|hint`
appear as `popover` containers. Their notes report native SHOWING state and opener
labels from DETAILS_FOR relations when available. If a saved ref's containing
popover disappears from the fresh snapshot, resolution refuses with
`stale_ref` (`reason=popover_closed`) and asks to reopen and re-observe it,
without suggesting unrelated controls at the old tree position. Absence from
the snapshot does not prove the dismissal mechanism.

### GTK icon-view ref clicks

GTK icon-view children can report native AT-SPI `icon` or `image` roles; both
appear as `AXImage` in snapshots. Their `activate` action opens the item rather
than selecting it. Ref clicks use one pointer click at the item's measured
bounds and require that it becomes the only selected child. An unavailable
pointer path or unverified selection returns an error without invoking activate.

### GTK2 HexChat bridge limitation

[Issue #678](https://github.com/Perception-Dynamics-Inc/a11y-computer-use/issues/678)
reports HexChat 2.16.2 windows listed and focusable on X11 but absent from
the AT-SPI desktop, even with `GTK_MODULES=gail:atk-bridge`.
This remains a known limitation: without a registered tree, snapshot,
find, and accessibility menus cannot operate.

GTK2 needs both GAIL and the GTK2 ATK-to-AT-SPI bridge module.
[Debian's accessibility guide](https://wiki.debian.org/accessibility-devel)
names `libgail-common` and `libatk-adaptor`. Setting `GTK_MODULES` alone
does not install these modules. On Debian/Ubuntu, check these prerequisites:

```bash
sudo apt install libgail-common libatk-adaptor
# Fully quit the existing HexChat process first; use the same desktop session.
GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0 hexchat
```

These are troubleshooting steps, not a verified recovery for the reported
HexChat environment. Verify that HexChat appears on the AT-SPI desktop and
a snapshot returns its tree before treating it as accessible. If it remains
absent, the backend continues to return `unsupported` with
`reason: no_accessibility_bridge`. A listed/focusable window does not prove
accessibility registration. Screenshots and explicitly requested coordinate
actions are separate tools; the backend does not substitute them for ref or
menu actions, invent refs, or infer control geometry.

Unnamed GTK sliders and spin buttons use their AT-SPI `labelled-by` label as
their title, before the description fallback. Without a relation, a measured
left-hand label in the same parent and row supplies the title when the nearest
candidate is unambiguous. Explicit accessible names remain authoritative; numeric
values and descriptions remain separate from the title. This naming behavior is
covered by synthetic tests; native GTK verification is pending.


### Same-app window covers in discovery

Multi-window AT-SPI snapshots annotate clickable and editable controls with
`covered by 'window title'` when the existing X11 same-app ownership check
finds a different sibling window at the control's measured center. The note
appears in full snapshots, interactive snapshots, and `find` results; native
refs, capabilities, and bounds are retained. Missing ownership or geometry
evidence produces no cover claim. Discovery shares one lightweight X11 stacking
read within a snapshot and resolves native identity once per owning window,
using the observed parent refs instead of walking every control's native
ancestors. This cache ends with the snapshot. Clicks still recheck coverage
at action time.
This behavior has synthetic regression coverage; Xarchiver was not live-tested
for this change.

Top-level application children are enumerated with the native child cache disabled
for both window-root selection and showing-window discovery. This prevents a
new Qt window, such as Scribus's Story Editor, from remaining absent behind a
stale cached application child list. The existing cache-mask helper keeps the
count and indexed reads on the event thread and restores the default mask after
the read. Regression coverage is synthetic; this change has not been live-verified
against Scribus.

GTK header-bar apps such as Eye of GNOME can expose a **Menu** or **Main Menu**
button and a popover of ordinary buttons/checkboxes without a menu bar. Menu
listing and paths remain unsupported for that shape; the error points to
`desktop_snapshot`, clicking the menu button's ref, and a fresh snapshot's
popover control refs. If already open, use the current popover refs directly.
Do not dismiss Eye of GNOME's menu with Escape, which can quit the app.

### Eye of GNOME zoom entry

`set_value` refuses GTK percentage entries in Eye of GNOME (`eog`) with
`unsupported` / `commit_unverified` before changing their text. EditableText
read-back can match the requested percentage while the image scale stays
unchanged. Use the zoom UI and verify the displayed image scale; the backend
does not send an activation key or substitute a zoom-button click.
