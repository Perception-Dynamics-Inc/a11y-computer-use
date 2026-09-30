# Working while the agent works

The cursor problem had an answer: ref clicks go through the accessibility
API and never move the pointer. Focus is the same problem one level up.
`app focus`, `app launch`, and anything that needs the target app in front
make macOS activate it, and activation pulls the user's screen to that app's
Space. An agent typing in TextEdit on Space 3 yanks a user who is writing in
Codex on Space 1.

## What never activates anything

| Action | How |
|---|---|
| `click(ref=...)`, `act` steps on refs | `AXPress` on the element |
| `set_value(ref=...)` | `AXValue` on the element |
| `menu`, `file_dialog` | accessibility menu bar and panels |
| `type(text, app=X)`, `key(chord, app=X)` | keystrokes addressed to X's process (`CGEventPostToPid`) |
| `app launch ... activate=false` | `open -g`: the app starts behind the current one |
| `desktop_snapshot`, `find`, `screen_text`, `wait_for`, `wait_until`, `window list` | read-only |

Addressed keystrokes reach the app's key or main window without the app
being frontmost; verified on TextEdit while another app had focus. They are
gated against the addressed app (tier `full`), not against whatever is in
front, and the recheck before injection confirms the process is still the one
the grant was decided for, so a keystroke can never land in another app. That
is a stronger guarantee than the frontmost check it replaces.

## What still needs the app in front

Coordinate clicks, drags, and wheel scrolls. AppKit drops pointer events that
carry no window, so `CGEventPostToPid` cannot deliver them; they go through
the HID tap and need the window under the point. When a task cannot avoid
one, the agent should say so before calling `app focus`.

## Background focus mode

`A11Y_COMPUTER_USE_FOCUS_MODE=background` makes the quiet paths the default:
`type` and `key` without `app` address the app of the latest snapshot, and
`app launch` starts apps behind the current one. The owner's Claude Code,
Claude Desktop, and Codex configurations set it. The default, `auto`, keeps
the classic frontmost behaviour for hosts that expect it.

## The Space limit, stated plainly

macOS's accessibility API only exposes an app's windows on the *current*
Space (the desktop the user is looking at, per display). Verified 2026-10-01:
TextEdit with two windows on another desktop answered `AXWindows` with zero
windows, and `desktop_snapshot` saw no text. So:

- On the same desktop as the user, everything above holds: the agent works
  behind the user's windows, observes through refs, types by address, and
  the user's screen never moves.
- On a different desktop, or while the user is in a fullscreen app (its own
  Space), the agent cannot observe the app at all. `app launch` still
  recognises the window (an all-Spaces window query), typing still lands,
  but observation returns an empty tree until the app and the user share a
  desktop again.

Pixels are the exception: `screen_text(app=X)` captures X's own window through `screencapture -l`, which works for a window on another Space and ignores whatever covers it, so OCR refs of an app the user is not looking at are available; acting on them by coordinate still needs the window on this desktop.

A second display changes the picture: each display has its own current
Space, so an app on the other display's desktop is observable while the
user works on theirs. A virtual display for the agent is the way to make
that reliable on a laptop; it is not built.

## Not solved yet

Screen Recording captures the whole display, so a `screenshot` while the
user is on another Space shows the user's Space, not the agent's app. Ref
observation does not have this problem. A per-window capture
(`CGWindowListCreateImage` for the app's windows, even on other Spaces) is
the next step.

## The human has the keyboard

Every activation and every keystroke the human could collide with is refused
with `user_active` while their last hardware mouse or keyboard event is
younger than `A11Y_COMPUTER_USE_USER_IDLE_S` (1.5 s): `app focus`, `app
quit`, `window raise`, coordinate clicks, drags, scrolls, frontmost typing,
and addressed typing into the app the user is currently in. Addressed
typing into any other app is unaffected. The error carries the age of the
input and a retry hint, so an agent waits instead of re-raising the app
every second while the user tries to click something, which is what "I
cannot control After Effects" looked like on 2026-10-01. Synthesized input
from this process never counts as the user's.

## Open and save panels

They are not part of the app: AppKit hosts them in
`openAndSavePanelService`. `file_dialog`, and `type`/`key` with `app=`,
address keystrokes to the process that owns the panel (the app's focused
element), and `file_dialog` reads the go-to-folder field back before it
presses Return.

