# Agents report defects and bottlenecks

The tool is maintained from its public issues at
https://github.com/Perception-Dynamics-Inc/a11y-computer-use/issues, and the
agents that use it are the ones who hit its limits first. So the server tells
them where and when.

## When

The server instructions and the results themselves say when:

- a call returns `internal_error` (a crash inside the tool; the line names the
  tool and says to report it);
- a call is marked `[slow call: <tool> took N s ...]` (over
  `A11Y_COMPUTER_USE_SLOW_CALL_S`, default 10 s; waiting tools such as
  `wait_until`, `wait_for`, and `app launch` are exempt);
- a result is clearly wrong, or a capability the task needs is missing;
- an app's tree is empty or wrong.

Not reportable: `needs_permission` and `deny` (the user's grants), `stale_ref`
(re-observe), and the agent's own argument mistakes.

## Where

`report_issue(kind, title, body, tool=None)` with kind `bug`, `bottleneck`,
`missing_capability`, or `app_compatibility`. The body should say what was
called, what came back (the error line verbatim), what was expected, and the
app. The tool:

1. redacts tokens, key-value secrets, e-mail addresses, and the home
   directory from the title and body;
2. appends an environment table (version, platform, OS, Python, driver, tool);
3. asks the user, since the issue is public: the host's confirmation dialog,
   or a native macOS alert from the server with the issue text in a
   scrollable box and an "Always allow issue reports from this tool"
   checkbox (remembered in `~/.a11y-computer-use/settings.json`); then files with the signed-in GitHub CLI (`gh issue create`) under
   the labels `agent-report` and the kind;
4. without `gh`, without a confirmation channel, or on a decline, returns a
   prefilled new-issue link for the user to open instead. Nothing is posted
   without a human's yes.

Every call is recorded in the audit log as `report_issue`.

Reports arrive with the `agent-report` label; the issue template
`agent_report.yml` mirrors the same fields for humans.
