# Security policy

computerUse injects pointer and keyboard input and reads the accessibility trees of other applications on behalf of a language model. That is the purpose of the software, and it is why this file is longer than most. Please read the scope section before reporting: a good report tells us that the safety layer did not do what this document says it does.

## Supported versions

| Version | Where it lives | Status |
|---|---|---|
| 0.0.x | branch `computeruse-mvp` | Supported. Unreleased; install from a clone. Fixes land here as ordinary commits. |
| 0.0.1 snapshot on `main` | branch `main` | Not supported. `main` last moved on 2026-07-13 and is behind `computeruse-mvp`. |

There are no git tags, no GitHub releases, and no package on PyPI (the name `computeruse` is not registered). `pip install computeruse` does not install this project. The only supported way to run it is from a clone of the repository, as the README describes. When a versioned release exists, this table will say which releases receive fixes.

## Reporting a vulnerability

Report privately through GitHub Security Advisories on the repository:

https://github.com/Perception-Dynamics-Inc/computerUse

Open the Security tab, choose Advisories, then Report a vulnerability. The report is visible only to you and the repository maintainers until it is published.

If the Security tab offers no report button, private vulnerability reporting has not been enabled for the repository yet. In that case open an ordinary issue that says only that you have a security report and need a private channel; a maintainer will open one. Do not put technical details or proof-of-concept code in a public issue or pull request.

There is no security mailing address. This file leaves one out rather than list an address that is not monitored. If that changes, this file will be updated.

A useful report contains:

- the commit hash you tested (`git rev-parse HEAD`), the backend (`macos`, `windows`, `linux`, or `browser`), and the OS version
- the tool call or Python call that triggered the behavior, and the grant state in `~/.computeruse/permissions.json` at the time
- what you expected the safety layer to do and what it did instead
- the relevant lines from `~/.computeruse/audit/<date>.jsonl`, with anything personal removed

## What to expect after you report

computerUse has a very small maintainer team and no dedicated security staff. A maintainer will acknowledge your report after reading it, tell you whether we consider it in scope, and keep you informed while a fix is prepared. We do not promise a fixed turnaround.

Fixes land on `computeruse-mvp` as normal commits, since there is no release channel to backport to yet. When a fix lands we publish the advisory, and we credit you in it if you want credit. Please hold public details until then. If we go silent for an unreasonable time, publishing is your call.

## Scope and threat model

### What the software does

- Reads the accessibility tree of a target application (or of a Chromium tab over the Chrome DevTools Protocol) and sends a pruned text rendering to the model.
- Captures the screen and sends downscaled pixels to the model.
- Injects clicks, drags, scrolls, key chords, typed text, and clipboard writes into the operator's session.

Whatever the model does through these tools, it does with the operator's desktop privileges. On macOS those privileges are bounded by the Accessibility and Screen Recording grants of the responsible process. On Windows there is no OS-level grant at all; `WindowsDriver.ensure_trusted` is a no-op. On Linux the only requirement is a reachable AT-SPI2 bus.

### Who is trusted

The human operator is trusted. So is the host process that launches the server (`computeruse mcp`, which speaks MCP over stdio and opens no network port) or that imports `computeruse.server.Runtime` in-process. Anyone who can write to that process's stdin, edit its permission file, or set its environment variables has the same power as the operator. The safety layer does not defend the operator against local software running as the same user, and it is not a sandbox.

The model is not trusted, and neither is the UI content it reads. The safety layer's job is to keep a confused or manipulated model inside bounds the human approved, and to leave a record of what happened.

### What the safety layer does

Every tool, observation included, goes through one method: `Runtime._run_gated` in `computeruse/server.py`. The order is fixed. Permission check, then confirmation, then a recheck of what is in front or under the pointer, then the driver call, then an audit entry. Refusals and driver errors are audited before they propagate.

Per-app permission tiers. `computeruse/safety.py` defines three tiers. `read` covers snapshots, `find`, screenshots, `zoom`, `wait_for`, clipboard reads, the browser `console` and `network` feeds, and the list verbs of `app` and `window`. `click` adds pointer actions plus the `launch`, `focus`, and `raise` verbs. `full` adds typed text, key chords, `set_value`, and clipboard writes. An app with no grant gets `needs_permission`, for reads as well as for input. An app on the deny list gets `deny` regardless of tier. A non-empty allow list denies everything not on it. Grants are keyed by app identity: bundle id on macOS, process image name on Windows, process comm name on Linux, CDP tab id on the browser backend. No tool or prompt lets the model grant a tier. The only CLI path that writes a grant is the operator-invoked `computeruse agent --grant`, which runs before the model sees anything. Grants are written by a human editing `~/.computeruse/permissions.json`, by an operator passing `computeruse agent --grant <read|click|full>` on the command line (which calls `PermissionStore.set_tier` for the target app before the agent loop starts and persists to the same file), or by an embedder calling `PermissionStore.set_tier`. The store re-reads the file when its modification time or size changes, so a deny added mid-session takes effect on the next call.

