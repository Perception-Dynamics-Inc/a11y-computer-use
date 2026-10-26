"""Read-only renderer liveness for an already opted-in local Chrome process.

AT-SPI reads in Chromium are served by the browser's accessibility cache,
not the renderer. A missing CDP endpoint is unknown, never a healthy page.
"""
from __future__ import annotations

import json
import time
import urllib.request

from a11y_computer_use.schema import ComputerUseError, ErrorCode

BUDGET = 0.25
UNKNOWN = ("page responsiveness is unverified; the page may not be responding; "
           "input may be queued; re-observe before retrying")
STALE_TREE = ("page responsiveness is unverified; accessibility tree may be stale if the page "
              "is not responding; re-observe before retrying input")
UNRESPONSIVE = ("page not responding to a renderer probe; accessibility tree may be stale; "
                "input may be queued; re-observe before retrying")


def probe(pid: int | None, title: str, url: str | None = None) -> bool | None:
    """True for an answered renderer read, False for its timeout, else unknown.

    Bind by exact document URL and/or window title, never by tab ordering.
    Discovery/connection failures do not diagnose a renderer hang. No input,
    focus, script interruption, browser launch or persistent cache is used.
    """
    from . import _cdp, _cdp_forms

    if type(pid) is not int or pid <= 0:
        return None
    deadline = time.monotonic() + BUDGET
    session = None
    transport = None
    try:
        port = _cdp_forms.port_for_pid(pid)
        if port is None:
            return None
        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            return left
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=remaining()) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None
        pages = json.loads(raw)
        if not isinstance(pages, list):
            return None
        candidates = []
        for page in pages:
            if not isinstance(page, dict) or page.get("type") != "page":
                continue
            name = page.get("title")
            matches_title = isinstance(name, str) and bool(name) and (
                title == name or any(title.startswith(name + sep) for sep in (" - ", " — ", " – "))
            )
            if (url and page.get("url") == url or not url and matches_title):
                candidates.append(page)
        if len(candidates) != 1:
            return None
        socket = candidates[0].get("webSocketDebuggerUrl")
        if not isinstance(socket, str) or not socket.startswith(f"ws://127.0.0.1:{port}/"):
            return None
        transport = _cdp.connect(socket, timeout=remaining())
        session = _cdp.CDPSession(transport, default_timeout=remaining())
        try:
            reply = session.call("Runtime.evaluate", {"expression": "1", "returnByValue": True}, timeout=remaining())
        except ComputerUseError as exc:
            if exc.code is ErrorCode.TIMEOUT and exc.detail.get("sent") is True:
                return False
            return None
        result = reply.get("result")
        return True if isinstance(result, dict) and result.get("value") == 1 else None
    except (OSError, ValueError, ComputerUseError):
        return None
    finally:
        if session is not None:
            session.close()
        elif transport is not None:
            transport.close()


def chromium_process(pid: int | None) -> bool:
    """True when ``pid`` runs a Chromium-family binary (Chrome, Chromium, Electron).

    Reads /proc only. Snapshot-time AT-SPI reads on the document after the
    walk perturbed Chromium's row pool binding (stale row identity, #1138).
    """
    import os

    if type(pid) is not int or pid <= 0:
        return False
    try:
        exe = os.path.realpath(f"/proc/{pid}/exe")
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as handle:
            comm = handle.read().strip().lower()
    except OSError:
        return False
    base = os.path.basename(exe).lower()
    if "chrom" in base or "chrom" in comm:
        return True
    # Electron and other Chromium embedders ship Chromium's resource packs.
    return os.path.isfile(os.path.join(os.path.dirname(exe), "chrome_100_percent.pak"))


#: Share of wall time a renderer must spend on CPU between the before and
#: after retry reads for the page to look hung (a busy loop reads ~1.0; an
#: idle or merely repainting renderer reads well under 0.1).
BUSY_SHARE = 0.6
BUSY = ("page renderer was busy throughout the action (it may not be responding); "
        "accessibility tree may be stale; input may be queued; re-observe before retrying")


def renderer_ticks(pid: int | None) -> dict[int, int] | None:
    """CPU clock ticks (user+system) of each Chromium renderer under ``pid``.

    Reads /proc only: no CDP, no input, no AT-SPI. A renderer stuck in a
    script busy loop keeps answering nothing over CDP, while the browser's
    accessibility cache keeps answering AT-SPI reads, so its own CPU use is
    the remaining readable liveness signal (#1092). None means unknown.
    """
    import os

    if type(pid) is not int or pid <= 0:
        return None
    ticks: dict[int, int] = {}
    pending, seen = [pid], set()
    try:
        while pending and len(seen) < 512:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            try:
                with open(f"/proc/{current}/task/{current}/children", encoding="ascii") as handle:
                    pending.extend(int(child) for child in handle.read().split())
            except FileNotFoundError:
                if current == pid:
                    return None
                continue
            if current == pid:
                continue
            try:
                with open(f"/proc/{current}/cmdline", "rb") as handle:
                    if b"--type=renderer" not in handle.read():
                        continue
                with open(f"/proc/{current}/stat", encoding="ascii", errors="replace") as handle:
                    fields = handle.read().rsplit(")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError, IndexError):
                continue
            ticks[current] = int(fields[11]) + int(fields[12])
    except (OSError, ValueError):
        return None
    return ticks


def renderer_busy(before: tuple[float, dict[int, int]] | None,
                  after: tuple[float, dict[int, int]] | None) -> bool | None:
    """True when one renderer ran on CPU for most of the interval, else False/None."""
    import os

    if before is None or after is None:
        return None
    elapsed = after[0] - before[0]
    if elapsed < 0.3:
        return None  # Too short for 10 ms clock ticks to separate busy from idle.
    try:
        hertz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return None
    shared = set(before[1]) & set(after[1])
    if not shared:
        return None
    return any((after[1][pid] - before[1][pid]) / hertz >= BUSY_SHARE * elapsed for pid in shared)


def refusal() -> ComputerUseError:
    return ComputerUseError(ErrorCode.APP_NOT_RESPONDING, UNRESPONSIVE,
                            detail={"reason": "page_not_responding", "next": [],
                                    "outcome": "unverifiable", "evidence": UNRESPONSIVE})
