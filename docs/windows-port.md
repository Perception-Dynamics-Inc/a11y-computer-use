# Porting a11y-computer-use to Windows

**Goal:** one reusable a11y-computer-use across macOS, Windows, Linux, and the browser (CDP). The architecture is built for it: everything OS-specific lives behind the `Driver` protocol (`a11y_computer_use/drivers/base.py`), and the schema, tree-pruning engine, safety layer, and MCP server are **platform-free and shared**. Porting is "implement `drivers/windows.py`", never "touch the core."

**Status:** the Windows backend (`drivers/windows.py`) is **partially implemented and CI-verified live** on `windows-latest` (`tests/test_windows_live.py`, five tests against Notepad; `5 passed` on run 33436980587). Implemented: `snapshot` (UIA via `_uia.find_window` + `UIAAccessor` through the shared pruning engine), `press_element` (`Invoke` / `Toggle` / `SelectionItem.Select` / `ExpandCollapse.Expand`, `SetFocus` for editables; only the `SetFocus` path is live-asserted), `scroll_into_view` (`ScrollItemPattern.ScrollIntoView`), `set_value` (`ValuePattern.SetValue`), `type_text` (`SendInput` with `KEYEVENTF_UNICODE`), `key_chord` (VK codes via `SendInput`), and the gated Runtime end to end (`desktop_snapshot` + `type`, with app identity from `_win_system.frontmost_app_id`, e.g. `notepad.exe`). Still `NotImplementedError`, each naming its native API (15 methods): `resolve_ref`, coordinate `click` / `drag` / `scroll`, `wait_for`, `screenshot` / `zoom_region`, and the driver's `frontmost_app` / `app_at_point` / `running_apps` / `launch_app` / `activate_app` / `windows` / `read_clipboard` / `write_clipboard`. The Runtime re-resolves refs through `driver.resolve_ref`, so ref-based Runtime actions (`click(ref)`, `set_value`, `wait_for`, `act` steps), the vision path, and the `app` / `window` / `clipboard` tools are not yet available on Windows. Prior art: [Terminator](https://github.com/mediar-ai/terminator) already validated Rust+UIA for exactly this shape.

## What's already shared (no per-OS work)

- `schema.py`: the canonical Element/Snapshot/Action contract. Identical on every OS.
- `observe.build_snapshot` and the pruning engine: walks any `TreeAccessor` and prunes/indexes it. A Windows `UIA TreeAccessor` feeds the *same* engine; `_match_anchor` (ref re-resolution) is reused verbatim.
- `safety.py` (tiers, gates, audit log, confirmation classifier) and the MCP server with its full 16-tool surface (`console` and `network` are added only on the browser backend). Platform-free.

## Primitive mapping (macOS → Windows)

