# Voice: the reflex layer

`a11y-computer-use voice` is the other end of the scale from the agent loop.
The agent observes, plans with a model, acts and verifies; that is right for
"file my expenses" and wrong for "open Notes" said out loud, where the whole
budget is a few hundred milliseconds. In the reflex layer the model never
plans. It only picks: one skill per spoken command, with the slots (app name,
URL, query, text) extracted deterministically from the transcript, executed
through fixed fast paths, and checked for at most 300 ms.

```
a11y-computer-use voice --grant full --apps "Notes,Google Chrome,Photo Booth"
a11y-computer-use voice --text "open Notes and create a new note"
a11y-computer-use voice --text examples/voice/demo-transcript.txt --router jev --json
```

Each command prints one line, every stage as measured:

```
[stt 412 ms] [route 0 ms local] [act 62 ms] open_app app='Notes' -> ok: focused com.apple.Notes (Notes frontmost)
```

`stt` is the speech endpointing wait (absent with `--text`), `route` the
router's latency and backend, `act` the fast path, and the parenthesis the
bounded post-check.

## Pipeline

1. **Listen.** `voice.SpeechListener` streams Apple's on-device recogniser
   (`SFSpeechRecognizer` with `requiresOnDeviceRecognition`, fed from an
   `AVAudioEngine` microphone tap, partial results on). An utterance is
   committed after 500 ms without a change in the partial transcript
   (`--silence`), or on a final result. `--push-to-talk` replaces the silence
   endpoint with Return to start and Return to commit. `--text` takes a
   transcript, or a file with one utterance per line, and needs no microphone.
2. **Split.** Fillers go first ("um", "okay", "can you", "once you're there",
   "for me", ...), then the utterance splits into clauses on sentence
   punctuation, "then", and "and" before a verb. Quoted phrases and domains
   stay whole ("x.com", "x dot com").
3. **Route.** One skill per clause. `--router local` (default) is a regex
   router: no network, well under a millisecond. `--router jev` asks
   TypeSafe's System One for one Choice over the skill list plus a
   `needs_clarification` score; the slots are still extracted locally, and
   any transport failure (timeout, HTTP error, no key) falls back to the local
   router with the backend reported as `local-fallback`, so a timeline never
   hides it. The key comes from `TYPESAFE_API_KEY` in the environment or a
   `.env` in the working directory.
4. **Act.** Skills use only fast primitives of the gated `Runtime`: app
   focus or launch, key chords, typing, menu presses, screenshots. No
   observation, no coordinates, no model-generated text.
5. **Check.** A skill may declare a post-check (is the app frontmost?). It is
   reported, never blocks the next command, and is capped at 300 ms.

## Skills

| Skill | Says | Does | Tier |
| --- | --- | --- | --- |
| `open_app` | "open up the Notes app", "switch to Chrome" | focus if running, else launch and wait for the first window | click |
| `new_document` | "create a new note", "open a new tab" | cmd+n in the frontmost app | full |
| `set_title` | "make the title say 'Hello'" | types the phrase (Notes makes the first line the title) | full |
| `type_text` | "type hello there" | types the phrase into the focused field | full |
| `web_search` | "Google search Norbert Wiener" | new tab in the browser, Google results URL, Return | full |
| `open_url` | "open up x.com", "go to news dot ycombinator dot com" | new tab in the browser, URL, Return | full |
| `take_photo` | "take a picture of me" | Photo Booth to the front, `File > Take Photo` (cmd+return if the menu is unavailable) | click |
| `screenshot` | "take a screenshot" | captures the screen | read |
| `menu_item` | "in Photo Booth press File > Take Photo" | presses a menu item by path | click |

The browser is the one named in the command, else the frontmost browser, else
the first installed of Arc, Google Chrome, Safari, Firefox, Brave. A browser
that is not installed maps to that choice too, so "open the Arc browser" on a
Mac without Arc opens Chrome.

Every skill goes through the same safety gate as every other tool. An app
without a grant refuses before any input, and the refusal is the command's
outcome line (`-> refused/failed: ActionRefused: ...`), not an exception.
`--grant TIER --apps A,B` sets the grants up front, the same way `agent
--grant` does.

