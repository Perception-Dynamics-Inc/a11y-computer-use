"""Conditions the agent (or a mission runner) can wait for.

``wait_until`` complements ``wait_for``: where ``wait_for`` polls one element
ref in the accessibility tree, ``wait_until`` polls things outside the tree
that long tasks depend on: a downloaded or rendered file, a deployed URL, a
line of text appearing in an app's snapshot, or text on screen (through OCR,
when the Runtime provides ``screen_text``).

Condition shapes (one key selects the kind; the rest are options)::

    {"file_exists": "~/Downloads/car.mp4", "min_bytes": 1000000}
    {"file_stable": "~/Movies/render.mp4", "seconds": 5, "min_bytes": 1}
    {"url_status": "https://example.com/", "status": 200}
    {"snapshot_text": "Render complete", "app": "com.adobe.AfterEffects"}
    {"screen_text": "Message sent"}

File paths may contain ``~`` and glob characters; the newest match wins. Paths
outside the user's home are refused unless ``A11Y_COMPUTER_USE_ALLOW_ANY_PATH=1``,
so a planner cannot use the checker to probe the file system.
"""

from __future__ import annotations

import glob
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from a11y_computer_use.schema import ComputerUseError, ErrorCode

#: The kinds ``wait_until`` understands; exactly one of these keys selects it.
KINDS = ("file_exists", "file_stable", "url_status", "snapshot_text", "screen_text")

#: Ceiling for a single wait (seconds). Renders and deploys take minutes;
#: nothing an agent waits for should take longer than half an hour.
MAX_WAIT_UNTIL_S = 1800.0


def kind_of(condition: object) -> str:
    if not isinstance(condition, dict):
        raise ValueError('condition must be an object such as {"file_exists": path}')
    kinds = [k for k in KINDS if k in condition]
    if len(kinds) != 1:
        raise ValueError(
            f"condition must have exactly one of {list(KINDS)}; got {sorted(condition)}"
        )
    return kinds[0]


def _allowed_path(raw: object) -> Path:
    path = Path(os.path.expanduser(str(raw)))
    if os.environ.get("A11Y_COMPUTER_USE_ALLOW_ANY_PATH") == "1":
        return path
    home = Path.home().resolve()
    candidate = path if path.is_absolute() else (Path.cwd() / path)
    try:
        candidate.resolve(strict=False).relative_to(home)
    except ValueError:
        raise ValueError(
            f"{raw!r} is outside the home directory; file conditions are limited to ~ "
            "(set A11Y_COMPUTER_USE_ALLOW_ANY_PATH=1 to lift this)"
        ) from None
    return path


def _newest_match(pattern: Path, min_bytes: int) -> Path | None:
    if glob.has_magic(str(pattern)):
        matches = [Path(p) for p in glob.glob(str(pattern))]
    else:
        matches = [pattern] if pattern.exists() else []
    files = [p for p in matches if p.is_file() and p.stat().st_size >= min_bytes]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return the 3xx as-is so every hop goes through the address check."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _public_host(url: str) -> None:
    """Refuse URLs whose host resolves to a non-public address.

    ``wait_until`` runs on the planner's request, and a planner can be steered
    by page content, so probing loopback, link-local (cloud metadata), private
    or multicast addresses would turn it into a reachability oracle for services
    on this machine and network. ``A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS=1`` opts
    out for local development servers.
    """
    if os.environ.get("A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS") == "1":
        return
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise ValueError("url_status needs a host")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError(f"url_status could not resolve {host!r}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
                or addr.is_reserved or addr.is_unspecified):
            raise ValueError(
                f"url_status refuses non-public address {addr} for {host!r} "
                "(set A11Y_COMPUTER_USE_ALLOW_LOCAL_URLS=1 for local servers)"
            )


