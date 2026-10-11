"""OCR word boxes and small-box geometry. The agent does not click from these.

``words_from_spans`` keeps one box per input word. ``match_ocr_word``,
``tree_for_labels``, and ``snap_point`` are exact-title and box helpers.
The agent loop does not call them to choose a click, and it does not read
the goal to name a target. A missing ref is resolved only from that ref's
last-known role and name, or from a label on the model's own call.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

#: Worker pools stay at this cap. A lower value already set in the environment
#: is left alone. Eight threads was slower under load than four.
MAX_WORKER_THREADS = 4

#: A Holo point snaps only into a box this small. A window or a line does not.
_SNAP_MAX_W = 200
_SNAP_MAX_H = 40

_THREAD_ENV = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS")

_QUOTED = re.compile(r'"([^"]+)"|\'([^\']+)\'')
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.-]{2,}")
_STOP = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this", "then",
    "click", "press", "open", "dont", "not", "button", "field", "menu",
    "item", "target", "please", "drawn", "canvas", "inside", "named",
    "called", "just", "only", "when", "green", "blue", "red",
})

_MATCH_MIN = 0.8


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One word in screen pixels. A value with whitespace is not a word."""

    text: str
    x: int
    y: int
    w: int
    h: int
    display_id: int = 0

    @property
    def area(self) -> int:
        return max(1, self.w) * max(1, self.h)

    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


@dataclass(frozen=True, slots=True)
class TreeBox:
    """One accessibility element the pointer may snap to."""

    ref: str
    title: str
    x: int
    y: int
    w: int
    h: int
    display_id: int = 0

    @property
    def area(self) -> int:
        return max(1, self.w) * max(1, self.h)

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


def cap_worker_threads(limit: int = MAX_WORKER_THREADS) -> int:
    """Cap OpenMP, BLAS, and a torch that is already imported.

    Does not import torch. ``limit`` above 4 is capped at 4. A pool already
    set lower than the cap is not raised.
    """
    count = MAX_WORKER_THREADS
    if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit < count:
        count = limit
    existing: list[int] = []
    for name in _THREAD_ENV:
        raw = os.environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        try:
            current = int(str(raw).strip())
        except ValueError:
            continue
        if current >= 1:
            existing.append(current)
    if existing:
        count = min(count, min(existing))
    for name in _THREAD_ENV:
        raw = os.environ.get(name)
        try:
            current = int(str(raw).strip()) if raw is not None and str(raw).strip() else None
        except ValueError:
            current = None
        if current != count:
            os.environ[name] = str(count)
    torch = sys.modules.get("torch")
    setter = getattr(torch, "set_num_threads", None) if torch is not None else None
    if callable(setter):
        setter(count)
    return count


