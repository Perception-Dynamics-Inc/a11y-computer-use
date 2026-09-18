"""The agent's scratchpad: short facts that must outlive context compaction.

A long task produces facts the planner needs later (a downloaded file's path,
a deployed URL, an id copied from one app into another). The conversation
history is bounded, so those facts would be lost; notes are kept by the
Runtime, persisted next to the audit log, and injected into every planner turn
by the agent loop.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

MAX_NOTES = 200
MAX_NOTE_CHARS = 2000


class NoteStore:
    """Ordered notes, persisted as JSON at ``path`` when one is given."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._notes: list[dict] = []
        if path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    self._notes = [n for n in loaded if isinstance(n, dict) and "text" in n]
            except (OSError, ValueError):
                self._notes = []

    def add(self, text: str, *, source: str = "agent") -> dict:
        text = str(text).strip()
        if not text:
            raise ValueError("a note needs text")
        with self._lock:
            if len(self._notes) >= MAX_NOTES:
                raise ValueError(f"note store is full ({MAX_NOTES}); clear it first")
            note = {
                "n": len(self._notes) + 1,
                "ts": time.time(),
                "source": source,
                "text": text[:MAX_NOTE_CHARS],
            }
            self._notes.append(note)
            self._persist()
        return dict(note)

    def clear(self) -> int:
        with self._lock:
            count = len(self._notes)
            self._notes = []
            self._persist()
        return count

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(n) for n in self._notes]

    def render(self) -> str:
        """The notes as numbered lines (what the planner sees)."""
        notes = self.all()
        if not notes:
            return "(no notes)"
        return "\n".join(f"{n['n']}. {n['text']}" for n in notes)

    def _persist(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._notes, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # notes are a convenience; never fail an action over disk


__all__ = ["MAX_NOTES", "MAX_NOTE_CHARS", "NoteStore"]
