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
    {"settle": 8}

``settle`` is the one condition with nothing to observe: it holds once the
given seconds have passed. It exists for apps that show nothing checkable
while they load or animate (a Qt app with no accessibility tree); prefer an
observable condition wherever one exists.

``file_exists`` and ``file_stable`` match regular files only. ``min_bytes``
(default 1) applies to those files. A path that already exists and is not a
regular file, or a glob whose matches are all non-files, raises ``ValueError``
on the first look (the MCP layer reports that as ``invalid_arguments``). A
directory never counts, and the call does not wait out ``timeout_s``. A path
that is not there yet keeps waiting, so a download can still appear.

File paths may contain ``~`` and glob characters; the newest regular file
wins. Paths outside the user's home are refused unless
``A11Y_COMPUTER_USE_ALLOW_ANY_PATH=1``, so a planner cannot use the checker
to probe the file system.

A timeout's ``detail`` keeps ``condition``, ``waited_s``, and ``polls``, and
adds the last observation: ``last_status`` or ``last_error`` for a URL;
``exists``, ``path``, ``last_size``, and ``min_bytes`` for a file (plus
``stable_for_s`` while a ``file_stable`` candidate is being watched);
``found`` for snapshot and screen text; ``elapsed_s`` and ``settle_s`` for
``settle``.
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
KINDS = ("file_exists", "file_stable", "url_status", "snapshot_text", "screen_text", "settle")

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


class _FileLook:
    """One look at a file condition: the qualifying file, or why there isn't one."""

    def __init__(
        self,
        match: Path | None,
        exists: bool,
        path: str | None,
        last_size: int | None,
        non_files: tuple[Path, ...] = (),
    ) -> None:
        self.match = match
        self.exists = exists
        self.path = path
        self.last_size = last_size
        self.non_files = non_files


def _scan_files(pattern: Path, min_bytes: int) -> _FileLook:
    """Newest regular file of at least ``min_bytes``, plus what else is there.

    A directory, or a glob that only hits non-files, is reported in
    ``non_files`` so the caller can fail immediately. A regular file that is
    still smaller than ``min_bytes`` is reported by size and does not qualify.
    """
    if glob.has_magic(str(pattern)):
        raw = [Path(p) for p in glob.glob(str(pattern))]
    else:
        raw = [pattern] if pattern.exists() else []
    files: list[Path] = []
    non_files: list[Path] = []
    for path in raw:
        try:
            if not path.exists():
                continue
            if path.is_file():
                files.append(path)
            else:
                non_files.append(path)
        except OSError:
            non_files.append(path)
    if files:
        qualified = [path for path in files if path.stat().st_size >= min_bytes]
        match = max(qualified, key=lambda path: path.stat().st_mtime) if qualified else None
        reported = match or max(files, key=lambda path: path.stat().st_mtime)
        return _FileLook(match, True, str(reported), reported.stat().st_size)
    if non_files:
        shown = min(non_files, key=lambda path: str(path))
        return _FileLook(None, True, str(shown), None, tuple(non_files))
    return _FileLook(None, False, None, None)


def _not_a_file_message(kind: str, pattern: Path, non_files: tuple[Path, ...]) -> str:
    """The path is there, and it is not something ``file_exists`` can match."""
    names = sorted({str(path) for path in non_files})
    rule = f"{kind} only matches regular files"
    if not glob.has_magic(str(pattern)) and len(names) == 1:
        return f"{names[0]!r} exists but is not a regular file; {rule}"
    listed = ", ".join(repr(name) for name in names[:4])
    if len(names) > 4:
        listed += f", and {len(names) - 4} more"
    if len(names) == 1:
        return f"{str(pattern)!r} matches {listed}, which is not a regular file; {rule}"
    return f"{str(pattern)!r} matches {listed}, which are not regular files; {rule}"


def _file_observation(look: _FileLook, min_bytes: int, *, stable_for_s: float | None = None) -> dict[str, object]:
    last: dict[str, object] = {
        "exists": look.exists,
        "path": look.path,
        "last_size": look.last_size,
        "min_bytes": min_bytes,
    }
    if stable_for_s is not None:
        last["stable_for_s"] = round(stable_for_s, 2)
    return last


def _reject_non_file(kind: str, pattern: Path, look: _FileLook) -> None:
    if look.non_files and look.match is None and look.last_size is None:
        raise ValueError(_not_a_file_message(kind, pattern, look.non_files))


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


def _connection_error_text(exc: urllib.error.URLError) -> str:
    """Class and message of a failed GET, for a timeout's last observation."""
    reason = exc.reason
    if isinstance(reason, BaseException):
        return f"{type(reason).__name__}: {reason}"
    return f"{type(exc).__name__}: {reason}"