def labels_from_instruction(text: str) -> list[str]:
    """Labels the instruction names: quoted phrases, then other tokens.

    Generic verbs (``click``, ``button``, …) are not labels. Order is the
    order they appear. Duplicates are dropped.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        cleaned = label.strip()
        key = _norm(cleaned)
        if not cleaned or not key or key in seen:
            return
        seen.add(key)
        found.append(cleaned)

    for match in _QUOTED.finditer(text or ""):
        add(match.group(1) or match.group(2) or "")
    for token in _TOKEN.findall(text or ""):
        if token.casefold() in _STOP:
            continue
        add(token)
    return found


def words_from_spans(spans: object) -> list[OcrWord]:
    """Word boxes from ``ocr`` spans. A box whose text has whitespace is a line.

    ``words`` on a span, when present, are the input boxes. The span's own
    bounds are the line union and are not returned. A span with no ``words``
    list is kept only when its text is a single word.
    """
    if not isinstance(spans, (list, tuple)):
        return []
    words: list[OcrWord] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        nested = span.get("words")
        items: Sequence[object]
        if isinstance(nested, (list, tuple)) and nested:
            items = nested
        else:
            items = (span,)
        for item in items:
            word = _word_from_item(item)
            if word is not None:
                words.append(word)
    return words


def match_ocr_word(words: Sequence[OcrWord], labels: Sequence[str]) -> OcrWord | None:
    """The word whose line matches ``labels``. Never the line's union box.

    A line matches at 1.0 when the text is the label, at 0.9 when the label
    (at least 3 characters) sits inside the line, otherwise by sequence
    ratio. The threshold is 0.8. The click is a word on that line, never the
    line's union. A word at or above the same threshold wins. Otherwise the
    highest-scoring word on the matching line is the target. Smaller boxes
    win ties, then the earlier word.
    """
    if not words or not labels:
        return None
    best_line: tuple[float, int, list[OcrWord]] | None = None
    for line in _group_lines(words):
        text = " ".join(word.text for word in line)
        score = max(_score(label, text) for label in labels)
        area = sum(word.area for word in line)
        if score < _MATCH_MIN:
            continue
        if best_line is None or score > best_line[0] or (score == best_line[0] and area < best_line[1]):
            best_line = (score, area, line)
    if best_line is None:
        return None
    chosen: tuple[tuple[float, int, int], OcrWord] | None = None
    for index, word in enumerate(best_line[2]):
        score = max(_score(label, word.text) for label in labels)
        # A word that itself clears the threshold outranks a weaker token
        # on the same line, even when that weaker token is smaller.
        rank = (1.0 if score >= _MATCH_MIN else 0.0, score, -word.area, -index)
        if chosen is None or rank > chosen[0]:
            chosen = (rank, word)
    if chosen is None:
        return None
    return chosen[1]


def tree_for_labels(boxes: Sequence[TreeBox], labels: Sequence[str]) -> TreeBox | None:
    """The smallest element whose title is a label. Opaque regions are not passed in."""
    if not boxes or not labels:
        return None
    wanted = {_norm(label) for label in labels if _norm(label)}
    hits = [box for box in boxes if _norm(box.title) in wanted and box.ref]
    if not hits:
        return None
    hits.sort(key=lambda box: (box.area, box.ref))
    return hits[0]


def snap_point(
    x: int,
    y: int,
    tree: Sequence[TreeBox],
    words: Sequence[OcrWord],
) -> tuple[str, int, int, int] | None:
    """Snap ``(x, y)`` to the smallest small tree box or word box that contains it.

    Returns ``("ref", x, y, display)`` or ``("word", x, y, display)``. A box
    wider than 200 or taller than 40 is not a target. None when the point is
    in no such box. The coordinates are that box's center.
    """
    inside: list[tuple[int, str, int, int, int]] = []
    for box in tree:
        if box.w > _SNAP_MAX_W or box.h > _SNAP_MAX_H or not box.contains(x, y):
            continue
        cx, cy = box.x + box.w // 2, box.y + box.h // 2
        inside.append((box.area, "ref", cx, cy, box.display_id))
    for word in words:
        if word.w > _SNAP_MAX_W or word.h > _SNAP_MAX_H or not word.contains(x, y):
            continue
        cx, cy = word.center()
        inside.append((word.area, "word", cx, cy, word.display_id))
    if not inside:
        return None
    inside.sort(key=lambda item: item[0])
    _area, kind, cx, cy, display = inside[0]
    return kind, cx, cy, display


def _word_from_item(item: object) -> OcrWord | None:
    if not isinstance(item, dict):
        return None
    text = _plain(item.get("text"))
    if not text or any(ch.isspace() for ch in text):
        return None
    box = item.get("bounds")
    if not isinstance(box, dict):
        return None
    try:
        return OcrWord(
            text=text,
            x=int(box["x"]),
            y=int(box["y"]),
            w=max(1, int(box["width"])),
            h=max(1, int(box["height"])),
            display_id=int(box.get("display_id", 0) or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _plain(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("<untrusted"):
        from a11y_computer_use.untrusted import unwrap

        inner = unwrap(text)
        if inner is not None:
            return inner.strip()
    return text


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _score(label: str, text: str) -> float:
    left = _norm(label)
    right = _norm(text)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if len(left) >= 3 and left in right:
        return 0.9
    return difflib.SequenceMatcher(None, right, left).ratio()


def _group_lines(words: Sequence[OcrWord]) -> list[list[OcrWord]]:
    ordered = sorted(words, key=lambda word: (word.y + word.h / 2, word.x))
    lines: list[list[OcrWord]] = []
    for word in ordered:
        placed = False
        for line in lines:
            last = line[-1]
            height = max(1, min(last.h, word.h))
            same_row = abs((last.y + last.h / 2) - (word.y + word.h / 2)) <= height / 2
            gap = word.x - (last.x + last.w)
            if same_row and -height / 2 <= gap <= 0.8 * height:
                line.append(word)
                placed = True
                break
        if not placed:
            lines.append([word])
    return lines
