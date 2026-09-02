# Provider executor adapters

`computeruse.adapters` lets an agent that already speaks a provider's native
computer-use tool run on computerUse without changing its prompt or loop. The
model keeps emitting Anthropic or OpenAI computer actions; the adapter executes
each one through the gated `Runtime` and returns the screenshot the provider
expects. Every action passes the same permission tiers, frontmost recheck,
secure-field refusal, confirmation gate, and audit log as the MCP tools.

The point of the layer is snap-to-ref. A coordinate click that lands inside an
interactive element of a fresh accessibility snapshot is executed as a ref
click: the Runtime re-resolves the element against the live tree and, for a
plain left click, activates it through the accessibility API without moving the
pointer. When nothing actionable is under the point, or the element is too large
to trust (a text area, a canvas, the window itself), the click falls back to the
raw coordinate exactly as the model asked.

Sources for the wire shapes below:

- Anthropic: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
- OpenAI: https://platform.openai.com/docs/guides/computer-use

## Usage

```python
from computeruse import server
from computeruse.adapters import AnthropicComputerAdapter, OpenAIComputerAdapter

runtime = server.Runtime()                       # OS driver, or COMPUTERUSE_DRIVER=browser
anthropic = AnthropicComputerAdapter(runtime, app="com.apple.TextEdit")
tools = [anthropic.tool_definition()]            # {"type": "computer_toolset_20260801"}
tool_result = anthropic.handle_tool_use(block)   # a tool_use block -> a tool_result dict

openai = OpenAIComputerAdapter(runtime)
output_item, results = openai.handle_call(computer_call)   # -> computer_call_output
```

Both adapters share the `ComputerAdapter` options:

| Option | Default | Meaning |
|---|---|---|
| `app` | None | App whose tree backs snap-to-ref and Set-of-Mark. None means the frontmost app (the bound tab on the browser backend). |
| `display_id` | None | Display to capture and act on; None is the main display. The id the Runtime reports for the first screenshot is remembered. |
| `max_long_edge` | 1280 | Long-edge budget for screenshots. |
| `marks` | False | Draw Set-of-Mark ref labels on every screenshot. |
| `snap_to_refs` | True | Execute coordinate clicks as ref clicks when possible. |
| `max_snap_fraction` | 0.25 | Elements covering more of the display than this never snap. |
| `confirm` | None | Human-confirmation callback for plausibly irreversible clicks. None blocks them with `confirmation_declined`. |
| `max_wait_s` | 30 | Ceiling for provider `wait` actions. |

Runnable loops live in `examples/anthropic_computer_use.py` and
`examples/openai_computer_use.py`. Without an API key they replay a scripted
action sequence through the adapter so the wiring is visible.

## Results

Every action returns a `Result` and never raises to the host:

| Field | Content |
|---|---|
| `action` | The provider action name. |
| `text` | The Runtime's outcome line plus the adapter's mapping note, for example `clicked e3 (AXButton 'Save') [snapped from image point (160, 104) to e3 AXButton 'Save']`. Empty for a bare screenshot. |
| `png` | PNG bytes for `screenshot` and `zoom`. |
| `error` | None, a `schema.ErrorCode` value (`secure_field`, `stale_ref`, `confirmation_declined`, `unsupported`, ...), `refused` for a permission-tier decision, or `invalid` for bad input. |
| `snapped_ref` | The ref a coordinate click was snapped to, if any. |

`Result.to_anthropic_tool_result(tool_use_id, toolset_name=...)` builds the
`tool_result` block (errors set `is_error: true` with the text as content).
`Result.image_data_url()` gives the `data:image/png;base64,...` form OpenAI takes.

## Coordinates and scaling

The model sees the screenshots the adapter returns and speaks in that image's
pixel space. The adapter keeps the `capture.ScaledImage` of the last screenshot
and maps model coordinates back to display-qualified physical pixels with the
same `to_source` mapping the Runtime's `screenshot` tool documents; the reverse
mapping (`from_physical`) is what the tests use to aim at elements. A pointer
action with no prior screenshot takes one first, so there is always a reference
image. Coordinates outside the image clamp to its edge.

On the legacy Anthropic tool the declared `display_width_px` and
`display_height_px` are read back from the first screenshot, so they always equal
the image Claude receives. Passing them sets the long-edge budget; the exact
values come from the real display's aspect ratio (a 1600x1000 display declared
as 1024x768 becomes 1024x640).

`zoom` crops the region at native resolution and scales the crop back to fit
within the screenshot dimensions with its aspect ratio preserved, as the toolset
docs describe. Coordinates stay in full-screenshot space afterwards.

