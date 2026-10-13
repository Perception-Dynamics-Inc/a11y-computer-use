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

- 2026-10-13 (#503): Keep the GTK 4 garbage-allocation filter limited to unnamed, childless panels; large real containers still need their extents for clipping and sampling. When merge conflicts join appended regression tests, retain both branches' tests and compare the final diff against main.

- 2026-10-14 (PR #535): When merging AT-SPI naming fallbacks, retain independent role-specific rules together. Relation-derived GTK menu labels must pass through the same rich-text normalization as native names; cover quoted attribute delimiters in the relation-label regression too.
