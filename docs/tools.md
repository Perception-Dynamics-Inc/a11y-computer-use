# What the model sees, and the tools

> Moved here from the top-level README on 2026-10-17. The README is now a short landing page.

## What the model sees

```text
[snap-7] com.apple.TextEdit (window)
  e1 window "Untitled"
    e2 textarea ="Hello" (edit,focus)
    e3 button "Save" (click)
```

Then it acts: `click(ref="e3")`, `type("hello")`, `set_value(ref="e2", value="...")`. Ref clicks go through the accessibility API and leave your pointer alone; typing addressed to an app (`type(text, app=...)`) leaves your screen alone too. A stale ref comes back as `stale_ref` with the nearest live candidates. Re-observing as a diff costs about 10 tokens. Apps with no accessibility tree (Telegram, canvases) get OCR refs `o1..oN` from on-device text recognition, and `click(ref="o7")` works the same way ([docs/ocr-refs.md](./ocr-refs.md)).

26 tools: `desktop_snapshot`, `find`, `screen_text`, `screenshot`, `zoom`, `crop` (PNG of one ref's on-screen bounds; no OCR), `click`, `hover` (Linux: move the pointer and deliver a hover with no click; elsewhere `unsupported`), `type`, `key`, `scroll`, `drag` (with waypoint paths for strokes), `wait_for`, `wait_until`, `act`, `set_value`, `scroll_to_find`, `notes`, `menu`, `file_dialog`, `app`, `window`, `clipboard`, `request_permission`, `grant_app`, `report_issue` (agents file the tool's own defects and slow calls as issues here, [docs/reporting.md](./reporting.md)), plus `console`, `network`, and `webmcp` (tools a page registers through `navigator.modelContext`, listed as `w1..wN`, [docs/webmcp.md](./webmcp.md)) on the browser. Menus and file dialogs stay reachable even in custom-drawn apps ([docs/macos-primitives.md](./macos-primitives.md)). Details: [docs/agent-loop.md](./agent-loop.md) and the tool docstrings.
