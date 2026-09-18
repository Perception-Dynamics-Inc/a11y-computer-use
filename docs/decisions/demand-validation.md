# Demand Validation — Failed-Automation Stories (docs/decisions/plan-2026-07.md §5)

**Date:** 2026-07-12 · **Status:** Complete — kill criterion evaluated
**Question:** Do at least 20 real, concrete "I tried to automate X native app and it failed" stories exist? (If not: reshape or kill the project.)

## Verdict

**PASS — 30 stories survived dedupe and quality filtering (threshold: 20).**

65 raw entries were mined; 30 were kept as core macOS-native-angle stories. A further ~10 Windows-platform and browser-only entries were strong but out of scope for the macOS launch claim; they are listed as supporting evidence below, not counted toward the threshold. The demand signal is real, recent (the majority of kept stories are from 2026), and spans four independent populations: computer-use tool users, tool *builders*, AppleScript novices asking LLMs for help, and mainstream reviewers of Claude/Codex desktop agents.

---

## 1. Methodology

**Sources mined:** GitHub issue trackers (trycua/cua, bytedance/UI-TARS-desktop, CursorTouch/MacOS-MCP, OthersideAI/self-operating-computer, OpenInterpreter/open-interpreter, electron/electron, OpenAdaptAI/OpenAdapt, mediar-ai/terminator, bytebot-ai/bytebot); Reddit (r/ClaudeAI, r/AI_Agents, r/applescript, r/LocalLLaMA); Hacker News threads; forums (Keyboard Maestro, MacRumors, Mac Power Users, Apple Communities); hands-on press/blog reviews (PCWorld, ZDNET/Yahoo Tech, How-To Geek, findskill.ai, dev.to, personal blogs).

**Inclusion criteria (a kept story must have all four):**
1. A concrete task against a named app or surface (not "agents are unreliable" in the abstract).
2. An observed failure mode, not a hypothetical.
3. A native-desktop angle relevant to an a11y-first macOS framework (browser-only entries excluded from the count).
4. A plausible, well-formed source URL. (URLs were carried from the mining pass; format-checked, not all individually re-fetched.)