| Capability | macOS (implemented) | Windows | Windows status |
|---|---|---|---|
| Read UI tree | `AXUIElement` + `AXUIElementSetMessagingTimeout` batching | **UI Automation** via `uiautomation`: `_uia.find_window` + a recursive `GetChildren` walk into the shared `build_snapshot`; a **`CacheRequest`** batch (role/name/bounds/patterns in one cross-process call) is the perf follow-up | ✅ |
| Ref re-resolution | anchored re-walk → `observe._match_anchor` | same `_match_anchor`, over a fresh UIA walk | ❌ `resolve_ref` raises |
| Activate a ref (no cursor) | `AXUIElementPerformAction(AXPress/…)` | `InvokePattern.Invoke` / `TogglePattern.Toggle` / `SelectionItemPattern.Select` / `ExpandCollapsePattern.Expand`; `SetFocus` for editables | ✅ (CI-live on Notepad's edit, the `SetFocus`-for-editables path per `tests/test_windows_live.py`; `Invoke` / `Toggle` / `Select` / `Expand` have no live assertion yet) |
| Scroll a ref into view | `AXScrollToVisible` | `ScrollItemPattern.ScrollIntoView` | ✅ (no live assertion yet) |
| Set a field value | `AXUIElementSetAttributeValue(AXValue)` | `ValuePattern.SetValue` | ✅ (no live assertion yet) |
| Click / drag (coords) | `CGEvent` mouse | `SendInput(MOUSEINPUT)` at physical px | ❌ |
| Scroll (delta) | `CGEventScrollWheel` | `SendInput(MOUSEEVENTF_WHEEL/HWHEEL)` | ❌ |
| Type text | `CGEventKeyboardSetUnicodeString` | `SendInput(KEYBDINPUT, KEYEVENTF_UNICODE)`, layout-free Unicode path | ✅ |
| Key chords | `_US_KEYCODES` / (UCKeyTranslate TODO) | VK codes via `SendInput` (`_win_input.press_chord`: fixed VK table for letters, digits, F1-F12 and named keys; `VkKeyScanEx` layout awareness is a follow-up) | ✅ |
| Capture | `CGWindowListCreateImage` (+ `screencapture`) | **DXGI Desktop Duplication** (BitBlt / `PrintWindow` fallback) | ❌ |
| Foreground app | `NSWorkspace` frontmost | `GetForegroundWindow` + `GetWindowThreadProcessId` | ◐ `_win_system.frontmost_app_id` serves the Runtime's gate; the driver method raises |
| Hit-test (act recheck) | `CGWindowList` / point → app | `WindowFromPoint` + process image name | ◐ `_win_system.app_at_point_id` serves the Runtime's recheck; the driver method raises |
| Enumerate apps/windows | `NSWorkspace` / `CGWindowList` | `EnumWindows` / Toolhelp32 | ❌ |
| Launch / focus | `open` / `activateWithOptions` | `ShellExecute`/`CreateProcess` / `SetForegroundWindow` | ❌ |
| Clipboard | `NSPasteboard` | `OpenClipboard`/`GetClipboardData(CF_UNICODETEXT)` | ❌ |

## Windows-specific concerns (no macOS analog)

- Integrity / UIPI, not TCC. There is no Accessibility grant to request. A medium-integrity process can read UIA and `SendInput` to same/lower-integrity windows; targeting an **elevated** window silently no-ops. Detect the target window's integrity level and return a structured `elevation_blocked` error instead of a silent failure (add `ErrorCode.ELEVATION_BLOCKED`). Not started: `ensure_trusted` returns `None` unconditionally and `ErrorCode.ELEVATION_BLOCKED` does not exist in `schema.py` yet. Detect locked workstation / secure desktop (UAC prompts live on the uncapturable secure desktop) in `snapshot`.
- COM threading. UIA is COM; use an MTA client and `CacheRequest` batching; event callbacks are deadlock-prone, so keep the driver on timeout-killable supervised workers (the same isolation the plan specifies for hung macOS AX servers).
- Signing. Same "host owns the identity" model as macOS: the integrator signs *their* app with Authenticode (EV for SmartScreen reputation); an unsigned input-injecting/screen-reading binary is a textbook malware flag. a11y-computer-use ships no certificate.

## Suggested order

1. `snapshot` (UIA `GetChildren` walk into `build_snapshot`): done, CI-verified on Notepad. `CacheRequest` batching is still the perf follow-up.
2. `press_element` / `scroll_into_view` / `set_value` (patterns): done. `frontmost_app` / `app_at_point` (gating): implemented in `_win_system.py` for the Runtime's recheck; the driver methods still raise.
3. `type_text` / `key_chord` (`SendInput`): done. `click` / `scroll` / `drag` (`SendInput` mouse): remaining, together with `resolve_ref` and `wait_for`, which block ref-based Runtime actions.
4. `screenshot` / `zoom_region` (DXGI): remaining.
5. system/windowing + clipboard on the driver: remaining.
6. Integrity/UIPI semantics + `elevation_blocked`: remaining.

In CI the shared core tests (`tests/test_drivers.py`, `tests/test_observe.py`) and the MCP `build_server()` smoke run on `windows-latest`, and `tests/test_windows_live.py` exercises the implemented primitives live (UIA snapshot of Notepad, accessibility press + typing, a `ctrl+a` chord, and the gated Runtime end to end). The Driver contract is shared with macOS, and the same platform-free core tests pass on `windows-latest`, but the Windows job does not compare behavior with macOS: it asserts only that the shared engine passes there and that the live-implemented primitives (UIA snapshot through the shared pruning engine, accessibility press via UIA patterns / `SetFocus`, SendInput typing, a `ctrl+a` chord, and gated Runtime snapshot + type against Notepad) work. Methods still marked `NotImplementedError` in `a11y_computer_use/drivers/windows.py` (including `resolve_ref`, `click`, `screenshot`, and the app/window/clipboard ops) are not exercised.
