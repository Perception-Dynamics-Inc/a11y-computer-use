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