Same-window recheck. `type` and `key` re-read the frontmost app immediately before injection and abort with `focus_changed` if it is not the gated app. Ref-based `click`, `scroll`, `drag`, and `set_value` hit-test the target point and abort with `focus_changed` if another app's window now owns it. On the browser backend both rechecks compare against the bound tab.

Secure fields. Snapshots mark password fields and never emit their value. Every backend's `press_element` and `set_value` refuse a secure element (on Windows the guard exists but is never reached, because the UIA accessor marks nothing secure; see item 3 below). On macOS the synthetic-click fallback also refuses secure elements, and `type` refuses when `IsSecureEventInputEnabled` is set or the system-wide focused AX element is an `AXSecureTextField`. The result is a structured `secure_field` error and the human types the secret.

Confirmation gate. A ref click whose label contains one of `delete`, `move to trash`, `empty trash`, `trash`, `discard`, `erase`, `uninstall`, `permanently`, `wipe`, or `don't save` needs a human yes through MCP elicitation before it fires. When the host cannot elicit, the click is blocked with `confirmation_declined` rather than fired. `COMPUTERUSE_CONFIRM=0` disables the gate. Details are in `docs/confirmation-gate.md`.

Audit log. Every gated call appends one JSON line to `~/.computeruse/audit/YYYY-MM-DD.jsonl`, named for the UTC day. The log is always on. There is no switch to disable it, only a constructor argument to relocate it. Typed text and key chords are replaced with `[REDACTED]` when the action hit a secure field. Clipboard-write text, and the `value` of any clicked or dragged element, are redacted unconditionally.

### What it does not protect against

Each item below is verified in the code as of the commit this file was written against. They are known limits, not open vulnerabilities, and a report that only restates one of them will be closed with a pointer here.

1. A model holding `full` on an app can do anything the operator can do in that app, including sending a message or deleting a file through a control whose label is not in the destructive list. Tiers bound which apps the model may touch, not what it intends.
2. The confirmation classifier is a substring match on a ref click's label. It does not classify coordinate clicks, typed text, key chords (a destructive shortcut, say), drags, or `set_value`, and it knows nothing about app context. `docs/confirmation-gate.md` records these as Phase-1 follow-ups.
3. Secure-field handling differs by backend:

   | Protection | macOS | Windows | Linux | Browser |
   |---|---|---|---|---|
   | Password field marked in the snapshot, value never emitted | yes (`AXSecureTextField`) | no (`_uia.py` maps `EditControl` to `AXTextField` and never emits `AXSecureTextField`; there is no `IsPassword` check, so nothing is marked secure and values are not withheld) | yes (AT-SPI role `password text`) | yes (`<input type="password">`) |
   | `press_element` and `set_value` refuse a secure element | yes | guard present but unreachable (`windows.py` checks `element.secure`, which is never true on Windows) | yes | yes |
   | Coordinate click on a secure element refused | yes | coordinate click not implemented | no | no |
   | `type` refused while a password field has focus | yes (two probes) | no | no | no |

   On Linux and in the browser, a click that falls back from a refused `press_element` to a pointer event proceeds. Only macOS probes for a focused password field before typing. On Windows, password-field protection is absent: the secure flag is never set, so the `press_element` and `set_value` refusals never fire, and a password edit's UIA value, if the platform exposes it, is emitted in the snapshot.
