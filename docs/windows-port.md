# Porting computerUse to Windows

**Goal:** one reusable computerUse across macOS and Windows. The architecture is built for it — everything OS-specific lives behind the `Driver` protocol (`computeruse/drivers/base.py`), and the schema, tree-pruning engine, safety layer, and MCP server are **platform-free and shared**. Porting is "implement `drivers/windows.py`", never "touch the core."

**Status:** the Windows backend is a **mapped skeleton** (`drivers/windows.py`) — every method raises `NotImplementedError` naming the native API it will use. It is **unverified**; it must be implemented and validated on a real Windows box. Prior art: [Terminator](https://github.com/mediar-ai/terminator) already validated Rust+UIA for exactly this shape.

## What's already shared (no per-OS work)

- **`schema.py`** — the canonical Element/Snapshot/Action contract. Identical on every OS.
- **`observe.build_snapshot` + the pruning engine** — walks any `TreeAccessor` and prunes/indexes it. A Windows `UIA TreeAccessor` feeds the *same* engine; `_match_anchor` (ref re-resolution) is reused verbatim.
- **`safety.py`** (tiers, gates, audit log, confirmation classifier) and the **MCP server + 12-tool surface**. Platform-free.

## Primitive mapping (macOS → Windows)

| Capability | macOS (implemented) | Windows (to build) |
|---|---|---|
| Read UI tree | `AXUIElement` + `AXUIElementSetMessagingTimeout` batching | **UI Automation** (`IUIAutomation`) with a **`CacheRequest`** to batch role/name/bounds/patterns into one cross-process call (avoids per-property COM round-trips) |
| Ref re-resolution | anchored re-walk → `observe._match_anchor` | same `_match_anchor`, over a fresh UIA walk |
| Activate a ref (no cursor) | `AXUIElementPerformAction(AXPress/…)` | `InvokePattern.Invoke` / `TogglePattern.Toggle` / `SelectionItemPattern.Select` |
| Scroll a ref into view | `AXScrollToVisible` | `ScrollItemPattern.ScrollIntoView` |
| Click / drag (coords) | `CGEvent` mouse | `SendInput(MOUSEINPUT)` at physical px |
| Scroll (delta) | `CGEventScrollWheel` | `SendInput(MOUSEEVENTF_WHEEL/HWHEEL)` |
| Type text | `CGEventKeyboardSetUnicodeString` | `SendInput(KEYBDINPUT, KEYEVENTF_UNICODE)` — layout-free Unicode path |
| Key chords | `_US_KEYCODES` / (UCKeyTranslate TODO) | VK codes via `VkKeyScanEx` / `MapVirtualKeyEx` (layout-aware) |
| Capture | `CGWindowListCreateImage` (+ `screencapture`) | **DXGI Desktop Duplication** (BitBlt / `PrintWindow` fallback) |
| Foreground app | `NSWorkspace` frontmost | `GetForegroundWindow` + `GetWindowThreadProcessId` |
| Hit-test (act recheck) | `CGWindowList` / point → app | `WindowFromPoint` + process image name |
| Enumerate apps/windows | `NSWorkspace` / `CGWindowList` | `EnumWindows` / Toolhelp32 |
| Launch / focus | `open` / `activateWithOptions` | `ShellExecute`/`CreateProcess` / `SetForegroundWindow` |
| Clipboard | `NSPasteboard` | `OpenClipboard`/`GetClipboardData(CF_UNICODETEXT)` |

## Windows-specific concerns (no macOS analog)

- **Integrity / UIPI, not TCC.** There is no Accessibility grant to request. A medium-integrity process can read UIA and `SendInput` to same/lower-integrity windows; targeting an **elevated** window silently no-ops. Detect the target window's integrity level and return a structured `elevation_blocked` error instead of a silent failure (add `ErrorCode.ELEVATION_BLOCKED`). Detect locked workstation / secure desktop (UAC prompts live on the uncapturable secure desktop) in `snapshot`.
- **COM threading.** UIA is COM; use an MTA client and `CacheRequest` batching; event callbacks are deadlock-prone — keep the driver on timeout-killable supervised workers (the same isolation the plan specifies for hung macOS AX servers).
- **Signing.** Same "host owns the identity" model as macOS: the integrator signs *their* app with Authenticode (EV for SmartScreen reputation); an unsigned input-injecting/screen-reading binary is a textbook malware flag. computerUse ships no certificate.

## Suggested order

1. `snapshot` (UIA + CacheRequest → `build_snapshot`) — unlocks observe end-to-end, testable read-only.
2. `press_element` / `scroll_into_view` (patterns) + `frontmost_app` / `app_at_point` (gating).
3. `click` / `type_text` / `key_chord` / `scroll` / `drag` (`SendInput`).
4. `screenshot` / `zoom_region` (DXGI).
5. system/windowing + clipboard.
6. Integrity/UIPI semantics + `elevation_blocked`.

Each step is validated against the *same* MCP tool tests the macOS backend passes — the contract is shared, so "green on Windows" means the same behavior.