## Snap-to-ref

For a click at image point (x, y):

1. The point maps to physical pixels.
2. Only a plain left single-click is a snap candidate. Right, middle, double,
   and triple clicks and clicks with modifiers carry pointer semantics (a
   context menu, word/paragraph selection) and always keep the model's
   coordinate.
3. A fresh snapshot of `app` is taken through the gate. A missing READ grant, an
   app without a tree, or any other failed refresh disables snapping for that
   click: an older snapshot (possibly of a different app or layout) is never
   used to redirect a click.
4. The smallest actionable element (enabled, clickable or editable, on the same
   display) whose bounds contain the point is chosen. Elements larger than
   `max_snap_fraction` of the display are skipped, so a click inside a text
   area, a canvas, or the window keeps its exact coordinate. So does a click
   inside an editable element that already has content (the point is a caret
   position) or on a position-sensitive control (slider, scrollbar, stepper,
   colour well); empty fields still snap, keeping the coordinate-free
   focus-then-type path.
5. The Runtime clicks the ref: the click becomes an accessibility press when the
   driver supports it (`press_element`), and the pointer never moves. A
   `stale_ref` during re-resolution falls back to the coordinate click.

Snapping is a hybrid, not a guarantee: the model's own coordinate is still the
input, and the audit log records the ref action that actually ran. Set
`snap_to_refs=False` for a pure pixel executor.

## Anthropic mapping

Both the `computer_toolset_20260801` member shape (`tool_use.name` is the member,
`toolset_name` is `computer`) and the legacy `computer_20251124` /
`computer_20250124` shape (`name` is `computer`, the member sits in
`input.action`) are accepted. `tool_definition(version)` returns the matching
`tools` entry and `beta_header(version)` the `anthropic-beta` value the legacy
versions need (`computer-use-2025-11-24`, `computer-use-2025-01-24`); the toolset
needs none.

| Member | Parameters | Runs as |
|---|---|---|
| `screenshot` | | `Runtime.screenshot`, downscaled to `max_long_edge`, marks optional |
| `zoom` | `region: [x0, y0, x1, y1]` | `Runtime.zoom` on the physical rect, then fit to screenshot size |
| `left_click`, `right_click`, `middle_click` | `coordinate?`, `text?` (modifiers) | `Runtime.click` with snap-to-ref; no coordinate means the last pointer position |
| `double_click`, `triple_click` | `coordinate?`, `text?` | `Runtime.click` with `count` 2 or 3 |
| `left_click_drag` | `start_coordinate`, `coordinate` | `Runtime.drag` |
| `left_mouse_down`, `left_mouse_up` | `coordinate?` | Remembered, then executed on `left_mouse_up` as one drag (or a click when the pointer did not move) |
| `mouse_move` | `coordinate` | Records the pointer position only (see approximations) |
| `cursor_position` | | `X=..., Y=...` in image space from the recorded position |
| `scroll` | `scroll_direction`, `scroll_amount`, `coordinate?`, `text?` | `Runtime.scroll` in lines; `down` is a positive `dy`, `right` a positive `dx` |
| `type` | `text` | `Runtime.type_text` |
| `key` | `text` (xdotool syntax), `repeat?` | `Runtime.key` with the converted chord, repeated |
| `hold_key` | `text`, `duration` | One press of the chord (see approximations) |
| `wait` | `duration` | `time.sleep`, capped at `max_wait_s` |

Modifier spellings `shift`, `ctrl`/`control`, `alt`/`option`, `super`/`meta`/
`cmd`/`win` (and the `_L`/`_R` keysym forms) map to the chord modifiers
`shift`, `ctrl`, `alt`, `cmd`. Key names follow xdotool: `Return`, `KP_Enter`,
`BackSpace`, `Delete`, `Escape`, `Tab`, `space`, arrows, `Home`, `End`,
`Page_Up`/`Prior`, `Page_Down`/`Next`, `F1` to `F12`, letters, digits, and
punctuation by keysym name or character (`minus` or `-`). `Delete` is the
forward-delete key: `forward_delete` on macOS, `delete` on the other drivers,
where `delete` already means that key. Unknown names return an `invalid` result.

## OpenAI mapping