4. The recheck runs for typing, keys, ref clicks, scrolls, drags, and `set_value`. It does not run for `wait_for`, the observation tools, `app`, `window`, `clipboard`, or the inner scroll steps of `scroll_to_find`. The re-snapshot taken for `verify=true` is not gated or audited on its own. When the pointer recheck cannot determine the window owner (the menu bar and the desktop are not in the window list), it lets the action proceed rather than blocking.
5. `set_value` on a secure field is refused before the gate runs, so that refusal leaves no audit entry; ref resolution (`stale_ref`) also runs before the gate for every ref-targeted tool and is unaudited. Invalid arguments (a bad modifier, an unparseable chord, a missing target) are rejected before the gate as well and are not audited.
6. The clipboard is cross-app. A `read` grant on whatever app is frontmost exposes whatever the operator last copied anywhere. On macOS, typed text longer than 50 characters is staged through the pasteboard and pasted with cmd+v; the previous contents are restored after a delay unless something else wrote to the pasteboard in between, and clipboard managers may record the transient text.
7. A screenshot captures the entire display, other apps and notifications included, and is gated only against the frontmost app's `read` tier.
8. UI text is untrusted model input. Element titles, values (up to 200 characters each), placeholders, page console output, and request URLs are handed to the model as text. computerUse does not filter that text for instructions. A window or page that says "click Delete" is a prompt-injection vector by construction; the tiers and the confirmation gate are the mitigation, with the limits listed above.
9. `COMPUTERUSE_CONFIRM` and `COMPUTERUSE_AX_CLICKS` are read once, when `computeruse.server` is imported. Whoever controls the process environment controls the gate.
10. Two side effects reach beyond the target app. On macOS, snapshotting a Chromium or Electron app sets `AXManualAccessibility` and `AXEnhancedUserInterface` on that app so it builds its accessibility tree. On Linux, the driver sets `org.a11y.Status.IsEnabled` and `ScreenReaderEnabled` to true on the session bus, which tells Chromium and Electron apps already running in the session to build their accessibility trees; the flag is session-wide, not per app. `COMPUTERUSE_NO_WEB_A11Y=1` turns both off.

### Files computerUse writes

`~/.computeruse/permissions.json` holds the grants:

```json
{
  "apps": {"com.apple.TextEdit": {"tier": "full"}},
  "deny": ["com.example.banking"],
  "allow": []
}
```

Anyone who can write this file can grant `full` to any app, and the running server picks the change up on its next check.

`~/.computeruse/audit/YYYY-MM-DD.jsonl` holds one entry per gated call with the fields `ts`, `app`, `action`, `params`, `decision`, `result`, and (on success) `metrics`. `params` carries the action's arguments. For a click that means the target element's role, title, path, and bounds, with its value redacted. For `type` it is the typed text in the clear, unless a secure field was hit. For `key` it is the chord. So the log contains UI text: window and button titles, and whatever the model typed into non-secure fields. Treat it as sensitive.

Both files are created with the process's default file permissions. computerUse sets no restrictive mode and encrypts nothing. Embedders can move both locations; see below.

### What the model receives

A snapshot carries, for each kept element, its role, title, value (clipped to 200 characters in the data and 48 in the rendering, omitted for secure fields), placeholder, state flags, and geometry. A screenshot is the whole display, downscaled. A clipboard read is the operator's current clipboard. On the browser backend, `console` returns the page's console messages and uncaught exceptions, and `network` returns request method, URL, and status or failure. Through `computeruse mcp`, all of this leaves the machine if the model runs remotely; computerUse has no view of where the MCP host sends it. Through `computeruse agent`, computerUse sends it itself; see the next section.

### Reference agent loop

`computeruse agent` (`computeruse/agent.py`, planners in `computeruse/providers.py`) is the one code path where computerUse itself makes outbound requests; `computeruse bench h2h` (`computeruse/h2h.py`) drives the same planners and shares it. Each planner turn posts the conversation so far, including snapshot text and, for the `anthropic` and `openai` planners, base64 PNG screenshots, to the chosen provider over HTTPS with the standard library's `urllib`:

- `anthropic`: `ANTHROPIC_BASE_URL` (default `https://api.anthropic.com`), authenticated with `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`.
- `openai`: `OPENAI_BASE_URL` (default `https://api.openai.com/v1`), authenticated with `OPENAI_API_KEY`; any OpenAI-compatible endpoint, local or remote, can be set here, and the key is required only for `api.openai.com`.
- `claude-cli`: runs the local `claude` binary via `subprocess` with `--strict-mcp-config`; no HTTP from computerUse. Under `computeruse agent` no image content is passed (screenshots reach it as text). In the pixel modes of `computeruse bench h2h` the newest screenshot is written to a temporary PNG and the CLI is allowed only its `Read` tool to open it. Where that CLI sends the prompt is governed by its own configuration.

`COMPUTERUSE_PROVIDER` or `--provider` selects the planner; with neither set, the first of those credentials found in the environment wins, then a `claude` binary on `PATH`. computerUse reads these credentials from the environment, stores none of them, and adds no verification of the endpoints beyond the standard library's default TLS handling. The `agent_step` and `agent_run` audit rows it writes carry the tool name, the parameters, the result, and planner token counts, not credentials. `bench h2h` keeps its permission store and audit log under a temporary directory rather than `~/.computeruse`.

`--grant read|click|full` writes a tier for the target app to `~/.computeruse/permissions.json` before the loop starts, exactly as if the operator had edited the file; it is an operator flag, not something the model can invoke. Every action the planner chooses still goes through `Runtime._run_gated`, so the tiers, confirmation, and audit described above apply unchanged, and the MCP server path (`computeruse mcp`) still opens no network port and makes no outbound requests.

