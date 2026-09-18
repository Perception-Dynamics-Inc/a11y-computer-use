"""LIVE end-to-end smoke test: drive the real TextEdit through the full
observe -> act -> verify loop (PLAN.md §9 Phase 0 exit criterion: one
workflow completed end-to-end via element refs alone).

The whole module is skipif-gated on the Accessibility TCC grant: on an
ungranted machine every test here skips, and the graceful permission-error
path is covered by tests/e2e/test_mcp_stdio.py and tests/test_cli.py. Grant
Accessibility to the host app named by ``a11y_computer_use doctor`` and this suite
arms itself automatically — no code changes needed.

Scope discipline (non-negotiable): every click and keystroke targets a
TextEdit document backed by a pytest temp file this test created. Before
every injected action the test re-checks that TextEdit is frontmost and
aborts rather than act on whatever else grabbed focus.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.schema import ComputerUseError, Element, Snapshot
from tests.conftest import HAS_AX, HAS_DISPLAYS

pytestmark = [
    pytest.mark.skipif(not HAS_DISPLAYS, reason="needs an unlocked window-server session reporting a display"),
    pytest.mark.skipif(
        not HAS_AX,
        reason="live smoke test needs the Accessibility TCC grant (run `a11y-computer-use doctor`)",
    ),
]

TEXTEDIT = "com.apple.TextEdit"
SMOKE_TEXT = "Hello from a11y-computer-use MVP"
WINDOW_TIMEOUT_S = 20.0
VERIFY_TIMEOUT_S = 10.0
POLL_S = 0.5


@pytest.fixture
def runtime(tmp_path: Path) -> server.Runtime:
    """A Runtime with TextEdit pre-granted FULL in a throwaway store + audit.

    The real ``~/.a11y_computer_use`` is never touched: grants and the JSONL audit
    log live under pytest's temp dir and evaporate with it.
    """
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(TEXTEDIT, safety.Tier.FULL)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"))


def _frontmost_is_textedit() -> bool:
    bundle, _pid = safety.frontmost_app()
    return bundle == TEXTEDIT


def _require_frontmost() -> None:
    """Refuse to inject input into any app that stole focus mid-test."""
    if not _frontmost_is_textedit():
        pytest.fail(f"aborting before input injection: frontmost app is not {TEXTEDIT}")


def _refocus(rt: server.Runtime) -> None:
    """Re-assert TextEdit focus right before an action, so a concurrent app
    (e.g. a browser stealing the foreground) doesn't abort the smoke test."""
    rt.app("focus", TEXTEDIT)
    time.sleep(0.3)


def _snapshot_epoch(rt: server.Runtime) -> Snapshot:
    """Fresh snapshot through the runtime, returned as the structured tree.

    ``Runtime.desktop_snapshot`` returns rendered text for the model and
    parks the live epoch on ``_current``; the test wants the elements.
    """
    rt.desktop_snapshot(TEXTEDIT)
    assert rt._current is not None
    return rt._current


def _find_text_area(snap: Snapshot) -> Element | None:
    return next(
        (el for el in snap.elements if el.role == "AXTextArea" and el.editable), None
    )


def _wait_for_document(rt: server.Runtime, title_marker: str) -> Element:
    """Poll until our smoke document is frontmost-window-snapshottable.

    Retries APP_NOT_FOUND while Launch Services is still starting TextEdit,
    and keeps polling while some *other* TextEdit window is in front.
    """
    deadline = time.monotonic() + WINDOW_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            # Best effort: on a CI Mac without a user session in front, focus
            # honestly reports focus_changed; the snapshot does not need it.
            rt.app("focus", TEXTEDIT)
        except ComputerUseError:
            pass
        try:
            snap = _snapshot_epoch(rt)
        except ComputerUseError:
            time.sleep(POLL_S)
            continue
        area = _find_text_area(snap)
        titles = [el.title or "" for el in snap.elements if el.role == "AXWindow"]
        if area is not None and any(title_marker in t for t in titles):
            return area
        time.sleep(POLL_S)
    pytest.fail(f"TextEdit window for {title_marker!r} not snapshottable in {WINDOW_TIMEOUT_S}s")


def _ax_verify_text(rt: server.Runtime, expected: str) -> None:
    """The pass condition: the typed text is present in the live AX tree."""
    deadline = time.monotonic() + VERIFY_TIMEOUT_S
    while time.monotonic() < deadline:
        area = _find_text_area(_snapshot_epoch(rt))
        if area is not None and expected in (area.value or ""):
            return
        time.sleep(POLL_S)
    pytest.fail(f"typed text {expected!r} never appeared in the AX tree")


def _assert_audited(audit_dir: Path) -> None:
    """The always-on JSONL audit log recorded the click and the typing."""
    entries = [
        json.loads(line)
        for path in sorted(audit_dir.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    kinds = {entry["action"] for entry in entries}
    assert {"click", "typetext"} <= kinds, f"audit log missing actions: {kinds}"


def _close_smoke_window(rt: server.Runtime) -> None:
    """Best-effort teardown: save (into our own temp file) and close.

    Every chord is preceded by a frontmost check; if focus moved, the window
    is left open for the human rather than risk keying another app. Failures
    here never mask the test result.
    """
    try:
        rt.app("focus", TEXTEDIT)
        time.sleep(0.5)
        if not _frontmost_is_textedit():
            return
        rt.key("cmd+s")  # writes to the pytest temp file this test owns
        time.sleep(1.0)
        if not _frontmost_is_textedit():
            return
        rt.key("cmd+w")
    except Exception:
        pass


def test_textedit_type_and_ax_verify(runtime: server.Runtime, tmp_path: Path) -> None:
    doc = tmp_path / "a11y_computer_use-smoke.txt"
    doc.write_text("")
    # Open OUR temp file (plain text, no prompts) so every subsequent
    # keystroke lands in a document this test created and owns.
    subprocess.run(["open", "-a", "TextEdit", str(doc)], check=True, timeout=15)
    try:
        area = _wait_for_document(runtime, doc.name)

        _refocus(runtime)
        _require_frontmost()
        runtime.click(ref=area.ref)  # element-ref click: the flagship path

        _refocus(runtime)
        _require_frontmost()
        runtime.type_text(SMOKE_TEXT)

        _ax_verify_text(runtime, SMOKE_TEXT)
        _assert_audited(tmp_path / "audit")
    finally:
        _close_smoke_window(runtime)