**Dedupe rules applied:**
- Same person/thread/task → one story. Two iPhone Mirroring reports were kept as separate stories because they are different people, tools, and threads hitting the same surface (that repetition *is* the signal).
- Secondhand aggregations that only re-report a kept primary source were discarded (e.g. the chatforest.com UI-TARS review, which re-reports UI-TARS-desktop #1876 — the primary issue is kept instead).

**Discarded as weak (with reasons):**

| Entry | Reason |
|---|---|
| chatforest.com UI-TARS review | Secondhand aggregation; duplicates kept story #19 (UI-TARS #1876) |
| Medium "I let Claude run my Mac for weeks" | Vague failure ("did something I had not asked for"), paywalled, unverifiable |
| Gigazine UI-TARS install review | Install/docs failure, not an automation-workflow story |
| HN 44480517 (ncurses/ping) | Terminal apps, out of scope for a GUI a11y framework |
| HN 46594055 (Cowork Notion connectors) | Connector plumbing failure, not desktop control |
| r/ClaudeAI 1ukmisu ("powerful but fragile") | No specific app or task; theme captured by concrete stories |
| r/applescript 1gu6lhy (LLM AppleScript generally broken) | No specific task; theme captured by three concrete AppleScript stories |
| UI-TARS-desktop #408 (Word/Excel) | Feature request, not a failure story (its duplicate #407 merged with it) |
| bytebot #175 (Ollama "Task Failed") | Vague failure mode in a Linux VM; local-model demand covered in supporting evidence |
| OpenAdapt #145 (trackpad gestures) | Recorder gap, not agent control; marginal relevance |

**Trimmed for redundancy (real but over-quota within an already-saturated theme):** r/applescript 1aram5h (un-minimize windows), Apple Communities installer-scripting thread, balatero.com VimMode Electron post, HN 48032105 (latency), HN 43774258 (Cua VM), HN 48025696 (Codex screenshot loop), HN 48031468 (hit-and-miss), HN 47860942 (Tesseron), HN 47983840 (Maestro/iOS), MacRumors unattended-permissions and Siri threads, How-To Geek Draw Things review, Keyboard Maestro AI-access thread, MacOS-MCP #23 (deprecated capture API). These corroborate the kept themes and several are cited in §4.

---

## 2. Kept Stories (30)

### Theme T1 — A11y coverage gaps: native apps with missing, empty, or lying AX trees (6 stories)

| # | Task | App | Failure | Source |
|---|---|---|---|---|
| 1 | Dismiss Zoom's recording popup via AX API | Zoom.app | Custom-drawn UI has no AX elements; fell back to blind clicking | [HN 47992922](https://news.ycombinator.com/item?id=47992922) |
| 2 | Toggle HomeKit scenes from Keyboard Maestro | Home.app | No AppleScript dictionary; on-screen "buttons" aren't real AX buttons; blind X,Y clicks only | [KM forum](https://forum.keyboardmaestro.com/t/automating-home-app-to-set-a-scene/15334) |
| 3 | Enable Electron apps' AX tree via AXManualAccessibility | Slack, VS Code (Electron) | kAXErrorAttributeUnsupported from every tool; tree stays empty | [electron#37465](https://github.com/electron/electron/issues/37465) |
| 4 | Ship an a11y-tree desktop automation tool | WPF/Win32/Qt/Electron apps | Enormous share of apps expose elements poorly; per-toolkit handlers + vision fallback required | [HN 48032560](https://news.ycombinator.com/item?id=48032560) |
| 5 | Drive iPhone UI via iPhone Mirroring | iPhone Mirroring (macOS) | Remote-rendered surface likely exposes no AX tree; maintainers hypothesize the AX-first modality breaks here (issue relays an external user report) | [cua#2074](https://github.com/trycua/cua/issues/2074) |
| 6 | Click inside iPhone Mirroring window | iPhone Mirroring (macOS) | Injected clicks have no effect; same agent works in browser | [UI-TARS#1845](https://github.com/bytedance/UI-TARS-desktop/issues/1845) |

### Theme T2 — LLM-generated AppleScript / GUI scripting is a dead end (6 stories)

The purest demand signal: non-programmers are *already asking AI to drive native Mac apps* and the only substrate on offer (AppleScript/GUI scripting) fails them repeatedly.

| # | Task | App | Failure | Source |
|---|---|---|---|---|
| 7 | Clear recent-items lists via ChatGPT AppleScript | Finder, Preview | ChatGPT iterated 20+ times, never produced working code | [r/applescript](https://www.reddit.com/r/applescript/comments/1hbbekz/trying_to_clear_system_preview_finder_recent/) |
| 8 | Create event with 10-day recurrence via AI AppleScript | Calendar | ChatGPT never converged on Calendar's event/recurrence model | [r/applescript](https://www.reddit.com/r/applescript/comments/1caodqo/im_a_complete_amateur_trying_to_create_a_script/) |
| 9 | Navigate to & toggle "Announce the Time" | System Settings | ChatGPT-generated UI-scripting help didn't work; OP knew of Accessibility Inspector but had zero AppleScript experience (eventually hand-built a script) | [r/applescript](https://www.reddit.com/r/applescript/comments/1fpqi9p/navigate_to_announce_the_time_setting/) |
| 10 | Hide/show toolbar+inspector hotkey via ChatGPT-4o macro | Pages | State-dependent menu labels (Hide/Show) broke the hardcoded path | [MPU forum](https://talk.macpowerusers.com/t/chatgpt-4o-helped-me-create-a-keyboard-maestro-macro/37266) |
| 11 | Let coding agents control creative/office apps | Photoshop, Excel, Blender | Screen-vision agents went on long tangents; built per-app AppleScript/COM bridges instead | [HN 48049803](https://news.ycombinator.com/item?id=48049803) |
| 12 | Let a local LLM control native Apple apps | Notes, Contacts, Calendar, Reminders | Had to hand-write bespoke MCP tooling per app; explicit ask for an AI-to-native-desktop layer | [KM forum](https://forum.keyboardmaestro.com/t/mcp-for-keyboard-maestro/51429) |

### Theme T3 — Untrustworthy input synthesis & action verification (4 stories)

| # | Task | App | Failure | Source |
|---|---|---|---|---|
| 13 | Type into an RDP session from macOS | MS Windows App (RDP) | Unicode CGEvent keystrokes silently dropped; tool returns `verified:false` with no clear failure signal (under-signals; the false-confirm case is #14) | [cua#2083](https://github.com/trycua/cua/issues/2083) |
| 14 | Type into an Electron app via AX setValue | Codex.app (Electron) | Write false-confirms: read-back echoes the attribute, real editor state unchanged | [cua#2081](https://github.com/trycua/cua/issues/2081) |
| 15 | Open Chrome via Spotlight on AZERTY keyboard | Spotlight | QWERTY-assumed injection typed "Google Chro,e"; app never launched | [soc#109](https://github.com/OthersideAI/self-operating-computer/issues/109) |
| 16 | Check a Calendar appointment via Spotlight (AZERTY) | Calendar / Spotlight | Typed "cqlendqr"; couldn't launch Spotlight; task never started | [OI#1530](https://github.com/OpenInterpreter/open-interpreter/issues/1530) |

### Theme T4 — Pixel/coordinate grounding fragility on macOS (4 stories)

| # | Task | App | Failure | Source |
|---|---|---|---|---|
| 17 | Play music | Spotify | Vision guessed play button at wrong percentage coordinates | [soc#7](https://github.com/OthersideAI/self-operating-computer/issues/7) |
| 18 | General control on Retina/4K Mac | macOS desktop | 2x scaling breaks screenshot→screen coordinate mapping | [soc#94](https://github.com/OthersideAI/self-operating-computer/issues/94) |
| 19 | Local-model computer use on macOS Tahoe 26 | Any app | Click marker vs actual click diverge after OS update | [UI-TARS#1876](https://github.com/bytedance/UI-TARS-desktop/issues/1876) |
| 20 | Play chess; create a Notes shopping list | Chess.app, Notes | Couldn't click pieces on 3D-perspective board; retries burned a 5-hour usage allotment in ~30 min | [PCWorld](https://www.pcworld.com/article/3097542/claude-controlled-my-mac-for-half-an-hour-it-was-a-wild-worrisome-ride.html) |

### Theme T5 — Mainstream native-app workflows too slow, unreliable, or expensive (5 stories)

| # | Task | App | Failure | Source |
|---|---|---|---|---|
| 21 | List recent files; summarize Calendar; make a Note; draft a Mail | Finder, Calendar, Notes, Mail | Far slower than manual; re-grant permissions per request; subject typed into Mail's To field | [ZDNET/Yahoo](https://tech.yahoo.com/ai/claude/articles/let-claude-ai-control-mac-123500940.html) |
| 22 | Play music; scheduled background GUI automation | Music.app | Perplexity agent stopped short of the Play button (AppleScript integration couldn't do it); Codex fails silently on lock screen | [danielvaughan.com](https://codex.danielvaughan.com/2026/04/17/codex-app-computer-use-macos-background-gui-automation/) |
| 23 | Spreadsheet → presentation → email workflow | Office apps via Cowork | ~50% success; breaks mid-task; loses context across app switches | [findskill.ai](https://findskill.ai/blog/claude-computer-use-honest-review/) |
| 24 | Open and control native apps/folders | Finder + native apps | Claude's environment only exposed Safari despite all permissions granted | [r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1urhmjj/desktop_app_only_detects_safari_in_computer_use/) |
| 25 | Order groceries end to end | Desktop agent flow | 30s thinking + ~$0.10/click; hallucinated an OTP; author built record-replay tool instead | [r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1tyqwsa/a_computeruse_agent_that_thinks_for_30_seconds/) |

### Theme T6 — A11y-first works, but the engineering is hard (5 stories)

Direct validation of docs/decisions/plan-2026-07.md §6's contracts: ref lifecycle, display-qualified coordinates, supervised workers, TCC design.

| # | Task | App | Failure | Source |
|---|---|---|---|---|
| 26 | Cross-app handoff (Mail link → Safari) | Mail, Safari, modal sheets | Stale tree after frontmost change (old PID queried); clicks through modal sheets; negative multi-monitor coords hit wrong screen | [r/AI_Agents](https://www.reddit.com/r/AI_Agents/comments/1ti5a3p/the_accessibility_tree_gotchas_that_kept_breaking/) |
| 27 | Automate SMB invoicing/CRM/scheduling | SMB desktop apps | Pixel agent lost its place on re-sort/resize; **switching to the a11y tree made it reliable** | [r/AI_Agents](https://www.reddit.com/r/AI_Agents/comments/1u2kb1s/the_boring_desktop_tasks_turned_out_harder_to/) |
| 28 | Desktop tasks on multi-monitor Mac | Native app windows | Minutes wasted locating which monitor a window is on | [r/ClaudeAI](https://www.reddit.com/r/ClaudeAI/comments/1u296ut/claudes_computer_use_hilaritywhiplash/) |
| 29 | Watch the focused window with an AX observer | Chrome (macOS) | Observer forces tree rebuilds on every navigation; 1-2s system-wide freezes | [MacOS-MCP#5](https://github.com/CursorTouch/MacOS-MCP/issues/5) |
| 30 | Run a desktop-automation MCP under Claude Desktop | macOS TCC | Spawn-via-helper gives child an empty TCC identity; AXIsProcessTrusted() always False | [MacOS-MCP#26](https://github.com/CursorTouch/MacOS-MCP/issues/26) |

---

## 3. Theme Ranking (frequency × intensity)

| Rank | Theme | Count | Intensity signal |
|---|---|---|---|
| 1 | **T2 — LLM+AppleScript dead end** | 6 (+3 trimmed corroborating) | Non-programmers already ask AI to drive Mac apps and fail after 20+ retries; experts build per-app bridges by hand; explicit "give AI a native control layer" asks. This is unserved demand stated in the users' own words. |
| 2 | **T1 — A11y coverage gaps** | 6 | Silent, total failures (empty trees, no-op clicks) on exactly the apps people want automated (Zoom, Home, Electron). This is simultaneously the strongest argument *for* an a11y-first framework and its biggest technical risk — confirms docs/decisions/plan-2026-07.md's vision-fallback-as-core-deliverable. |
| 3 | **T5 — Mainstream workflows too slow/unreliable/expensive** | 5 (+4 trimmed) | Hands-on reviewers with large audiences independently conclude the built-in agents are slower than manual work; ~50% success; $0.10/click. The head-to-head benchmark wedge lands directly on this pain. |
| 4 | **T6 — A11y engineering is hard** | 5 | Builders who tried the a11y approach hit stale refs, modal sheets, multi-monitor coords, observer perf, TCC attribution — and one reports the a11y switch *fixed* reliability. Validates that a reusable framework (not another weekend MCP) is the product. |
| 5 | **T3 — Untrustworthy input synthesis** | 4 | Highest severity per incident: silent false-confirms poison the whole agent loop. Directly validates the three-path `type()` spec and verification-on-write in docs/decisions/plan-2026-07.md §6. |
| 6 | **T4 — Pixel/coordinate fragility** | 4 (+quantitative evidence below) | Every macOS release (Retina, Tahoe 26) breaks coordinate mapping; custom-drawn UI defeats vision grounding. The "why a11y refs" half of the pitch. |

---

## 4. Top-3 Hero Workflows

Selection criteria: (a) recurs across independent stories, (b) demoable in under 60 seconds, (c) exercises the AX tree of a native (non-browser) macOS app, (d) has a machine-checkable end state, (e) works on a stock Mac with no accounts or installs.

### Hero 1 — Calendar round-trip: "Create a recurring event and read it back"

*Create an event ('Standup', next Tuesday 09:30, repeats weekly) in Calendar.app, then confirm it exists by reading the event back from the AX tree.*

- **Supporting stories:** #8 (ChatGPT never converged on Calendar recurrence via AppleScript), #16 (Open Interpreter couldn't even launch Calendar — garbled AZERTY typing), #21 (Claude far slower than manual on Calendar summarization), #12 (Calendar among the apps users hand-wrote MCP tools for). Trimmed corroboration: MacRumors LLM-Siri thread (calendar lookups failing ~50% of the time).
- **Why it's a good benchmark/demo:** stock app on every Mac; pure-AppKit UI with a rich, deep AX surface (sidebar, month grid, event-editor popover, date/time steppers, recurrence popup) that pixel agents fumble and AppleScript novices demonstrably can't reach; the created-event read-back gives a deterministic pass/fail for CI; the whole loop fits in well under 60 seconds via refs. It is also the single most-recurring app across the corpus.

### Hero 2 — System Settings toggle: "Navigate to a named setting and flip it"

*Open System Settings, navigate to Control Center (or Clock → 'Announce the Time'), flip a named switch, and verify the new value via AX read-back.*

- **Supporting stories:** #9 (ChatGPT GUI-scripting of exactly this setting failed a motivated novice), #30 (TCC/settings-adjacent permission failures), #19 (Tahoe 26 broke pixel coordinates — OS churn punishes pixels), #21 (per-request permission re-granting pain). Trimmed corroboration: Apple Communities thread — UI scripting breaks across macOS versions; MacRumors thread — agents stall on Settings permission prompts.
- **Why it's a good benchmark/demo:** System Settings is the canonical deep hierarchical AX tree (SwiftUI panes, sidebar search, nested groups); the toggle's boolean AX value is a perfect binary exit criterion; and it is the sharpest pixel-vs-tree contrast available — Apple redesigns Settings cosmetically every release (Ventura, Tahoe), killing coordinate scripts while AX identifiers survive. Bonus: it demos the safety layer (settings changes are a natural confirmation-gate showcase).

### Hero 3 — Music.app: "Find a song and play it"

*Search the Music.app library for a named track, press Play, and confirm via the now-playing AX state — with audible proof.*

- **Supporting stories:** #22 (Perplexity's Mac agent stopped short of the Play button; its AppleScript integration couldn't do it — though note the same source shows Codex *succeeding* at this task, so the demo contrast is speed/cost/reliability, not impossibility), #17 (vision grounding guessed Spotify's play button at wrong coordinates — the same media-transport failure class), #25 (per-click cost makes long hunts absurd; this demo is 4-5 actions). Trimmed corroboration: MacRumors LLM-Siri thread (playlist playback errored repeatedly).
- **Why it's a good benchmark/demo:** instantly legible — the room hears success within seconds, ideal for the split-screen hero gif (pixel loop hunting vs. refs clicking once); media transport controls are a documented vision-grounding weak spot going back to 2023; Music.app ships on every Mac with a genuine AX tree (search field, songs table, transport buttons, now-playing text) exercising text input + table navigation + button press + state read-back in one sub-60s task.

**Runner-up:** Finder "five most recent files in Documents" (#21, #7) — strong AX-table exercise, kept as a candidate for the `examples/` gallery rather than a hero slot because it demos less visibly than audio playback or a flipped toggle.

---

## 5. Supporting Evidence (not counted toward the kill criterion)

**Quantitative, browser-only:** [r/ClaudeAI controlled experiment](https://www.reddit.com/r/ClaudeAI/comments/1uaeb9s/i_compared_claude_opus_48_computer_use_vs_browser/) — same Claude Opus 4.8 model, 5 identical tasks, only the perception layer changed: pixel computer-use spent 16 steps hunting an add-to-cart button that structured access found in 4 steps at one-third the cost. This is the head-to-head shape of the launch benchmark, already replicated informally by a third party.

**Windows-platform (relevant to Phase 2, same failure classes):**
- [HN 48029567](https://news.ycombinator.com/item?id=48029567) — Epic EHR: no API access forces vision screen-scraping plus a standing human error-review team; the economic ceiling of pixel automation.
- [cua#2100](https://github.com/trycua/cua/issues/2100) — UIA enumeration 100x slower than the raw API; a11y perception perf is a real product problem.
- [terminator#473](https://github.com/mediar-ai/terminator/issues/473) — multi-monitor geometry breaks element clicks (mirrors kept stories #26/#28 on macOS).
- [r/LocalLLaMA Windows MCP](https://www.reddit.com/r/LocalLLaMA/comments/1rqh9uz/open_source_mcp_server_that_gives_any_ai_agent/) — UIA-invisible surfaces (web dialogs, Flutter, dark themes) forced an OCR fallback.
- [HN 48032043](https://news.ycombinator.com/item?id=48032043) — enterprise app deliberately hides grids from accessibility APIs.
- [dev.to CliGate](https://dev.to/codekingai/my-ai-assistant-could-code-but-it-couldnt-operate-my-desktop-4d97) — "the moment a workflow hit a real desktop app, the illusion broke."
- [HN 43776085](https://news.ycombinator.com/item?id=43776085) — unprompted "this but for Windows" demand.

**Local-model demand (launch hook per docs/decisions/plan-2026-07.md §4):** [r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1segtsi/replaced_perplexity_computer_with_a_local_llm/) — cloud agent burns credits, wants $200/mo; no working local-first alternative known to the community. Corroborated by kept story #28 (user wants a small local model to execute actions).

---

## 6. Implications for the Plan

1. **Kill criterion passed; proceed to the Phase 0 spike.** The §5 gate is cleared with headroom (30/20), and the stories are recent and first-person.
2. **Hero workflows replace synthetic exit criteria** (per docs/decisions/plan-2026-07.md §5): Phase 0/1 exit = Calendar round-trip, System Settings toggle, and Music find-and-play completed end-to-end via refs alone, benchmarked against the pixel-loop reference.
3. **The corpus independently re-derives docs/decisions/plan-2026-07.md §6's hard contracts** — stale refs/PID staleness (#26), display-qualified coordinates (#26, #28), Electron false-confirm verification (#14, #3), layout-aware typing (#15, #16, #13), observer perf budgets (#29), TCC responsible-process design (#30). None of these were invented risks.
4. **Beta cohort recruiting list:** the authors behind stories #1, #4, #11, #12, #22, #25, #26, #27 are builders or power users with demonstrated willingness to invest effort; they are the first 8 outreach targets for the 10-20 person cohort.
5. **Honest caveat:** three stories predate 2024 (#2, #3, #17) and are kept because their failure modes are confirmed still-live by 2026 stories in the same theme. If a stricter recency filter (≥2024) were applied, 27 stories remain — still above threshold.
