"""Shell and Python execution for the agent.

These actions are not MCP tools. They run only when an ``Agent`` is constructed
with ``allow_exec=True`` (or the CLI ``--allow-exec`` flag), and only after the
approve hook allows that specific call. Every attempt is one line in an
append-only audit log.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_EXEC_TIMEOUT_S = 30.0
MAX_EXEC_TIMEOUT_S = 120.0
EXEC_OUTPUT_CAP = 4_000

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Outcome of one approved shell or Python invocation."""

    exit_code: int | None
    output: str
    truncated: bool
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code == 0


def bound_timeout(value: float) -> float:
    """Clamp a positive timeout into the executor's allowed range."""
    return min(float(value), MAX_EXEC_TIMEOUT_S)


def run_command(
    command: str,
    *,
    shell: bool,
    cwd: str,
    timeout_s: float,
    cap: int = EXEC_OUTPUT_CAP,
    python_code: str | None = None,
) -> ExecResult:
    """Run ``command`` or a fresh interpreter over ``python_code``.

    ``timeout_s`` kills the process group. Stored output is capped at ``cap``
    characters; the rest is marked truncated and discarded.
    """
    if python_code is not None:
        argv: str | list[str] = [sys.executable, "-I", "-c", python_code]
        use_shell = False
    else:
        argv = command
        use_shell = shell
    popen_kwargs: dict = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": use_shell,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **popen_kwargs)
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill(proc)
            stdout, stderr = "", ""
    output, truncated = _cap(_combine(stdout, stderr), cap)
    if timed_out:
        return ExecResult(exit_code=None, output=output, truncated=truncated, error="timeout")
    code = proc.returncode
    error = None if code == 0 else f"exit {code}"
    return ExecResult(exit_code=code, output=output, truncated=truncated, error=error)


def audit_record(
    *,
    action: str,
    command: str,
    cwd: str,
    exit_code: int | None,
    output: str,
    approval: str,
    error: str | None = None,
    truncated: bool = False,
) -> dict:
    """One JSON object for ``exec-audit.jsonl``."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "command": command,
        "cwd": cwd,
        "exit_code": exit_code,
        "output": output,
        "truncated": truncated,
        "approval": approval,
        "error": error,
    }


def append_audit(path: Path, record: dict) -> None:
    """Append one JSON line and flush it. Existing lines are never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with _lock_for(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def _combine(stdout: str | None, stderr: str | None) -> str:
    out = stdout or ""
    err = stderr or ""
    if out and err:
        return out + ("\n" if not out.endswith("\n") else "") + err
    return out or err


def _cap(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    omitted = len(text) - cap
    return text[:cap] + f"\n…[truncated {omitted} chars]", True


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve()) if path.parent.exists() else str(path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


__all__ = [
    "DEFAULT_EXEC_TIMEOUT_S",
    "EXEC_OUTPUT_CAP",
    "MAX_EXEC_TIMEOUT_S",
    "ExecResult",
    "append_audit",
    "audit_record",
    "bound_timeout",
    "run_command",
]
