# Embedding a11y-computer-use

a11y-computer-use is a library you embed in your AI platform, not an end-user app. There are two integration shapes for embedding the Runtime; three further scripts run a model loop on top of it (see [Agent-loop examples](#agent-loop-examples) below). Both shapes run under your app's identity, so your Developer ID or Authenticode signature and your OS permission grants apply; a11y-computer-use ships no certificate of its own.

| | [`inprocess_python.py`](./inprocess_python.py) | [`mcp_subprocess.mjs`](./mcp_subprocess.mjs) |
|---|---|---|
| Shape | Import the library, in-process | Spawn `a11y-computer-use mcp` as a child, speak MCP over stdio |
| Host language | Python | Any language with an MCP stdio client. Node is shown in `mcp_subprocess.mjs` (not run by tests or CI); the Python `mcp` client is the one exercised against the server, in `tests/e2e/test_mcp_stdio.py`. No other language has been run in this repo. |
| Identity | runs as your process | child of your process; you own responsible-process attribution |
| Best for | Python agents | non-Python platforms, or process isolation |

The other four scripts are not integration shapes. [`web_a11y_demo.py`](./web_a11y_demo.py) is a macOS-only diagnostic, described below. [`agent_task.py`](./agent_task.py), [`anthropic_computer_use.py`](./anthropic_computer_use.py), and [`openai_computer_use.py`](./openai_computer_use.py) embed the in-process shape and add a planner or a provider adapter on top; they drive the Runtime from a model and are described under [Agent-loop examples](#agent-loop-examples). No script in this directory is run by tests or CI (`grep -rn examples .github/workflows/ci.yml tests/` finds nothing), and the two provider loops have been exercised only in their scripted mode.

## What each example does

`inprocess_python.py` grants the `read` tier to `com.apple.finder` in the default permission store (`~/.a11y-computer-use/permissions.json`), builds `server.Runtime(store=store)`, prints `runtime.desktop_snapshot("com.apple.finder", scope="window")`, and counts the `(click` and `(edit` flags in that text. The act calls (`click(ref=...)`, `type_text(...)`, `scroll(ref=..., into_view=True)`) are printed as comments only. Nothing is clicked or typed.

`mcp_subprocess.mjs` spawns `a11y-computer-use mcp` through the MCP SDK's `StdioClientTransport` with `command: "a11y-computer-use"` and `args: ["mcp"]`, lists the tools, calls `desktop_snapshot` with `{ app: "com.apple.finder", scope: "window" }`, prints the returned text, and closes. The `click` and `type` calls are commented out. It needs `@modelcontextprotocol/sdk` installed and `a11y-computer-use` on `PATH`.

`web_a11y_demo.py` is macOS-only. It picks a running Chromium or Electron app by bundle id through `NSWorkspace` (or takes one as `argv[1]`), snapshots it twice with `observe.snapshot(Scope.APP, app=...)`, once with `A11Y_COMPUTER_USE_NO_WEB_A11Y=1` and once without, and prints the element and interactive counts side by side plus up to eight of the newly addressable lines. It calls `observe` directly, so it bypasses the `Runtime`, the permission store, the audit log, and the driver seam.

## Run them on macOS

```bash
# In-process (Python). Reads Finder's accessibility tree (read tier, side-effect-free).
python examples/inprocess_python.py

# MCP subprocess (Node). Any MCP-speaking language does the same.
npm i @modelcontextprotocol/sdk
node examples/mcp_subprocess.mjs

# Before/after accessibility force-enable on a running Chromium/Electron app.
python examples/web_a11y_demo.py                 # auto-picks a running app
python examples/web_a11y_demo.py com.google.Chrome
```

All of the examples that touch a native app need Accessibility granted to whatever runs them (your app, or your terminal while developing). That is the point: the grant attaches to the host, and a11y-computer-use inherits it. `a11y-computer-use doctor` reports the responsible app and the state of both grants.

## Agent-loop examples

None of these is run by CI or the test suite. All three build `server.Runtime()`, so `A11Y_COMPUTER_USE_DRIVER` selects the backend as described in the next section.

`agent_task.py` runs one task through the reference agent loop (`a11y_computer_use.agent.run_task`, the same loop behind `a11y-computer-use agent`; details in `docs/agent-loop.md`). It grants the `full` tier to the frontmost app (`runtime._frontmost()`; the bound tab on the browser backend), picks a planner with `providers.get_provider(model=...)`, and prints each step plus the final summary and token usage. It has no offline mode: the planner is `anthropic` if `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set, else `openai` if `OPENAI_API_KEY` or `OPENAI_BASE_URL` is set, else `claude-cli` if a `claude` binary is on `PATH`, else `get_provider` raises `ProviderError` (`a11y_computer_use/providers.py`). `A11Y_COMPUTER_USE_PROVIDER` overrides the choice; a second argument is the model name.

```bash
python examples/agent_task.py "Open the File menu and read the first item"
A11Y_COMPUTER_USE_DRIVER=browser python examples/agent_task.py "Fill Name with Alice and press Go"
OPENAI_BASE_URL=http://localhost:11434/v1 A11Y_COMPUTER_USE_PROVIDER=openai python examples/agent_task.py "..." llama3.1
```

`anthropic_computer_use.py` is an executor for Anthropic's native `computer` toolset via `a11y_computer_use.adapters.AnthropicComputerAdapter` (details in `docs/provider-adapters.md`). With the `anthropic` package installed and `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`) set it runs a live loop (model `claude-opus-5`, up to 40 turns). Otherwise it replays a fixed six-action sequence (screenshot, left_click, type, key, scroll, screenshot) through the adapter and prints each result, so the wiring is visible with nothing but this repo. `A11Y_COMPUTER_USE_APP` names the app whose tree backs snap-to-ref.

`openai_computer_use.py` is the same shape for OpenAI's Responses API `computer` tool via `OpenAIComputerAdapter`. With the `openai` package and `OPENAI_API_KEY` it runs live (`OPENAI_COMPUTER_MODEL`, default `gpt-5.4`; `OPENAI_COMPUTER_PREVIEW=1` switches to the deprecated `computer_use_preview` shape with the `computer-use-preview` model), up to 40 turns. Otherwise it replays one scripted `computer_call` with five actions (screenshot, click, type, keypress, scroll) and prints the resulting `computer_call_output`.

```bash
ANTHROPIC_API_KEY=... python examples/anthropic_computer_use.py "Open the File menu"
OPENAI_API_KEY=... python examples/openai_computer_use.py "Open the File menu"
python examples/anthropic_computer_use.py   # no key: scripted replay
```

The scripted replays still execute real actions (a click at 100,100, typing, Return, a scroll) against whatever app is frontmost or the bound tab, subject to the permission tier of that app.

## Mission example

[`missions/agency-demo.toml`](./missions/agency-demo.toml) is a six-phase mission for `a11y-computer-use mission run` (format in `docs/missions.md`, story in `docs/missions/agency-demo.md`): read a brief in Telegram, design in Figma, generate a video in Chrome, edit it, deploy a site, reply with the link. Each phase names its apps, step budget, and runner-side checks. `a11y-computer-use mission validate examples/missions/agency-demo.toml` checks the file; running it drives real apps on your Mac and needs the Accessibility and Screen Recording grants. It has not been run end to end yet; two of its apps (After Effects, Media Encoder) are commented out where they are not installed.

## The same examples on the other backends

`server.Runtime()` takes its driver from `drivers.get_driver()`, which reads `A11Y_COMPUTER_USE_DRIVER` (`macos`, `windows`, `linux`, `browser`) and falls back to the current OS (`a11y_computer_use/drivers/__init__.py:40`). `a11y-computer-use mcp` builds its `Runtime` the same way, so both integration shapes follow the variable. One thing does not: `web_a11y_demo.py`, which calls the macOS `observe.snapshot` directly. The `a11y-computer-use snapshot` subcommand goes through `get_driver()` as well.

The `app` argument and the permission-grant key are platform identifiers, so the hard-coded `com.apple.finder` has to change per backend:

| Backend | What `app` matches | Grant key | Example |
|---|---|---|---|
| macOS | bundle id or localized app name | bundle id | `com.apple.finder` |
| Windows | substring of a top-level window's title, class name, or process image name (`a11y_computer_use/drivers/_uia.py:156-174`) | process image name, for example `notepad.exe` | `notepad` |
| Linux | substring of an application's accessible name (`a11y_computer_use/drivers/_atspi.py:433-439`) | `/proc/<pid>/comm` name, for example `gedit` | `gedit` |
| Browser | a CDP page target id (one tab) | that target id | the `id` field from `http://127.0.0.1:9222/json` |

### Browser (Chromium over CDP)

```bash
pip install -e ".[browser]"      # adds websocket-client; install from the clone, the package is not on PyPI
google-chrome --headless=new --remote-debugging-port=9222 about:blank &
export A11Y_COMPUTER_USE_DRIVER=browser
export A11Y_COMPUTER_USE_CDP_ENDPOINT=http://127.0.0.1:9222   # this is also the default
curl -s http://127.0.0.1:9222/json                      # copy a page target's "id"
python examples/inprocess_python.py                     # with APP set to that id
A11Y_COMPUTER_USE_DRIVER=browser a11y-computer-use mcp              # the server the Node example spawns
```

- No OS grant is involved: `ensure_trusted()` only connects to the endpoint (`a11y_computer_use/drivers/browser.py:133-135`). An unreachable endpoint returns `app_not_found` with the hint to start Chrome with `--remote-debugging-port=<port>` (`a11y_computer_use/drivers/_cdp.py:48-55`); a reachable browser with no open tab also returns `app_not_found`, but its hint is to open a tab or check the port (`a11y_computer_use/drivers/browser.py:101-106`).
- If the id you pass does not match an open tab, the driver binds the first tab instead (`a11y_computer_use/drivers/browser.py:107-110`) while the permission check still uses the string you passed. Pass a real id.
- Chrome needs no `--remote-allow-origins`; the WebSocket handshake sends no Origin header (`a11y_computer_use/drivers/_cdp.py:75`).
- In a container, add the flags CI uses: `--no-sandbox --disable-gpu --disable-dev-shm-usage --no-first-run --no-default-browser-check` (the "launch headless Chrome" step in `.github/workflows/ci.yml`).
- The tool surface is 18 here: `console` and `network` are registered only on this backend. `clipboard` read returns an empty string and write raises `unsupported`; `app launch` takes a URL and navigates the bound tab (`a11y_computer_use/drivers/browser.py:500-512`, `547-554`).
- For the Node example, both variables must reach the child process. The script passes only `command` and `args` (the `StdioClientTransport` call, `mcp_subprocess.mjs:20-23`); how the MCP TypeScript SDK forwards environment variables to the child was not verified here. The Python equivalent with an explicit `env` dict is in `tests/e2e/test_mcp_stdio.py:53-57`.

### Windows

```powershell
pip install -e ".[windows]"           # uiautomation
$env:A11Y_COMPUTER_USE_DRIVER = "windows"   # optional; windows is the default on Windows
python examples\inprocess_python.py   # with APP set to "notepad" (open Notepad first)
```

- There is no OS grant to request: `ensure_trusted()` returns `None` (`a11y_computer_use/drivers/windows.py:47-52`). Elevated (UIPI-protected) windows are not detected; nothing in that method checks integrity levels.
- Implemented in the driver: `snapshot` (UI Automation through the shared pruning engine), `press_element`, `scroll_into_view`, `set_value`, `type_text` (SendInput), `key_chord` (SendInput). Live in CI on `windows-latest` against Notepad: snapshot, press plus type, the `ctrl+a` chord, and `desktop_snapshot` plus `type` through the gated `Runtime` (`tests/test_windows_live.py`, run by the "windows backend (live UIA)" step in `.github/workflows/ci.yml`).
- Not implemented yet, raising `NotImplementedError` from the driver (`a11y_computer_use/drivers/windows.py:73-74`, `141-153`, `175-209`): `resolve_ref`, so every ref-based `Runtime` call fails (`click(ref=...)`, `scroll(ref=..., into_view=True)`, `set_value`, `wait_for`, `act` steps with refs); coordinate `click`, `drag`, `scroll`; `screenshot` and `zoom`; the `app`, `window`, and `clipboard` tools. Of the commented act calls in `inprocess_python.py`, only `runtime.type_text(...)` runs on Windows today.

### Linux

```bash
sudo apt install at-spi2-core gir1.2-atspi-2.0   # accessibility bus + Atspi typelib
pip install -e ".[linux]"                         # PyGObject + python-xlib
export A11Y_COMPUTER_USE_DRIVER=linux                   # optional; linux is the default on Linux
python examples/inprocess_python.py               # with APP set to e.g. "gedit"
```

CI takes the distro route instead of building PyGObject: `apt-get install at-spi2-core gir1.2-atspi-2.0 gir1.2-gtk-3.0 python3-gi xvfb dbus dbus-x11 openbox xdotool x11-utils xclip`, then a venv created with `--system-site-packages` and `pip install -e ".[dev,browser]" python-xlib` (the "install AT-SPI2 / GTK / X11 system deps" and "venv with system gi" steps in `.github/workflows/ci.yml`).

- No per-app grant. The requirement is a reachable AT-SPI2 bus: `ensure_trusted()` flips `org.a11y.Status` on the session bus and probes the desktop; missing bindings or an unreachable bus return `permission_denied_accessibility` with the apt and `gsettings` hints (`a11y_computer_use/drivers/linux.py:88-115`).
- Clipboard needs one of `xclip`, `xsel`, or `wl-clipboard`. Headless hosts run under `xvfb-run` plus `dbus-run-session`, the way the "linux backend (live AT-SPI2)" step in `.github/workflows/ci.yml` does.
- Wayland (`WAYLAND_DISPLAY` set, no `DISPLAY`): snapshot, `find`, ref press, `set_value`, and typing into a field focused through the driver work over D-Bus; coordinate `click`, `drag`, `scroll`, and `key_chord` return `unsupported` with a hint to use ref-based actions (`a11y_computer_use/drivers/linux.py:48-63`). Screenshots there go through `grim`. The Wayland matrix in `docs/linux-port.md` has no test in this repo behind it.
- Live in CI under Xvfb with the openbox window manager against a GTK3 window: snapshot, an accessibility press with an observable effect, and accessibility typing (`tests/test_linux_live.py`), plus coordinate clicks, wheel scrolls, drags, key chords, XTEST typing including off-keymap Unicode, and the app, window, and clipboard tools (`tests/test_linux_desktop_live.py`). The same suites ran on a real Ubuntu desktop (`docs/box-testbed.md`). Screenshots are implemented and were checked by hand there, not asserted by a test.

## Your integration checklist (macOS)

1. Sign your app with your own Developer ID (Authenticode on Windows). We ship no cert and need none.
2. Request the OS permissions your app uses: Accessibility always; Screen Recording only if you use the `screenshot`/`zoom` vision fallback. `a11y-computer-use doctor` probes both grants from its own process (`AXIsProcessTrusted`, `CGPreflightScreenCaptureAccess`) and names the `.app` in its parent chain that TCC attributes them to, so run it as a child of your app to see your app's grants; if you embed Python in-process, call `a11y_computer_use.doctor.run_doctor()` from inside your app instead.
3. Hardened runtime (needed for notarization): embed in-process, sign the bundled components with your Team ID, or set `com.apple.security.cs.disable-library-validation`. This is standard for any app embedding Python.
4. Subprocess model only: make sure TCC's "responsible process" resolves to your signed app (embed in-process, or ship the helper signed with your Team ID at a stable path). `doctor` prints the responsible app so you can check.

Your users grant permissions to your trusted app once, and the grants survive your updates because the identity is yours and stable.

## Checklist notes for the other backends

- Windows: no OS grant exists to request; step 1 (Authenticode) is the whole identity story.
- Linux: ship or document the apt packages above. Observe returns `permission_denied_accessibility` until the accessibility bus is running in the user's session.
- Browser: your app starts, or attaches to, a Chromium launched with `--remote-debugging-port`; nothing is requested from the OS. Grants are keyed by CDP target id (`a11y_computer_use/drivers/browser.py:79-83`, `486-489`), so one grant covers one tab.
