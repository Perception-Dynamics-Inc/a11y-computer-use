# Lessons learned (append-only; newest at the bottom)

- The libatspi child cache goes stale when the AT-SPI event thread is on (`A11Y_COMPUTER_USE_ATSPI_EVENTS`); code that relies on fresh children (e.g. Calc grid editors, #475/#411) must not assume the cache is current.
- #489's Audacity track/clock labels were applied app-wide and leaked into the crash-recovery dialog (#505). Scope app-specific labels to the window/role they belong to.
- #512's same-app dialog occlusion refusal broke tests/test_server.py on every platform. Run test_server.py for any change that affects click results, not just Linux suites.
- #498 (Qt toggle click, #493) may have regressed `test_linux_qt_line_edit_combo_and_values` on main. Qt click changes must run the Qt live tests' synthetic equivalents and check line edits/combos.
- GTK comboboxes: options were capped at the first 64 children (#513, fixed in #516). Never cap option lists silently; look up the requested value among all children and say when an error list is truncated.
- The first #503 attempt (dropping GTK 4 nodes with garbage sizes) broke the huge Calc and combo popup tests. Large-but-valid geometry is legitimate; only filter clearly uninitialized sizes.
- 2026-10-13 (#480): Linux maps native AT-SPI terminals to AXTextArea; inspect native terminal ancestry, including remembered editable handles, rather than treating every multiline editor as a shell. Preserve cached-field ownership checks before terminal detection so stale focus still returns an audited focus_changed error. Execution permission is independent of per-app FULL grants and defaults off for MCP Runtime hosts as well as agents.
- 2026-10-13 (#537): Repeated AT-SPI `get_application` calls are expensive in Calc snapshots. Cache successful owner lookups within the existing operation-scoped getter cache; preserve retries for missing owners and invalidate on mutations. A getter can return a fresh GI wrapper, so toolkit attributes attached to that wrapper alone do not prevent repeated owner lookups.
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
- 2026-10-14 (PR #564 merge repair): An inherited DISPLAY can name an inaccessible server in the command sandbox. Run display-dependent focused tests under a fresh Xvfb/D-Bus session; run the hermetic suite without a display so existing prerequisite guards reflect the actual environment.

- 2026-10-14 (#568 repair): gucharmap Character Table is a childless GTK drawing area. Keep its surface label separate from statusbar selection: a codepoint/name there is not a character-cell ref, and scrolling the opaque table may change selection.

- 2026-10-14 (#548 repair): A window may map between the registration eligibility check and the final missing-root check. Do not report a missing bridge for an app that has not received its registration retry; keep that first observation retryable. Exercise this ordering with synthetic lists and a scripted clock.

- 2026-10-14 (PR #570 conflict repair): Refresh the remote PR head before merging main; an older local branch can miss repairs already pushed. Resolve independently appended regression tests by retaining both complete test blocks and check the resulting diff against main.

- 2026-10-14 (#570 merge repair): Preserve main's platform_editable override when merging text-view guards. A text-view handle with a state-set API still needs the live EDITABLE recheck when GI is unavailable; transports without a state-set API retain their existing verified replacement path.

- 2026-10-14 (#202 repair): Linux live segfault logs can identify concurrent native getters beside `Atspi.event_main`. Preserve main's `_call_first` event-thread dispatch and its regression when merging identity changes; driver-level dispatch alone does not cover direct AT-SPI callers.
- 2026-10-14 (PR #472 repair): A drawing-canvas fallback must run only when no surface label was already recognized. Otherwise an empty Inkscape result erases Gnumeric and gucharmap labels; run those surface regressions alongside the canvas tests when merging app-specific naming rules.

- 2026-10-14 (PR #472 repair): Inkscape can expose both its SVG canvas and a childless painted filler inside the Fill tab as opaque regions. Preserve their native bounds and derive document context from the native frame title; keep the inaccessible SVG content note specific to the drawing canvas. An empty app-specific fallback must not overwrite another application's surface name.
- 2026-10-14 (PR #472 merge repair): Concurrent isolated D-Bus test sessions need distinct writable XDG_CACHE_HOME directories. AT-SPI bus launchers share the cache socket path; ending one session can remove another session's socket and abort MCP subprocess tests. Recheck bus failures in a fresh session with its own cache before treating them as code regressions.
- 2026-10-14 (PR #549 repair): A supplied worktree can lag its tracking branch even when its files are clean. Fetch and preserve the current remote PR head before merging main, so a plain repair push retains prior repair commits.

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

- 2026-10-14 (PR #626 CI repair): GTK 4.14 routes GtkAccessibleText through a bridge that implements neither range nor character extents, unlike newer GTK. Keep buffer read-back separate from verified visibility in the search index; label unverified contents and never allow them to satisfy find. Reproduce version-dependent failures with the runner’s GTK and app libraries.
- 2026-10-14 (#618): GTK 4 tree-expander buttons expose namespaced listitem expansion actions. Prefer toggle-expand, otherwise choose expand/collapse from the live state, and verify EXPANDED changes before success. Do not fall back to pointer input after a failed native action.

- 2026-10-14 (PR #630 repair): Tree-expander regressions and Chromium combobox regressions append at the same location; preserve both complete blocks during a main merge and run the combined synthetic suite.

- 2026-10-14 (#646): Real Kate 25.04.3 bisect identified #562 (e3b272f) as the missing-status-controls regression. Skip generic Qt viewport-text reads before Kate’s existing buffer-visibility refusal; a discarded probe still disrupts later accessibility traversal. Preserve other Qt editors’ viewport filtering and Kate’s off-screen text refusal.

- 2026-10-14 (PR #608 merge repair): Recheck failed browser live cases after preserving newer main fixes before adding another workaround. The Chrome large-table search/click and Firefox web-field typing failures both pass after merging main; retain independent heading-control-label and web-combobox regression blocks together.
- 2026-10-14 (#643): Refuse writes to already observed secure fields before live ref resolution; GTK4 PasswordEntry may rebuild its inner accessible. Treat native password-text parent/child boundaries as secure before Text reads. Never reuse an old secure handle to perform an action; non-secure refs still need live resolution.

- 2026-10-14 (PR #644 repair): GTK4 secure-field refusal and unknown-visibility write redaction are complementary: preserve the early secure-ref refusal together with the private success marker reaching Runtime and audit. Keep both live regression blocks when merging these fixes.

- 2026-10-14 (PR #630 conflict repair): Preserve Chromium heading control-label regressions alongside tree-expander tests when merging appended blocks. The failed Linux run contained only the tracked Calc timing flake; inspect the complete job summary before changing unrelated action code.
- 2026-10-14 (#647): Suppressing Kate whole-buffer Text values exposes automatic scroll-anchor fallbacks: retain the editor view ahead of sibling breadcrumb lists. Container selection cannot establish text visibility; refuse unverifiable editor text searches before wheel/Find input and preserve explicit container refs. Live v0.4.66..main bisect identifies #563 (c226b7b) as the interaction trigger, not a reason to restore whole-buffer values.

- 2026-10-14 (PR #648 repair): Editor scroll-anchor selection and secure-field pre-resolution refusals are independent server behaviors. Preserve both regression blocks when resolving appended test conflicts; the unreadable-editor refusal must still happen before wheel or Find input.

- 2026-10-14 (PR #649 repair): Kate viewport-probe avoidance preserves status traversal, while editor scroll-anchor selection independently preserves search refusal. Retain both documented behaviors when merging their adjacent backend notes.
- 2026-10-16 (#595): XTEST wheel taps need both per-notch sync and real time between taps; sleeping while events remain queued still delivers a coalescible burst to Qt Quick. Wheel line units describe input notches, not verified pixels or rows.

- 2026-10-14 (PR #626 merge repair): Preserve GTK buffer search exclusion and Kate owner detection together when merging adjacent snapshot rules; they protect independent toolkit visibility paths.

- 2026-10-16 (PR #626 merge repair): GTK 4 verified text visibility and GTK 4 tree-expander click verification are independent behaviors; preserve both backend notes when their adjacent documentation conflicts.
- 2026-10-16 (PR #655 repair): Require the AT-SPI Value interface before meter numeric reads even when a missing interface returns a plausible stale number. Preserve Qt Quick slider/spin unknown-bound handling independently of that interface gate, and retain both regression blocks when merging appended tests.
- 2026-10-16 (PR #659 repair): Qt Quick combo selection and numeric Value-interface support are independent behaviors. Preserve both backend documentation blocks when resolving adjacent additions; verify combo regressions alongside numeric Qt tests after merging main.

- 2026-10-16 (#596): Qt Quick MenuBarItem exposes a named nested menu-bar role with zero actions; its popup is a sibling in the same window. Recognize that shape, use only measured native bounds for pointer opening, and verify popup visibility changed before walking rows. The bar label's SHOWING state alone does not establish an open popup.
- 2026-10-16 (#603): Claws Mail folder and message lists share an empty panel-under-scroll-pane shape. Use a combined surface label and retain the missing-row limitation; pane dimensions or focusability do not establish list identity. Scope recognition to Claws split-pane windows and preserve native labels.

- 2026-10-16 (#672): HTML submit ownership must honor explicit `form` attributes before ancestry and keep backend ids isolated by CDP session/document. Native DEFAULT and DOM submit markers are discovery hints only; preserve lexical exact-target behavior for unrelated queries and never turn markers into action substitution.

- 2026-10-16 (#598 merge repair): Preserve asynchronous Chromium PDF reveal regressions alongside XTEST wheel-pacing regressions when both append to the Linux synthetic suite. A document-text hit has no table row index; retain its actual handle without click bounds while native extents settle.
- 2026-10-16 (#671): Exec-off terminal refusals must be final and direct planners to GUI routes; permission wording alone can invite requests to enable exec. Keep this guidance conditional on exec being off and preserve the terminal gate and audit.

- 2026-10-16 (PR #677 repair): Claws Mail wizard page titles and unnamed entry row labels are independent naming fallbacks. Preserve both regression blocks when merging their appended tests; entry labels must retain native handles and measured bounds.


- 2026-10-16 (#609): Chrome's accessible hit test can return the underlying control through a top-layer HTML popover. Use showing `ispopup` panels and measured bounds to identify covers, and keep the mapped `AXPopover` in the container no-value set to avoid numeric interface reads.

- 2026-10-16 (PR #679 repair): HTML popover mapping and Claws Mail wizard tab naming have independent appended regressions. Preserve both complete test blocks when merging main, including native unknown-geometry assertions for hidden wizard tabs.

- 2026-10-16 (PR #676 repair): Optional AT-SPI observation hints must tolerate missing GI bindings as well as missing native state members. Exercise the fallback with an ImportError even on Linux so synthetic backend tests remain portable to macOS and Windows.

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

- 2026-10-16 (PR #685 merge): Thunar path replacement and failed-navigation verification are complementary. Preserve focused replacement with a separate Return, and check navigation errors before accepting a changed accessibility tree as success.
- 2026-10-16 (PR #685 live verification): Thunar can expose dialog-error-symbolic before and after successful navigation. A persistent icon alone is not a new failure; retain unchanged-window refusal and newly appearing error detection, and verify successful navigation with the live location and title.
- 2026-10-16 (PR #667 validation): Run the full suite through `python -m pytest`: invoking pytest from stdin breaks multiprocessing spawn because `<stdin>` is not an importable file. Limit forced missing-GI checks to hermetic backend tests; Linux CLI, menu, and live checks can require the installed native binding even without a display.

- 2026-10-16 (PR #677 CI review): Check failed-run job summaries against the current head before changing platform code; the older run failed only the tracked Calc timing test while Windows, macOS, Browser, and Package passed.

- 2026-10-16 (PR #677 main merge): Preserve HTML popover coverage alongside GTK sibling-row entry naming when resolving appended tests and backend notes; these observation hints are independent.
- 2026-10-16 (#610): Chromium HTML popovers expose panel + ispopup and DETAILS_FOR opener relations. Preserve their container boundary before descendant ref matching; dismissed descendants can otherwise receive reorder hints naming unrelated browser chrome. Snapshot absence alone does not prove how a popover was dismissed.

- 2026-10-16 (PR #692 repair): Preserve SHOWING-gated HTML popover mapping when adding opener metadata and stale descendant guidance; hidden panels must retain ordinary group behavior so containment checks cannot treat a dismissed popup as open.

- 2026-10-16 (#610 repair): Runtime click verification can pass a target-only snapshot to rematch_ref. Ancestor inspection must tolerate omitted parents while retaining containment checks for ancestors present in a full snapshot.

- 2026-10-16 (PR #685 CI repair): A failed short-budget agent test can stop in show-desktop setup before the agent runs. Reproduce both the test alone and after the preceding terminal-launch test under a real window manager before attributing that focus failure to unrelated text replacement code.
- 2026-10-16 (#684): Qt text recovery must handle malformed QSpiTextRangeList replies and failures in the fallback Text read itself. Retry getText(0,-1) only for a native character count within the existing cap, then capped chunks; retain native character visibility checks and refuse incomplete reads.

- 2026-10-16 (PR #701 repair): Preserve GTK numeric-control label regressions and Qt bounded-range recovery regressions as independent complete blocks when merging appended tests. Numeric labels must precede changing slider descriptions while native Value reads remain unchanged.


- 2026-10-16 (PR #696 repair): Keep VLC's bounded registration retry and VLC-specific Qt setup hint alongside the generic GTK2 bridge prerequisites when merging missing-tree diagnostics. Preserve both the VLC registration regressions and main's Qt text-recovery regressions.
- 2026-10-16 (#640): Escape multiline accessible names only when rendering; retain native titles for find/ref matching. Shared `_render_line` serves full snapshots, interactive element lines, and find results.

- 2026-10-16 (PR #711 repair): Calc viewport bands use the table role to retain sheet pruning limits, while ordinary table-cell bands use groups to avoid orphaned final cells. Preserve both representations and their regressions when merging adjacent band changes; label only the Calc synthetic bands.
- 2026-10-16 (#651): Eye of GNOME GTK zoom entries can echo EditableText percentages without changing image scale. Buffer equality cannot verify applied zoom; refuse before mutation rather than send an unrequested activation. Preserve the independent header-bar menu guidance when merging adjacent backend notes.
- 2026-10-16 (PR #701 main merge): Retain GTK numeric label tests alongside VLC registration tests and same-app cover documentation when adjacent additions conflict; they protect independent observation behaviors.

- 2026-10-17 (PR #720 repair): Native popover-refresh gating and per-snapshot sibling-window cover caching address independent tree and window-scan costs. Preserve both complete regression blocks when merging their appended action-outcome tests.

- 2026-10-17 (PR #720 Calc CI investigation): A live Calc failure in the initial LinuxDriver.snapshot loop occurs before Runtime creation, so Runtime popover-refresh gating cannot cause it. Check the failing phase before changing input code. The exact failed head 8eb1b60 passed test_linux_calc_range_formula_and_normalised_numbers_confirm alone in 164.23 s under private 1280x800 Xvfb, D-Bus, AT-SPI and Openbox; retain the unchanged startup assertions and limits.
