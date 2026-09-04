## Summary

What changed and why. Link the issue if there is one.

## Platforms touched

- [ ] Platform-free core (observe, act, safety, schema, server, cli)
- [ ] macOS driver (computeruse/drivers/macos.py and the observe, act, capture modules it delegates to)
- [ ] Windows driver (computeruse/drivers/windows.py, _uia, _win_input, _win_system)
- [ ] Linux driver (computeruse/drivers/linux.py, _atspi, _linux_input, _linux_system)
- [ ] Browser driver (computeruse/drivers/browser.py, _cdp, _cdp_ax)
- [ ] CI workflow (.github/workflows/ci.yml)
- [ ] Docs or examples only

## Tests run

Paste the summary line of each run and name the skips you saw.

```text
.venv/bin/pytest -q
<summary line, for example: N passed, M skipped>
```

Live runs, where the change touches them:

- [ ] macOS live tests ran with the Accessibility and Screen Recording grants held by the test process. Without the grants they skip with "Accessibility TCC grant missing"; `computeruse doctor` shows the grant state.
- [ ] Windows live UIA tests: `pytest tests/test_windows_live.py -q -s` on Windows.
- [ ] Linux live AT-SPI2 tests: `pytest tests/test_linux_live.py` under `xvfb-run` and `dbus-run-session`, the way the linux job in ci.yml runs them.
- [ ] Browser live tests: `pytest tests/test_browser.py -k live` and `pytest tests/test_arena.py -k live` against a Chromium started with `--remote-debugging-port=9222`, with `COMPUTERUSE_CDP_ENDPOINT` pointing at it.
- [ ] Not applicable, the change has no live path.

If a tool was added or removed: `tests/test_server.py` pins the tool list in `EXPECTED_TOOLS`, and `tests/test_browser.py` asserts that `console` and `network` exist only on the browser backend. Update both, and every doc that states the count.

## Docs updated

- [ ] README.md
- [ ] docs/ (name the file)
- [ ] Docstrings and the MCP tool descriptions in computeruse/server.py
- [ ] PLAN.md, where the roadmap status changed
- [ ] No doc change needed (say why)

## Claims

- [ ] Every sentence in this PR, its commits and the docs it touches states what is implemented and verified. Anything gated, unsupported, skipped in CI or tested only through a fake transport is labelled that way in the same sentence.
- [ ] Every number I wrote (tool counts, test counts, token figures, timings) was measured on this branch, and the command that produced it is in this description.

## Commits

- [ ] Commit messages carry no `Co-Authored-By` or "Generated with" trailers. The message ends at the body.
- [ ] The branch is rebased on the target branch and CI is green, or the failing job is explained above.
