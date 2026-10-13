# Lessons learned (append-only; newest at the bottom)

- The libatspi child cache goes stale when the AT-SPI event thread is on (`A11Y_COMPUTER_USE_ATSPI_EVENTS`); code that relies on fresh children (e.g. Calc grid editors, #475/#411) must not assume the cache is current.
- #489's Audacity track/clock labels were applied app-wide and leaked into the crash-recovery dialog (#505). Scope app-specific labels to the window/role they belong to.
- #512's same-app dialog occlusion refusal broke tests/test_server.py on every platform. Run test_server.py for any change that affects click results, not just Linux suites.
- #498 (Qt toggle click, #493) may have regressed `test_linux_qt_line_edit_combo_and_values` on main. Qt click changes must run the Qt live tests' synthetic equivalents and check line edits/combos.
- GTK comboboxes: options were capped at the first 64 children (#513, fixed in #516). Never cap option lists silently; look up the requested value among all children and say when an error list is truncated.
- The first #503 attempt (dropping GTK 4 nodes with garbage sizes) broke the huge Calc and combo popup tests. Large-but-valid geometry is legitimate; only filter clearly uninitialized sizes.
- 2026-10-13 (#480): Linux maps native AT-SPI terminals to AXTextArea; inspect native terminal ancestry, including remembered editable handles, rather than treating every multiline editor as a shell. Preserve cached-field ownership checks before terminal detection so stale focus still returns an audited focus_changed error. Execution permission is independent of per-app FULL grants and defaults off for MCP Runtime hosts as well as agents.
- 2026-10-13 (#537): Repeated AT-SPI `get_application` calls are expensive in Calc snapshots. Cache successful owner lookups within the existing operation-scoped getter cache; preserve retries for missing owners and invalidate on mutations. A getter can return a fresh GI wrapper, so toolkit attributes attached to that wrapper alone do not prevent repeated owner lookups.

- 2026-10-13 (PR #528): Show-desktop requests are asynchronous; verify foreground focus has settled before starting a short agent budget. Use a scripted monotonic clock in the setup regression so host scheduling cannot exhaust the wait, and check the hermetic tests with GI imports blocked.
- 2026-10-13 (#530): `gh run view --log-failed` can return empty output for a failed job. Fetch `repos/{owner}/{repo}/actions/jobs/{job_id}/logs` directly before concluding there are no failure details.

- 2026-10-13 (#406): Linux snapshot titles fall back to AT-SPI descriptions when names are empty; normalize both fields. Rich-text tag matching must respect quoted attribute delimiters and full tag names so `>` inside an attribute and literal `<p-custom>` labels are handled correctly.

- 2026-10-14 (#446 repair): A Calc grid read can time out while the final cell editor remains open. Refresh both native child caches and operation-scoped getters, keep mask changes on the AT-SPI event thread, and commit the discovered editor before reading sibling cells. Refresh the subsequent table lookup too. Reproduce live failures with CI's window manager and display size.
- 2026-10-13 (#503): Keep the GTK 4 garbage-allocation filter limited to unnamed, childless panels; large real containers still need their extents for clipping and sampling. When merge conflicts join appended regression tests, retain both branches' tests and compare the final diff against main.

- 2026-10-14 (#491): In restricted Linux sandboxes, CLI subprocess tests can abort in libatspi before Python handles an error when the accessibility bus is unavailable. Use a fresh D-Bus session and writable XDG_CACHE_HOME; preserve access to GSettings schemas when isolating XDG desktop entries. Installed desktop programs can also change mock launch-resolution results, so distinguish environment-dependent failures from code regressions.
- 2026-10-13 (#567): Lifecycle trace readers need flushed events, not physical-storage durability. `os.fsync` before every agent step can stall Windows runs; flushing and closing the file preserves cross-process visibility. Verify both MCP completion and subprocess cancellation when changing this path.
- 2026-10-14 (#446 repair): A post-commit pause inside the AT-SPI event-thread callback cannot process the editor-removal event. Commit in one driver operation, pause on the caller, then verify in the next operation; keep the existing pause bound.

- 2026-10-14 (#559 repair): Check shell-capable launchers before app resolution so command strings return audited exec_disabled rather than app_not_found. Inspect executable symlinks and desktop Exec programs even when gtk-launch delegates the launch; interpreter launch classification must not block ordinary GUI input merely because its process comm is python3.

- 2026-10-14 (#478): Kate’s painted gutter has no accessible line labels. Scope its contextual name to native editor siblings and retain the opaque-region limitation; never infer line targets from the strip’s bounds. When merging surface-label branches, keep each application’s recognition condition independent.
