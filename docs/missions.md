# Missions

A mission is a long, multi-app task run as verified phases. Each phase is one
agent run with its own task text, the apps it may drive, a step budget, and
checks that the runner evaluates itself once the planner says it is done. A
failed check re-runs the phase with the failure written into the agent's notes.
The run leaves artifacts a video editor can cut from.

```bash
a11y-computer-use mission validate examples/missions/agency-demo.toml
a11y-computer-use mission run examples/missions/agency-demo.toml --provider claude-cli
a11y-computer-use mission run mission.toml --from-phase 3 --runs-dir runs
```

## Why phases

A 30-minute job across five apps does not fit one planner prompt. The history
would grow past any context, a wrong turn in minute 20 would need a restart
from minute 0, and "done" would rest on the planner's word. Phases give each
leg its own budget and an external check, so the mission advances only on
evidence: a file that exists and stopped growing, a URL that answers, text that
shows up in an app, a note the agent recorded.

Three tools carry facts across phases and around compaction:

- `notes(action, text)`: the agent records facts (paths, URLs, ids). The loop
  shows the notes in every planner turn, so compaction cannot lose them.
- `wait_until(condition, timeout_s, poll_s)`: wait for a file, a URL, snapshot
  text, or on-screen text (where OCR exists) instead of polling snapshots. Up to
  1800 seconds.
- Compaction: when the history passes a token budget (default about 60k), older
  turns collapse into one "so far" block that lists each earlier call and the
  first line of its result. The latest observation and the last exchange stay
  intact. `AgentResult.compactions` counts how often it happened.

## The file

```toml
[mission]
name = "agency-demo"        # letters, digits, - _ .
deadline_s = 3600           # optional wall-clock budget for the whole mission

[mission.record]            # optional shell hooks around the run
start = "echo start"
stop = "echo stop"

[[phase]]
name = "video"
task = "Generate the car video on Higgsfield and download it. Note the path as 'video:'."
apps = ["com.google.Chrome"]        # granted at `tier` for this phase, restored afterwards
tier = "full"                       # read | click | full
max_steps = 120                     # planner turns, up to 400
retries = 1                         # re-run the phase this many times on a failed check
check_timeout_s = 60                # how long each check may wait
on_fail_notes = "Downloads land in ~/Downloads."   # appended to the failure note
checks = [
  { file_stable = "~/Downloads/*.mp4", seconds = 5, min_bytes = 1000000 },
  { notes_contain = "video:" },
]
```

Phase keys not listed above are rejected, so a typo cannot silently disable a
check.

## Checks

Checks run on the runner's side after the planner calls `done` (or stops), and
the phase passes only when every check holds. They accept the same shapes as
`wait_until`, plus one runner-only kind:

| Check | Holds when |
|---|---|
| `file_exists = path`, optional `min_bytes` | a file matches the path (globs allowed, `~` expanded) and is at least `min_bytes` |
| `file_stable = path`, `seconds`, optional `min_bytes` | the newest match kept the same size for `seconds` (a finished download or render) |
| `url_status = url`, optional `status` | a GET returns that status (default 200) |
| `snapshot_text = text`, optional `app` | the text appears in a fresh accessibility snapshot of the app |
| `screen_text = text` | the text appears in on-device OCR of the screen; `unsupported` where OCR is missing |
| `notes_contain = text` | the agent recorded a note containing the text |

File paths are limited to the user's home unless `A11Y_COMPUTER_USE_ALLOW_ANY_PATH=1`.

The planner also sees the checks: each phase task ends with "This phase is
complete only when ..." so the agent knows what evidence to produce.

## Grants

Before a phase, the runner grants each app in `apps` at the phase's tier and
remembers what was there before. After the phase, grants return to their
previous state (a previously ungranted app is revoked). The planner never gains
a permanent grant from a mission run.

## Artifacts

Each run writes `runs/<mission>/<UTC timestamp>/`:

| File | Contents |
|---|---|
| `phase-NN-<name>.json` | attempts, checks with their outcomes, and the full agent result per attempt |
| `notes.json` | the notes at the end of the run |
| `result.json` | the mission verdict, phase summaries, durations |
| `timeline.jsonl` | one row per event with wall-clock `ts` and seconds-from-start `t`: mission start and end, record hooks, phase start and end, every step with tool and outcome, every check |

The timeline is what a video editor uses: it maps each recorded second to the
phase and step happening on screen.

## Recording

`[mission.record]` runs `start` before the first phase and `stop` after the
last one (or after a failure), so a recorder can be driven from the mission.
Screen Studio has no documented CLI or URL scheme at the time of writing, so
the example uses placeholders; any command works, and the timeline's timestamps
line up with the recording either way.

## The example

`examples/missions/agency-demo.toml` is the reel described in
[missions/agency-demo.md](./missions/agency-demo.md): six phases from a Telegram
brief to a Telegram reply. Telegram and the After Effects panels expose no
accessibility tree, so those phases rely on screenshots and coordinates today;
on-device OCR refs are the planned replacement. After Effects and Media Encoder
are not installed on the reference Mac yet, so the `edit` phase fails its checks
there until they are.

## Verified

Hermetic tests cover the notes store and its injection into planner turns,
every `wait_until` kind including timeouts and the home-directory rule,
deterministic compaction, the deadline stop, mission validation, and a
two-phase run with a retry, grant restoration, and the artifact set. No mission
has been run against real apps on a granted machine yet; the example file is a
plan, not a recorded result.

## URL checks and local servers

`url_status` refuses hosts that resolve to loopback, private, link-local, multicast, or reserved addresses, and it does not follow redirects. A planner can be steered by page content, so probing internal addresses would turn the check into a reachability oracle. Set `A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS=1` when a mission checks a development server on this machine.