## Permissions

Speech Recognition and Microphone are TCC grants attached to the responsible
app (your terminal), like Accessibility. `a11y-computer-use doctor` reports
both under `speech_recognition_grant`; the first `voice` run without `--text`
shows the system prompts. `voice --text` needs neither. The wheels
`pyobjc-framework-Speech` and `pyobjc-framework-AVFoundation` are installed
with the package on macOS.

## Measured

Live on a MacBook (macOS 26, Apple silicon), the six demo commands in
`examples/voice/demo-transcript.txt` run through `--text` (no speech stage):

| Command | local: route | local: act | jev: route | jev: act |
| --- | ---: | ---: | ---: | ---: |
| open the Notes app | 0 ms | 62 ms | 1017 ms | 204 ms |
| create a new note | 0 ms | 94 ms | 953 ms | 124 ms |
| title says "Hello" | 0 ms | 153 ms | 815 ms | 43 ms |
| open the Arc browser (Chrome) | 0 ms | 150 ms | 1699 ms | 163 ms |
| Google search Norbert Wiener | 2 ms | 350 ms | 739 ms | 240 ms |
| open x.com | 0 ms | 141 ms | 1963 ms | 195 ms |
| open Photo Booth | 0 ms | 42 ms | 3018 ms (fallback) | 808 ms (launch) |
| take a picture | 0 ms | 238 ms | 2256 ms | 205 ms |

All 8 commands succeeded on both routers. Jev's latency from this network was
0.7 to 2.4 s per call, one call timed out and fell back locally; that is the
network and the model, not the client, and it is why `local` is the default.
The action column is the wall time of the fast path itself: focusing a running
app lands in 40 to 150 ms, a key chord in under 100 ms, a new tab plus URL in
140 to 350 ms.

Speech endpointing was not measured live: the Speech Recognition and
Microphone grants are not determined for this terminal, and granting them
is the owner's decision. The listener is built and imports cleanly; the
`--text` path exercises everything after the endpoint.

## What changed on the hot path

Profiling the fast primitives found three fixed costs that had nothing to do
with the action itself, all in `server.py`, `observe.py` and `menus.py`:

| Path | Before | After |
| --- | ---: | ---: |
| `app focus` of a running app without a window in front | 5077 ms (the whole `APP_FOCUS_WAIT_S`) | 8 to 11 ms steady state |
| resolving an app by name right after `app launch` | never within the process (stale NSWorkspace list, then the timeout) | under 1 ms |
| `menu_state` per call | AX element created and probed on every call | 0.9 ms with the element cached per pid |

* `_wait_frontmost` used to accept only NSWorkspace's frontmost app while
  `_activate` also accepted the WindowServer's stacking order, so a focus that
  `_activate` verified could still burn the full 5 s wait. Both now use the
  same predicate, polled every 20 ms and debounced over two polls.
* `NSWorkspace.runningApplications` and `frontmostApplication` only update
  when the main thread's run loop turns, which a CLI or an asyncio MCP server
  never does. `safety.refresh_workspace` runs one non-blocking pass of the
  loop (0.01 ms idle) before the frontmost read and after a failed name
  lookup, so a launched app resolves and a switch is seen at once.
* Notes runs a `com.apple.Notes.WidgetExtension` helper whose display name
  is also "Notes", so "Notes" resolved to it even when Notes was closed and
  its AX server never answered. Name lookups now skip faceless helpers
  (`_match_running_app`); a helper is still reachable by its bundle id.
* Menu queries cache the app's `AXUIElementRef` per pid, so the creation and
  responsiveness probe are paid once per app.

## Limits

* The local router knows the nine built-in skills and their phrasings; an
  utterance that matches none is reported as `unrouted` and nothing runs.
* Slots are deterministic and case-preserving, but they are regexes over a
  transcript: "make the title say Hello there" types "Hello there", and an
  app name the recogniser has never heard of is passed through as typed.
* The reflex layer never observes, so it cannot verify content, only focus.
  Anything that needs the tree (a specific button, a form) belongs to the
  agent loop.
