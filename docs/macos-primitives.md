# macOS primitives: menus, file panels, app lifecycle

Two parts of a Mac app stay accessible even when its content is custom-drawn
(After Effects, Figma, games, Electron shells before their tree is enabled):
the menu bar and the system open and save panels. These tools drive both, so
an agent can reach Export, Render, Preferences, or a file path without
guessing pixels.

## Menu paths

```text
menu(app="After Effects", path="File > Export > Add to Render Queue")
menu(app="TextEdit", action="list")                 # top-level menus
menu(app="TextEdit", action="list", path="File")    # items of one menu
```

- Paths are written like a manual: titles separated by `>` (`→` and `»` are
  accepted too). Matching is case-insensitive, ignores a trailing ellipsis
  (`Save As…` matches `save as`), and accepts a unique prefix.
- `list` returns JSON rows: `title`, `enabled`, `shortcut` (rendered as a
  chord, for example `alt+shift+cmd+s`), `submenu`, `checked`.
- `press` opens each level in turn and reads the items again after every
  level opens. That matters: apps rebuild menu items and validate their titles
  and enabled state only when a menu is displayed, so `Show Fonts` becomes
  `Hide Fonts` at that moment and a handle captured earlier can be dead. A
  direct `AXPress` on a deep item is accepted by many apps and does nothing
  while its menu is closed.
- Errors are structured: an unknown component returns `app_not_found` with
  the titles that were available at that level, a disabled item returns
  `unsupported` with `reason: disabled`, and open menus are closed with
  Escape before the error is raised.
- Tier: `read` to list, `click` to press, gated against the app. A press whose
  label reads like an irreversible action (Delete, Move to Trash, Discard,
  Erase) goes through the same confirmation gate as a destructive click.

## File panels

```text
menu(app="TextEdit", path="File > Save As")
file_dialog(action="save", path="/Users/me/out/final.mp4")

menu(app="Preview", path="File > Open")
file_dialog(action="open", path="/Users/me/Downloads/evidence.txt")
```

`file_dialog` finds the frontmost `NSOpenPanel` or `NSSavePanel` (a sheet on
a window, or a panel window; classified by its buttons), opens the go-to-folder
sheet with `cmd+shift+g`, types the path, and presses Return. For a save it
types the directory, sets the file name field through accessibility (typing
is the fallback), and presses Return. It returns JSON with the steps taken.
No panel showing, or a panel of the other kind, is a structured
`unsupported` error. Tier `full`, because it types.

## App lifecycle

- `app launch <name>` returns after the app's first window appears (up to
  20 s) and includes the window title, so the next snapshot sees a ready app.
- `app focus <name>` waits until the app is frontmost (up to 5 s) and says so
  when another app kept focus.
- `app quit <name>` brings the app forward, sends `cmd+q`, and reports one of:
  quit; still running with a dialog showing (unsaved changes, a human's
  decision); still running. Tier `full` (it is a key injection).

## Copying text between apps

There is no dedicated copy tool; the primitives compose:

```text
click(ref="e12")            # or set focus with set_value / a triple-click for a line
key(chord="cmd+a")
key(chord="cmd+c")
clipboard(action="read")    # the text, as a string
app(action="focus", name="Google Chrome")
click(ref="e7")
type(text=<the text>)       # or clipboard write + cmd+v for long text
```

`clipboard read` is tier `read`; the writes and pastes are tier `full`.

## Other backends

Windows, Linux, and the browser driver answer these tools with a structured
`unsupported` error and a hint. AT-SPI exposes menu bars on Linux, so the
same walk can be implemented there; the browser has no native menus (drive
page controls by ref).

## What is verified

- Hermetic on every OS (`tests/test_menus.py`): path parsing and matching,
  shortcut rendering, list and press over a fake accessibility tree including
  disabled, unknown, and refused items, panel classification and the
  go-to-folder drive for open and save, tiers, confirmation, audit rows, app
  launch and focus waits, quit with and without a lingering dialog.
- Live on this Mac with the Accessibility grant (`tests/test_macos_menus_live.py`,
  2026-09-19): `menu list` of TextEdit's File menu returns the real items and
  shortcuts (`New` = `cmd+n`, `Open…` = `cmd+o`, `Open Recent` is a submenu);
  `Format > Font > Show Fonts` then `Hide Fonts` both press, and a second
  `Hide Fonts` is refused with `Show Fonts` listed as available, which shows
  each press took effect.
- Not live-verified: `file_dialog` and the `app quit` dialog check. The shell
  these were built in has no window-server session, so window and sheet
  elements do not resolve there (every window read back as the application
  element) while menu-bar elements do. They are covered hermetically only;
  run `file_dialog` against a real Save As panel from a terminal with a GUI
  session before relying on it.