def _url_status(url: object, timeout_s: float) -> int | None:
    target = str(url)
    if not target.startswith(("http://", "https://")):
        raise ValueError("url_status needs an http:// or https:// URL")
    _public_host(target)
    request = urllib.request.Request(target, method="GET", headers={"User-Agent": "a11y-computer-use"})
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=timeout_s) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError):
        return None


class Checker:
    """Evaluate one condition repeatedly until it holds or the deadline passes.

    ``snapshot_text`` and ``screen_text`` need callables that produce the text
    to search; the Runtime supplies gated ones, a mission runner supplies its own.
    """

    def __init__(
        self,
        *,
        snapshot_text: Callable[[str | None], str] | None = None,
        screen_text: Callable[[], str] | None = None,
    ) -> None:
        self._snapshot_text = snapshot_text
        self._screen_text = screen_text

    def probe(self, condition: dict, state: dict) -> str | None:
        """One evaluation. Returns a description of the match, or None."""
        kind = kind_of(condition)
        if kind == "file_exists":
            path = _newest_match(
                _allowed_path(condition["file_exists"]), int(condition.get("min_bytes", 1))
            )
            return f"file {path} ({path.stat().st_size} bytes)" if path else None
        if kind == "file_stable":
            seconds = float(condition.get("seconds", 5))
            path = _newest_match(
                _allowed_path(condition["file_stable"]), int(condition.get("min_bytes", 1))
            )
            if path is None:
                state.pop("stable", None)
                return None
            size = path.stat().st_size
            key = (str(path), size)
            if state.get("stable", (None, None))[0] != key:
                state["stable"] = (key, time.monotonic())
                return None
            if time.monotonic() - state["stable"][1] >= seconds:
                return f"file {path} stable at {size} bytes for {seconds:g}s"
            return None
        if kind == "url_status":
            wanted = int(condition.get("status", 200))
            status = _url_status(
                condition["url_status"],
                timeout_s=min(10.0, float(condition.get("request_timeout_s", 10.0))),
            )
            return f"{condition['url_status']} returned {status}" if status == wanted else None
        if kind == "snapshot_text":
            if self._snapshot_text is None:
                raise ComputerUseError(ErrorCode.UNSUPPORTED, "snapshot_text needs a Runtime")
            text = self._snapshot_text(condition.get("app"))
            needle = str(condition["snapshot_text"])
            return f"snapshot contains {needle!r}" if needle.lower() in text.lower() else None
        if kind == "screen_text":
            if self._screen_text is None:
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    "screen_text needs OCR (Runtime.screen_text), which this backend does not provide",
                )
            text = self._screen_text()
            needle = str(condition["screen_text"])
            return f"screen shows {needle!r}" if needle.lower() in text.lower() else None
        raise ValueError(kind)

    def wait(self, condition: dict, *, timeout_s: float = 600.0, poll_s: float = 2.0) -> dict:
        """Poll until the condition holds; raise ``timeout`` otherwise.

        Returns ``{"matched": description, "waited_s": seconds, "polls": n}``.
        """
        kind_of(condition)  # validate before waiting
        if not (0 <= timeout_s <= MAX_WAIT_UNTIL_S):
            raise ValueError(f"timeout_s must be between 0 and {MAX_WAIT_UNTIL_S:g}")
        if poll_s <= 0:
            raise ValueError("poll_s must be positive")
        started = time.monotonic()
        deadline = started + timeout_s
        state: dict = {}
        polls = 0
        while True:
            polls += 1
            matched = self.probe(condition, state)
            if matched is not None:
                return {
                    "matched": matched,
                    "waited_s": round(time.monotonic() - started, 2),
                    "polls": polls,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ComputerUseError(
                    ErrorCode.TIMEOUT,
                    f"condition not met within {timeout_s:g}s: {json.dumps(condition)}",
                    detail={
                        "condition": condition,
                        "waited_s": round(time.monotonic() - started, 2),
                        "polls": polls,
                    },
                )
            time.sleep(min(poll_s, remaining))


__all__ = ["Checker", "KINDS", "MAX_WAIT_UNTIL_S", "kind_of"]
