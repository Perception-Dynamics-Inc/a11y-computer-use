"""Agents report tool defects and bottlenecks to the project's public issues.

The MCP server tells an agent *when* to report (its instructions and the
nudges `server.run` appends to slow or crashed calls) and *where* (the
`report_issue` tool, which files against `REPO`). This module is the
transport: it composes the issue, redacts what must never leave the machine,
files through the GitHub CLI when one is signed in, and otherwise hands back
a prefilled new-issue URL for a human to open.

Filing is a public write, so the server asks the host to confirm (MCP
elicitation) before `gh` runs; with no confirmation channel it never posts
and returns the URL instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

REPO = "Perception-Dynamics-Inc/a11y-computer-use"
ISSUES_URL = f"https://github.com/{REPO}/issues"

#: kind -> label. Reports carry `agent-report` too, so maintainers can filter.
KINDS: dict[str, str] = {
    "bug": "bug",
    "bottleneck": "bottleneck",
    "missing_capability": "missing-capability",
    "app_compatibility": "app-compatibility",
}

#: Calls slower than this get a report nudge appended (0 disables).
SLOW_CALL_S = float(os.environ.get("A11Y_COMPUTER_USE_SLOW_CALL_S", "10"))

#: Tools whose whole purpose is to wait; their duration is the caller's choice.
WAITING_TOOLS = frozenset({"wait_until", "wait_for", "app", "scroll_to_find", "mission",
                           "request_permission", "grant_app", "report_issue"})  # the last three wait on a human

#: GitHub prefilled-URL budget (the request line, not the body limit).
_URL_BUDGET = 7000

_SECRET_PATTERNS = [
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\s*[:=]\s*\S+"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
]


def redact(text: str) -> str:
    """Strip what a public issue must not carry: tokens, key-value secrets,
    e-mail addresses, and the user's home directory (a username)."""
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    home = str(Path.home())
    if home and home != "/":
        out = out.replace(home, "~")
    return out


def environment(driver_name: str | None = None) -> dict[str, str]:
    """The facts every report needs and no agent should have to gather."""
    try:
        from importlib.metadata import version

        ver = version("a11y-computer-use")
    except Exception:  # noqa: BLE001 - a source checkout without metadata
        ver = "unknown"
    import platform

    from a11y_computer_use.drivers import current_platform

    return {
        "a11y-computer-use": ver,
        "platform": current_platform(),
        "os": platform.platform(terse=True),
        "python": platform.python_version(),
        "driver": driver_name or os.environ.get("A11Y_COMPUTER_USE_DRIVER", "default"),
    }


def compose(kind: str, title: str, body: str, *, tool: str | None = None,
            env: dict[str, str] | None = None) -> tuple[str, str, list[str]]:
    """The issue as GitHub sees it: (title, markdown body, labels).

    Raises:
        ValueError: unknown ``kind`` or an empty title/body.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, not {kind!r}")
    title = redact(" ".join(title.split()))
    body = redact(body.strip())
    if not title or not body:
        raise ValueError("title and body are required")
    env = env or environment()
    lines = [f"[{kind}] {title}"] if not title.lower().startswith(f"[{kind}]") else [title]
    facts = "\n".join(f"| {k} | {v} |" for k, v in env.items())
    md = (
        f"{body}\n\n"
        f"### Environment\n\n| | |\n|---|---|\n{facts}\n"
        + (f"| tool | `{redact(tool)}` |\n" if tool else "")
        + "\nFiled by an agent through `report_issue`; secrets, e-mail addresses "
        "and the home directory were redacted before filing.\n"
    )
    return lines[0], md, ["agent-report", KINDS[kind]]


def new_issue_url(title: str, body: str, labels: list[str]) -> str:
    """A prefilled GitHub new-issue link, body truncated to the URL budget."""
    base = f"{ISSUES_URL}/new?labels={quote(','.join(labels))}&title={quote(title)}&body="
    room = max(200, _URL_BUDGET - len(base))
    text = body
    if len(quote(text)) > room:
        while text and len(quote(text + "\n\n(truncated)")) > room:
            text = text[: int(len(text) * 0.8)]
        text += "\n\n(truncated)"
    return base + quote(text)


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def gh_ready(runner: Runner = subprocess.run) -> bool:
    """True when a GitHub CLI is installed and signed in."""
    if shutil.which("gh") is None:
        return False
    try:
        done = runner(["gh", "auth", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def file_issue(title: str, body: str, labels: list[str], *, runner: Runner = subprocess.run) -> str:
    """Create the issue with ``gh`` and return its URL.

    Raises:
        RuntimeError: gh failed; the message carries its stderr.
    """
    cmd = ["gh", "issue", "create", "-R", REPO, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    try:
        done = runner(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"gh issue create failed: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError(f"gh issue create failed: {(done.stderr or done.stdout).strip()[:400]}")
    url = (done.stdout or "").strip().splitlines()[-1] if done.stdout else ""
    return url or f"{ISSUES_URL} (created; gh printed no URL)"


def report(kind: str, title: str, body: str, *, tool: str | None = None,
           confirm: Callable[[str], bool] | None = None, env: dict[str, str] | None = None,
           runner: Runner = subprocess.run) -> str:
    """File a report or hand back the link to file it.

    ``confirm`` is the host's confirmation channel (None: none available).
    Posting happens only when ``gh`` is signed in *and* the human accepted;
    every other path returns a prefilled URL and says why it was not posted.
    """
    full_title, md, labels = compose(kind, title, body, tool=tool, env=env)
    url = new_issue_url(full_title, md, labels)
    if not gh_ready(runner):
        return (f"not posted: no signed-in GitHub CLI on this machine. Give the user this "
                f"prefilled link to open the issue: {url}")
    if confirm is None:
        return (f"not posted: this host has no confirmation channel and filing a public issue "
                f"needs the user's yes. Give the user this prefilled link: {url}")
    prompt = (f"a11y-computer-use wants to file a public GitHub issue on {REPO}:\n"
              f"{full_title}\n\nlabels: {', '.join(labels)}\n\n{md[:1200]}"
              + ("\n..." if len(md) > 1200 else ""))
    if not confirm(prompt):
        return f"not posted: the user declined. The prefilled link, should they change their mind: {url}"
    try:
        posted = file_issue(full_title, md, labels, runner=runner)
    except RuntimeError as exc:
        return f"not posted: {exc}. Prefilled link instead: {url}"
    return f"posted: {posted}"


def slow_call_note(tool: str, seconds: float) -> str | None:
    """The nudge appended to a slow result, or None when it is expected."""
    if SLOW_CALL_S <= 0 or tool in WAITING_TOOLS or seconds < SLOW_CALL_S:
        return None
    return (f"[slow call: {tool} took {seconds:.1f} s. If this repeats and the app was "
            f"not busy, report it: report_issue(kind='bottleneck', tool='{tool}', ...)]")


def internal_error_text(tool: str, exc: BaseException) -> str:
    """One line for a crash inside a tool, with the report instruction."""
    detail = redact(f"{type(exc).__name__}: {exc}")[:300]
    return (f"internal_error: {tool} crashed: {detail} | hint: this is a defect in "
            f"a11y-computer-use, not in your call. Report it with report_issue(kind='bug', "
            f"tool='{tool}', title=..., body=<this line plus what you were doing>).")


def as_json(env: dict[str, str]) -> str:
    return json.dumps(env, sort_keys=True)


if __name__ == "__main__":  # a quick manual check: python -m a11y_computer_use.reporting
    print(as_json(environment()), file=sys.stderr)
