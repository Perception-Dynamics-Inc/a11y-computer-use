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


def refusal() -> ComputerUseError:
    return ComputerUseError(ErrorCode.APP_NOT_RESPONDING, UNRESPONSIVE,
                            detail={"reason": "page_not_responding", "next": [],
                                    "outcome": "unverifiable", "evidence": UNRESPONSIVE})