`tool_definition()` returns the GA entry `{"type": "computer"}`;
`tool_definition(preview=True)` returns the deprecated `computer_use_preview`
entry with `display_width`, `display_height`, and `environment` (`mac`,
`windows`, `ubuntu`, or `browser`, derived from the driver name unless
overridden). `handle_call(call)` runs a `computer_call` item's `actions` array
(or the preview's single `action`) in order, stops at the first failure, and
returns the `computer_call_output` item plus every per-action `Result`. The
output carries a screenshot (`computer_screenshot`, or `input_image` for the
preview shape) whenever one can be captured, and `current_url` on the browser
backend. If the capture itself fails (no endpoint, no Screen Recording grant)
the item has no `image_url` and the last `Result` says why; the host decides
whether to send it or stop.

| Action | Parameters | Runs as |
|---|---|---|
| `click` | `x`, `y`, `button` (`left`, `right`, `wheel`), `keys?` | `Runtime.click` with snap-to-ref; `wheel` is the middle button; `keys` are held modifiers |
| `click` with `button` `back` or `forward` | | The platform history shortcut (`cmd+[` / `cmd+]` on macOS, `alt+left` / `alt+right` elsewhere) |
| `double_click` | `x`, `y`, `keys?` | `Runtime.click` with `count` 2 |
| `drag` | `path: [{x, y}, ...]` | `Runtime.drag` from the first to the last point |
| `keypress` | `keys: [...]` | One chord per non-modifier key, each holding the listed modifiers (`["CTRL", "A"]` is `ctrl+a`) |
| `move` | `x`, `y` | Records the pointer position only |
| `screenshot` | | `Runtime.screenshot` |
| `scroll` | `x`, `y`, `scroll_x`, `scroll_y` | `Runtime.scroll` in pixels; positive `scroll_y` scrolls down, positive `scroll_x` scrolls right, matching the Runtime's sign convention |
| `type` | `text` | `Runtime.type_text` |
| `wait` | | `time.sleep(default_wait_s)` (1 second) |

OpenAI's uppercase key names (`ENTER`, `ESCAPE`, `ARROWUP`, `PAGEDOWN`, `CTRL`,
`META`, ...) go through the same alias table as the xdotool names.

A call carrying `pending_safety_checks` (`malicious_instructions`,
`irrelevant_domain`, `sensitive_domain`) runs only when
`handle_call(..., acknowledge_safety_checks=True)`; otherwise every action is
returned as a `refused` Result with a fresh screenshot and nothing executes.
Acknowledging authorises execution (the host surfaces the check to a human
first) and is echoed back as `acknowledged_safety_checks`.

## Approximations and limits

- `mouse_move` and `move` record the pointer position and send no event: no
  driver exposes a hover primitive. A following click or mouse-up uses the
  recorded position, so `left_mouse_down` / `mouse_move` / `left_mouse_up`
  sequences still become one drag.
- `hold_key` presses the chord once. The drivers expose no key-hold, and the
  result text says so.
- `drag` executes start to end; intermediate `path` points are dropped and
  counted in the result text.
- Modifiers on `scroll` are not applied (wheel scrolls carry no modifiers in
  the Runtime); the result text notes it.
- `left_click_drag` and drags in general, wheel scrolls, and raw coordinate
  clicks that did not snap are synthetic pointer events and move the physical
  pointer, exactly as the MCP tools do. Snapped left clicks do not.
- Actions that the backend cannot perform (coordinate input on native Wayland,
  the Windows driver's unimplemented primitives, clipboard on the browser)
  return the Runtime's `unsupported` or `NotImplementedError` text as an error
  result rather than raising.
- Safety refusals arrive as text: `needs_permission: ...` for a missing tier,
  `secure_field: ...` for a password field, `confirmation_declined: ...` when a
  plausibly irreversible click has no confirmer.

## What is verified

- Unit tests (`tests/test_adapters.py`, any OS, no permissions): every action of
  both providers against a recording fake driver, coordinate scaling, the
  snap-to-ref selection rules, the key tables, the tool-definition shapes, the
  `tool_result` and `computer_call_output` shapes, refusals and structured
  errors as text, audit entries, and a hermetic browser run over the scripted
  CDP transport (a pixel click on the fixture's Save button becomes a DOM
  `this.click()` with no `Input.dispatchMouseEvent`).
- Live (macOS, headless Chrome 9444, `pytest tests/test_adapters.py -k live`):
  the Anthropic adapter screenshot a `data:` page, a pixel click at the text
  field's center snapped to the field's ref, `type` entered text, a pixel click
  at the button's center snapped to the button's ref, and the next snapshot
  showed the page's `clicked world` heading. The live test self-skips without a
  reachable CDP endpoint.
- Not live-verified: the adapters on the macOS, Windows, and Linux OS drivers
  (unit-tested through the fake driver only), and the provider loops in
  `examples/` against real Anthropic or OpenAI endpoints (they were exercised
  in their scripted mode only).
