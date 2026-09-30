# Permissions in one step

Two layers gate every action, and both used to need a trip through settings by
hand. Both now come to the user.

## The macOS grants (Accessibility, Screen Recording)

macOS attaches these to the *host app* that launched the server: the
terminal, Claude, ChatGPT (for Codex), an IDE. Nobody knows that name by heart,
and the settings list is long. So the first `permission_denied_accessibility`
or `permission_denied_screen` in a process does three things on its own:

1. asks macOS to show its own dialog ("*Host* would like to control this
   computer using accessibility features", with an Open System Settings
   button), through `AXIsProcessTrustedWithOptions` / `CGRequestScreenCaptureAccess`;
2. opens the exact pane (`x-apple.systempreferences:...?Privacy_Accessibility`);
3. names the host app in the error's hint, so the agent can tell the user
   which single switch to flip.

The agent then calls `request_permission(kind="accessibility" | "screen_recording")`,
which repeats 1 and 2 if needed and waits up to 90 s for the switch, returning
`granted: true` with the host app's name. Accessibility applies immediately;
Screen Recording applies after the host app is quit and reopened.

The OS still requires the user's click and, on recent macOS, Touch ID or the
password. No process can grant itself; that is the point of TCC.

`A11Y_COMPUTER_USE_NO_OS_PROMPT=1` keeps the dialog and the pane closed
(unattended machines, test suites); the hints still name the host.

## This tool's per-app tiers

Each app the agent touches needs a tier in `~/.a11y-computer-use/permissions.json`:
`read` (observe), `click` (press elements, menus, dialogs), `full` (type text
and key chords). An ungranted app answers `needs_permission`, and the refusal
now carries the two ways to fix it:

- `grant_app(app, tier)`: the agent asks, the host shows its confirmation
  dialog (MCP elicitation) with the app, the tier, and what it allows, and the
  grant is recorded on accept. Hosts without elicitation get the command below
  instead; nothing is recorded without a human's yes.
- `a11y-computer-use grant <app> <read|click|full>` for the user at a shell
  (`grant` alone lists the grants; `--revoke` removes one).

No tool can grant itself access: `grant_app` records only what the host's
own dialog returned as accepted, and the file stays the user's to edit.

## Codex, Claude Code, Claude Desktop

| Host | Grant goes to | Note |
|---|---|---|
| Claude Code in a terminal | the terminal app (Ghostty, Terminal, iTerm2) | `doctor` names it |
| Claude Desktop | Claude.app | restart after Screen Recording |
| Codex | ChatGPT.app | Codex's own controller uses a separate helper app, "Codex Computer Use.app"; that entry does not cover this server |

## What is still manual

A helper app of our own, holding the grants once for every host the way
Codex's helper does, would remove the per-host step entirely. It needs an
app bundle and a local socket between it and the MCP process; it is the next
step, not this release.
