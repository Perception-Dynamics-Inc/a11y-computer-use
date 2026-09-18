# OCR refs

Some apps expose no accessibility tree (Telegram on macOS), or a shell around a
custom-drawn canvas (After Effects panels, Krita, most games). Until now the
only path there was the pixel loop: screenshot, let the model guess an (x, y)
pair, click. OCR refs keep the model out of the coordinate business on that
path too. The screen is captured, on-device OCR reads every line of text with
its bounding box, and each line becomes a ref `o1..oN` with a rect in
display-qualified physical pixels, like the `e` refs of a snapshot.

```text
[ocr-3] display 1: 42 text line(s) via OCR; refs o1..oN target the text centre (click ref="o7")
  o1 "Telegram" [64x16 @1:50,50] (0.98)
  o2 "Saved Messages" [150x20 @1:200,300] (0.97)
  o3 "Write a message..." [300x20 @1:200,900] (0.95)
```

## What the agent gets

| Call | Effect |
|---|---|
| `screen_text(display_id, region, min_confidence)` | OCR the display (or a `{x, y, width, height}` region) and publish the lines as the current OCR epoch |
| `click(ref="o7")`, `scroll(ref=...)`, `drag(start_ref=..., end_ref=...)` | act at the centre of that text |
| `find(app, text=..., ocr=true)` | fresh OCR, only the lines containing the text |
| `wait_for("o7", "exists" or "gone")` | re-OCR until the text appears or leaves |
| `screenshot(marks=true)` | draws `o` refs in blue next to the red `e` refs |
| `desktop_snapshot` with no actionable element | appends the OCR lines automatically, so the planner has targets in the same reply (`A11Y_COMPUTER_USE_AUTO_OCR=0` turns this off) |

Every call goes through the same gate as a screenshot: tier `read` against
the frontmost app, an audit row, and the Screen Recording grant. An `o` ref
that cannot be found again returns `stale_ref` with up to three candidate
lines, audited like an `e` ref miss.

## How the mapping works

`ocr.py` has two layers. The engine (`VisionOcr` on macOS: Apple's Vision
framework through pyobjc, accurate level, language correction on) turns PNG
bytes into text boxes in image pixels. `build_screen_text` groups boxes into
reading-order lines, drops boxes under the confidence floor (0.5 by default),
and scales image pixels onto the display's physical pixel space, so a capture
at backing scale 2 (a 3200x2000 image of a 1600x1000 display) and a capture at
nominal resolution give the same rects. A region capture scales onto the
region's size and adds its offset.

Acting on an `o` ref re-reads the screen and re-finds the line by text near its
old centre (same text first, then a line containing it, within 400 px), the OCR
analogue of re-resolving an `e` ref against the live tree. Text that moved a
little is followed; text that left is reported. `A11Y_COMPUTER_USE_OCR_REMATCH=0`
trusts the stored box instead, saving one capture per action.

The `o` ref resolves to a `Point`, so the secure-field hit-test and the
same-window recheck apply exactly as they do to a raw coordinate click. On
macOS the click is a synthetic mouse event, not an accessibility press, so it
moves the pointer.

## Measured

Live on this Mac (Apple silicon, 3024x1964 Retina display, Screen Recording
granted, 2026-09-19), a full-display OCR through Vision:

| Step | Time |
|---|---|
| capture (`CGWindowListCreateImage`) | about 100 ms |
| `VisionOcr.recognize` on the 3024x1964 PNG, 80 lines | 465 ms median, 454 to 801 ms over 5 runs |

So a `screen_text` call costs about half a second, and an `o` ref action costs
one more capture plus recognition before the click when rematching is on.

## Limits

- OCR sees text, not controls. Icon-only buttons, sliders, and canvases with
  no labels have no `o` ref; use `screenshot` and coordinates there.
- Small or low-contrast text can be misread. The confidence per line is in the
  render; raise `min_confidence` to drop doubtful lines.
- Two identical labels near each other resolve to the nearest one to the old
  position, which is what you want when a list scrolls slightly and wrong when
  two identical buttons swap places.
- macOS only today. Off macOS `screen_text` returns `unsupported` and the
  automatic escalation stays silent. The `OcrEngine` protocol is two methods, so
  a Windows OCR or Tesseract engine can be plugged in through `Runtime(ocr_engine=...)`.
- Vision reads image bytes, so the engine works without Screen Recording; the
  capture that feeds it does not.

## Verified

Hermetic tests (`tests/test_ocr.py`, every OS, fake engine and fake driver):
line grouping, confidence filter, image to display scaling incl. backing scale
2 and region crops, ref assignment and render, `find(ocr=true)`, click and drag
and scroll on `o` refs through the gate, rematch after movement, `stale_ref`
with candidates and its audit row, secure-field refusal under an `o` ref, the
escalation and its opt-out, `wait_for` on `o` refs, blue marks on screenshots,
`unsupported` without an engine, the MCP and `call_tool` surface.

Live on macOS: the Vision engine reads a rendered PNG (no grant needed) and
the live screen (Screen Recording granted). Not yet verified live: driving a
real custom-drawn app end to end through `o` refs; that is the agency demo's job.

## Cropping to the app

The automatic escalation (an empty snapshot appending OCR lines) reads only the
target app's windows: the union of the window rects in the snapshot, or, when
the tree has none, of the driver's window list for that app. The menu bar and
other apps' pixels stay out of the refs. When no window rect is known at all,
the whole display is read and the reply says so. `screen_text(app="Telegram")`
does the same crop on request; `region` is still available for a hand-picked
rect, and the two cannot be combined. Found live: before this, an empty
TextEdit snapshot came back with Telegram's chat list as `o` refs.
