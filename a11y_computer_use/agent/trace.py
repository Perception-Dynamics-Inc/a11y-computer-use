"""Per-run trace: ``trajectory.jsonl``, ``steps.jsonl``, and PNG screenshots.

``trajectory.jsonl`` is the full record of what the model saw and what the
loop did. ``steps.jsonl`` is the redacted ``StepRecord`` log. Secrets are
removed before either file is written.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from pathlib import Path

_OBS_LIMIT = 24_000
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SECRET_KEYS = frozenset({"password", "secret", "token", "otp", "cvv", "cvc", "pin", "ssn"})


def truncate_observation(text: str, limit: int = _OBS_LIMIT) -> str:
    """Keep ``limit`` characters and mark how much was cut."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n…[truncated {omitted} chars]"


def redact_args(args: dict, *, sensitive: bool = False) -> tuple[dict, list[str]]:
    """Return ``(redacted args, original secret strings)``.

    Secret-shaped keys are always removed. ``text`` and ``value`` are removed
    when the target is a sensitive field, and a card-number-shaped string is
    removed wherever it appears.
    """
    redacted: dict = {}
    secrets: list[str] = []
    for key, value in args.items():
        secret = False
        if str(key).lower() in _SECRET_KEYS:
            secret = True
        elif sensitive and str(key).lower() in {"text", "value"}:
            secret = True
        elif isinstance(value, str) and _CARD_RE.search(value):
            secret = True
        if secret:
            if isinstance(value, str) and value:
                secrets.append(value)
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted, secrets


def summarize_args(args: dict, *, limit: int = 160) -> str:
    """A short JSON summary of ``args`` with secrets removed.

    Keys that start with ``_`` are dropped. A card-shaped string and a
    secret-shaped key become ``[REDACTED]``. Long strings are cut before the
    JSON is cut, so the summary stays one short line.
    """
    public = {key: value for key, value in args.items() if not str(key).startswith("_")}
    redacted, _secrets = redact_args(public)
    compact: dict = {}
    for key, value in redacted.items():
        if isinstance(value, str) and value != "[REDACTED]" and len(value) > 80:
            compact[key] = value[:79] + "…"
        else:
            compact[key] = value
    text = json.dumps(compact, default=str, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def redact_text(text: str, secrets: list[str]) -> str:
    """Replace known secret strings and card-number-shaped runs."""
    out = text
    for secret in secrets:
        if len(secret) >= 8:
            out = out.replace(secret, "[REDACTED]")
    return _CARD_RE.sub("[REDACTED]", out)


class Trace:
    """Append-only trace directory for one run."""

    def __init__(self, directory: str | Path | None = None) -> None:
        if directory is None:
            self.dir = Path(tempfile.mkdtemp(prefix="a11y-agent-"))
        else:
            self.dir = Path(directory)
            self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.trajectory_path = self.dir / "trajectory.jsonl"
        self.steps_path = self.dir / "steps.jsonl"

    def save_png(self, index: int, png: bytes) -> str:
        path = self.dir / f"step-{index:04d}.png"
        path.write_bytes(png)
        return str(path)

    def append(self, entry: dict) -> None:
        """Append one object to ``trajectory.jsonl`` only."""
        line = json.dumps(entry, default=str, ensure_ascii=False)
        with self._lock:
            with self.trajectory_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def record(self, entry: dict, step: dict) -> None:
        line_entry = json.dumps(entry, default=str, ensure_ascii=False)
        line_step = json.dumps(step, default=str, ensure_ascii=False)
        with self._lock:
            with self.trajectory_path.open("a", encoding="utf-8") as handle:
                handle.write(line_entry + "\n")
            with self.steps_path.open("a", encoding="utf-8") as handle:
                handle.write(line_step + "\n")


__all__ = ["Trace", "redact_args", "redact_text", "summarize_args", "truncate_observation"]
