# Contributor Guide

## Project structure

This Python 3.11+ package provides accessibility-first computer use through an MCP server, CLI, and agent loop.

- `a11y_computer_use/`: shared schema, observation/pruning, safety, runtime (`server.py`), and CLI (`cli.py`).
- `a11y_computer_use/drivers/`: macOS, Windows, Linux AT-SPI2, and browser CDP backends; `base.py` defines the driver protocol.
- `a11y_computer_use/agent/`: agent loop, model clients, execution, and replay; `adapters/` integrates provider computer-use APIs.
- `tests/`: pytest unit, regression, and live tests; `fixtures/`, `agent_fixtures/`, and `conftest.py` supply synthetic trees and app fixtures; `e2e/` covers end-to-end behavior.
- `docs/`, `examples/`, and `scripts/box/`: reference docs, runnable examples, and desktop VM tooling. `.github/workflows/` defines CI and releases.

Read `CONTRIBUTING.md` for detailed setup and backend requirements. Treat `docs/decisions/plan-2026-07.md` as a dated design record; use current code and backend docs for implementation status.

## Build and test commands

Run from the repository root. Use the supplied interpreter for pytest:

```bash
# Editable development install; add backend extras as needed.
/workspace/a11y-venv-codex/bin/python -m pip install -e '.[dev,browser]'

# Full suite, including explicit skip reasons.
/workspace/a11y-venv-codex/bin/python -m pytest -q -rs -p no:cacheprovider

# Focused regression tests (choose files relevant to the change).
/workspace/a11y-venv-codex/bin/python -m pytest tests/test_drivers.py tests/test_server.py -q -rs -p no:cacheprovider

# Build wheel and source distribution with the Hatchling backend.
uv build
```

Native live tests require the relevant OS, permissions, services, or browser endpoint. Consult `docs/ci.md` and `.github/workflows/ci.yml` for CI recipes. Linux live tests need an AT-SPI2 bus and display session; browser live tests need Chromium with remote debugging and `A11Y_COMPUTER_USE_CDP_ENDPOINT`. Skips do not establish live verification.

## Coding style and architecture

- Follow `.editorconfig`: UTF-8, LF, final newline, four spaces for Python, two for YAML/JSON/TOML and Markdown lists. Match nearby naming, type annotations, and docstrings; use `snake_case` functions and `PascalCase` classes. No formatter or linter is configured in `pyproject.toml`.
- Keep schema, pruning, safety, and the MCP surface platform-independent. Put new OS operations behind `Driver`; load optional native dependencies lazily so imports work on other platforms. Existing macOS `act.py` and `capture.py` are legacy native implementations delegated to by the macOS driver.
- Reuse `observe.build_snapshot` and shared ref matching. Preserve permission gates, destructive-action confirmation, secure-field protection, act-time rechecks, and audit redaction.
- Report platform gaps explicitly with `ComputerUseError(ErrorCode.UNSUPPORTED, ...)` and a useful hint. Unimplemented driver methods raise `NotImplementedError` naming the intended native API.

## Testing guidelines

- Add regression tests at the layer changed, using synthetic `TreeAccessor` objects, monkeypatched drivers, or scripted transports before requiring real apps. Reuse fixtures in `tests/conftest.py` and keep persistent state in pytest temporary directories.
- Guard live tests by platform and prerequisites. macOS tests use `HAS_AX`, `HAS_SCREEN`, or `HAS_DISPLAYS` as appropriate; other backends self-skip when their bus, endpoint, or native dependency is unavailable.
- Run focused tests while iterating and the full suite before a PR. Record summary lines, relevant skip reasons, and where live tests ran.
- For MCP tool changes, update tool-list assertions in `tests/test_server.py`, browser-specific assertions in `tests/test_browser.py`, tool docstrings, and affected documentation.

## Commits and pull requests

- Use concise imperative subjects. `CONTRIBUTING.md` recommends conventional prefixes such as `fix(linux): ...` or `test(browser): ...`; recent history also uses descriptive subjects with issue/PR references. Do not add `Co-Authored-By` or "Generated with" trailers.
- Target `main` and follow `.github/PULL_REQUEST_TEMPLATE.md`: explain the behavior change, link the issue, identify platforms touched, report tests and skips, and update relevant docs. Rebase on the target branch and ensure CI passes or explain failures.
- State precisely what is implemented and verified. Distinguish hermetic tests, CI results, and live runs; label unsupported or untested paths. Include measurement commands for numerical claims.

## Standing Rules (mandatory)
- One improvement per PR, always with a test. Body ends with `Fixes #N` when an issue exists.
- Never weaken, skip, xfail or delete tests; never raise timeouts, bounds or limits to make CI pass.
- Never change the version in `pyproject.toml` or edit `CHANGELOG.md` in a fix PR (release PRs only).
- #333/#308 rules: never rewrite an explicit click into a different action, never substitute an action the caller did not ask for, never fabricate or estimate geometry. Never report success without verifying the effect; return an honest refusal/error instead.
- Security: with shell/exec off, terminals and anything that can start a shell are blocked even for granted apps; only an explicit shell grant gets through, and every block writes an audit row.
- Read `.codex/lessons.md` before starting; append new lessons there in the same PR.
- PRs are merged by `/workspace/automerge.sh N FULLSHA` after all CI jobs are green. Never use `gh pr merge --auto`.

## CI Facts
- 5 required jobs: Linux, macOS, Windows, Browser, Package. All must be green to merge.
- The Linux job runs live AT-SPI tests against real GTK/Qt/LibreOffice apps under Xvfb; synthetic tests alone do not prove a Linux fix.
- The Calc 5 s timing test is flaky on slow runners (~6 s seen). Re-run it; never raise its limit. Repeated failures need a root cause.
