# Mission: the design agency reel

One inbound Telegram message, one delivered website, no human in between. This
is the hero demo for a11y-computer-use and the driver for the 0.2.0 features:
OCR refs for apps without an accessibility tree, a mission runner for long,
multi-app jobs, menu and file-dialog primitives, and text-based waits.

## The story

1. A client writes on Telegram: "I need a website for my supercar shop. Can you
   build the design, make a video about one of our cars, put it on the site, and
   send us the link?"
2. The agent reads the message (Telegram exposes no accessibility tree: OCR
   refs), designs the landing page in Figma (Electron chrome via accessibility,
   canvas via the Figma plugin API or OCR), generates the car video on
   Higgsfield in Chrome (accessibility refs over CDP or AX), edits it in After
   Effects from a prepared template (menus via the accessibility menu bar, panels
   via OCR refs, export through Media Encoder), builds and deploys the site
   (Higgsfield website builder in Chrome), and replies on Telegram with the site
   link and a Stripe **test-mode** Payment Link. Never a fabricated payment page.
3. Screen Studio records the whole run; the published reel is a 24x time-lapse
   with the real elapsed time on screen.

## Phases and checks

| Phase | Apps | Done when |
|---|---|---|
| Read brief | Telegram | brief text captured to notes |
| Design | Figma | file exists, frame named "Landing" present |
| Video | Chrome (Higgsfield) | mp4 in ~/Downloads, size > 1 MB |
| Edit | After Effects, Media Encoder | rendered mp4 exists, duration 15 to 30 s |
| Site | Chrome (Higgsfield website) | deployed URL returns 200, page contains the video |
| Reply | Telegram | sent message visible in the chat, contains the URL |

Each phase is a task for the agent loop with its own step budget, allowed apps,
and a state check the runner evaluates itself (file system, HTTP, snapshot or
OCR text). A failed check re-runs the phase with the failure in the notes.

## What 0.2.0 has to provide

- `screen_text` observation: on-device OCR (Vision framework on macOS) with
  bounding boxes, exposed as refs `o1..oN`; `click(ref="o7")` resolves to the
  text box centre; automatic escalation when a snapshot has no actionable
  elements or `find` misses.
- `menu` tool: open a menu path such as "File > Export > Add to Render Queue"
  through the accessibility menu bar, which stays accessible even in canvas
  apps.
- `file_dialog` helper: drive NSOpenPanel and NSSavePanel by typing a path.
- `wait_until`: wait for on-screen text, a file, or a URL status, with long
  timeouts for renders.
- `notes` tool: the agent records facts (file paths, URLs) that survive context
  compaction between phases.
- `mission run mission.yaml`: phases, budgets, checks, retries, recording hooks,
  a run log that a video editor can cut from.

Owner's words: "I want a11y-computer-use to be this strong."
