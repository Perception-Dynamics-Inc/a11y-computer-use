# Lessons learned (append-only; newest at the bottom)

- The libatspi child cache goes stale when the AT-SPI event thread is on (`A11Y_COMPUTER_USE_ATSPI_EVENTS`); code that relies on fresh children (e.g. Calc grid editors, #475/#411) must not assume the cache is current.
- #489's Audacity track/clock labels were applied app-wide and leaked into the crash-recovery dialog (#505). Scope app-specific labels to the window/role they belong to.
- #512's same-app dialog occlusion refusal broke tests/test_server.py on every platform. Run test_server.py for any change that affects click results, not just Linux suites.
- #498 (Qt toggle click, #493) may have regressed `test_linux_qt_line_edit_combo_and_values` on main. Qt click changes must run the Qt live tests' synthetic equivalents and check line edits/combos.
- GTK comboboxes: options were capped at the first 64 children (#513, fixed in #516). Never cap option lists silently; look up the requested value among all children and say when an error list is truncated.
- The first #503 attempt (dropping GTK 4 nodes with garbage sizes) broke the huge Calc and combo popup tests. Large-but-valid geometry is legitimate; only filter clearly uninitialized sizes.
- 2026-10-13 (#480): Linux maps native AT-SPI terminals to AXTextArea; inspect native terminal ancestry, including remembered editable handles, rather than treating every multiline editor as a shell. Preserve cached-field ownership checks before terminal detection so stale focus still returns an audited focus_changed error. Execution permission is independent of per-app FULL grants and defaults off for MCP Runtime hosts as well as agents.
- 2026-10-13 (#537): Repeated AT-SPI `get_application` calls are expensive in Calc snapshots. Cache successful owner lookups within the existing operation-scoped getter cache; preserve retries for missing owners and invalidate on mutations. A getter can return a fresh GI wrapper, so toolkit attributes attached to that wrapper alone do not prevent repeated owner lookups.

- 2026-10-13 (#467): Geany live fixtures must launch with `--new-instance`; otherwise a launch can delegate to an existing editor, so terminating the returned process leaves windows behind and contaminates later symbol/caret assertions. Verify the Scintilla and symbol-click fixtures together under AT-SPI.
- 2026-10-13 (#548): A mapped GTK4 window can precede AT-SPI registration. Before reporting a missing bridge, preserve the remaining existing process-startup grace for an unambiguous owner; older apps still fail fast. Keep registration tests hermetic and run them with gi imports disabled to check non-Linux dependency isolation.






- 2026-10-13 (#202): When merging grant aliases with desktop launch resolution, preserve GenericName program selection and exact permission gates alongside grant-selected labels. Installed bundle resolution must keep the requested spelling even if a Linux executable with a case-folded name is on PATH.
- 2026-10-13 (#488): xdg-desktop-portal backends share one process across requesting apps. Resolve the full backend name before matching it, inherit identity only from a managed `WM_TRANSIENT_FOR` parent, and bind observations to the active chooser frame. Never add a portal PID to a requesting app's `pids_matching` set, expand that frame into sibling dialogs, or treat a matching portal PID as sufficient for a pointer permission recheck: this would expose other apps under the requesting app's grant.



- 2026-10-13 (PR #528): Show-desktop requests are asynchronous; verify foreground focus has settled before starting a short agent budget. Use a scripted monotonic clock in the setup regression so host scheduling cannot exhaust the wait, and check the hermetic tests with GI imports blocked.
- 2026-10-13 (#530): `gh run view --log-failed` can return empty output for a failed job. Fetch `repos/{owner}/{repo}/actions/jobs/{job_id}/logs` directly before concluding there are no failure details.
- 2026-10-13 (#496): Okular's childless PDF filler has no AT-SPI Text interface. Keep document surface labels scoped to the unnamed layered/split document pane with a scrollbar sibling; exclude dialogs and named sidebar panes. Do not treat an in-app search invocation as a verified page-text match.
- 2026-10-13 (#515): AbiWord opaque document views can select text through native Find without exposing a confirming accessibility match. Refuse before Ctrl+F/query input when the search container contains an opaque region; unrelated opaque siblings must not block a verifiable container. Launch-gate tests must stub PATH and desktop entries because executable casing is intentionally canonicalized.

- 2026-10-13 (#406): Linux snapshot titles fall back to AT-SPI descriptions when names are empty; normalize both fields. Rich-text tag matching must respect quoted attribute delimiters and full tag names so `>` inside an attribute and literal `<p-custom>` labels are handled correctly.

- 2026-10-13 (PR #529 CI repair): Calc can publish a verified editor value during type settlement and then lose that transient editor in a later AT-SPI snapshot. Carry the actual settled read into the immediate outcome check once, bound to its app; do not turn an acknowledged input into assumed effect or relax live assertions.
- 2026-10-14 (#446 repair): A Calc grid read can time out while the final cell editor remains open. Refresh both native child caches and operation-scoped getters, keep mask changes on the AT-SPI event thread, and commit the discovered editor before reading sibling cells. Refresh the subsequent table lookup too. Reproduce live failures with CI's window manager and display size.
- 2026-10-13 (#503): Keep the GTK 4 garbage-allocation filter limited to unnamed, childless panels; large real containers still need their extents for clipping and sampling. When merge conflicts join appended regression tests, retain both branches' tests and compare the final diff against main.

- 2026-10-14 (#521): Header-only accessibility detection must stay within the selected container’s reported descendants and require header geometry plus a scrollbar. Unrelated rows, ordinary body controls, and unknown bounds must not trigger early refusal.
- For distro-installed native test libraries, use the package manager file list rather than a recursive glob of `/usr/lib`. Recursive glob follows SDK symlinks and can consume the entire live-suite CI timeout before the application starts. Preserve the live assertions and test the bounded discovery path.

- 2026-10-13 (#464): Merging a browser-name gate with verified find support must retain both the native-viewer refusal and the driver capability probe; otherwise the query audit and focused-find-field checks become unreachable. Calc editor discovery must bypass the native child cache and invalidate operation-scoped child reads together on the event thread, restoring the default mask even when the read fails. Keep Calc grid verification waits off the event thread and marshal each native read, including direct Table calls, individually; holding the event loop through the waits can leave the registry marked hung after the commit.
- 2026-10-14 (#202): Compare a failed Actions run's head SHA with the current remote PR head before repairing it; a newer commit may already address the failure. Preserve those remote commits when merging main and pushing without force.
- 2026-10-14 (#491): In restricted Linux sandboxes, CLI subprocess tests can abort in libatspi before Python handles an error when the accessibility bus is unavailable. Use a fresh D-Bus session and writable XDG_CACHE_HOME; preserve access to GSettings schemas when isolating XDG desktop entries. Installed desktop programs can also change mock launch-resolution results, so distinguish environment-dependent failures from code regressions.
- 2026-10-13 (#567): Lifecycle trace readers need flushed events, not physical-storage durability. `os.fsync` before every agent step can stall Windows runs; flushing and closing the file preserves cross-process visibility. Verify both MCP completion and subprocess cancellation when changing this path.
- 2026-10-14 (PR #535): When merging AT-SPI naming fallbacks, retain independent role-specific rules together. Relation-derived GTK menu labels must pass through the same rich-text normalization as native names; cover quoted attribute delimiters in the relation-label regression too.
- 2026-10-14 (#446 repair): A post-commit pause inside the AT-SPI event-thread callback cannot process the editor-removal event. Commit in one driver operation, pause on the caller, then verify in the next operation; keep the existing pause bound.

- 2026-10-14 (#548 repair): Injecting a Linux driver into Runtime does not change its host-platform app resolver. Synthetic Runtime regressions must supply the fake app identity as well as Linux app/window lists, so macOS does not query NSWorkspace before reaching the driver.

- 2026-10-14 (#560 repair): EditableText alone does not prove a GTK text view is writable; preserve the native EDITABLE recheck and the shared read_only override when merging observation changes. Refresh the PR branch from origin before resolving main conflicts so existing repairs are retained.
- 2026-10-14 (#560 validation): In a sandbox with a read-only home cache, AT-SPI bus activation aborts Python subprocesses even in hermetic CLI tests. Set XDG_CACHE_HOME to a writable temporary directory and run under dbus-run-session; an Xvfb session alone does not fix the cache path.
- 2026-10-14 (#515 repair): Opaque document search refusal must run before unreadable-container returns and outside Chromium-only Find eligibility; preserve the separate unreadable-list result and scope the refusal to the chosen container.

- 2026-10-14 (#501 repair): Xournal++ zoom uses `log(percent / 100 - 0.20753)` and requires both raw Value and displayed percent read-back. In isolated Chrome live tests, a D-Bus keyring prompt can steal keyboard focus; reproduce with CI's window manager and prevent unrelated credential prompts in the temporary desktop session before diagnosing an input regression.
- 2026-10-14 (#578): GTK 4 masked Gtk.Entry can expose neither password role nor protected state nor visibility metadata. Positive-offset Text reads after an empty sentinel must stay private to boolean write verification; publishing them leaks secrets even when is_secure is false.
- 2026-10-14 (#559 repair): Check shell-capable launchers before app resolution so command strings return audited exec_disabled rather than app_not_found. Inspect executable symlinks and desktop Exec programs even when gtk-launch delegates the launch; interpreter launch classification must not block ordinary GUI input merely because its process comm is python3.
- 2026-10-14 (#472 repair): Inkscape exposes empty dock content as an opaque filler beside its SVG drawing area. Preserve existing surface labels when adding canvas facts; an empty dock may use the real window/document label, but must not inherit the SVG canvas's inaccessible-content note. Verify with a real Inkscape snapshot as well as synthetic drawing areas.

- 2026-10-14 (PR #549 repair): When merging adjacent app-specific surface labels, retain each app recognizer and avoid overwriting an already recognized surface with an unrelated fallback. Run both applications' synthetic snapshot tests after resolving the conflict.
- 2026-10-14 (PR #539 repair): A failed-looking PR check can be a cancelled job with no failed step or retained log. Inspect the Actions jobs API conclusion and steps before attributing it to a platform regression.
- 2026-10-14 (#464 repair): When combining Calc commit fixes, marshal the editor commit and individual native reads to the event thread, but keep both the post-commit pause and the grid verification loop on the caller. Regression tests should assert that events can run between operations as well as that native reads retain thread ownership.
- 2026-10-14 (PR #259): Keep native AT-SPI EDITABLE-state enforcement distinct from synthetic transports that have no native state API. Preserve the latter's verified write/focus fallback, and forward internal keyword arguments (such as Writer's force=True) when wrapping a write to restore caret/selection on failure.
- 2026-10-13 (#259): When tightening AT-SPI entry-state checks, preserve the dedicated Writer document and table-cell paths and the Impress/Draw paragraph exception. Writer cell replacement uses `set_text(..., force=True)` without selection; wrappers that restore caret/selection must forward that flag.
- 2026-10-14 (PR #259 merge): Caret-restoration wrappers must retain private GTK 4 write verification in the wrapped implementation; do not replace that verification with snapshot text reads, which must keep masked entry contents private.
- 2026-10-14 (#478): Kate’s painted gutter has no accessible line labels. Scope its contextual name to native editor siblings and retain the opaque-region limitation; never infer line targets from the strip’s bounds. When merging surface-label branches, keep each application’s recognition condition independent.
- 2026-10-14 (#478): Kate gutter geometry regressions must stub the AT-SPI coordinate binding as well as state flags, so native GI availability cannot change synthetic extents. Keep the gutter opaque and never derive line targets from its measured box.

- 2026-10-14 (#548 repair): GTK program names can differ from interpreter process names before AT-SPI registration. Measure startup age from an unambiguous WM_CLASS window PID and preserve the original caller name for this timing hint; titles alone are insufficient and permission routing must stay unchanged.

- 2026-10-14 (#560 merge repair): A sandboxed test process can see desktop windows with host PIDs that differ from subprocess PIDs. Such a session cannot validate live window ownership; run hermetic checks without DISPLAY/WAYLAND_DISPLAY and use CI for native verification. Keep the live ownership assertions intact.

- 2026-10-14 (#570 repair): Writable worktree files do not guarantee writable linked Git metadata or protected lesson files. If those mounts refuse writes, use a writable temporary clone of the current remote PR branch, merge main there, and push normally to preserve branch history.

- 2026-10-14 (PR #539 merge repair): In a sandbox sharing a desktop, GTK live tests can enumerate a launched window yet fail their on-screen/process-family match. Reproduce the same assertion from an unchanged main checkout before attributing it to a merged server change; do not relax window ownership checks.
- 2026-10-14 (#552 repair): Opaque-search refusals must remain before input when merging verified Find support; retain the FULL grant gate and focused find-bar verification as independent requirements.
- 2026-10-14 (PR #562 repair): When merging viewport text reads with app-specific snapshot fixes, retain both native visibility helpers and independent label/value handling. Run Qt, AT-SPI cache, and shared server regressions; whole-buffer Text values do not establish substring visibility.
- 2026-10-14 (#542): Omit clipped prefixes and suffixes from Qt viewport text, adding separators only between visible fragments. Placeholder prefixes can make a genuine newly visible token look like text appended to an editor query and prevent shared scroll-search confirmation.
- 2026-10-14 (#542 repair): Qt line edits can map to AXTextArea without a SINGLE_LINE state. Require native MULTI_LINE or a multiline attribute before viewport text reads; probing GetBoundedRanges on ordinary Qt controls can stall snapshots when Qt cannot marshal QSpiTextRange replies.
- 2026-10-14 (PR #562 CI repair): urllib HTTPSHandler eagerly creates a TLS context even in an HTTP-only opener. Initialize the shared HTTP handler directly when an overridden https_open already creates its verified context; otherwise native trust-store loading can exhaust a short HTTP observation deadline before the request. Test that HTTP probes never load TLS settings.
- 2026-10-14 (PR #562 merge repair): Preserve Kate's app-specific document visibility refusal after generic Qt viewport reads. Cover both Kate and FeatherPad with an available viewport result so a successful generic read cannot silently override the conservative KTextEditor behavior.

- 2026-10-14 (PR #564 merge repair): An inherited DISPLAY can name an inaccessible server in the command sandbox. Run display-dependent focused tests under a fresh Xvfb/D-Bus session; run the hermetic suite without a display so existing prerequisite guards reflect the actual environment.
- 2026-10-13 (#518): `_confirm_focus_on_target` may call `grab_focus` again, so explicit pointer clicks must poll `_focus_on_target` directly. A focus confirmation helper that mutates focus would hide a missed click and violate the requested action.

- 2026-10-14 (PR #549 merge repair): A reachable AT-SPI bus can enable desktop live fixtures even without a display. Run those fixtures under Xvfb with a working window manager; an extracted Openbox also needs its theme directory in XDG_DATA_DIRS. Verify the window manager starts before diagnosing app-activation failures as code regressions.
- 2026-10-14 (PR #539 repair): A supplied worktree can lag the remote PR branch and have read-only shared Git metadata or protected `.codex` files. Fetch into a writable checkout and start from the current remote head before merging main; a plain push must retain that remote head as an ancestor.

- 2026-10-14 (#568 repair): gucharmap Character Table is a childless GTK drawing area. Keep its surface label separate from statusbar selection: a codepoint/name there is not a character-cell ref, and scrolling the opaque table may change selection.

- 2026-10-14 (#548 repair): A window may map between the registration eligibility check and the final missing-root check. Do not report a missing bridge for an app that has not received its registration retry; keep that first observation retryable. Exercise this ordering with synthetic lists and a scripted clock.

- 2026-10-14 (PR #570 conflict repair): Refresh the remote PR head before merging main; an older local branch can miss repairs already pushed. Resolve independently appended regression tests by retaining both complete test blocks and check the resulting diff against main.

- 2026-10-14 (#570 merge repair): Preserve main's platform_editable override when merging text-view guards. A text-view handle with a state-set API still needs the live EDITABLE recheck when GI is unavailable; transports without a state-set API retain their existing verified replacement path.

- 2026-10-14 (#202 repair): Linux live segfault logs can identify concurrent native getters beside `Atspi.event_main`. Preserve main's `_call_first` event-thread dispatch and its regression when merging identity changes; driver-level dispatch alone does not cover direct AT-SPI callers.
- 2026-10-14 (PR #472 repair): A drawing-canvas fallback must run only when no surface label was already recognized. Otherwise an empty Inkscape result erases Gnumeric and gucharmap labels; run those surface regressions alongside the canvas tests when merging app-specific naming rules.

- 2026-10-14 (PR #472 repair): Inkscape can expose both its SVG canvas and a childless painted filler inside the Fill tab as opaque regions. Preserve their native bounds and derive document context from the native frame title; keep the inaccessible SVG content note specific to the drawing canvas. An empty app-specific fallback must not overwrite another application's surface name.
- 2026-10-14 (PR #472 merge repair): Concurrent isolated D-Bus test sessions need distinct writable XDG_CACHE_HOME directories. AT-SPI bus launchers share the cache socket path; ending one session can remove another session's socket and abort MCP subprocess tests. Recheck bus failures in a fresh session with its own cache before treating them as code regressions.
- 2026-10-14 (PR #549 repair): A supplied worktree can lag its tracking branch even when its files are clean. Fetch and preserve the current remote PR head before merging main, so a plain repair push retains prior repair commits.

- 2026-10-14 (PR #467 repair): An unnamed GTK status bar can expose its caret/selection label only through Text. Preserve that native text when mapping it to AXStatusBar; keep numeric Value probes disabled and retain any explicit accessible name. Merge Scintilla visibility filtering with exact-target selection and opaque-search refusal independently.
- 2026-10-14 (PR #467 repair): A document reveal takes a new snapshot; apply exact-target preference again after that refresh so incidental labels do not reappear as find targets. Preserve opaque-container refusals when combining visibility filters with scroll search.

- 2026-10-14 (PR #467 repair): After revealing an off-screen document match, apply exact-target preference again to the refreshed snapshot. Merge the visibility filter with current search filtering so a refresh does not resurrect broad document or label matches that main excludes.

- 2026-10-14 (PR #467 repair): After revealing a document match, apply exact-target preference again to the refreshed snapshot. Unnamed GTK status bars can expose their displayed message only through Text; retain the statusbar role and avoid a numeric Value fallback.


- 2026-10-14 (#467 repair): GtkStatusbar may expose caret status only through its Text interface. Preserve that text as the unnamed bar's title while keeping numeric Value probes disabled; verify Geany symbol clicks alongside Scintilla search.
- 2026-10-13 (#467): Refresh AT-SPI status-bar properties before reading geometry. GTK can cache the bar's unallocated startup rectangle; refreshing later in `_state_flags` leaves that snapshot with stale zero-size geometry and drops caret/status evidence. Cover the allocation transition hermetically and verify Geany symbol clicks and Scintilla find together live.

- 2026-10-14 (PR #467 repair): Repair checkouts may fetch only main. Inspect remote.origin.fetch and explicitly fetch the PR branch before pushing so concurrent repairs are merged rather than overlooked.
- 2026-10-14 (PR #562 repair): Qt viewport text filtering must coexist with application-specific visibility restrictions such as Kate, Evince page handling, and independent button labels. Preserve both sides of appended regression conflicts and compare the resolved patch against main.
- 2026-10-14 (PR #562 repair): Trim clipped-character placeholders at the edges of Qt viewport text, while retaining internal separators. Leading placeholders otherwise trigger the shared appended-query guard when the visible viewport contains exactly the requested token.
- 2026-10-14 (#492): Gnumeric's name-box `go-jump` image can crash the application through accessibility activation. Refuse before semantic activation and element pointer input; do not substitute keyboard navigation for an explicit click.

- 2026-10-14 (PR #566 repair): Appended test conflicts can contain regressions already present earlier in main. Preserve each test once and compare the resolved diff against main before validation.

- 2026-10-14 (#600): Chromium single-line inputs cannot retain a trailing newline. Verify the payload in the native entry before sending Enter once; a simultaneously focused PDF toolbar container is not field read-back. Preserve both typing regressions and native activation safety regressions when merging appended tests.

- 2026-10-14 (#584): Empty GTK 4 plain and visibility-off entries both expose an empty rendered LINE and no visibility metadata. A zero character count makes this state unknown, not secure; first writes require private buffer equality and redacted receipts/audit. Only later rendered text distinguishes visible contents from masking. GTK text roles may map to AXTextArea even for single-line entries.

- 2026-10-14 (PR #614 CI repair): AuditLog accepts a directory and writes daily JSONL files inside it. Live redaction assertions must read those files, require nonempty audit output, and never call read_text on the audit directory, even when it was named audit.jsonl.
- 2026-10-14 (#612): Element bounds are optional in model observation diffs. Compare movement thresholds only when both rectangles exist; preserve missing/present transitions and unrelated state changes without dereferencing missing geometry.

- 2026-10-14 (#577): AT-SPI heading role changes must preserve text-role filtering for uninitialized Value doubles as well as text matching. Carry only the reported positive heading level; do not infer it from HTML tags or geometry. Preserve independent HTML section regressions when merging appended tests.

- 2026-10-14 (#577 repair): Chromium headings around a same-named button have no native static-text child. Preserve that existing control-label ref when mapping ordinary headings, and run the live statictext-click regression; do not synthesize child geometry or substitute an action.

- 2026-10-14 (#589): GTK transient siblings can change the pruned path of an unnamed textarea without replacing it. Rebind only when the fresh snapshot contains the same AT-SPI bus/object identity and preserves process/document binding; geometry and an unnamed role alone do not identify the editor. Preserve independent appended regression tests when merging main.
- 2026-10-14 (PR #606 repair): Gate Chromium focused-entry discovery on the already observed node’s toolkit before repeating focus discovery. Native Calc read-back must retain a single focus-tree lookup; browser-specific disambiguation must not add native tree traversals to other apps.

- 2026-10-14 (PR #562 conflict repair): Qt viewport-search and app-registration regressions can conflict where both branches append tests. Retain both complete blocks; confirm the failed Linux job contains only the tracked Calc timing flake before changing unrelated code.
- 2026-10-14 (#590): GTK 4 GtkMenuButton may expose an actionless push-button wrapper around its actionable toggle. Fold only a sole toggle with the same native name and positive, identical measured bounds; preserve the inner native handle so find refs use the existing verified click path.
- 2026-10-14 (#588): GTK 4 text ranges can reject SCREEN coordinates and point-to-offset queries while WINDOW range extents work. Compare ranges and widget bounds in WINDOW space; keep full-buffer write verification separate from visible snapshot values.

- 2026-10-14 (PR #626 repair): GTK 4 bridges can omit WINDOW GetRangeExtents while supporting GetCharacterExtents. Keep unavailable geometry unreadable, but verify every character through the native fallback rather than relying on endpoint boxes for wrapped lines.

- 2026-10-14 (PR #623 CI repair): Firefox address-bar combo wrappers can expose an input tag, Text, and EDITABLE together. Those signals alone do not identify the Chromium ARIA/datalist editor from #582. Scope that exception to Chromium and preserve Gecko wrapper, select, and paragraph refusals; verify both the Firefox live regression and hermetic tests with GI imports blocked.
- 2026-10-14 (#537): `_atspi()` was initialized once but still ran `gi.require_version` for every state/geometry probe; the newer masking probes multiplied typelib version enumeration during Calc snapshots. Cache only the successfully initialized binding, preserve `_inited` invalidation on D-Bus reconnect and missing-GI dependency isolation, and keep all native state/geometry/readback calls live. Profile the event-thread callback, not just the caller waiting in `submit`; use fresh LibreOffice and D-Bus processes for repeated loaded measurements.

- 2026-10-14 (PR #602 repair): Validate browser live failures with a running window manager before attributing them to ref matching. An extracted Openbox needs both its shared-library path and theme data; missing either can leave native window verification unavailable.
- 2026-10-14 (#638): Exact-target ranking must retain ordinary substring label matches, including trailing-colon form labels. #446 also asserts that a prose label mentioning an indexed row identifier alone must not stop off-screen cell discovery; preserve that narrower guard and test both find and scroll_to_find.
- 2026-10-14 (#642): Private GTK4 set_value verification must carry its boolean privacy marker through set_text caret restoration, LinuxDriver, and Runtime receipts; coercing it to bool loses redaction when preflight misses the entry. Empty-string snapshot values do not establish visibility.


- 2026-10-14 (PR #467 merge validation): Validate Geany symbol navigation and Scintilla visibility in the same fresh desktop session; caret status is shared evidence for both paths. Preserve both independently appended regression blocks when merging main.

- 2026-10-14 (PR #467 CI repair): A GTK status bar can have a generic accessible name such as Status and a distinct caret message in Text. Read that native message even for named bars, preserving the name and exposing the distinct message as its value without probing numeric Value.



- 2026-10-14 (PR #467 repair): Set DISPLAY and a unique writable XDG_CACHE_HOME before starting dbus-run-session, not only inside its child script. D-Bus-activated AT-SPI services inherit the session daemon environment and otherwise can bind the wrong display or abort on a read-only home cache. Validate Geany symbol jumps and Scintilla search together with CI-sized Openbox geometry.

- 2026-10-14 (PR #467 repair): A repository fetch refspec may omit cursor branches. Explicitly fetch the PR branch and compare its remote head before conflict repair so existing repair commits and regressions are retained.


- 2026-10-14 (PR #626 CI repair): GTK 4.14 routes GtkAccessibleText through a bridge that implements neither range nor character extents, unlike newer GTK. Keep buffer read-back separate from verified visibility in the search index; label unverified contents and never allow them to satisfy find. Reproduce version-dependent failures with the runner’s GTK and app libraries.
- 2026-10-14 (#618): GTK 4 tree-expander buttons expose namespaced listitem expansion actions. Prefer toggle-expand, otherwise choose expand/collapse from the live state, and verify EXPANDED changes before success. Do not fall back to pointer input after a failed native action.

- 2026-10-14 (PR #630 repair): Tree-expander regressions and Chromium combobox regressions append at the same location; preserve both complete blocks during a main merge and run the combined synthetic suite.

- 2026-10-14 (#646): Real Kate 25.04.3 bisect identified #562 (e3b272f) as the missing-status-controls regression. Skip generic Qt viewport-text reads before Kate’s existing buffer-visibility refusal; a discarded probe still disrupts later accessibility traversal. Preserve other Qt editors’ viewport filtering and Kate’s off-screen text refusal.

- 2026-10-14 (PR #608 merge repair): Recheck failed browser live cases after preserving newer main fixes before adding another workaround. The Chrome large-table search/click and Firefox web-field typing failures both pass after merging main; retain independent heading-control-label and web-combobox regression blocks together.
- 2026-10-14 (#643): Refuse writes to already observed secure fields before live ref resolution; GTK4 PasswordEntry may rebuild its inner accessible. Treat native password-text parent/child boundaries as secure before Text reads. Never reuse an old secure handle to perform an action; non-secure refs still need live resolution.

- 2026-10-14 (PR #644 repair): GTK4 secure-field refusal and unknown-visibility write redaction are complementary: preserve the early secure-ref refusal together with the private success marker reaching Runtime and audit. Keep both live regression blocks when merging these fixes.

- 2026-10-14 (PR #467 conflict repair): Globally blocking GI imports is useful for hermetic driver checks but invalidates binding tests that inject a fake GI module and CLI subprocess tests that initialize Linux. Recheck those files with native GI in a fresh Xvfb/D-Bus session before diagnosing their failures as regressions.

- 2026-10-14 (PR #467 validation): A supplied linked worktree can have read-only Git metadata and lesson files despite writable source files. Validate and commit in a writable temporary clone, explicitly fetch the PR branch, preserve its current remote history, and use a plain push; do not replace the mounted .git link or force-push.
- 2026-10-14 (PR #630 conflict repair): Preserve Chromium heading control-label regressions alongside tree-expander tests when merging appended blocks. The failed Linux run contained only the tracked Calc timing flake; inspect the complete job summary before changing unrelated action code.
- 2026-10-14 (#647): Suppressing Kate whole-buffer Text values exposes automatic scroll-anchor fallbacks: retain the editor view ahead of sibling breadcrumb lists. Container selection cannot establish text visibility; refuse unverifiable editor text searches before wheel/Find input and preserve explicit container refs. Live v0.4.66..main bisect identifies #563 (c226b7b) as the interaction trigger, not a reason to restore whole-buffer values.

- 2026-10-14 (PR #648 repair): Editor scroll-anchor selection and secure-field pre-resolution refusals are independent server behaviors. Preserve both regression blocks when resolving appended test conflicts; the unreadable-editor refusal must still happen before wheel or Find input.

- 2026-10-14 (PR #649 repair): Kate viewport-probe avoidance preserves status traversal, while editor scroll-anchor selection independently preserves search refusal. Retain both documented behaviors when merging their adjacent backend notes.
- 2026-10-16 (#595): XTEST wheel taps need both per-notch sync and real time between taps; sleeping while events remain queued still delivers a coalescible burst to Qt Quick. Wheel line units describe input notches, not verified pixels or rows.

- 2026-10-14 (PR #467 status repair): Keep GTK status message reads independent of a positive Text character count. When the count is empty or unavailable, request the native Text endpoint (-1) and retain only actual returned text; do not infer a caret line from symbol names or document geometry. Validate the empty-count case hermetically and both Geany navigation paths live.

- 2026-10-16 (#467 CI repair): Geany 2.0's startup welcome message masks caret statistics even after its symbol tree/editor is accessible. Statistics refresh on editor input, so initialize the fixture caret with Ctrl+Home and require the real initial line status within the existing startup deadline before testing navigation. Stop setup input once ready and preserve all final caret/visibility assertions; Text probes and geometry refreshes cannot reveal statistics the application is not displaying yet.



- 2026-10-14 (PR #626 merge repair): Preserve GTK buffer search exclusion and Kate owner detection together when merging adjacent snapshot rules; they protect independent toolkit visibility paths.

- 2026-10-16 (PR #626 merge repair): GTK 4 verified text visibility and GTK 4 tree-expander click verification are independent behaviors; preserve both backend notes when their adjacent documentation conflicts.
- 2026-10-16 (PR #655 repair): Require the AT-SPI Value interface before meter numeric reads even when a missing interface returns a plausible stale number. Preserve Qt Quick slider/spin unknown-bound handling independently of that interface gate, and retain both regression blocks when merging appended tests.
- 2026-10-16 (PR #659 repair): Qt Quick combo selection and numeric Value-interface support are independent behaviors. Preserve both backend documentation blocks when resolving adjacent additions; verify combo regressions alongside numeric Qt tests after merging main.

- 2026-10-16 (#596): Qt Quick MenuBarItem exposes a named nested menu-bar role with zero actions; its popup is a sibling in the same window. Recognize that shape, use only measured native bounds for pointer opening, and verify popup visibility changed before walking rows. The bar label's SHOWING state alone does not establish an open popup.
- 2026-10-16 (#603): Claws Mail folder and message lists share an empty panel-under-scroll-pane shape. Use a combined surface label and retain the missing-row limitation; pane dimensions or focusability do not establish list identity. Scope recognition to Claws split-pane windows and preserve native labels.

- 2026-10-16 (#672): HTML submit ownership must honor explicit `form` attributes before ancestry and keep backend ids isolated by CDP session/document. Native DEFAULT and DOM submit markers are discovery hints only; preserve lexical exact-target behavior for unrelated queries and never turn markers into action substitution.

- 2026-10-16 (PR #467 merge validation): An inherited DISPLAY can opt the full suite into unrelated desktop fixtures and credential prompts. Run the hermetic suite with DISPLAY unset, then validate Geany symbol navigation and Scintilla search together in a fresh Xvfb/Openbox/D-Bus session; preserve both native-status readiness and meter Value-interface regressions when merging their adjacent test additions.


- 2026-10-16 (#598 merge repair): Preserve asynchronous Chromium PDF reveal regressions alongside XTEST wheel-pacing regressions when both append to the Linux synthetic suite. A document-text hit has no table row index; retain its actual handle without click bounds while native extents settle.
- 2026-10-16 (#671): Exec-off terminal refusals must be final and direct planners to GUI routes; permission wording alone can invite requests to enable exec. Keep this guidance conditional on exec being off and preserve the terminal gate and audit.



- 2026-10-16 (PR #467/#626 merge repair): Scintilla match visibility and GTK 4 visible-buffer snapshot filtering are complementary. Preserve both helper blocks when their insertion points conflict, and verify Geany symbol navigation together with Scintilla search under the CI window manager before adding a workaround for an older failed run.
- 2026-10-16 (PR #677 repair): Claws Mail wizard page titles and unnamed entry row labels are independent naming fallbacks. Preserve both regression blocks when merging their appended tests; entry labels must retain native handles and measured bounds.


- 2026-10-16 (#609): Chrome's accessible hit test can return the underlying control through a top-layer HTML popover. Use showing `ispopup` panels and measured bounds to identify covers, and keep the mapped `AXPopover` in the container no-value set to avoid numeric interface reads.

- 2026-10-16 (PR #679 repair): HTML popover mapping and Claws Mail wizard tab naming have independent appended regressions. Preserve both complete test blocks when merging main, including native unknown-geometry assertions for hidden wizard tabs.



- 2026-10-16 (PR #676 repair): Optional AT-SPI observation hints must tolerate missing GI bindings as well as missing native state members. Exercise the fallback with an ImportError even on Linux so synthetic backend tests remain portable to macOS and Windows.

- 2026-10-16 (PR #467 Geany verification): XSync confirms X server delivery, not completion of symbol navigation. After the requested double-click, poll Geany's native status line on separate AT-SPI operations; status may be exposed through Name or Text. Keep waits on the caller, preserve both pointer clicks, and return an unconfirmed error if the reported destination never appears.


- 2026-10-16 (#682): Thunar toolbar path entries can echo EditableText while leaving the navigation model stale. Retype through confirmed focus even when the buffer already equals the requested path; keep Return separate and verify navigation from location/title.
- 2026-10-16 (PR #667 repair): Preserve Chromium PDF asynchronous-reveal notes and native DEFAULT submit-discovery notes together when adjacent backend documentation conflicts; they describe independent behaviors. Inspect the direct Actions job log when the failed-log command returns no output.

- 2026-10-16 (PR #677 conflict repair): GTK sibling-row entry naming and native DEFAULT submit discovery are independent observation hints. Preserve both documentation blocks and test their combined synthetic behavior after merging main.


- 2026-10-16 (#672 reopened): AT-SPI exposes StateType.IS_DEFAULT (nick is-default), not DEFAULT. Native StateSet has contains() but no names attribute; test that shape so synthetic fallback names cannot hide an incorrect enum lookup.
- 2026-10-16 (#599): Chromium PDF thumbnail accessibility actions can return True without navigating. Ref clicks require a page-navigation verifier before success; until available, refuse with a coordinate-click hint rather than trust do_press, guess a visible point, or substitute another action.

- 2026-10-16 (PR #685 repair): Thunar toolbar path replacement and Chromium PDF thumbnail refusal protect independent action paths. Preserve both backend notes and regression blocks when merging main; neither requires changing the other action.
- 2026-10-16 (PR #667 main merge): Keep asynchronous PDF find reveal tests alongside PDF thumbnail click refusals; finding a native text handle and verifying page navigation are independent guarantees.

- 2026-10-16 (PR #667 default-state merge): Preserve the native IS_DEFAULT StateSet regression alongside asynchronous PDF reveal behavior; synthetic state-name fallbacks must not hide native enum mistakes.

- 2026-10-16 (PR #677 merge): Claws Mail opaque list labels and wizard entry row labels are independent fallbacks. Preserve both complete regression blocks when merging their adjacent additions.


- 2026-10-16 (PR #679 merge repair): Preserve Claws Mail opaque-list labels and HTML popover coverage as independent behaviors when merging adjacent documentation and synthetic regressions. Inspect the complete Linux failure summary before changing code for the tracked Calc timing flake.

- 2026-10-16 (test isolation): Concurrent D-Bus test sessions need distinct writable XDG_CACHE_HOME directories; sharing the AT-SPI socket path can close a remote-runtime subprocess connection when the other session exits.

- 2026-10-16 (#690): Thunar location-entry error icons change the accessibility tree without opening a folder. Inspect the post-Return entry/window before accepting generic tree-change confirmation; unchanged titles alone must not be described as observed error icons. Keep this outcome check independent of #685's path replacement, and preserve KP_Enter's native keypad keysym.

- 2026-10-16 (PR #467 repair): A Geany navigation or show-desktop CI failure can disappear in a clean desktop session. Reproduce both failed cases alongside Scintilla search under isolated D-Bus, Xvfb, and Openbox before changing pointer behavior or verification bounds.

- 2026-10-16 (PR #467 conflict repair): Independently appended Geany navigation, GTK icon selection, and Qt viewport regressions must all survive conflict resolution. Reproduce older failed checks at the current merged head: Geany navigation and Scintilla visibility, and terminal-grant cleanup followed by the short-budget agent test, pass in fresh isolated desktop sessions.


- 2026-10-16 (PR #685 merge): Thunar path replacement and failed-navigation verification are complementary. Preserve focused replacement with a separate Return, and check navigation errors before accepting a changed accessibility tree as success.
- 2026-10-16 (PR #685 live verification): Thunar can expose dialog-error-symbolic before and after successful navigation. A persistent icon alone is not a new failure; retain unchanged-window refusal and newly appearing error detection, and verify successful navigation with the live location and title.
- 2026-10-16 (PR #667 validation): Run the full suite through `python -m pytest`: invoking pytest from stdin breaks multiprocessing spawn because `<stdin>` is not an importable file. Limit forced missing-GI checks to hermetic backend tests; Linux CLI, menu, and live checks can require the installed native binding even without a display.

- 2026-10-16 (PR #677 CI review): Check failed-run job summaries against the current head before changing platform code; the older run failed only the tracked Calc timing test while Windows, macOS, Browser, and Package passed.

- 2026-10-16 (PR #677 main merge): Preserve HTML popover coverage alongside GTK sibling-row entry naming when resolving appended tests and backend notes; these observation hints are independent.
- 2026-10-16 (#610): Chromium HTML popovers expose panel + ispopup and DETAILS_FOR opener relations. Preserve their container boundary before descendant ref matching; dismissed descendants can otherwise receive reorder hints naming unrelated browser chrome. Snapshot absence alone does not prove how a popover was dismissed.

- 2026-10-16 (PR #692 repair): Preserve SHOWING-gated HTML popover mapping when adding opener metadata and stale descendant guidance; hidden panels must retain ordinary group behavior so containment checks cannot treat a dismissed popup as open.

- 2026-10-16 (#610 repair): Runtime click verification can pass a target-only snapshot to rematch_ref. Ancestor inspection must tolerate omitted parents while retaining containment checks for ancestors present in a full snapshot.

- 2026-10-16 (PR #467 caret verification): Geany's status label is asynchronous. Verify symbol navigation through either the reported status line or the focused editor's native caret offset and exact text prefix; never use symbol selection, an unfocused buffer, or an incomplete text read as proof. Keep the pointer sequence and existing polling bounds unchanged.




- 2026-10-16 (PR #685 CI repair): A failed short-budget agent test can stop in show-desktop setup before the agent runs. Reproduce both the test alone and after the preceding terminal-launch test under a real window manager before attributing that focus failure to unrelated text replacement code.
- 2026-10-16 (#684): Qt text recovery must handle malformed QSpiTextRangeList replies and failures in the fallback Text read itself. Retry getText(0,-1) only for a native character count within the existing cap, then capped chunks; retain native character visibility checks and refuse incomplete reads.

- 2026-10-16 (PR #701 repair): Preserve GTK numeric-control label regressions and Qt bounded-range recovery regressions as independent complete blocks when merging appended tests. Numeric labels must precede changing slider descriptions while native Value reads remain unchanged.


- 2026-10-16 (PR #696 repair): Keep VLC's bounded registration retry and VLC-specific Qt setup hint alongside the generic GTK2 bridge prerequisites when merging missing-tree diagnostics. Preserve both the VLC registration regressions and main's Qt text-recovery regressions.
- 2026-10-16 (#640): Escape multiline accessible names only when rendering; retain native titles for find/ref matching. Shared `_render_line` serves full snapshots, interactive element lines, and find results.

- 2026-10-16 (PR #467 validation): A worktree can have read-only shared Git metadata and protected contributor files even when ordinary source files are writable. Preserve the remote PR head in a writable temporary checkout before merging; do not leave a partially updated original worktree after a checkout fails on a protected file.
- 2026-10-16 (PR #715 merge repair): Preserve stale top-level application-child cache regressions alongside table-band boundary regressions when both append to the Linux synthetic suite; window discovery and complete table-cell publication are independent behaviors.


- 2026-10-16 (PR #711 repair): Calc viewport bands use the table role to retain sheet pruning limits, while ordinary table-cell bands use groups to avoid orphaned final cells. Preserve both representations and their regressions when merging adjacent band changes; label only the Calc synthetic bands.
- 2026-10-16 (#651): Eye of GNOME GTK zoom entries can echo EditableText percentages without changing image scale. Buffer equality cannot verify applied zoom; refuse before mutation rather than send an unrequested activation. Preserve the independent header-bar menu guidance when merging adjacent backend notes.
- 2026-10-16 (PR #701 main merge): Retain GTK numeric label tests alongside VLC registration tests and same-app cover documentation when adjacent additions conflict; they protect independent observation behaviors.
- 2026-10-16 (visible actions): Keep X11 rendering and expiry on a daemon-owned connection and reuse one empty-input-shaped window. Keep native AT-SPI reads on their owner thread, never put typed text in feedback labels, and hold captures until unmap. Disable cosmetic feedback globally in tests/CI so existing timing budgets remain unchanged.
- 2026-10-17 (PR #719): Ref typing uses ``type_into``, not ``type_text``. Announce the Type overlay on both paths; never put typed text in the label.

- 2026-10-17 (PR #467 repair): When a Geany symbol double-click is unconfirmed, preserve main’s XTEST inter-click flush and pacing fix (#721) before adding caret/status workarounds. Validate symbol navigation and Scintilla viewport search together under a fresh Xvfb/Openbox/D-Bus session; XSync delivery alone does not verify navigation.
- 2026-10-17 (PR #467 merge regression): Patching linux.time.sleep also patches the shared time module used by XTEST. Geany navigation tests must require both main’s 30 ms inter-click gap and the existing navigation polling pauses, including the refusal path.
- 2026-10-17 (PR #720 repair): Native popover-refresh gating and per-snapshot sibling-window cover caching address independent tree and window-scan costs. Preserve both complete regression blocks when merging their appended action-outcome tests.

- 2026-10-17 (PR #720 Calc CI investigation): A live Calc failure in the initial LinuxDriver.snapshot loop occurs before Runtime creation, so Runtime popover-refresh gating cannot cause it. Check the failing phase before changing input code. The exact failed head 8eb1b60 passed test_linux_calc_range_formula_and_normalised_numbers_confirm alone in 164.23 s under private 1280x800 Xvfb, D-Bus, AT-SPI and Openbox; retain the unchanged startup assertions and limits.

- 2026-10-17 (#652): Okular TOC QTreeView cells expose selection-only Toggle without CHECKABLE. Highlighting a row does not verify page navigation; refuse ref activation before mutation until navigation can be verified, and keep grid-cell and other tree-item handling separate.
- 2026-10-17 (#697): Folder creation can trigger file-output classification through paths and moved filenames. Remove the folder-create phrase before detecting independent file writes; verify directories with dir_exists while retaining regular-file/content/freshness checks for actual file output.

- 2026-10-17 (PR #467 diagnosis): A passing isolated Geany run does not explain a repeated CI navigation refusal. Measure the whole Runtime double-click under CI’s Xvfb/Openbox session, assert the five-second bound in the live regression, and retain the native navigation assertions. Keep each parallel D-Bus session’s XDG_CACHE_HOME unique; an extracted Openbox also needs its theme directory in XDG_DATA_DIRS.
- 2026-10-17 (#665): Calc type read-back prefers the transient cell editor over focused text. Probe that editor first to avoid a discarded focus-tree traversal; preserve the focused-text fallback for empty/missing editors and keep every read live across input. Standalone native call counts are instrumentation evidence, not proof of the loaded five-second CI bound.

- 2026-10-17 (PR #720 Chrome CI investigation): The large-table top-of-page wait uses LinuxDriver.snapshot before Runtime is created. A failure there cannot exercise Runtime popover-refresh gating; preserve the test and distinguish browser setup flakes from click-guard regressions.

- 2026-10-17 (PR #467 main merge): Retain Scintilla visibility checks alongside main's native-click popover-refresh gate. The previously failing Geany symbol double-click and both document-visibility live regressions pass together after the merge under private Xvfb/Openbox/D-Bus; this local pass does not establish the cause of the earlier CI refusal.
- 2026-10-17 (#670): PCManFM Icon View can expose no file children at all. Label only the childless folder panel under the native folder-content scroll pane; keep Detailed List View rows native and never infer icons from status counts.
- 2026-10-17 (PR #735 repair): Preserve Kate character-viewport scroll-search regressions and GTK entry relation-label regressions together when merging appended tests; editor visibility and field naming are independent behaviors.

- 2026-10-17 (PR #729 repair): Qt menu cleanup can need one Escape to close the popup and another to clear the active menubar action. QMenuBar may retain widget FOCUSED after tracking ends; for Qt Widgets, inspect FOCUSED on its menu-item children (activeAction), while retaining the bar-focus check for other shapes/toolkits. Verify both phases and recheck showing dialogs before another Escape; the live checkable-menu fixture catches this distinction.

- 2026-10-17 (PR #467 merge validation): Keep Scintilla buffer visibility filtering alongside Kate's native-character viewport search and target-bound popover evidence when merging main. The Geany symbol-click, Scintilla reveal, and GTK TextView reveal regressions passed together in five consecutive private Xvfb/Openbox/D-Bus runs; those passes do not establish the cause of the earlier CI navigation refusal.
- 2026-10-17 (PR #738 repair): Preserve Calc editor-first read-back regressions alongside PCManFM opaque-view and Qt viewport regressions when appended tests conflict. The failed Linux job contained only the tracked Calc five-second timing flake; retain all action verification and timing bounds.


- 2026-10-17 (PR #746 repair): Preserve Scribus embedded Properties-cell hints alongside PCManFM opaque-folder labels and Qt viewport regressions when merging appended tests; each describes a separate native accessibility limitation. Keep both the Scribus guidance and Qt menu-cleanup documentation.

- 2026-10-17 (#697 reopened): Same-filesystem moves/renames preserve mtime. POSIX ctime can establish metadata freshness only for relocation-only goals; Windows ctime is creation time. Keep save/content checks on mtime and exclude the write-clock slack from relocation checks so a recently pre-existing untouched file cannot pass.

- 2026-10-17 (#686 reopened): libatspi Text.get_character_count returns 0 even on Scribus cells advertising no Text interface; use the advertised interfaces for opaque-widget detection. Test real leading column headers and item-widget overhangs. Discovery cover probes need the owning-window intersection, while action probes retain the actual input point. Failed find results must retain all distinct content notes so a document note cannot hide a palette limitation.
- 2026-10-17 (#687): Scribus New Document contains an unnamed Qt `frame` under its real `dialog`. Treating that child as the native window makes overlap matching select the blocked main window and exhaust the five-second activation wait before an otherwise immediate Value write. Profile window preparation as well as text helpers; preserve the dialog's native identity, numeric read-back, and Qt viewport/masking checks.

- 2026-10-17 (PR #776 repair): Page-relative Chromium depth tests and Scribus palette visibility tests are independent appended regressions; preserve both when merging main. Chromium chrome must not spend the document budget or an iframe restart, and buried-control fixtures must remain at the page-relative cap.
- 2026-10-17 (#755): Agent step_log ordering and length control execution budgets and replay. Keep per-skipped-call records separate, and never use _commit for them: it captures screenshots and advances progress counters.
- 2026-10-17 (PR #768 repair): Relocation freshness and multi-call screen-change guards are independent agent guarantees. Preserve both documentation blocks when merging them, and run the file-evidence and fingerprint regressions together.

- 2026-10-17 (PR #467 repair): Bounded Geany navigation evidence walks must visit shallow status/editor nodes before large menu subtrees. Depth-first traversal can exhaust the unchanged node budget without reaching either native status or caret evidence; use breadth-first traversal and retain the focused-editor and exact-line checks.
- 2026-10-17 (#775): Calc's driver already carries a one-use, app-bound typed read-back. Judge it before a full-window traversal; after targeted verification, the old whole-tree baseline must not be reused as current state. Preserve process-liveness evidence separately. A selected-cell address change can verify a key; unchanged or missing addresses still need the full-window fallback.
- 2026-10-17 (PR #771 repair): Screen-change fingerprints and per-call statuses must reach the same executed record. Skipped trace rows are separate JSONL records with no fingerprint; assert both rows while retaining all fingerprint, no-extra-observation, and execution-budget checks.

- 2026-10-17 (PR #467 repair): Geany status Text and its accessible name can disagree. Prefer the native Text message when present, falling back to the name only when Text is empty; concatenating them lets an older line number override live evidence and can cause either false refusal or false confirmation. Preserve focused-caret fallback and the explicit double-click.

- 2026-10-17 (#594 reopened): Qt Quick ComboBox popups can appear as sibling AT-SPI dialogs, not menus. Bounded readiness polling must refresh the popup container itself, not only its parent, to observe cached SHOWING state and newly published rows. Retain event-thread-free waits and value-plus-popup-close verification.

- 2026-10-17 (PR #467 repair): A restricted origin fetch refspec can leave a Cursor PR's tracking ref stale even after `git fetch origin`. Compare it with GitHub's headRefOid and fetch that branch explicitly before merging or interpreting CI logs. Geany symbol navigation and Scintilla search passed together after merging current main in a private 1280x800 Xvfb/D-Bus session with xfwm4; this does not establish the loaded Ubuntu CI result.
- 2026-10-17 (#604): Claws Mail wizard page headers can start with filler → filler → icon, with the title in a later sibling. Recognize the native dialog as well as frame ancestry, and skip decorative icons along the leading layout path, but stop at controls so field captions cannot become page names; preserve hidden tabs without geometry or actions.

- 2026-10-17 (PR #467 repair): Breadth-first traversal alone does not protect nested Geany caret/status evidence from large shallow menus. Exclude menu subtrees from navigation-only evidence walks while retaining the existing node budget, focused-caret checks, and explicit pointer double-click.


- 2026-10-17 (#796): Chromium CSS overflow clipping can leave a VISIBLE child with on-screen bounds under a SHOWING region/document while the child lacks SHOWING. Filter the child itself and recheck old refs before both pointer and optional CDP activation; document visibility alone does not establish target visibility.



- 2026-10-17 (PR #803 repair): Chromium also clears SHOWING on scrolled-off rows and containers with painted descendants. Keep traversing containers and preserve native off-screen geometry handling before classifying CSS-clipped leaves, including NOT_VISIBLE for old scrolled-off refs. Run the live infobox-expand, find-bar-close, and off-screen-crop regressions together.

- 2026-10-17 (#812): XTEST Unicode runs must flush and retain the existing client-translation wait before restoring or reusing spare keycodes. Keep baseline key resolution separate from run bindings, and preflight zero capacity before any ASCII prefix.

- 2026-10-17 (PR #814 repair): Preserve Chromium visibility regressions alongside Unicode keymap-run regressions when their appended tests conflict. A Firefox page-setup failure before input is not evidence of an XTEST regression; inspect the actual startup dialog and browser version before changing typing code.

- 2026-10-17 (#813): Calc click verification uses the active window root. Format Cells replaces that root with a dialog, so spreadsheet_open=False must not silently delegate to generic tree-change confirmation. Return an explicit partial verdict naming the dialog when the cell selection cannot be verified.

- 2026-10-17 (PR #819 repair): A synthetic Linux pointer driver must keep resolves_apps=False to exercise native point guards. Stub the host _running_app lookup separately; otherwise macOS rejects the fake app during the initial snapshot before any occlusion assertion runs.

- 2026-10-18 (#800): Chromium timer-driven transform changes can retain both old extents and SHOWING, so #796's visibility guard alone cannot protect ref pointer clicks. Use document hit-test identity (including text descendants and fresh native proxies), refuse unavailable hits, and keep this separate from explicit coordinate input. Synthetic hits do not establish live Chromium hit-test freshness.

- 2026-10-18 (PR #826 repair): Preserve Chromium ref hit-testing immediately before pointer input alongside Geany destination capture and post-click navigation verification. Retain both Geany evidence-walk and Unicode keymap regressions when their appended tests conflict. The Geany symbol, Scintilla reveal, and GTK TextView reveal live tests passed together after this merge under private Xvfb/Openbox/D-Bus; this does not establish the cause of the earlier CI navigation refusal.

- 2026-10-18 (PR #826 investigation): An isolated Geany navigation pass, including Ubuntu's Geany/GTK builds, does not explain a repeated loaded-CI refusal. Preserve the native line-verification gate and record only numeric status/caret evidence and requested/sent points on refusal; never expose document/status contents in diagnostics. Background LibreOffice geometry can affect the generic pointer-adjustment path, so compare actual input coordinates as well as navigation evidence.

- 2026-10-18 (PR #826 native-pointer repair): `_libreoffice_frame_at` searches background frames too. Before applying its title-bar delta to a ref click, check the ref's owning application; otherwise Geany can receive a click on the next symbol, and noting that click incorrectly marks the background Office frame aligned. A private live two-symbol Geany experiment with a background Writer and the LibreOffice 24 version branch selected navigated to line 304 instead of 301 on both the PR and main; preserving Geany's native coordinates fixed it. Keep Office's own correction and the double-click/navigation verification intact.

- 2026-10-18 (#800 reopened): Chromium Component hit tests share stale transform-only geometry with extents and SHOWING; a matching native hit is not freshness evidence. Use exact node-bound click/press for Runtime page-button clicks, report doAction honestly, and never fall back after an absent/failed action. Live Chrome 155 reports native role `button` (not only `push button`); regression fixtures must cover both.

- 2026-10-18 (PR #835 repair): AXButton includes Chromium native toggle buttons, whose advertised action need not be click/press. Scope the node-bound push-button route by native button/push button roles and retain the existing click path for toggles; never substitute toggle for an explicit click. Cover the distinction with synthetic routing tests and the unchanged live form-state test.

- 2026-10-18 (PR #826 main merge): Preserve both Chromium node-bound button routing and Scintilla visibility filtering when merging #835/#837. The failed Geany CI run already included the owning-app pointer fix and reported identical requested/sent coordinates; do not attribute that refusal to the older Office coordinate bug. Geany navigation passed on main and the merged PR in private desktops, including the PR checks with the Ubuntu Geany build; these passes do not establish the loaded-CI failure cause.


- 2026-10-18 (PR #834 repair): Chromium tab-write preflight and node-bound button clicks protect independent input paths. Preserve the tab refusal before any insertion or replacement mutation alongside main’s updated click mechanism and freshness limitations when merging backend documentation.

- 2026-10-18 (PR #826 merge retry): When combining document reveal with text-range targets, refresh the snapshot after reveal, attach range targets to the surviving matches, and record issued refs from that final snapshot. Keep both visibility refusals and range-target regressions.

- 2026-10-18 (PR #826 range integration): Keep observed widget geometry in the current snapshot when publishing native text-range matches. Render the measured range and retain its offsets for act-time revalidation; replacing widget bounds breaks document-visibility checks after reveal. The unchanged GTK TextView and both Geany live regressions passed together under Xvfb/Openbox/D-Bus.

- 2026-10-18 (PR #826 clipped outline repair): GTK text-cell bounds can extend beyond the Symbols scroll pane into Geany’s editor. Identical requested/sent points do not establish that the point belongs to the row. A long-symbol live regression reproduced navigation to editor line 5; intersecting the reported cell, tree, and viewport rectangles made the unchanged double-click and line-301 verification pass. Preserve both pointer clicks and the native destination check.


- 2026-10-18 (#842): Impress/Draw EditableText multiline replacement splits a paragraph while its frame child list can stay stale. Refuse LF/CR replacement before writing; LinuxDriver captures/restores caret separately from set_text, so preflight both entry points before that state handling.

- 2026-10-18 (#864): Writer select-all from an empty focused paragraph can change only sibling Text selection ranges. Compare native sibling ranges for selection chords; an unchanged empty paragraph is insufficient evidence of a no-op. Reject incomplete or oversized sibling reads rather than confirming from a truncated sample.
- 2026-10-18 (PR #826 merge repair): Keep Geany outline viewport regressions and Writer exact-paragraph replacement regressions together when appended tests conflict; they protect independent native geometry and text-verification behavior.
- 2026-10-18 (#850): Chrome ref-click cost includes a menu-bar discovery traversal as well as resolve/outcome snapshots. A live Collection query can prove menu-bar absence without walking the page; hits and failures must retain active-frame selection. Scoped button name evidence must be bound to the exact sent node action, consumed once, and invalidate the old full-tree baseline. The fifteen 20 ms sleeps are process-exit verification, not input settlement; preserve that grace.

- 2026-10-18 (PR #826 safety/performance merge): Retain issuing-snapshot menu ancestry and exact-target click evidence alongside Geany navigation verification when merging shared Runtime click changes; validate the menu gate, Chromium button, and Scintilla regressions together.

- 2026-10-18 (PR #866 repair): Scoped Writer key outcomes must retain Writer-specific evidence when sharing probes with Impress. PR #870 failed the evidence-label assertion after all four outcomes were confirmed; its generic LibreOffice label caused the failure, not missing caret confirmation. Cover the label in the hermetic Runtime regression as well as the live timing test. An isolated Calc grid pass does not explain a loaded-CI A1 read-back mismatch.
- 2026-10-18 (#873): Scintilla caret movement is an input mutation even when used to reveal text. Plain find may use Text.scroll_substring_to but must never fall back to set_caret_offset; reserve that fallback for explicit scroll_to_find and verify native extents afterwards.

- 2026-10-18 (PR #870 repair): Extending a scoped Writer key probe to Impress/Draw must preserve the Writer identity in its evidence text. The live timing test checks confirmed outcomes and then that identity separately; its string-list assertion does not imply input or confirmation failed. Cover the evidence contract in hermetic outcome tests too.

- 2026-10-18 (PR #866 merge): Writer sibling-selection evidence and Impress frame metadata both extend key states to eight items. Tuple length alone cannot select the Writer path; retain a native Writer-document check so Ctrl+A and Shift navigation preserve Impress metadata evidence for empty and non-empty paragraphs. Cover both selection chords when merging these probes.

- 2026-10-18 (PR #866 Geany merge): Writer sibling-selection probes and Geany reveal behavior are independent. Preserve plain find’s non-mutating scroll path and explicit scroll_to_find’s caret fallback while retaining Writer’s native document guard and complete sibling-range evidence.
- 2026-10-18 (#868): LibreOffice Math can leave FOCUSED on an Elements icon and command panel while keys reach the focused editable paragraph. Read-back must disambiguate within the active window and capture the paragraph selection before input: replacing `<?>` shortens the formula, so insertion/length checks alone still report a false mismatch. Keep exact replacement verification and return unverified for ambiguous or incomplete discovery.

- 2026-10-18 (PR #876 repair): The cuamenudlg missing-tree CI failure occurred during startup before either menu action. The unchanged test passed with its neighboring dialog test and in ten repeated private Xvfb/Openbox/D-Bus runs. Isolated passes do not establish the loaded-CI cause; preserve the live assertions and distinguish registration failures from Math typing verification.

- 2026-10-18 (PR #876 validation): Concurrent private D-Bus sessions must use separate XDG_CACHE_HOME directories: their AT-SPI launchers otherwise share the same socket path. A full-suite remote-runtime subprocess lost that socket after the other session ended; all six remote tests passed with a separate cache.

- 2026-10-18 (#878): Scintilla EditableText insertion can leave the caret at the insertion point even when exact buffer read-back succeeds. Advance only that stationary caret after verified insertion, using GTK code-point or Qt UTF-16 offsets, and verify the move. Preserve Draw/Impress's separate paragraph append behavior; a caret failure after insertion must not trigger duplicate keystrokes.

- 2026-10-18 (PR #866 Math merge): Preserve Math focused-editable discovery and exact replacement read-back alongside Writer sibling-selection probes. The GTK icon-view CI failure was an interpreter-name permission refusal before click delivery, outside these paths; an initial local refusal followed by five unchanged PR passes and twelve main passes does not establish its intermittent identity-resolution cause. Keep the permission gate and live assertions intact.

- 2026-10-18 (#886): Qt menu actions opening modal dialogs can return False after taking effect. Before coordinate fallback, re-resolve and require native identity, name/role, SHOWING and an exact hit; a generic tree change after a second input can otherwise falsely confirm a click on a different dialog control. Qt popup hits require the popup's Component: desktop/frame hit tests may return nothing or the underlying editor even for a valid visible menu item.

- 2026-10-18 (PR #889 repair): A library refusal after a declined native press must also stop the agent's same-step recovery ladder, including when raised by a recovery attempt. Otherwise vision/coordinate recovery can click the stale location and report verified despite carrying the original error. A new visible dialog can be reported as dispatch evidence only after complete before/after native identity reads; keep that receipt distinct from confirmed effect and bypass the AT-SPI application child cache for both reads.

- 2026-10-18 (PR #889 CI): A false native press result does not itself prove dispatch: Firefox checkboxes and GTK4 rows can have no eligible native press action. Record dispatch immediately before DoAction and bind a no-dispatch receipt to the exact target, consuming it once. Keep all stale-target checks after an actual attempted action; retain ordinary click guards when nothing was sent.

- 2026-10-18 (PR #883 repair): Preserve both appended lessons when merging the Scintilla caret fix with Writer selection changes. An Impress failure while discovering its initial text frame occurs before typing and does not exercise the stationary-caret correction; do not classify that startup failure as a typing regression without further evidence. The unchanged Impress multiline test passed on both main and the merged PR under private Xvfb/Openbox/D-Bus; isolated passes do not establish the loaded-CI startup cause.

- 2026-10-18 (PR #877 repair): Math ref editing and keystroke verification are complementary. Preserve active-window discovery for read-back when combining them: a panel-local paragraph lookup misses stale Elements icon focus and ambiguity elsewhere in the window. Synthetic command panels must expose their native application identity as well as the paragraph.
- 2026-10-18 (PR #877 repair): Preserve Math command-editing and single-use outcome-readback regressions together when appended tests conflict. Impress frame discovery uses native find_root/child/extents reads before any snapshot or typing, so a missing PresentationSubtitle at that stage does not exercise the Math editing patch. The unchanged live test passed on main and the merged PR under private Xvfb/Openbox/D-Bus; these isolated passes do not establish the loaded-CI startup failure cause.

- 2026-10-18 (#874): Scoped Chromium toggle verification must distinguish an unreadable StateSet from unchecked; general false-default state helpers can falsely confirm an uncheck after a bus failure. Keep pre/post reads on the same native handle, waits outside the AT-SPI thread, and invalidate the full-tree baseline after a verified transition. Only that transition bypasses process-exit grace; unchanged/unreadable toggles retain the full outcome path.
- 2026-10-18 (#874 coordinate follow-up): AT-SPI registry/application roots may have no Component interface. Coordinate toggle regressions must exercise the real frame hit-test entry point, not stub `_accessible_at_point`; select a showing frame of the native point-owner PID and retain full verification when frame selection is unavailable or ambiguous.

- 2026-10-18 (#900): Chrome beforeunload uses harmless Leave/Reload labels for discarding edits. Pair the exact button label with its enclosing Leave site?/Reload site? title; keep ordinary page controls and Cancel ungated. Cover declined and unavailable confirmation channels through MCP and assert no click plus an audit refusal.
- 2026-10-18 (PR #877 main merge): Math command editing and Scintilla stationary-caret correction share insert_text but retain separate eligibility checks. Preserve both when merging; the unchanged Impress frame multiline regression passed under private Xvfb/Openbox/D-Bus after this merge.

- 2026-10-18 (#900/#901 repair): Browser permission and beforeunload decisions need the dispatch-time tree, including coordinate targets and focused/default Enter buttons. Share the dialog predicate between Runtime and the agent; recheck after human approval, preserve the original input, and keep Block/Never allow/Cancel/Escape/page Allow controls ungated. Synthetic bench cases must avoid the separate duplicate-prompt stale-ref bug (#899).

- 2026-10-18 (PR #905 second repair): Browser dialog dispatch checks must bind risk to the target's ancestry, not a matching label elsewhere in the window. On Linux, read the resolved handle's parents for refs and skip web-document subtrees for key/coordinate probes; regression tests should fail on full-window rereads. Cover access keys as well as Return/Space, and preserve Escape/Tab and negative buttons through both Runtime and the agent harness.


- 2026-10-18 (PR #889 status): New-dialog dispatch receipts need an explicit dispatched mapping in agent step records and events; otherwise an unverified receipt falls through to no_op and invites retries. Preserve the dialog name and empty next hints across library, MCP, feedback, and trace surfaces.

- 2026-10-18 (PR #877 main merge): Math command editing and Scintilla stationary-caret correction share insert_text but retain separate eligibility checks. Preserve both when merging; the unchanged Impress frame multiline regression passed under private Xvfb/Openbox/D-Bus after this merge.

- 2026-10-18 (PR #903 repair): Chromium coordinate-owner hit testing and LibreOffice Math command editing are independent AT-SPI paths. Preserve both on merge and run the toggle and Linux synthetic regressions together; append-only lesson conflicts must retain both branches’ entries.
- 2026-10-18 (PR #889 Math merge): Preserve Math command-editing documentation and regressions alongside declined-press fallback safety and dispatched-dialog reporting. The failed Chrome large-table CI check reported no initial rows before scrolling or clicking; the unchanged four-pass live regression passed on the merged head under private Xvfb/Openbox/D-Bus. This isolated pass does not establish the loaded-CI failure cause; the related Chromium cache investigation is tracked separately in #898 / PR #902.

- 2026-10-18 (PR #905 speed repair): A bounded Chrome toolbar walk still adds hundreds of milliseconds when no dialog is open. Query native dialog/window roles first, retain the bounded fallback on query failure, and share each button StateSet within the probe. Reuse the decision only within the immediate action; human approval still requires a fresh read. Alt access keys require reported bindings or mnemonics, never the first letter of a label. Pin probe and node-read counts in regressions.
- 2026-10-18 (#867 reopened): LibreOffice Math 25.2 uses an em-dash frame title; grab_focus resets an already focused command caret to zero, and EditableText selection replacement can retain the old endpoint. Preserve confirmed focus and verify the post-write caret as well as text. Native parent links can omit the visible Commands container, so retain a generic command label instead of inheriting the window title.

- 2026-10-18 (PR #905 main merge): Browser dialog gating and shaded-window input refusal protect different dispatch conditions. Preserve the approved dialog probe unchanged when importing the window visibility guard, and run both gate and Linux synthetic regressions.