### Browser backend

With `COMPUTERUSE_DRIVER=browser`, the driver fetches `/json` from `COMPUTERUSE_CDP_ENDPOINT` (default `http://127.0.0.1:9222`) and opens a WebSocket to the tab it binds. computerUse adds no authentication on top of the Chrome DevTools Protocol, and Chromium's remote debugging port has none of its own, so any process that can reach that port has full control of the browser regardless of computerUse's grants. Keep the port on loopback, as the default is, and do not forward it. Grants on this backend are keyed by CDP target id.

## Guidance for embedders

Your application is the responsible process for OS permissions. On macOS, TCC attributes the Accessibility and Screen Recording grants to the nearest ancestor `.app` bundle in the process tree; `computeruse doctor` names it. Your users grant those permissions to your app once, and everything you embed, computerUse included, inherits them. In the operating system's eyes you are the accountable party.

Beyond that:

- Your product decides grants. `PermissionStore.set_tier` is the only grant API (the `computeruse agent --grant` CLI flag is a thin operator-invoked wrapper over it), and computerUse ships no prompt to ask the human. Build that prompt yourself and never expose `set_tier` to the model.
- Pass a `confirm` callback into `Runtime.click` and `Runtime.act_batch`, or accept that destructive clicks are blocked. Do not ship with `COMPUTERUSE_CONFIRM=0` unless your product has taken over that responsibility in some other way.
- Construct `Runtime(store=PermissionStore(path=...), audit=AuditLog(dir_path=...))` to keep both files where your application controls the permissions; the defaults live under the user's home directory.
- Set `COMPUTERUSE_CONFIRM` and `COMPUTERUSE_AX_CLICKS` before importing `computeruse.server`, not after.
- If your agent loop is reachable over a network, authenticate there. computerUse trusts its caller and has no authentication of its own.
- On Windows there is no OS permission prompt to fall back on; computerUse's tiers are the only gate. On Linux the same is true, and the accessibility bus side effect in item 10 above applies to the whole session.
- Treat the audit log as sensitive data under your data-handling rules. It cannot be disabled, only relocated.

## What to report

- A driver call that executes without passing through `Runtime._run_gated` and `safety.check_action`.
- An action that runs at a lower tier than `safety.required_tier` says it needs.
- A way past the frontmost or hit-test recheck on a path where this document says it runs.
- Secure-field content, clipboard-write text, or a clicked element's value reaching the audit log unredacted.
- Typing into a focused password field on macOS despite the two probes.
- UI content (titles, values, console output) that changes how computerUse itself parses or gates, for example by forging a snapshot header or an element ref.
- A problem in how computerUse uses one of its dependencies: `mcp`, `pillow`, the `pyobjc` frameworks, `websocket-client`, `uiautomation`, `PyGObject`, or `python-xlib`.

## Out of scope

- Anything that requires write access to `~/.computeruse` or to the server's process environment.
- Actions the model performed within a tier the human granted.
- A destructive button whose label is not in the substring list. That is a documented limit; suggestions for the list are welcome as ordinary issues.
- Bugs in the operating system's accessibility APIs, in Chromium's DevTools Protocol, or in the MCP host application. Report those upstream.
- Reports against `main`.

## Configuration that affects safety

| Setting | Effect | When it is read |
|---|---|---|
| `~/.computeruse/permissions.json` | Per-app tiers, deny list, allow list | On every check; re-read when the file changes |
| `~/.computeruse/audit/` | Audit log directory | On every gated call |
| `COMPUTERUSE_CONFIRM=0` | Disables the destructive-click confirmation gate | Once, at import of `computeruse.server` |
| `COMPUTERUSE_AX_CLICKS=0` | Forces synthetic mouse clicks instead of accessibility press actions | Once, at import of `computeruse.server` |
| `COMPUTERUSE_DRIVER` | Selects the backend (`macos`, `windows`, `linux`, `browser`) | When the driver is created |
| `COMPUTERUSE_CDP_ENDPOINT` | DevTools endpoint for the browser backend | When the browser driver is created |
| `COMPUTERUSE_NO_WEB_A11Y` | Disables the Chromium/Electron and Linux accessibility force-enable | When a snapshot or the Linux driver's trust check runs |
| `COMPUTERUSE_PROVIDER`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` | Planner and endpoint for `computeruse agent` and `computeruse bench h2h`; not read by the MCP server | When the provider is constructed (`computeruse/providers.py`) |

The MCP transport is stdio only; `computeruse mcp` calls `build_server().run(transport="stdio")` and listens on no port. The outbound requests described under Reference agent loop are made only by `computeruse agent` and `computeruse bench h2h`, never by the server.