def _url_observation(url: object, timeout_s: float) -> tuple[int | None, str | None]:
    """``(status, None)`` when the server answered, else ``(None, error text)``.

    ``HTTPError`` is an answer (a 404 is ``last_status``, not a connection
    error). A refused or dropped connection is ``last_error``.
    """
    target = str(url)
    if not target.startswith(("http://", "https://")):
        raise ValueError("url_status needs an http:// or https:// URL")
    _public_host(target)
    request = urllib.request.Request(target, method="GET", headers={"User-Agent": "a11y-computer-use"})
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=timeout_s) as response:
            return int(response.status), None
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except urllib.error.URLError as exc:
        return None, _connection_error_text(exc)
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _url_status(url: object, timeout_s: float) -> int | None:
    status, _error = _url_observation(url, timeout_s)
    return status


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
        if kind == "settle":
            seconds = float(condition["settle"])
            if not (0 <= seconds <= MAX_WAIT_UNTIL_S):
                raise ValueError(f"settle must be between 0 and {MAX_WAIT_UNTIL_S:g} seconds")
            started = state.setdefault("settle_started", time.monotonic())
            elapsed = time.monotonic() - started
            state["last"] = {"elapsed_s": round(elapsed, 2), "settle_s": seconds}
            return f"settled for {seconds:g}s" if elapsed >= seconds else None
        if kind == "file_exists":
            pattern = _allowed_path(condition["file_exists"])
            min_bytes = int(condition.get("min_bytes", 1))
            look = _scan_files(pattern, min_bytes)
            _reject_non_file("file_exists", pattern, look)
            state["last"] = _file_observation(look, min_bytes)
            path = look.match
            return f"file {path} ({path.stat().st_size} bytes)" if path else None
        if kind == "file_stable":
            seconds = float(condition.get("seconds", 5))
            pattern = _allowed_path(condition["file_stable"])
            min_bytes = int(condition.get("min_bytes", 1))
            look = _scan_files(pattern, min_bytes)
            _reject_non_file("file_stable", pattern, look)
            path = look.match
            if path is None:
                state.pop("stable", None)
                state["last"] = _file_observation(look, min_bytes)
                return None
            size = path.stat().st_size
            key = (str(path), size)
            established = state.get("stable", (None, None))[0] != key
            if established:
                state["stable"] = (key, time.monotonic())
                held = 0.0
            else:
                held = time.monotonic() - state["stable"][1]
            state["last"] = _file_observation(look, min_bytes, stable_for_s=held)
            # The poll that first sees this size does not count as already stable.
            if not established and held >= seconds:
                return f"file {path} stable at {size} bytes for {seconds:g}s"
            return None
        if kind == "url_status":
            wanted = int(condition.get("status", 200))
            status, error = _url_observation(
                condition["url_status"],
                timeout_s=min(10.0, float(condition.get("request_timeout_s", 10.0))),
            )
            if error is not None:
                state["last"] = {"last_error": error}
            else:
                state["last"] = {"last_status": status}
            return f"{condition['url_status']} returned {status}" if status == wanted else None
        if kind == "snapshot_text":
            if self._snapshot_text is None:
                raise ComputerUseError(ErrorCode.UNSUPPORTED, "snapshot_text needs a Runtime")
            text = self._snapshot_text(condition.get("app"))
            needle = str(condition["snapshot_text"])
            found = needle.lower() in text.lower()
            state["last"] = {"found": found, "snapshot_chars": len(text)}
            return f"snapshot contains {needle!r}" if found else None
        if kind == "screen_text":
            if self._screen_text is None:
                raise ComputerUseError(
                    ErrorCode.UNSUPPORTED,
                    "screen_text needs OCR (Runtime.screen_text), which this backend does not provide",
                )
            text = self._screen_text()
            needle = str(condition["screen_text"])
            found = needle.lower() in text.lower()
            state["last"] = {"found": found, "screen_chars": len(text)}
            return f"screen shows {needle!r}" if found else None
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
                detail: dict[str, object] = {
                    "condition": condition,
                    "waited_s": round(time.monotonic() - started, 2),
                    "polls": polls,
                }
                detail.update(state.get("last") or {})
                raise ComputerUseError(
                    ErrorCode.TIMEOUT,
                    f"condition not met within {timeout_s:g}s: {json.dumps(condition)}",
                    detail=detail,
                )
            time.sleep(min(poll_s, remaining))


__all__ = ["Checker", "KINDS", "MAX_WAIT_UNTIL_S", "kind_of"]
