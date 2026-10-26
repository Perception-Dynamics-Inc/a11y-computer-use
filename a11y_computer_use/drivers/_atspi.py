"""AT-SPI2 adapter: presents the Linux accessibility tree to the SHARED,
platform-free pruning engine (`observe.build_snapshot`).

Same trick as the Windows adapter (`_uia.py`): map AT-SPI role names onto the
**same AX role vocabulary** the pruning engine keys off (`AXButton`,
`AXTextField`, `AXWindow`, ...) and AT-SPI action names onto the AX action names
(`AXPress`/`AXPick`). Then a Linux tree prunes/indexes through the identical
engine as macOS and Windows, with zero engine changes.

Binding: PyGObject's `gi.repository.Atspi` (the modern AT-SPI2 client). Imported
lazily so `drivers` stays import-safe on macOS/Windows. Every attribute read is
defensive — AT-SPI is a D-Bus protocol with no batch read, so any single call
can be slow or fail cross-process, and a flaky read must degrade to a sane
default, not crash the walk. (The batch/prefetch weakness AT-SPI has vs UIA's
`CacheRequest` / AX's multi-attribute reads is why we cap the fetched children:
see `_MAX_CHILDREN_FETCH`. Duplicate stable getters inside one
`LinuxDriver._run` are reused by `read_cache`; a mutator drops that cache.
A subtree cache/diff across calls is still the follow-up.)

Linux-only; imported lazily by `drivers/linux.py`.
"""

from __future__ import annotations

import html
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from functools import lru_cache
from contextlib import contextmanager

from a11y_computer_use.observe import (
    MAX_CHILDREN,
    SHEET_MAX_CHILDREN,
    DisplayGeometry,
    RawNode,
    _generic_region_name,
    normalize_document_title,
)
from a11y_computer_use.schema import ComputerUseError, Display, ErrorCode

# AT-SPI role name (english, from get_role_name()) -> canonical AX role.
# Keyed by the human role-name string rather than the numeric Atspi.Role enum
# because the strings are stable across atspi2 versions and bindings.
_ROLE = {
    "push button": "AXButton",
    "push button menu": "AXButton",
    "toggle button": "AXButton",
    "spin button": "AXTextField",
    "button": "AXButton",
    "check box": "AXCheckBox",
    "check menu item": "AXMenuItem",
    "radio button": "AXRadioButton",
    "radio menu item": "AXMenuItem",
    "link": "AXLink",
    "entry": "AXTextField",
    "password text": "AXSecureTextField",
    "text": "AXTextArea",
    "document text": "AXTextArea",
    "terminal": "AXTextArea",  # VTE (gnome-terminal, tilix): its Text iface is the screen contents
    # ATSPI_ROLE_PAGE. Evince puts the PDF page on this role: an empty name
    # and a Text interface that holds the page. The AXGroup fallback never
    # reads that text, and the pruner then publishes the page as an unnamed
    # opaque_region. AXStaticText is not an editable field. A blank page is
    # put back to a group while reading, so the box is not dropped.
    "page": "AXStaticText",
    "document frame": "AXGroup",
    "document web": "AXGroup",
    "document email": "AXGroup",
    # A cross-origin iframe (reCAPTCHA's "I'm not a robot" lives in one).
    # Explicit, not the AXGroup fallback: the pruner restarts kept depth here.
    "internal frame": "AXGroup",
    "label": "AXStaticText",
    "static": "AXStaticText",
    "heading": "AXHeading",
    "paragraph": "AXStaticText",
    "caption": "AXStaticText",
    "image": "AXImage",
    "icon": "AXImage",
    # A canvas or GTK drawing area has bounds and no accessible children.
    # The pruner publishes it as opaque_region. WebGL is a canvas.
    "canvas": "AXCanvas",
    "drawing area": "AXCanvas",
    "frame": "AXWindow",
    "window": "AXWindow",
    "dialog": "AXDialog",
    "alert": "AXDialog",
    # ATSPI_ROLE_FILE_CHOOSER is a dialog (Ristretto Open, GTK FileChooserDialog).
    # AXWindow rendered it as a document window. The name is the title; the
    # Value interface is not the chosen path, so the value probe stays off.
    "file chooser": "AXDialog",
    "color chooser": "AXWindow",
    "calendar": "AXCalendar",
    "menu item": "AXMenuItem",
    "menu bar": "AXMenuBar",
    "menu": "AXMenu",
    "popup menu": "AXMenu",
    "list": "AXList",
    "list box": "AXList",
    "list item": "AXRow",
    "tree": "AXOutline",
    "tree table": "AXOutline",
    "tree item": "AXRow",
    "table": "AXTable",
    # LibreOffice Calc. The name is the address (A1). AXGroup is in
    # _NO_VALUE_ROLES, so mapping this to a group hid the cell text.
    # A GTK file chooser's body cells use the same role and stay in the
    # snapshot by name, via `_with_table_body`. Qt uses the same role for a
    # QTableWidget cell and a QTreeWidget item.
    "table cell": "AXCell",
    "table column header": "AXColumn",
    "table row header": "AXRow",
    # Chromium <th> exposes these native header roles, which are distinct
    # from TABLE_*_HEADER. Its static-text child is not always published.
    "column header": "AXColumn",
    "row header": "AXRow",
    "table row": "AXRow",
    "combo box": "AXComboBox",
    "tool bar": "AXToolbar",
    "tool tip": "AXTooltip",
    "scroll bar": "AXScrollBar",
    "slider": "AXSlider",  # Gtk.Scale and friends: draggable, carries a Value iface
    "progress bar": "AXProgressIndicator",
    "level bar": "AXProgressIndicator",
    # Ristretto's image metadata bar is ATSPI_ROLE_STATUS_BAR. The name is
    # the status text. AXGroup, the unmapped fallback, hid it among layout.
    "status bar": "AXStatusBar",
    "scroll pane": "AXScrollArea",
    "viewport": "AXScrollArea",
    "page tab list": "AXTabGroup",
    "page tab": "AXTab",
    "panel": "AXGroup",
    "filler": "AXGroup",
    "section": "AXGroup",
    "grouping": "AXGroup",
    "redundant object": "AXUnknown",
    "separator": "AXSplitter",
    "unknown": "AXUnknown",
}

# AT-SPI role name -> RawNode.atspi_web. Only Chromium document/frame roles.
# Anything else stays "" so the pruner's macOS/Windows path is unchanged.
_ATSPI_WEB = {
    "document web": "page",
    "document frame": "docframe",
    "internal frame": "iframe",
}


def atspi_web_kind(role_name: str) -> str:
    """``page`` / ``docframe`` / ``iframe`` for a Chromium document or frame, else ``""``."""
    return _ATSPI_WEB.get((role_name or "").lower(), "")

# AT-SPI action name (lowercased) -> AX action the pruning engine treats as
# "interactive" (`_PRESS_ACTIONS` = AXPress/AXOpen/AXConfirm/AXPick).
# ``click`` on a real control is a press. Chrome also stamps ``click``,
# ``clickAncestor``, and ``showContextMenu`` on plain divs. ``clickAncestor``
# and ``showContextMenu`` are not presses: a context menu is not activation,
# and clickAncestor would mark every container that holds a click listener.
# A zero-size web wrapper that still receives AXPress from ``click`` is not a
# target; the pruner drops that flag (`observe._plain_zero_wrapper`).
_PRESS_ACTION_NAMES = frozenset(
    {"click", "press", "activate", "do default", "jump", "open", "toggle",
     "expand", "collapse", "expand or contract", "show", "showmenu", "menu"}
)
_PICK_ACTION_NAMES = frozenset({"select", "pick"})

#: Cap on children fetched per node. AT-SPI has no batch read (one D-Bus
#: round-trip per get_child_at_index), so fetching a virtualized 10k-row list is
#: ruinous; the engine only walks the first `_MAX_WALK_CHILDREN` (200) anyway,
#: so stop just above that. Overflow is elided by the engine's fan-out caps.
_MAX_CHILDREN_FETCH = 250

_inited = False
_atspi_binding = None


def _atspi():
    """The `Atspi` module, initialized once. Raises ImportError if PyGObject /
    the AT-SPI2 typelib are absent (the driver turns that into a clear message).

    The import stays inside this function. A missing ``gi`` module, or a
    typelib ``require_version`` cannot load, is ImportError either way.
    Reuse the successfully initialized binding, not any accessibility data.
    A bus reconnect clears ``_inited`` and must still initialize it again.
    """
    global _inited, _atspi_binding
    if _inited and _atspi_binding is not None and sys.modules.get("gi") is not None:
        return _atspi_binding
    try:
        import gi
    except ImportError as exc:
        raise ImportError("PyGObject is not installed; the AT-SPI2 binding is unavailable") from exc
    try:
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        raise ImportError("the AT-SPI2 typelib is not available") from exc

    if not _inited:
        # Before the first bus connection: a last-unref inside libatspi must
        # not abort this process. See drivers/_dbus_guard.py.
        from a11y_computer_use.drivers import _dbus_guard

        _dbus_guard.install()
        _safe(Atspi.init)  # 0 = ok, 1 = already running; both fine
        # Per-call D-Bus wait is 300ms. The second argument is the startup
        # grace: while an app is younger than that, libatspi waits the grace
        # instead (default 15000). An unanswered read of a just-opened GTK
        # dialog then blocks type and snapshot for 15s. Grace 0 keeps the
        # 300ms bound from the first call.
        _safe(lambda: Atspi.set_timeout(300, 0))
        _inited = True
    _atspi_binding = Atspi
    return Atspi


# libatspi's per-call D-Bus wait is 300ms (`Atspi.set_timeout` above). A call
# that raises after this long did not answer. A call that returns, however
# long it took, is a live app: Firefox, Chrome, and LibreOffice cross 300ms
# on startup and on large documents while still replying.
ATSPI_SLOW_CALL_S = 0.25
# A process that is still scheduled is frozen only after this much consecutive
# unanswered time, and only when a cheap ping also gets no reply.
ATSPI_PING_WINDOW_S = 4.0
ATSPI_PING_TRIES = 3
ATSPI_PING_CALL_MS = 2000
# Matches the registration wait. A young live process is retried by the
# caller instead of declared frozen; SIGSTOP is not given this grace.
ATSPI_STARTUP_GRACE_S = 15.0

_APP_NOT_RESPONDING_HINT = "The app is not responding. Wait, then snapshot again."

_reply_local = threading.local()


class _ReplyWatch:
    """Unanswered-call streak for the application currently being read."""

    def __init__(self, app: str, pid: int | None, target: object | None) -> None:
        self.app = app or "application"
        self.pid = pid
        self.target = target
        self.unanswered = 0
        self.unanswered_s = 0.0
        self.checked_stopped = False


@contextmanager
def app_reply_watch(app: str, pid: int | None, target: object | None = None):
    """Charge unanswered AT-SPI reads to ``app`` until the block exits.

    A call that returns is never a hang. A call that raises after
    `ATSPI_SLOW_CALL_S` is unanswered. That fails the read only with evidence
    the app is actually wedged: ``/proc/<pid>/stat`` state ``T`` or ``t``, or
    no reply to a cheap ping across `ATSPI_PING_WINDOW_S` after
    `ATSPI_PING_TRIES` misses. A process younger than `ATSPI_STARTUP_GRACE_S`
    is not failed for those misses; the caller retries. Reads outside a watch
    keep today's default-on-failure behavior, so one frozen sibling cannot
    fail a different application's snapshot.
    """
    previous = getattr(_reply_local, "watch", None)
    watch = _ReplyWatch(app, pid, target)
    _reply_local.watch = watch
    try:
        yield watch
    finally:
        _reply_local.watch = previous


def _current_watch() -> _ReplyWatch | None:
    return getattr(_reply_local, "watch", None)


def _proc_stat_after_comm(pid: int) -> list[bytes] | None:
    """Fields of ``/proc/<pid>/stat`` after the comm, or None when unreadable.

    The comm is wrapped in parentheses and may itself contain spaces and
    parentheses, so the state is the token after the last ``)``.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    end = data.rfind(b")")
    if end == -1 or end + 2 >= len(data):
        return None
    return data[end + 2 :].split()


def process_is_stopped(pid: int | None) -> bool:
    """True when the process is stopped (``T`` or ``t``), the SIGSTOP hang."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    fields = _proc_stat_after_comm(pid)
    if not fields:
        return False
    return fields[0] in {b"T", b"t"}


def process_age_s(pid: int | None) -> float | None:
    """Seconds since the process started, or None when ``/proc`` cannot say."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    fields = _proc_stat_after_comm(pid)
    # starttime is field 22; field 3 (state) is the first token after comm.
    if not fields or len(fields) <= 19:
        return None
    try:
        start_ticks = int(fields[19])
    except ValueError:
        return None
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        ticks = 100
    if not ticks:
        return None
    try:
        with open("/proc/uptime", "rb") as handle:
            uptime = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    age = uptime - (start_ticks / float(ticks))
    return age if age >= 0 else 0.0


def process_in_startup(pid: int | None) -> bool:
    """True while the process is younger than the registration grace.

    Snapshot waits already retry ``app_not_found`` through this window. A
    busy launch that misses a few reads is the same kind of delay, not a hang.
    """
    age = process_age_s(pid)
    return age is not None and age < ATSPI_STARTUP_GRACE_S


def registration_startup_remaining(app: str) -> float:
    """Remaining existing startup grace for an unambiguous window owner.

    A newly mapped window can precede AT-SPI registration. Older apps and
    owners whose age cannot be verified get no additional registration wait.
    """
    from a11y_computer_use.drivers import _linux_system

    try:
        pids = _linux_system.pids_matching(app)
        if not pids:
            # GTK's program name can differ from its interpreter's comm.
            # WM_CLASS identifies the mapped window for this timing hint;
            # titles alone never establish an owner.
            pids = {
                row["pid"] for row in _linux_system.windows()
                if isinstance(row.get("pid"), int) and row["pid"] > 0
                and _linux_system._class_matches_identifier(
                    app, str(row.get("wm_class") or ""),
                    str(row.get("wm_class_class") or ""),
                )
            }
    except Exception:
        # Process age is only a registration hint; unavailable X11 identity
        # must not replace the snapshot's own accessibility result.
        return 0.0
    if len(pids) != 1:
        return 0.0
    age = process_age_s(next(iter(pids)))
    if age is None:
        return 0.0
    return max(0.0, ATSPI_STARTUP_GRACE_S - age)


def stopped_pid_for_app(app: str) -> int | None:
    """The stopped pid for ``app``, when one process is unambiguously it.

    Window owners come first: a single managed window whose process is
    ``T``/``t``. A GTK fixture registers on AT-SPI as its program name
    (``cuatestapp``) while ``/proc/<pid>/comm`` stays ``python``, so the
    window lookup misses. The fallback is the one stopped process whose
    command line contains that program name. A live window owner is not
    replaced by some other stopped process.
    """
    needle = (app or "").strip().lower()
    if len(needle) < 3:
        return None
    try:
        from a11y_computer_use.drivers import _linux_system

        owners = _linux_system.pids_matching(app)
    except Exception:
        owners = set()
    if owners:
        stopped = [pid for pid in owners if process_is_stopped(pid)]
        if len(owners) == 1 and len(stopped) == 1:
            return stopped[0]
        return None
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if not process_is_stopped(pid):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").lower()
        if needle in text:
            found.append(pid)
            if len(found) > 1:
                return None
    return found[0] if found else None


def raise_if_app_stopped(app: str) -> None:
    """Raise ``app_not_responding`` when ``app`` is already stopped.

    A ``SIGSTOP`` is visible in ``/proc`` before the next AT-SPI call. Waiting
    for that call to time out walks every other desktop child first, which
    turned a stopped GTK fixture into an 8s snapshot. The pid check is the
    whole test.
    """
    pid = stopped_pid_for_app(app)
    if not isinstance(pid, int) or pid <= 0 or not process_is_stopped(pid):
        return
    _raise_not_responding(_ReplyWatch(app or "application", pid, None))


def _raise_not_responding(watch: _ReplyWatch) -> None:
    pid = watch.pid
    pid_text = f" (pid {pid})" if isinstance(pid, int) and pid > 0 else ""
    raise ComputerUseError(
        ErrorCode.APP_NOT_RESPONDING,
        f"{watch.app} did not answer accessibility queries{pid_text}",
        detail={
            "app": watch.app,
            "pid": pid if isinstance(pid, int) and pid > 0 else None,
            "hint": _APP_NOT_RESPONDING_HINT,
        },
    )


def _ping_answers(acc: object) -> bool:
    """True when ``acc`` answers a cheap read inside a longer budget.

    The walk stays on the 300ms per-call wait. This probe is the hang check
    for a process that is still scheduled: a few tries, each allowed
    `ATSPI_PING_CALL_MS`. A return, including an empty name, is a reply. An
    exception is a miss.
    """
    if acc is None:
        return False
    try:
        Atspi = _atspi()
    except Exception:
        return False
    try:
        Atspi.set_timeout(ATSPI_PING_CALL_MS, 0)
    except Exception:
        return False
    try:
        for _ in range(ATSPI_PING_TRIES):
            for method_name in ("get_name", "get_process_id"):
                method = getattr(acc, method_name, None)
                if method is None:
                    continue
                try:
                    method()
                except Exception:
                    continue
                return True
        return False
    finally:
        try:
            Atspi.set_timeout(300, 0)
        except Exception:
            pass


def _note_answered(watch: _ReplyWatch) -> None:
    watch.unanswered = 0
    watch.unanswered_s = 0.0


def _note_unanswered(watch: _ReplyWatch, elapsed: float) -> None:
    watch.unanswered += 1
    watch.unanswered_s += elapsed
    if not watch.checked_stopped and not process_is_stopped(watch.pid):
        watch.checked_stopped = True
        stopped = stopped_pid_for_app(watch.app)
        if isinstance(stopped, int) and stopped > 0:
            watch.pid = stopped
    if process_is_stopped(watch.pid):
        _raise_not_responding(watch)
    # A just-started app misses reads while it builds its tree. The snapshot
    # wait retries those; declaring it frozen here fails a live Firefox.
    if process_in_startup(watch.pid):
        return
    if watch.unanswered < ATSPI_PING_TRIES or watch.unanswered_s < ATSPI_PING_WINDOW_S:
        return
    if watch.target is None or _ping_answers(watch.target):
        _note_answered(watch)
        return
    _raise_not_responding(watch)


def call_went_unanswered() -> bool:
    """True when the last `_safe` call raised and used the D-Bus timeout.

    A slow call that returned is not unanswered. Desktop listing and the
    Gtk.Dialog.run revive use this so a busy app is not dropped or skipped.
    """
    return bool(getattr(_reply_local, "last_failed", False)) and (
        float(getattr(_reply_local, "last_elapsed", 0.0)) >= ATSPI_SLOW_CALL_S
    )


def _safe(fn, default=None, *, marshal=True):
    watch = _current_watch()
    start = time.monotonic()
    failed = False
    try:
        # Direct native calls (e.g. Table.get_accessible_at) need the same
        # thread ownership as version-tolerant getters. Keep waits on the
        # caller so the GLib loop can process replies between reads.
        from a11y_computer_use.drivers import _atspi_events

        result = _atspi_events.run(fn) if marshal else fn()
    except ComputerUseError:
        raise
    except Exception:
        failed = True
        result = default
    elapsed = time.monotonic() - start
    _reply_local.last_failed = failed
    _reply_local.last_elapsed = elapsed
    if watch is not None:
        if failed and elapsed >= ATSPI_SLOW_CALL_S:
            _note_unanswered(watch, elapsed)
        elif not failed:
            _note_answered(watch)
    return result


class DesktopApplications:
    """Applications on the AT-SPI desktop, split by whether they answered."""

    def __init__(self, ready: list[tuple[str, object]], silent: int) -> None:
        self.ready = ready
        self.silent = silent


def collect_app_snapshots(items: Sequence[tuple[str, int | None, Callable[[], object]]]) -> list:
    """Run each ``(app, pid, fn)`` under that app's reply budget.

    An application that does not answer is omitted. The others are returned
    in order. When every application fails, the first `app_not_responding`
    error is raised so the desktop does not look like an empty success.
    """
    kept: list = []
    first_fail: ComputerUseError | None = None
    for app, pid, fn in items:
        try:
            with app_reply_watch(app, pid):
                kept.append(fn())
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_RESPONDING:
                raise
            if first_fail is None:
                first_fail = exc
    if not kept and first_fail is not None:
        raise first_fail
    return kept


_a11y_status_forced = False


def enable_a11y_status() -> bool:
    """Force the desktop's accessibility 'active' flags on via D-Bus so already-
    running Chromium/Electron apps (Chrome, Slack, VS Code, Discord, Electron)
    build their AT-SPI tree WITH NO RELAUNCH — the Linux equivalent of the macOS
    AXEnhancedUserInterface trick, and the counter to Grok's a11y-OFF desktop.

    Chromium enables renderer accessibility when ``org.a11y.Status.IsEnabled`` /
    ``ScreenReaderEnabled`` go true on the session bus (the at-spi-bus-launcher
    owns ``org.a11y.Bus`` at ``/org/a11y/bus``); it listens for the change live.
    Idempotent + cached per process. Opt out with A11Y_COMPUTER_USE_NO_WEB_A11Y=1.
    Returns True if the flags are (now) set."""
    global _a11y_status_forced
    if _a11y_status_forced:
        return True
    if os.environ.get("A11Y_COMPUTER_USE_NO_WEB_A11Y"):
        return False
    try:
        import gi

        gi.require_version("Atspi", "2.0")  # ensure the a11y stack is present
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def _set(prop: str) -> None:
            bus.call_sync(
                "org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
                GLib.Variant("(ssv)", ("org.a11y.Status", prop, GLib.Variant("b", True))),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )

        for prop in ("IsEnabled", "ScreenReaderEnabled"):
            _safe(lambda p=prop: _set(p))
        _a11y_status_forced = True
        return True
    except Exception:
        return False


class _ReadCacheState:
    """Successful getter results for one ``read_cache`` block on this thread."""

    __slots__ = ("store", "pinned")

    def __init__(self) -> None:
        # id(obj) -> {(method name, args): value}. The object itself is pinned
        # so a freed proxy cannot reuse the id while the entry is live.
        self.store: dict[int, dict[tuple, object]] = {}
        self.pinned: dict[int, object] = {}


_read_cache_local = threading.local()


def _read_cache_stack() -> list[_ReadCacheState]:
    stack = getattr(_read_cache_local, "stack", None)
    if stack is None:
        stack = []
        _read_cache_local.stack = stack
    return stack


@contextmanager
def read_cache():
    """Reuse successful stable getter results until this block exits.

    The cache is per thread. An inner block sees an outer hit, and a
    mutator drops every block on this thread. ``get_name`` is never
    cached: a root probe retries an empty name. A child count of -1 is
    not cached: that value is a failed read the walk revives.
    ``get_state_set`` is never cached: a pointer click or Escape changes
    SELECTED and EXPANDED inside the same call, and those inputs are not
    AT-SPI mutators. An attribute dict that carries ``valuetext`` or an
    ``aria-*`` state is not cached either: typing into a date segment
    updates ``valuetext`` while the same call is still polling.
    """
    stack = _read_cache_stack()
    stack.append(_ReadCacheState())
    try:
        yield
    finally:
        stack.pop()


def _drop_read_cache() -> None:
    """Forget cached getters on this thread. Called before a mutator runs."""
    for state in _read_cache_stack():
        state.store.clear()
        state.pinned.clear()


def _drop_child_read_cache() -> None:
    """Refresh structure between discovery and traversal, retaining metadata."""
    getters = {"get_child_count", "get_childCount", "get_child_at_index", "getChildAtIndex"}
    for state in _read_cache_stack():
        for slot in state.store.values():
            for key in list(slot):
                if key[0] in getters:
                    del slot[key]


def _cache_key(name: str, args: tuple) -> tuple | None:
    key = (name, args)
    try:
        hash(key)
    except TypeError:
        return None
    return key


def _lookup_read_cache(obj, key: tuple):
    oid = id(obj)
    for state in reversed(_read_cache_stack()):
        slot = state.store.get(oid)
        if slot is not None and key in slot:
            found = slot[key]
            return dict(found) if isinstance(found, dict) else found
    return _CACHE_MISS


def _store_read_cache(obj, key: tuple, result) -> None:
    stack = _read_cache_stack()
    if not stack:
        return
    state = stack[-1]
    state.pinned[id(obj)] = obj
    slot = state.store.setdefault(id(obj), {})
    slot[key] = dict(result) if isinstance(result, dict) else result


class _CacheMiss:
    """Sentinel: a missing cache entry is distinct from a cached None."""


_CACHE_MISS = _CacheMiss()


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and value != ""


def _any_str(value) -> bool:
    return isinstance(value, str)


def _nonneg_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _present(value) -> bool:
    return value is not None


# These change when the user types or clicks, and the same driver call
# polls them until the new value is visible. Caching the dict freezes
# the first read.
_VOLATILE_ATTRIBUTES = frozenset({
    "valuetext",
    "aria-valuetext",
    "aria-valuenow",
    "aria-expanded",
    "aria-selected",
    "aria-checked",
    "aria-pressed",
})


def _attribute_keys(value) -> set[str] | None:
    try:
        keys = value.keys()
    except Exception:
        return None
    try:
        return {str(key).lower() for key in keys}
    except Exception:
        return None


def _attributes(value) -> bool:
    if isinstance(value, dict) or (
        value is not None and hasattr(value, "keys") and hasattr(value, "get")
    ):
        keys = _attribute_keys(value)
        return keys is not None and not (keys & _VOLATILE_ATTRIBUTES)
    return False


# Stable for the length of one driver call. Names, descriptions, extents,
# text, and state sets are read again: they change without a method we
# can see here, or a caller retries an empty result. A cached state set
# kept a combo EXPANDED after Escape and a Qt cell unselected after the
# pointer click that selected it. Attribute dicts are cached only when
# they have no valuetext or aria state key.
_CACHEABLE = {
    "get_role_name": _nonempty_str,
    "get_action_name": _any_str,
    "get_n_actions": _nonneg_int,
    "get_nActions": _nonneg_int,
    "get_attributes": _attributes,
    "get_component_iface": _present,
    "get_component": _present,
    "get_action_iface": _present,
    "get_action": _present,
    "get_accessible_id": _nonempty_str,
    "get_toolkit_name": _nonempty_str,
    "getToolkitName": _nonempty_str,
    # Toolkit/browser probes repeatedly resolve the same node's owner.
    # LibreOffice makes this a costly bus call; ownership is stable until
    # the operation ends or a mutator invalidates this cache.
    "get_application": _present,
    "getApplication": _present,
    "get_process_id": _positive_int,
    "get_child_at_index": _present,
    "getChildAtIndex": _present,
    "get_child_count": _nonneg_int,
    "get_childCount": _nonneg_int,
}

_MUTATORS = frozenset({
    "clear_cache", "clearCache",
    "set_text_contents", "setTextContents",
    "insert_text", "insertText",
    "set_current_value", "setCurrentValue",
    "scroll_to", "scrollTo",
    "do_action", "doAction",
    "grab_focus", "grabFocus",
    "select_child", "selectChild",
    "set_caret_offset", "setCaretOffset",
    "delete_text", "deleteText",
})


def _is_mutator(name: str) -> bool:
    if name in _MUTATORS:
        return True
    return name.replace("_", "").lower().startswith("deselect")


def _call_first(obj, names, *args, default=None):
    """Call the first method in ``names`` that exists on ``obj`` (binding-version
    tolerant — atspi2 renamed a few getters across releases).

    Inside ``read_cache``, a successful stable getter is remembered for
    this object and these arguments. A mutator drops that memory first.

    While ``event_main`` is running, the call moves onto that thread.
    libatspi is not safe from the caller and the loop at once.
    """
    if obj is None:
        return default

    def invoke():
        return _call_first_inline(obj, names, *args, default=default)

    from a11y_computer_use.drivers import _atspi_events

    return _atspi_events.run(invoke)


@contextmanager
def input_modal_cache(cache):
    """Bind a caller-owned click cache on the native operation's thread."""
    previous = getattr(_read_cache_local, "input_modals", None)
    _read_cache_local.input_modals = cache
    try:
        yield
    finally:
        _read_cache_local.input_modals = previous


def _call_first_inline(obj, names, *args, default=None):
    # _call_first already marshaled the entire getter/cache operation.
    for name in names:
        method = getattr(obj, name, None)
        if method is None:
            continue
        if _is_mutator(name):
            modal_cache = getattr(_read_cache_local, "input_modals", None)
            walk_parents = getattr(_read_cache_local, "walk_parents", None)
            if walk_parents is not None:
                walk_parents.clear()
            if name not in {"clear_cache", "clearCache"}:
                _modal_discoveries.clear()
                if modal_cache is not None:
                    modal_cache.clear()
            _drop_read_cache()
            return _safe(lambda m=method: m(*args), default, marshal=False)
        key = _cache_key(name, args)
        if key is not None and _read_cache_stack():
            cached = _lookup_read_cache(obj, key)
            if cached is not _CACHE_MISS:
                _reply_local.last_failed = False
                _reply_local.last_elapsed = 0.0
                return cached
        result = _safe(lambda m=method: m(*args), default, marshal=False)
        if (
            key is not None
            and _read_cache_stack()
            and not getattr(_reply_local, "last_failed", False)
        ):
            accept = _CACHEABLE.get(name)
            if accept is not None and accept(result):
                _store_read_cache(obj, key, result)
        return result
    return default


def _move_caret(atspi_mod, acc, offset):
    """Move the caret and drop cached getters; the tree may have changed."""
    _drop_read_cache()
    return _safe(lambda: atspi_mod.Text.set_caret_offset(acc, int(offset)))


# ---------------------------------------------------------------------------
# Per-node reads (each defensive; each may be a D-Bus round-trip)
# ---------------------------------------------------------------------------


def _role_name(acc) -> str:
    return (_call_first(acc, ("get_role_name",), default="") or "").lower()


def _component(acc):
    return _call_first(acc, ("get_component_iface", "get_component"))


def _extents(acc, *, keep_zero: bool = False):
    """(position, size) in SCREEN pixels, or (None, None).

    A zero width or height is ``(None, None)`` for hit-testing and scrolling.
    The snapshot read passes ``keep_zero=True`` so a 0-height Chromium section
    still carries its size into the pruner, which keeps the painted children
    under a web document. Callers that do not opt in are unchanged.
    """
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return None, None
    coord = getattr(getattr(Atspi, "CoordType", None), "SCREEN", 0)
    rect = _safe(lambda: comp.get_extents(coord))
    if rect is None:
        return None, None
    w = float(getattr(rect, "width", 0) or 0)
    h = float(getattr(rect, "height", 0) or 0)
    if w < 0 or h < 0:
        # GTK's "no allocation of my own" sentinel (-1, -1, -1, -1): a notebook
        # page tab whose label is hidden. Passed through as a negative extent so
        # the engine keeps the page's content below it instead of dropping it.
        return (-1.0, -1.0), (-1.0, -1.0)
    if w == 0 or h == 0:
        if keep_zero:
            return (float(getattr(rect, "x", 0) or 0), float(getattr(rect, "y", 0) or 0)), (w, h)
        return None, None
    return (float(getattr(rect, "x", 0) or 0), float(getattr(rect, "y", 0) or 0)), (w, h)


def _raw_rect(acc, coord_name: str):
    """(x, y, width, height) in one AT-SPI coord type, or None.

    ``SCREEN`` uses coord 0 when the binding has no ``CoordType``. A missing
    ``WINDOW`` type is None rather than a second screen read. The GTK
    no-allocation sentinel is returned as-is. This does not add the
    LibreOffice title-bar shift.
    """
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return None
    coord_type = getattr(Atspi, "CoordType", None)
    coord = getattr(coord_type, coord_name, None) if coord_type is not None else None
    if coord is None:
        if coord_name != "SCREEN":
            return None
        coord = 0
    rect = _safe(lambda: comp.get_extents(coord))
    if rect is None:
        return None
    return (
        float(getattr(rect, "x", 0) or 0),
        float(getattr(rect, "y", 0) or 0),
        float(getattr(rect, "width", 0) or 0),
        float(getattr(rect, "height", 0) or 0),
    )


def _share_parent_application(parent, kids) -> None:
    """Give each child its parent's application for the rest of this read.

    ``get_application`` climbs every ancestor, and on LibreOffice each of
    those climbs is a bus round trip. A Calc sheet walk asked it of every
    cell and widget, once from each toolkit check. A child whose own
    parent is ``parent`` has the same application, which the walk already
    knows. A child that names another parent climbs as before. Nothing is
    stored outside a ``read_cache`` block.
    """
    if parent is None or not kids or not _read_cache_stack():
        return
    if _role_name(parent) in {"application", "desktop frame"}:
        app = parent if _role_name(parent) == "application" else None
    else:
        app = _call_first(parent, ("get_application",))
    if app is None:
        return
    key = _cache_key("get_application", ())
    for kid in kids:
        if kid is None or not hasattr(kid, "get_application"):
            continue
        if _lookup_read_cache(kid, key) is not _CACHE_MISS:
            continue
        native_parent = _call_first(kid, ("get_parent",))
        memo = getattr(_read_cache_local, "walk_parents", None)
        kid_stable = _atspi_wrapper_key(kid) if memo is not None else None
        if kid_stable is not None:
            # This walk's modal ancestry asks the same parent again.
            memo.setdefault(kid_stable, native_parent)
        if native_parent is not parent:
            # GI may return another wrapper for the same native parent.
            # Require exact bus/path identity, and never inherit ownership
            # across an embedded application's bus boundary.
            parent_key = _atspi_wrapper_key(parent)
            kid_key = _atspi_wrapper_key(kid)
            if (parent_key is None or kid_key is None or kid_key[0] != parent_key[0]
                    or _atspi_wrapper_key(native_parent) != parent_key):
                continue
        _store_read_cache(kid, key, app)


def _frame_ancestor(acc):
    """The top-level frame that contains ``acc``, or ``acc`` when it is one."""
    node = acc
    for _ in range(32):
        if node is None:
            return None
        role = _role_name(node)
        if role == "frame":
            return node
        if role == "application":
            return None
        node = _call_first(node, ("get_parent", "getParent"))
    return None


def _negative_extent(size) -> bool:
    """True for GTK's no-allocation sentinel, (-1, -1) width and height."""
    if size is None:
        return False
    try:
        return float(size[0]) < 0 or float(size[1]) < 0
    except (TypeError, ValueError):
        return False


def _frame_accessible_name(node) -> str:
    """Accessible name of the frame that contains ``node``, or empty."""
    frame = _frame_ancestor(node)
    if frame is None:
        return ""
    return str(_call_first(frame, ("get_name",), default="") or "").strip()


# GIMP action buttons append modifier help to the tooltip:
# ``tooltip + "\n<b>" + modifier + "</b> " + ext``. GTK strips the markup
# and leaves two spaces between the modifier and the rest of the line
# (``Shift  Create new layers with last used values``). A single space
# (``Shift the layer``) is a sentence, not that help.
_MODIFIER_HINT_LINE = re.compile(
    r"(?i)^(?:shift|ctrl|control|alt|super|meta|primary)"
    r"(?:\+(?:shift|ctrl|control|alt|super|meta|primary))*"
    r"[ \t]{2,}\S"
)


def _modifier_hint_body(text: str) -> tuple[str, str] | None:
    """``(primary line, hint lines)`` when every extra line is modifier help.

    None when ``text`` is a single line, the first line is empty, or any
    extra line is not the GIMP modifier pattern. Plug-in blurbs such as
    ``Align Visible Layers...\\nThis plug-in only works...`` stay whole.
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in raw:
        return None
    lines = raw.split("\n")
    label = lines[0].strip()
    if not label:
        return None
    hints = [line.strip() for line in lines[1:] if line.strip()]
    if not hints or any(_MODIFIER_HINT_LINE.match(line) is None for line in hints):
        return None
    return label, "\n".join(hints)


def _split_modifier_hints(name: str, description: str) -> tuple[str, str]:
    """Keep the visible label as the name and the modifier lines as the description.

    GIMP leaves the accessible name empty and puts the whole tooltip,
    including Shift/Ctrl lines, in the description. The snapshot uses the
    description when the name is empty, so the button's name becomes those
    lines. The first line is the label drawn on the button. The rest is
    modifier help and stays on the description, which the snapshot does not
    render. A name that already contains the same lines is split the same
    way. Anything that is not that pattern is unchanged.
    """
    split = _modifier_hint_body(name)
    if split is not None:
        label, hints = split
        if not str(description).strip():
            return label, hints
        return label, description
    if str(name).strip():
        return name, description
    split = _modifier_hint_body(description)
    if split is None:
        return name, description
    return split


# Inkscape toolbox rows put the short name on the first line and the tooltip
# plus a parenthetical shortcut on the last: ``Text Tool\nCreate and edit
# text objects (T)``. The chord is a key, not a sentence in parentheses.
_TOOL_SHORTCUT = re.compile(
    r"(?i)^(?:(?:ctrl|control|shift|alt|super|meta|primary|cmd|command|option)\+)*"
    r"(?:f\d{1,2}|[a-z]|[0-9]|plus|minus|esc|escape|del|delete|tab|space|"
    r"return|enter|backspace|up|down|left|right|home|end|insert|"
    r"pageup|pagedown|page\s*up|page\s*down)$"
)
_TOOLTIP_SHORTCUT_LINE = re.compile(r"^(?P<hint>.+?) \((?P<shortcut>[^)]+)\)$")


def _tooltip_shortcut_body(text: str) -> tuple[str, str, str] | None:
    """``(label, hint, shortcut)`` for a name plus a tooltip ending in a chord.

    None when the text is one line, a line is blank, or the last line does
    not end in a parenthetical key. A sentence without that chord stays
    whole, and so does a GIMP modifier line.
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in raw:
        return None
    lines = [line.strip() for line in raw.split("\n")]
    if not lines or any(not line for line in lines):
        return None
    label = lines[0]
    if _generic_region_name(label):
        return None
    match = _TOOLTIP_SHORTCUT_LINE.match(lines[-1])
    if match is None:
        return None
    shortcut = match.group("shortcut").strip()
    if _TOOL_SHORTCUT.match(shortcut) is None:
        return None
    hint_bits = lines[1:-1] + [match.group("hint").strip()]
    hint = "\n".join(bit for bit in hint_bits if bit)
    if not hint:
        return None
    return label, hint, shortcut


def _split_tooltip_fields(name: str, description: str) -> tuple[str, str, str, str]:
    """``(name, description, hint, shortcut)`` for an Inkscape tool row.

    The visible name is the first line. The tooltip sentence is the hint
    and the parenthetical chord is the shortcut. A description that already
    has text is kept. A name that is not this pattern is unchanged, so the
    GIMP modifier split that ran first still owns those lines.
    """
    split = _tooltip_shortcut_body(name)
    if split is not None:
        label, hint, shortcut = split
        kept = description if str(description).strip() else hint
        return label, kept, hint, shortcut
    if str(name).strip():
        return name, description, "", ""
    split = _tooltip_shortcut_body(description)
    if split is None:
        return name, description, "", ""
    label, hint, shortcut = split
    return label, hint, hint, shortcut


# A GIMP image window's canvas is a focusable childless panel. Its name is
# not on that panel. The image title is a label elsewhere in the frame, and
# the frame name is the window title. This search is local to that one
# panel; it does not raise the snapshot's depth or child caps.
_SURFACE_SKIP_ROLES = frozenset({"layered pane", "menu", "menu bar", "table", "tree table"})
_SURFACE_WALK_NODES = 480
_SURFACE_WALK_DEPTH = 16
_SURFACE_WALK_CHILDREN = 48
_TITLE_PREFIX_NEXT = frozenset(" .-(–—")


def _page_tab_ancestor(acc):
    """Nearest page tab above ``acc``, or None when a window comes first."""
    node = acc
    for _ in range(32):
        if node is None:
            return None
        role = _role_name(node)
        if role == "page tab":
            return node
        if role in {"application", "frame", "window", "dialog"}:
            return None
        node = _parent_of(node)
    return None


def _title_prefix_label(name: str, title: str) -> bool:
    """True when ``name`` is the image title copied into the window title.

    GIMP's frame name starts with the image title and then a view suffix
    (``[Untitled]-1.0 (RGB...``). The next character is ``.``, a space, a
    dash, or ``(``. A label that merely shares a shorter stem is not used.
    """
    if not name or _generic_region_name(name):
        return False
    if not title.startswith(name) or len(title) <= len(name):
        return False
    return title[len(name)] in _TITLE_PREFIX_NEXT


def _prefix_image_labels(frame, title: str) -> list[str]:
    """Label names under ``frame`` that are the image-title prefix.

    Menus, icon grids, and tables are not walked. The budget stops the
    search; a miss falls back to the frame title rather than a guess.
    """
    found: list[str] = []
    budget = _SURFACE_WALK_NODES

    def walk(node, depth: int) -> None:
        nonlocal budget
        if budget <= 0 or depth > _SURFACE_WALK_DEPTH or len(found) > 1:
            return
        budget -= 1
        role = _role_name(node)
        if role == "label":
            name = _node_name(node)
            if _title_prefix_label(name, title):
                found.append(name)
        if role in _SURFACE_SKIP_ROLES:
            return
        count = _child_count(node)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return
        for index in range(min(int(count), _SURFACE_WALK_CHILDREN)):
            child = _child_at(node, index)
            if child is not None:
                walk(child, depth + 1)

    walk(frame, 0)
    return found


def _panel_surface_name(node, size, focusable: bool) -> str:
    """Image or window title for a focusable childless unnamed panel.

    Empty unless the panel sits under a page tab, which is the GIMP image
    canvas. The name is a label that prefixes the frame title when exactly
    one such label is in the frame, otherwise the frame title itself. No
    string is invented: an empty frame title leaves the panel unnamed.
    """
    if not focusable or size is None:
        return ""
    try:
        if float(size[0]) < 32 or float(size[1]) < 32:
            return ""
    except (TypeError, ValueError, IndexError):
        return ""
    count = _child_count(node)
    if isinstance(count, bool) or not isinstance(count, int) or count != 0:
        return ""
    if _page_tab_ancestor(node) is None:
        return ""
    frame = _frame_ancestor(node)
    if frame is None:
        return ""
    title = str(_call_first(frame, ("get_name",), default="") or "").strip()
    if not title or _generic_region_name(title):
        return ""
    labels = _prefix_image_labels(frame, title)
    if len(labels) == 1:
        return labels[0]
    return title


# A ruler is a drawing area that is thin on one axis. The document canvas
# is large on both. 64 keeps a default Inkscape ruler unnamed.
_CANVAS_MIN_SIDE = 64
_CANVAS_CONTENT_NOTE = "canvas content isn't accessible"


def _application_name(node) -> str:
    """Accessible name of the application that owns ``node``, or empty."""
    app = _call_first(node, ("get_application", "getApplication"))
    if not app:
        return ""
    return str(_call_first(app, ("get_name",), default="") or "").strip()


def _inkscape_window(node) -> bool:
    """True when the application or the frame title is Inkscape.

    The frame check is the `` - Inkscape`` suffix. A document whose name
    merely contains the word is not enough. A parent walk that does not
    yield an accessible returns false, and the canvas stays unnamed.
    """
    try:
        app = _application_name(node).casefold()
    except Exception:
        return False
    if "inkscape" in app:
        return True
    # A named application that is not Inkscape keeps its own canvases.
    # The frame suffix is only the fallback when the application name
    # itself was not readable.
    if app:
        return False
    try:
        title = _frame_accessible_name(node).casefold()
    except Exception:
        return False
    return title.endswith(" - inkscape") or title.endswith("– inkscape")


def _drawing_canvas_facts(node, size) -> tuple[str, str, str]:
    """``(surface name, content note, exposed text)`` for one drawing area.

    Empty unless this is a large unnamed Inkscape drawing area. The name is
    the frame title, or the single label that prefixes that title. The same
    rule names the GIMP image canvas. The note is set only when the node
    has no Text and no children: the SVG is not in the tree. Text that the
    toolkit does expose is returned so the snapshot can show it. The box
    stays the drawing area's own box.
    """
    if size is None:
        return "", "", ""
    try:
        if float(size[0]) < _CANVAS_MIN_SIDE or float(size[1]) < _CANVAS_MIN_SIDE:
            return "", "", ""
    except (TypeError, ValueError, IndexError):
        return "", "", ""
    if not _inkscape_window(node):
        return "", "", ""
    text = _page_text(node) or ""
    count = _raw_child_count(node)
    note = ""
    if not text and count == 0:
        note = _CANVAS_CONTENT_NOTE
    frame = _frame_ancestor(node)
    if frame is None:
        return "", note, text
    title = str(_call_first(frame, ("get_name",), default="") or "").strip()
    if not title or _generic_region_name(title):
        return "", note, text
    labels = _prefix_image_labels(frame, title)
    name = labels[0] if len(labels) == 1 else title
    return name, note, text


def _audacity_track_area(container) -> bool:
    """Recognize a project track area by its published Timeline Options control.

    Stay in this container: a separate wx dialog may also have role frame.
    A bounded search fails closed when the project context is unavailable.
    """
    budget = _SURFACE_WALK_NODES

    def walk(node, depth):
        nonlocal budget
        if budget <= 0 or depth > _SURFACE_WALK_DEPTH:
            return False
        budget -= 1
        role = _role_name(node)
        if role in {"application", "frame", "window", "dialog"} and depth:
            return False
        if _node_name(node).strip().casefold() == "timeline options":
            return True
        if role in _SURFACE_SKIP_ROLES:
            return False
        count = _child_count(node)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return False
        for index in range(min(count, _SURFACE_WALK_CHILDREN)):
            child = _child_at(node, index)
            if child is not None and walk(child, depth + 1):
                return True
        return False

    return walk(container, 0)


def _gucharmap_character_surface(node) -> tuple[str, str]:
    """Identify the empty painted Character Table without inventing glyph cells."""
    if _child_count(node) != 0:
        return "", ""
    parent = _parent_of(node)
    if not parent or _role_name(parent) != "scroll pane":
        return "", ""
    app = _application_ancestor(node)
    if app is None or _node_name(app).casefold() != "gucharmap":
        return "", ""
    character_table = False
    for _ in range(20):
        if parent is None:
            break
        role = _role_name(parent)
        if role in {"dialog", "application"}:
            break
        if role in {"frame", "window"}:
            if not character_table:
                break
            return (
                "gucharmap Character Table",
                "Individual characters are not exposed by AT-SPI. This ref identifies "
                "the whole painted table, not a character; find and scroll_to_find cannot "
                "target its glyphs by codepoint or name, and clicking it cannot confirm "
                "a requested character. Scrolling may change the selection. Use Search > "
                "Find or the Go menu as separate requested operations; verify the selected "
                "character in the statusbar or Character Details.",
            )
        character_table = character_table or (
            role == "page tab" and _node_name(parent).casefold() == "character table"
        )
        parent = _parent_of(parent)
    return "", ""


def _okular_thumbnail_surface(node) -> tuple[str, str]:
    """Label an empty thumbnail strip from its native sidebar context.

    No page labels or thumbnail bounds are inferred from this painted filler.
    Native names and children take precedence over the fallback.
    """
    if _child_count(node) != 0:
        return "", ""
    app = _application_ancestor(node)
    if app is None or _node_name(app).casefold() != "okular":
        return "", ""
    parent = _parent_of(node)
    if parent is None or not any(
        _role_name(child) == "scroll bar" for child in _direct_children(parent)
    ):
        return "", ""
    thumbnails = False
    for _ in range(20):
        if parent is None:
            break
        role = _role_name(parent)
        if role in {"dialog", "application"}:
            break
        if role in {"frame", "window"}:
            if _node_name(parent).casefold() == "sidebar" and thumbnails:
                return (
                    "Okular thumbnails",
                    "Per-page thumbnails are not exposed by AT-SPI; use the page-number "
                    "field, Go to Page…, or the Find bar for navigation.",
                )
            break
        thumbnails = thumbnails or any(
            _role_name(child) in {"label", "static", "text"}
            and _node_name(child).casefold() == "thumbnails"
            for child in _direct_children(parent)
        )
        parent = _parent_of(parent)
    return "", ""


def _claws_list_surface(node) -> bool:
    """Recognize Claws' empty list panels without guessing which list it is."""
    if _child_count(node) != 0 or _has_text_interface(node):
        return False
    app = _application_ancestor(node)
    if app is None or _node_name(app).casefold() not in {"claws-mail", "claws mail"}:
        return False
    parent = _parent_of(node)
    if parent is None or _role_name(parent) != "scroll pane":
        return False
    split = False
    for _ in range(20):
        parent = _parent_of(parent)
        if parent is None:
            return False
        role = _role_name(parent)
        if role in {"dialog", "tool bar", "page tab", "page tab list", "application"}:
            return False
        if role in {"frame", "window"}:
            return split
        split = split or role == "split pane"
    return False


def _scribus_embedded_properties(node) -> bool:
    """Recognize the empty QTreeWidget item hosting Scribus palette widgets."""
    if _child_count(node) != 0:
        return False
    app = _application_ancestor(node)
    if app is None or _node_name(app).casefold() != "scribus":
        return False
    tree = _parent_of(node)
    if tree is None or _role_name(tree) != "tree":
        return False
    frame = _parent_of(tree)
    if (frame is None or _role_name(frame) not in {"frame", "window"}
            or _node_name(frame).casefold() != "properties"):
        return False
    # libatspi's Text.get_character_count can return 0 for a node without
    # Text. Prefer the native interface list over that ambiguous sentinel.
    interfaces = _call_first(node, ("get_interfaces",))
    if interfaces is not None:
        if any(str(interface).rsplit(".", 1)[-1] in {"Text", "EditableText"}
               for interface in interfaces):
            return False
    elif _has_text_interface(node):
        return False
    # The content item immediately follows its expanded section header.
    previous = None
    for index in range(_child_count(tree)):
        child = _child_at(tree, index)
        if child == node:
            return (previous is not None and _role_name(previous) == "table cell"
                    and bool(_node_name(previous).strip())
                    and _state_has(previous, "EXPANDED"))
        previous = child
    return False


def _okular_page_surface(node) -> bool:
    """Recognize Okular's opaque document viewport from its tree context."""
    if _child_count(node) != 0 or not _parent_of(node):
        return False
    app = _application_ancestor(node)
    if app is None or _node_name(app).casefold() != "okular":
        return False
    parent = _parent_of(node)
    if parent is None:
        return False
    count = _child_count(parent)
    if not isinstance(count, int) or count <= 0:
        return False
    if not any(_role_name(_child_at(parent, i)) == "scroll bar" for i in range(count)):
        return False
    layered = False
    split = False
    for _ in range(20):
        if parent is None:
            return False
        role = _role_name(parent)
        if role in {"dialog", "tool bar", "page tab", "page tab list"}:
            return False
        if role in {"frame", "window"}:
            return layered and split
        if role == "application" or _node_name(parent).strip():
            return False
        layered = layered or role == "layered pane"
        split = split or role == "split pane"
        parent = _parent_of(parent)
    return False


def _kate_gutter(node, position, size) -> bool:
    """Recognize Kate's childless gutter beside an accessible text editor.

    Use only native boxes and immediate siblings. Missing geometry or an
    ambiguous editor leaves the filler unnamed; no line positions are inferred.
    """
    if position is None or size is None or _child_count(node) != 0:
        return False
    app = _application_ancestor(node)
    if app is None or _node_name(app).casefold() != "kate":
        return False
    parent = _parent_of(node)
    if parent is None or _frame_ancestor(node) is None:
        return False
    ancestor = parent
    for _ in range(20):
        if ancestor is None or _role_name(ancestor) == "dialog":
            return False
        if _role_name(ancestor) in {"frame", "window"}:
            break
        ancestor = _parent_of(ancestor)
    else:
        return False
    count = _child_count(parent)
    if not isinstance(count, int) or isinstance(count, bool) or not 0 < count <= 64:
        return False
    matches = 0
    x, y = position
    width, height = size
    if width <= 0 or height <= width:
        return False
    for index in range(count):
        sibling = _child_at(parent, index)
        if sibling is None or _role_name(sibling) != "text":
            continue
        if not _state_has(sibling, "MULTI_LINE"):
            continue
        editor_pos, editor_size = _extents(sibling)
        if editor_pos is None or editor_size is None:
            continue
        ex, ey = editor_pos
        ew, eh = editor_size
        if ew > width and abs(x + width - ex) <= 1 and abs(y - ey) <= 1 and abs(height - eh) <= 1:
            matches += 1
    return matches == 1


def _audacity_panel(node, size, focusable: bool) -> tuple[str, str, str]:
    """Role, surface name, and note for Audacity's childless wx panels.

    Toolbar tools put their label in the tooltip; wxGTK may identify their
    container only through a sibling grip's description. The track view and Time
    clock paint their contents without publishing controls or a value. Keep
    those surfaces opaque; never invent track names or clock readings.
    Require Timeline Options in the local track area / project window for
    surface hints: wxGTK dialogs can expose a frame rather than a dialog.
    """
    if _child_count(node) != 0 or not _parent_of(node):
        return "", "", ""
    app = _application_ancestor(node)
    if app is None or _node_name(app).casefold() != "audacity":
        return "", "", ""
    toolbar = None
    parent = _parent_of(node)
    for _ in range(20):
        if parent is None:
            return "", "", ""
        role = _role_name(parent)
        if role == "dialog":
            return "", "", ""
        if role == "tool bar" and toolbar is None:
            toolbar = parent
        if role in {"frame", "window"}:
            break
        if role == "application":
            return "", "", ""
        parent = _parent_of(parent)
    else:
        return "", "", ""
    name = _node_name(node).strip()
    description = str(_call_first(node, ("get_description",), default="") or "").strip()
    if toolbar is None and not name and description:
        container = _parent_of(node)
        if _role_name(container) == "panel":
            for index in range(_child_count(container)):
                sibling = _child_at(container, index)
                marker = str(_call_first(
                    sibling, ("get_description",), default="",
                ) or "").strip().casefold()
                if (_role_name(sibling) == "panel" and _child_count(sibling) == 0
                        and marker.startswith("audacity ") and marker.endswith(" toolbar")):
                    if description.casefold() != marker:
                        return "AXButton", "", ""
    # wxGTK publishes the Time toolbar as a panel containing a labelled
    # grip, the clock, and a resize grip, rather than a named tool bar.
    container = _parent_of(node)
    if not name and not description and _role_name(container) == "panel" and _child_count(container) == 3:
        grip = _child_at(container, 0)
        clock = _child_at(container, 1)
        resize_grip = _child_at(container, 2)
        if (
            clock == node
            and grip is not None
            and resize_grip is not None
            and _role_name(grip) == "panel"
            and _role_name(resize_grip) == "panel"
            and str(_call_first(grip, ("get_description",), default="") or "").strip().casefold()
            == "audacity time toolbar"
        ):
            return "", "Audacity Time clock", "Clock reading is not exposed by AT-SPI."
    if toolbar is not None:
        if description and not name:
            return "AXButton", "", ""
        if (not name and not description
                and _node_name(toolbar).casefold() in {"time", "time toolbar"}
                and _audacity_track_area(parent)):
            return "", "Audacity Time clock", "Clock reading is not exposed by AT-SPI."
    elif not name and not description and focusable and size is not None:
        if size[0] >= 300 and size[1] >= 100 and _audacity_track_area(_parent_of(node)):
            return (
                "", "Audacity track / waveform",
                "Track controls are not exposed by AT-SPI; use Tracks > Mute/Unmute menus.",
            )
    return "", "", ""


def _direct_label_names(acc) -> list[str]:
    """Non-generic names of ``acc``'s immediate label children.

    Stops once two names are found. The caller treats that as ambiguous.
    """
    count = _child_count(acc)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return []
    found: list[str] = []
    for index in range(min(int(count), 48)):
        child = _child_at(acc, index)
        if child is None or _role_name(child) != "label":
            continue
        name = _node_name(child)
        if name and not _generic_region_name(name):
            found.append(name)
        if len(found) > 1:
            return found
    return found


def _selected_icon_name(acc) -> str:
    """The one label beside a selected icon, or empty.

    GIMP brush-grid icons expose no name, description, text, or tooltip.
    The dock's selected brush is a label on the filler that also holds the
    scroll pane. Unselected icons have nothing to read, so this returns
    empty for them only because the caller skips them. An ancestor with
    two labels is ambiguous. The walk stops at a page tab or a window so
    a label from another dock is not borrowed.
    """
    node = acc
    for _ in range(6):
        node = _parent_of(node)
        if node is None:
            return ""
        role = _role_name(node)
        if role in {"application", "frame", "window", "dialog", "page tab", "page tab list"}:
            return ""
        labels = _direct_label_names(node)
        if len(labels) == 1:
            return labels[0]
        if len(labels) > 1:
            return ""
    return ""


def _hidden_page_tab(node) -> bool:
    """True when ``node`` is a page tab GTK gave no allocation of its own."""
    if node is None or _role_name(node) != "page tab":
        return False
    _pos, size = _extents(node, keep_zero=True)
    return _negative_extent(size)


def _terminal_page_frame_title(node) -> str:
    """Borrow a frame title only for its single, terminal-content page."""
    notebook = _call_first(node, ("get_parent", "getParent"))
    if _role_name(notebook) != "page tab list" or _child_count(notebook) != 1:
        return ""
    # Reject detached popovers and nested notebooks. Only layout wrappers
    # may separate the notebook from its own frame.
    current = notebook
    for _ in range(8):
        current = _call_first(current, ("get_parent", "getParent"))
        role = _role_name(current)
        if role in {"frame", "window"}:
            break
        if role not in {"panel", "filler", "section"}:
            return ""
    else:
        return ""
    frame = current
    current = node
    for _ in range(8):
        kids = _direct_children(current)
        if _role_name(current) == "scroll pane":
            # GTK exposes the pane's scrollbars beside its terminal content.
            # They do not make this a second content page.
            kids = [child for child in kids if _role_name(child) != "scroll bar"]
        if len(kids) != 1:
            return ""
        current = kids[0]
        role = _role_name(current)
        if role == "terminal":
            return _node_name(frame).strip()
        if role not in {"panel", "filler", "section", "scroll pane", "viewport"}:
            return ""
    return ""


def _claws_wizard_page_title(node) -> str:
    """Read the leading page label of Claws Mail's hidden wizard notebook."""
    if _application_name(node).casefold() not in {"claws-mail", "claws mail"}:
        return ""
    window = next((ancestor for ancestor in _ancestor_chain(node)
                   if _role_name(ancestor) in {"frame", "window", "dialog"}), None)
    if window is None or "setup wizard" not in _node_name(window).casefold():
        return ""
    # Descend through leading layout children, skipping decorative icons in
    # the title row. Stop at controls: later labels include
    # explanatory prose and field captions, not the page title. Never borrow
    # a label from another page or substitute the shared wizard window title.
    current = node
    for _ in range(8):
        kids = _direct_children(current)
        if not kids:
            return ""
        current = next((kid for kid in kids if _role_name(kid) != "icon"), None)
        if current is None:
            return ""
        role = _role_name(current)
        if role == "label":
            return _plain_accessible_name(_node_name(current))
        if role not in {"panel", "filler", "section"}:
            return ""
    return ""


def _direct_children(node) -> list:
    """Immediate children, without the snapshot's page-tab hoist."""
    count = _child_count(node)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return []
    found = []
    for index in range(min(int(count), _MAX_CHILDREN_FETCH)):
        child = _call_first(node, ("get_child_at_index", "getChildAtIndex"), index)
        if child is not None:
            found.append(child)
    return found


def _hoist_hidden_page_tabs(kids: list) -> list:
    """Lift a hidden page tab's page up to the tab list.

    The page stays in the snapshot, and it is not a child of the tab button.
    A page tab that has a real box keeps its own children.
    """
    out = []
    for kid in kids:
        out.append(kid)
        if _hidden_page_tab(kid):
            out.extend(_direct_children(kid))
    return out


def _usable_window_rect(rect) -> bool:
    """False for a missing box, a zero box, or GTK's off-screen sentinel."""
    if rect is None:
        return False
    x, y, width, height = rect
    if width <= 0 or height <= 0:
        return False
    return x > -1_000_000 and y > -1_000_000


def _coords_close(a: float, b: float, tol: float = 4.0) -> bool:
    return abs(a - b) <= tol


def _client_area_is_on_screen(frame, frame_screen, client) -> bool:
    """True when SCREEN coordinates already include the window-manager frame.

    The X client origin sits below the outer frame by the title bar. A
    direct child whose SCREEN top is that client origin — LibreOffice's
    root pane — is already on the pixels, so adding the origin again drops
    every box by one title bar. A child whose SCREEN top is still the outer
    frame's top is the build that omits the title bar. No such child means
    the node's own SCREEN point is kept.
    """
    if frame is None or frame_screen is None or client is None:
        return True
    fx, fy = float(frame_screen[0]), float(frame_screen[1])
    cx, cy = float(client[0]), float(client[1])
    if _coords_close(cx, fx) and _coords_close(cy, fy):
        return True
    count = _child_count(frame)
    if count < 0:
        count = 0
    saw_outer = False
    for index in range(min(count, 6)):
        child = _child_at(frame, index)
        if child is None or _role_name(child) == "frame":
            continue
        rect = _raw_rect(child, "SCREEN")
        if rect is None or rect[2] <= 0 or rect[3] <= 0:
            continue
        if _coords_close(float(rect[0]), cx, 8) and _coords_close(float(rect[1]), cy, 8):
            return True
        if _coords_close(float(rect[0]), fx, 4) and _coords_close(float(rect[1]), fy, 4):
            saw_outer = True
    return not saw_outer


def _libreoffice_content_origin(
    screen, window, frame_screen, client, *, screen_is_absolute: bool,
):
    """Screen top-left for one LibreOffice content node.

    ``screen`` and ``window`` are that node's SCREEN and WINDOW points.
    ``frame_screen`` is the frame's outer SCREEN rectangle. ``client`` is
    the X client origin inside that frame. ``screen_is_absolute`` is the
    frame-child check in ``_client_area_is_on_screen``.

    A node whose SCREEN point is already the client origin plus its WINDOW
    point is on the pixels. When SCREEN and WINDOW are the same point, the
    client origin is added only if the frame's client area was reported at
    the outer corner. Adding it on a build whose SCREEN point already
    matches the screen drops the box by one title bar.
    """
    sx, sy = float(screen[0]), float(screen[1])
    wx, wy = float(window[0]), float(window[1])
    cx, cy = float(client[0]), float(client[1])
    fx, fy = float(frame_screen[0]), float(frame_screen[1])
    if _coords_close(sx, cx + wx) and _coords_close(sy, cy + wy):
        return (sx, sy)
    if _coords_close(sx, wx) and _coords_close(sy, wy):
        if screen_is_absolute:
            return (sx, sy)
        return (cx + wx, cy + wy)
    if _coords_close(sx, fx + wx) and _coords_close(sy, fy + wy):
        return (sx, sy)
    if not screen_is_absolute:
        return (cx + wx, cy + wy)
    return (sx, sy)


def _libreoffice_pointer_delta(
    screen_is_absolute: bool,
    major: int | None,
    client,
    frame_screen,
) -> tuple[float, float]:
    """Pointer adjustment so a pixel point hits that pixel.

    The published box stays on the pixels. LibreOffice 24.2 gtk3 still
    hit-tests one title bar above those pixels, so a click on the published
    centre selects the row above. The title bar is added to the event only.
    Adding it to the box as well drops a crop onto the next line. LibreOffice
    25 hit-tests the published point, and a build whose client area is still
    reported at the outer corner already has the title bar in the box.
    """
    if not screen_is_absolute or client is None or frame_screen is None:
        return (0.0, 0.0)
    if major is None or major >= 25:
        return (0.0, 0.0)
    return (
        float(client[0]) - float(frame_screen[0]),
        float(client[1]) - float(frame_screen[1]),
    )


_lo_major: int | None = None
_lo_major_known = False


def libreoffice_major() -> int | None:
    """Major version of the installed ``soffice``, or None when it cannot be read.

    Cached for the process. ``LibreOffice 24.2.7.2`` is 24. A missing binary
    is None, which does not move the pointer.
    """
    global _lo_major, _lo_major_known
    if _lo_major_known:
        return _lo_major
    _lo_major_known = True
    try:
        out = subprocess.check_output(
            ["soffice", "--version"], text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for token in out.split():
        head, dot, _rest = token.partition(".")
        if dot and head.isdigit():
            _lo_major = int(head)
            return _lo_major
    return None


def _libreoffice_frame_at(x: int, y: int):
    """The LibreOffice frame whose SCREEN rectangle contains ``(x, y)``."""
    try:
        Atspi = _atspi()
    except ImportError:
        return None
    desktop = _safe(lambda: Atspi.get_desktop(0))
    if desktop is None:
        return None
    count = _child_count(desktop)
    if count < 0:
        count = 0
    for index in range(min(int(count), 40)):
        app = _child_at(desktop, index)
        if app is None:
            continue
        name = str(_call_first(app, ("get_name",), default="") or "")
        if not libreoffice_app(name):
            continue
        frames = _child_count(app)
        if frames < 0:
            frames = 0
        for child_index in range(min(int(frames), 8)):
            frame = _child_at(app, child_index)
            if frame is None or _role_name(frame) != "frame":
                continue
            rect = _raw_rect(frame, "SCREEN")
            if rect is None or rect[2] <= 0 or rect[3] <= 0:
                continue
            if (
                rect[0] - 2 <= x <= rect[0] + rect[2] + 2
                and rect[1] - 2 <= y <= rect[1] + rect[3] + 2
            ):
                return frame
    return None


# After the first pointer event, LibreOffice 24.2 republishes content
# extents in the hit-test space (about one title bar lower). A later event
# in that process must not add the inset again. A new soffice process, even
# one whose frame lands on the same screen rectangle, still needs the first
# inset. Keyed by process id plus the frame's SCREEN rectangle.
_lo_pointer_aligned: set[tuple[int, int, int, int, int]] = set()


def _libreoffice_frame_key(frame) -> tuple[int, int, int, int] | None:
    screen = _raw_rect(frame, "SCREEN")
    if screen is None or screen[2] <= 0 or screen[3] <= 0:
        return None
    return (int(screen[0]), int(screen[1]), int(screen[2]), int(screen[3]))


def _libreoffice_align_key(frame) -> tuple[int, int, int, int, int] | None:
    rect = _libreoffice_frame_key(frame)
    if rect is None:
        return None
    return (pid_of(frame) or 0, *rect)


def libreoffice_pointer_shift(x: int, y: int) -> tuple[float, float]:
    """``(dx, dy)`` added to a pointer event aimed at pixel ``(x, y)``.

    Zero when the point is not in a LibreOffice frame, when that frame's
    client area is still reported at the outer corner, on LibreOffice 25
    and newer, or after a pointer event has already landed in that process's
    frame. That first event moves the hit-test onto the published extents, so
    a second click must use the new box as-is. Failures are zero so a click
    outside LibreOffice is unchanged.
    """
    try:
        frame = _libreoffice_frame_at(int(x), int(y))
        if frame is None:
            return (0.0, 0.0)
        key = _libreoffice_frame_key(frame)
        align = _libreoffice_align_key(frame)
        if key is None or align is None or align in _lo_pointer_aligned:
            return (0.0, 0.0)
        screen = _raw_rect(frame, "SCREEN")
        if screen is None:
            return (0.0, 0.0)
        from a11y_computer_use.drivers import _linux_system

        title = str(_call_first(frame, ("get_name",), default="") or "")
        origin = _linux_system.client_origin_for_outer_frame(
            key[0], key[1], key[2], key[3], title=title,
        )
        absolute = True if origin is None else _client_area_is_on_screen(frame, screen, origin)
        return _libreoffice_pointer_delta(absolute, libreoffice_major(), origin, screen)
    except Exception:
        return (0.0, 0.0)


# LibreOffice 25.2 keeps content SCREEN about one title bar above WINDOW
# until the first pointer event. The gap is that bar (about 21–29px), so
# anything smaller is the ordinary LO 24 reading, where the two already
# agree and a motion would drop the published box into the hit-test.
_LO_PRE_MOTION_GAP = 12
_lo_settle_attempts: dict[int, int] = {}


def libreoffice_pre_motion(screen_y: float, window_y: float) -> bool:
    """True when a content SCREEN top is about a title bar above WINDOW.

    That is the pre-motion reading on LibreOffice 25.2. LibreOffice 24
    reports the two equal before the first pointer event, and moves SCREEN
    down afterwards. Neither of those is this gap.
    """
    return float(window_y) - float(screen_y) >= _LO_PRE_MOTION_GAP


def libreoffice_status_point(x: int, y: int, width: int, height: int) -> tuple[int, int] | None:
    """A point in the status bar of a client rectangle, or None when it is tiny.

    The bottom centre is status text. It is not a cell, a menu, or a toolbar
    button, so the motion does not select or press anything.
    """
    if width < 40 or height < 40:
        return None
    return (int(x) + int(width) // 2, int(y) + int(height) - 8)


def _client_box_for_frame(frame) -> tuple[int, int, int, int] | None:
    """``(x, y, width, height)`` of the X client area under ``frame``.

    The origin is the managed window that sits on the frame's outer SCREEN
    rectangle. None when that window is not listed.
    """
    screen = _raw_rect(frame, "SCREEN")
    if screen is None or screen[2] <= 0 or screen[3] <= 0:
        return None
    from a11y_computer_use.drivers import _linux_system

    title = str(_call_first(frame, ("get_name",), default="") or "")
    origin = _linux_system.client_origin_for_outer_frame(
        int(screen[0]), int(screen[1]), int(screen[2]), int(screen[3]), title=title,
    )
    if origin is None:
        return None
    try:
        listed = _linux_system.windows()
    except Exception:
        return None
    for row in listed:
        bounds = row.get("bounds") if isinstance(row, dict) else None
        if not isinstance(bounds, dict):
            continue
        if abs(int(bounds.get("x", 0)) - origin[0]) > 4:
            continue
        if abs(int(bounds.get("y", 0)) - origin[1]) > 4:
            continue
        width = int(bounds.get("width") or 0)
        height = int(bounds.get("height") or 0)
        if width > 0 and height > 0:
            return (origin[0], origin[1], width, height)
    return None


def _pre_motion_nodes(root) -> list:
    """Content nodes whose SCREEN/WINDOW pair can show the pre-motion gap.

    The frame itself is the outer window and is not content. A spreadsheet
    contributes one cell; its other cells are not walked.
    """
    found: list = []
    stack: list[tuple] = [(root, 0)]
    while stack and len(found) < 24:
        node, depth = stack.pop()
        if node is None or depth > 5:
            continue
        role = _role_name(node)
        if role not in {"frame", "application"}:
            found.append(node)
        if role in {"menu", "menu bar", "popup menu", "tool bar"} or role == "table":
            if _spreadsheet_table(node):
                cell = _table_cell_at(node, 0, 0)
                if cell is not None:
                    found.append(cell)
            continue
        if depth >= 5:
            continue
        count = _child_count(node)
        if count < 0:
            count = 0
        for index in range(min(int(count), 6) - 1, -1, -1):
            stack.append((_child_at(node, index), depth + 1))
    return found


def _frame_is_pre_motion(frame) -> bool:
    for node in _pre_motion_nodes(frame):
        screen = _raw_rect(node, "SCREEN")
        window = _raw_rect(node, "WINDOW")
        if screen is None or window is None:
            continue
        if screen[2] <= 0 or screen[3] <= 0 or window[2] <= 0 or window[3] <= 0:
            continue
        if libreoffice_pre_motion(screen[1], window[1]):
            return True
    return False


def settle_libreoffice_geometry(root) -> None:
    """Move the pointer once when this window's content is still pre-motion.

    LibreOffice 25.2 then republishes SCREEN on the pixels. The snapshot
    that follows reads those extents. The placement cache and the
    LibreOffice 24 pointer inset are unchanged: a window whose SCREEN
    already matches WINDOW is not moved, so the first click there still
    uses the title-bar shift. The motion is two points in the status bar.
    A process is tried at most twice. Failures leave the bounds as read.
    """
    try:
        frame = root if _role_name(root) == "frame" else _frame_ancestor(root)
        if frame is None:
            count = _child_count(root)
            if count < 0:
                count = 0
            for index in range(min(int(count), 4)):
                child = _child_at(root, index)
                if child is not None and _role_name(child) == "frame":
                    frame = child
                    break
        if frame is None or not _frame_is_pre_motion(frame):
            return
        pid = pid_of(frame) or pid_of(root) or 0
        if _lo_settle_attempts.get(pid, 0) >= 2:
            return
        box = _client_box_for_frame(frame)
        if box is None:
            return
        point = libreoffice_status_point(*box)
        if point is None:
            return
        from a11y_computer_use.drivers import _linux_input

        x, y = point
        _linux_input.hover(x, y)
        _linux_input.hover(min(x + 3, box[0] + box[2] - 4), y)
        _lo_settle_attempts[pid] = _lo_settle_attempts.get(pid, 0) + 1
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            if not _frame_is_pre_motion(frame):
                return
            time.sleep(0.05)
    except Exception:
        return


def note_libreoffice_pointer(x: int, y: int) -> None:
    """Remember that a pointer event landed in the LibreOffice frame at ``(x, y)``."""
    try:
        frame = _libreoffice_frame_at(int(x), int(y))
        if frame is None:
            return
        key = _libreoffice_align_key(frame)
        if key is not None:
            _lo_pointer_aligned.add(key)
    except Exception:
        return


def _action_iface(acc):
    return _call_first(acc, ("get_action_iface", "get_action"))


def _gtk4_menu_button_wrapper(acc) -> bool:
    """Recognize an actionless duplicate around one native GTK 4 toggle."""
    if not _gtk4_app(acc) or _action_names(acc) or _child_count(acc) != 1:
        return False
    child = _call_first(acc, ("get_child_at_index",), 0)
    if child is None or _role_name(child) != "toggle button":
        return False
    if "AXPress" not in _action_names(child) or _node_name(acc) != _node_name(child):
        return False
    position, size = _extents(acc, keep_zero=True)
    return (
        position is not None and size is not None
        and size[0] > 0 and size[1] > 0
        and (position, size) == _extents(child, keep_zero=True)
    )


def _action_names(acc) -> tuple[str, ...]:
    """AX action names for this node, mapped from its AT-SPI actions."""
    if gtk4_tree_expander(acc):
        return ("AXPress",)
    action = _action_iface(acc)
    if action is None:
        return ()
    n = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    out: list[str] = []
    for i in range(int(n)):
        raw = (_call_first(action, ("get_action_name", "get_name"), i, default="") or "").lower()
        if raw in _PRESS_ACTION_NAMES:
            out.append("AXPress")
        elif raw in _PICK_ACTION_NAMES:
            out.append("AXPick")
    return tuple(dict.fromkeys(out))  # de-dup, preserve order


# Roles that never carry a text/numeric value — reading one costs two wasted
# D-Bus round-trips per node (Text + Value iface probes that always fail), and
# groups/buttons/windows are the bulk of a tree. Their label lives in the Name
# (title), not the value, so skipping the value read is a pure speedup.
_NO_VALUE_ROLES = frozenset({
    "AXButton", "AXImage", "AXCanvas", "AXGroup", "AXWindow", "AXToolbar", "AXMenuBar",
    "AXMenu", "AXScrollBar", "AXScrollArea", "AXSplitter", "AXTabGroup", "AXUnknown",
    "AXStatusBar", "AXPopover", "AXCalendar", "AXTab", "AXTooltip",
})


# Chromium embeds each list option in the parent's text as U+FFFC (object
# replacement). That character is not the selected option.
_OBJECT_REPLACEMENT = "\ufffc"
_CHOICE_ROLES = frozenset({"AXComboBox", "AXList", "AXPopUpButton"})
_CHOICE_ROLE_NAMES = frozenset({"combo box", "list box", "list"})
_OPTION_ROLE_NAMES = frozenset({
    "menu item", "check menu item", "radio menu item", "list item", "list box item",
})
_OPTION_CONTAINER_NAMES = frozenset({
    "menu", "popup menu", "list", "list box", "panel", "filler", "scroll pane", "combo box",
})
_ROW_ROLE_NAMES = frozenset({
    "table cell", "tree item", "list item", "table row", "row", "list box item",
})
# A flat GTK tree cell's first action is expand or edit. Performing it returns
# success and does not move the selection. A Chrome list option's click does.
_SELECTING_ACTION_NAMES = frozenset({
    "click", "press", "select", "pick", "jump", "toggle", "do default",
})
_TEXT_ROLE_NAMES = frozenset({
    "entry", "text", "password text", "terminal", "document text", "paragraph",
})
# Roles whose Value interface is a real range. Qt exposes Value on labels,
# checks, rows, and empty text fields too; those numbers are 0.0 or an
# uninitialized double, not a value the widget holds.
_RANGE_ROLE_NAMES = frozenset({"slider", "spin button", "progress bar", "level bar"})


def _strip_objects(text: str) -> str:
    return text.replace(_OBJECT_REPLACEMENT, "").strip()


# Qt rich-text names (FeatherPad's word-count control) are HTML. Only these
# names are tags. ``<not-a-tag>`` and ``a < b`` are text and stay.
_HTML_TAG_NAMES = (
    "a|b|i|u|s|p|br|hr|img|div|span|em|strong|font|qt|html|body|head|title|"
    "table|thead|tbody|tfoot|tr|td|th|ul|ol|li|pre|blockquote|center|big|small|"
    "sub|sup|tt|nobr|code|meta|style|script|h1|h2|h3|h4|h5|h6|dl|dt|dd|caption"
)
# The name starts immediately after ``<`` or ``</``. ``a < b and c > d`` is
# not a tag: HTML does not allow a space between ``<`` and the name.
# Quoted attributes may contain angle brackets; require the full tag name
# so a literal ``<p-custom>`` is not mistaken for a paragraph.
_HTML_TAG_RE = re.compile(
    rf"<(/)?({_HTML_TAG_NAMES})(?=[\s/>])"
    r"((?:[^<>\"']|\"[^\"]*\"|'[^']*')*)>",
    re.IGNORECASE,
)
_HTML_VOID_TAGS = frozenset({"br", "hr", "img", "meta"})
_HTML_BLOCK_TAGS = frozenset({
    "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "qt", "html",
    "body", "pre", "blockquote", "ul", "ol", "table", "head", "title", "dl",
    "dt", "dd", "caption",
})


def _html_markup(tags: list[re.Match[str]]) -> bool:
    """True when ``tags`` are rich text, not a lone ``<a>`` or ``<not-a-tag>``.

    A tag with attributes, a void tag, or an open/close pair is markup.
    ``<a>`` with no attributes and no closer is left alone, and so is any
    name that is not an HTML tag.
    """
    opened: set[str] = set()
    closed: set[str] = set()
    for match in tags:
        name = match.group(2).lower()
        rest = (match.group(3) or "").strip(" /")
        if name in _HTML_VOID_TAGS or rest:
            return True
        if match.group(1):
            closed.add(name)
        else:
            opened.add(name)
    return bool(opened & closed)


def _strip_known_markup(text: str) -> str:
    """Remove rich-text tags. Text that is not markup is returned unchanged."""
    tags = list(_HTML_TAG_RE.finditer(text))
    if not _html_markup(tags):
        return text

    def replace(match: re.Match[str]) -> str:
        name = match.group(2).lower()
        if name in _HTML_VOID_TAGS or (match.group(1) and name in _HTML_BLOCK_TAGS):
            return " "
        return ""

    cleaned = _HTML_TAG_RE.sub(replace, text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    return cleaned.strip()


def _plain_accessible_name(text: str) -> str:
    """Accessible name with rich-text markup removed and entities decoded.

    ``<p style='white-space:pre'>Calculate number of words</p>`` becomes
    ``Calculate number of words``. ``&amp;`` becomes ``&``. ``<not-a-tag>``,
    ``a < b``, and a bare ``<a>`` are not markup and are kept. A name with
    no ``<`` and no ``&`` is returned unchanged.
    """
    if "<" not in text and "&" not in text:
        return text
    current = text
    for _ in range(3):
        decoded = html.unescape(current)
        if decoded != current:
            decoded = decoded.replace("\xa0", " ")
        stripped = _strip_known_markup(decoded)
        if stripped == current:
            return current
        current = stripped
    return current


# An unnamed icon's Text is a caption, not a document. Evince thumbnail
# page numbers are one or two characters. 80 holds a label and does not
# raise the snapshot value caps.
_IMAGE_LABEL_CHARS = 80


def _image_text_label(acc) -> str:
    """Text of an unnamed icon or image, used as its name.

    Evince thumbnail icons have an empty name and a Text interface whose
    string is the page label (``1``, ``2``, ``3``). A missing interface, a
    blank string, or a generic word such as ``image`` stays empty, and the
    pruner can still fold the icon. The read is bounded. ``get_text`` is
    not asked for ``-1``.
    """
    if is_secure(acc):
        return ""
    try:
        Atspi = _atspi()
    except ImportError:
        return ""
    count = _safe(lambda: Atspi.Text.get_character_count(acc))
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return ""
    limit = min(int(count), _IMAGE_LABEL_CHARS)
    got = _safe(lambda: Atspi.Text.get_text(acc, 0, limit))
    if not isinstance(got, str):
        return ""
    text = " ".join(
        got.replace("\u00a0", " ").replace(_OBJECT_REPLACEMENT, " ").split()
    )
    if not text or _generic_region_name(text):
        return ""
    return text


def _is_choice_role(role: str, role_name: str) -> bool:
    return role in _CHOICE_ROLES or role_name in _CHOICE_ROLE_NAMES


def _norm_nbsp(text: str | None) -> str | None:
    """U+00A0 is a space. Chrome stores edge spaces in contenteditable as NBSP."""
    if text is None:
        return None
    return text.replace("\u00a0", " ")


def _texts_match(got: str | None, wanted: str | None) -> bool:
    if got is None or wanted is None:
        return False
    return _norm_nbsp(got) == _norm_nbsp(wanted)


def paragraph_breaks_match(got: str | None, wanted: str | None) -> bool:
    """True when the only difference is which paragraph separator was used.

    Newline, CR, CRLF, VT, FF, NEL, U+2028, U+2029, and U+FFFC are the same
    break. ``\\r\\n`` is one break, not two. ``a\\n\\nb`` is not ``a\\nb``,
    ``a b`` is not ``a\\nb``, and ``ab`` is not ``a\\nb``. A trailing break
    is kept.
    """
    from a11y_computer_use.outcome import paragraph_breaks_match as same_breaks

    return same_breaks(got, wanted)


def _text_is_blank(text: str | None) -> bool:
    """True when the snapshot read is empty, unreadable, or only whitespace.

    Chrome's empty contenteditable reads back as a newline (the ``<br>``) or a
    single space, not ``""``. That leftover is not the user's text.
    """
    if text is None:
        return True
    return _norm_nbsp(text).strip() == ""


def _direct_text(acc) -> str:
    if is_secure(acc):
        return ""
    got = _safe(lambda: _atspi().Text.get_text(acc, 0, -1))
    return got if isinstance(got, str) else ""


def _hypertext_objects(acc) -> list:
    """Accessibles Chromium embeds as U+FFFC, in text order.

    ``Hypertext.get_link`` is that mapping. A node with no hypertext (or a
    fake that does not implement it) returns an empty list so the caller can
    fall back to the children whose text is not already in the parent string.
    """
    try:
        Atspi = _atspi()
    except Exception:
        return []
    hyper = getattr(Atspi, "Hypertext", None)
    link_cls = getattr(Atspi, "Hyperlink", None)
    if hyper is None or link_cls is None:
        return []
    count = _safe(lambda: hyper.get_n_links(acc))
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return []
    objects = []
    for index in range(min(int(count), 64)):
        link = _safe(lambda index=index: hyper.get_link(acc, index))
        obj = None
        if link is not None:
            obj = _safe(lambda link=link: link_cls.get_object(link, 0))
        objects.append(obj)
    return objects


def _embedded_targets(acc, text: str) -> list:
    """The accessibles that stand in for each U+FFFC in ``text``, in order."""
    needed = text.count(_OBJECT_REPLACEMENT)
    objects = _hypertext_objects(acc)
    if objects:
        return objects[:needed]
    plain = text.replace(_OBJECT_REPLACEMENT, "")
    embedded = []
    for index in range(min(_child_count(acc), 64)):
        child = _child_at(acc, index)
        if child is None:
            continue
        visible = _direct_text(child).replace(_OBJECT_REPLACEMENT, "").strip()
        if visible and visible in plain:
            continue
        embedded.append(child)
    return embedded[:needed]


def _object_text(obj, depth: int, surrounding: str) -> str:
    """Text to splice in place of one embedded-object character.

    A choice control is not spliced: its options are U+FFFC too, and the
    selected option is already the control's own value. An empty text field
    contributes its name when that name is not already in the surrounding
    words, so ``<p><input aria-label="Bare para input"></p>`` is not blank.
    """
    if obj is None or depth > 6 or is_secure(obj):
        return ""
    role_name = _role_name(obj)
    role = _ROLE.get(role_name, "AXGroup")
    if _is_choice_role(role, role_name):
        return ""
    raw = _direct_text(obj)
    if _OBJECT_REPLACEMENT in raw:
        expanded = _expand_embedded(obj, raw, depth + 1)
        if expanded.strip():
            return expanded
        raw = raw.replace(_OBJECT_REPLACEMENT, "")
    if raw.strip():
        return raw
    name = _node_name(obj)
    if name and name not in surrounding:
        return name
    return ""


def _section_text(acc, role_str: str, attrs: dict) -> str | None:
    """Read a text-bearing HTML section's own Text interface.

    Firefox and WebKitGTK expose pre, code, and quote text on sections
    without static-text children. AT-SPI maps sections to groups, so the
    value read is skipped. Chromium's text is the sentence with one U+FFFC
    per link, and the same words are also separate static-text children, so
    find cannot match a phrase that crosses them. Firefox exposes that same
    string and no static-text children, so the valueless group collapses
    onto the link and the words disappear. A div whose text is only an
    embedded control (a button wrapper, ``\\ufffc``) stays valueless and
    still collapses onto that control. A choice control is not spliced in.
    """
    if role_str != "section":
        return None
    if str(attrs.get("tag") or "").lower() not in {
        "div", "pre", "code", "blockquote", "address", "figcaption",
    }:
        return None
    raw = _direct_text(acc)
    if not raw.replace(_OBJECT_REPLACEMENT, "").strip():
        return None
    if _OBJECT_REPLACEMENT in raw:
        expanded = _expand_embedded(acc, raw)
        if expanded.strip():
            return expanded
    return raw.strip() or None


_BLOCK_TAGS = frozenset({
    "p", "div", "li", "pre", "blockquote", "address",
    "h1", "h2", "h3", "h4", "h5", "h6",
})


def _block_boundary(obj) -> bool:
    """True when ``obj`` is a paragraph or block, not an inline embed.

    A bold word and a link are inline. A contenteditable paragraph, heading,
    or block tag is the boundary a newline asked for.
    """
    if obj is None:
        return False
    try:
        role = _role_name(obj)
    except Exception:
        return False
    if role in {"paragraph", "heading"}:
        return True
    try:
        tag = str(_get_attributes(obj).get("tag") or "").lower()
    except Exception:
        return False
    return tag in _BLOCK_TAGS


def _with_block_boundary(preceding: str, obj, piece: str) -> str:
    """``piece`` with a newline in front when ``obj`` starts another block.

    The first block has no leading break. A piece that already starts with a
    break is left as it is. An empty block still contributes the break, so
    two boundaries stay two.
    """
    if not preceding or not _block_boundary(obj):
        return piece
    if piece.startswith(("\n", "\r", "\u2028", "\u2029")):
        return piece
    return "\n" + piece


def _expand_embedded(acc, text: str, depth: int = 0) -> str:
    """Replace each U+FFFC with the embedded child's text.

    ``"The \\ufffc fox jumps over the \\ufffc dog."`` with a link "quick brown"
    and an emphasis "lazy" becomes "The quick brown fox jumps over the lazy
    dog." A leftover object character with no child is dropped, which is what
    a label around a select already did. A paragraph or block boundary is a
    newline between those children, not a character that is dropped.
    """
    if not isinstance(text, str):
        return ""
    if depth > 6 or _OBJECT_REPLACEMENT not in text:
        return text.replace(_OBJECT_REPLACEMENT, "").strip()
    targets = _embedded_targets(acc, text)
    parts = text.split(_OBJECT_REPLACEMENT)
    surrounding = "".join(parts)
    out: list[str] = []
    for index, part in enumerate(parts):
        out.append(part)
        if index >= len(parts) - 1:
            break
        obj = targets[index] if index < len(targets) else None
        piece = _object_text(obj, depth + 1, surrounding)
        out.append(_with_block_boundary("".join(out), obj, piece))
    return "".join(out).strip()


def _option_label(node) -> str:
    if is_secure(node):
        return ""
    label = _node_name(node)
    if label:
        return label
    raw = _safe(lambda n=node: _atspi().Text.get_text(n, 0, -1))
    if isinstance(raw, str):
        return _strip_objects(raw)
    return ""


def _iface_selected_labels(acc) -> list[str] | None:
    """Labels from the Selection interface, or None when it names nothing.

    Every selected child is included. Index 0 alone is what a multi-select
    listbox used to show, hiding the rest. A child with no label is skipped
    so the state walk can still see the option.
    """
    iface = _selection_iface(acc)
    if iface is None:
        return None
    labels: list[str] = []
    count = _call_first(iface, ("get_n_selected_children", "getNSelectedChildren"))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        for index in range(min(int(count), 64)):
            child = _call_first(iface, ("get_selected_child", "getSelectedChild"), index)
            if child is None:
                continue
            label = _option_label(child)
            if label and label not in labels:
                labels.append(label)
        if len(labels) > 1:
            return labels
    child = _call_first(iface, ("get_selected_child", "getSelectedChild"), 0)
    if child is None:
        return labels or None
    label = _option_label(child)
    if label and label not in labels:
        labels.append(label)
    return labels or None


def _state_selected_labels(acc, *, skip_menus: bool) -> list[str]:
    """SELECTED option labels in tree order.

    ``skip_menus`` leaves a popup menu alone. GTK marks the highlighted row
    SELECTED there without changing the combo. A listbox's own items are not
    in a menu, so a multi-select still reports every selected option.
    """
    labels: list[str] = []

    def walk(node, depth: int) -> None:
        if depth > 4:
            return
        count = min(_child_count(node), 64)
        for index in range(count):
            child = _child_at(node, index)
            if child is None:
                continue
            role = _role_name(child)
            if skip_menus and role in {"menu", "popup menu"}:
                continue
            if _state_has(child, "SELECTED"):
                label = _option_label(child)
                if label and label not in labels:
                    labels.append(label)
                continue
            if role in _OPTION_CONTAINER_NAMES:
                walk(child, depth + 1)

    walk(acc, 0)
    return labels


def _selected_option_text(acc) -> str | None:
    """The combo or list's selected option text, every one of them.

    ``Selection.get_selected_child`` is the active item (GTK's combo uses it
    for ``gtk_combo_box_get_active``). A popup menu can mark a row SELECTED
    when that row is only highlighted, which leaves the combo unchanged, so
    that menu is not merged in. A multi-select listbox has several selected
    children; all of their names are shown, in tree order, separated by
    ", ". One selected option is that option's text alone.
    """
    iface_labels = _iface_selected_labels(acc)
    if iface_labels is not None and len(iface_labels) > 1:
        return ", ".join(iface_labels)
    outside_menus = _state_selected_labels(acc, skip_menus=True)
    if outside_menus and (not iface_labels or len(outside_menus) > len(iface_labels)):
        return ", ".join(outside_menus)
    if iface_labels:
        return ", ".join(iface_labels)
    nested = _state_selected_labels(acc, skip_menus=False)
    if not nested:
        return None
    return ", ".join(nested)


def _toolkit_name(acc) -> str:
    """AT-SPI toolkit name for ``acc``'s application, cached per application.

    Qt and GTK share the GI client. The server is what interprets insert
    length and which Value numbers are real, so the split is the toolkit
    name (``Qt`` or ``gtk``), not the binding module.
    """
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    cached = getattr(app, "_a11y_toolkit_name", None)
    if isinstance(cached, str):
        return cached
    name = (_call_first(app, ("get_toolkit_name", "getToolkitName"), default="") or "")
    text = str(name).lower()
    try:
        setattr(app, "_a11y_toolkit_name", text)
    except Exception:
        pass
    return text


def _qt_app(acc) -> bool:
    """True when this node belongs to Qt. GTK and a fake with no toolkit are not."""
    return "qt" in _toolkit_name(acc)


def _java_app(acc) -> bool:
    """True when this node belongs to the Java ATK wrapper.

    The toolkit name is ``JAVA``. GTK, Qt, and a fake with no toolkit are not.
    """
    return "java" in _toolkit_name(acc)


def _gtk_combo(acc) -> bool:
    """A GTK combo box. Chromium, Firefox, Qt, and a fake with no toolkit are not.

    The toolkit name is what distinguishes a GtkComboBox from a web select
    that happens to use the same AT-SPI role. An empty toolkit stays on the
    EXPANDABLE flag the snapshot already had.
    """
    if acc is None or _role_name(acc) != "combo box":
        return False
    try:
        if _chromium_app(acc) or _gecko_app(acc) or _qt_app(acc):
            return False
    except Exception:
        return False
    return "gtk" in _toolkit_name(acc)


def _toolkit_version(acc) -> str:
    """AT-SPI toolkit version for ``acc``'s application, cached per application.

    An empty string means the application did not report one. Callers that
    need GTK 4 do not guess from a missing version.
    """
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    cached = getattr(app, "_a11y_toolkit_version", None)
    if isinstance(cached, str):
        return cached
    version = _call_first(app, ("get_toolkit_version", "getToolkitVersion"), default="") or ""
    text = str(version).strip()
    try:
        setattr(app, "_a11y_toolkit_version", text)
    except Exception:
        pass
    return text


def _gtk_major(acc) -> int | None:
    """Major GTK version, or None when the toolkit is not GTK or has no version."""
    if "gtk" not in _toolkit_name(acc):
        return None
    text = _toolkit_version(acc)
    if not text:
        return None
    head = text.split(".", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def _gtk4_app(acc) -> bool:
    """True for GTK 4 or newer. GTK 3, Qt, Chromium, and a missing version are not."""
    major = _gtk_major(acc)
    return major is not None and major >= 4


def _gtk4_frame_size(acc) -> tuple[int, int]:
    """Client size of ``acc``'s frame, from WINDOW extents, else SCREEN."""
    frame = _frame_ancestor(acc) or acc
    rect = _raw_rect(frame, "WINDOW")
    if rect is None or rect[2] <= 0 or rect[3] <= 0:
        rect = _raw_rect(frame, "SCREEN")
    if rect is None or rect[2] <= 0 or rect[3] <= 0:
        return 0, 0
    return int(rect[2]), int(rect[3])


def _gtk4_client_origin(acc, cache: dict | None = None) -> tuple[int, int] | None:
    """X client origin for a GTK 4 accessible, or None when it cannot be read.

    The result is cached by pid and frame size for one snapshot. None is
    cached too, so a missing window is not looked up again for every node.
    """
    if not _gtk4_app(acc):
        return None
    pid = pid_of(acc)
    if not pid:
        app = _call_first(acc, ("get_application", "getApplication"))
        pid = pid_of(app)
    width, height = _gtk4_frame_size(acc)
    key = (int(pid or 0), width, height)
    if cache is not None and key in cache:
        return cache[key]
    origin: tuple[int, int] | None = None
    if pid:
        try:
            from a11y_computer_use.drivers import _linux_system

            origin = _linux_system.gtk4_content_origin(int(pid), width, height)
        except Exception:
            origin = None
    if cache is not None:
        cache[key] = origin
    return origin


def gtk4_recovered_box(acc, position, size, cache: dict | None = None):
    """Screen position and size for a GTK 4 node, or None to keep SCREEN.

    GTK 4 reports every SCREEN origin as (0, 0) while WINDOW coordinates are
    relative to the X client window. The screen point is that window's origin
    plus the WINDOW box. A sum that is already the SCREEN point is left
    alone, including a window that really sits at the origin. A missing
    origin is None: the SCREEN point is not replaced with a guess. Negative
    extents and a non-zero SCREEN origin are left alone.
    """
    if position is None or size is None or not _gtk4_app(acc):
        return None
    x, y = float(position[0]), float(position[1])
    width, height = float(size[0]), float(size[1])
    if width < 0 or height < 0 or x < 0 or y < 0:
        return None
    if abs(x) > 0.5 or abs(y) > 0.5:
        return None
    window = _raw_rect(acc, "WINDOW")
    if window is None or window[2] <= 0 or window[3] <= 0:
        return None
    origin = _gtk4_client_origin(acc, cache)
    if origin is None:
        return None
    nx = float(origin[0]) + float(window[0])
    ny = float(origin[1]) + float(window[1])
    if abs(nx - x) <= 1 and abs(ny - y) <= 1:
        return None
    return (nx, ny), (float(window[2]), float(window[3]))


def gtk4_in_window_dialog(acc) -> bool:
    """GTK 4 dialog descendants whose WINDOW origin is not a screen box.

    AdwAlertDialog is nested inside a frame, rather than an X window of its
    own. Its positive WINDOW coordinates can still omit dialog offsets (#520).
    """
    if acc is None or not _gtk4_app(acc):
        return False
    dialog = False
    for node in _ancestor_chain(acc):
        role = _role_name(node)
        if role in {"alert", "dialog"}:
            dialog = True
        elif role in {"frame", "window"}:
            return dialog
    return False


def gtk4_window_origin_untrusted(acc) -> bool:
    """GTK 4 header-bar boxes can lie outside the frame's content origin.

    Adding the recovered content origin fixes body widgets, but does not
    make boxes outside the frame reliable pointer targets (#502). The frame
    itself can start at a negative WINDOW offset; a body row fully inside
    that reported frame is still a valid target (#617). Missing frame bounds
    leave the negative-origin guard in place.
    """
    if acc is None or not _gtk4_app(acc):
        return False
    window = _raw_rect(acc, "WINDOW")
    if window is None or (window[0] >= 0 and window[1] >= 0):
        return False
    frame = _frame_ancestor(acc)
    frame_box = _raw_rect(frame, "WINDOW") if frame is not None else None
    if (frame_box is not None and all(math.isfinite(value) for value in frame_box)
            and frame_box[2] > 0 and frame_box[3] > 0):
        x, y, width, height = window
        fx, fy, fw, fh = frame_box
        if (width > 0 and height > 0 and fx <= x and fy <= y
                and x + width <= fx + fw and y + height <= fy + fh):
            return False
    return True


def published_origin_untrusted(acc) -> bool:
    """True when a box published at screen (0, 0) is not that pixel.

    GTK 4's SCREEN origin is (0, 0) while the WINDOW box is elsewhere, and
    the X client origin was not added (or could not be read). A window whose
    origin plus its WINDOW box is (0, 0) really is at the origin. Other
    toolkits, and a GTK build with no version, are not this case.
    """
    if acc is None or not _gtk4_app(acc):
        return False
    screen = _raw_rect(acc, "SCREEN")
    window = _raw_rect(acc, "WINDOW")
    if screen is None or window is None:
        return True
    if screen[2] <= 0 or screen[3] <= 0 or window[2] <= 0 or window[3] <= 0:
        return True
    if abs(float(screen[0])) > 0.5 or abs(float(screen[1])) > 0.5:
        return False
    origin = _gtk4_client_origin(acc, None)
    if origin is None:
        return True
    nx = float(origin[0]) + float(window[0])
    ny = float(origin[1]) + float(window[1])
    return abs(nx) > 1 or abs(ny) > 1


def _sane_range(low: float, high: float) -> bool:
    """True when minimum and maximum are a real widget range.

    An uninitialized Qt double is finite and tiny (about ``1e-310``), and the
    minimum, maximum, and current value are the same garbage. A range needs
    a finite minimum strictly below a finite maximum, and neither end may be
    a subnormal.
    """
    if not math.isfinite(low) or not math.isfinite(high) or not low < high:
        return False
    for number in (low, high):
        if number != 0.0 and abs(number) < 1e-200:
            return False
    return True


# LibreOffice GTK dialogs and checkboxes expose an AtkValue that is not
# AtkValue. CurrentValue then reads an uninitialized double (about
# 6.93e-310), which is subnormal. Zero and ordinary small values stay.
_UNINITIALIZED_VALUE_ROLES = frozenset({"AXDialog", "AXCheckBox"})
_UNINITIALIZED_VALUE_ROLE_NAMES = frozenset({"dialog", "alert", "check box"})
#: An empty text control whose character count is 0. ``document text`` is
#: absent: a Writer document's text lives in its paragraphs, and that read
#: has to run. A filler is absent too, so its Value stays.
_EMPTY_TEXT_VALUE_NAMES = frozenset({
    "entry", "text", "password text", "terminal", "paragraph",
})
#: Roles whose Value interface is not the text a person reads. A subnormal
#: double on these is dropped. A filler is not here.
_SUBNORMAL_TEXT_ROLES = frozenset({
    "AXTextField", "AXTextArea", "AXSearchField", "AXStaticText", "AXHeading",
})
_SUBNORMAL_TEXT_ROLE_NAMES = frozenset({
    "entry", "text", "static", "label", "password text", "paragraph",
    "terminal", "document text",
})


def _uninitialized_double(value: object) -> bool:
    """True for a non-finite number or a subnormal double.

    ``0``, ``0.0``, and small normalized values such as ``0.001`` and
    ``1e-6`` are not this. The uninitialized LibreOffice reading is.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    if not math.isfinite(number):
        return True
    return number != 0.0 and abs(number) < sys.float_info.min


def _relation_type_token(value) -> str:
    parts = (
        value,
        getattr(value, "value_name", ""),
        getattr(value, "value_nick", ""),
    )
    return " ".join(str(part) for part in parts).lower().replace("-", "_")


def _relation_set(acc):
    """Relations on ``acc``. Fakes expose ``get_relation_set``. GI uses the class form."""
    if not hasattr(type(acc), "__gtype__"):
        got = _call_first(acc, ("get_relation_set", "getRelationSet"))
        if got:
            return got
    return _safe(lambda: _atspi().Accessible.get_relation_set(acc)) or []


def _gi_object(obj) -> bool:
    return hasattr(type(obj), "__gtype__")


def _relation_kind(rel) -> str:
    rtype = None
    if not _gi_object(rel):
        rtype = _call_first(rel, ("get_relation_type", "getRelationType"))
    if rtype is None:
        rtype = _safe(lambda: _atspi().Relation.get_relation_type(rel))
    if rtype is None:
        return ""
    return _relation_type_token(rtype)


def _relation_targets(acc, kind: str) -> list:
    """Targets of relations whose type token contains ``kind``."""
    wanted = kind.lower().replace("-", "_")
    targets: list = []
    for rel in _relation_set(acc):
        if wanted not in _relation_kind(rel):
            continue
        count = None
        if not _gi_object(rel):
            count = _call_first(rel, ("get_n_targets", "getNTargets"))
        if not isinstance(count, int) or isinstance(count, bool):
            count = _safe(lambda r=rel: _atspi().Relation.get_n_targets(r))
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        for index in range(int(count)):
            target = None
            if not _gi_object(rel):
                target = _call_first(rel, ("get_target", "getTarget"), index)
            if target is None:
                target = _safe(lambda r=rel, i=index: _atspi().Relation.get_target(r, i))
            if target is not None:
                targets.append(target)
    return targets


def _labelled_by_name(acc) -> str:
    """Name of the first LABELLED_BY target, or ``""``."""
    for target in _relation_targets(acc, "labelled_by"):
        label = _node_name(target)
        if label:
            return label
    return ""


def _strip_label_colon(label: str) -> str:
    """``Orientation:`` is the label text. The control's title is ``Orientation``."""
    text = label.strip()
    if text.endswith(":"):
        text = text[:-1].rstrip()
    return text


def _combo_display_name(acc, name: str) -> str:
    """Prefer a label over GTK's selected-item name, preserving explicit names.

    Qt keeps its existing relation override. Unnamed combos use LABELLED_BY;
    GTK combos named by their displayed value also allow measured row labels.
    """
    if _qt_app(acc):
        label = _labelled_by_name(acc)
        if label and label != name:
            return label
        return name
    if _gtk_combo(acc) and name.strip():
        _, value = _choice_value(acc, "AXComboBox", "combo box")
        if value is not None and name == _plain_accessible_name(value):
            label = _plain_accessible_name(_labelled_by_name(acc))
            if label:
                return _strip_label_colon(label) or name
            return _gtk_row_label_name(acc, name)
    if str(name).strip():
        return name
    label = _labelled_by_name(acc)
    if not label:
        return name
    title = _strip_label_colon(label)
    return title or name


def _gtk_button_row_name(acc, name: str) -> str:
    """Name ambiguous GTK grid buttons from measured same-row sibling labels.

    Explicit relations win; unique names and missing or ambiguous geometry
    retain the raw name. GTK child order need not match screen row order.
    """
    if "gtk" not in _toolkit_name(acc):
        return name
    label = _labelled_by_name(acc)
    if label:
        return _strip_label_colon(label) or name
    return _gtk_row_label_name(acc, name, ambiguous_button=True)


def _gtk_row_label_name(acc, name: str, *, ambiguous_button: bool = False) -> str:
    """Use a measured left-hand label in the same native parent and row."""
    parent = _parent_of(acc)
    if parent is None or _role_name(parent) not in {"panel", "filler", "grouping"}:
        return name
    siblings = [_child_at(parent, i) for i in range(_child_count(parent))]
    if ambiguous_button and name.strip() and sum(
        _role_name(child) == "push button" and _node_name(child) == name
        for child in siblings if child is not None
    ) < 2:
        return name
    position, size = _extents(acc)
    if position is None or size is None:
        return name
    x, y = position
    width, height = size
    if not all(math.isfinite(v) for v in (*position, *size)) or min(width, height) <= 0:
        return name
    candidates = []
    for child in siblings:
        if child is None or _role_name(child) not in {"label", "static", "static text"}:
            continue
        text = _node_name(child)
        pos, span = _extents(child)
        if not text.strip() or pos is None or span is None:
            continue
        if not all(math.isfinite(v) for v in (*pos, *span)) or min(span) <= 0:
            continue
        left, top = pos
        w, h = span
        if left + w <= x and top < y + height and top + h > y:
            candidates.append((x - left - w, text))
    candidates.sort()
    if not candidates or (len(candidates) > 1 and candidates[0][0] == candidates[1][0]):
        return name
    return _strip_label_colon(candidates[0][1]) or name


def _gtk_entry_row_name(acc) -> str:
    """Recover an unnamed single-line GTK entry's unambiguous row label."""
    if "gtk" not in _toolkit_name(acc):
        return ""
    label = _labelled_by_name(acc)
    if label:
        return _strip_label_colon(_plain_accessible_name(label))
    parent = _parent_of(acc)
    if parent is None or _role_name(parent) not in {"panel", "filler", "grouping"}:
        return ""

    def box(node):
        pos, size = _extents(node)
        if pos is None or size is None:
            return None
        if not all(math.isfinite(v) for v in (*pos, *size)) or min(size) <= 0:
            return None
        return (*pos, *size)

    target = box(acc)
    if target is None:
        return ""
    x, y, _, height = target
    siblings = [_child_at(parent, i) for i in range(_child_count(parent))]
    candidates = []
    for child in siblings:
        if child is None or _role_name(child) not in {"label", "static", "static text"}:
            continue
        text = _plain_accessible_name(_node_name(child)).strip()
        rect = box(child)
        if not text or rect is None:
            continue
        left, top, w, h = rect
        if left + w <= x and y <= top and top + h <= y + height:
            candidates.append(text)
    if len(candidates) != 1:
        return ""
    # Multiple editors on one row do not establish which owns the label.
    for child in siblings:
        if child is None or child == acc or _role_name(child) not in {"entry", "text", "password text"}:
            continue
        rect = box(child)
        if rect is None:
            return ""
        _, top, _, h = rect
        if top < y + height and top + h > y:
            return ""
    return _strip_label_colon(candidates[0])


def _aria_spinbutton(acc, role_name: str | None = None, attrs: dict | None = None) -> bool:
    """True for a Chromium custom ``role=spinbutton``, not a native number input.

    Chrome exposes both as AT-SPI ``spin button``. The custom control's
    ``xml-roles`` is ``spinbutton`` and its tag is not an ``<input
    type=number>``. A date, time, or month segment also carries
    ``xml-roles=spinbutton``; that segment stays on the valuetext path.
    A GTK spin button is not Chromium.
    """
    role = role_name if role_name is not None else _role_name(acc)
    if role != "spin button" or not _chromium_app(acc):
        return False
    if _number_input(acc) or _chrome_date_segment(acc):
        return False
    if attrs is None:
        attrs = _get_attributes(acc)
    xml = {
        part.strip().lower()
        for part in str(attrs.get("xml-roles") or "").split()
        if part.strip()
    }
    return "spinbutton" in xml


def _aria_spin_value(acc) -> str | None:
    """The spin button's displayed value, then its minimum and maximum.

    ``valuetext`` is Chrome's ``GetValueForControl``: ``aria-valuetext`` when
    that attribute is set, otherwise the numeric value. A blank attribute
    falls back to the Value interface (``aria-valuenow``). Minimum and
    maximum are included only when that interface reports a real range, the
    same range a slider's value comes from.
    """
    raw = _get_attributes(acc).get("valuetext")
    shown = raw.strip() if isinstance(raw, str) else ""
    if not shown:
        if not _value_interface_present(acc):
            return None
        cur = _safe(lambda: _atspi().Value.get_current_value(acc))
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            return None
        number = float(cur)
        if not math.isfinite(number):
            return None
        shown = _format_bound(number)
    span = _value_range(acc)
    if span is None or not _sane_range(*span):
        return shown
    low, high = span
    return f"{shown} (min {_format_bound(low)}, max {_format_bound(high)})"


def _rect_hits_box(
    rect, box: tuple[float, float, float, float], *, allow_zero_width: bool = False,
) -> bool | None:
    """Whether ``rect`` intersects ``box``, or None when the rect is unreadable.

    Zero-size glyphs are normally off the widget. Qt character reads may
    accept a zero-width glyph with positive height at a measured in-box x.
    Coordinates are the same space as ``box`` (screen pixels).
    """
    if rect is None:
        return None
    try:
        x = float(getattr(rect, "x"))
        y = float(getattr(rect, "y"))
        width = float(getattr(rect, "width"))
        height = float(getattr(rect, "height"))
    except (TypeError, ValueError, AttributeError):
        return None
    if width < 0 or height <= 0 or (width == 0 and not allow_zero_width):
        return False
    left, top, box_w, box_h = box
    if width == 0:
        return left <= x < left + box_w and y < top + box_h and y + height > top
    return y < top + box_h and y + height > top and x < left + box_w and x + width > left


def _terminal_on_screen(acc, full: str) -> str:
    """The on-screen slice of a VTE buffer.

    ``Text.get_text`` is the scrollback plus the screen on some builds and
    only the screen on others. Characters above the widget are omitted and
    a leading ellipsis marks that cut. When a character cannot be placed,
    the whole buffer is returned and the snapshot cap keeps its tail.
    """
    text = full
    count = len(text)
    if count <= 1:
        return text
    box = _raw_rect(acc, "SCREEN")
    if box is None or box[2] <= 0 or box[3] <= 0:
        return text
    Atspi = _atspi()
    coord = getattr(getattr(Atspi, "CoordType", None), "SCREEN", 0)

    def on_screen(offset: int) -> bool | None:
        rect = _safe(lambda index=offset: Atspi.Text.get_character_extents(acc, index, coord))
        return _rect_hits_box(rect, box)

    first = on_screen(0)
    last = on_screen(count - 1)
    if first is None or last is None:
        return text
    start = 0
    end = count
    if not first:
        if not last:
            return text
        lo, hi = 0, count - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            flag = on_screen(mid)
            if flag is None:
                return text
            if flag:
                hi = mid
            else:
                lo = mid
        start = hi
    if not last:
        lo, hi = start, count - 1
        if not (0 <= start < count - 1):
            return text
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            flag = on_screen(mid)
            if flag is None:
                return text
            if flag:
                lo = mid
            else:
                hi = mid
        end = lo + 1
    screen = text[start:end]
    if not screen:
        return text
    if start > 0 and not screen.startswith("…"):
        return "…" + screen
    return screen


# Characters taken from a page's Text interface. The snapshot still clips
# what the model reads (`observe._MAX_VALUE_CHARS`) and what the line shows
# (`observe._RENDER_VALUE_CHARS`). Find searches this string, including the
# part past those caps. A longer page is cut here. This does not raise a
# walk, depth, or child cap, and ``get_text`` is not asked for ``-1``.
_PAGE_TEXT_CHARS = 4000


def _drain_atspi_replies(budget_s: float = 0.25) -> None:
    """Dispatch pending D-Bus replies (such as libatspi's hang-check ping).

    Bounded: at most ``budget_s`` seconds, non-blocking iterations only.
    """
    try:
        from gi.repository import GLib
    except ImportError:
        return
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if ctx.pending():
            ctx.iteration(False)
        else:
            time.sleep(0.01)
            if not ctx.pending():
                break


_unanswered_bounded_range_apps: set[str] = set()


def _qt_viewport_text(acc) -> str | None:
    """Only native text ranges inside a Qt editor's measured viewport.

    Qt Text.get_text exposes the whole document, including off-screen lines.
    An unavailable bounded-range interface is not evidence of visibility.
    """
    box = _raw_rect(acc, "SCREEN")
    if box is None or box[2] <= 0 or box[3] <= 0:
        return None
    if "qt" in _toolkit_name(acc):
        # Qt 5 and Qt 6 bridges cannot marshal QSpiTextRangeList. Avoid
        # paying a D-Bus timeout (and marking the app hung) on every read.
        # The fallback still verifies every published character's bounds.
        return _qt_character_viewport_text(acc, box)
    instance = app_instance_id(acc)
    if instance is not None and instance in _unanswered_bounded_range_apps:
        return _qt_character_viewport_text(acc, box)
    Atspi = _atspi()
    coord = getattr(getattr(Atspi, "CoordType", None), "SCREEN", 0)
    clip = getattr(getattr(Atspi, "TextClipType", None), "BOTH", None)
    ranges = None
    if clip is not None:
        ranges = _safe(lambda: Atspi.Text.get_bounded_ranges(
            acc, *map(int, box), coord, clip, clip,
        ))
    if clip is not None and _reply_local.last_failed:
        if instance is not None:
            _unanswered_bounded_range_apps.add(instance)
        # Qt 5 cannot marshal QSpiTextRangeList, so GetBoundedRanges never
        # answers. libatspi then marks the whole application hung until its
        # follow-up Peer.Ping reply is dispatched, which fails every later
        # read of this editor (and of the rest of the walk) at once.
        try:
            from a11y_computer_use.drivers import _atspi_events

            # Iterate the context on the thread that owns libatspi.
            _atspi_events.run(_drain_atspi_replies)
        except Exception:
            pass
    if ranges:
        # A malformed/marshalling-failed reply is as unusable as no reply.
        try:
            contents = [span.content for span in ranges]
            if any(contents) and all(isinstance(content, str) for content in contents):
                return "\n".join(contents)
        except Exception:
            pass
    return _qt_character_viewport_text(acc, box)


def _qt_character_viewport_text(acc, box=None) -> str | None:
    """Read text verified by native character boxes without GetBoundedRanges.

    KTextEditor cannot marshal bounded ranges reliably; keep that request
    off its bridge while retaining native visibility checks.
    """
    if box is None:
        box = _raw_rect(acc, "SCREEN")
    if box is None or box[2] <= 0 or box[3] <= 0:
        return None
    Atspi = _atspi()
    coord = getattr(getattr(Atspi, "CoordType", None), "SCREEN", 0)
    # Qt commonly leaves GetBoundedRanges unimplemented. Use native
    # point offsets and character rectangles instead, never the buffer
    # alone. These reads are repeated after every scroll.
    left, top, width, height = map(int, box)
    offsets = [
        _safe(lambda x=x, y=y: Atspi.Text.get_offset_at_point(acc, x, y, coord))
        for x in (left, left + width - 1)
        for y in (top, top + height - 1)
    ]
    if any(not isinstance(offset, int) or offset < 0 for offset in offsets):
        # Qt 5.15 cannot marshal bounded ranges and may reject corner
        # offsets too. Read a capped raw prefix, then verify visibility
        # through native character rectangles before making it searchable.
        count = _safe(lambda: Atspi.Text.get_character_count(acc))
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return None
        start, end = 0, min(count, _PAGE_TEXT_CHARS)
    else:
        start = min(offsets)
        end = min(max(offsets) + 1, start + _PAGE_TEXT_CHARS)
    text = _safe(lambda: Atspi.Text.get_text(acc, start, end))
    if not isinstance(text, str) or (not text and end > start):
        # Some Qt Text implementations reject explicit end offsets while
        # accepting the end-of-buffer sentinel. Only request the whole
        # buffer when its native count fits the existing read budget.
        count = _safe(lambda: Atspi.Text.get_character_count(acc))
        valid_count = isinstance(count, int) and not isinstance(count, bool) and count >= 0
        if valid_count and count <= _PAGE_TEXT_CHARS:
            text = _safe(lambda: Atspi.Text.get_text(acc, 0, -1))
            start, end = 0, count
        if not isinstance(text, str) or (not text and end > start):
            # A failed large read may still support small explicit reads.
            # Never publish a partial result when a chunk is unavailable.
            chunks = []
            for offset in range(start, end, 256):
                stop = min(offset + 256, end)
                chunk = _safe(lambda offset=offset, stop=stop:
                              Atspi.Text.get_text(acc, offset, stop))
                if not isinstance(chunk, str) or len(chunk) != stop - offset:
                    return None
                chunks.append(chunk)
            text = "".join(chunks)
    if not isinstance(text, str):
        return None
    visible = []
    clipped = False
    for index, char in enumerate(text[:end - start]):
        rect = _safe(lambda index=index: Atspi.Text.get_character_extents(
            acc, start + index, coord,
        ))
        # Qt reports combining marks, joiners and variation selectors with
        # zero advance. Their native position/height still establish visibility.
        hits = _rect_hits_box(rect, box, allow_zero_width=True)
        if hits is None:
            return None
        # Keep a separator at clipped characters so two disconnected
        # visible fragments cannot manufacture a substring match.
        if hits:
            if clipped and visible:
                visible.append("\n")
            visible.append(char)
            clipped = False
        else:
            clipped = True
    return "".join(visible)


def _evince_page(node) -> bool:
    """Evince PDF pages have reliable SHOWING state, but drifting extents."""
    if _role_name(node) != "page":
        return False
    app = _application_of(node)
    name = _call_first(app, ("get_name",), default="") if app is not None else ""
    return str(name).strip().lower() == "evince"


def _page_text(acc) -> str | None:
    """The page's Text interface, bounded by ``_PAGE_TEXT_CHARS``.

    Evince (and poppler) expose each PDF page as AT-SPI role ``page``. The
    name is empty. The words are ``Text.get_text`` from the start through
    the character count, or through ``_PAGE_TEXT_CHARS`` when the page is
    longer. ``None`` is a missing interface or a blank page. An embedded
    object character is expanded the same way a paragraph's is.
    """
    if is_secure(acc):
        return None
    try:
        Atspi = _atspi()
    except ImportError:
        return None
    count = _safe(lambda: Atspi.Text.get_character_count(acc))
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return None
    limit = min(int(count), _PAGE_TEXT_CHARS)
    got = _safe(lambda: Atspi.Text.get_text(acc, 0, limit))
    if not isinstance(got, str) or not got:
        return None
    if _OBJECT_REPLACEMENT in got:
        expanded = _expand_embedded(acc, got)
        if expanded.strip():
            got = expanded
        else:
            got = got.replace(_OBJECT_REPLACEMENT, "")
    text = got.replace("\u00a0", " ").strip()
    return text or None


def _gtk_multiline_document(acc) -> bool:
    """A GTK multi-line text widget whose Text interface is the whole buffer.

    Scintilla (Geany) and GtkTextView are this widget. A terminal already
    separates the screen from the scrollback. A single-line entry is the
    field, not a document. Chromium, Firefox, and Qt are not this path.
    """
    if acc is None or _role_name(acc) != "text":
        return False
    if _state_has(acc, "SINGLE_LINE") and not _state_has(acc, "MULTI_LINE"):
        return False
    toolkit = _toolkit_name(acc)
    if "gtk" not in toolkit or _qt_app(acc):
        return False
    try:
        if _chromium_app(acc) or _gecko_app(acc):
            return False
    except Exception:
        return False
    return True


def _fold_span(text: str, needle: str) -> tuple[str, str, int]:
    """Lowercased haystack and needle, and -1 when the casefold changes length.

    Offsets have to land on the original characters. A casefold that changes
    the length is not a place this can point at, so the caller does not guess.
    """
    folded = text.replace("\u00a0", " ").lower()
    wanted = needle.replace("\u00a0", " ").lower()
    if len(folded) != len(text) or len(wanted) != len(needle):
        return folded, wanted, -1
    return folded, wanted, 0


def _buffer_offsets(text: str, needle: str) -> list[int] | None:
    """Start offsets of ``needle`` in ``text``. None when the offset is not stable."""
    folded, wanted, broken = _fold_span(text, needle)
    if broken < 0 or not wanted:
        return None
    offsets: list[int] = []
    start = 0
    while True:
        at = folded.find(wanted, start)
        if at < 0:
            return offsets
        offsets.append(at)
        start = at + max(1, len(wanted))


def _char_place(acc, offset: int, box: tuple[float, float, float, float]) -> str | None:
    """Where the character at ``offset`` sits relative to ``box``.

    ``hit`` intersects the widget. ``above`` and ``below`` are a real rect
    that misses it vertically. ``miss`` is a zero-size glyph. None means the
    extent could not be read, and the caller does not invent a box.
    """
    Atspi = _atspi()
    coord = getattr(getattr(Atspi, "CoordType", None), "SCREEN", 0)
    rect = _safe(lambda index=offset: Atspi.Text.get_character_extents(acc, int(index), coord))
    if rect is None:
        return None
    hit = _rect_hits_box(rect, box)
    if hit is None:
        return None
    if hit:
        return "hit"
    try:
        y = float(getattr(rect, "y"))
        width = float(getattr(rect, "width"))
        height = float(getattr(rect, "height"))
    except (TypeError, ValueError, AttributeError):
        return None
    if width <= 0 or height <= 0:
        return "miss"
    _left, top, _width, box_h = box
    if y + height <= top:
        return "above"
    if y >= top + box_h:
        return "below"
    return "miss"


def _visible_text_span(acc, count: int, box: tuple[float, float, float, float]) -> tuple[int, int] | None:
    """Half-open span of characters that intersect ``box``.

    None when an extent cannot be read. ``(0, 0)`` when the characters that
    could be placed all miss the widget. The search assumes one visible
    band, which is a text view scrolled through a document.
    """
    if count <= 0:
        return (0, 0)

    def place(offset: int) -> str | None:
        flag = _char_place(acc, offset, box)
        if flag != "miss":
            return flag
        # A zero-size glyph (often a newline) is not a band edge. A nearby
        # character with a real rect is. Giving up here would hide a match
        # that the widget is showing.
        for step in range(1, 24):
            for candidate in (offset + step, offset - step):
                if 0 <= candidate < count:
                    nearby = _char_place(acc, candidate, box)
                    if nearby != "miss":
                        return nearby
        return None

    def first_not_above() -> int | None:
        lo, hi = 0, count - 1
        found = None
        while lo <= hi:
            mid = (lo + hi) // 2
            flag = place(mid)
            if flag is None or flag == "miss":
                return None
            if flag == "above":
                lo = mid + 1
            else:
                found = mid
                hi = mid - 1
        return found

    def last_not_below() -> int | None:
        lo, hi = 0, count - 1
        found = None
        while lo <= hi:
            mid = (lo + hi) // 2
            flag = place(mid)
            if flag is None or flag == "miss":
                return None
            if flag == "below":
                hi = mid - 1
            else:
                found = mid
                lo = mid + 1
        return found

    first = place(0)
    last = place(count - 1)
    if first is None or last is None or first == "miss" or last == "miss":
        return None
    if first == "below" or last == "above":
        return (0, 0)
    start = 0 if first == "hit" else first_not_above()
    if start is None:
        return None
    end_at = count - 1 if last == "hit" else last_not_below()
    if end_at is None:
        return None
    if end_at < start:
        return (0, 0)
    # The edge itself has to hit. A neighbour that is only "below" is not
    # a visible character, and this does not stretch the span to include it.
    if place(start) != "hit" or place(end_at) != "hit":
        return (0, 0)
    return (start, end_at + 1)


def _buffer_text(acc) -> str | None:
    got = _safe(lambda: _atspi().Text.get_text(acc, 0, -1))
    if not isinstance(got, str):
        return None
    return got


def buffer_match_visibility(acc, needle: str) -> str | None:
    """Where ``needle`` sits in a GTK multi-line document.

    None when ``acc`` is not such a document, the needle is in the
    accessible name, or character extents cannot be read. ``visible`` when
    a character of a match intersects the widget. ``hidden`` when every
    match that was placed misses the widget. ``absent`` when the buffer
    does not contain the needle. No rectangle is invented for a match.
    """
    text_needle = needle.strip() if isinstance(needle, str) else ""
    if not text_needle or not _gtk_multiline_document(acc):
        return None
    name = str(_call_first(acc, ("get_name",), default="") or "")
    if text_needle.lower() in name.replace("\u00a0", " ").lower():
        return None
    full = _buffer_text(acc)
    if full is None:
        return None
    offsets = _buffer_offsets(full, text_needle)
    if offsets is None:
        return None
    if not offsets:
        return "absent"
    box = _raw_rect(acc, "SCREEN")
    if box is None or box[2] <= 0 or box[3] <= 0:
        return None
    count = len(full)
    span = _visible_text_span(acc, count, box)
    if span is None:
        return None
    start, end = span
    length = len(text_needle.replace("\u00a0", " "))
    for at in offsets:
        if at < end and at + length > start:
            return "visible"
    return "hidden"


def _scroll_buffer_offset(acc, start: int, end: int, *, move_caret: bool = False) -> bool:
    """Ask the text interface to bring ``[start, end)`` into the widget.

    ``scroll_substring_to`` is the AT-SPI scroll. ``set_caret_offset`` is
    the Scintilla fallback, enabled only for explicit scroll-search. Neither
    call is success by itself. The caller reads character extents again.
    """
    Atspi = _atspi()
    scroll_type = getattr(getattr(Atspi, "ScrollType", None), "ANYWHERE", None)
    scrolled = False
    if scroll_type is not None:
        scrolled = bool(_safe(
            lambda: Atspi.Text.scroll_substring_to(acc, int(start), int(end), scroll_type)
        ))
    if move_caret:
        scrolled = bool(_safe(lambda: Atspi.Text.set_caret_offset(acc, int(start)))) or scrolled
    return scrolled


def reveal_buffer_match(acc, needle: str, *, move_caret: bool = False) -> str | None:
    """Bring a hidden document match into the widget.

    None when this node is not judged. ``visible`` when a match already
    intersects the widget, and the caret is not moved. ``scrolled`` when
    ``scroll_substring_to`` or ``set_caret_offset`` leaves a character of
    the match inside the widget. ``hidden`` when the match is in the buffer
    and still misses the widget afterwards. ``absent`` when it is not in
    the buffer. Only explicit scroll-search may enable ``move_caret``.
    Extents after the call are the only proof. No box is invented.
    """
    kind = buffer_match_visibility(acc, needle)
    if kind != "hidden":
        return kind
    text_needle = needle.strip()
    full = _buffer_text(acc)
    if full is None:
        return None
    offsets = _buffer_offsets(full, text_needle)
    if not offsets:
        return "absent"
    length = len(text_needle.replace("\u00a0", " "))
    start = offsets[0]
    if not _scroll_buffer_offset(acc, start, start + length, move_caret=move_caret):
        return buffer_match_visibility(acc, needle)
    # Gtk.TextView applies scroll_substring_to on a later iteration of the
    # toolkit loop. The extents read in the same call are still the old
    # ones. A later read is the proof. Scintilla's caret scroll is already
    # visible on the first read, so this wait does not run.
    deadline = time.monotonic() + 1.5
    after = buffer_match_visibility(acc, needle)
    while after == "hidden" and time.monotonic() < deadline:
        time.sleep(0.2)
        after = buffer_match_visibility(acc, needle)
    if after == "visible":
        return "scrolled"
    if after == "hidden":
        return "hidden"
    return after


def _gtk4_visible_buffer(acc, text: str) -> str | None:
    """Read complete lines verified inside the widget in WINDOW coordinates.

    GTK 4 can reject SCREEN ranges and point-to-offset queries. Missing
    geometry must not make the whole buffer searchable as visible text.
    """
    Atspi = _atspi()
    coord = getattr(getattr(Atspi, "CoordType", None), "WINDOW", None)
    box = _raw_rect(acc, "WINDOW")
    if coord is None or box is None or box[2] <= 0 or box[3] <= 0:
        return None
    x, y, width, height = box
    visible = []
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if content:
            rect = _safe(lambda: Atspi.Text.get_range_extents(
                acc, offset, offset + len(content), coord,
            ))
            rectangles = [rect]
            if rect is None:
                # Older GTK 4 bridges omit GetRangeExtents but implement
                # GetCharacterExtents. Verify every character independently;
                # endpoint boxes cannot establish visibility of wrapped text.
                rectangles = [
                    _safe(lambda index=index: Atspi.Text.get_character_extents(
                        acc, index, coord,
                    ))
                    for index in range(offset, offset + len(content))
                ]
                if any(item is None for item in rectangles):
                    return None
            if all(item.width > 0 and item.height > 0
                   and x <= item.x and y <= item.y
                   and item.x + item.width <= x + width
                   and item.y + item.height <= y + height
                   for item in rectangles):
                visible.append(content)
        offset += len(line)
    # Preserve separation even when intervening lines are off screen.
    return "\n".join(visible)


def _value_text(acc, role: str, role_name: str | None = None) -> object | None:
    """The node's current value: text contents for text roles, numeric value
    for sliders/progress. Secure fields never have their value read here (the
    engine also blanks AXSecureTextField values).

    Text is read via the explicit interface class form ``Atspi.Text.get_text(acc,
    0, -1)``. The instance form (``acc.get_text_iface().get_text(a, b)``) resolves
    to ``Atspi.Accessible.get_text`` (a 1-arg method) on this binding and raises
    TypeError — which, swallowed defensively, silently blanked every field value.
    Calling the interface method with the accessible as the first argument avoids
    the name collision. Non-Text accessibles make the call raise → None.

    Chromium's select and listbox text is U+FFFC once per option. The value is
    the selected option's name instead. Anywhere else, each U+FFFC is the
    embedded child's text (a link, an emphasis, a control), not a character to
    delete: a paragraph then reads as the sentence, and a paragraph whose only
    content is a control is not an empty text leaf. An empty number field
    exposes Value 0.0 with no text; that default is not shown. A slider has no
    text interface and still reports its Value. A custom ARIA spin button's
    text interface is empty too; its value is ``valuetext`` when Chrome set
    one, otherwise ``aria-valuenow``, with the minimum and maximum when the
    Value interface has a real range. An ``<input type=number>`` and a date
    segment are not that control.

    Qt publishes a Value interface on labels, checks, rows, and empty text
    fields. The number is 0.0 or an uninitialized double (about ``1e-310``).
    Text, or the accessible name, is what those roles show. Value is read for
    a slider, spin button, or progress bar. Qt requires a real range, except
    that sliders/spins reporting 0..0 have unknown bounds. GTK's fallback
    for a non-range control requires
    a Value interface. A dialog or a checkbox whose CurrentValue is a
    subnormal double: that reading is absent. Zero and a small normalized
    value on those roles stay. An empty entry, text field, or paragraph
    (character count 0) has no value either: Chromium's empty input
    publishes a subnormal double on Value, and that number is not the
    field. A filler still keeps that double. An empty GTK text field
    implements Text and not Value. CurrentValue is then an uninitialized
    double (about ``6.93e-310``). That read is skipped when the Value
    interface is absent. When the text interface does not report a
    character count, a subnormal on a text role is absent too, and zero
    and a small normalized value stay when the interface is present.
    """
    if role == "AXSecureTextField" or is_secure(acc):
        return None
    Atspi = _atspi()
    if role_name is None:
        role_name = _role_name(acc)
    if role_name == "spin button" and _aria_spinbutton(acc):
        return _aria_spin_value(acc)
    count = _safe(lambda: Atspi.Text.get_character_count(acc))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        got = _read_full_text(acc, count=count)
        if isinstance(got, str) and got:
            if (_gtk4_app(acc) and role_name in {"text", "editable text", "entry"}
                    and (_reports_multiline(acc, _get_attributes(acc)) or "\n" in got)):
                return _gtk4_visible_buffer(acc, got)
            if _OBJECT_REPLACEMENT in got:
                if _is_choice_role(role, role_name or ""):
                    cleaned = _strip_objects(got)
                    if cleaned:
                        return cleaned
                else:
                    expanded = _expand_embedded(acc, got)
                    if expanded:
                        return expanded
            else:
                # A terminal keeps the whole buffer here. ``read`` records
                # the on-screen slice separately so the snapshot can show
                # that text while find still searches this string.
                return got
        handled, choice = _choice_value(acc, role, role_name)
        if handled:
            return choice
        if role_name == "spin button" or (role_name in {"entry", "text"} and _number_input(acc)):
            return None
    elif count == 0 and (
        role_name == "spin button" or (role_name in {"entry", "text"} and _number_input(acc))
    ):
        # Text is present and empty. Value 0.0 is the number field's default,
        # not a number the user entered. A slider has no text interface
        # (count is None) and still falls through to Value.
        # A Chrome date, time, or month segment also has empty text. Its
        # Value interface is not that segment: an empty one stays 0.0, and a
        # filled date can publish one float across the segments. The segment
        # is the valuetext attribute ("17", "1994", "March").
        if role_name == "spin button" and _chrome_date_segment(acc):
            return _date_segment_text(acc)
        return None
    elif count == 0 and role_name in _EMPTY_TEXT_VALUE_NAMES:
        # Text is present and empty. Chromium's Value on that entry is a
        # subnormal double (about 4e-323), which is not the field.
        return None
    handled, choice = _choice_value(acc, role, role_name)
    if handled:
        return choice
    if role_name == "table cell":
        # An empty Calc cell's Value interface is a full double range whose
        # current value is 0.0. That is not the cell's text. A formula is
        # shown only when the text interface is empty; LibreOffice stores
        # the formula without a leading "=".
        formula = _sheet_formula_text(acc)
        if formula:
            return formula
        if _SHEET_ADDRESS.match(_node_name(acc)):
            return None
    if role_name in _RANGE_ROLE_NAMES:
        cur = _range_value(acc)
        if role_name == "level bar" and cur is not None:
            span = _value_range(acc)
            if span is not None and _sane_range(*span):
                low, high = span
                return f"{cur:.17g} (min {low:.17g}, max {high:.17g})"
        return cur
    if role_name == "document text":
        # The document node's own text is empty. Paragraphs hold the lines.
        # The Value interface on that node is an uninitialized double
        # (about 4e-323), which is not the document.
        document = writer_document_text(acc)
        if document:
            return document
        if document == "":
            return None
    # A Qt label, check, row, or empty line edit has a Value interface whose
    # current value is not a number the widget holds. The name is the title.
    if _qt_app(acc):
        return None
    # GTK 4 text and popover menu items have no Value. Reading CurrentValue
    # without that interface can return an uninitialized double.
    # ``_value_interface_present`` is false only when the accessible reports
    # the interface missing. A test double with no getter still reads Value.
    if not _value_interface_present(acc):
        return None
    cur = _safe(lambda: Atspi.Value.get_current_value(acc))
    if cur is None:
        return None
    if (
        role in _UNINITIALIZED_VALUE_ROLES
        or role_name in _UNINITIALIZED_VALUE_ROLE_NAMES
        or role in _SUBNORMAL_TEXT_ROLES
        or role_name in _SUBNORMAL_TEXT_ROLE_NAMES
    ) and _uninitialized_double(cur):
        return None
    return cur


def _qt_unknown_value_range(acc, span) -> bool:
    """Qt Quick sliders/spins can expose Value but fail both bound getters."""
    return (
        span == (0.0, 0.0)
        and _role_name(acc) in {"slider", "spin button"}
        and _qt_app(acc)
        and _value_interface_present(acc)
    )


def _range_value(acc):
    """Current Value for a slider, spin button, progress bar, or level bar.

    Qt requires a real range, except that sliders/spins reporting 0..0
    have unknown bounds. Denormal or other invalid bounds are not shown.
    Other toolkits keep
    the current value whenever the interface returns a finite number.
    """
    if not _value_interface_present(acc):
        return None
    if _qt_app(acc):
        span = _value_range(acc)
        if span is None or not (_sane_range(*span) or _qt_unknown_value_range(acc, span)):
            return None
    cur = _safe(lambda: _atspi().Value.get_current_value(acc))
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    number = float(cur)
    if not math.isfinite(number):
        return None
    if _qt_app(acc) and number != 0.0 and abs(number) < 1e-200:
        return None
    return cur


def _has_default_state(acc) -> bool:
    """Read explicit native IS_DEFAULT state without inferring a submit action."""
    states = _call_first(acc, ("get_state_set",))
    try:
        state_type = getattr(_atspi(), "StateType", None)
    except ImportError:
        state_type = None
    member = getattr(state_type, "IS_DEFAULT", None)
    if member is not None:
        return bool(_safe(lambda: states.contains(member), False))
    names = getattr(states, "names", ())
    return isinstance(names, (set, frozenset, list, tuple)) and "IS_DEFAULT" in {
        str(name).upper().replace("-", "_") for name in names
    }


def _state_flags(acc):
    """(enabled, focused, checked, selected, expanded, focusable, editable).

    checked/expanded are None when the element is not checkable/expandable so
    the shared schema can tell "off" apart from "not a checkbox". ``editable``
    is ``STATE_EDITABLE``. A fake state set that lists names and has no
    ``StateType`` member matches the name string."""
    Atspi = _atspi()
    _drop_cached_properties(acc)
    sset = _call_first(acc, ("get_state_set",))
    if sset is None:
        return True, False, None, False, None, False, False
    st = getattr(Atspi, "StateType", None)

    def has(name: str) -> bool:
        member = getattr(st, name, None)
        if member is not None:
            return bool(_safe(lambda: sset.contains(member), False))
        names = getattr(sset, "names", None)
        if isinstance(names, (set, frozenset, list, tuple)):
            folded = {str(item).upper() for item in names}
            return name.upper() in folded
        return False

    enabled = has("ENABLED") or has("SENSITIVE")
    focused = has("FOCUSED")
    checked = has("CHECKED") if (has("CHECKABLE") or has("CHECKED")) else None
    # aria-pressed is AT-SPI PRESSED, not CHECKED. A GTK toggle already uses
    # CHECKED. A pressed Chrome button is shown the same way.
    if checked is None and has("PRESSED"):
        checked = True
    selected = has("SELECTED")
    if (not selected and focused and _role_name(acc) == "page tab"
            and "qt" in _toolkit_name(acc)):
        # Qt 6.0–6.6 QAccessibleTabButton::state encodes currentIndex in
        # FOCUSED, independent of keyboard focus, and omits SELECTED.
        # Qt 6.7 adds SELECTED. Do not infer selection from ordinary focus
        # for other toolkits or versions with different tab semantics.
        version = _toolkit_version(acc).split(".")
        if (len(version) >= 2 and version[0] == "6"
                and version[1].isdigit() and int(version[1]) < 7
                and _role_name(_parent_of(acc)) == "page tab list"):
            selected = True
    expanded = has("EXPANDED") if has("EXPANDABLE") else None
    focusable = has("FOCUSABLE")
    editable = has("EDITABLE")
    return enabled, focused, checked, selected, expanded, focusable, editable


# ARIA role (AT-SPI 'xml-roles' attribute, set by Chromium/GTK for web content)
# -> canonical AX role. Used to sharpen a generic AT-SPI role (a role="button"
# <div> is a 'section'/'panel' to AT-SPI but an AXButton to us). Standard HTML
# already gets a proper AT-SPI role, so this only refines generic containers.
_XML_ROLE = {
    "button": "AXButton", "link": "AXLink", "textbox": "AXTextField",
    "searchbox": "AXSearchField", "checkbox": "AXCheckBox", "radio": "AXRadioButton",
    "tab": "AXButton", "menuitem": "AXMenuItem", "menuitemcheckbox": "AXMenuItem",
    "menuitemradio": "AXMenuItem", "combobox": "AXComboBox", "switch": "AXCheckBox",
    "slider": "AXSlider", "option": "AXRow", "listbox": "AXList", "tablist": "AXTabGroup",
}


def _get_attributes(acc) -> dict:
    """The AT-SPI object attributes as a plain dict (xml-roles, id, tag, class,
    …), fetched ONCE per node so stable-id + web-role both reuse it — one D-Bus
    round-trip instead of two (AT-SPI has no batch read). {} on any failure."""
    a = _call_first(acc, ("get_attributes",))
    if a is None:
        return {}
    if isinstance(a, dict):
        return a
    if hasattr(a, "keys") and hasattr(a, "get"):  # GLib.HashTable-ish
        try:
            return {str(k): (a.get(k) and str(a.get(k))) for k in a.keys()}
        except Exception:
            return {}
    return {}


def _opaque_surface_role(role: str, role_str: str, attrs: dict) -> str:
    """``AXCanvas`` for a canvas, a drawing area, or a WebGL/graphics node.

    Chromium sometimes reports ``<canvas>`` as a section. The tag and the
    graphics ARIA roles are the same surface. An image stays ``AXImage``;
    the pruner turns an unnamed one into ``opaque_region``.
    """
    tag = str(attrs.get("tag") or attrs.get("html-tag") or "").strip().lower()
    xml = {
        part.strip().lower()
        for part in str(attrs.get("xml-roles") or "").split()
        if part.strip()
    }
    if (
        role_str in {"canvas", "drawing area"}
        or tag == "canvas"
        or xml & {"graphics-document", "graphics-symbol"}
    ):
        return "AXCanvas"
    return role


def _heading_level(attrs: dict) -> int | None:
    """A positive heading level reported by AT-SPI, without inferring one."""
    value = attrs.get("level")
    try:
        level = int(str(value))
    except (TypeError, ValueError):
        return None
    return level if level > 0 else None


def _refine_web_role(role: str, attrs: dict) -> str:
    """Upgrade a generic container role to the ARIA role its xml-roles declares,
    so ARIA-widget <div>s become real interactive elements in the snapshot."""
    if role == "AXGroup" and attrs:
        for token in (attrs.get("xml-roles") or "").split():
            if token in _XML_ROLE:
                return _XML_ROLE[token]
    return role


def _entry_is_multiline(node, attrs: dict) -> bool:
    """True when an AT-SPI ``entry`` is a multi-line ``<textarea>``.

    Chromium exposes ``<input>`` and ``<textarea>`` as role ``entry``.
    The textarea carries ``STATE_MULTI_LINE``, or the ``multiline``
    attribute. A contenteditable ``role=textbox`` with ``aria-multiline``
    is also an entry and can carry ``STATE_MULTI_LINE``. That editor stays
    a text field: its tag is not ``textarea``.
    """
    tag = str(attrs.get("tag") or attrs.get("html-tag") or "").strip().lower()
    if tag and tag != "textarea":
        return False
    roles = {
        part.strip().lower()
        for part in str(attrs.get("xml-roles") or "").split()
        if part.strip()
    }
    if "textbox" in roles and tag != "textarea":
        return False
    if _state_has(node, "MULTI_LINE"):
        return True
    flag = attrs.get("multiline")
    if isinstance(flag, bool):
        return flag
    return str(flag or "").strip().lower() in {"true", "1", "yes"}


def _stable_id(acc, attrs: dict | None = None) -> str | None:
    """A layout-independent id for ``acc``: the AT-SPI ``accessible-id``, else a
    web element's DOM id from the object attributes (Chromium exposes 'id').
    None when the app assigns none."""
    sid = _call_first(acc, ("get_accessible_id",))
    if sid:
        return str(sid)
    attrs = attrs if attrs is not None else _get_attributes(acc)
    for key in ("id", "html-id", "xml-id"):
        val = attrs.get(key)
        if val:
            return str(val)
    return None


class _BandRect:
    """Screen box for a snapshot-only row group. Not an AT-SPI rect."""

    def __init__(self, x: float, y: float, width: float, height: float) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _OnScreenBand:
    """Group of on-screen rows the snapshot walk can keep. Not an AT-SPI object.

    The shared pruner keeps at most ``MAX_CHILDREN`` children of one node and
    drops the rest. A Chromium list can paint more rows than that (29 rows of
    18px in a 520px box). A band stays within the cap, so find still matches
    the last painted row. A shorter run is not wrapped. A Calc band uses the
    table role so the sheet cap, not the dense cap, applies to its cells.
    """

    def __init__(self, rows: list[tuple], *, role: str = "panel", name: str = "") -> None:
        self._rows = rows
        self._role = role
        self._name = name
        left = min(float(pos[0]) for _acc, pos, _size in rows)
        top = min(float(pos[1]) for _acc, pos, _size in rows)
        right = max(float(pos[0]) + float(size[0]) for _acc, pos, size in rows)
        bottom = max(float(pos[1]) + float(size[1]) for _acc, pos, size in rows)
        self._rect = _BandRect(left, top, max(1.0, right - left), max(1.0, bottom - top))

    def get_role_name(self) -> str:
        return self._role

    def get_name(self) -> str:
        return self._name

    def get_description(self) -> str:
        return ""

    def get_child_count(self) -> int:
        return len(self._rows)

    def get_child_at_index(self, index: int):
        return self._rows[int(index)][0]

    def get_component_iface(self):
        return self

    def get_extents(self, _coord):
        return self._rect


def _bands_for_snapshot(rows: list[tuple]) -> list:
    """Row accessibles, grouped when they would exceed the snapshot child cap."""
    if len(rows) <= MAX_CHILDREN:
        return [acc for acc, _pos, _size in rows]
    chunks = [
        rows[start:start + MAX_CHILDREN]
        for start in range(0, len(rows), MAX_CHILDREN)
    ]
    # A singleton wrapper collapses in the shared pruner, leaving its cell
    # beside the other bands. Keep the tail band structural without changing
    # cell order, native handles, geometry, or the child cap.
    if len(chunks[-1]) == 1:
        chunks[-1].insert(0, chunks[-2].pop())
    return [_OnScreenBand(chunk) for chunk in chunks]


def _cached_rows_for_snapshot(bounds: dict, accs: list) -> list:
    """The saved row list, grouped the same way as a live walk.

    A saved row with no box is returned flat, which is the previous list.
    """
    rows = []
    for acc in accs:
        found = bounds.get(id(acc))
        if found is None:
            return list(accs)
        pos, size = found
        rows.append((acc, pos, size))
    return _bands_for_snapshot(rows)


def _list_item_subtrees(container, rows: list[tuple]) -> list[tuple]:
    """Retain native list-item subtrees around sampled labels.

    A CSS list item is not a clipping viewport: a showing dropdown can
    extend outside both the item and the list. Publishing only its sampled
    text discards that subtree. Keep native item bounds and let the ordinary
    snapshot traversal apply each descendant's own visibility checks.
    """
    result = []
    seen = set()
    container_key = _ancestor_key(container)
    for acc, pos, size in rows:
        # Already sampled native rows retain their viewport-clipped bounds.
        if _role_name(acc) == "list item":
            key = _ancestor_key(acc)
            if key not in seen:
                seen.add(key)
                result.append((acc, pos, size))
            continue
        item = None
        for ancestor in _ancestor_chain(acc):
            if _ancestor_key(ancestor) == container_key:
                if item is not None:
                    item_pos, item_size = _extents(item)
                    # Plain marker/label rows keep the existing sampled
                    # text and clipped geometry. Only structural/control
                    # children need their native item restored.
                    has_subtree = any(
                        _role_name(_child_at(item, index)) not in
                        {"static", "text", "list marker"}
                        for index in range(min(_child_count(item), _MAX_CHILDREN_FETCH))
                    )
                    if has_subtree and item_pos is not None and item_size is not None:
                        acc, pos, size = item, item_pos, item_size
                break
            if _role_name(ancestor) == "list item":
                item = ancestor
        key = _ancestor_key(acc)
        if key not in seen:
            seen.add(key)
            result.append((acc, pos, size))
    return result


def chromium_closed_menu_model(node) -> bool:
    """Exclude stale select models only from automatic scroll discovery."""
    if _role_name(node) not in {"menu", "popup menu"} or not _chromium_app(node):
        return False
    if not _state_has(node, "SHOWING"):
        return True
    parent = _parent_of(node)
    return (parent is not None and _role_name(parent) == "combo box"
            and _state_has(parent, "EXPANDABLE")
            and not _state_has(parent, "EXPANDED"))


class ATSPIAccessor:
    """`observe.TreeAccessor` over `Atspi.Accessible` handles.

    For a Chromium list, the snapshot lists every row of the list node it
    is reading whose own top is on or below the list's top and which
    extends below the clipped top edge. A row flush with that top is on
    screen. The walk does not stop after 16 rows: that number is the
    hit-test sample count, and using it as the row set omitted painted
    rows with no elision marker. When more rows are on screen than the
    shared child cap, they are grouped so the cap does not drop the tail.
    A saved head from a different Python wrapper is not that list: a live
    walk wraps a new object each time, and the 0.4.16 retest still showed
    the pre-scroll row at y=-2. ``scroll_to_find`` searches this snapshot.
    A GTK table whose rows do not fit the child cap lists the cells that
    overlap the screen, grouped so the cap keeps that whole run. ``find``
    scrolls a named Table cell into the run when the name is not already
    on screen. A cell the table still exposes that stays off screen is
    remembered here and attached with no bounds. A short table and a Qt
    grid keep the walks they already had.
    A Calc sheet lists the cells inside its on-screen box, banded so the
    child cap does not stop at the first six rows.
    """

    def __init__(self) -> None:
        self._active_modals: list = []
        self._modal_ancestry: dict = {}
        self._visible_children: dict[int, list] = {}
        self._defer_row_identity = False
        self._visible_bounds: dict[int, tuple] = {}
        self._gecko: bool | None = None
        # Whether this walk's application is Chromium. One walk reads one
        # application, so this is asked once instead of for every child.
        self._row_identity_cache: dict = {}
        self._chromium: bool | None = None
        # id(node) -> (node, its frame or None) for LibreOffice placement.
        self._frames: dict[int, tuple[object, object]] = {}
        self._frame_screens: dict[int, tuple[object, tuple]] = {}
        # Set by LinuxDriver.snapshot from a ``find`` text. None on a plain
        # snapshot, so listing the window does not scroll the tree.
        self._table_seek: str | None = None
        # ``(cell, table)`` pairs ``find`` matched that are still off screen.
        # Bounds are not invented for them; the snapshot attaches them as-is.
        self._offscreen_seek: list[tuple[object, object]] = []
        # ``(cache key, start index, page size)`` set for one snapshot so
        # expand lists that node's next children instead of the visible band.
        self._expand_page: tuple | None = None
        # Children a list or table did not publish. ``take_unlisted`` moves
        # the count onto the prune elision for ``… N more``.
        self._unlisted: dict[object, int] = {}
        # Expand-page nodes the pruner must keep even when their box is
        # below the viewport. Empty on a normal snapshot.
        self._page_keep: set[object] = set()
        # Set by the Linux snapshot when the tree is LibreOffice. Content
        # bounds then pick SCREEN or the client origin plus WINDOW, per node.
        self.libreoffice = False
        self._lo_placements: dict[
            tuple[int, int, int, int],
            tuple[tuple[int, int] | None, bool],
        ] = {}
        # One process identity for the whole walk. The root is read first.
        self._instance_id: str | None = None
        self._instance_ready = False
        # Whether this tree is Chromium or Firefox. None until the first read.
        # A childless browser-frame view is not a page region.
        self._browser_app: bool | None = None
        self._browser_chrome_cache: dict[object, bool] = {}
        # GTK 4 client origins for this walk, keyed by pid and frame size.
        # None means the origin could not be read and must not be invented.
        self._gtk4_origins: dict[tuple[int, int, int], tuple[int, int] | None] = {}

    def _bound_instance_id(self, node: object) -> str | None:
        if not self._instance_ready:
            self._instance_id = app_instance_id(node)
            self._instance_ready = True
        return self._instance_id

    def refresh_visible(self, root: object) -> None:
        """Point Chromium lists at the rows inside their boxes.

        No-op unless the tree's toolkit is Chromium. The cached child list is
        left untouched on the accessible. Rows are taken from the list node
        this walk is reading. A saved head stored on a different wrapper is
        not reused ahead of those rows.
        """
        self._visible_children.clear()
        self._visible_bounds.clear()
        if root is None or not _chromium_app(root):
            return
        for container in _list_containers(root):
            rows = _shown_or_probe(container)
            if len(_row_names(rows)) < 2:
                continue
            self._visible_children[id(container)] = [acc for acc, _pos, _size in rows]
            for acc, pos, size in rows:
                self._visible_bounds[id(acc)] = (pos, size)
        # List discovery reads document children before the snapshot walk.
        # Timer-driven toasts can appear during that probe without a local
        # mutator. Native CHILDREN bypass alone does not invalidate our own
        # cached counts/handles; keep roles and interfaces, but re-read structure.
        _drop_child_read_cache()

    def _drawing_paragraph(self, node: object, role_str: str, state_editable: bool) -> bool:
        """True for an editable Impress or Draw paragraph in this snapshot.

        Only a LibreOffice snapshot asks. A Firefox paragraph that reports
        ``STATE_EDITABLE`` is not walked. Both the state and EditableText
        are required, and the paragraph has to sit in a slide or a Draw page.
        """
        if not self.libreoffice or role_str not in {"paragraph", "heading"} or not state_editable:
            return False
        try:
            return drawing_text_editable(node, role_str=role_str, state_editable=state_editable)
        except Exception:
            return False

    def read(self, node: object) -> RawNode:
        role_str = _role_name(node)
        role = _ROLE.get(role_str, "AXGroup")
        if role_str == "status bar":
            # GTK can register the bar before allocating it. Refresh before
            # reading geometry: _state_flags refreshes later, after a cached
            # zero-size rectangle would already have hidden the status text.
            _drop_cached_properties(node)
        # GTK3 gives a single-line Gtk.Entry the same "text" role as a
        # Gtk.TextView. The entry carries SINGLE_LINE; the view carries
        # MULTI_LINE. A single-line entry is a text field, not a textarea.
        if role_str == "text" and _state_has(node, "SINGLE_LINE") and not _state_has(node, "MULTI_LINE"):
            role = "AXTextField"
        attrs = _get_attributes(node)  # one D-Bus fetch, reused for role + id
        if is_modal(node, attrs=attrs):
            self._active_modals.append(node)
        # Chromium maps <input> and <textarea> both to entry. MULTI_LINE,
        # or a multiline attribute, is the textarea.
        if role_str == "entry" and _entry_is_multiline(node, attrs):
            role = "AXTextArea"
        role = _refine_web_role(role, attrs)
        role = _opaque_surface_role(role, role_str, attrs)
        # Chromium reports HTML top-layer popovers as ordinary panels.
        if (role_str == "panel" and attrs.get("ispopup") in {"auto", "manual", "hint"}
                and _state_has(node, "SHOWING")):
            role = "AXPopover"
        # A custom role=spinbutton is an AT-SPI spin button, which this map
        # sends to AXTextField. That renders as textfield (edit) and hides
        # the number. AXSpinButton is the slider treatment: the role is the
        # control, and it is not an editable text field. A native number
        # input and a date segment stay on the text-field mapping. Qt spins
        # use Value as well, including Qt Quick spins with no Text interface.
        if _aria_spinbutton(node, role_str, attrs) or (role_str == "spin button" and _qt_app(node)):
            role = "AXSpinButton"
        # `_entry_is_multiline` already mapped a tagged <textarea>. A Gecko
        # entry that check left as a text field is still a textarea when it
        # carries MULTI_LINE, multiline, or aria-multiline. A single-line
        # input stays a text field. Chromium entries are not matched here.
        if role == "AXTextField" and role_str == "entry" and _gecko_multiline_entry(node, attrs):
            role = "AXTextArea"
        if is_secure(node):
            role = "AXSecureTextField"
        override = self._visible_bounds.get(id(node))
        if override is not None:
            position, size = override
        else:
            # Keep a 0-height section's size. Hit-testing still treats it as
            # no box; only the snapshot walk needs the zero extent.
            position, size = _extents(node, keep_zero=True)
            if self.libreoffice:
                shifted = self._libreoffice_position(node, position, size)
                if shifted is not None:
                    position = shifted
            else:
                recovered = gtk4_recovered_box(node, position, size, self._gtk4_origins)
                if recovered is not None:
                    position, size = recovered
        enabled, focused, checked, selected, expanded, focusable, state_editable = _state_flags(node)
        if role_str == "viewport" and _gtk_major(node) == 3:
            # GTK 3 applies the scroll offset to the viewport's own
            # visibility check. Its children can still be SHOWING with
            # correct screen coordinates while it sits at G_MININT.
            # Use only the enclosing scroll pane's measured box; child
            # geometry remains untouched and is pruned independently.
            if not _state_has(node, "SHOWING") or (
                position is not None and -2147483648.0 in position
            ):
                parent = _parent_of(node)
                if (_role_name(parent) == "scroll pane"
                        and _state_has(parent, "SHOWING")):
                    parent_pos, parent_size = _extents(parent)
                    if (parent_pos is not None and parent_size is not None
                            and all(math.isfinite(v) for v in (*parent_pos, *parent_size))
                            and min(parent_size) > 0
                            and -2147483648.0 not in parent_pos):
                        position, size = parent_pos, parent_size
        # A GTK combo does not set EXPANDABLE, so the shared flag stays unset
        # while its menu is on screen. The open popup is that menu becoming
        # SHOWING. expanded follows the menu, including after it closes.
        if role_str == "combo box" and _gtk_combo(node):
            expanded = bool(_popup_open(node))
        name = _plain_accessible_name(str(_call_first(node, ("get_name",), default="") or ""))
        # The snapshot uses the description as its title when the name is empty.
        description = _plain_accessible_name(
            str(_call_first(node, ("get_description",), default="") or "")
        )
        # Chromium publishes a heading around a button as its line-wide
        # label, with no separate static-text child. Keep that existing label
        # ref so an explicit label click still takes the verified control path.
        # Ordinary headings (including Firefox/WebKitGTK) retain AXHeading.
        if role == "AXHeading" and name and _chromium_app(node):
            count = _child_count(node)
            if count == 1:
                child = _child_at(node, 0)
                if (
                    child is not None
                    and _role_name(child) in {"button", "push button"}
                    and _node_name(child) == name
                ):
                    role = "AXStaticText"
        # GTK 4 can expose uninitialized allocations on empty anonymous
        # panels (#503). Do not turn that filler into a clickable opaque
        # region. A side longer than the full X coordinate span (65535)
        # is suspect only here. Keep real containers' extents for clipping
        # and row sampling.
        if (
            role_str == "panel"
            and not str(name).strip()
            and size is not None
            and any(not math.isfinite(v) or v > 65535 for v in size)
            and _gtk_major(node) == 4
            and _child_count(node) == 0
        ):
            position, size = (-1.0, -1.0), (-1.0, -1.0)
        if (
            role in {"AXTextField", "AXSecureTextField"}
            and role_str in {"entry", "text", "password text"}
            and not name.strip()
        ):
            # Explicit labels precede tooltips, including password labels.
            # Relation evidence does not require geometry.
            label = _plain_accessible_name(_labelled_by_name(node))
            name = _strip_label_colon(label)
            if not name and not description.strip() and role == "AXTextField":
                name = _gtk_entry_row_name(node)
        if role_str == "calendar" and not name.strip():
            name = (_plain_accessible_name(_labelled_by_name(node))
                    or description or "Calendar")
        if role_str == "combo box":
            name = _combo_display_name(node, str(name))
        if (role_str in {"slider", "spin button"} and not name.strip()
                and "gtk" in _toolkit_name(node)):
            label = _plain_accessible_name(_labelled_by_name(node))
            name = (_strip_label_colon(label) if label else
                    _gtk_row_label_name(node, str(name)))
            # GtkScale copies its drawn (possibly rounded) number into the
            # description. Shared snapshots fall back to that field as the
            # title, making an unlabeled scale stale after its own write.
            # Keep the native Value as the value, never as an identity label.
            if (role_str == "slider" and not name.strip()
                    and re.fullmatch(r"\s*[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?\s*",
                                     description)):
                description = ""
        if role == "AXMenuItem" and not str(name).strip():
            # GTK 4 Gio.Menu labels live on LABELLED_BY targets, sometimes
            # only as Text rather than an accessible name.
            for target in _relation_targets(node, "labelled_by"):
                label = _node_name(target) or _direct_text(target).strip()
                if label:
                    name = _plain_accessible_name(label)
                    break
        button_name = str(name)
        if role_str == "push button":
            name = _gtk_button_row_name(node, button_name)
        if _OBJECT_REPLACEMENT in str(name):
            name = _strip_objects(str(name))
        # An unnamed icon can still carry a caption on its Text interface.
        # Evince thumbnail icons do: the text is the page number under the
        # image. That string is the name, so the pruner does not fold the
        # thumbnail into an opaque region. A named image is left alone.
        if (
            role == "AXImage"
            and not str(name).strip()
            and _generic_region_name(str(description))
        ):
            label = _image_text_label(node)
            if label:
                name = label
        # GtkStatusbar can expose its changing label only through Text.
        # Keep the statusbar role and avoid probing its numeric Value interface.
        status_text = None
        if role_str == "status bar":
            try:
                Atspi = _atspi()
                # An empty or unavailable count is not a status-message read.
                # Ask Text for the actual message before suppressing it; never
                # derive a caret message from the document or selected symbol.
                count = _safe(lambda: Atspi.Text.get_character_count(node))
                end = min(count, _IMAGE_LABEL_CHARS) if (
                    isinstance(count, int) and not isinstance(count, bool) and count > 0
                ) else -1
                text = _safe(lambda: Atspi.Text.get_text(node, 0, end))
                if isinstance(text, str) and text.strip():
                    text = text.strip()[:_IMAGE_LABEL_CHARS]
                    if not str(name).strip():
                        name = text
                    elif text != name:
                        status_text = text
            except ImportError:
                pass
        unallocated = False
        if role_str == "page tab" and _negative_extent(size):
            # A notebook with one page hides the tab bar. GTK reports no
            # allocation and, for a terminal, an empty name. Only a main
            # terminal page may borrow the frame title; unrelated pages
            # retain their own label or remain unnamed. Inventing a box
            # would put a click on the menu or page, so it stays hidden with
            # no bounds and no press action.
            label = str(name).strip()
            if not label and not str(description).strip():
                label = _claws_wizard_page_title(node)
                if label:
                    role = "AXTab"
            if not label and not str(description).strip():
                label = _terminal_page_frame_title(node)
            name = label
            position, size = None, None
            unallocated = True
        if checked is None and role == "AXButton":
            flag = str(attrs.get("aria-pressed") or attrs.get("pressed") or "").strip().lower()
            if flag in {"true", "false"}:
                checked = flag == "true"
        # A Gecko contenteditable is a section with STATE_EDITABLE and a text
        # interface. That is a text control: textarea when it is multi-line.
        # Chromium's roleless contenteditable stays a group.
        role = _gecko_contenteditable_role(node, role, role_str, attrs, state_editable)
        # Groups skip the value probe. Text-bearing HTML sections need their
        # own Text string so find can match words absent from child nodes.
        # A file chooser is a dialog, but its name is the title. Probing Value
        # used to be skipped when the role was a window.
        # A PDF page is the exception: its Text interface is the page, and
        # the name is empty. The read is bounded. A blank page stays a group
        # so a childless on-screen page is still a region, not a dropped label.
        if role_str == "page":
            page_text = _page_text(node)
            if page_text:
                value = page_text
                if _evince_page(node) and _state_has(node, "SHOWING"):
                    # Evince's page boxes drift after scrolling. Keep the
                    # visible text without offering those boxes for targeting.
                    position, size = None, None
                    unallocated = True
            else:
                role = "AXGroup"
                value = None
        else:
            value = None if role in _NO_VALUE_ROLES or role_str == "file chooser" else _value_text(
                node, role, role_str,
            )
        if status_text is not None:
            value = status_text
        if (
            role_str == "section"
            and role in {"AXTextField", "AXTextArea"}
            and not isinstance(value, str)
        ):
            # An empty contenteditable can publish a Value double. The text
            # interface is the value. A number is not that text.
            value = _section_text(node, role_str, attrs)
        if value is None:
            sentence = _section_text(node, role_str, attrs)
            if sentence:
                value = sentence
        search_value = None
        if (value is None and role == "AXTextArea"
                and role_str in {"text", "editable text", "entry"}
                and _gtk4_app(node) and not is_secure(node)):
            # GTK 4.14 implements neither text extent API for AccessibleText.
            # Preserve native buffer read-back, without indexing it as visible.
            buffer = _read_full_text(node)
            if isinstance(buffer, str) and buffer and (
                _reports_multiline(node, attrs) or "\n" in buffer
            ):
                value = buffer
                search_value = ""
        owner_name = ""
        if role == "AXTextArea":
            owner = _application_of(node)
            owner_name = str(_call_first(owner, ("get_name",), default="") or "")
        qt_document = (
            role == "AXTextArea" and role_str != "terminal"
            and _qt_app(node) and _reports_multiline(node, attrs)
        )
        if qt_document and owner_name.casefold() != "kate":
            # Kate uses character geometry below. Probing bounded ranges
            # disrupts its bridge, hiding later controls in this walk.
            value = _qt_viewport_text(node)
        if role_str == "push button" and name != button_name and button_name.strip() and value is None:
            value = button_name
        screen_text = None
        if role == "AXTextArea":
            if owner_name.casefold() == "kate":
                # KTextEditor publishes the whole buffer, including off-screen
                # characters. Its widget box cannot verify substring visibility,
                # even for a document containing one long line. Keep the ref.
                value = _qt_character_viewport_text(node) if qt_document else None
                if value is None:
                    description = (
                        f"{description} Document text visibility unavailable; "
                        "use in-document navigation to locate text."
                    ).strip()
        if role_str == "terminal" and isinstance(value, str) and value:
            screen_text = _terminal_on_screen(node, value)
        name, description = _split_modifier_hints(str(name), str(description))
        name, description, hint, shortcut = _split_tooltip_fields(str(name), str(description))
        if qt_document and owner_name.casefold() == "kate" and value is not None:
            hint = (hint + "; " if hint else "") + "Native character viewport text"
        if qt_document and value is None:
            hint = (hint + "; " if hint else "") + (
                "Visible document text unavailable; use in-document Find or line navigation"
            )
        embedded_properties = (
            role_str == "table cell" and not str(name).strip()
            and not str(description).strip() and value is None
            and size is not None and size[0] > 0 and size[1] > 0
            and _scribus_embedded_properties(node)
        )
        if embedded_properties:
            hint = (hint + "; " if hint else "") + (
                "Embedded widgets are not exposed to accessibility; "
                "inspect a screenshot and use coordinate clicks or keyboard input. "
                "This ref identifies the whole cell, not an individual control."
            )
        surface_name = ""
        content_note = (
            "Buffer contents; text visibility unavailable, excluded from text search."
            if search_value is not None else ""
        )
        if embedded_properties:
            content_note = hint
        panel_note = ""
        if (
            role_str == "panel" and role == "AXGroup"
            and not str(name).strip() and not str(description).strip()
            and value is None and _child_count(node) == 0
            and _application_name(node).casefold() == "pcmanfm"
            and _role_name(_parent_of(node)) == "scroll pane"
            and _node_name(_parent_of(node)).casefold() == "view of folder contents"
            and not _has_text_interface(node)
        ):
            # A painted file surface, even when its box duplicates the scroll pane.
            role = "AXCanvas"
            surface_name = "PCManFM folder contents"
            panel_note = (
                "File and folder icons are not exposed by AT-SPI; find and "
                "scroll_to_find cannot read or target them. This ref identifies "
                "the whole pane. Explicitly switch to Detailed List View for "
                "accessible named rows."
            )

        if (
            role_str == "panel" and role == "AXGroup"
            and not str(name).strip() and not str(description).strip()
            and value is None and _claws_list_surface(node)
        ):
            surface_name = "Claws Mail folder / message list"
            panel_note = (
                "Folder rows and message subjects are not exposed by AT-SPI; "
                "find and scroll_to_find cannot read or target those rows. "
                "This ref identifies the whole pane; its list type is unavailable."
            )
        if (
            role_str == "drawing area"
            and not str(name).strip() and not str(description).strip()
        ):
            surface_name, panel_note = _gucharmap_character_surface(node)
        if (
            role_str == "filler" and role == "AXGroup"
            and not str(name).strip() and not str(description).strip()
        ):
            surface_name, panel_note = _okular_thumbnail_surface(node)
        if (
            not surface_name
            and role_str in {"filler", "panel"}
            and role == "AXGroup"
            and not str(name).strip()
            and not str(description).strip()
            and value is None
            and _okular_page_surface(node)
            and not _has_text_interface(node)
        ):
            surface_name = "Okular document / page view"
            panel_note = (
                "Page text is not exposed by AT-SPI; find and scroll_to_find cannot read it. "
                "Use Okular's Ctrl+F Find bar explicitly and verify the result in the UI."
            )
        if (
            not surface_name
            and role_str == "filler"
            and not str(name).strip()
            and not str(description).strip()
            and _kate_gutter(node, position, size)
        ):
            surface_name = "Kate editor gutter"
            panel_note = "Line numbers are not exposed by AT-SPI; individual lines cannot be targeted here."
        if (
            role_str == "drawing area"
            and not str(name).strip()
            and not str(description).strip()
            and _child_count(node) == 0
            and _parent_of(node)
        ):
            app = _application_ancestor(node)
            if app is not None and _node_name(app).casefold() == "gnumeric":
                surface_name = "Gnumeric drawing area"
                panel_note = (
                    "Cell content is not exposed by AT-SPI on this surface. "
                    "The formula bar exposes only the active cell; inspect it to read "
                    "that cell. Other cells require visual inspection."
                )
        if role_str == "panel" and role == "AXGroup" and not surface_name:
            panel_role, surface_name, panel_note = _audacity_panel(node, size, focusable)
            if panel_role:
                role = panel_role
        if role_str == "panel" and not str(name).strip() and not str(description).strip():
            surface_name = surface_name or _panel_surface_name(node, size, focusable)
        if (
            role_str == "icon"
            and selected
            and not str(name).strip()
            and not str(description).strip()
        ):
            label = _selected_icon_name(node)
            if label:
                name = label
        if (
            role_str in {"drawing area", "canvas"}
            and _generic_region_name(str(name))
            and _generic_region_name(str(description))
        ):
            canvas_name, content_note, canvas_text = _drawing_canvas_facts(node, size)
            surface_name = canvas_name or surface_name
            if canvas_text and not isinstance(value, str):
                value = canvas_text
        elif (
            role_str == "filler" and not surface_name
            and _generic_region_name(str(name))
            and _generic_region_name(str(description))
            and _child_count(node) == 0 and value is None
        ):
            # Inkscape's empty dock content is also an opaque surface.
            # Use its real document/window label, without claiming that
            # this sibling is the SVG canvas or inventing a content box.
            surface_name, _note, _text = _drawing_canvas_facts(node, size)
        entry = editable_entry(
            node, role_str=role_str, attrs=attrs, state_editable=state_editable,
            drawing=self._drawing_paragraph(node, role_str, state_editable),
        )
        if entry and role_str == "paragraph" and math_command_editable(node):
            role = "AXTextArea"
            if not str(name).strip():
                name, description = "Formula commands", ""
                for ancestor in _ancestor_nodes(_parent_of(node)):
                    if _role_name(ancestor) in {"frame", "dialog", "application"}:
                        break
                    label = _node_name(ancestor).strip()
                    if label and not re.search(r" [—–-] LibreOffice Math$", label):
                        name, description = label, ""
                        break
        if role_str == "push button" and _gtk4_menu_button_wrapper(node):
            # GtkMenuButton's Action belongs to its inner toggle. Make the
            # duplicate outer node a collapsible layout wrapper, preserving
            # the child's native handle and geometry as the click target.
            role, name, description = "AXGroup", "", ""
            focusable, value = False, None
        if role == "AXPopover":
            panel_note = "open" if _state_has(node, "SHOWING") else "not showing"
            openers = _relation_targets(node, "details_for")
            labels = [_plain_accessible_name(_node_name(opener)) for opener in openers]
            labels = [label for label in labels if label]
            if labels:
                panel_note += "; opened by " + ", ".join(repr(label) for label in labels)
        if role_str == "calendar" and _child_count(node) == 0:
            panel_note = ("day cells and selected date are not exposed; ref clicks are refused; "
                          "use keyboard navigation or screenshot-chosen coordinates")
        outline_note = outline_double_click_note(node) if role_str in _ROW_ROLE_NAMES else ""
        if outline_note and position is not None and size is not None:
            position, size = _outline_visible_bounds(node, position, size)
        return RawNode(
            row_identity=(chromium_row_identity(node, cache=self._row_identity_cache)
                          if not self._defer_row_identity and self._tree_is_chromium(node) else ()),
            role=role,
            heading_level=_heading_level(attrs) if role == "AXHeading" else None,
            subrole=(
                "terminal" if role_str == "terminal"
                else "label" if role_str == "label"
                else None
            ),
            title=str(name),
            description=str(description),
            enabled=enabled,
            modal=is_modal(node, attrs=attrs),
            modal_scope_root=role_str in {"document web", "document frame"},
            blocked_by_modal=outside_modals(node, self._active_modals, cache=self._modal_ancestry),
            scroll_candidate=not chromium_closed_menu_model(node),
            focused=focused,
            default=role == "AXButton" and _has_default_state(node),
            focusable=focusable,
            position=position,
            size=size,
            actions=() if unallocated else _action_names(node),
            unallocated=unallocated,
            value=value,
            checked=checked,
            selected=selected,
            expanded=expanded,
            stable_id=_stable_id(node, attrs),
            atspi_web=atspi_web_kind(role_str),
            editable=entry,
            platform_editable=entry,
            instance_id=self._bound_instance_id(node),
            document_id=window_document_id(node, role_str, str(name)),
            context_identity=window_context_identity(node, role_str),
            document_route=_doc_url(node) if role_str in {"document web", "document frame"} else None,
            node_identity=_atspi_wrapper_key(node) or (),
            # Only a group can be suppressed as browser chrome. A button,
            # cell, or canvas does not pay for the parent walk.
            browser_chrome=self._browser_frame(node, role_str) if role == "AXGroup" else False,
            screen_text=screen_text,
            search_value=search_value,
            surface_name=surface_name,
            content_note=content_note,
            hint=hint,
            shortcut=shortcut,
            note=(outline_note or gtk_table_selection_note(node)) if role_str in _ROW_ROLE_NAMES else panel_note,
        )

    def _browser_frame(self, node: object, role_str: str) -> bool:
        """True when ``node`` is browser UI outside a content document.

        GTK and Qt are false after one toolkit check. Page content under a
        document web is false. The cache is per snapshot.
        """
        if self._browser_app is None:
            try:
                self._browser_app = bool(_chromium_app(node) or _gecko_app(node))
            except Exception:
                self._browser_app = False
        if not self._browser_app:
            return False
        try:
            return browser_frame_view(node, self._browser_chrome_cache, role_str=role_str)
        except Exception:
            return False

    def _note_unlisted(self, node: object, count: int) -> None:
        if count > 0:
            self._unlisted[_frame_cache_key(node)] = int(count)

    def take_unlisted(self, node: object) -> int:
        """Hidden children recorded for ``node`` during ``children``, once."""
        return int(self._unlisted.pop(_frame_cache_key(node), 0))

    def keep_offscreen(self, node: object) -> bool:
        """True for an expand-page child the pruner must list below the viewport."""
        return _frame_cache_key(node) in self._page_keep

    def _mark_page(self, node: object, depth: int) -> None:
        """Remember ``node`` and one level of its children so their text is listed."""
        if node is None or depth < 0:
            return
        self._page_keep.add(_frame_cache_key(node))
        if depth == 0:
            return
        count = _child_count(node)
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            return
        for index in range(min(int(count), 6)):
            self._mark_page(_child_at(node, index), depth - 1)

    def _expanded_child_page(self, node: object, start: int, limit: int) -> list:
        """Children ``[start, start + limit)``, in that order.

        Every child in the slice is returned, including ones below the
        viewport. Replacing the slice with whichever rows happen to be
        painted skips the ones in between. The first child is scrolled
        toward the screen; the rest stay in the page even if they do not
        fit. The pruner keeps them via ``keep_offscreen``.
        """
        count = _child_count(node)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            count = 0
        start = max(0, int(start))
        limit = max(1, int(limit))
        end = min(count, start + limit)

        def read() -> list:
            found = []
            for index in range(start, end):
                child = _child_at(node, index)
                if child is not None:
                    found.append(child)
            return found

        kids = read()
        if kids:
            _bring_cell_on_screen(kids[0], _screen_size(), start, max(count, 1))
            _invalidate_tree_cache(node)
            kids = read()
        for child in kids:
            self._mark_page(child, 1)
        remaining = max(0, count - (start + len(kids)))
        if remaining:
            self._note_unlisted(node, remaining)
        return kids

    def children(self, node: object) -> Sequence[object]:
        # The pruner's list object is not the wrapper ``refresh_visible``
        # probed. Keying the overlay by ``id()`` published the cached first
        # child, whose live top was above the viewport (y=-2, y=-18, y=-26
        # on the 0.4.16 retest). Read this node.
        page = self._expand_page
        if page is not None and _frame_cache_key(node) == page[0]:
            return self._expanded_child_page(node, int(page[1]), int(page[2]))
        if _role_name(node) in _LIST_ROLES and self._tree_is_chromium(node):
            seek = self._table_seek.strip() if isinstance(self._table_seek, str) else ""
            role = _role_name(node)
            if seek and role in {"table", "tree table"}:
                live = _in_view_named_rows(node)
                if not _visible_tree_matches(live, seek):
                    if _reveal_chromium_table_match(node, seek):
                        live = _in_view_named_rows(node)
            else:
                live = _in_view_named_rows(node)
            if len(_row_names(live)) >= 2:
                if role == "list":
                    live = _list_item_subtrees(node, live)
                for acc, pos, size in live:
                    self._visible_bounds[id(acc)] = (pos, size)
                # The band is the on-screen rows only. The rest of a long
                # table is still there; record it so the fold note names
                # this node and expand can page it.
                if role in {"table", "tree table"}:
                    total = _child_count(node)
                    if (
                        isinstance(total, int)
                        and not isinstance(total, bool)
                        and total > len(live)
                    ):
                        self._note_unlisted(node, total - len(live))
                return _bands_for_snapshot(live)
        visible = self._visible_children.get(id(node))
        if visible is not None:
            return _cached_rows_for_snapshot(self._visible_bounds, visible)
        # A GTK TreeView keeps every cell as a child and parks the ones
        # outside the scrolled window at the G_MININT sentinel. Reading
        # child 0..249 after End never reaches the painted rows (they sit
        # near the end of the child list), and a few hundred rows also
        # matches the Calc shortcut that only samples the first rows.
        # The on-screen span is what snapshot and scroll_to_find list.
        painted = _gtk_table_rows_for_snapshot(
            node, self._table_seek, self._offscreen_seek,
        )
        if painted:
            for acc, pos, size in painted:
                self._visible_bounds[id(acc)] = (pos, size)
            return _bands_for_snapshot(painted)
        # -1 is a wedged D-Bus read. Revive once, then treat a still-negative
        # count as empty so range() does not walk nothing and hide the dialog.
        count = _child_count(node)
        if count < 0:
            count = 0
        # Calc reports 1048576×16384 and get_child_at_index is row-major, so
        # the first fetch is all of row 1. A modest child count is the
        # visible cells (LibreOffice 25.2 exposes about 65) and is walked
        # as usual. A million-row sheet lists the cells in its on-screen
        # box. Those are banded: a flat list would keep the first 96 and
        # drop the rows below that, which is most of a 1280×800 sheet.
        if count > _MAX_CHILDREN_FETCH and _spreadsheet_table(node):
            cells = _sheet_window_cells(node)
            _share_parent_application(node, cells)
            return _publish_sheet_cells(cells)
        count = min(count, _MAX_CHILDREN_FETCH)
        kids = []
        for i in range(count):
            child = _call_first(node, ("get_child_at_index", "getChildAtIndex"), i)
            if child is not None:
                kids.append(child)
        # Toolkit/visibility guards below also ask for the child's owner.
        # Share proven ownership before those checks, not after they have
        # already climbed the same native ancestry for every child.
        _share_parent_application(node, kids)
        role = _role_name(node)
        # Evince's hidden pages can have on-screen rectangles, and its
        # painted page can have an off-screen rectangle. Trust SHOWING.
        kids = [
            child for child in kids
            if not _evince_page(child) or _state_has(child, "SHOWING")
        ]
        # A scrolled Chromium container may lack SHOWING while its children
        # are painted. Keep traversing it; apply clipping to leaf targets.
        # Only a Chromium tree has these. Asking each child walked its
        # parents to the application on every node of a Calc sheet.
        if self._tree_is_chromium(node):
            kids = [child for child in kids
                    if not chromium_clipped_target(child) or _child_count(child) > 0]
        if self._tree_is_gecko(node):
            # Firefox keeps background tabs and the preloaded New Tab page in
            # the tree with on-screen bounds. They are not SHOWING. Drop the
            # document here so snapshot and find never offer it.
            kids = [child for child in kids if not _hidden_gecko_browser(child)]
        if role == "application":
            # The combo menu is already a child of the combo. A top-level
            # window or a second menu for that same popup would list the
            # items again.
            kids = [child for child in kids if _combo_owning_popup(child) is None]
        if role == "page tab" and _negative_extent(_extents(node, keep_zero=True)[1]):
            return []
        kids = _with_table_body(node, kids)
        _share_parent_application(node, kids)
        if role == "page tab list":
            return _hoist_hidden_page_tabs(kids)
        return kids

    def _lo_placement(self, frame):
        """``(client origin, screen coordinates already include the frame)``.

        The client origin is None when no managed window sits on the frame's
        SCREEN rectangle. The flag is the root-pane check against that origin.
        """
        screen = self._frame_screen(frame)
        if screen is None or screen[2] <= 0 or screen[3] <= 0:
            return None, True
        key = (int(screen[0]), int(screen[1]), int(screen[2]), int(screen[3]))
        cached = self._lo_placements.get(key)
        if cached is not None:
            return cached
        from a11y_computer_use.drivers import _linux_system

        title = str(_call_first(frame, ("get_name",), default="") or "")
        origin = _linux_system.client_origin_for_outer_frame(
            key[0], key[1], key[2], key[3], title=title,
        )
        absolute = True if origin is None else _client_area_is_on_screen(frame, screen, origin)
        placed = (origin, absolute)
        self._lo_placements[key] = placed
        return placed

    def _libreoffice_position(self, node, position, size):
        """Screen top-left for a LibreOffice content node, or None.

        The frame's own SCREEN rectangle is the outer window, so it is left
        alone. A content node uses ``_libreoffice_content_origin``. The
        client origin is added only when this frame's client area is still
        reported at the outer corner.
        """
        if position is None or size is None:
            return None
        if size[0] < 0 or size[1] < 0:
            return None
        if position[0] < -1_000_000 or position[1] < -1_000_000:
            return None
        if _role_name(node) == "frame":
            return None
        frame = self._frame_ancestor_of(node)
        if frame is None:
            return None
        client, screen_is_absolute = self._lo_placement(frame)
        if client is None:
            return None
        window = _raw_rect(node, "WINDOW")
        if not _usable_window_rect(window):
            return None
        frame_screen = self._frame_screen(frame)
        if frame_screen is None:
            return None
        return _libreoffice_content_origin(
            position, window, frame_screen, client, screen_is_absolute=screen_is_absolute,
        )

    def _frame_screen(self, frame: object):
        """The frame's SCREEN rectangle, read once for this walk.

        Each content node placed against its frame read that rectangle
        twice, which on a Calc sheet was about 1500 extents round trips
        for one window. A failed read is not kept, so the next node asks.
        """
        known = self._frame_screens.get(id(frame))
        if known is not None and known[0] is frame:
            return known[1]
        rect = _raw_rect(frame, "SCREEN")
        if rect is not None:
            self._frame_screens[id(frame)] = (frame, rect)
        return rect

    def _frame_ancestor_of(self, node: object):
        """``_frame_ancestor`` for this walk, sharing each parent's answer.

        Every LibreOffice content node asked for its frame by climbing to
        it, so a Calc sheet climbed about five parents per node. The walk
        reaches a node through its parent, so the climb stops at the first
        ancestor this walk already placed. Entries hold the node itself,
        so a freed wrapper cannot reuse an id.
        """
        chain: list = []
        frame = None
        current = node
        for _ in range(32):
            if current is None:
                break
            known = self._frames.get(id(current))
            if known is not None and known[0] is current:
                frame = known[1]
                break
            chain.append(current)
            role = _role_name(current)
            if role == "frame":
                frame = current
                break
            if role == "application":
                break
            current = _call_first(current, ("get_parent", "getParent"))
        for item in chain:
            self._frames[id(item)] = (item, frame)
        return frame

    def _tree_is_chromium(self, node: object) -> bool:
        if self._chromium is None:
            self._chromium = _chromium_app(node)
        return self._chromium

    def _tree_is_gecko(self, node: object) -> bool:
        if self._gecko is None:
            self._gecko = _gecko_app(node)
        return self._gecko


# ---------------------------------------------------------------------------
# Geometry, app resolution, act-time helpers
# ---------------------------------------------------------------------------


def primary_geometry() -> tuple[DisplayGeometry, ...]:
    """The primary X screen as one `DisplayGeometry`. AT-SPI SCREEN extents are
    physical pixels with a top-left origin, so scale=1.0 makes the engine's
    point->pixel projection an identity (same as the Windows adapter)."""
    width, height = _screen_size()
    display = Display(display_id=0, width=width, height=height, scale=1.0, is_main=True)
    return (DisplayGeometry(display=display, origin=(0.0, 0.0)),)


def _screen_size() -> tuple[int, int]:
    """Primary screen size in pixels: Xlib, then $A11Y_COMPUTER_USE_SCREEN, then 1280x800.

    A too-small guess would make the engine drop real nodes as "offscreen", so
    err large; the value only bounds the projected coordinate space."""
    try:
        from Xlib import display as _xd

        d = _xd.Display()
        try:
            screen = d.screen()
            w = int(screen.width_in_pixels)
            h = int(screen.height_in_pixels)
        finally:
            try:
                d.close()
            except Exception:
                pass
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    env = os.environ.get("A11Y_COMPUTER_USE_SCREEN", "")
    if "x" in env:
        try:
            w_s, h_s = env.lower().split("x", 1)
            return int(w_s), int(h_s)
        except Exception:
            pass
    return 1280, 800


def pid_of(acc) -> int | None:
    if acc is None:
        return None
    pid = _call_first(acc, ("get_process_id",))
    return int(pid) if pid else None


def process_instance_id(pid: int | None) -> str | None:
    """``pid:<pid>|start:<ticks>`` for a live Linux process, else None.

    Start time is field 22 of ``/proc/<pid>/stat`` (clock ticks since boot).
    A pid reused by a later process has a different start time, so a ref
    bound to this token does not resolve against the replacement.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return None
    # comm is field 2 and may contain spaces and parentheses. Fields after
    # the last ``)`` start at field 3. starttime is field 22, index 19.
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 2:].split()
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return f"pid:{pid}|start:{fields[19]}"


def _bus_name_of(acc) -> str | None:
    """Unique AT-SPI bus name of ``acc``'s application, or None.

    libatspi stores it on ``AtspiApplication.bus_name`` (``:1.N``). A
    relaunched process connects again and gets a new name.
    """
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    inner = getattr(app, "app", None)
    if inner is None:
        inner = getattr(acc, "app", None)
    name = getattr(inner, "bus_name", None) if inner is not None else None
    if isinstance(name, bytes):
        name = name.decode("utf-8", "replace")
    text = str(name or "")
    if text.startswith(":"):
        return text
    return None


def app_instance_id(acc) -> str | None:
    """Process identity for ``acc``: pid and start time, plus the bus name.

    None when neither the pid nor a unique bus name can be read. Callers
    that cannot record an identity leave the ref unbound rather than
    inventing one.
    """
    if acc is None:
        return None
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    pid = pid_of(app) or pid_of(acc)
    token = process_instance_id(pid)
    bus = _bus_name_of(acc)
    if token and bus:
        return f"{token}|bus:{bus}"
    if token:
        return token
    if bus:
        return f"bus:{bus}"
    return None


_DOCUMENT_WINDOW_ROLES = frozenset({"frame", "window", "dialog"})


def window_context_identity(acc, role: str) -> tuple:
    """Stable native context for titles that change without replacing controls.

    A document web maps to AXGroup, so the shared role cannot distinguish it
    from a named semantic container. Never use a Python proxy id as identity
    across observations; missing native identity keeps ordinary name checks.
    """
    if role not in {"frame", "window", "document web", "document frame"}:
        return ()
    key = _atspi_wrapper_key(acc)
    return (role, *key) if key is not None else ()


def window_document_id(_acc, role: str, title: str) -> str | None:
    """Identity of a top-level window or dialog, else None.

    The accessible name, with one leading ``*`` removed so a dirty GTK
    buffer (``*/tmp/a.txt - Mousepad``) stays the same document as the
    clean title. Descendants inherit the id while the snapshot is flattened.
    The accessible is accepted so a later reader can prefer a document URL
    without changing the call.
    """
    if role not in _DOCUMENT_WINDOW_ROLES:
        return None
    text = normalize_document_title(title)
    if not text:
        return None
    return f"title:{text}"


_DOC_URL_KEYS = ("docurl", "uri", "url")
_LINK_URL_KEYS = ("href", "link-target")


def _hyperlink_target(acc) -> str | None:
    """The Hyperlink URI of this node, not a document URL inherited from a parent."""
    from a11y_computer_use.untrusted import looks_like_url

    link = _call_first(acc, ("get_hyperlink",))
    if link is not None:
        uri = _call_first(link, ("get_uri",), 0)
        if isinstance(uri, str) and looks_like_url(uri.strip()):
            return uri.strip()
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() in _LINK_URL_KEYS and isinstance(val, str) and looks_like_url(val.strip()):
            return val.strip()
    return None


def hyperlink_uri(acc) -> str | None:
    """URI of a link node, or of an ancestor whose role is link.

    Chromium's document exposes Hyperlink as the page URL. That is the
    document URL, not a navigation target, so the walk stops at a document.
    """
    node = acc
    for _ in range(6):
        if node is None:
            return None
        role = _role_name(node)
        if "document" in role:
            return None
        if role == "link":
            return _hyperlink_target(node)
        node = _call_first(node, ("get_parent",))
    return None


def _doc_url(acc) -> str | None:
    """DocURL for a document accessible, else None."""
    from a11y_computer_use.untrusted import looks_like_url

    role = _role_name(acc)
    if "document" not in role:
        return None
    # Chromium exposes the page URL as the document's hyperlink. That is not
    # a link the agent clicked; document_url is the only reader that wants it.
    link = _call_first(acc, ("get_hyperlink",))
    if link is not None:
        uri = _call_first(link, ("get_uri",), 0)
        if isinstance(uri, str) and looks_like_url(uri.strip()):
            return uri.strip()
    for key in ("DocURL", "URI", "Url"):
        val = _call_first(acc, ("get_document_attribute_value",), key)
        if isinstance(val, str) and looks_like_url(val.strip()):
            return val.strip()
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() in _DOC_URL_KEYS and isinstance(val, str) and looks_like_url(val.strip()):
            return val.strip()
    description = _call_first(acc, ("get_description",), default="")
    if isinstance(description, str) and looks_like_url(description.strip()):
        return description.strip()
    return None


def _collected_content_document(root) -> str | None:
    """On-screen content URL from Collection, or None when it has no documents.

    Firefox's showing document is reachable from the focused node and from
    Collection, and a child walk from the window often never meets it. The
    same filter as the walk applies: a hidden tab is not the page, and an
    iframe does not replace the top document.
    """
    if root is None:
        return None
    from a11y_computer_use.untrusted import is_browser_chrome_url

    try:
        Atspi = _atspi()
    except Exception:
        return None
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return None
    role_enum = getattr(Atspi, "Role", None)
    roles = []
    for name in ("DOCUMENT_WEB", "DOCUMENT_FRAME"):
        role = getattr(role_enum, name, None)
        if role is not None:
            roles.append(role)
    if not roles:
        return None
    try:
        states = Atspi.StateSet.new([])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(
            states, mt.NONE, {}, mt.NONE, roles, mt.ANY, [], mt.NONE, False,
        )
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 12, True)
    except Exception:  # noqa: BLE001 - GTK and fakes expose no Collection
        return None
    held: list[tuple[object, str]] = []
    for hit in hits or []:
        url = _doc_url(hit)
        if url and not is_browser_chrome_url(url):
            held.append((hit, url))
    return _pick_content_document(held)


def _libreoffice_accessible(acc) -> bool:
    """True when ``acc`` belongs to LibreOffice.

    The application name is ``soffice`` (the frame title also says
    LibreOffice). A browser window whose page title mentions LibreOffice is
    not this: its application name is Firefox or Chrome.
    """
    if acc is None:
        return False
    app = _call_first(acc, ("get_application", "getApplication"))
    target = app if app is not None else acc
    name = _call_first(target, ("get_name", "getName"), default="") or ""
    return libreoffice_app(str(name))


def document_url_of(root) -> str | None:
    """Content-document URL under ``root``.

    Firefox keeps every tab's document in the tree. The URL is the selected
    tab that is SHOWING, not the first document a walk meets. Collection is
    asked first, because that showing document is often missing from the
    window's children. A Chromium omnibox popup is browser chrome and is not
    a page URL. An iframe's document is nested; the page URL is the document
    that is not inside another one. The walk is bounded so one policy check
    cannot become a full tree walk.

    LibreOffice has no page URL. Collection ``get_matches`` on a Calc sheet
    walks the table (about a million by sixteen thousand cells, child count
    2**31, ``MANAGES_DESCENDANTS``) and wedges that app's accessibility
    connection: the next child count is -1 and the following snapshot spends
    the per-call timeout on every read. Return None before any of that.
    """
    if root is None or _libreoffice_accessible(root):
        return None
    collected = _collected_content_document(root)
    if collected:
        return collected
    from a11y_computer_use.untrusted import is_browser_chrome_url

    held: list[tuple[object, str]] = []
    queue = [root]
    seen = 0
    visited: set[int] = set()
    while queue and seen < 400:
        node = queue.pop(0)
        ident = id(node)
        if ident in visited:
            continue
        visited.add(ident)
        seen += 1
        url = _doc_url(node)
        if url and not is_browser_chrome_url(url):
            if not _gecko_app(node) and not _nested_in_document(node):
                return url
            if (
                _gecko_app(node)
                and _gecko_web_document_on_screen(node)
                and not _nested_in_document(node)
            ):
                return url
            held.append((node, url))
        elif url and is_browser_chrome_url(url):
            # The popup's children are browser UI. Do not spend the budget
            # walking them when the page document is a sibling window.
            continue
        count = _call_first(node, ("get_child_count",), default=0) or 0
        for index in range(min(int(count), 80)):
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                queue.append(child)
    return _pick_content_document(held)


def _nested_in_document(node) -> bool:
    """True when ``node`` sits inside another content document (an iframe)."""
    from a11y_computer_use.untrusted import is_browser_chrome_url

    parent = _parent_of(node)
    seen: set[int] = set()
    for _ in range(16):
        if parent is None or id(parent) in seen:
            return False
        seen.add(id(parent))
        url = _doc_url(parent)
        if url and not is_browser_chrome_url(url):
            return True
        if _role_name(parent) in {"frame", "window", "application"}:
            return False
        parent = _parent_of(parent)
    return False


def _pick_content_document(candidates: list[tuple[object, str]]) -> str | None:
    """The on-screen top document, or None when every Firefox document is hidden."""
    if not candidates:
        return None
    visible: list[tuple[object, str]] = []
    for node, url in candidates:
        if _gecko_app(node) and not _gecko_web_document_on_screen(node):
            continue
        visible.append((node, url))
    if not visible:
        if any(_gecko_app(node) for node, _url in candidates):
            return None
        visible = list(candidates)
    top = [(node, url) for node, url in visible if not _nested_in_document(node)]
    pool = top or visible
    for node, url in pool:
        if _state_has(node, "SHOWING"):
            return url
    return pool[0][1]


def other_frame_document_url(app_root, skip) -> str | None:
    """Page URL of a top-level frame other than ``skip``.

    The active frame can be the omnibox popup. The page lives in another
    frame of the same application. Each frame is walked on its own budget.
    """
    if app_root is None:
        return None
    count = _call_first(app_root, ("get_child_count",), default=0) or 0
    for index in range(min(int(count), 8)):
        frame = _call_first(app_root, ("get_child_at_index",), index)
        if frame is None or frame is skip:
            continue
        url = document_url_of(frame)
        if url:
            return url
    return None


def document_url_for(acc) -> str | None:
    """URL of the content document that owns ``acc``.

    An iframe is its own document, so a control inside the frame reports the
    frame origin. The toolbar, the tab strip, and the omnibox are not inside
    a content document.
    """
    from a11y_computer_use.untrusted import is_browser_chrome_url

    node = acc
    seen: set[int] = set()
    for _ in range(32):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        url = _doc_url(node)
        if url and not is_browser_chrome_url(url):
            return url
        if _role_name(node) == "application":
            return None
        parent = _parent_of(node)
        if parent is None or parent is node:
            return None
        node = parent
    return None


def _atspi_object_path(acc) -> str | None:
    """The AT-SPI object path, or None for a test double.

    Read from the libatspi object. This does not call D-Bus. A path that is
    not an accessibility object is ignored.
    """
    pointer = _gobject_pointer(acc)
    if not pointer:
        return None
    try:
        import ctypes

        class _GObject(ctypes.Structure):
            _fields_ = [
                ("g_class", ctypes.c_void_p),
                ("ref_count", ctypes.c_uint),
                ("pad", ctypes.c_uint),
                ("qdata", ctypes.c_void_p),
            ]

        class _AtspiObject(ctypes.Structure):
            _fields_ = [
                ("parent", _GObject),
                ("app", ctypes.c_void_p),
                ("path", ctypes.c_char_p),
            ]

        raw = _AtspiObject.from_address(pointer).path or b""
        if not raw.startswith(b"/org/a11y/atspi/"):
            return None
        return raw.decode("utf-8", "replace")
    except Exception:
        return None


@lru_cache(maxsize=1)
def _atspi_identity_layouts():
    """Build immutable native layouts once; identity values remain live reads."""
    import ctypes

    class _GObject(ctypes.Structure):
        _fields_ = [
            ("g_class", ctypes.c_void_p),
            ("ref_count", ctypes.c_uint),
            ("pad", ctypes.c_uint),
            ("qdata", ctypes.c_void_p),
        ]

    class _AtspiObject(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("app", ctypes.c_void_p),
            ("path", ctypes.c_char_p),
        ]

    class _AtspiApplication(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("hash", ctypes.c_void_p),
            ("bus_name", ctypes.c_char_p),
            ("bus", ctypes.c_void_p),
        ]

    return _AtspiObject, _AtspiApplication


def _atspi_wrapper_key(acc) -> tuple[str, str] | None:
    """``(bus_name, object_path)`` for one libatspi accessible, else None.

    ``get_child_at_index`` can return a new Python wrapper after the previous
    one is collected, and CPython can reuse that wrapper's ``id()``. The bus
    name and object path stay on the C object. This does not call D-Bus. A
    test double has no gpointer. An object whose unique bus name cannot be
    read is None, so the caller does not treat two applications that happen
    to publish the same path as one node.
    """
    pointer = _gobject_pointer(acc)
    if not pointer:
        return None
    try:
        _AtspiObject, _AtspiApplication = _atspi_identity_layouts()
    except ImportError:
        return None

    try:
        obj = _AtspiObject.from_address(pointer)
        raw_path = obj.path or b""
        # GTK4 exports accessibles below its application path (for example
        # /org/gtk/application/.../a11y/UUID), not the ATK bridge prefix.
        # The libatspi object already owns this D-Bus path; pair it with the
        # unique bus name rather than discarding GTK4's native identity.
        if not raw_path.startswith(b"/") or not obj.app:
            return None
        raw_bus = _AtspiApplication.from_address(obj.app).bus_name or b""
        if not raw_bus.startswith(b":"):
            return None
        return (raw_bus.decode("utf-8", "replace"), raw_path.decode("utf-8", "replace"))
    except Exception:
        return None


def _ancestor_key(acc) -> object:
    """Identity of one accessible across recreated Python wrappers.

    The AT-SPI bus name and object path when both are on the GObject.
    Otherwise ``id()`` of this wrapper. A collected wrapper's ``id()`` can
    be reused for a different accessible, so a walk that falls back to
    ``id()`` keeps every wrapper alive until it finishes.
    """
    stable = _atspi_wrapper_key(acc)
    if stable is not None:
        return stable
    return id(acc)


def _ancestor_chain(acc, limit: int = 32):
    """Yield ``acc`` and its parents, nearest first.

    Each wrapper stays referenced until this generator is closed. The seen
    set is `_ancestor_key`: the bus name and object path when the GObject
    has them. ``get_parent`` returns a new proxy, and CPython reuses the
    ``id()`` of one that nothing else references. Electron's page sits
    several panels under the frame; that collision used to end the walk
    on a panel and report the showing document hidden.
    """
    node = acc
    pinned: list = []
    seen: set[object] = set()
    for _ in range(limit):
        if node is None:
            return
        identity = _ancestor_key(node)
        if identity in seen:
            return
        seen.add(identity)
        pinned.append(node)
        yield node
        parent = _parent_of(node)
        if parent is None:
            return
        if _ancestor_key(parent) == identity:
            return
        node = parent


def _frame_cache_key(acc) -> object:
    """A stable key for one accessible across wrapper objects.

    ``get_parent`` returns a new proxy each call. ``id()`` would miss, and
    every node would walk to the application over D-Bus. The object path is
    the same proxy to proxy. A test double has no path and uses ``id()``.
    """
    path = _atspi_object_path(acc)
    if path:
        return path
    return id(acc)


def browser_frame_view(
    acc,
    cache: dict[object, bool],
    *,
    role_str: str | None = None,
) -> bool:
    """True when ``acc`` is Chromium or Firefox UI outside a content document.

    The caller has already decided ``acc`` belongs to a browser. A
    ``document web`` or ``internal frame`` is page content, including when
    its URL is not published yet. An omnibox-popup URL is still browser UI.
    ``cache`` remembers each object path so a snapshot walks each parent
    once. GTK and Qt must not call this: a walk that never meets a content
    document would mark the whole tree as browser UI.
    """
    if acc is None:
        return False
    found = cache.get(_frame_cache_key(acc))
    if found is not None:
        return found
    from a11y_computer_use.untrusted import is_browser_chrome_url

    chain: list = []
    current = acc
    current_role = role_str
    seen: set[int] = set()
    result = True
    for _ in range(32):
        if current is None or id(current) in seen:
            result = True
            break
        seen.add(id(current))
        if current is not acc:
            cached = cache.get(_frame_cache_key(current))
            if cached is not None:
                result = cached
                break
        chain.append(current)
        role = current_role if current_role is not None else _role_name(current)
        current_role = None
        if role in {"document web", "internal frame"}:
            url = _doc_url(current)
            result = bool(url and is_browser_chrome_url(url))
            break
        if "document" in role:
            url = _doc_url(current)
            if url:
                result = bool(is_browser_chrome_url(url))
                break
        if role == "application":
            result = True
            break
        parent = _parent_of(current)
        if parent is None or parent is current:
            result = True
            break
        current = parent
    for item in chain:
        cache[_frame_cache_key(item)] = result
    return result


def in_browser_chrome(acc) -> bool:
    """True when ``acc`` is Chromium or Firefox UI, not page content.

    GTK and Qt are not browser chrome. A node inside a page or iframe
    document is page content even when its text looks like a URL.
    """
    from a11y_computer_use.untrusted import is_browser_chrome_url

    if acc is None or not (_chromium_app(acc) or _gecko_app(acc)):
        return False
    node = acc
    seen: set[int] = set()
    for _ in range(32):
        if node is None or id(node) in seen:
            return True
        seen.add(id(node))
        url = _doc_url(node)
        if url and is_browser_chrome_url(url):
            return True
        if url:
            return False
        if _role_name(node) == "application":
            return True
        parent = _parent_of(node)
        if parent is None or parent is node:
            return True
        node = parent
    return True


_LOCATION_NAME_BITS = (
    "address and search",
    "enter address",
    "search or enter",
    "search with google",
    "location bar",
    "url bar",
)


def _node_id(acc) -> str:
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() in {"id", "html-id", "id-attribute"} and isinstance(val, str):
            return val.strip().lower()
    return ""


def _node_plain_text(acc) -> str:
    """Text of ``acc`` for the address bar. A fake may set ``text`` or ``get_text``."""
    direct = getattr(acc, "text", None)
    if isinstance(direct, str) and direct:
        return direct
    getter = getattr(acc, "get_text", None)
    if callable(getter):
        got = None
        try:
            got = getter(0, -1)
        except TypeError:
            got = _safe(getter, None)
        if isinstance(got, str) and got:
            return got
    try:
        raw = _full_text(acc)
    except Exception:
        raw = None
    return raw if isinstance(raw, str) else ""


def is_location_entry(acc) -> bool:
    """True for the address bar of Chromium or Firefox, not a page text field.

    The node is outside any content document. Its name or id is the location
    bar, or it is an entry whose text is a URL. A GTK entry is neither.
    """
    from a11y_computer_use.untrusted import looks_like_url

    if acc is None or not in_browser_chrome(acc):
        return False
    ident = _node_id(acc)
    if "urlbar" in ident:
        return True
    name = _node_name(acc).lower()
    if any(bit in name for bit in _LOCATION_NAME_BITS):
        return True
    role = _role_name(acc)
    if role in {"entry", "text", "combo box", "editable text", "combo-box"}:
        if looks_like_url(_node_plain_text(acc).strip()) or looks_like_url(_node_name(acc).strip()):
            return True
    return False


def address_bar_text_under(root) -> str | None:
    """Text of the location entry under ``root``, preferring the focused one."""
    from a11y_computer_use.untrusted import is_browser_chrome_url, looks_like_url

    if root is None:
        return None
    found = []
    queue = [root]
    seen = 0
    visited: set[int] = set()
    while queue and seen < 500 and len(found) < 4:
        node = queue.pop(0)
        if node is None or id(node) in visited:
            continue
        visited.add(id(node))
        seen += 1
        if is_location_entry(node):
            found.append(node)
        url = _doc_url(node)
        if url and not is_browser_chrome_url(url):
            continue
        count = _call_first(node, ("get_child_count",), default=0) or 0
        for index in range(min(int(count), 40)):
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                queue.append(child)
    if not found:
        return None
    ordered = sorted(found, key=lambda node: not _state_has(node, "FOCUSED"))
    for node in ordered:
        text = _node_plain_text(node).strip()
        if looks_like_url(text):
            return text
        name = _node_name(node).strip()
        if looks_like_url(name):
            return name
        if text:
            return text
    return None


def location_text(node) -> str | None:
    """Visible text of a location entry. A URL wins over the accessible name."""
    from a11y_computer_use.untrusted import looks_like_url

    if node is None:
        return None
    text = _node_plain_text(node).strip()
    name = _node_name(node).strip()
    if looks_like_url(text):
        return text
    if looks_like_url(name):
        return name
    return text or None


def _location_entries(root) -> list:
    """Location entries Collection can see under ``root``.

    Chromium's address bar is the focused entry, and Collection lists it.
    A child walk from the application often never meets that entry. A fake
    has no Collection interface, so the caller also walks.
    """
    if root is None:
        return []
    try:
        Atspi = _atspi()
    except Exception:
        return []
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return []
    role = getattr(getattr(Atspi, "Role", None), "ENTRY", None)
    if role is None:
        return []
    try:
        states = Atspi.StateSet.new([])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(
            states, mt.NONE, {}, mt.NONE, [role], mt.ANY, [], mt.NONE, False,
        )
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 12, True)
    except Exception:  # noqa: BLE001 - GTK and fakes expose no Collection
        return []
    return [hit for hit in (hits or []) if is_location_entry(hit)]


def address_bar_text(app: str) -> str | None:
    """The address bar under ``app``, including when the omnibox popup is focused.

    The popup window does not contain the entry that holds the typed URL.
    The focused location entry is read first. Collection is next, because
    Chromium does not put that entry where a child walk can see it. The
    walk remains for a toolkit that has no Collection interface.
    """
    from a11y_computer_use.schema import Scope

    if not app:
        return None
    try:
        acc, truncated = _focused_node(app)
    except Exception:  # noqa: BLE001 - an unreadable focus is not the bar
        acc, truncated = None, False
    if acc is not None and not truncated and is_location_entry(acc):
        text = location_text(acc)
        if text:
            return text
    root = find_root(app, Scope.APP)
    collected = _collected_address_bar(app)
    if collected:
        return collected
    return address_bar_text_under(root)


def _collected_address_bar(app: str) -> str | None:
    """The address bar as Collection reports it, ignoring keyboard focus.

    The focused wrapper can still show the pre-type URL after the keys
    landed in a newer accessible for the same entry.
    """
    from a11y_computer_use.schema import Scope

    if not app:
        return None
    return _address_bar_from_entries(_location_entries_under(find_root(app, Scope.APP)))


def _location_entries_under(root) -> list:
    """Location entries on ``root`` and on each of its top-level frames."""
    entries = _location_entries(root)
    if entries or root is None:
        return entries
    count = _call_first(root, ("get_child_count",), default=0) or 0
    for index in range(min(int(count), 6)):
        frame = _call_first(root, ("get_child_at_index",), index)
        entries.extend(_location_entries(frame))
    return entries


def _address_bar_from_entries(entries: list) -> str | None:
    from a11y_computer_use.untrusted import looks_like_url

    ordered = sorted(entries, key=lambda node: not _state_has(node, "FOCUSED"))
    for node in ordered:
        text = location_text(node)
        if text and looks_like_url(text):
            return text
    for node in ordered:
        text = location_text(node)
        if text:
            return text
    return None


def location_shows_typed(before: str | None, after: str | None, typed: str) -> bool:
    """Whether ``typed`` landed in a location entry.

    The omnibox often drops ``http://`` once the host matches the current
    site. A page field does not use this. An entry that did not change is
    not a success.
    """
    if _typed_visible(before, after, typed):
        return True
    if not after or not typed:
        return False
    bare = typed
    lowered = typed.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            bare = typed[len(prefix):]
            break
    if bare == typed or not bare:
        return False
    if _typed_visible(before, after, bare):
        return True
    before_n = _norm_nbsp(before) or ""
    after_n = _norm_nbsp(after) or ""
    return bare in after_n and after_n != before_n


def _focused_accessibles(root, limit: int = 6) -> list:
    """Focused nodes Collection can see under ``root``.

    Chromium can mark the address bar and the page focused at the same time
    after ctrl+l. A query for one match may return the page. Callers that
    care about the address bar have to see the whole set.
    """
    if root is None:
        return []
    try:
        Atspi = _atspi()
    except Exception:
        return []
    st = getattr(Atspi, "StateType", None)
    focused_state = getattr(st, "FOCUSED", None)
    if focused_state is None:
        return []
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return []
    try:
        states = Atspi.StateSet.new([focused_state])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(
            states, mt.ALL, {}, mt.NONE, [], mt.NONE, [], mt.NONE, False,
        )
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, limit, True)
    except Exception:  # noqa: BLE001 - GTK and fakes expose no Collection
        return []
    return list(hits or [])


def focused_location_entry(app: str):
    """The focused address bar, even when the page is focused too."""
    if not app:
        return None
    from a11y_computer_use.schema import Scope

    try:
        root = find_root(app, Scope.APP)
    except Exception:  # noqa: BLE001 - no desktop is not an address bar
        root = None
    for hit in _focused_accessibles(root):
        try:
            if is_location_entry(hit):
                return hit
        except Exception:  # noqa: BLE001 - one bad node is not the bar
            continue
    try:
        acc, truncated = _focused_node(app)
    except Exception:  # noqa: BLE001 - unknown focus is not the address bar
        return None
    if acc is None or truncated:
        return None
    if is_location_entry(acc):
        return acc
    return None


def focus_in_browser_chrome(app: str) -> bool:
    """True when keyboard focus is in browser UI rather than a page document."""
    if not app:
        return False
    try:
        if focused_location_entry(app) is not None:
            return True
    except Exception:  # noqa: BLE001 - fall through to the single focused node
        pass
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        acc, truncated = None, False
    if acc is not None and not truncated:
        return in_browser_chrome(acc)
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    if root is None:
        return False
    return _window_is_only_browser_chrome(root)


def _window_is_only_browser_chrome(root) -> bool:
    """True when ``root`` has a browser-chrome document and no page document."""
    from a11y_computer_use.untrusted import is_browser_chrome_url

    chrome = False
    queue = [root]
    seen = 0
    visited: set[int] = set()
    while queue and seen < 80:
        node = queue.pop(0)
        if node is None or id(node) in visited:
            continue
        visited.add(id(node))
        seen += 1
        url = _doc_url(node)
        if url and is_browser_chrome_url(url):
            chrome = True
            continue
        if url:
            return False
        count = _call_first(node, ("get_child_count",), default=0) or 0
        for index in range(min(int(count), 20)):
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                queue.append(child)
    return chrome


def _showing_menu_labels(menu):
    """Ordered item names of an unnamed open menu, or None.

    A named menu is an application menu. Open means the menu or one of its
    items is SHOWING. A closed model is not this menu.
    """
    if menu is None:
        return None
    try:
        if _role_name(menu) not in {"menu", "popup menu"}:
            return None
        if _node_name(menu):
            return None
        menu_showing = _state_has(menu, "SHOWING")
    except Exception:
        return None
    count = _child_count(menu)
    if count < 0:
        count = 0
    labels = []
    item_showing = False
    for index in range(min(int(count), 64)):
        child = _child_at(menu, index)
        if child is None:
            continue
        if _role_name(child) not in {"menu item", "check menu item", "radio menu item"}:
            continue
        if _state_has(child, "SHOWING"):
            item_showing = True
        labels.append(_node_name(child))
    if not labels or not any(labels) or not (menu_showing or item_showing):
        return None
    return tuple(labels)


def _popup_item_labels(node):
    """Item names of an unnamed showing popup, or None.

    A window counts when it holds one such menu. A second menu is a menu
    bar tear-off, not a combo popup. A named window or menu is not one.
    """
    if node is None or _node_name(node):
        return None
    role = _role_name(node)
    if role in {"menu", "popup menu"}:
        return _showing_menu_labels(node)
    if role != "window":
        return None
    count = _child_count(node)
    if count < 0:
        count = 0
    labels = None
    for index in range(min(int(count), 4)):
        child = _child_at(node, index)
        child_labels = _showing_menu_labels(child)
        if child_labels is None:
            continue
        if labels is not None:
            return None
        labels = child_labels
    return labels


def _application_ancestor(node):
    """The application that contains ``node``, or None."""
    current = node
    for _ in range(20):
        if current is None:
            return None
        if _role_name(current) == "application":
            return current
        parent = _parent_of(current)
        if parent is None or parent is current:
            return _call_first(current, ("get_application", "getApplication"))
        current = parent
    return None


def _open_gtk_combo_menus(application):
    """``(combo, item names)`` for each GTK combo whose menu is open.

    Tables, documents, and menus are not descended into. A combo's menu is
    read from the combo itself.
    """
    found = []
    stack = [application]
    seen: set[int] = set()
    # Keep each wrapper alive. ``id()`` is reused after a PyGObject proxy is freed.
    held: list[object] = []
    steps = 0
    skip = {
        "table", "tree table", "tree", "list", "list box",
        "document web", "document frame", "menu bar", "menu", "popup menu",
    }
    while stack and steps < 250:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        held.append(node)
        steps += 1
        role = _role_name(node)
        if role == "combo box" and _gtk_combo(node):
            count = _child_count(node)
            if count < 0:
                count = 0
            for index in range(min(int(count), 8)):
                labels = _showing_menu_labels(_child_at(node, index))
                if labels is not None:
                    found.append((node, labels))
                    break
            continue
        if role in skip:
            continue
        count = _child_count(node)
        if count < 0:
            count = 0
        for index in range(min(int(count), 30)):
            child = _child_at(node, index)
            if child is not None:
                stack.append(child)
    return found


def _combo_with_same_open_menu(window):
    """The GTK combo whose open menu lists the same items as ``window``, or None.

    Used when the popup's parent is the application or the popup window, so
    the parent walk never reaches the combo. The item names have to match
    the combo's open menu exactly. A context menu with different items
    stays. Chromium, Firefox, and Qt are not matched.
    """
    try:
        if _chromium_app(window) or _gecko_app(window) or _qt_app(window):
            return None
        labels = _popup_item_labels(window)
        if labels is None:
            return None
        app = _application_ancestor(window)
        if app is None or app is window:
            return None
        for combo, item_labels in _open_gtk_combo_menus(app):
            if item_labels == labels:
                return combo
    except Exception:
        return None
    return None


def _combo_owning_popup(window):
    """The GTK combo whose menu this top-level popup is showing, or None.

    GtkComboBox puts the open menu in its own window and marks that window
    ACTIVE. The menu's parent is still the combo, so the options are already
    under the combo. GTK also exposes that same popup as its own menu under
    the application. When that menu's parent is not the combo, the SHOWING
    item names still identify it. A named frame, a dialog, a menu-bar menu,
    a context menu with different items, and a Chromium or Qt popup are not
    this window.
    """
    if window is None:
        return None
    try:
        role = _role_name(window)
        if role not in {"window", "popup menu", "menu"}:
            return None
        if role == "window" and _node_name(window):
            return None
        if role == "menu" and _node_name(window):
            return None
        if _chromium_app(window) or _gecko_app(window) or _qt_app(window):
            return None
    except Exception:
        return None
    if role == "menu":
        # A context menu can be parented to the combo it was opened on.
        # Item names, not that parent, decide whether this is the combo popup.
        return _combo_with_same_open_menu(window)
    starts = []
    if role == "popup menu":
        starts.append(window)
    else:
        count = _child_count(window)
        if count < 0:
            count = 0
        for index in range(min(int(count), 4)):
            child = _child_at(window, index)
            if child is not None and _role_name(child) in {"menu", "popup menu"}:
                starts.append(child)
    for start in starts:
        node = start
        for _ in range(8):
            parent = _parent_of(node)
            if parent is None or parent is node:
                break
            parent_role = _role_name(parent)
            if parent_role == "combo box":
                if _gtk_combo(parent):
                    return parent
                return None
            if parent_role in {
                "menu bar", "frame", "dialog", "application", "window",
                "document web", "document frame",
            }:
                break
            node = parent
    # The popup menu is a different object, parented to the application or
    # to this window, so the walk above never reaches the combo.
    return _combo_with_same_open_menu(window)


def _frame_containing(node, frames):
    """The frame or dialog in ``frames`` that contains ``node``, or None."""
    if node is None:
        return None
    wanted = {id(frame): frame for frame in frames}
    current = node
    for _ in range(20):
        if current is None:
            return None
        found = wanted.get(id(current))
        if found is not None:
            return found
        parent = _parent_of(current)
        if parent is None or parent is current:
            return None
        current = parent
    return None


def _preferred_window(frames, is_active):
    """The window a snapshot should root at.

    An ACTIVE frame or dialog wins. When the only ACTIVE top-level is a GTK
    combo popup, the frame that contains that combo stays the root, so the
    popup's menu items are listed under the combo instead of replacing it.
    """
    if not frames:
        return None
    popup_ids = {id(frame) for frame in frames if _combo_owning_popup(frame) is not None}
    ordinary = [frame for frame in frames if id(frame) not in popup_ids]
    for frame in ordinary:
        if is_active(frame):
            return frame
    if popup_ids:
        owners = []
        for frame in frames:
            if id(frame) not in popup_ids:
                continue
            combo = _combo_owning_popup(frame)
            owner = _frame_containing(combo, ordinary)
            if owner is not None and all(owner is not item for item in owners):
                owners.append(owner)
        if len(owners) == 1:
            return owners[0]
        showing = [frame for frame in ordinary if _state_has(frame, "SHOWING")]
        dialogs = [frame for frame in showing if _role_name(frame) == "dialog"]
        if len(dialogs) == 1:
            return dialogs[0]
        if len(showing) == 1:
            return showing[0]
        if dialogs:
            return dialogs[0]
        if showing:
            return showing[0]
    if ordinary:
        return ordinary[0]
    return frames[0]


def find_root(app: str, scope) -> object | None:
    """The AT-SPI root for ``app`` at ``scope``.

    ``app`` matches an application accessible by name substring (case-insensitive)
    — the Linux analog of a bundle id / process exe. For `Scope.APP` the app
    accessible is returned; for `Scope.WINDOW` its active (else first) top-level
    frame. A GTK combo popup that is the only ACTIVE window does not replace
    the frame that contains the combo. None when nothing matches (the engine
    yields an empty snapshot)."""
    from a11y_computer_use.schema import Scope

    Atspi = _atspi()
    needle = (app or "").lower()
    if not needle:
        return None
    desktop = _safe(lambda: Atspi.get_desktop(0))
    if desktop is None:
        return None
    # A killed/restarted app can be absent from the desktop's cached child
    # array. Refresh the registry before ranking candidates: refreshing the
    # selected app's windows cannot discover its replacement registrant.
    applications = _fresh_application_children(desktop)
    st = getattr(Atspi, "StateType", None)
    active = getattr(st, "ACTIVE", None)

    def _frames(acc):
        return _fresh_application_children(acc)

    def _is_active(frame) -> bool:
        sset = _call_first(frame, ("get_state_set",))
        return bool(active is not None and sset is not None and _safe(lambda: sset.contains(active), False))

    # Several applications can share a name: on Linux the permission-keying app
    # id is the process comm ("python3"), and every Python process that touched
    # AT-SPI (this one included) registers on the bus with that name and no
    # windows. Rank matches so a windowless registrant never shadows the real
    # app: active top-level frame first, then any app that owns frames, then
    # the first name match.
    # The a11y application name is the program name (GLib prgname, e.g.
    # "cuatestapp"), while the Linux app id used for permissions is the window
    # owner's comm (e.g. "python3"); match by PID as well as by name so an id
    # resolved from X11 finds the same application on the a11y bus.
    from a11y_computer_use.drivers import _linux_system
    from a11y_computer_use.schema import ComputerUseError

    try:
        pids = _linux_system.pids_matching(app)
    except ComputerUseError:
        raise
    except Exception:  # noqa: BLE001 - X11 may be unavailable (Wayland/headless)
        pids = set()
    # A portal process can contain dialogs for several apps. Bind only its
    # active, explicitly parented window, never its whole application tree.
    portal = None
    try:
        row = _linux_system.active_window()
        if row and row.get("app", "").lower() == needle:
            pid = int(row.get("pid") or 0)
            identity = _linux_system._identity_for_pid(pid, _linux_system._comm_for_pid(pid))
            if identity in _linux_system._PORTAL_BACKENDS and row["app"] != identity:
                rows = [r for r in _linux_system.windows()
                        if r.get("pid") == pid and r.get("title") == row.get("title")]
                if row.get("title") and len(rows) == 1:
                    portal = row
    except ComputerUseError:
        raise
    except Exception:  # noqa: BLE001 - unavailable X11 cannot authorize a portal
        pass
    if portal is not None:
        for candidate in applications:
            if candidate is None or pid_of(candidate) != portal["pid"]:
                continue
            frames = [f for f in _frames(candidate)
                      if _call_first(f, ("get_name",), default="") == portal["title"]]
            if len(frames) == 1 and _is_active(frames[0]):
                return frames[0]
        return None
    best = None
    best_rank = -1
    hinted = next(iter(pids)) if len(pids) == 1 else None
    for candidate in applications:
        if candidate is None:
            continue
        name = (_call_first(candidate, ("get_name",), default="") or "").lower()
        name_unanswered = call_went_unanswered()
        # A dialog opened with Gtk.Dialog.run() wedges this process's
        # connection to the app: get_name comes back empty and child count
        # is -1 until the bus is replaced. Revive before treating the
        # registrant as unnamed, then read the name on the fresh connection.
        # A read that timed out is not that dialog: reviving it would spend
        # another timeout per child. A slow reply that returned still can be.
        if not name and not name_unanswered:
            _child_count(candidate)
            name = (_call_first(candidate, ("get_name",), default="") or "").lower()
        if needle not in name and not (pids and pid_of(candidate) in pids):
            continue
        with app_reply_watch(app, hinted, candidate) as watch:
            pid = pid_of(candidate)
            if pid:
                watch.pid = pid
            frames = _frames(candidate)
            rank = 2 if any(_is_active(f) for f in frames) else (1 if frames else 0)
        if rank > best_rank:
            best, best_rank = candidate, rank
            if rank == 2:
                break
    app_acc = best
    if app_acc is None or scope is Scope.APP:
        return app_acc
    # WINDOW scope: prefer the ACTIVE top-level frame, else the first child.
    # The same per-app budget covers this second frame read. A stopped
    # target raises here instead of walking every child at 300ms each.
    with app_reply_watch(app, hinted, app_acc) as watch:
        pid = pid_of(app_acc)
        if pid:
            watch.pid = pid
        frames = _frames(app_acc)
        picked = _preferred_window(frames, _is_active)
        return picked if picked is not None else app_acc


# Top-level windows a named-app snapshot lists. Menus, popups, and tooltips
# are not windows: they belong to the frame that owns them.
_TOPLEVEL_ROLES = frozenset({
    "frame", "window", "dialog", "alert", "file chooser", "color chooser",
})


def _application_of(node):
    """The AT-SPI application that owns ``node``, or None."""
    if node is None:
        return None
    if _role_name(node) == "application":
        return node
    app = _call_first(node, ("get_application", "getApplication"))
    if app is not None and _role_name(app) == "application":
        return app
    parent = _call_first(node, ("get_parent", "getParent"))
    if parent is not None and _role_name(parent) == "application":
        return parent
    return None


def _toplevel_of(node):
    """The showing-window ancestor of ``node``, or None at the application."""
    current = node
    for _ in range(32):
        if current is None:
            return None
        role = _role_name(current)
        if role in _TOPLEVEL_ROLES:
            return current
        if role == "application":
            return None
        current = _call_first(current, ("get_parent", "getParent"))
    return None


def _fresh_application_children(app) -> list:
    """Read top-level children without libatspi's stale application cache.

    A newly opened Qt window can be absent from the cached array indefinitely.
    Keep the count and indexed reads under the same cache mask; clearing cached
    properties alone allows reentrant cache events to restore the stale array.
    """
    def read_children():
        count = _child_count(app)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return []
        return [child for index in range(count)
                if (child := _child_at(app, index)) is not None]

    return _without_child_cache(app, read_children)


def _showing_chrome_frame_menus(frame) -> list:
    """Native menus in an unnamed Chromium popup frame, with real handles.

    The wrapper frame can lack SHOWING or usable geometry even while its
    menu is on screen. Snapshot the menu itself, not that empty wrapper.
    """
    if (_role_name(frame) != "frame" or _node_name(frame)
            or not _chromium_app(frame)):
        return []
    return [child for index in range(max(0, _child_count(frame)))
            if (child := _child_at(frame, index)) is not None
            if _role_name(child) in {"menu", "popup menu"}
            and _state_has(child, "SHOWING")]


def showing_toplevels(node) -> list:
    """Showing top-level windows of ``node``'s application, active first.

    One showing window is that window alone, so a single-window snapshot
    stays one tree. A window that is not SHOWING (minimized) is left out, as
    are menus and tooltips. A GTK combo's popup window is left out too: its
    items already sit under the combo, and listing it would replace the
    frame that owns the combo. When several windows are ACTIVE, the first in
    child order leads and the rest keep that order.
    """
    if node is None:
        return []
    app = _application_of(node)
    if app is None:
        return [node]
    from a11y_computer_use.drivers import _linux_system

    pid = pid_of(app)
    identity = _linux_system._identity_for_pid(pid, _linux_system._comm_for_pid(pid))
    if identity in _linux_system._PORTAL_BACKENDS:
        return [node]
    frames = []
    for child in _fresh_application_children(app):
        if _role_name(child) not in _TOPLEVEL_ROLES:
            continue
        if not _state_has(child, "SHOWING") and not _showing_chrome_frame_menus(child):
            continue
        if _combo_owning_popup(child) is not None:
            continue
        frames.append(child)
    if len(frames) <= 1:
        return frames or [node]
    active = [frame for frame in frames if _state_has(frame, "ACTIVE")]
    head = active[:1]
    if not head and _role_name(node) in _TOPLEVEL_ROLES and _state_has(node, "SHOWING"):
        # find_root already chose this window. Lead with it when AT-SPI has
        # not marked any frame ACTIVE.
        chosen = _frame_cache_key(node)
        head = [frame for frame in frames if _frame_cache_key(frame) == chosen][:1]
    head_keys = {_frame_cache_key(frame) for frame in head}
    return head + [frame for frame in frames if _frame_cache_key(frame) not in head_keys]


def snapshot_window_list(node) -> list:
    """Showing windows to snapshot, active first.

    A find bar, an omnibox popup, and Chrome's empty cached frame are
    showing siblings of the page. They are not windows. Merging one of
    them is the untitled group with no rows that stops ``scroll_to_find``
    before the wheel. A GTK dialog stays in the list. One real window is
    a one-item list, so the snapshot stays a single tree. Showing native
    Chromium menus are walked directly when their unnamed frame wrapper
    has no usable geometry.
    """
    if node is None:
        return []
    chrome = _chromium_app(node)
    frames = []
    for item in showing_toplevels(node):
        menus = _showing_chrome_frame_menus(item) if chrome else []
        if menus:
            frames.extend(menus)
            continue
        if _is_browser_bar_frame(item):
            continue
        if chrome and _sparse_untitled_chrome_frame(item):
            continue
        frames.append(item)
    return frames or [node]


def toplevel_raise_target(node):
    """``(active, title, pid, extents)`` for the window that owns ``node``.

    None when ``node`` is not inside a top-level window. ``active`` is
    STATE_ACTIVE on that window. ``extents`` is ``(x, y, width, height)`` in
    screen pixels, or None when the window has no box. The caller raises the
    matching X window before it acts, and leaves an already active window
    alone. Embedded unnamed chooser widgets use their enclosing window, so
    occlusion checks cannot mistake that window for a separate covering one.
    """
    top = _toplevel_of(node)
    if top is None and _qt_app(node):
        # A plain top-level QWidget is a filler, not a frame. Only accept
        # the application's direct child; embedded layout fillers are not
        # windows and must not supply guessed window geometry.
        for candidate in _ancestor_chain(node):
            if (_role_name(candidate) == "filler"
                    and _role_name(_parent_of(candidate)) == "application"):
                top = candidate
                break
    if top is None:
        return None
    if not _node_name(top) and (
        _role_name(top) in {"file chooser", "color chooser"}
        or (_role_name(top) in {"frame", "window"} and _qt_app(top))
    ):
        # GTK exposes embedded chooser widgets with top-level roles; Qt
        # exposes stacked/tab pages as unnamed windows inside QDialog.
        # Their box overlaps both the dialog and the blocked main window;
        # choosing by overlap can spend the whole activation wait on the
        # main window. Use the enclosing native dialog's title and bounds.
        enclosing = _toplevel_of(_parent_of(top))
        if enclosing is not None and _role_name(enclosing) in {
            "frame", "window", "dialog", "alert", "file chooser", "color chooser",
        }:
            top = enclosing
    title = str(_call_first(top, ("get_name", "getName"), default="") or "")
    position, size = _extents(top)
    extents = None
    if position is not None and size is not None:
        extents = (float(position[0]), float(position[1]), float(size[0]), float(size[1]))
    return (_state_has(top, "ACTIVE"), title, pid_of(top), extents)


def desktop_application_roots() -> DesktopApplications:
    """Every application under the AT-SPI desktop.

    A child whose name read times out and whose process is stopped is counted
    as silent and is not walked. A slow reply is a live app and stays in the
    list, as does a live process that missed this one read. Applications that
    answer are returned with the name the registry reported. An empty desktop
    is ``ready`` empty and ``silent`` 0.
    """
    Atspi = _atspi()
    desktop = _safe(lambda: Atspi.get_desktop(0))
    if desktop is None:
        return DesktopApplications([], 0)
    count = _call_first(desktop, ("get_child_count",), default=0) or 0
    ready: list[tuple[str, object]] = []
    silent = 0
    for index in range(int(count)):
        child = _call_first(desktop, ("get_child_at_index",), index)
        if child is None:
            continue
        name = str(_call_first(child, ("get_name",), default="") or "").strip()
        if call_went_unanswered() and process_is_stopped(pid_of(child)):
            silent += 1
            continue
        ready.append((name or "application", child))
    return DesktopApplications(ready, silent)


def _text_security(acc):
    """Read security metadata once; visibility is meaningful only for entries."""
    role = _role_name(acc)
    if role == "password text":
        return True, role, {}, None
    # State sets stay uncached across probes: masking can change after input.
    # Within this probe all three flags come from the same native reply.
    states = _call_first(acc, ("get_state_set",))
    if any(_state_flag(acc, name, drop_cache=False, states=states)
           for name in ("PASSWORD", "PROTECTED", "INVISIBLE")):
        return True, role, {}, None
    attrs = _get_attributes(acc)
    if any(str(attrs.get(key, "")).strip().lower() in {"true", "1"}
           for key in ("password", "protected")):
        return True, role, attrs, None
    # GTK 4 PasswordEntry can expose an entry wrapper and a password-text
    # child. Classify both sides before reading Text; the inner accessible
    # may be rebuilt between observation and a write.
    if (role in {"entry", "text"}
            and str(attrs.get("toolkit", "")).lower() == "gtk"
            and _toolkit_version(acc).split(".", 1)[0] != "3"):
        parent = _parent_of(acc)
        if parent is not None and _role_name(parent) == "password text":
            return True, role, attrs, None
        if role == "entry" and any(
            _role_name(_child_at(acc, index)) == "password text"
            for index in range(_child_count(acc))
        ):
            return True, role, attrs, None
    visibility = None
    if role in {"entry", "text"}:
        visibility = _call_first(acc, ("get_visibility",))
        if visibility is False or str(attrs.get("visibility", "")).strip().lower() in {"false", "0"}:
            return True, role, attrs, visibility
        if any(str(attrs.get(key, "")).strip() not in {"", "0"}
               for key in ("invisible-char", "invisible_char")):
            return True, role, attrs, visibility
        if (str(attrs.get("toolkit", "")).lower() == "gtk"
                and not _state_flag(acc, "MULTI_LINE", drop_cache=False, states=states)
                and _toolkit_version(acc).split(".", 1)[0] != "3"):
            # GTK 4 GetText leaks the buffer even for visibility=False.
            # GetStringAtOffset uses the rendered layout instead: the real
            # bridge returns bullets for masked entries and text for plain ones.
            # Do not infer masking from widget SHOWING/VISIBLE or text attributes.
            api = _atspi()
            rendered = _safe(lambda: api.Text.get_string_at_offset(
                acc, 0, api.TextGranularity.LINE,
            ))
            content = getattr(rendered, "content", None)
            if content == "":
                count = _safe(lambda: api.Text.get_character_count(acc))
                if isinstance(count, int) and not isinstance(count, bool) and count == 0:
                    return False, role, attrs, "unknown"
                return True, role, attrs, visibility
            if not isinstance(content, str) or _entry_text_masked(role, attrs, content):
                return True, role, attrs, visibility
            # A custom masking glyph need not be one of the known bullets.
            # Compare privately with the buffer; never publish a mismatch.
            count = _safe(lambda: api.Text.get_character_count(acc))
            buffer = _safe(lambda: api.Text.get_text(acc, 0, count)) if (
                isinstance(count, int) and not isinstance(count, bool) and count > 0
            ) else None
            if content != buffer:
                return True, role, attrs, visibility
            visibility = True
    return False, role, attrs, visibility


def is_secure(acc) -> bool:
    """Recognize password roles, protected states, and explicit masking.

    GTK 4 layout strings expose masking even when GetText and metadata do
    not. Empty GTK entries without masking metadata remain unknown until written.
    """
    return _text_security(acc)[0]


def text_visibility_unknown(acc) -> bool:
    """An empty GTK 4 entry with no real masking or visibility signal."""
    return _text_security(acc)[3] == "unknown"


class _PrivateTextCount(int):
    """A verified character count whose receipt and audit must stay redacted."""

    private_text_verified = True


def _write_unknown_entry(acc, text: str, *, insert: bool) -> bool:
    """Write an empty unknown entry once; expose only a verified boolean.

    The rendered layout may become masked after this write. Buffer reads here
    must never reach a snapshot, error detail, or action result.
    """
    api = _atspi()
    eti = _editable_iface(acc)
    if eti is None:
        return False
    if insert:
        method = getattr(eti, "insert_text", None) or getattr(eti, "insertText", None)
        if method is None:
            return False
        _call_first(eti, ("insert_text", "insertText"), 0, text,
                    _insert_length(method, text, acc), default=False)
    else:
        _call_first(eti, ("set_text_contents",), text, default=False)
    for attempt in range(_TEXT_CONFIRM_POLLS):
        _call_first(acc, ("clear_cache", "clearCache"))
        count = _safe(lambda: api.Text.get_character_count(acc))
        if count == len(text) and _safe(lambda: api.Text.get_text(acc, 0, count)) == text:
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    return False


def _focused_via_collection(root, Atspi, focused_state):
    """The FOCUSED descendant of ``root`` through org.a11y.atspi.Collection
    (one round-trip; served by the at-spi2-atk bridge for GTK3/Chromium/Firefox/
    Electron/LibreOffice). Returns (found, acc): found=False when the interface
    is absent or the call fails, so the caller falls back to the walk."""
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is None:
        return False, None
    try:
        states = Atspi.StateSet.new([focused_state])
        mt = Atspi.CollectionMatchType
        rule = Atspi.MatchRule.new(states, mt.ALL, {}, mt.NONE, [], mt.NONE, [], mt.NONE, False)
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 1, True)
    except Exception:  # noqa: BLE001 - GTK4/Qt expose no Collection; fall back to the walk
        return False, None
    return True, (hits[0] if hits else None)


def _focused_sheet_selection(table):
    """The focused selected cell of a spreadsheet table, or None.

    ``Selection.get_selected_child`` is one round-trip. Indexing the table
    is not: Calc's child count is 2**31 and the cells are row-major.
    """
    iface = _call_first(table, ("get_selection_iface", "get_selection"))
    if iface is None:
        return None
    child = _call_first(iface, ("get_selected_child", "getSelectedChild"), 0)
    if child is None:
        return None
    sset = _call_first(child, ("get_state_set",))
    if sset is None:
        return None
    try:
        focused = _atspi().StateType.FOCUSED
    except Exception:
        return None
    if not _safe(lambda state=sset: state.contains(focused), False):
        return None
    return child


def _focused_node(app: str, *, max_nodes: int = 400):
    """(focused accessible or None, truncated).

    truncated is True when the walk stopped before it could tell whether
    anything is focused. A Collection query that finds no focused node, a
    missing app, and a full walk that meets no focused node are
    ``(None, False)``.

    AT-SPI has no global "focused accessible" getter (focus arrives as
    events). The Collection interface answers in one round-trip where the
    toolkit exposes it; otherwise the active top-level frame is walked
    breadth-first, bounded by ``max_nodes``.
    """
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    if root is None:
        return None, False
    Atspi = _atspi()
    st = getattr(Atspi, "StateType", None)
    focused_state = getattr(st, "FOCUSED", None)
    if focused_state is None:
        return None, False
    found, acc = _focused_via_collection(root, Atspi, focused_state)
    if found:
        return acc, False
    queue = [root]
    seen = 0
    truncated = False
    while queue and seen < max_nodes:
        acc = queue.pop(0)
        seen += 1
        sset = _call_first(acc, ("get_state_set",))
        if sset is not None and _safe(lambda state=sset: state.contains(focused_state), False):
            return acc, False
        n = int(_call_first(acc, ("get_child_count",), default=0) or 0)
        # A Calc table reports 2**31 children. Indexing them materializes
        # cells and, past the first row, never reaches the focused cell.
        # The table or its selected cell is the focus target. A closed menu
        # is the same kind of skip: its items are in the tree while it is
        # shut, and walking them spends the node budget before the sheet.
        if _spreadsheet_table(acc) and n > _MAX_CHILDREN_FETCH:
            selected = _focused_sheet_selection(acc)
            if selected is not None:
                return selected, False
            continue
        role = _role_name(acc)
        if role in {"menu", "popup menu"} and not (
            _state_has(acc, "EXPANDED")
            or _state_has(acc, "FOCUSED")
            or _state_has(acc, "SELECTED")
        ):
            continue
        if n > _MAX_CHILDREN_FETCH:
            truncated = True
        for j in range(min(n, _MAX_CHILDREN_FETCH)):
            child = _call_first(acc, ("get_child_at_index",), j)
            if child is not None:
                queue.append(child)
    if queue or truncated:
        return None, True
    return None, False


def focused_secure(app: str, *, max_nodes: int = 400) -> bool | None:
    """Whether the keyboard-focused node of ``app``'s active window is a
    password field: True / False / None.

    True: the focused node is a password field. False: a focused non-secure
    node was found, or the whole frame was walked and nothing is focused, or the
    app is not on the bus (no signal, the same degradation as the macOS
    ``AXFocusedUIElement`` probe). None: the frame is larger than the walk bound
    (or a node had more than `_MAX_CHILDREN_FETCH` children) and no focused node
    was met, so focus is UNKNOWN; the caller must not type blind on None.

    A truncated LibreOffice walk is not unknown when the open cell editor or
    the selected sheet cell is in hand. The editor is a panel or group named
    ``Cell A1`` and a paragraph under it, not a password field. It is True
    only when that panel or its text child has the password-text role. A
    password the walk itself reaches still wins.

    This is the check `LinuxDriver.type_text` runs before the XTEST path,
    which types into whatever holds focus."""
    acc, truncated = _focused_node(app, max_nodes=max_nodes)
    if not truncated:
        if acc is None:
            return False
        return is_secure(acc)
    editor = _cell_editor_is_secure(app)
    if editor is not None:
        return editor
    cell = _selected_sheet_cell(app)
    if cell is not None:
        return is_secure(cell)
    return None


# Chrome UI fields whose text is not the web page. The address bar rewrites
# while suggestions settle. The find bar reopens with the previous query
# selected, so typing that query again leaves the same string.
_CHROME_REWRITE_NAMES = frozenset({"Address and search bar", "Find"})

# Pids whose find bar or omnibox popup has been in a snapshot, or whose find
# field had keyboard focus. libatspi keeps the child list from that. Closing
# the bar and Reload do not drop it, so later reads still walk the bar or an
# empty document. The next snapshot of that process clears the client cache
# once and uses the page frame. Clearing on every later snapshot walks the
# document again and blows the short-page scroll budget.
_BROWSER_BAR_PIDS: set[int] = set()
_BROWSER_BAR_CLEARED: set[int] = set()
# Pids whose find bar or omnibox is still the same opening. A later snapshot
# of that opening must not schedule another cache clear: clearing on every
# read walks an empty document and blows the scroll budget.
_BROWSER_BAR_OPEN: set[int] = set()


_FIND_ENTRY_ROLES = frozenset({
    "text", "entry", "password text", "edit bar", "paragraph",
})


def focused_find_field(acc) -> bool:
    """True when ``acc`` is a browser find field, not a page control or a cell.

    Chrome names that field ``Find`` and gives it an entry role with no HTML
    tag. A page input titled Find has a tag. A push button titled Find, a
    table cell, and a Swing editor are not this field. A find-in-page frame
    that itself holds focus counts too.
    """
    if acc is None:
        return False
    try:
        name = _node_name(acc) or ""
    except Exception:
        return False
    folded = name.lstrip().lower()
    if folded.startswith("find in page"):
        return True
    if name != "Find":
        return False
    try:
        attrs = _get_attributes(acc)
    except Exception:
        attrs = {}
    tag = str(attrs.get("tag") or attrs.get("html-tag") or "").strip()
    if tag:
        return False
    try:
        role = _role_name(acc)
    except Exception:
        return False
    if role in {"push button", "toggle button", "link", "menu item", "table cell", "cell"}:
        return False
    return role in _FIND_ENTRY_ROLES or role == "entry" or "text" in role


def app_supports_in_page_find(root) -> bool:
    """True for Chromium and Firefox, which open a find bar on Ctrl+F.

    Java, Swing, GTK, Qt, and a tree with no toolkit are false. A failed
    read is false, so the caller does not type into a table cell.
    """
    if root is None:
        return False
    try:
        return bool(_chromium_app(root) or _gecko_app(root))
    except Exception:
        return False


def snapshot_shows_browser_bar(elements) -> bool:
    """True when a snapshot still contains the find bar or an omnibox popup.

    The address bar itself is always present and is not this. A page button
    titled Find is not this either: only the find field, the close button,
    the find-in-page frame, and an omnibox URL count.
    """
    for el in elements:
        title = getattr(el, "title", None) or ""
        role = getattr(el, "role", None) or ""
        value = str(getattr(el, "value", None) or "")
        folded = title.lstrip().lower()
        if title == "Close find bar" or folded.startswith("find in page"):
            return True
        if title == "Find" and role in {"AXTextField", "AXSearchField", "AXTextArea"}:
            return True
        if "omnibox" in folded or "omnibox" in value.lower():
            return True
    return False


def note_browser_bar(root, elements) -> None:
    """Remember that ``root``'s process showed a find bar or omnibox popup.

    The first snapshot of an opening schedules one cache clear. Further
    snapshots of that same opening do not. The next opening, after the bar
    has been absent, schedules a clear again.
    """
    if root is None:
        return
    showing = snapshot_shows_browser_bar(elements)
    if not showing:
        if not _BROWSER_BAR_OPEN:
            return
        try:
            pid = pid_of(root)
        except Exception:
            return
        if pid:
            _BROWSER_BAR_OPEN.discard(int(pid))
        return
    try:
        pid = pid_of(root)
    except Exception:
        return
    if not pid:
        return
    pid = int(pid)
    if pid in _BROWSER_BAR_OPEN:
        return
    _BROWSER_BAR_PIDS.add(pid)
    # The bar is on screen again. The cache from the last clear is stale.
    _BROWSER_BAR_CLEARED.discard(pid)
    _BROWSER_BAR_OPEN.add(pid)


def _browser_bar_latched(root) -> bool:
    if not _BROWSER_BAR_PIDS or root is None:
        return False
    try:
        pid = pid_of(root)
    except Exception:
        return False
    return bool(pid and int(pid) in _BROWSER_BAR_PIDS)


def note_focused_find_bar(app: str | None) -> None:
    """Latch Chrome when keyboard focus is in the find field.

    ``scroll_to_find`` opens the bar, types, and closes it before the next
    snapshot, so that snapshot never contains the find field and would not
    otherwise remember the process. The address bar does not latch.
    """
    if not app:
        return
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        return
    if truncated or acc is None or not _chrome_rewrite_field(acc):
        return
    if (_node_name(acc) or "") != "Find":
        return
    try:
        pid = pid_of(acc)
    except Exception:
        return
    if pid:
        pid = int(pid)
        _BROWSER_BAR_PIDS.add(pid)
        _BROWSER_BAR_CLEARED.discard(pid)


def latch_chromium_find(app: str) -> bool:
    """Record Chrome's pid so the next snapshot drops the find-bar cache.

    ``scroll_to_find`` closes the bar before a snapshot can see the Find
    field, so the focus latch misses. The next walk is then an empty
    document or the find frame. True when a Chrome pid was recorded.
    """
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    if root is None:
        return False
    try:
        if not _chromium_app(root):
            return False
    except Exception:
        return False
    try:
        pid = pid_of(root)
    except Exception:
        return False
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    pid = int(pid)
    _BROWSER_BAR_PIDS.add(pid)
    _BROWSER_BAR_CLEARED.discard(pid)
    # This opening is already handled. A snapshot that still shows the
    # closing bar must not schedule a second clear.
    _BROWSER_BAR_OPEN.add(pid)
    return True


def _is_browser_bar_frame(node) -> bool:
    """True for the find-in-page frame or an omnibox popup, not the page window."""
    if node is None:
        return False
    name = (_node_name(node) or "").lstrip().lower()
    if name.startswith("find in page"):
        return True
    url = _doc_url(node) or ""
    return "omnibox" in url.lower()


def _clear_browser_bar_cache(node, depth: int = 0) -> None:
    """Drop libatspi's child cache on the window and its documents.

    The walk stops at a document, so the page's own nodes are fetched again
    by the snapshot instead of being cleared one by one.
    """
    if node is None or depth > 12:
        return
    role = _role_name(node)
    if depth == 0 or role in {
        "application", "frame", "window", "document web", "document frame", "internal frame",
    }:
        _call_first(node, ("clear_cache", "clearCache"))
    if depth and role in {"document web", "document frame"}:
        return
    count = _child_count(node)
    if count < 0:
        count = 0
    for index in range(min(int(count), 8)):
        _clear_browser_bar_cache(_child_at(node, index), depth + 1)


def _application_bar_frame(app):
    """A find-in-page or omnibox frame cached under ``app``, else None."""
    if app is None:
        return None
    count = _child_count(app)
    if count < 0:
        count = 0
    for index in range(min(int(count), 8)):
        frame = _child_at(app, index)
        if frame is not None and _is_browser_bar_frame(frame):
            return frame
    return None


def _sparse_untitled_chrome_frame(frame) -> bool:
    """True for an empty cached Chrome frame, not a real window.

    Beside the page, Chrome keeps untitled frames whose only child is an
    empty panel. After find-in-page that frame can be the active root.
    Its snapshot is an untitled group with no rows, and ``scroll_to_find``
    then stops before the wheel. A named window, the open find bar, and a
    frame that already holds a document are not this.
    """
    if frame is None or _is_browser_bar_frame(frame):
        return False
    if _role_name(frame) in {"menu", "popup menu"}:
        return False
    if (_node_name(frame) or "").strip():
        return False
    try:
        count = _child_count(frame)
    except Exception:
        return False
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > 2:
        return False
    for index in range(int(count)):
        child = _child_at(frame, index)
        if child is None:
            continue
        role = _role_name(child)
        if role in {"document web", "document frame", "internal frame"}:
            return False
        if (_node_name(child) or "").strip():
            return False
        try:
            kids = _child_count(child)
        except Exception:
            kids = 0
        if isinstance(kids, int) and not isinstance(kids, bool) and kids > 2:
            return False
    return True


def _chrome_page_window(app, root):
    """The named page window when ``root`` is an empty untitled frame.

    ``root`` is unchanged when it already has a title, or when no titled
    sibling exists. A showing titled window wins over one that is not.
    """
    if app is None or root is None or not _sparse_untitled_chrome_frame(root):
        return root
    count = _child_count(app)
    if count < 0:
        count = 0
    named = []
    for index in range(min(int(count), 8)):
        frame = _child_at(app, index)
        if frame is None or _is_browser_bar_frame(frame):
            continue
        if _role_name(frame) == "alert":
            continue
        if not (_node_name(frame) or "").strip():
            continue
        named.append(frame)
    if not named:
        return root
    for frame in named:
        if _state_has(frame, "ACTIVE") or _state_has(frame, "SHOWING"):
            return frame
    return named[0]


def chromium_snapshot_root(root):
    """Page frame for a Chrome snapshot after the find bar or omnibox was open.

    ``root`` is unchanged until that bar has appeared, or a find-in-page or
    omnibox frame is still cached beside the page window. After that, the
    client cache on the application is dropped, and a leftover find-in-page
    or omnibox frame is not the root. The frame that holds the selected tab
    is. Reload then reads the new document instead of the cached bar. The
    open bar itself stays the root the first time it is active, so Find and
    Close find bar stay in that snapshot. An empty untitled frame cached
    beside the page is not the root either: that walk is an untitled group
    with no rows, and ``scroll_to_find`` stops before the wheel.
    """
    if root is None or not _chromium_app(root):
        return root
    app = _call_first(root, ("get_application", "getApplication")) or root
    latched = _browser_bar_latched(root)
    if not latched:
        # This snapshot is the open bar. Show it. The elements record the pid.
        if _is_browser_bar_frame(root):
            return root
        # scroll_to_find closes the bar before the snapshot, so the page
        # window is the root and the bar frame is only a cached sibling.
        # The document walk is then the empty cached tree.
        if _application_bar_frame(app) is None:
            return _chrome_page_window(app, root)
    else:
        try:
            pid = pid_of(root)
        except Exception:
            pid = None
        # One clear per latch. A bar frame that is still cached asks for
        # another, which covers Reload. Further snapshots keep the fresh tree.
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid in _BROWSER_BAR_CLEARED
            and _application_bar_frame(app) is None
            and not _is_browser_bar_frame(root)
        ):
            return _chrome_page_window(app, root)
    _clear_browser_bar_cache(app)
    try:
        pid = pid_of(root)
    except Exception:
        pid = None
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        _BROWSER_BAR_CLEARED.add(int(pid))
    if not latched or not _is_browser_bar_frame(root):
        return _chrome_page_window(app, root)
    count = _child_count(app)
    if count < 0:
        count = 0
    pages = []
    for index in range(min(int(count), 8)):
        frame = _child_at(app, index)
        if frame is None or _is_browser_bar_frame(frame):
            continue
        if _role_name(frame) == "alert":
            continue
        # An empty cached frame can be ACTIVE. It is not the page.
        if _sparse_untitled_chrome_frame(frame):
            continue
        pages.append(frame)
    for frame in pages:
        if _state_has(frame, "ACTIVE"):
            return frame
    for frame in pages:
        if _selected_tab_name(frame):
            return frame
    return pages[0] if pages else _chrome_page_window(app, root)


def _chrome_rewrite_field(acc) -> bool:
    """True for the Chrome address bar or find bar, not a web input."""
    try:
        if not _chromium_app(acc):
            return False
    except Exception:
        return False
    if _get_attributes(acc).get("tag"):
        return False
    return _node_name(acc) in _CHROME_REWRITE_NAMES


def focused_chrome_rewrite(app: str, *, max_nodes: int = 400):
    """The focused address bar or find bar, else None."""
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
    except Exception:
        return None
    if truncated or acc is None or not _chrome_rewrite_field(acc):
        return None
    return acc


def _focused_contenteditable(app: str, *, max_nodes: int = 400):
    """The focused node when it is a Chrome contenteditable, else None.

    The XTEST type path uses this to decide whether a late AT-SPI read
    should be polled. An input, a textarea, and a GTK field are None.
    """
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
    except Exception:
        return None
    if truncated or acc is None or not _chromium_contenteditable(acc):
        return None
    return acc


def chromium_single_line_entry(acc) -> bool:
    """Whether native metadata identifies a Chromium single-line entry."""
    if not _chromium_app(acc) or _role_name(acc) not in {"entry", "spin button"}:
        return False
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or "").lower()
    return tag in {"", "input"} and not _reports_multiline(acc, attrs)


def focused_chromium_entry(app: str, *, max_nodes: int = 400):
    """Find the focused entry when Chrome also marks its container focused.

    Only native focused states identify the target; ambiguous entries are
    refused by returning None. Focused web documents are resolved across the
    owning window because Electron can expose multiple focused sibling views.
    """
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
        if truncated or acc is None or not _chromium_app(acc):
            return None
        # Electron WebContentsViews are sibling documents. Both documents
        # can report FOCUSED, so the first Collection hit need not contain
        # the keyboard target.
        if _role_name(acc) == "document web":
            from a11y_computer_use.schema import Scope

            root = find_root(app, Scope.WINDOW)
            if root is None:
                return None
            hits = _focused_accessibles(root, limit=max_nodes + 1)
            if hits:
                if len(hits) > max_nodes:
                    return None
                entries = [
                    node for node in hits
                    if chromium_single_line_entry(node) and _state_has(node, "FOCUSED")
                ]
                return entries[0] if len(entries) == 1 else None
            acc = root
        queue = [acc]
        entries = []
        seen = 0
        while queue and seen < max_nodes:
            node = queue.pop(0)
            seen += 1
            if chromium_single_line_entry(node) and _state_has(node, "FOCUSED"):
                entries.append(node)
            n = int(_call_first(node, ("get_child_count",), default=0) or 0)
            if n > max_nodes - seen:
                return None
            for index in range(n):
                child = _call_first(node, ("get_child_at_index",), index)
                if child is not None:
                    queue.append(child)
        if queue or len(entries) != 1:
            return None
        return entries[0]
    except Exception:
        return None


def math_command_editable(acc) -> bool:
    """An editable command paragraph in a LibreOffice Math window."""
    if (
        acc is None or _role_name(acc) != "paragraph"
        or not _state_has(acc, "EDITABLE") or _state_has(acc, "READ_ONLY")
        or _editable_iface(acc) is None or not _libreoffice_accessible(acc)
    ):
        return False
    parent = _parent_of(acc)
    if parent is None or _role_name(parent) != "panel" or not _state_has(parent, "EDITABLE"):
        return False
    math_identity = False
    frame = None
    for node in _ancestor_nodes(parent):
        role = _role_name(node)
        if role == "dialog":
            return False
        attrs = _get_attributes(node)
        services = str(attrs.get("service-name") or attrs.get("service") or "").split()
        math_identity |= (
            role in {"math", "document math"}
            or "com.sun.star.formula.FormulaProperties" in services
            or (role == "application" and _node_name(node).casefold() == "libreoffice math")
        )
        if role == "frame":
            frame = node
        if role == "application":
            break
    if frame is None:
        return False
    return math_identity or bool(re.search(r" [—–-] LibreOffice Math$", _node_name(frame)))


def _focused_math_command(acc, *, max_nodes: int = 400):
    """Resolve a focused Math panel to its unambiguous focused paragraph."""
    if math_command_editable(acc):
        return acc
    if acc is None or _role_name(acc) != "panel" or not _state_has(acc, "EDITABLE"):
        return None
    count = _child_count(acc)
    if count > max_nodes:
        return None
    matches = []
    for index in range(count):
        child = _call_first(acc, ("get_child_at_index",), index)
        if child is not None and _state_has(child, "FOCUSED") and math_command_editable(child):
            matches.append(child)
    return matches[0] if len(matches) == 1 else None


def _focused_office_paragraph(app: str, *, max_nodes: int):
    """Unique focused editable paragraph in the active window, or None.

    Math leaves FOCUSED on an Elements icon and the command panel after
    activation. Neither is the keyboard destination; its paragraph is.
    An incomplete or ambiguous walk cannot establish a read-back target.
    """
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    queue = [root] if root is not None else []
    candidate = None
    seen = 0
    while queue and seen < max_nodes:
        node = queue.pop(0)
        seen += 1
        role = _role_name(node)
        if role in {"menu", "popup menu"} and not (
            _state_has(node, "EXPANDED")
            or _state_has(node, "FOCUSED")
            or _state_has(node, "SELECTED")
        ):
            continue
        if (
            role == "paragraph"
            and _state_has(node, "FOCUSED")
            and _state_has(node, "EDITABLE")
            and _state_has(node, "SHOWING")
            and not is_secure(node)
        ):
            if candidate is not None:
                return None
            candidate = node
        count = int(_call_first(node, ("get_child_count",), default=0) or 0)
        if count > max_nodes - seen - len(queue):
            return None
        for index in range(count):
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                queue.append(child)
    return None if queue else candidate


def focused_text(app: str, *, max_nodes: int = 400) -> str | None:
    """Text of the focused node, or None when that text cannot be read.

    A terminal and a canvas that expose Text are readable even when they
    have no EditableText. None means a keystroke type has nothing to compare
    against and the caller may report the count it sent. An unreachable bus
    is None, not an error.
    """
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
    except Exception:
        return None
    if truncated or acc is None:
        return None
    if _role_name(acc) in {"icon", "panel"} and _libreoffice_accessible(acc):
        acc = _focused_office_paragraph(app, max_nodes=max_nodes)
        if acc is None:
            return None
    if _chromium_app(acc):
        entry = focused_chromium_entry(app, max_nodes=max_nodes)
        if entry is not None:
            acc = entry
    # GTK can return an empty string from Text.get_text even for a drawing
    # area without Text. That is no read-back, not a readable empty field.
    getter = getattr(acc, "get_text_iface", None)
    if callable(getter) and _safe(lambda: getter()) is None:
        return None
    # A PDF page's Text interface is the document, not a field. Evince
    # leaves that page focused while Ctrl+F types into the find bar. Using
    # the page as the read-back makes the type look like a mismatch and
    # scroll_to_find never confirms the hit.
    try:
        if _role_name(acc) == "page":
            return None
    except Exception:
        return None
    try:
        return _readable_text(acc)
    except Exception:
        return None


def _readable_text(acc) -> str | None:
    """Text a type read-back compares.

    NBSP is a space. Each U+FFFC is the embedded child's text, so a
    contenteditable whose paragraphs are object characters still contains the
    words that were typed into them. A choice control is left as the snapshot
    read it: expanding it would list every option.
    """
    raw = _full_text(acc)
    if raw is None:
        return None
    if _OBJECT_REPLACEMENT in raw:
        role_name = _role_name(acc)
        role = _ROLE.get(role_name, "AXGroup")
        if not _is_choice_role(role, role_name):
            expanded = _expand_embedded(acc, raw)
            if expanded:
                raw = expanded
    return raw.replace("\u00a0", " ")


def _field_text_for_type(text: str | None, typed: str) -> str | None:
    """The field text a type read-back compares.

    NBSP is a space: Chrome stores the edge spaces of a contenteditable as
    U+00A0. One trailing newline is the empty paragraph's ``<br>`` and is
    not part of the value, unless the typed text itself ends in a newline.
    Interior spaces stay, so a missing space is still a mismatch.
    """
    if text is None:
        return None
    normalized = text.replace("\u00a0", " ")
    if normalized.endswith("\n") and not (typed or "").replace("\u00a0", " ").endswith("\n"):
        normalized = normalized[:-1]
    return normalized


def _type_needles(text: str, *, chrome: bool) -> tuple[str, ...]:
    """Strings that count as ``text`` having landed.

    NBSP is already a space in the caller. Chrome's contenteditable drops a
    trailing space (the edge one is not kept once the read settles). The
    needle without those trailing spaces counts. An interior space is part
    of the needle, so ``a  b`` does not match ``a b``. A string of only
    spaces is not trimmed down to empty.
    """
    needle = _norm_nbsp(text) or ""
    if not chrome or not needle:
        return (needle,)
    trimmed = needle.rstrip(" ")
    if not trimmed or trimmed == needle:
        return (needle,)
    return (needle, trimmed)


def _expansion_starts(acc, raw: str) -> tuple[str, list[int]]:
    """Expanded text and the expanded index of each raw character.

    ``starts[i]`` is where raw character ``i`` begins. The last entry is the
    length, so an exclusive end offset has a place to land. A U+FFFC begins
    at the child text that replaces it. NBSP is a space, matching the
    read-back. No embedded objects leaves the raw string unchanged.
    """
    if _OBJECT_REPLACEMENT not in raw:
        text = raw.replace("\u00a0", " ")
        return text, list(range(len(text) + 1))
    targets = _embedded_targets(acc, raw)
    parts = raw.split(_OBJECT_REPLACEMENT)
    surrounding = "".join(parts)
    out: list[str] = []
    starts: list[int] = []
    built = 0
    obj_i = 0
    for part_index, part in enumerate(parts):
        for ch in part:
            starts.append(built)
            piece = " " if ch == "\u00a0" else ch
            out.append(piece)
            built += 1
        if part_index >= len(parts) - 1:
            break
        obj = targets[obj_i] if obj_i < len(targets) else None
        obj_i += 1
        piece = _with_block_boundary(
            "".join(out), obj, _object_text(obj, 1, surrounding).replace("\u00a0", " "),
        )
        starts.append(built)
        out.append(piece)
        built += len(piece)
    starts.append(built)
    return "".join(out), starts


def _span_in_contribution(
    readable: str, span: tuple[int, int], contributed: str,
) -> tuple[int, int] | None:
    """``span`` moved from ``readable`` into ``contributed``, or None.

    The embed's read-back and the slice spliced into the parent can differ by
    a stripped edge. Offsets that fall outside the contribution are None.
    """
    start, end = span
    read_n = readable.replace("\u00a0", " ")
    contrib = contributed.replace("\u00a0", " ")
    if start < 0 or end <= start or end > len(read_n):
        return None
    lead = 0
    if read_n == contrib:
        lead = 0
    elif contrib and read_n.strip() == contrib:
        lead = len(read_n) - len(read_n.lstrip())
    elif contrib and contrib in read_n:
        lead = read_n.find(contrib)
    else:
        return None
    placed_start = start - lead
    placed_end = end - lead
    if placed_start < 0 or placed_end > len(contrib) or placed_end <= placed_start:
        return None
    return placed_start, placed_end


def _descendant_text_span(acc, readable: str, depth: int) -> tuple[int, int] | None:
    """Selection on a descendant, expressed in ``readable``.

    A paragraph can leave its own Text selection empty while the static text
    that holds the word reports one. The child's offsets are placed where
    that child's text sits in ``readable``.
    """
    if depth > 6 or not readable:
        return None
    text = readable.replace("\u00a0", " ")
    cursor = 0
    spans: list[tuple[int, int]] = []
    for index in range(min(_child_count(acc), 32)):
        child = _child_at(acc, index)
        if child is None:
            continue
        try:
            child_raw = _full_text(child)
        except Exception:
            child_raw = None
        if isinstance(child_raw, str) and child_raw and _OBJECT_REPLACEMENT not in child_raw:
            child_text = child_raw.replace("\u00a0", " ")
        else:
            try:
                child_text = _readable_text(child) or ""
            except Exception:
                child_text = ""
        if not child_text:
            continue
        at = text.find(child_text, cursor)
        if at < 0:
            continue
        local = _selection_span(child, child_text, depth + 1)
        if local is None:
            local = _descendant_text_span(child, child_text, depth + 1)
        if local is not None and local[1] <= len(child_text):
            spans.append((at + local[0], at + local[1]))
        cursor = at + len(child_text)
    if not spans:
        return None
    return spans[0][0], spans[-1][1]


def _embed_inner_span(obj, contributed: str, depth: int) -> tuple[int, int] | None:
    """Selection inside one embedded paragraph, relative to ``contributed``.

    None keeps the whole embed. Chrome's contenteditable parent selects the
    paragraph object, and the paragraph (or the static text inside it)
    selects the word. Typing replaces the word.
    """
    if obj is None or not contributed or depth > 6:
        return None
    try:
        readable = _readable_text(obj)
    except Exception:
        return None
    if not isinstance(readable, str) or not readable:
        return None
    span = _selection_span(obj, readable, depth + 1)
    if span is None:
        span = _descendant_text_span(obj, readable, depth + 1)
    if span is None:
        return None
    return _span_in_contribution(readable, span, contributed)


def _refine_embedded_selection(
    acc, raw: str, start: int, end: int, expanded: str, starts: list[int], depth: int,
) -> tuple[int, int] | None:
    """The expanded span, shrunk to a word selected inside a U+FFFC.

    A selection that also covers ordinary characters keeps the object
    mapping. An embed with no inner selection stays the whole embed, which
    is a selected paragraph. The word's range is what a selected word
    inside that paragraph reports.
    """
    targets = _embedded_targets(acc, raw)
    obj_i = 0
    pieces: list[tuple[int, int]] = []
    saw_object = False
    for index, ch in enumerate(raw):
        if ch != _OBJECT_REPLACEMENT:
            if start <= index < end:
                return None
            continue
        contrib_s = starts[index] if index < len(starts) else len(expanded)
        contrib_e = starts[index + 1] if index + 1 < len(starts) else len(expanded)
        if start <= index < end:
            saw_object = True
            contributed = expanded[contrib_s:contrib_e]
            inner = _embed_inner_span(
                targets[obj_i] if obj_i < len(targets) else None, contributed, depth,
            )
            if inner is None:
                pieces.append((contrib_s, contrib_e))
            else:
                pieces.append((contrib_s + inner[0], contrib_s + inner[1]))
        obj_i += 1
    if not saw_object or not pieces:
        return None
    refined_start, refined_end = pieces[0][0], pieces[-1][1]
    if refined_end <= refined_start:
        return None
    return refined_start, refined_end


def _selection_span(acc, readable: str | None, _depth: int = 0) -> tuple[int, int] | None:
    """Active ``[start, end)`` in ``readable``, or None.

    The Text interface counts characters in its own string. A contenteditable
    parent is often one U+FFFC per paragraph, while the read-back is the
    children's words. The span is translated into that read-back. When that
    object is selected only because the caret is inside it, the paragraph's
    own selection is the word, and that word is the span. A collapsed
    caret, a missing selection, and offsets that are not in ``readable`` are
    None, and the caller keeps the append check.
    """
    if _depth > 6 or not isinstance(readable, str) or not readable:
        return None
    try:
        raw = _full_text(acc)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    try:
        Atspi = _atspi()
        count = _safe(lambda: Atspi.Text.get_character_count(acc))
    except Exception:
        count = None
    nchars = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else len(raw)
    try:
        _caret, start, end = _caret_and_selection(acc, nchars)
    except Exception:
        return None
    if end <= start:
        return None
    readable_n = readable.replace("\u00a0", " ")
    if _OBJECT_REPLACEMENT not in raw:
        raw_n = raw.replace("\u00a0", " ")
        if end <= len(readable_n) and (
            nchars == len(readable_n)
            or raw_n == readable_n
            or raw_n.rstrip("\n") == readable_n
            or readable_n.rstrip("\n") == raw_n
        ):
            return start, end
        return None
    expanded, starts = _expansion_starts(acc, raw)
    if start >= len(starts):
        return None
    r_start = starts[start]
    r_end = starts[end] if end < len(starts) else len(expanded)
    try:
        refined = _refine_embedded_selection(acc, raw, start, end, expanded, starts, _depth)
    except Exception:
        refined = None
    if refined is not None:
        r_start, r_end = refined
    lead = 0
    if expanded != readable_n:
        stripped = expanded.strip()
        if stripped != readable_n and stripped.rstrip("\n") != readable_n.rstrip("\n"):
            if nchars == len(readable_n) and end <= len(readable_n):
                return start, end
            return None
        found = expanded.find(stripped) if stripped else 0
        lead = found if found > 0 else 0
    r_start -= lead
    r_end -= lead
    if r_start < 0 or r_end < r_start or r_end > len(readable_n):
        return None
    return r_start, r_end


def _selection_replaced(
    before_n: str,
    after_n: str,
    needles: tuple[str, ...],
    start: int,
    end: int,
    typed: str,
) -> bool:
    """True when ``after_n`` is ``before_n`` with ``[start, end)`` replaced."""
    from a11y_computer_use.outcome import replaced_text

    if end <= start or end > len(before_n):
        return False
    from a11y_computer_use.outcome import typed_breaks_match

    for needle in needles:
        if not needle:
            continue
        expected = replaced_text(before_n, needle, start, end)
        if expected is None:
            continue
        if typed_breaks_match(_field_text_for_type(expected, typed), after_n):
            return True
    return False


def _matched_replacement(
    before: str | None,
    after: str | None,
    text: str,
    selection: tuple[int, int] | None,
    *,
    chrome: bool = False,
) -> str | None:
    """The final field text when ``after`` is the selection replacement.

    None when there is no selection or ``after`` is some other edit. The
    outcome check compares the snapshot with this string.
    """
    if not selection or len(selection) != 2:
        return None
    before_n = _field_text_for_type(before, text)
    after_n = _field_text_for_type(after, text)
    if before_n is None or after_n is None:
        return None
    start, end = selection
    if not _selection_replaced(before_n, after_n, _type_needles(text, chrome=chrome), start, end, text):
        return None
    from a11y_computer_use.outcome import replaced_text, typed_breaks_match

    for needle in _type_needles(text, chrome=chrome):
        expected = replaced_text(before_n, needle, start, end)
        shown = _field_text_for_type(expected, text) if expected is not None else None
        if shown is not None and typed_breaks_match(shown, after_n):
            return shown
    return None


def _typed_visible(
    before: str | None,
    after: str | None,
    text: str,
    *,
    chrome: bool = False,
    selection: tuple[int, int] | None = None,
) -> bool:
    """Whether ``text`` showed up in the focused text.

    A readable field that still shows the pre-type text, or that shows the
    case-inverted string, does not count. With an active selection the
    expected result is the previous text with that range replaced, and the
    final text has to be that string. A suffix or an insertion is the check
    when the caret is collapsed. NBSP compares as a space, and one trailing
    contenteditable newline is not part of the value. A newline, CRLF,
    U+2028, U+2029, and a paragraph or block boundary are the same break.
    A missing or extra character is not. ``chrome`` also accepts the typed
    text with its trailing spaces removed, which is the string Chrome keeps.
    A field that settled without the characters is not a match. A field
    whose whole text is already exactly the typed string is a match:
    Chrome's find bar reopens with that query selected, and typing it again
    does not change the string.
    """
    from a11y_computer_use.outcome import normalize_typed_breaks

    before_n = _field_text_for_type(before, text)
    after_n = _field_text_for_type(after, text)
    needles = _type_needles(text, chrome=chrome)
    after_b = None if after_n is None else normalize_typed_breaks(after_n)
    before_b = None if before_n is None else normalize_typed_breaks(before_n)
    needles_b = tuple(normalize_typed_breaks(needle) for needle in needles)
    if after_b is None or not any(needle and needle in after_b for needle in needles_b):
        return False
    if (
        selection is not None
        and len(selection) == 2
        and before_n is not None
        and int(selection[1]) > int(selection[0])
        and int(selection[1]) <= len(before_n)
    ):
        return _selection_replaced(
            before_n, after_n, needles, int(selection[0]), int(selection[1]), text,
        )
    if before_n is None:
        return True
    if after_b == before_b:
        # The field already showed exactly the typed text. Chrome's find bar
        # reopens with that query selected, and typing it again leaves the
        # same string. That is the text landing. A longer field that did not
        # change still does not count: the needle is only a piece of it.
        return any(needle and after_b == needle for needle in needles_b)
    for needle in needles_b:
        if needle and (after_b.endswith(needle) or after_b == ((before_b or "") + needle)):
            return True
    return (before_b or "") in after_b or len(after_b) > len(before_b or "")


# Chrome publishes a contenteditable's new characters a beat after the keys.
# Sixteen reads, 50ms apart, cover that gap. A value that changed and then
# stayed wrong is settled: more waiting would not turn it into the request.
_TYPE_SETTLE_POLLS = 16
_TYPE_SETTLE_PAUSE_S = 0.05
_TYPE_SETTLE_STABLE = 2


def _poll_typed_text(
    read,
    before: str | None,
    text: str,
    *,
    chrome: bool = False,
    selection: tuple[int, int] | None = None,
) -> str | None:
    """Poll ``read`` until ``text`` is visible or the field settles.

    ``read`` returns the current readable text. The first hit wins. A read
    that is still the pre-type text is not settled: the update can still be
    in flight. A read that changed to something else and stays there is
    settled, and the caller reports ``text_mismatch`` when the characters
    are absent. ``chrome`` uses Chrome's trailing-space comparison. The
    last read is what the caller shows.
    """
    last: str | None = None
    stable = 0
    seen: str | None = None
    before_n = _field_text_for_type(before, text)
    for attempt in range(_TYPE_SETTLE_POLLS):
        seen = read()
        if _typed_visible(before, seen, text, chrome=chrome, selection=selection):
            return seen
        norm = _field_text_for_type(seen, text)
        if norm is not None and norm == last and norm != before_n:
            stable += 1
            if stable >= _TYPE_SETTLE_STABLE:
                return seen
        else:
            stable = 0
        last = norm
        if attempt + 1 < _TYPE_SETTLE_POLLS:
            time.sleep(_TYPE_SETTLE_PAUSE_S)
    return seen


def opaque_office_toolbar_focus(app: str) -> bool:
    """Identify unreadable embedded toolbar controls, including Calc's Name Box.

    Missing or truncated focus discovery is not evidence of this shape.
    These keystrokes must not be verified as selected-cell edits.
    """
    if not libreoffice_app(app):
        return False
    try:
        acc, truncated = _focused_node(app)
        if truncated or acc is None or _role_name(acc) not in {"panel", "combo box"}:
            return False
        if is_secure(acc):
            return False
        interfaces = _call_first(acc, ("get_interfaces",))
        has_text = interfaces is None or any(
            str(interface).rsplit(".", 1)[-1] in {"Text", "EditableText"}
            for interface in interfaces
        )
        # libatspi may return an empty sentinel even without a Text interface.
        if has_text and _readable_text(acc) is not None:
            return False
        for _ in range(12):
            acc = _parent_of(acc)
            if acc is None:
                return False
            role = _role_name(acc)
            if role == "tool bar":
                return True
            if role not in {"panel", "combo box", "filler"}:
                return False
    except Exception:
        return False
    return False


def focused_editable(app: str, *, max_nodes: int = 400):
    """The focused node when `type` can insert into it, else None.

    A password field is returned so the caller can refuse it before any
    write. None means there is no focused editable: nothing is focused, the
    focused node has no EditableText, the walk could not find focus, or
    PyGObject is not installed. The caller then uses keystrokes. An Impress
    or Draw frame is the exception: the focused container has no
    EditableText, and the focused or caret paragraph inside it is returned.
    The same node `insert_text` accepts, so a coordinate click and a ref
    click share that helper. Math command panels similarly resolve their
    single focused editable command paragraph before testing the interface.
    """
    try:
        acc, truncated = _focused_node(app, max_nodes=max_nodes)
    except ImportError:
        return None
    if truncated or acc is None:
        return None
    if is_secure(acc):
        return acc
    command = _focused_math_command(acc, max_nodes=max_nodes)
    if command is not None:
        return command
    if _editable_iface(acc) is None:
        # Impress and Draw focus the presentation or the text frame. The
        # paragraph being edited is below that container and is the node
        # insert_text can write.
        paragraph = _drawing_edit_paragraph(acc)
        if paragraph is None:
            return None
        return paragraph
    return acc


def gtk_toggle_cell(acc) -> bool:
    """GTK renderer toggle cells need pointer input, not row selection."""
    return (
        _toolkit_name(acc).lower() == "gtk"
        and not _gtk4_app(acc)
        and _role_name(acc) == "table cell"
        and "toggle" in _raw_action_names(acc)
    )


def checked_state(acc) -> bool:
    """Read the cell itself, even when unchecked GTK cells omit CHECKABLE."""
    return _state_has(acc, "CHECKED")


def checked_state_flipped(acc, before: bool) -> bool:
    """Require a stable checked change, independent of row selection."""
    for attempt in range(8):
        if checked_state(acc) != before:
            time.sleep(0.04)
            if checked_state(acc) != before:
                return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def qt_quick_checkable(acc) -> bool:
    """Qt Quick checkables expose Toggle plus a nonfunctional Press action."""
    return (
        _qt_app(acc)
        and _role_name(acc) in {"check box", "radio button", "toggle button"}
        and set(_raw_action_names(acc)) - {"setfocus"} == {"toggle", "press"}
    )


def okular_toc_toggle_cell(acc) -> bool:
    """Identify Okular tree cells whose Toggle only changes selection."""
    return (
        _role_name(acc) == "table cell"
        and _qt_app(acc)
        and _application_name(acc).casefold() == "okular"
        and set(_raw_action_names(acc)) - {"setfocus"} == {"toggle"}
        and any(_role_name(parent) == "tree" for parent in _ancestor_nodes(acc))
    )


def qt_toggle_only_control(acc) -> bool:
    """Qt buttons and list items whose only activating action is Toggle.

    Qt's Toggle changes checked/selected state without emitting the user
    click signals. SetFocus is harmless here, but a real Press/Click or any
    other activating action keeps the existing AT-SPI path. Table cells
    and tree items keep their separate selection handling.
    """
    if not _qt_app(acc) or _role_name(acc) not in {
        "check box", "radio button", "toggle button", "push button", "button",
        "list item", "list box item",
    }:
        return False
    action = _action_iface(acc)
    if action is None:
        return False
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    activating = set()
    for index in range(int(count)):
        raw = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw in _PRESS_ACTION_NAMES or raw in _PICK_ACTION_NAMES:
            activating.add(raw)
    return activating == {"toggle"}


def gtk4_tree_expander(acc) -> bool:
    """GTK 4 expandable buttons with native list-item expansion actions."""
    return (
        _role_name(acc) in {"button", "push button"}
        and _state_has(acc, "EXPANDABLE")
        and _gtk4_app(acc)
        and bool(set(_raw_action_names(acc)) & {
            "listitem.toggle-expand",
            "listitem.collapse" if _state_has(acc, "EXPANDED") else "listitem.expand",
        })
    )


def click_gtk4_tree_expander(acc) -> bool:
    """Send one native expansion click; the caller verifies the live state."""
    names = _raw_action_names(acc)
    name = "listitem.toggle-expand"
    if name not in names:
        name = "listitem.collapse" if _state_has(acc, "EXPANDED") else "listitem.expand"
    return _do_action_named(acc, frozenset({name}))


def qt_menu_button(acc) -> bool:
    """Qt split/menu tool button whose menu action is not a main press."""
    return _role_name(acc) == "push button menu" and _qt_app(acc)


_MAIN_BUTTON_ACTIONS = frozenset({"click", "press", "activate"})


def qt_menu_button_without_press(acc) -> bool:
    """True when Qt provides no explicit main-button activation action."""
    return qt_menu_button(acc) and not (_MAIN_BUTTON_ACTIONS & set(_raw_action_names(acc)))


def do_press(acc, *, on_dispatch=None) -> bool:
    """Perform an activating AT-SPI action (True on success).

    ``on_dispatch`` runs immediately before each native action attempt, even
    when its reply fails. No callback means no action was sent.
    """
    action = _action_iface(acc)
    if action is None:
        return False
    n = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    allowed = _MAIN_BUTTON_ACTIONS if qt_menu_button(acc) else _PRESS_ACTION_NAMES | _PICK_ACTION_NAMES
    for i in range(int(n)):
        raw = (_call_first(action, ("get_action_name", "get_name"), i, default="") or "").lower()
        if raw in allowed:
            if on_dispatch is not None:
                on_dispatch()
            if _call_first(action, ("do_action", "doAction"), i, default=False):
                return True
    return False


# Window titles of a GTK file chooser. A bare "Open" is a document title often
# enough that Return in that window must stay a key.
_FILE_CHOOSER_TITLES = frozenset({
    "open file",
    "save file",
    "save as",
    "select file",
    "select a file",
    "choose a file",
    "open document",
    "save document",
})
_FILE_CHOOSER_ACCEPT = frozenset({"open", "save", "ok", "select"})
_FILE_CHOOSER_BUTTON_ROLES = frozenset({"push button", "button"})
_FILE_CHOOSER_ENTRY_ROLES = frozenset({
    "entry", "text", "editable text", "combo box", "combo-box", "text entry",
})
_FILE_CHOOSER_ROW_ROLES = frozenset({"table cell", "list item", "tree item", "table row"})
_FILE_CHOOSER_TABLE_ROLES = frozenset({"table", "tree table", "tree", "list", "list box"})
_CHOOSER_WALK_BUDGET = 500


def file_chooser_window_title(title: str) -> bool:
    """True when ``title`` is a GTK open or save dialog, not a document."""
    return _folded_title(title) in _FILE_CHOOSER_TITLES


def file_basename(path: str) -> str:
    """The last path segment of ``path``, without a ``file://`` prefix."""
    text = str(path or "").strip().replace("\\", "/")
    if text.lower().startswith("file://"):
        text = text[7:]
    text = text.rstrip("/")
    if not text:
        return ""
    return text.rsplit("/", 1)[-1]


def file_chooser_controls(pid: int | None, title: str):
    """``(button, path, app)`` for the active GTK chooser, or None.

    ``button`` is the Open or Save push button. ``path`` is the location
    entry text, or the selected file name when the entry is empty. ``app``
    is the application accessible, so a later read can see the opener after
    the dialog closes. None when the window title is not a chooser, or the
    tree has no accept button. Chrome's chooser sets ``NO_AT_BRIDGE`` and
    has no button here.
    """
    if not file_chooser_window_title(title):
        return None
    Atspi = _atspi()
    desktop = _safe(lambda: Atspi.get_desktop(0))
    if desktop is None:
        return None
    folded = _folded_title(title)
    apps = _desktop_apps(desktop)
    owned = [app for app in apps if pid and pid_of(app) == pid]
    found = _chooser_in_apps(owned, folded, title)
    if found is not None:
        return found
    rest = [app for app in apps if app not in owned]
    return _chooser_in_apps(rest, folded, title)


def chooser_controls_under(root, title: str):
    """``(button, path)`` under ``root``, or None when no Open/Save button.

    The file list is walked only for a selected name. Its rows are not
    allowed to consume the budget before the action button, which sits
    beside the list rather than inside it.
    """
    if root is None:
        return None
    accept: list[tuple[object, str]] = []
    entries: list[tuple[str, bool, str]] = []
    selected: list[str] = []
    _walk_chooser(root, accept, entries, selected, [_CHOOSER_WALK_BUDGET], 0)
    if not accept:
        return None
    return _prefer_accept(accept, title), _chooser_path(entries, selected)


def tree_shows_file(root, filename: str, *, skip_titles: tuple[str, ...] = ()) -> bool:
    """True when ``filename``'s basename appears outside a skipped chooser frame.

    The chooser itself shows the typed path. That text does not mean the
    opener accepted the file. Frames whose title is in ``skip_titles`` are
    left out of the search.
    """
    base = file_basename(filename)
    full = str(filename or "").strip()
    if not base and not full:
        return False
    skip = {_folded_title(item) for item in skip_titles if _folded_title(item)}
    blob: list[str] = []
    _collect_opener_text(root, skip, blob, [_CHOOSER_WALK_BUDGET], 0)
    text = "\n".join(blob)
    if base and base in text:
        return True
    return bool(full and full in text)


def _folded_title(title: str) -> str:
    return " ".join(str(title or "").casefold().split())


def _button_label(name: str) -> str:
    text = str(name or "").replace("_", " ").strip().casefold()
    return text.split("(", 1)[0].strip()


def _desktop_apps(desktop) -> list:
    count = _child_count(desktop)
    if count < 0:
        return []
    apps = []
    for index in range(min(int(count), 80)):
        child = _child_at(desktop, index)
        if child is not None:
            apps.append(child)
    return apps


def _chooser_in_apps(apps, folded: str, title: str):
    for app in apps:
        frames = _direct_children(app)
        named = [
            frame for frame in frames
            if _folded_title(_node_name(frame)) == folded
            or _role_name(frame) == "file chooser" and _folded_title(_node_name(frame)) in {"", folded}
        ]
        if named:
            targets = named
        else:
            # Buttons elsewhere in the app are not an active chooser frame.
            continue
        for target in targets:
            found = chooser_controls_under(target, title)
            if found is not None:
                button, path = found
                return button, path, app
    return None


def _direct_children(acc) -> list:
    count = _child_count(acc)
    if count < 0:
        return []
    out = []
    for index in range(min(int(count), 40)):
        child = _child_at(acc, index)
        if child is not None:
            out.append(child)
    return out


def _walk_chooser(node, accept, entries, selected, budget, depth: int) -> None:
    if node is None or budget[0] <= 0 or depth > 16:
        return
    budget[0] -= 1
    role = _role_name(node)
    name = _node_name(node)
    if role in _FILE_CHOOSER_BUTTON_ROLES:
        label = _button_label(name)
        if label in _FILE_CHOOSER_ACCEPT and _chooser_enabled(node):
            accept.append((node, label))
    elif role in _FILE_CHOOSER_ENTRY_ROLES:
        text = _chooser_field_text(node).strip()
        if text:
            entries.append((text, "FOCUSED" in _state_names(node), name.casefold()))
    elif role in _FILE_CHOOSER_ROW_ROLES and "SELECTED" in _state_names(node):
        label = name.strip() or _chooser_field_text(node).strip()
        if label and label not in {".", ".."}:
            selected.append(label)
    structural, tables = [], []
    for child in _direct_children(node):
        if _role_name(child) in _FILE_CHOOSER_TABLE_ROLES:
            tables.append(child)
        else:
            structural.append(child)
    for child in structural:
        _walk_chooser(child, accept, entries, selected, budget, depth + 1)
    for child in tables:
        _walk_chooser_selection(child, selected, [40], 0)


def _walk_chooser_selection(node, selected, budget, depth: int) -> None:
    if node is None or budget[0] <= 0 or depth > 8:
        return
    budget[0] -= 1
    role = _role_name(node)
    if role in _FILE_CHOOSER_ROW_ROLES and "SELECTED" in _state_names(node):
        label = _node_name(node).strip() or _chooser_field_text(node).strip()
        if label and label not in {".", ".."}:
            selected.append(label)
    count = _child_count(node)
    if count < 0:
        return
    for index in range(min(int(count), 40)):
        _walk_chooser_selection(_child_at(node, index), selected, budget, depth + 1)


def _chooser_enabled(node) -> bool:
    states = _state_names(node)
    if not states:
        return True
    if "SENSITIVE" in states or "ENABLED" in states:
        return True
    return not ({"SHOWING", "VISIBLE", "FOCUSABLE"} & states)


def _chooser_path(entries, selected) -> str:
    """Location text when it was typed, otherwise the selected file name."""
    for text, focused, name in entries:
        if focused and ("/" in text or "\\" in text or "location" in name or name == "name"):
            return text
    for text, _focused, name in entries:
        if "location" in name or name in {"name", "filename"}:
            return text
    for text, _focused, _name in entries:
        if "/" in text or "\\" in text:
            return text
    if selected:
        return selected[-1]
    if entries:
        return entries[-1][0]
    return ""


def _prefer_accept(accept, title: str):
    folded = title.casefold()
    want = "save" if "save" in folded else "open"
    for button, label in accept:
        if label == want:
            return button
    for button, label in accept:
        if label in _FILE_CHOOSER_ACCEPT:
            return button
    return accept[0][0]


def _chooser_field_text(acc) -> str:
    """Text of a chooser entry. A real accessible uses ``Text.get_text``.

    ``Accessible.get_text`` is the deprecated wrapper. A test double has no
    GObject pointer and exposes ``get_text`` or ``text`` itself.
    """
    if getattr(acc, "__gpointer__", None) is not None:
        try:
            got = _full_text(acc)
        except Exception:
            return ""
        return got or ""
    getter = getattr(acc, "get_text", None)
    if callable(getter):
        got = None
        try:
            try:
                got = getter(0, -1)
            except TypeError:
                got = getter()
        except Exception:
            got = None
        if isinstance(got, str):
            return got
    direct = getattr(acc, "text", None)
    if isinstance(direct, str):
        return direct
    return ""


def _state_names(acc) -> set[str]:
    direct = getattr(acc, "states", None)
    if isinstance(direct, (set, frozenset, list, tuple)) and direct:
        return {str(item).upper() for item in direct}
    sset = _call_first(acc, ("get_state_set",))
    if sset is None:
        return set()
    try:
        Atspi = _atspi()
    except ImportError:
        return set()
    st = getattr(Atspi, "StateType", None)
    if st is None:
        return set()
    found = set()
    for label in ("FOCUSED", "SELECTED", "SHOWING", "VISIBLE", "SENSITIVE", "ENABLED", "FOCUSABLE"):
        token = getattr(st, label, None)
        if token is not None and _safe(lambda t=token: sset.contains(t), False):
            found.add(label)
    return found


def _collect_opener_text(node, skip: set[str], blob: list[str], budget, depth: int) -> None:
    if node is None or budget[0] <= 0 or depth > 16:
        return
    role = _role_name(node)
    name = _node_name(node)
    if role in {"frame", "dialog", "window", "file chooser"} and _folded_title(name) in skip:
        return
    budget[0] -= 1
    if name:
        blob.append(name)
    if role in _FILE_CHOOSER_ENTRY_ROLES or role in {"label", "static"}:
        text = _chooser_field_text(node).strip()
        if text:
            blob.append(text)
    count = _child_count(node)
    if count < 0:
        return
    for index in range(min(int(count), 60)):
        _collect_opener_text(_child_at(node, index), skip, blob, budget, depth + 1)


# Chrome's doAction("click") on a canvas or image synthesizes a DOM click at
# the element's top-left. offsetX/offsetY are then 0. A button, link, or
# checkbox keeps that action. These roles need a pointer click at the center.
_COORDINATE_CLICK_ROLES = frozenset({"image", "icon", "canvas", "drawing area"})
_CANVAS_XML_ROLES = frozenset({"img", "image", "graphics-document", "graphics-symbol"})


def coordinate_click_target(acc) -> bool:
    """A Chromium canvas or image. Its action click has no pointer position."""
    if acc is None or not _chromium_app(acc):
        return False
    if _role_name(acc) in _COORDINATE_CLICK_ROLES:
        return True
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or attrs.get("html-tag") or "").strip().lower()
    if tag == "canvas":
        return True
    xml = {
        part.strip().lower()
        for part in str(attrs.get("xml-roles") or "").split()
        if part.strip()
    }
    return bool(xml & _CANVAS_XML_ROLES)


def atspi_screen_box(acc) -> tuple[float, float, float, float] | None:
    """SCREEN ``(x, y, width, height)``, including a zero width or height.

    None when the node has no component, or the box is GTK's negative
    sentinel. A zero side is kept so the caller can replace the size from
    the DOM and still use a real origin.
    """
    position, size = _extents(acc, keep_zero=True)
    if position is None or size is None:
        return None
    x, y = float(position[0]), float(position[1])
    width, height = float(size[0]), float(size[1])
    if x < 0 and y < 0 and width < 0 and height < 0:
        return None
    return (x, y, width, height)


def opaque_click_target(acc) -> bool:
    """A canvas, or an image with no name.

    The accessibility tree does not contain the pixels, so a focus or
    state change is not evidence the click landed inside the drawing.
    A named image that is not a canvas can still change the page, and
    that change remains evidence.
    """
    if acc is None:
        return False
    role = _role_name(acc)
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or attrs.get("html-tag") or "").strip().lower()
    xml = {
        part.strip().lower()
        for part in str(attrs.get("xml-roles") or "").split()
        if part.strip()
    }
    if role in {"canvas", "drawing area"} or tag == "canvas" or xml & {"graphics-document", "graphics-symbol"}:
        return True
    if role in {"image", "icon"} or bool(xml & {"img", "image"}):
        name = str(_call_first(acc, ("get_name",), default="") or "").strip()
        if name:
            return False
        # The accessible name is empty. A Text caption (a thumbnail's page
        # number) is still a label, so the click is not an unnamed image.
        return not _image_text_label(acc)
    return False


def opaque_click_verdict(acc) -> tuple[str, str] | None:
    """``(unverifiable, evidence)`` for a canvas or unnamed image. None otherwise.

    The click may already have been sent. This does not claim it missed.
    Accessibility cannot see the pixels, so the result is not confirmed
    from the tree. The evidence tells the caller to check a crop or a
    screenshot.
    """
    if not opaque_click_target(acc):
        return None
    return (
        "unverifiable",
        "accessibility cannot see inside this canvas or unnamed image, so the click is not confirmed; "
        "verify with crop or a screenshot",
    )


def coordinate_click_info(acc) -> dict | None:
    """Identity and AT-SPI box for a canvas or image click, or None.

    The box may have a zero side. ``element_id``, ``label``, and ``tag`` are
    plain strings so a DOM lookup does not touch AT-SPI again.
    """
    if not coordinate_click_target(acc):
        return None
    attrs = _get_attributes(acc)
    return {
        "box": atspi_screen_box(acc),
        "pid": pid_of(acc),
        "element_id": str(attrs.get("id") or attrs.get("html-id") or "").strip(),
        "label": str(_call_first(acc, ("get_name",), default="") or "").strip(),
        "tag": str(attrs.get("tag") or attrs.get("html-tag") or "").strip().lower(),
    }


# A canvas ancestor sits between the control and its document. Stopping at
# the document keeps a normal button from walking the whole window. The
# canvas itself is not fallback content: a click on it stays a center click.
_CANVAS_FALLBACK_STOP_ROLES = frozenset({
    "document web", "document frame", "application", "frame", "window", "desktop frame",
})


def _canvas_surface(acc) -> bool:
    """A Chromium ``<canvas>``. A GTK drawing area and an image are not one.

    Chrome exposes ``<canvas>`` as role ``embedded`` with ``tag=canvas``, and
    sometimes as role ``canvas``. The tag is the boundary. Fallback content
    nested in that element is in the tree and is not painted.
    """
    if acc is None:
        return False
    if _role_name(acc) == "canvas":
        return True
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or attrs.get("html-tag") or "").strip().lower()
    return tag == "canvas"


def canvas_fallback_content(acc) -> bool:
    """True when ``acc`` is Chromium content nested inside a canvas.

    That content is not painted, so a pointer click lands on the canvas and
    the control's handler does not run. False for the canvas element itself,
    for GTK, and for anything that is not inside a canvas.
    """
    if acc is None or not _chromium_app(acc) or _canvas_surface(acc):
        return False
    current = acc
    seen: set[int] = set()
    for _ in range(40):
        parent = _parent_of(current)
        if parent is None or id(parent) in seen:
            return False
        seen.add(id(parent))
        if _canvas_surface(parent):
            return True
        if _role_name(parent) in _CANVAS_FALLBACK_STOP_ROLES:
            return False
        current = parent
    return False


def canvas_fallback_dom_spec(acc) -> dict:
    """DOM id, name, and tag for a click on canvas fallback content.

    The caller has already decided ``acc`` is fallback content. The strings
    are plain so the DOM lookup does not touch AT-SPI again.
    """
    attrs = _get_attributes(acc) if acc is not None else {}
    label = ""
    if acc is not None:
        label = str(_call_first(acc, ("get_name",), default="") or "").strip()
    return {
        "pid": pid_of(acc),
        "element_id": str(attrs.get("id") or attrs.get("html-id") or "").strip(),
        "label": label,
        "tag": str(attrs.get("tag") or attrs.get("html-tag") or "").strip().lower(),
    }


def grab_focus(acc) -> bool:
    """Give ``acc`` keyboard focus via AT-SPI (True on success) — no cursor move."""
    comp = _component(acc)
    if comp is None:
        return False
    return bool(_call_first(comp, ("grab_focus", "grabFocus"), default=False))


_TEXT_CARET_ROLES = frozenset({"paragraph", "heading"})
_WRITER_DOCUMENT_ROLES = frozenset({"document text"})
_SHAPE_DOCUMENT_ROLES = frozenset({"document presentation", "document drawing"})
_SHAPE_FRAME_ROLES = frozenset({"shape", "text frame", "text-frame"})
_SHAPE_TEXT_ROLES = _TEXT_CARET_ROLES | _SHAPE_FRAME_ROLES


def _ancestor_nodes(acc, limit: int = 40) -> list:
    """``acc`` and its parents, nearest first. Cycles stop the walk."""
    nodes = []
    node = acc
    seen: set[int] = set()
    for _ in range(limit):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        nodes.append(node)
        node = _parent_of(node)
    return nodes


def _document_roles(acc) -> set[str]:
    """Document roles on ``acc`` and its parents."""
    found = set()
    for node in _ancestor_nodes(acc):
        role = _role_name(node)
        if role in _WRITER_DOCUMENT_ROLES or role in _SHAPE_DOCUMENT_ROLES:
            found.add(role)
        if role == "application":
            break
    return found


def is_libreoffice_text_paragraph(acc) -> bool:
    """A Writer paragraph or heading under ``document text``.

    Impress and Draw paragraphs live in a shape's text frame. Setting their
    caret does not start text editing, so they are not this shortcut.
    """
    if acc is None or _role_name(acc) not in _TEXT_CARET_ROLES:
        return False
    if not _libreoffice_accessible(acc):
        return False
    roles = _document_roles(acc)
    if roles & _SHAPE_DOCUMENT_ROLES:
        return False
    return bool(roles & _WRITER_DOCUMENT_ROLES)


def _text_frame_panel(acc) -> bool:
    """A panel LibreOffice names as a text frame. The role stays ``panel``."""
    if acc is None or _role_name(acc) != "panel":
        return False
    name = str(_call_first(acc, ("get_name",), default="") or "").strip().casefold()
    return name.startswith("text frame")


def _shape_text_node(acc) -> bool:
    """A paragraph, shape, text-frame role, or a panel named as a text frame."""
    if acc is None:
        return False
    role = _role_name(acc)
    if role in _SHAPE_TEXT_ROLES:
        return True
    return _text_frame_panel(acc)


def is_libreoffice_shape_text(acc) -> bool:
    """An Impress or Draw shape, text frame, or a paragraph inside one."""
    if not _shape_text_node(acc) or not _libreoffice_accessible(acc):
        return False
    return bool(_document_roles(acc) & _SHAPE_DOCUMENT_ROLES)


def _shape_text_frame(acc):
    """The shape or text frame that holds ``acc``, or ``acc`` when it is one."""
    fallback = acc
    for node in _ancestor_nodes(acc):
        if _role_name(node) in _SHAPE_FRAME_ROLES or _text_frame_panel(node):
            return node
        if _role_name(node) in _SHAPE_DOCUMENT_ROLES:
            break
    return fallback


def _bounded_descendants(acc, *, limit: int = 48) -> list:
    """``acc`` and descendants, capped so a slide tree cannot walk the app."""
    found: list = []
    stack = [acc]
    seen: set[int] = set()
    while stack and len(found) < limit:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        found.append(node)
        count = _raw_child_count(node)
        if count <= 0:
            continue
        for index in range(min(count, 24)):
            if len(found) + len(stack) >= limit:
                break
            stack.append(_child_at(node, index))
    return found


def _shape_text_nodes(acc) -> list:
    """The frame, its descendants, and ``acc`` when the frame did not list it."""
    frame = _shape_text_frame(acc)
    nodes = _bounded_descendants(frame)
    if acc is not None and all(node is not acc for node in nodes):
        nodes.append(acc)
    return nodes


def _caret_offset(acc):
    """The text caret, or None when this node has none."""
    try:
        Atspi = _atspi()
    except ImportError:
        return None
    caret = _safe(lambda: Atspi.Text.get_caret_offset(acc))
    if isinstance(caret, bool) or not isinstance(caret, int) or caret < 0:
        return None
    return caret


def _focused_editable_paragraph(acc) -> bool:
    """True when ``acc`` is a focused paragraph that can take text."""
    if acc is None or _role_name(acc) not in _TEXT_CARET_ROLES:
        return False
    if not _state_has(acc, "FOCUSED"):
        return False
    if _state_has(acc, "EDITABLE"):
        return True
    return _editable_iface(acc) is not None


def shape_text_click_verdict(acc) -> tuple[str, str] | None:
    """``(outcome, evidence)`` for an Impress or Draw text click.

    Confirmed when a paragraph inside the frame is focused and editable, or
    when the caret is in that frame and the frame has focus. A caret set on
    an unfocused paragraph is not text editing, and that click is partial.
    None when ``acc`` is not shape text.
    """
    if not is_libreoffice_shape_text(acc):
        return None
    nodes = _shape_text_nodes(acc)
    if any(_focused_editable_paragraph(node) for node in nodes):
        return "confirmed", "a focused editable paragraph is inside the text frame"
    focused = any(_state_has(node, "FOCUSED") for node in nodes)
    if focused and any(
        _role_name(node) in _TEXT_CARET_ROLES and _caret_offset(node) is not None
        for node in nodes
    ):
        return "confirmed", "the caret is inside the text frame"
    return "partial", "the text frame is not in text edit"


def shape_text_editing(acc) -> bool:
    """True when an Impress or Draw click has entered text editing."""
    verdict = shape_text_click_verdict(acc)
    return verdict is not None and verdict[0] == "confirmed"


def _shape_document(acc):
    """The presentation or drawing that contains ``acc``, or None."""
    for node in _ancestor_nodes(acc):
        if _role_name(node) in _SHAPE_DOCUMENT_ROLES:
            return node
    return None


def _shape_edit_paragraph_text(acc) -> str | None:
    """Text of the paragraph in text edit, or None.

    The document presentation stays focused and its own text stays empty.
    The paragraph inside the text frame is where the keys land. A caret on
    an unfocused paragraph is still that frame. None when ``acc`` is not in
    a LibreOffice presentation or drawing, or no paragraph there is editing.
    """
    root = _shape_document(acc)
    if root is None or not _libreoffice_accessible(root):
        return None
    caret_text = None
    for node in _bounded_descendants(root, limit=96):
        if _role_name(node) not in _TEXT_CARET_ROLES:
            continue
        try:
            text = _full_text(node)
        except Exception:
            text = None
        if text is None:
            continue
        shown = text.replace("\u00a0", " ")
        if _state_has(node, "FOCUSED"):
            return shown
        if caret_text is None and _caret_offset(node) is not None:
            caret_text = shown
    return caret_text


def focused_shape_edit_text(app: str) -> str | None:
    """Paragraph text of the Impress or Draw frame in text edit, or None.

    None when focus is not in a presentation or drawing, so a Writer
    document and a Calc cell keep their own read-back.
    """
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        return None
    if truncated or acc is None:
        return None
    try:
        return _shape_edit_paragraph_text(acc)
    except Exception:
        return None


def poll_focused_shape_type(app: str, before: str | None, text: str) -> str | None:
    """Settled paragraph text of the Impress or Draw frame in edit.

    None when that frame is no longer in text edit. ``before`` is the
    paragraph captured before the keys. The document's empty text is not
    this result, so a title that gains the typed characters confirms.
    """
    def read():
        return focused_shape_edit_text(app)

    try:
        current = read()
    except Exception:
        return None
    if current is None:
        return None
    return _poll_typed_text(read, before, text)


def place_paragraph_caret(acc, x: int, y: int) -> bool:
    """Put the caret in this Writer paragraph. False when ``acc`` is not one.

    A coordinate click uses LibreOffice screen extents, which sit about one
    title bar above the text, so the pointer lands in the paragraph above.
    ``set_caret_offset`` addresses the paragraph itself. A point inside that
    paragraph's text uses that character. Any other point uses the end,
    which is where a following ``type`` appends. Impress and Draw shapes
    are not this path: a pointer click enters their text edit.
    """
    if not is_libreoffice_text_paragraph(acc):
        return False
    try:
        Atspi = _atspi()
    except ImportError:
        return False
    grab_focus(acc)
    count = _safe(lambda: Atspi.Text.get_character_count(acc))
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        text = _full_text(acc)
        if text is None:
            return False
        count = len(text)
    coord = getattr(getattr(Atspi, "CoordType", None), "SCREEN", 0)
    point = _safe(lambda: Atspi.Text.get_offset_at_point(acc, int(x), int(y), coord))
    offset = count
    if isinstance(point, int) and not isinstance(point, bool) and 0 <= point <= count:
        offset = point
    wrote = _move_caret(Atspi, acc, offset)
    caret = _safe(lambda: Atspi.Text.get_caret_offset(acc))
    if isinstance(caret, int) and not isinstance(caret, bool) and caret >= 0:
        return True
    return bool(wrote)


def paragraph_click_verdict(acc) -> tuple[str, str] | None:
    """``(outcome, evidence)`` for a paragraph click, or None for any other target.

    Writer is confirmed only when the caret is in that paragraph. A click
    that changes focus and leaves the caret in the line above is not
    confirmed. Impress and Draw are confirmed only when the frame is in
    text edit.
    """
    shape = shape_text_click_verdict(acc)
    if shape is not None:
        return shape
    if not is_libreoffice_text_paragraph(acc):
        return None
    try:
        Atspi = _atspi()
    except ImportError:
        return None

    def read():
        return _safe(lambda: Atspi.Text.get_caret_offset(acc))

    caret = read()
    if isinstance(caret, bool) or not isinstance(caret, int) or caret < 0:
        time.sleep(0.05)
        caret = read()
    if isinstance(caret, int) and not isinstance(caret, bool) and caret >= 0:
        return "confirmed", "the caret is in the target paragraph"
    return "partial", "the caret is not in the target paragraph"


def _editable_iface(acc):
    return _call_first(acc, ("get_editable_text_iface", "get_editable_text"))


_CONTAINER_TEXT_ROLES = frozenset({"section", "paragraph", "panel", "grouping", "filler"})
# Roles whose text interface is the document or a static node. Selecting one
# selects the page. A combo is not an entry either; set_value writes it first.
_STATIC_REPLACE_ROLES = frozenset({
    "paragraph", "heading", "label", "static", "caption",
    "panel", "grouping", "filler",
    "document web", "document frame", "document email",
    "page",
    "combo box", "combo-box",
})
_ENTRY_ROLE_NAMES = frozenset({
    "entry", "password text", "text", "editable text", "terminal", "document text",
    # A GTK spin and an <input type=number> are spin buttons. They are entries
    # only when this node itself is STATE_EDITABLE. A date segment is still
    # written by set_text; the block check lets that through.
    "spin button",
})
_CONTENTEDITABLE_TAGS = frozenset({"div", "span"})


def editable_entry(
    acc,
    *,
    role_str: str | None = None,
    attrs: dict | None = None,
    state_editable: bool | None = None,
    drawing: bool = False,
) -> bool:
    """True when this node itself is an editable text entry.

    The positive signal is ``STATE_EDITABLE`` on this node. EditableText
    support is not that signal: Firefox exposes the interface on paragraphs,
    documents, and address-bar wrappers that are not editors. A paragraph,
    panel, document, or select-style combo is not an entry even when it reports
    ``STATE_EDITABLE``. A read-only node is not.

    An entry, password, text, or spin-button role is an entry only when this
    node is ``STATE_EDITABLE``. A contenteditable with no textbox role is a section
    whose tag is ``div`` or ``span`` and whose own state is ``EDITABLE``.
    A Chromium input-tagged combobox with Text and EDITABLE is also an entry.
    Chromium often has no EditableText (#221). Firefox's roleless
    contenteditable is the same section and does not need the interface
    either. A paragraph is not that section.
    An Impress or Draw paragraph with STATE_EDITABLE and EditableText
    is also an entry when drawing is true. Math command paragraphs require
    their native editable panel and Math window. Browser paragraphs stay refused.
    """
    if acc is None:
        return False
    role = role_str if role_str is not None else _role_name(acc)
    if role in {"paragraph", "heading"}:
        return bool(drawing) or math_command_editable(acc)
    if role in {"combo box", "combo-box"}:
        # Chromium ARIA autocomplete and datalist inputs own their text; their popup
        # options are not children and do not constrain the input value.
        # Gecko address-bar wrappers also report input, Text, and EDITABLE;
        # those wrappers must retain the select-style classification.
        if not _chromium_app(acc):
            return False
        if attrs is None:
            attrs = _get_attributes(acc)
        if state_editable is None:
            state_editable = _state_has(acc, "EDITABLE")
        return bool(
            str(attrs.get("tag") or "").lower() == "input"
            and state_editable and not _state_has(acc, "READ_ONLY")
            and _call_first(acc, ("get_text_iface",)) is not None
        )
    if role in _STATIC_REPLACE_ROLES:
        return False
    if role in _ENTRY_ROLE_NAMES:
        if _state_has(acc, "READ_ONLY"):
            return False
        if state_editable is None:
            state_editable = _state_has(acc, "EDITABLE")
        return bool(state_editable)
    if role != "section":
        return False
    if attrs is None:
        attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or "").lower()
    if tag not in _CONTENTEDITABLE_TAGS:
        return False
    xml = set(str(attrs.get("xml-roles") or "").split())
    if "combobox" in xml:
        return False
    if state_editable is None:
        state_editable = _state_has(acc, "EDITABLE")
    if not state_editable or _state_has(acc, "READ_ONLY"):
        return False
    return bool(_chromium_app(acc) or _gecko_app(acc))


def _blocks_text_replace(acc) -> bool:
    """True when ``set_text`` must not select or type into this node.

    A role-less object is a synthetic field. A browser paragraph, panel,
    document, or select-style combo is blocked even when it exposes EditableText or
    ``STATE_EDITABLE``. An Impress or Draw paragraph with both is not
    blocked. A contenteditable section, input combobox, and real entry are not.
    """
    role = _role_name(acc)
    if role == "":
        return False
    if is_writer_document(acc):
        return False
    if _chrome_date_segment(acc):
        return False
    if math_command_editable(acc):
        return False
    if role in {"paragraph", "heading"} and drawing_text_editable(acc, role_str=role):
        return False
    if role in {"combo box", "combo-box"}:
        return not editable_entry(acc)
    if role in _STATIC_REPLACE_ROLES:
        return True
    if role == "section":
        return not (editable_entry(acc) or _chromium_contenteditable(acc))
    if role in _ENTRY_ROLE_NAMES and role != "spin button":
        # A spin button's own EditableText is the number field, not the
        # page. Clearing one must stay on set_text. An entry or text role
        # without STATE_EDITABLE is a wrapper and is not typed into.
        if _chromium_contenteditable(acc):
            return False
        # Synthetic transports can expose entry roles without a native
        # EDITABLE state API. Keep their existing write/read-back path;
        # Real AT-SPI nodes, Firefox wrappers, and text views exposing a
        # state-set API still require the bit, even without native GI.
        native_state = _safe(lambda: _atspi().StateType.EDITABLE) is not None
        if (native_state or _gecko_app(acc)
                or (role in {"text", "editable text"}
                    and _call_first(acc, ("get_state_set",)) is not None)):
            return not editable_entry(acc)
    return _state_has(acc, "READ_ONLY")


def _state_has(acc, name: str) -> bool:
    """Whether the state set contains ``name``.

    A fake state set matches the name string. The GI binding matches
    ``Atspi.StateType``. Missing gi, or a node with no state set, is false.
    The event-thread property cache is dropped first so a SELECTED or
    SHOWING bit is the bus value.
    """
    return _state_flag(acc, name, drop_cache=True)


def _state_flag(acc, name: str, *, drop_cache: bool, states=_CACHE_MISS) -> bool:
    """Whether the state set contains ``name``.

    ``drop_cache`` is false when the caller is about to read text on this
    same node. ``clear_cache_single`` on a Chromium contenteditable makes
    the following ``get_text`` return NULL. ``states`` may supply the fresh
    reply shared by the flags of one security probe; it is never cached
    across probes.
    """
    if drop_cache:
        _drop_cached_properties(acc)
    sset = _call_first(acc, ("get_state_set",)) if states is _CACHE_MISS else states
    if sset is None:
        return False
    names = getattr(sset, "names", None)
    if isinstance(names, (set, frozenset, list, tuple)):
        folded = {str(item) for item in names}
        return name in folded or name.lower() in {item.lower() for item in folded}
    if bool(_safe(lambda: sset.contains(name), False)):
        return True
    try:
        Atspi = _atspi()
    except Exception:
        return False
    member = getattr(getattr(Atspi, "StateType", None), name, None)
    if member is None:
        return False
    return bool(_safe(lambda: sset.contains(member), False))


def _utf16_length(text: str) -> int:
    """Number of QString code units, including surrogate pairs."""
    return len(text.encode("utf-16-le")) // 2


def _qt_python_offset(text: str, offset: int) -> int:
    """Convert a native Qt offset, refusing positions inside a surrogate pair."""
    try:
        return len(text.encode("utf-16-le")[:offset * 2].decode("utf-16-le"))
    except UnicodeDecodeError:
        raise _text_mismatch(
            "invalid_text_offset", "the Qt text offset splits a surrogate pair"
        ) from None


def _insert_length(method, text: str, acc=None) -> int:
    """The ``length`` argument EditableText.insert_text actually wants.

    GTK through libatspi and ``gi.repository.Atspi`` takes a UTF-8 byte count.
    Passing the character count keeps only that many bytes, so ``Привет``
    becomes ``При`` and a cut code point inserts nothing. Qt's adaptor does
    ``QString::resize(length)`` and inserts that string, so the same byte
    count reads past the text and appends uninitialized characters
    (``ünï`` becomes ``ünï`` plus a NUL and whatever followed it). Qt gets
    the UTF-16 code-unit count. Qt positions use UTF-16 units too.

    A Python double that slices characters has no ``gi.`` module and gets
    ``len(text)`` unless it sets ``_length_unit`` to ``bytes`` or ``chars``.
    ``_length_unit`` wins over the toolkit, so a test can force either one.
    """
    unit = getattr(method, "_length_unit", None)
    if unit == "bytes":
        return len(text.encode("utf-8"))
    if unit == "chars":
        return len(text)
    if acc is not None and _qt_app(acc):
        return _utf16_length(text)
    func = getattr(method, "__func__", method)
    module = str(getattr(method, "__module__", None) or getattr(func, "__module__", "") or "")
    if module.startswith("gi."):
        return len(text.encode("utf-8"))
    return len(text)


def _selection_bounds(selection) -> tuple[int, int] | None:
    """(start, end) character offsets from an AT-SPI selection, or None."""
    if selection is None:
        return None
    start = getattr(selection, "start_offset", None)
    end = getattr(selection, "end_offset", None)
    if isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int) and not isinstance(end, bool):
        return int(start), int(end)
    if isinstance(selection, tuple):
        nums = [item for item in selection if isinstance(item, int) and not isinstance(item, bool)]
        if len(nums) >= 2:
            return nums[-2], nums[-1]
    return None


def _caret_and_selection(acc, nchars: int) -> tuple[int, int, int]:
    """(caret, selection start, selection end). An empty selection has start == end.

    Offsets and nchars use native units (UTF-16 for Qt). A missing caret
    is the end of the field.
    """
    Atspi = _atspi()
    caret = _safe(lambda: Atspi.Text.get_caret_offset(acc))
    if not isinstance(caret, int) or isinstance(caret, bool) or caret < 0 or caret > nchars:
        caret = nchars
    start, end = caret, caret
    count = _safe(lambda: Atspi.Text.get_n_selections(acc))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        bounds = _selection_bounds(_safe(lambda: Atspi.Text.get_selection(acc, 0)))
        if bounds is not None:
            start, end = bounds
            start = max(0, min(int(start), nchars))
            end = max(0, min(int(end), nchars))
            if end < start:
                start, end = end, start
    return int(caret), start, end


def _text_mismatch(reason: str, message: str, **detail):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    payload = {"platform": "linux", "reason": reason}
    payload.update(detail)
    return ComputerUseError(ErrorCode.UNSUPPORTED, message, detail=payload)


def _excerpt(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 80:
        return value
    return value[:80] + "…"


def _refuse_chromium_tab_text(acc, text: str) -> None:
    """Reject tabs before a Chromium field write can clear or type a prefix.

    Chromium's key fallback interprets TAB as focus navigation. Even an
    advertised EditableText interface may fail and fall back to those keys,
    so preflight the entire write before attempting either route.
    """
    if "\t" in text and _chromium_app(acc):
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "TAB would move focus in a Chromium field; nothing was typed or cleared",
            detail={
                "platform": "linux",
                "reason": "tab_moves_focus",
                "outcome": "refused",
                "next": ["clipboard", "cdp"],
                "hint": "Paste the text using the clipboard or use the CDP browser "
                        "backend, then verify the complete field value.",
            },
        )


def insert_text(acc, text: str) -> int | None:
    """Insert ``text`` at the caret via AT-SPI EditableText.

    A selection is deleted first and the text replaces it. ``None`` means the
    element has no EditableText, so the caller may use keystrokes. The return
    value is the number of characters the read-back shows were inserted. A
    field that does not contain that text raises `ErrorCode.UNSUPPORTED`
    instead of reporting success. A CRLF is one newline.

    The insert length is the UTF-8 byte count on the GTK GI/C binding and the
    UTF-16 unit count for Qt and character count for a binding that slices
    characters. Qt positions also use UTF-16 units. A Qt read-back also requires
    the native count to equal the string’s UTF-16 length: a D-Bus string stops at an
    embedded NUL, so the text alone can look right while the widget holds
    extra characters.

    After an exact buffer read-back, a caret still at the insertion point is
    advanced past the inserted text and read back before reporting success.
    Draw/Impress paragraphs retain their separate append-at-end behavior.
    """
    _refuse_chromium_tab_text(acc, text)
    typed = text.replace("\r\n", "\n")
    if text_visibility_unknown(acc):
        if _write_unknown_entry(acc, typed, insert=True):
            return _PrivateTextCount(len(typed))
        raise _text_mismatch("text_mismatch", "private text write could not be verified",
                             private_text=True)
    eti = _editable_iface(acc)
    if eti is None:
        return None
    # A Writer document's own text stays empty. The paragraphs are the
    # read-back, captured before the insert so a later poll can see them
    # arrive. An entry is not this document and does not walk.
    writer_doc = writer_document_for(acc)
    writer_before = None
    if writer_doc is not None:
        writer_before = writer_document_text(writer_doc) or ""
    current = _full_text(acc)
    # Read the words, not the U+FFFC placeholders, before the insert. A
    # contenteditable's parent text can stay ``\ufffc`` while the child gains
    # the characters. NBSP in that read is a space.
    before_readable = _readable_text(acc) if current is not None else None
    if current is None:
        raise _text_mismatch(
            "text_unreadable",
            "the field text could not be read, so type was not reported as success",
        )
    qt = _qt_app(acc)
    nchars = _utf16_length(current) if qt else len(current)
    caret, start, end = _caret_and_selection(acc, nchars)
    py_caret, py_start, py_end = (
        tuple(_qt_python_offset(current, offset) for offset in (caret, start, end))
        if qt else (caret, start, end)
    )
    if "\n" in typed and _writer_document_type_target(acc) is not None:
        if end > start:
            _call_first(eti, ("delete_text",), start, end, default=False)
            cleared = _full_text(acc)
            wanted = current[:py_start] + current[py_end:]
            if cleared != wanted:
                raise _text_mismatch(
                    "selection_not_replaced",
                    "the selection was not removed, so the text was not inserted",
                    expected=_excerpt(wanted),
                    actual=_excerpt(cleared),
                )
        return _type_writer_paragraphs(acc, typed, eti)
    if end > start:
        _call_first(eti, ("delete_text",), start, end, default=False)
        cleared = _full_text(acc)
        wanted = current[:py_start] + current[py_end:]
        if cleared != wanted:
            raise _text_mismatch(
                "selection_not_replaced",
                "the selection was not removed, so the text was not inserted",
                expected=_excerpt(wanted),
                actual=_excerpt(cleared),
            )
        offset = start
        expected = wanted[:py_start] + typed + wanted[py_start:]
    else:
        offset = caret
        expected = current[:py_caret] + typed + current[py_caret:]
    insert = None
    for name in ("insert_text", "insertText"):
        insert = getattr(eti, name, None)
        if insert is not None:
            break
    length = _insert_length(insert, typed, acc) if insert is not None else len(typed)
    wrote = bool(_call_first(eti, ("insert_text", "insertText"), int(offset), typed, int(length), default=False))
    if _confirm_text(acc, expected):
        # Scintilla inserts the buffer text without advancing the caret. Only
        # correct a caret still at the verified insertion point; other native
        # caret movement belongs to the widget. Math can also retain the old
        # selection endpoint after replacement; place its caret after the
        # verified insertion. Positions are not UTF-8 bytes.
        Atspi = _atspi()
        after_caret = _safe(lambda: Atspi.Text.get_caret_offset(acc)) if typed else None
        if (
            typed and (
                math_command_editable(acc)
                or (type(after_caret) is int and after_caret == offset
                    and not drawing_text_editable(acc))
            )
        ):
            target = offset + (_utf16_length(typed) if qt else len(typed))
            _move_caret(Atspi, acc, target)
            if _safe(lambda: Atspi.Text.get_caret_offset(acc)) != target:
                raise _text_mismatch(
                    "caret_not_advanced",
                    "the text was inserted but the caret could not be advanced",
                    inserted_chars=len(typed),
                    unchanged=False,
                )
        return len(typed)
    # The raw string can still be U+FFFC, or spaces can come back as NBSP.
    # Compare the expanded read so a successful insert is not a mismatch.
    # Qt still has to report the character count of the string that was
    # asked for: GetText stops at a NUL, so the readable text can match
    # while the widget is longer.
    after_readable = _readable_text(acc)
    if (
        not _typed_visible(before_readable, after_readable, typed)
        and _chromium_contenteditable(acc)
    ):
        # The first AT-SPI read can still be the pre-type text. Wait until
        # the field settles. A settled read that lacks the characters is
        # still a mismatch, and a Firefox field is not delayed here.
        after_readable = _poll_typed_text(
            lambda: _readable_text(acc), before_readable, typed, chrome=True
        )
    if _typed_visible(
        before_readable, after_readable, typed, chrome=_chromium_contenteditable(acc)
    ) and _qt_text_count_matches(acc, expected):
        return len(typed)
    actual = _full_text(acc)
    if writer_doc is not None:
        # The document node reads "" while the paragraphs hold the lines.
        # Poll only when the insert reported a write or the node text
        # changed; otherwise one read decides. Unchanged paragraphs mean
        # the caller may send keys. A different paragraph text is a miss,
        # and keys are not sent on top of it.
        if wrote or actual != current:
            shown = poll_writer_document(writer_doc, writer_before, typed)
        else:
            shown = writer_document_text(writer_doc)
        if _writer_typed_visible(writer_before, shown, typed):
            return len(typed)
        if (shown or "") == (writer_before or ""):
            return None
        raise _text_mismatch(
            "text_mismatch",
            "the field text after type does not match what was inserted",
            expected=_excerpt(typed),
            actual=_excerpt(shown),
            inserted_chars=len(typed),
            unchanged=False,
        )
    if actual == current and not wrote:
        return None
    # ``unchanged`` is the words, not the raw object-replacement string.
    # A Firefox contenteditable can keep ``\ufffc\ufffc`` while a child
    # paragraph gains the typed text. That is a change. A parent that still
    # reads the same sentence did not change, and the caller may send keys.
    # A field that changed to something else is still a mismatch, with no
    # key fallback.
    if before_readable is None:
        unchanged = actual == current
        shown = actual
    else:
        unchanged = _norm_nbsp(after_readable) == _norm_nbsp(before_readable)
        shown = after_readable if after_readable is not None else actual
    raise _text_mismatch(
        "text_mismatch",
        "the field text after type does not match what was inserted",
        expected=_excerpt(expected),
        actual=_excerpt(shown),
        inserted_chars=len(typed),
        unchanged=unchanged,
        before=_excerpt(before_readable if before_readable is not None else current),
    )


# After a clear or a write, a web field's text can show up on a later read.
# The first read is immediate; these extra reads cover that gap. A value that
# never equals the request is still a failure.
_TEXT_CONFIRM_POLLS = 4
_TEXT_CONFIRM_PAUSE_S = 0.02


def _entry_text_visible(role, attrs, visibility) -> bool:
    """Require an explicit toolkit text-visibility signal before retrying."""
    if role not in {"entry", "text"}:
        return False
    return str(attrs.get("visibility", "")).strip().lower() in {"true", "1"} or visibility is True


def _entry_text_masked(role, attrs, text) -> bool:
    """Treat bullets and toolkit invisible characters as masked text."""
    if role not in {"entry", "text"} or not isinstance(text, str) or not text:
        return False
    glyphs = set("●•*◦○∙⋅▪▫■□⬤\u200b\u200c\u200d\u2060\ufeff")
    for key in ("invisible-char", "invisible_char"):
        char = str(attrs.get(key, ""))
        if len(char) == 1:
            glyphs.add(char)
        elif char.isdecimal():
            codepoint = int(char)
            if 0 < codepoint <= 0x10ffff:
                glyphs.add(chr(codepoint))
    return any(char in glyphs for char in text) and all(
        char in glyphs or char.isspace() for char in text
    )


def _read_full_text(acc, *, count=None, verification=False):
    """Keep ambiguous GTK 4 sentinel text empty outside boolean verification.

    Chromium's bounded read can echo a write instead of the actual contents,
    so a nonempty sentinel result remains authoritative. NULL still means
    unreadable. A bounded retry may expose an unmarked masked entry, so
    public retries require explicit visibility or a matching unmasked GTK layout
    string, and reject masking glyphs.
    """
    secure, role, attrs, visibility = _text_security(acc)
    if secure:
        # Preserve the public empty-text convention for invisible-char fields.
        return "" if any(key in attrs for key in ("invisible-char", "invisible_char")) else None
    Atspi = _atspi()
    got = _safe(lambda: Atspi.Text.get_text(acc, 0, -1))
    if got == "" and count != 0 and (
        verification or _entry_text_visible(role, attrs, visibility)
    ):
        if count is None:
            count = _safe(lambda: Atspi.Text.get_character_count(acc))
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            got = _safe(lambda: Atspi.Text.get_text(acc, 0, count))
    if not verification and _entry_text_masked(role, attrs, got):
        return ""
    return got


def _verification_matches(acc, text: str) -> bool:
    """Compare a private read without returning its contents."""
    if is_secure(acc):
        return False
    _call_first(acc, ("clear_cache", "clearCache"))
    return _texts_match(_read_full_text(acc, verification=True), text)


def _full_text(acc) -> str | None:
    """The text a snapshot shows: ``Text.get_text(acc, 0, -1)``.

    ``None`` means that read failed. ``""`` is an empty field when the toolkit
    returns one. GTK 4's empty sentinel is retried only with explicit toolkit
    text visibility or a matching unmasked GTK layout string; ambiguous entries
    and masking glyphs stay private. A nonempty
    sentinel read is kept: on a Chromium web
    field that count can be the length of the string just passed
    to ``set_text_contents``, and the bounded read echoes that string while
    ``get_text(0, -1)`` still has the previous contents. The client text cache
    is dropped first so the read is not the pre-write value.
    """
    _call_first(acc, ("clear_cache", "clearCache"))
    got = _read_full_text(acc)
    if got is None:
        return None
    return str(got)


def _x11_keys_available() -> bool:
    """True when XTEST can reach the session. Matches ``linux._on_wayland``."""
    return not (os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"))


def _select_range(acc, end: int) -> None:
    """Select ``[0, end)`` on the Text interface. A missing method is ignored."""
    if end <= 0:
        return
    Atspi = _atspi()
    if not _safe(lambda: Atspi.Text.set_selection(acc, 0, 0, end)):
        _safe(lambda: Atspi.Text.add_selection(acc, 0, end))


def _text_is_gone(acc) -> bool:
    """True when the snapshot read is blank or could not be read.

    Chromium returns NULL from ``get_text(0, -1)`` on an empty field, because
    the start offset is past the end. An empty contenteditable reads back as
    a newline or a space (the ``<br>``), which is the same clear. An
    unreadable field is treated as clear here so the replacement can be
    written; ``set_text`` still returns True only when a later snapshot read
    equals the new string.
    """
    return _text_is_blank(_full_text(acc))


def _wait_until_gone(acc) -> bool:
    for attempt in range(_TEXT_CONFIRM_POLLS):
        if _text_is_gone(acc):
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    return False


def _x11_select_all_and_delete(acc) -> None:
    """Focus the field and replace its selection with nothing. X11 only.

    Each chord is sent only after focus is on ``acc``. A field that does not
    take focus raises ``focus_lost`` and the chord is not sent.
    """
    grab_focus(acc)
    _chord_into_target(acc, "ctrl+a")
    _chord_into_target(acc, "backspace")


def _type_string(text: str) -> None:
    from a11y_computer_use.drivers import _linux_input

    _linux_input.type_string(text)


# A click or grab_focus can move focus after it returns. Firefox on a busy
# AT-SPI bus publishes FOCUSED later than one short pass, and one grab_focus
# can be dropped. The first 10 polls are that short pass. A miss asks for
# focus again and keeps waiting. 40 polls is 1.6s. A node that is still
# unfocused is not typed into.
_FOCUS_CONFIRM_POLLS = 40
_FOCUS_CONFIRM_PAUSE_S = 0.04
_FOCUS_CONFIRM_REGRAB = 10


def web_entry_context(acc):
    """Capture a web entry's identity before focus can detach its proxy."""
    role = _role_name(acc)
    name = _call_first(acc, ("get_name",), default="")
    if role not in {"entry", "text", "search box", "combo box", "combo-box"} or not name:
        return None
    chain = list(_ancestor_chain(acc))
    doc = next((n for n in chain if _role_name(n) in {"document web", "document frame"}), None)
    window = next((n for n in chain if _role_name(n) in {"frame", "window"}), None)
    if doc is None or window is None:
        return None
    parent = _parent_of(acc)
    box = _extents(acc)
    return role, name, doc, window, parent, box, _ancestor_key(doc), _ancestor_key(window)


def focus_web_entry(acc, *, context=None, request_focus: bool = True):
    """Focus a web entry, allowing only a detached, same-context remount.

    Capture identity before focus: a detached proxy can lose its ancestry.
    This exception is restricted to the focus we request here; it does not
    relax ordinary ref resolution or authorize writes to arbitrary focus.
    """
    context = context if context is not None else web_entry_context(acc)
    if context is None:
        return acc
    role, name, doc, window, parent, box, doc_key, window_key = context
    if request_focus:
        grab_focus(acc)
    for attempt in range(_FOCUS_CONFIRM_POLLS):
        # A startup placeholder may briefly report focus before its async
        # handler mounts the editable input (Wikipedia's header does this).
        if _focus_on_target(acc) is True and _state_has(acc, "EDITABLE"):
            return acc
        # Collection is document-local, so browser chrome and other tabs
        # cannot become candidates, even if they carry the same name.
        hits = _focused_accessibles(doc)
        if len(hits) == 1:
            candidate = hits[0]
            _call_first(candidate, ("clear_cache", "clearCache"))
            parents = list(_ancestor_chain(candidate))
            live_doc = next((n for n in parents if _role_name(n) in {"document web", "document frame"}), None)
            live_window = next((n for n in parents if _role_name(n) in {"frame", "window"}), None)
            _call_first(parent, ("clear_cache", "clearCache"))
            # Do not redirect to an existing peer field: prove detachment.
            detached = _state_has(acc, "DEFUNCT")
            if not detached and parent is not None:
                count = _child_count(parent)
                detached = 0 <= count <= 64 and all(
                    (child := _child_at(parent, i)) is not None
                    and _ancestor_key(child) != _ancestor_key(acc)
                    for i in range(count)
                )
            pos, size = box
            cpos, csize = _extents(candidate)
            overlap = (pos is not None and size is not None and cpos is not None and csize is not None
                       and min(pos[0]+size[0], cpos[0]+csize[0]) > max(pos[0], cpos[0])
                       and min(pos[1]+size[1], cpos[1]+csize[1]) > max(pos[1], cpos[1]))
            if (detached and live_doc is not None and live_window is not None
                    and _ancestor_key(live_doc) == doc_key
                    and _ancestor_key(live_window) == window_key
                    and _state_has(live_doc, "SHOWING")
                    and _state_has(live_window, "SHOWING")
                    and _role_name(candidate) == role
                    and _call_first(candidate, ("get_name",), default="") == name
                    and _state_has(candidate, "EDITABLE")
                    and _state_has(candidate, "SHOWING")
                    and _state_has(candidate, "FOCUSED")
                    and not is_secure(candidate) and not _blocks_text_replace(candidate)
                    and overlap):
                _invalidate_tree_cache(parent)
                return candidate
        if attempt + 1 < _FOCUS_CONFIRM_POLLS:
            time.sleep(_FOCUS_CONFIRM_PAUSE_S)
    raise _focus_lost_error("focus is not on the target or a verified replacement; nothing was typed")


def _focus_lost_error(message: str):
    """Refused set_value: focus is not on the target, so no key was sent."""
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    return ComputerUseError(
        ErrorCode.FOCUS_LOST,
        message,
        detail={
            "platform": "linux",
            "reason": "focus_lost",
            "outcome": "refused",
            "next": ["ref", "cdp"],
            "evidence": message,
        },
    )


def _focus_candidates(acc, *, embedded: bool) -> list:
    """Children that can hold the caret for ``acc``.

    Indexed children cover a paragraph or static text under a contenteditable
    section. Hypertext covers a text node Chrome embeds as U+FFFC and does
    not also return from ``get_child_at_index``. Only the top of the walk
    asks for those embeds; a nested call already came from one.
    """
    found = []
    count = _child_count(acc)
    if count > 0:
        for index in range(min(count, 16)):
            found.append(_child_at(acc, index))
    if embedded:
        for obj in _hypertext_objects(acc):
            found.append(obj)
    return found


def _descendant_focused(
    acc, depth: int = 0, budget: list | None = None, seen: set | None = None,
) -> bool:
    """True when a descendant, not ``acc`` itself, has ``FOCUSED``.

    A roleless contenteditable is a section. Chrome reports ``FOCUSED`` on
    that section or on a child text node: an indexed paragraph or static
    text, or a hypertext embed. A focused node inside this subtree counts.
    A focused node outside it does not.
    """
    if depth > 3:
        return False
    if budget is None:
        budget = [24]
    if seen is None:
        seen = set()
    for child in _focus_candidates(acc, embedded=(depth == 0)):
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        if child is None or id(child) in seen:
            continue
        seen.add(id(child))
        _call_first(child, ("clear_cache", "clearCache"))
        if _state_has(child, "FOCUSED"):
            return True
        if _descendant_focused(child, depth + 1, budget, seen):
            return True
    return False


def _focus_on_target(acc) -> bool | None:
    """Whether keyboard focus is on ``acc`` or a descendant inside it.

    True: the target has ``FOCUSED``, or a descendant does. A roleless
    contenteditable section counts when focus is on the section itself or
    on a child text node.
    False: the node can report focus and it is elsewhere or absent.
    None: the node has no state set, so focus cannot be read. That is a
    test double. A live control has a state set; unknown focus there is
    False, and the caller does not type.
    """
    if _call_first(acc, ("get_state_set",)) is None:
        return None
    _call_first(acc, ("clear_cache", "clearCache"))
    if _state_has(acc, "FOCUSED"):
        return True
    if _descendant_focused(acc):
        return True
    return False


def _confirm_focus_on_target(acc) -> bool:
    """Poll until ``acc`` is focused. True when focus cannot be read at all.

    The first polls are the short wait after the caller's grab_focus. When
    that wait misses, grab_focus is sent again and the poll continues.
    Keys are still not sent when focus never arrives.
    """
    for attempt in range(_FOCUS_CONFIRM_POLLS):
        seen = _focus_on_target(acc)
        if seen is None or seen is True:
            return True
        if attempt + 1 == _FOCUS_CONFIRM_REGRAB:
            _request_qt_focus_activation(acc)
            grab_focus(acc)
        if attempt + 1 < _FOCUS_CONFIRM_POLLS:
            time.sleep(_FOCUS_CONFIRM_PAUSE_S)
    return False


def _request_qt_focus_activation(acc) -> None:
    """Request the missing window activation before retrying Qt child focus.

    Qt's child-widget GrabFocus calls setFocus without activateWindow. An
    inactive window therefore remembers the widget but cannot give it input
    focus. Send one WM request; the existing FOCUSED polling remains the
    success signal and the only wait. Do not restore hidden windows here.
    """
    if not _qt_app(acc) or not _x11_keys_available():
        return
    target = toplevel_raise_target(acc)
    if target is None:
        return
    from a11y_computer_use.drivers import _linux_system

    _active, title, pid, extents = target
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return
    # The general matcher allows title/geometry fallbacks for clients without
    # PID metadata. Focus recovery must stay within this target's process.
    rows = [row for row in _linux_system.windows() if row.get("pid") == pid]
    window_id = _linux_system.window_id_for_toplevel(
        title=title, pid=pid, extents=extents, rows=rows,
    )
    if window_id is not None:
        _linux_system.request_input_activation(window_id)


def _type_into_target(acc, text: str) -> None:
    """Type ``text`` only while ``acc`` is focused. One character is one batch.

    The first batch waits briefly for focus to arrive after a click. Each
    later batch re-checks immediately and sends nothing when focus has moved.
    A node that cannot report focus is typed as one batch, which is the
    older test-double path.
    """
    _refuse_chromium_tab_text(acc, text)
    if text == "":
        return
    if not _confirm_focus_on_target(acc):
        raise _focus_lost_error("focus is not on the target; nothing was typed")
    if _focus_on_target(acc) is None:
        _type_string(text)
        return
    sent = False
    for ch in text:
        if _focus_on_target(acc) is False:
            if sent:
                raise _focus_lost_error(
                    "focus left the target; stopped before the next keystroke"
                )
            raise _focus_lost_error("focus is not on the target; nothing was typed")
        _type_string(ch)
        sent = True


def _chord_into_target(acc, chord: str) -> None:
    """Press ``chord`` only when ``acc`` is focused. One chord is one batch."""
    if not _confirm_focus_on_target(acc):
        raise _focus_lost_error("focus is not on the target; nothing was typed")
    if _focus_on_target(acc) is False:
        raise _focus_lost_error("focus is not on the target; nothing was typed")
    from a11y_computer_use.drivers import _linux_input

    _linux_input.press_chord(chord)


def _shown_matches(acc, text: str) -> bool:
    """True when the readable text is ``text``.

    NBSP is a space. One trailing newline is the empty contenteditable's
    ``<br>`` and is not part of the value. U+FFFC is expanded before the
    comparison, so a paragraph child counts.
    """
    raw = _full_text(acc)
    if _texts_match(raw, text):
        return True
    shown = _readable_text(acc)
    if shown is None:
        return False
    if _texts_match(shown, text):
        return True
    if shown.endswith("\n") and _texts_match(shown[:-1], text):
        return True
    return False


def _confirm_text_landed(acc, text: str) -> bool:
    """Like ``_confirm_text``, with a longer wait for a key event to land.

    The comparison is the readable text: NBSP is a space, and U+FFFC is the
    child text. A Firefox contenteditable updates a beat after the key.
    """
    for attempt in range(16):
        if _shown_matches(acc, text):
            return True
        if attempt + 1 < 16:
            time.sleep(0.05)
    return False


def chromium_contenteditable_type(acc, text: str) -> bool | None:
    """Type ``text`` into a Chrome contenteditable and wait for the read.

    ``None`` means this is not the path: ``acc`` is not a Chrome
    contenteditable, or XTEST cannot reach the session. ``True`` means a
    settled read is the previous text with an active selection replaced, or
    the typed text inserted at a collapsed caret. ``False`` means the read
    settled on something else. That is a mismatch, not a success. NBSP is a
    space and one trailing newline is the ``<br>``. An input, a textarea,
    and a GTK field are not this path. ``last_expected`` is the replacement
    string when a selection was replaced, else None.
    """
    chromium_contenteditable_type.last_expected = None
    if not _chromium_contenteditable(acc) or not _x11_keys_available():
        return None
    before = _readable_text(acc)
    selection = _selection_span(acc, before)
    grab_focus(acc)
    _type_string(text)
    after = _poll_typed_text(
        lambda: _readable_text(acc), before, text, chrome=True, selection=selection,
    )
    landed = bool(_typed_visible(before, after, text, chrome=True, selection=selection))
    if landed:
        chromium_contenteditable_type.last_expected = _matched_replacement(
            before, after, text, selection, chrome=True,
        )
    return landed


chromium_contenteditable_type.last_expected = None


def focus_and_type_into(acc, text: str) -> bool:
    """Focus ``acc`` and insert ``text`` with key events.

    True only when a later read of this field contains ``text``. A Firefox
    web entry's EditableText insert returns true and leaves the field empty;
    the same key events ``key`` already delivers do land once the entry has
    focus. The read-back is the expanded text, so a NBSP is a space and a
    contenteditable whose parent string stays U+FFFC still shows the words
    in its children. The read is polled: Firefox applies the keys a beat
    after they are sent. No EditableText is not this path: the caller types
    into whatever is focused and uses that read-back.
    """
    if not _x11_keys_available():
        return False
    grab_focus(acc)
    before = _readable_text(acc)
    if before is None:
        return False
    _type_string(text)
    for attempt in range(16):
        after = _readable_text(acc)
        if _typed_visible(before, after, text):
            return True
        if attempt + 1 < 16:
            time.sleep(0.05)
    return False


def _focus_and_replace(acc, text: str) -> bool:
    """Focus ``acc``, replace its text with key events, and read it back.

    True only when the readable text equals ``text``. An empty field,
    including one whose snapshot read is a newline, is focused and typed.
    A field that still has other text is cleared first; if that clear does
    not stick, nothing is typed on top of it. The caller puts the original
    text back when this returns False, so a contenteditable is not left empty.
    """
    if not _x11_keys_available():
        return False
    grab_focus(acc)
    current = _full_text(acc)
    if _shown_matches(acc, text):
        return True
    if current is None:
        return False
    if not _text_is_blank(current):
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            return False
    _type_into_target(acc, text)
    return _confirm_text_landed(acc, text)


def _replace_with_keys(acc, text: str) -> bool:
    """Clear the field and type ``text``. Used when EditableText is missing.

    Chromium's ATK objects do not implement AtkEditableText, so
    ``get_editable_text`` is absent and the runtime would otherwise type the
    new string onto the old one and still report success. Returns True only
    when the snapshot read equals ``text``. An unreadable field is not typed
    into. Native Wayland has no XTEST, so this returns False there.

    A contenteditable that fails the read-back is not left empty. The clear
    is what erases "Hello world"; if the new text cannot be verified, the
    original text is typed back.
    """
    current = _full_text(acc)
    if _texts_match(current, text):
        return True
    if current is None or not _x11_keys_available():
        return False
    original = current
    if not _text_is_blank(current):
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            _restore_text(acc, original)
            return False
    else:
        grab_focus(acc)
    _type_into_target(acc, text)
    if _confirm_text(acc, text):
        return True
    _restore_text(acc, original)
    return False


def _restore_text(acc, original: str | None) -> None:
    """Put ``original`` back when a write cannot be verified.

    No-op when the field already shows that text, or when XTEST cannot reach
    the session. A blank original stays blank. A field that still holds the
    original is not cleared.
    """
    if original is None or not _x11_keys_available():
        return
    now = _full_text(acc)
    if now == original or _texts_match(now, original):
        return
    if not _text_is_blank(now):
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            return
    else:
        grab_focus(acc)
    if not _text_is_blank(original):
        _type_into_target(acc, original)


def _clear_text(acc, eti, current: str) -> bool:
    """Remove ``current`` so the next write substitutes.

    The deleted range is ``len(current)`` from the snapshot read, not
    ``character_count``. ``delete_text`` is not trusted on its return value:
    the AT-SPI editable-text adaptor reports true after the call whether or
    not the text changed. When the snapshot read still has text, and this is
    an X11 session, the field is focused and ctrl+a, BackSpace is sent.
    """
    if current == "":
        return True
    _select_range(acc, len(current))
    _call_first(eti, ("delete_text",), 0, len(current), default=False)
    if _wait_until_gone(acc):
        return True
    if not _x11_keys_available():
        return False
    _x11_select_all_and_delete(acc)
    return _wait_until_gone(acc)


def _character_count(acc) -> int | None:
    """``Text.get_character_count``, or None when that read fails."""
    count = _safe(lambda: _atspi().Text.get_character_count(acc))
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return int(count)
    return None


def _qt_text_count_matches(acc, text: str) -> bool:
    """True when a Qt field's count equals the UTF-16 length of ``text``.

    GTK is not checked here. Qt's ``InsertText`` can append a NUL and more
    characters when the length was a byte count. ``GetText`` is a D-Bus
    string and stops at the NUL, so the text compares equal while the
    character count is longer than the string that was asked for.
    """
    if not _qt_app(acc):
        return True
    return _character_count(acc) == _utf16_length(text)


def _content_is_blank(acc) -> bool:
    """True when the field a person reads has no text.

    An empty Chrome contenteditable reads back as a newline (the ``<br>``),
    a space, a NBSP, or NULL from ``get_text(0, -1)``. U+FFFC is expanded
    first, so a parent whose children still hold words is not blank.
    """
    raw = _full_text(acc)
    if isinstance(raw, str) and _OBJECT_REPLACEMENT in raw:
        shown = _readable_text(acc)
        if shown is not None:
            return _text_is_blank(shown)
    return _text_is_blank(raw)


def _confirm_text(acc, text: str) -> bool:
    if is_secure(acc):
        return False
    for attempt in range(_TEXT_CONFIRM_POLLS):
        shown = _full_text(acc)
        blank = text == "" and (_text_is_blank(shown) or _content_is_blank(acc))
        if (blank or _verification_matches(acc, text) or writer_document_matches(acc, text)) and _qt_text_count_matches(acc, text):
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    return False


_DATE_INPUT_TYPES = frozenset({"date", "time", "month", "week", "datetime-local"})
_MONTH_NAMES = (
    "", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_DATE_SEGMENT_POLLS = 16
_DATE_SEGMENT_PAUSE_S = 0.05


def _refuse_gecko_date_segment_replace(acc) -> None:
    """Refuse Firefox date/time segment replacement before any mutation.

    These spin buttons advertise EditableText and Value, but generic text
    replacement can clear the segment and emit an empty input/change value.
    Time inputs use a panel parent, not the date editor role. Identify the
    native input type, never localized segment names or displayed digits.
    """
    if _role_name(acc) != "spin button" or not _gecko_app(acc):
        return
    for node in _ancestor_chain(acc, 8):
        role = _role_name(node)
        attrs = _get_attributes(node)
        kind = str(attrs.get("text-input-type") or "").lower()
        if role == "date editor" or (
            str(attrs.get("tag") or "").lower() == "input"
            and kind in _DATE_INPUT_TYPES
        ):
            raise _text_mismatch(
                "unsafe_date_segment_replace",
                "Firefox date/time segments cannot safely be replaced with set_value; "
                "no value was written. Use type(ref, digits) and verify the complete date/time",
            )
        if role in {"document web", "document frame", "frame", "application"}:
            break


def _chrome_date_segment(acc) -> bool:
    """True for one segment of a Chrome date, time, or month input.

    The role is ``spin button``. The Value interface is not the segment.
    On Chrome 148 ``set_current_value`` leaves an empty segment at 0.0.
    On the Chrome that filed #146 the year read back as ``171994.0`` after
    the day and the year were written. A number input and a GTK spin button
    are not this. ``week`` and ``datetime-local`` use the same editor.
    """
    try:
        if _role_name(acc) != "spin button" or _number_input(acc) or not _chromium_app(acc):
            return False
    except Exception:
        return False
    node = acc
    for _ in range(8):
        parent = _parent_of(node)
        if parent is None:
            return False
        if _role_name(parent) == "date editor":
            kind = str(_get_attributes(parent).get("text-input-type") or "").lower()
            return kind in _DATE_INPUT_TYPES or kind == ""
        node = parent
    return False


def _date_segment_text(acc) -> str | None:
    """The segment's displayed text, or None when it is unset.

    Chrome publishes ``valuetext``. An unset segment is ``"0"``. A minute
    the user set to zero is ``"00"``. ``"0"`` is not a value the field holds.
    """
    raw = _get_attributes(acc).get("valuetext")
    if not isinstance(raw, str):
        return None
    shown = raw.strip()
    if shown in {"", "0"}:
        return None
    return shown


def _segment_matches(requested: str, shown: str | None) -> bool:
    """True when ``shown`` is the segment ``requested`` asked for.

    Compared as text. ``"03"`` matches ``"3"``. ``"03"`` matches ``"March"``
    because a month input displays the month name. The Value interface's
    float is not an argument to this comparison.
    """
    if requested == "":
        return shown is None
    if shown is None:
        return False
    want = requested.strip()
    got = shown.strip()
    if want.casefold() == got.casefold():
        return True

    def _whole(text: str) -> float | None:
        try:
            number = float(text)
        except ValueError:
            return None
        if abs(number - round(number)) >= 1e-9:
            return None
        return number

    left = _whole(want)
    right = _whole(got)
    if left is not None and right is not None and left == right:
        return True
    if left is not None:
        index = int(round(left))
        if 1 <= index <= 12 and got.casefold() == _MONTH_NAMES[index]:
            return True
    return False


def _set_chrome_date_segment(acc, value: str) -> bool:
    """Type ``value`` into one date, time, or month segment.

    Success is ``valuetext``, not ``Value.get_current_value``. A float such
    as ``171994.0`` is never the read-back. An empty string clears the
    segment. Raises ``text_mismatch`` with the displayed text when the
    segment does not show ``value``.
    """
    if _segment_matches(value, _date_segment_text(acc)):
        return True
    if not _x11_keys_available():
        shown = _date_segment_text(acc) or ""
        raise _text_mismatch(
            "text_mismatch",
            f"the value read back {shown!r} does not match {value!r}",
            expected=value,
            actual=shown,
        )
    grab_focus(acc)
    _click_center(acc)
    if value == "":
        _chord_into_target(acc, "ctrl+a")
        _chord_into_target(acc, "backspace")
        _chord_into_target(acc, "delete")
    else:
        _type_into_target(acc, value)
    for attempt in range(_DATE_SEGMENT_POLLS):
        if _segment_matches(value, _date_segment_text(acc)):
            return True
        if attempt + 1 < _DATE_SEGMENT_POLLS:
            time.sleep(_DATE_SEGMENT_PAUSE_S)
    shown = _date_segment_text(acc) or ""
    raise _text_mismatch(
        "text_mismatch",
        f"the value read back {shown!r} does not match {value!r}",
        expected=value,
        actual=shown,
    )


def _number_input(acc) -> bool:
    """True when object attributes say this is ``<input type=number>``."""
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or "").lower()
    kind = str(
        attrs.get("text-input-type")
        or attrs.get("html-input-type")
        or attrs.get("input-type")
        or attrs.get("type")
        or ""
    ).lower()
    return tag in {"", "input"} and kind == "number" and (
        tag == "input" or "text-input-type" in attrs or "html-input-type" in attrs
    )


def _value_interface_present(acc) -> bool:
    """True when ``acc`` implements Value.

    ``get_value_iface`` is how AT-SPI reports the interface. None means it
    is missing, and minimum and maximum must not be read: that read returns
    an uninitialized double (about ``6.93e-310``) after the bridge logs that
    the object is not a Value. A test double with no ``get_value_iface``
    method is not that report. Its numbers still come from ``Atspi.Value``.
    """
    if acc is None:
        return False
    getter = getattr(acc, "get_value_iface", None)
    if not callable(getter):
        return True
    return _safe(lambda: getter()) is not None


def _raw_value_bounds(acc) -> tuple[float, float] | None:
    """Minimum and maximum as read, or None when either is not a finite number.

    This does not decide that the Value interface exists. A missing
    interface has to be rejected before this read.
    """
    Atspi = _atspi()
    minimum = _safe(lambda: Atspi.Value.get_minimum_value(acc))
    maximum = _safe(lambda: Atspi.Value.get_maximum_value(acc))
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
        return None
    if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
        return None
    low, high = float(minimum), float(maximum)
    if not math.isfinite(low) or not math.isfinite(high):
        return None
    return low, high


def _uninitialized_bounds(low: float, high: float) -> bool:
    """True when either end is the uninitialized double a missing Value returns."""
    return _uninitialized_double(low) or _uninitialized_double(high)


def _value_range(acc) -> tuple[float, float] | None:
    """(minimum, maximum) from the Value interface, or None when it has none.

    A missing interface is None and is not read. An uninitialized double is
    also None: it is not a range a number can be checked against.
    """
    if not _value_interface_present(acc):
        return None
    span = _raw_value_bounds(acc)
    if span is None or _uninitialized_bounds(*span):
        return None
    return span


def _parse_number(value: str) -> float:
    text = str(value).strip()
    if text.lower() in {"", "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
        raise ValueError(text)
    number = float(text)
    if not math.isfinite(number):
        raise ValueError(text)
    return number


def _numbers_match(got: float, wanted: float) -> bool:
    return abs(float(got) - float(wanted)) <= 1e-6 * max(1.0, abs(wanted))


def _format_bound(number: float) -> str:
    return format(number, "g")


def _outside_range(number: float, low: float, high: float, *, open_upper: bool) -> bool:
    """True when ``number`` is below the minimum or above a real maximum."""
    if number < low:
        return True
    return not open_upper and number > high


def _range_message(value: str, low: float, high: float, *, open_upper: bool) -> str:
    if open_upper:
        return f"value {value!r} is below the minimum {_format_bound(low)}"
    return f"value {value!r} is outside {_format_bound(low)}..{_format_bound(high)}"

# Calc cell name. A1, B2, AA10. Not a calendar day ("15") and not row 0.
_SHEET_ADDRESS = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*$")
_CELL_EDITOR_PANEL = re.compile(r"^Cell\s+([A-Z]{1,3}[1-9][0-9]*)$")
_SHEET_SCAN_ROWS = 32
_SHEET_SCAN_COLS = 16
_SHEET_KEEP = 96
# On-screen Calc window the snapshot will list. 64×32 covers a tall display
# at the default row height (a 1280×800 sheet shows about 29 rows). The
# pruner keeps SHEET_MAX_CHILDREN cells per band and MAX_CHILDREN bands
# before the dense cap, and 64×32 stays inside that. A million-row sheet
# is not walked.
_SHEET_VIEW_MAX_ROWS = 64
_SHEET_VIEW_MAX_COLS = 32
_LO_COMMS = frozenset({"soffice", "soffice.bin", "oosplash"})


def _sheet_formula(acc) -> str | None:
    """The Formula attribute, or None when the cell has none.

    LibreOffice stores ``=B1*2`` as ``B1*2``. An empty attribute is not a formula.
    """
    attrs = _get_attributes(acc)
    for key, val in attrs.items():
        if str(key).lower() != "formula":
            continue
        text = str(val or "").strip()
        return text or None
    return None


def _sheet_formula_text(acc) -> str | None:
    """Formula text for a snapshot, with the leading ``=`` restored."""
    formula = _sheet_formula(acc)
    if not formula:
        return None
    if formula.startswith("="):
        return formula
    return "=" + formula


def is_sheet_cell(acc) -> bool:
    """True for a Calc cell: address title, or a non-empty Formula attribute.

    A GTK tree cell and an HTML table cell are not addresses, so they stay
    on the path they already had. A Writer text-table cell is also named
    like an address. ``writer_text_cell`` tells those apart: its parent is
    a small table, not a spreadsheet.
    """
    if _role_name(acc) != "table cell":
        return False
    if _SHEET_ADDRESS.match(_node_name(acc)):
        return True
    return _sheet_formula(acc) is not None


def _writer_paragraphs(acc) -> list:
    """Paragraph and heading children. The cell node itself has no text."""
    found = []
    count = _child_count(acc)
    if count < 0:
        count = 0
    for index in range(min(count, 16)):
        child = _child_at(acc, index)
        if child is not None and _role_name(child) in {"paragraph", "heading"}:
            found.append(child)
    return found


def writer_text_cell(acc) -> bool:
    """A Writer table cell whose text lives in a child paragraph.

    The name is a spreadsheet address, so ``is_sheet_cell`` is true, and
    typing into the cell inserts a new paragraph in front of the old text.
    A Calc cell's parent table is a spreadsheet. This is not that cell.
    """
    if not is_sheet_cell(acc):
        return False
    parent = _parent_of(acc)
    if parent is None or _role_name(parent) != "table":
        return False
    if _spreadsheet_table(parent):
        return False
    return bool(_writer_paragraphs(acc))


# How long a Writer cell may take to publish the paragraph that set_text
# just replaced. The first reads often still show the old line, or the new
# line plus the old paragraph, while the view rebuilds that child. A stable
# wrong sample inside this window is not a miss.
_WRITER_CELL_SETTLE_S = 1.5


def writer_cell_text(acc) -> str:
    """The paragraph text of a Writer table cell, one line per paragraph."""
    parts = []
    for child in _writer_paragraphs(acc):
        text = _full_text(child)
        if text:
            parts.append(text)
    return "\n".join(parts)


def is_writer_document(acc) -> bool:
    """A LibreOffice Writer document. Its text lives in paragraph children.

    A GTK text view and a Mousepad buffer are not this node. Their own text
    interface is the value.
    """
    try:
        if _role_name(acc) != "document text":
            return False
    except Exception:
        return False
    app = _call_first(acc, ("get_application", "getApplication"))
    if app is None:
        return False
    name = str(_call_first(app, ("get_name",), default="") or "")
    return libreoffice_app(name)


def _document_paragraphs(acc) -> list:
    """Paragraph and heading children of a Writer document, in order.

    Empty paragraphs stay, so two breaks are not read back as one. Bounded
    so a document is not walked without a limit.
    """
    found = []
    count = _child_count(acc)
    if count < 0:
        count = 0
    for index in range(min(count, 256)):
        child = _child_at(acc, index)
        if child is not None and _role_name(child) in {"paragraph", "heading"}:
            found.append(child)
    return found


def _paragraph_line(text: str) -> str:
    """One paragraph's text, without the end mark the paragraph itself is.

    The join between paragraphs is the break. A separator at the end of the
    paragraph text is that same break, so it is not a second one. A break
    before that end mark stays. U+2028 inside the paragraph is a line break,
    not the next paragraph, so it is not turned into ``\\n``.
    """
    from a11y_computer_use.outcome import normalize_paragraph_breaks

    raw = text or ""
    if raw.endswith("\u2028"):
        raw = raw[:-1]
    token = "\ue000"
    body = normalize_paragraph_breaks(raw.replace("\u2028", token))
    if body.endswith("\n"):
        body = body[:-1]
    return body.replace(token, "\u2028")


def writer_document_text(acc) -> str | None:
    """Paragraph text of a Writer document, one line per paragraph.

    None when ``acc`` is not that document. An empty document is ``""``,
    not the node's Value interface. The snapshot uses this string, so a
    done check can see the lines. A separator inside a paragraph is kept.
    """
    if not is_writer_document(acc):
        return None
    # libatspi answers get_child_at_index from its cache. The first read
    # after a write can be the empty document or only the first paragraph,
    # and a later poll would keep that list until the cache is dropped.
    _call_first(acc, ("clear_cache", "clearCache"))
    paragraphs = _document_paragraphs(acc)
    if not paragraphs:
        return ""
    return "\n".join(_paragraph_line(_full_text(child) or "") for child in paragraphs)


def writer_document_matches(acc, text: str) -> bool:
    """True when the Writer paragraphs are ``text``, whatever the separator."""
    shown = writer_document_text(acc)
    if shown is None:
        return False
    return paragraph_breaks_match(shown, text)


def _writer_document_type_target(acc):
    """The Writer document a type into ``acc`` lands in, or None.

    A paragraph of that document counts. A table cell does not: a newline
    there belongs to the cell, not the document body.
    """
    node = acc
    for _ in range(8):
        if node is None:
            return None
        try:
            role = _role_name(node)
        except Exception:
            return None
        if role == "table cell":
            return None
        if is_writer_document(node):
            return node
        node = _parent_of(node)
    return None


def _type_wanted_paragraphs(text: str) -> list[str]:
    """Paragraphs ``text`` asks for. ``\\n`` is a paragraph. U+2028 is not.

    CR, CRLF, VT, FF, NEL, U+2029, and U+FFFC are paragraph breaks here, the
    same as ``\\n``. U+2028 stays inside its paragraph: the caller asked for
    a line break.
    """
    import re

    normalized = (text or "").replace("\u00a0", " ").replace("\r\n", "\n")
    normalized = re.sub(r"[\r\u000b\u000c\u0085\u2029\ufffc]", "\n", normalized)
    return normalized.split("\n")


def _type_paragraph_body(text: str) -> str:
    """One paragraph as type read it, keeping an internal line break.

    One trailing separator is the paragraph end. U+2028 and ``\\n`` before
    that end stay, so a line break inside the paragraph is not a new one.
    """
    body = (text or "").replace("\u00a0", " ")
    if body.endswith("\r\n"):
        return body[:-2]
    if body and body[-1] in "\n\r\u000b\u000c\u0085\u2029\ufffc\u2028":
        return body[:-1]
    return body


def _type_shown_paragraphs(acc) -> list[str] | None:
    """Paragraph bodies of a Writer document. None for any other node.

    U+2028 inside a paragraph stays U+2028. The cache is dropped first.
    """
    if not is_writer_document(acc):
        return None
    _call_first(acc, ("clear_cache", "clearCache"))
    return [
        _type_paragraph_body(_full_text(child) or "")
        for child in _document_paragraphs(acc)
    ]


def _type_lines_match(shown: list[str], wanted: list[str]) -> bool:
    """True when ``shown`` is exactly the paragraphs ``wanted`` asked for.

    One empty paragraph the document already had, before or after the typed
    lines, does not hide a match. A line break inside one paragraph does
    not count as those paragraphs.
    """
    from a11y_computer_use.outcome import typed_breaks_match

    def same(left: list[str], right: list[str]) -> bool:
        return len(left) == len(right) and all(
            typed_breaks_match(got, wanted_line) for got, wanted_line in zip(left, right)
        )

    if same(shown, wanted):
        return True
    if wanted and (same(shown, [""] + wanted) or same(shown, wanted + [""])):
        return True
    return False


def _writer_conflict_lines(shown: list[str], wanted: list[str]) -> bool:
    """True when the characters match but paragraph and line break do not.

    One paragraph ``a\\u2028b`` is not the two paragraphs ``a`` and ``b``.
    Two paragraphs are not the line break the caller asked for with U+2028.
    """
    import re

    if _type_lines_match(shown, wanted):
        return False
    folded_shown = "\n".join(part.replace("\u2028", "\n") for part in shown)
    folded_wanted = "\n".join(part.replace("\u2028", "\n") for part in wanted)
    if folded_shown == folded_wanted:
        return True
    pieces = []
    for part in shown:
        pieces.extend(re.split(r"\u2028|\n", part) if part else [""])
    return _type_lines_match(pieces, [
        piece
        for part in wanted
        for piece in (re.split(r"\u2028|\n", part) if part else [""])
    ]) and shown != wanted


def writer_type_readback(acc) -> str | None:
    """Document text with a line break kept distinct from a paragraph join."""
    shown = _type_shown_paragraphs(acc)
    if shown is None:
        return None
    return "\n".join(part.replace("\n", "\u2028") for part in shown)


def writer_type_conflict(acc, text: str) -> bool:
    """True when ``text`` landed with the wrong paragraph or line-break shape."""
    shown = _type_shown_paragraphs(acc)
    if shown is None:
        return False
    return _writer_conflict_lines(shown, _type_wanted_paragraphs(text))


def _writer_type_landed(document, text: str) -> bool:
    """Poll until Writer paragraphs match ``text``, or the shape is wrong.

    A later paragraph that is not published yet is not a miss. A stable
    read that has the characters as line breaks inside one paragraph is a
    miss: that is not the paragraphs ``\\n`` asked for. U+2028 in ``text``
    has to stay a line break inside its paragraph.
    """
    wanted = _type_wanted_paragraphs(text)
    deadline = time.monotonic() + _WRITER_DOCUMENT_DEADLINE_S
    last: object = object()
    stable_since = None
    while True:
        shown = _type_shown_paragraphs(document)
        now = time.monotonic()
        if shown != last:
            last = shown
            stable_since = now
        held = (
            stable_since is not None
            and (now - stable_since) >= _WRITER_DOCUMENT_STABLE_S
        )
        if held and shown is not None and _type_lines_match(shown, wanted):
            return True
        if held and shown is not None and _writer_conflict_lines(shown, wanted):
            return False
        if (
            held
            and shown is not None
            and len(shown) >= len(wanted)
            and not _type_lines_match(shown, wanted)
        ):
            return False
        if now >= deadline:
            return False
        time.sleep(0.05)


def _insert_writer_piece(acc, eti, piece: str) -> None:
    """Insert ``piece`` at the caret. ``piece`` has no newline and no U+2028."""
    if piece == "":
        return
    current = _full_text(acc)
    if current is None:
        current = ""
    caret, _start, _end = _caret_and_selection(acc, len(current))
    insert = None
    for name in ("insert_text", "insertText"):
        insert = getattr(eti, name, None)
        if insert is not None:
            break
    length = _insert_length(insert, piece, acc) if insert is not None else len(piece)
    _call_first(
        eti, ("insert_text", "insertText"), int(caret), piece, int(length), default=False,
    )


def _press_writer_line_break(acc) -> None:
    """Shift+Return. That is a line break inside the paragraph, not a new one."""
    if _focus_on_target(acc) is False:
        raise _focus_lost_error("focus is not on the target; nothing was typed")
    from a11y_computer_use.drivers import _linux_input

    _linux_input.press_chord("shift+return")


def _insert_writer_segment(acc, eti, segment: str) -> None:
    """Insert one paragraph. U+2028 in ``segment`` is a line break."""
    pieces = segment.split("\u2028")
    for index, piece in enumerate(pieces):
        if piece:
            _insert_writer_piece(acc, eti, piece)
        if index + 1 < len(pieces):
            if _x11_keys_available():
                _press_writer_line_break(acc)
            else:
                _insert_writer_piece(acc, eti, "\u2028")


def _type_writer_paragraphs(acc, text: str, eti) -> int | None:
    """Type ``text`` so each ``\\n`` is Return, then read the paragraphs back.

    U+2028 stays Shift+Return. ``insertText`` of a newline is not used: in
    Writer that is a line break inside the current paragraph, and a read-back
    that treats it as the next paragraph would report success for the wrong
    document. When the characters landed that way anyway, the error says
    they were inserted as line breaks. None when the paragraphs are still
    what they were, so the caller may send keys.
    """
    document = _writer_document_type_target(acc)
    before = writer_document_text(document) if document is not None else None
    parts = text.split("\n")
    if len(parts) > 1 and not _x11_keys_available():
        raise _text_mismatch(
            "text_mismatch",
            "a newline in a Writer document needs Return to become a paragraph",
            expected=text,
            actual=writer_type_readback(document) if document is not None else None,
        )
    for index, part in enumerate(parts):
        _insert_writer_segment(acc, eti, part)
        if index + 1 < len(parts):
            _type_into_target(acc, "\n")
    if document is not None and _writer_type_landed(document, text):
        return len(text)
    actual = writer_type_readback(document) if document is not None else None
    if document is not None and writer_type_conflict(document, text):
        raise _text_mismatch(
            "text_mismatch",
            "inserted as line breaks",
            expected=text,
            actual=actual,
        )
    # The paragraphs did not change. The caller may send keys. A different
    # paragraph text is still a mismatch, and keys are not sent on top of it.
    if document is not None and (writer_document_text(document) or "") == (before or ""):
        return None
    raise _text_mismatch(
        "text_mismatch",
        "the field text after type does not match what was inserted",
        expected=text,
        actual=actual,
        inserted_chars=len(text),
        unchanged=False,
    )


def focused_writer_document(app: str):
    """The Writer document that has focus, or None."""
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        return None
    if truncated or acc is None:
        return None
    return _writer_document_type_target(acc)


def writer_type_landed(document, text: str) -> bool:
    """True when ``document`` now holds ``text`` as paragraphs, not line breaks."""
    if document is None or not is_writer_document(document):
        return False
    return _writer_type_landed(document, text)


# How long a Writer document may take to publish every paragraph. A 1.5s
# poll expired on a slow runner while later paragraphs were still absent.
# The first reads are empty or only the first line. A sample that is still
# changing is not the read-back.
_WRITER_DOCUMENT_DEADLINE_S = 6.0
_WRITER_DOCUMENT_STABLE_S = 0.25


def _writer_document_landed(acc, text: str) -> bool:
    """Poll until the paragraph text is stable, or the deadline passes.

    A Writer document publishes paragraphs one at a time. The first reads
    are empty or only the first line, and that partial text is not a miss.
    Success is a read that matches, after separator normalisation, and has
    then stayed unchanged. A match that arrives at the deadline still has
    to stay unchanged for the stable window. A stable read that already has
    every requested break and still differs is a miss. This does not clear
    the document: selecting it deletes a write whose later paragraphs are
    not up yet.
    """
    if not is_writer_document(acc):
        return False
    from a11y_computer_use.outcome import normalize_paragraph_breaks

    wanted_breaks = normalize_paragraph_breaks(text).count("\n")
    deadline = time.monotonic() + _WRITER_DOCUMENT_DEADLINE_S
    last = object()
    stable_since = None
    while True:
        shown = writer_document_text(acc)
        now = time.monotonic()
        if shown != last:
            last = shown
            stable_since = now
        held = (
            stable_since is not None
            and (now - stable_since) >= _WRITER_DOCUMENT_STABLE_S
        )
        matches = shown is not None and paragraph_breaks_match(shown, text)
        if held and matches:
            return True
        shown_breaks = normalize_paragraph_breaks(shown or "").count("\n")
        # Fewer breaks than requested means a later paragraph is not
        # published yet. Keep waiting. A full, stable, different value is
        # the read-back.
        if held and shown and shown_breaks >= wanted_breaks and not matches:
            return False
        # A partial read waits out the deadline. A match that is still
        # settling may cross it, for one stable window only.
        if now >= deadline and not matches:
            return False
        if now >= deadline + _WRITER_DOCUMENT_STABLE_S:
            return False
        time.sleep(0.05)


def _set_writer_document_text(acc, text: str) -> bool:
    """Write a Writer document, then wait until its paragraphs are stable.

    The document node often has no EditableText, and its text interface can
    be missing for a moment. The generic key replace treats that as a miss
    and deletes the field after a few hundredths of a second, which removes
    paragraphs that are not published yet. A newline is Return. A miss here
    does not select the document.
    """
    eti = _editable_iface(acc)
    if eti is not None:
        _call_first(eti, ("set_text_contents",), text, default=False)
        return _writer_document_landed(acc, text)
    if not _x11_keys_available():
        return False
    grab_focus(acc)
    _type_into_target(acc, text)
    return _writer_document_landed(acc, text)


def writer_document_outcome_text(requested: str, acc) -> str | None:
    """The requested string when a Writer document's paragraphs now hold it.

    None for any other node, and when a paragraph break is actually missing
    or extra. A line break inside one paragraph is not that paragraph break:
    the returned text keeps U+2028, so the outcome is not confirmed. The
    snapshot value is the same paragraph text.
    """
    if not requested or acc is None:
        return None
    document = acc if is_writer_document(acc) else _writer_document_type_target(acc)
    if document is None:
        return None
    shown = _type_shown_paragraphs(document)
    if shown is None:
        return None
    wanted = _type_wanted_paragraphs(requested)
    if _type_lines_match(shown, wanted):
        return requested
    if _writer_conflict_lines(shown, wanted):
        return writer_type_readback(document)
    if not writer_document_matches(document, requested):
        return None
    return requested


_WRITER_WALK_ROLES = frozenset({"document text", "paragraph", "heading"})


def writer_document_for(acc):
    """The Writer document ``acc`` is, or the one that contains it.

    None for a GTK field, a Calc cell, and a Mousepad buffer. The walk
    starts only from a document, paragraph, or heading, so typing into an
    entry does not climb the tree.
    """
    if acc is None:
        return None
    try:
        if _role_name(acc) not in _WRITER_WALK_ROLES:
            return None
    except Exception:
        return None
    node = acc
    seen: set[int] = set()
    for _ in range(16):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        try:
            if is_writer_document(node):
                return node
        except Exception:
            return None
        parent = _parent_of(node)
        if parent is None or parent is node:
            return None
        node = parent
    return None


def _writer_typed_visible(before: str | None, shown: str | None, text: str) -> bool:
    """True when a Writer document's paragraphs show ``text``.

    Paragraph and line separators compare as one break. An empty document
    that now holds the typed lines matches. A longer document that did not
    change does not, unless the paragraphs are exactly the request.
    """
    if shown is None:
        return False
    from a11y_computer_use.outcome import normalize_paragraph_breaks, typed_breaks_match

    if typed_breaks_match(shown, text) or paragraph_breaks_match(shown, text):
        return True
    norm_shown = normalize_paragraph_breaks(shown)
    norm_text = normalize_paragraph_breaks(text or "")
    norm_before = None if before is None else normalize_paragraph_breaks(before)
    return _typed_visible(norm_before, norm_shown, norm_text)


def poll_writer_document(doc, before: str | None, text: str) -> str | None:
    """Poll a Writer document until its paragraphs show ``text`` or settle.

    The first reads are empty or only the first line. A partial publish is
    not a miss. A stable read that already has every requested break and
    still does not show ``text`` is returned without waiting out the
    deadline. The return is the paragraph text, which may still lack
    ``text``. This does not clear the document.
    """
    if doc is None:
        return None
    from a11y_computer_use.outcome import normalize_paragraph_breaks

    wanted_breaks = normalize_paragraph_breaks(text or "").count("\n")
    deadline = time.monotonic() + _WRITER_DOCUMENT_DEADLINE_S
    last = object()
    stable_since = None
    shown = None
    while True:
        shown = writer_document_text(doc)
        now = time.monotonic()
        if shown != last:
            last = shown
            stable_since = now
        held = (
            stable_since is not None
            and (now - stable_since) >= _WRITER_DOCUMENT_STABLE_S
        )
        if held and _writer_typed_visible(before, shown, text):
            return shown
        shown_breaks = normalize_paragraph_breaks(shown or "").count("\n")
        if (
            held
            and shown
            and shown_breaks >= wanted_breaks
            and not _writer_typed_visible(before, shown, text)
        ):
            return shown
        if now >= deadline and not _writer_typed_visible(before, shown, text):
            return shown
        if now >= deadline + _WRITER_DOCUMENT_STABLE_S:
            return shown
        time.sleep(0.05)


def writer_type_before(app: str) -> str | None:
    """Paragraph text of the focused Writer document, before a type.

    ``""`` is an empty Writer document. None means focus is not in one, so
    the caller keeps the ordinary read-back.
    """
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        return None
    if truncated or acc is None:
        return None
    doc = writer_document_for(acc)
    if doc is None:
        return None
    text = writer_document_text(doc)
    return "" if text is None else text


def poll_focused_writer_type(app: str, before: str | None, text: str) -> str | None:
    """Settled paragraph text of the focused Writer document, or None."""
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        return None
    if truncated or acc is None:
        return None
    doc = writer_document_for(acc)
    if doc is None:
        return None
    return poll_writer_document(doc, before, text)


def writer_cell_outcome_text(requested: str, cell) -> str | None:
    """The requested string when all Writer cell paragraphs match exactly.

    The cell node's own text stays empty, so the snapshot value cannot
    confirm the write. None for a Calc cell and when the paragraph does
    not match.
    """
    if cell is None or not writer_text_cell(cell):
        return None
    if _writer_cell_parts(cell) == requested.split("\n"):
        return requested
    return None


def _writer_cell_parts(cell) -> list[str | None]:
    """Read every paragraph, retaining blanks and refusing truncated cells."""
    _drop_read_cache()
    _call_first(cell, ("clear_cache", "clearCache"))
    if _child_count(cell) > 16:
        raise _text_mismatch(
            "text_mismatch", "Writer cell exceeds the paragraph read limit",
        )
    children = _writer_paragraphs(cell)
    if len(children) != _child_count(cell):
        raise _text_mismatch("text_mismatch", "cannot read every Writer cell paragraph")
    return [_full_text(child) for child in children]


def _writer_cell_boundary(cell, paragraph, offset: int, chord: str) -> bool:
    """Send one boundary edit only at a verified caret inside this cell."""
    if not _x11_keys_available():
        return False
    Atspi = _atspi()
    grab_focus(paragraph)
    if not _move_caret(Atspi, paragraph, offset):
        return False
    if _caret_offset(paragraph) != offset or _focus_on_target(paragraph) is not True:
        return False
    from a11y_computer_use.drivers import _linux_input

    _linux_input.press_chord(chord)
    _drop_read_cache()
    _call_first(cell, ("clear_cache", "clearCache"))
    return True


def replace_writer_cell_text(
    acc, value: str, *, run: Callable | None = None,
) -> None:
    """Replace and verify the complete Writer cell paragraph list.

    EditableText replaces characters, not paragraph boundaries. Empty extra
    paragraphs are merged with BackSpace at their verified start; Return at
    a verified paragraph end creates missing paragraphs. No document-wide
    selection or observation is used. A failed structural edit is not retried
    blindly: its unchanged count is polled within the existing settle bound.
    """
    if run is None:
        run = lambda operation: operation()
    originals = run(lambda: _writer_cell_parts(acc))
    wanted = value.split("\n")
    deadline = time.monotonic() + _WRITER_CELL_SETTLE_S
    next_write = 0.0
    last_write_index = None
    pending_count = None
    while True:
        parts = run(lambda: _writer_cell_parts(acc))
        if parts == wanted:
            return
        now = time.monotonic()
        if now >= deadline:
            break
        if pending_count is not None:
            if len(parts) == pending_count:
                time.sleep(0.05)
                continue
            pending_count = None
        live = run(lambda: _writer_paragraphs(acc))
        if live and all(isinstance(part, str) for part in parts):
            # First remove excess paragraphs, including empty ones. Keeping
            # the first paragraph also handles a freshly rebuilt child.
            if len(parts) > len(wanted):
                child = live[-1]
                if parts[-1] != "":
                    run(lambda: set_text(child, "", force=True))
                if run(lambda: _full_text(child)) == "":
                    pending_count = len(parts)
                    run(lambda: _writer_cell_boundary(acc, child, 0, "backspace"))
            else:
                mismatch = next((i for i, part in enumerate(parts)
                                 if part != wanted[i]), None)
                if mismatch is not None:
                    # Only retrying an unconfirmed write needs the backoff.
                    # A different paragraph can be written immediately once
                    # the preceding paragraph has read back correctly.
                    if mismatch != last_write_index or now >= next_write:
                        run(lambda: set_text(live[mismatch], wanted[mismatch], force=True))
                        last_write_index = mismatch
                        next_write = time.monotonic() + 0.4
                elif len(parts) < len(wanted):
                    pending_count = len(parts)
                    run(lambda: _writer_cell_boundary(acc, live[-1], len(parts[-1]), "return"))
        time.sleep(0.05)
    failed_parts = run(lambda: _writer_cell_parts(acc))
    failed = "\n".join(part or "" for part in failed_parts)
    # Restore characters only when the original paragraph structure remains.
    # Do not claim rollback after an unverified structural mutation.
    current = run(lambda: _writer_paragraphs(acc))
    if len(current) == len(originals):
        for child, original in zip(current, originals):
            if original is not None and run(lambda: _full_text(child)) != original:
                run(lambda: set_text(child, original, force=True))
    raise _text_mismatch(
        "text_mismatch",
        f"the value read back {failed!r} does not match {value!r}; "
        f"cell kept {len(failed_parts)} paragraphs (expected {len(wanted)})",
        expected=value, actual=failed, formula=None,
    )


# Impress exposes the slide as ``document presentation``. Draw exposes the
# page as ``document frame``. The slide sorter is also a document frame; it
# is not an editable frame. A browser document frame is not LibreOffice.
_DRAWING_DOCUMENT_ROLES = frozenset({"document presentation", "document frame"})
_DRAWING_SKIP_ROLES = frozenset({
    "menu", "menu bar", "menu item", "popup menu", "tool bar", "scroll bar",
})
_DRAWING_TYPE_DEADLINE_S = 2.0
_DRAWING_TYPE_PAUSE_S = 0.05


def _libreoffice_node(acc) -> bool:
    """True when ``acc`` belongs to LibreOffice."""
    app = _call_first(acc, ("get_application", "getApplication"))
    if app is None:
        return False
    name = str(_call_first(app, ("get_name",), default="") or "")
    return libreoffice_app(name)


def _is_drawing_document(acc) -> bool:
    """An Impress slide or a Draw page. The slide sorter is not one."""
    try:
        role = _role_name(acc)
    except Exception:
        return False
    if role not in _DRAWING_DOCUMENT_ROLES:
        return False
    if role == "document frame" and _node_name(acc) == "Slides View":
        return False
    return _libreoffice_node(acc)


def _drawing_document_of(acc):
    """The Impress or Draw document that contains ``acc``, or None."""
    node = acc
    seen: set[int] = set()
    for _ in range(10):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        try:
            if _is_drawing_document(node):
                return node
            role = _role_name(node)
            # A Writer document, a web page, or the application is not inside
            # a slide. Stop here so a paragraph does not walk the rest of the app.
            if role in _WRITER_DOCUMENT_ROLES or role in {
                "document web", "document email", "application",
            }:
                return None
        except Exception:
            return None
        parent = _parent_of(node)
        if parent is None or parent is node:
            return None
        node = parent
    return None


def _frame_paragraphs(frame) -> list:
    """Editable paragraph children of a text frame, in order."""
    found = []
    count = _child_count(frame)
    if count < 0:
        count = 0
    for index in range(min(int(count), 32)):
        child = _child_at(frame, index)
        if child is None:
            continue
        if _role_name(child) not in {"paragraph", "heading"}:
            continue
        found.append(child)
    return found


def _drawing_frame(doc):
    """The text frame that has the caret, or None when none is focused.

    The focused paragraph's frame wins. Otherwise the focused panel that
    holds paragraphs. A click focuses that panel before the paragraph
    itself is focused. A document with no focused frame is not guessed.
    """
    focused_paragraph = None
    focused_panel = None
    budget = [64]

    def walk(node, depth: int) -> None:
        nonlocal focused_paragraph, focused_panel
        if node is None or budget[0] <= 0 or depth > 8:
            return
        budget[0] -= 1
        try:
            role = _role_name(node)
        except Exception:
            return
        if role in _DRAWING_SKIP_ROLES:
            return
        if role == "document frame" and _node_name(node) == "Slides View":
            return
        if role in {"paragraph", "heading"} and (
            _state_has(node, "FOCUSED") or _state_has(node, "ACTIVE")
        ):
            focused_paragraph = node
        if role == "panel" and _state_has(node, "FOCUSED") and _frame_paragraphs(node):
            focused_panel = node
        count = _child_count(node)
        if count < 0:
            count = 0
        for index in range(min(int(count), 16)):
            walk(_child_at(node, index), depth + 1)

    walk(doc, 0)
    if focused_paragraph is not None:
        parent = _parent_of(focused_paragraph)
        if parent is not None:
            return parent
    return focused_panel


def _drawing_edit_paragraph(acc):
    """The paragraph a type into this Impress or Draw focus should use.

    None when ``acc`` is not inside such a document, or no frame has focus.
    The focused or active paragraph wins. A frame that was just clicked has
    one paragraph and a caret there. Several paragraphs use the last one,
    which is where Return leaves the caret.
    """
    doc = _drawing_document_of(acc)
    if doc is None:
        return None
    frame = _drawing_frame(doc)
    if frame is None:
        return None
    paragraphs = [
        child for child in _frame_paragraphs(frame) if _editable_iface(child) is not None
    ]
    if not paragraphs:
        return None
    for child in paragraphs:
        if _state_has(child, "FOCUSED") or _state_has(child, "ACTIVE"):
            return child
    if len(paragraphs) == 1:
        return paragraphs[0]
    return paragraphs[-1]


def drawing_paragraph_shown(acc) -> str | None:
    """Text of an Impress or Draw paragraph, or None for any other node.

    The snapshot title includes that text, so a replace changes the title
    and a later snapshot does not match the element that was written. This
    is ``Text.get_text(0, -1)``, the same read the write already checked.
    """
    try:
        if not is_drawing_text_paragraph(acc):
            return None
        shown = _full_text(acc)
    except Exception:
        return None
    if not isinstance(shown, str):
        return None
    return shown


def is_drawing_text_paragraph(acc) -> bool:
    """A paragraph inside an Impress slide or a Draw page."""
    if acc is None:
        return False
    try:
        if _role_name(acc) not in {"paragraph", "heading"}:
            return False
    except Exception:
        return False
    return _drawing_document_of(acc) is not None


def drawing_text_editable(
    acc,
    *,
    role_str: str | None = None,
    state_editable: bool | None = None,
) -> bool:
    """An Impress or Draw paragraph that ``set_value`` and ``type(ref)`` may write.

    Both ``STATE_EDITABLE`` and EditableText are required. A read-only node
    is not a target. A browser paragraph that reports those is not one: it
    has to sit in a slide or a Draw page.
    """
    if acc is None:
        return False
    try:
        role = role_str if role_str is not None else _role_name(acc)
    except Exception:
        return False
    if role not in {"paragraph", "heading"}:
        return False
    if state_editable is None:
        state_editable = _state_has(acc, "EDITABLE")
    if not state_editable or _state_has(acc, "READ_ONLY"):
        return False
    if _editable_iface(acc) is None:
        return False
    return is_drawing_text_paragraph(acc)


def _drawing_focus_frame(acc):
    """The shape that can take focus for this paragraph, or None.

    A shape or text-frame role wins. Draw publishes the shape as a panel
    named for the shape, and that panel is the paragraph's parent.
    """
    frame = _shape_text_frame(acc)
    if frame is not None and frame is not acc:
        return frame
    parent = _parent_of(acc)
    if parent is None or parent is acc:
        return None
    try:
        role = _role_name(parent)
    except Exception:
        return None
    if role in _SHAPE_FRAME_ROLES or role == "panel":
        return parent
    return None


def place_drawing_caret_at_end(acc, *, ignore_focus: bool = False) -> None:
    """Move the caret to the end of a drawing paragraph that is not in text edit.

    Draw reports offset 0 until the shape is editing, so an insert would land
    in front of the existing words. A paragraph that already has focus keeps
    its caret and its selection. ``ignore_focus`` is the retry after this
    caller's own ``grab_focus``: the paragraph was not editing before that
    grab. A missing text interface is left alone.
    """
    if acc is None:
        return
    if not ignore_focus and _state_has(acc, "FOCUSED"):
        return
    try:
        Atspi = _atspi()
    except ImportError:
        return
    text = _full_text(acc)
    if not isinstance(text, str):
        return
    count = _safe(lambda: Atspi.Text.get_n_selections(acc))
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return
    _move_caret(Atspi, acc, len(text))


def focus_drawing_text(acc) -> bool:
    """Focus an editable drawing paragraph, or the shape that holds it.

    The paragraph often has no Component. The shape panel does. True when
    the paragraph or that frame reports focus. False when ``acc`` is not an
    editable drawing paragraph, or focus never arrives. No key is sent.
    """
    if not drawing_text_editable(acc):
        return False
    seen = _focus_on_target(acc)
    if seen is True or seen is None:
        return True
    grab_focus(acc)
    seen = _focus_on_target(acc)
    if seen is True or seen is None:
        return True
    frame = _drawing_focus_frame(acc)
    if frame is None:
        return False
    grab_focus(frame)
    seen = _focus_on_target(acc)
    if seen is True or seen is None:
        return True
    if _confirm_focus_on_target(frame):
        return True
    return _focus_on_target(acc) is True


def _drawing_frame_text(doc) -> tuple[str, str | None]:
    """``(status, text)`` for the focused text frame inside ``doc``.

    ``text`` joins the frame's paragraphs with newlines. ``unreadable``
    means the frame or its text interface is not there. An empty paragraph
    is ``text`` and ``""``: that value was read.
    """
    _call_first(doc, ("clear_cache", "clearCache"))
    frame = _drawing_frame(doc)
    if frame is None:
        return "unreadable", None
    _call_first(frame, ("clear_cache", "clearCache"))
    paragraphs = _frame_paragraphs(frame)
    if not paragraphs:
        return "unreadable", None
    parts: list[str] = []
    for child in paragraphs:
        raw = _full_text(child)
        if raw is None:
            return "unreadable", None
        parts.append(_type_paragraph_body(raw))
    return "text", "\n".join(parts)


def drawing_focus_text(app: str) -> tuple[str, str | None]:
    """``(status, text)`` of the focused Impress or Draw frame.

    ``skip`` when focus is not in one. ``unreadable`` when the frame's
    paragraphs cannot be read. ``text`` is the paragraphs joined by
    newlines, and ``""`` is a frame that was read and is empty.
    """
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        return "skip", None
    if truncated or acc is None:
        return "skip", None
    doc = _drawing_document_of(acc)
    if doc is None:
        return "skip", None
    try:
        return _drawing_frame_text(doc)
    except Exception:
        return "unreadable", None


def drawing_typed_visible(before: str | None, shown: str | None, text: str) -> bool:
    """True when a drawing frame shows ``text``.

    Newline, CRLF, U+2028, U+2029, and a paragraph boundary are the same
    break. A missing or extra character is not. A frame that already held
    the text and did not change does not count, unless the frame is exactly
    that text.
    """
    if shown is None:
        return False
    from a11y_computer_use.outcome import typed_breaks_match

    if typed_breaks_match(shown, text):
        return True
    return _typed_visible(before, shown, text)


def poll_drawing_type(app: str, before: str | None, text: str) -> tuple[str, str | None]:
    """Poll an Impress or Draw frame until ``text`` shows or the read settles.

    The first read can still be the placeholder. A later paragraph that is
    not published yet is not a miss. A stable read that does not show
    ``text`` is returned as it is. ``skip`` is not a drawing frame.
    """
    deadline = time.monotonic() + _DRAWING_TYPE_DEADLINE_S
    last: object = object()
    stable = 0
    status, shown = "skip", None
    while True:
        status, shown = drawing_focus_text(app)
        if status == "skip":
            return status, shown
        if status == "text" and drawing_typed_visible(before, shown, text):
            return status, shown
        if status == "text" and shown == last:
            stable += 1
            if stable >= 2 and shown not in (None, before):
                return status, shown
        else:
            stable = 0
        last = shown
        if time.monotonic() >= deadline:
            return status, shown
        time.sleep(_DRAWING_TYPE_PAUSE_S)


def drawing_frame_outcome_text(
    app: str, requested: str, before: str | None = None,
) -> str | None:
    """``requested`` when the focused Impress or Draw frame shows it.

    None when focus is not in such a frame, when the paragraphs cannot be
    read, or when a character is missing or extra. The comparison treats a
    paragraph break and a newline as the same break.
    """
    if not requested or not libreoffice_app(app or ""):
        return None
    status, shown = drawing_focus_text(app)
    if status != "text" or shown is None:
        return None
    if drawing_typed_visible(before, shown, requested):
        return requested
    return None


def libreoffice_app(app: str) -> bool:
    """True when ``app`` names LibreOffice. The comm is often ``soffice.bin``."""
    name = (app or "").lower()
    return "soffice" in name or "libreoffice" in name


def _display_key(value: str) -> str:
    """``:1`` and ``localhost:1.0`` are the same display. An empty value stays empty."""
    text = str(value or "").strip()
    if ":" not in text:
        return text
    host, rest = text.rsplit(":", 1)
    number = rest.split(".", 1)[0]
    if not number.isdigit():
        return text
    if host in {"unix", "localhost"}:
        host = ""
    return f"{host}:{number}"


def _atspi_bus(env: dict[str, str]) -> str:
    return str(env.get("AT_SPI_BUS_ADDRESS") or env.get("AT_SPI_BUS") or "").strip()


def process_in_caller_session(environ: dict[str, str]) -> bool:
    """True when ``environ`` is this process's X display or AT-SPI bus.

    A soffice process on another display is another session, even on the
    same machine. When this process has no ``DISPLAY``, a process that has
    one is not treated as local. A shared AT-SPI bus matches only when the
    displays do not already disagree.
    """
    ours = _display_key(os.environ.get("DISPLAY", ""))
    theirs = _display_key(environ.get("DISPLAY", ""))
    if ours and theirs:
        return ours == theirs
    if theirs and not ours:
        return False
    ours_bus = _atspi_bus(dict(os.environ))
    theirs_bus = _atspi_bus(environ)
    if ours_bus and theirs_bus:
        return ours_bus == theirs_bus
    return False


def _proc_root() -> str:
    return "/proc"


def _read_proc_environ(pid: str, root: str) -> dict[str, str] | None:
    try:
        with open(os.path.join(root, pid, "environ"), "rb") as fh:
            raw = fh.read(262144)
    except OSError:
        return None
    found: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        found[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return found


def _libreoffice_processes() -> list[tuple[str, str, dict[str, str] | None]]:
    """``(comm, state, environ)`` for each soffice, soffice.bin, or oosplash.

    ``environ`` is None when ``/proc/<pid>/environ`` cannot be read. A
    command line that merely mentions the name is not a row.
    """
    root = _proc_root()
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    rows: list[tuple[str, str, dict[str, str] | None]] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(root, entry, "comm"), encoding="utf-8", errors="replace") as fh:
                comm = fh.read().strip()
            with open(os.path.join(root, entry, "stat"), encoding="utf-8", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue
        if comm not in _LO_COMMS:
            continue
        end = stat.rfind(")")
        state = stat[end + 2:].split(None, 1)[0] if end >= 0 and end + 2 < len(stat) else ""
        rows.append((comm, state, _read_proc_environ(entry, root)))
    return rows


def libreoffice_process_running() -> bool:
    """True when a LibreOffice process in this session is running.

    The comm is ``soffice``, ``soffice.bin``, or ``oosplash``. A process on
    another ``DISPLAY`` or AT-SPI bus does not count, and a zombie does not
    count. A command line that merely mentions the name is not a match.
    """
    for _comm, state, environ in _libreoffice_processes():
        if state == "Z":
            continue
        if environ is None:
            continue
        if process_in_caller_session(environ):
            return True
    return False


def libreoffice_without_bridge(app: str) -> bool:
    """True when ``app`` is LibreOffice and a soffice process in this session is running.

    The caller has already failed to find an AT-SPI root. The gen VCL plugin
    stays off the bus; gtk3 with libreoffice-gtk3 does not. A soffice process
    on another display is not this session's bridge.
    """
    return libreoffice_app(app) and libreoffice_process_running()


# LibreOffice's X window shows up in app list and window list while AT-SPI is
# still registering. That gap was 3–13 s. Snapshot polls until this deadline,
# then reports the app missing (or the missing gtk3 bridge).
ATSPI_REGISTER_WAIT_S = 15.0
ATSPI_REGISTER_POLL_S = 0.25


def app_listed(identifier: str) -> bool:
    """True when app list or window list already shows ``identifier``.

    App list keys the row by process comm. Window list uses that comm, the
    WM_CLASS, and the title. Those are the same names ``resolve_app`` uses,
    so a snapshot of ``LibreOffice`` sees the ``soffice.bin`` row the list
    already returned.
    """
    if not (identifier or "").strip():
        return False
    from a11y_computer_use.drivers import _linux_system
    from a11y_computer_use.schema import ComputerUseError

    try:
        apps = _linux_system.running_apps()
    except ComputerUseError:
        raise
    except Exception:
        apps = []
    for row in apps or []:
        if not isinstance(row, dict):
            continue
        comm = str(row.get("bundle_id") or row.get("name") or "")
        if _linux_system._comm_matches_identifier(identifier, comm):
            return True
    try:
        rows = _linux_system.windows()
    except ComputerUseError:
        raise
    except Exception:
        rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        comm = str(row.get("app") or "")
        if comm and _linux_system._comm_matches_identifier(identifier, comm):
            return True
        if _linux_system._class_matches_identifier(
            identifier,
            str(row.get("wm_class") or ""),
            str(row.get("wm_class_class") or ""),
        ):
            return True
        title = str(row.get("title") or "").lower()
        if title and any(
            name and name in title for name in _linux_system._identity_needles(identifier)
        ):
            return True
    return False


def should_wait_for_atspi(identifier: str) -> bool:
    """True when snapshot should wait for this app to appear on the AT-SPI bus.

    The app is already in the app list or the window list, or a LibreOffice
    process in this session is running and has not registered yet. A name
    that is in neither place, and is not that process, is ``app_not_found``
    on the first look. A soffice process on another display does not wait.
    """
    if app_listed(identifier):
        return True
    return libreoffice_app(identifier) and libreoffice_process_running()


def _table_dimensions(acc) -> tuple[int | None, int | None]:
    try:
        Atspi = _atspi()
    except ImportError:
        return None, None
    table = getattr(Atspi, "Table", None)
    if table is None:
        return None, None

    def _as_int(value) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return int(value)

    return (
        _as_int(_safe(lambda: table.get_n_rows(acc))),
        _as_int(_safe(lambda: table.get_n_columns(acc))),
    )


def _spreadsheet_table(node) -> bool:
    """True for a Calc grid, not a calendar or a GTK tree table."""
    if _role_name(node) != "table":
        return False
    rows, cols = _table_dimensions(node)
    if rows is not None and cols is not None and (rows >= 256 or cols >= 64):
        return True
    parent = _call_first(node, ("get_parent", "getParent"))
    if parent is not None and "spreadsheet" in _role_name(parent):
        return True
    return False


def _sheet_index(address: str) -> tuple[int, int] | None:
    """``(row, column)`` for ``A1`` or ``AA10``. None when it is not an address."""
    if not _SHEET_ADDRESS.match(address):
        return None
    split = 0
    while split < len(address) and address[split].isalpha():
        split += 1
    col = 0
    for char in address[:split]:
        col = col * 26 + (ord(char) - ord("A") + 1)
    row = int(address[split:]) - 1
    col -= 1
    if row < 0 or col < 0:
        return None
    return row, col


def _sheet_coord_from_node(node, table) -> tuple[int, int] | None:
    """Address of ``node`` or a close ancestor, as ``(row, column)``."""
    current = node
    for _depth in range(6):
        if current is None or current is table:
            return None
        found = _sheet_index(_node_name(current))
        if found is not None:
            return found
        parent = _parent_of(current)
        if parent is None or parent is current:
            return None
        current = parent
    return None


def _sheet_view_box(acc, screen: tuple[int, int]):
    """The table's screen rectangle clipped to the display, or None.

    ``(x0, y0, x1, y1)`` with ``x1`` and ``y1`` exclusive. This is the
    painted grid. Cells outside it are the rest of the million-row sheet.
    """
    pos, size = _extents(acc)
    if not _overlaps_screen(pos, size, screen):
        return None
    x0 = max(0.0, float(pos[0]))
    y0 = max(0.0, float(pos[1]))
    x1 = min(float(screen[0]), float(pos[0]) + float(size[0]))
    y1 = min(float(screen[1]), float(pos[1]) + float(size[1]))
    if x1 - x0 <= 1 or y1 - y0 <= 1:
        return None
    return (x0, y0, x1, y1)


def _cell_box(acc, row: int, col: int):
    """``(x0, y0, x1, y1)`` of the cell, or None when it has no positive box."""
    cell = _table_cell_at(acc, row, col)
    if cell is None:
        return None
    pos, size = _extents(cell)
    if pos is None or size is None:
        return None
    width = float(size[0])
    height = float(size[1])
    if width <= 0 or height <= 0:
        return None
    x = float(pos[0])
    y = float(pos[1])
    if x <= _OFFSCREEN_SENTINEL or y <= _OFFSCREEN_SENTINEL:
        return None
    return (x, y, x + width, y + height)


def _search_overlapping(count: int, span_at, low: float, high: float) -> tuple[int, int] | None:
    """Inclusive indexes whose ``(start, end)`` meets ``[low, high)``.

    ``span_at(index)`` returns that pair, or None when the cell has no box.
    Positions increase with the index. The search is exponential and then
    binary, a few dozen probes, so a million-row sheet is not walked.
    """
    if count <= 0 or not high > low:
        return None
    cache: dict[int, tuple[float, float] | None] = {}

    def span(index: int):
        if index < 0 or index >= count:
            return None
        if index not in cache:
            cache[index] = span_at(index)
        return cache[index]

    def kind(index: int) -> str:
        got = span(index)
        if got is None:
            return "none"
        start, end = got
        if end > low and start < high:
            return "hit"
        if end <= low:
            return "above"
        return "past"

    if kind(0) == "none":
        return None

    prev = 0
    index = 0
    first = None
    for _step in range(40):
        state = kind(index)
        if state == "above":
            prev = index
            nxt = 1 if index == 0 else index * 2
            if nxt <= index:
                return None
            if nxt >= count:
                if index >= count - 1:
                    return None
                index = count - 1
                continue
            index = nxt
            continue
        lo, hi = prev, index
        found = None
        while lo <= hi:
            mid = (lo + hi) // 2
            state = kind(mid)
            if state == "hit":
                found = mid
                hi = mid - 1
            elif state == "above":
                lo = mid + 1
            else:
                hi = mid - 1
        first = found
        break
    if first is None:
        return None

    last = first
    index = first
    for _step in range(40):
        nxt = index + 1 if index == first else index * 2
        if nxt <= index:
            break
        if nxt >= count:
            nxt = count - 1
        if nxt <= index:
            break
        state = kind(nxt)
        if state == "hit":
            last = nxt
            if nxt >= count - 1:
                break
            index = nxt
            continue
        lo, hi = index, nxt
        found = index if kind(index) == "hit" else None
        while lo <= hi:
            mid = (lo + hi) // 2
            if kind(mid) == "hit":
                found = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if found is None:
            return None
        last = found
        break
    if last < first or kind(last) != "hit":
        return None
    return first, last


def _clamp_sheet_span(span: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Keep the visible origin and at most the on-screen maximum."""
    row0, row1, col0, col1 = span
    row0 = max(0, int(row0))
    col0 = max(0, int(col0))
    row1 = max(row0, int(row1))
    col1 = max(col0, int(col1))
    return (
        row0,
        min(row1, row0 + _SHEET_VIEW_MAX_ROWS - 1),
        col0,
        min(col1, col0 + _SHEET_VIEW_MAX_COLS - 1),
    )


def _span_advances(acc, row0: int, row1: int, col0: int, col1: int) -> bool:
    """True when the corner cells move down and to the right.

    A toolkit that parks every cell at the same point would otherwise look
    like the whole sheet overlaps the window.
    """
    if row1 > row0:
        top = _cell_box(acc, row0, col0)
        bottom = _cell_box(acc, row1, col0)
        if top is None or bottom is None or bottom[1] <= top[1]:
            return False
    if col1 > col0:
        left = _cell_box(acc, row0, col0)
        right = _cell_box(acc, row0, col1)
        if left is None or right is None or right[0] <= left[0]:
            return False
    return True


def _sheet_span_from_extents(acc, box, n_rows: int, n_cols: int):
    """Inclusive ``(row0, row1, col0, col1)`` whose boxes meet ``box``.

    None when the cells have no boxes, or the boxes do not advance. The
    caller then hit-tests the same rectangle.
    """
    x0, y0, x1, y1 = box

    def row_span(row: int):
        got = _cell_box(acc, row, 0)
        if got is None:
            return None
        return (got[1], got[3])

    def col_span(col: int):
        got = _cell_box(acc, row0, col)
        if got is None:
            return None
        return (got[0], got[2])

    rows = _search_overlapping(n_rows, row_span, y0, y1)
    if rows is None:
        return None
    row0, row1 = rows
    cols = _search_overlapping(n_cols, col_span, x0, x1)
    if cols is None:
        return None
    col0, col1 = cols
    if not _span_advances(acc, row0, row1, col0, col1):
        return None
    return row0, row1, col0, col1


def _sheet_coord_at_point(acc, x: float, y: float) -> tuple[int, int] | None:
    """``(row, column)`` of the cell under ``(x, y)``, or None."""
    comp = _component(acc)
    if comp is None:
        return None
    return _sheet_coord_from_node(_hit_at(comp, x, y), acc)


def _sheet_walk_hit(acc, x: float, y: float, dx: float, dy: float, limit_x: float, limit_y: float):
    """First address hit walking from ``(x, y)`` by ``(dx, dy)``.

    Column headers and the row-header gutter answer no address. A short
    walk steps past them and stays inside the table box.
    """
    for _step in range(48):
        if dx > 0 and x >= limit_x:
            return None
        if dx < 0 and x <= limit_x:
            return None
        if dy > 0 and y >= limit_y:
            return None
        if dy < 0 and y <= limit_y:
            return None
        found = _sheet_coord_at_point(acc, x, y)
        if found is not None:
            return found
        x += dx
        y += dy
    return None


def _sheet_span_from_hits(acc, box):
    """Inclusive span from ``get_accessible_at_point`` inside ``box``.

    The four edges name the first and last visible row and column. None
    when no edge hits an address, which is a toolkit with no hit-testing.
    """
    x0, y0, x1, y1 = box
    mid_x = (x0 + x1) / 2.0
    mid_y = (y0 + y1) / 2.0
    top = _sheet_walk_hit(acc, mid_x, y0 + 1.0, 0.0, 4.0, x1, y1)
    bottom = _sheet_walk_hit(acc, mid_x, y1 - 1.0, 0.0, -4.0, x0, y0)
    left = _sheet_walk_hit(acc, x0 + 1.0, mid_y, 4.0, 0.0, x1, y1)
    right = _sheet_walk_hit(acc, x1 - 1.0, mid_y, -4.0, 0.0, x0, y0)
    coords = [item for item in (top, bottom, left, right) if item is not None]
    if not coords:
        return None
    return (
        min(row for row, _col in coords),
        max(row for row, _col in coords),
        min(col for _row, col in coords),
        max(col for _row, col in coords),
    )


def _rect_meets(pos, size, box) -> bool:
    x, y = float(pos[0]), float(pos[1])
    width, height = float(size[0]), float(size[1])
    x0, y0, x1, y1 = box
    return x < x1 and y < y1 and x + width > x0 and y + height > y0


def _sheet_cells_in_span(acc, span, screen, box, *, trust_missing: bool) -> list:
    """Cells of ``span`` that meet the on-screen box.

    ``trust_missing`` keeps a cell the toolkit gave no box, which is the
    hit-tested span. A cell with a box outside the window is still dropped.
    """
    row0, row1, col0, col1 = _clamp_sheet_span(span)
    found = []
    for row in range(row0, row1 + 1):
        for col in range(col0, col1 + 1):
            cell = _table_cell_at(acc, row, col)
            if cell is None:
                continue
            pos, size = _extents(cell)
            if pos is None or size is None or float(size[0]) <= 0 or float(size[1]) <= 0:
                if trust_missing:
                    found.append(cell)
                continue
            if _overlaps_screen(pos, size, screen) and _rect_meets(pos, size, box):
                found.append(cell)
    return found


def _sheet_fallback_cells(acc) -> list:
    """The first addresses, when the sheet reports no on-screen box.

    Scans 32 rows by 16 columns and keeps up to 96 cells that have a
    positive size. That window includes B2 and E1–G1 on a new sheet. When
    every cell in the scan is 0×0, the first 96 are returned so a toolkit
    that does not report extents still lists addresses.
    """
    try:
        table = _atspi().Table
    except (ImportError, AttributeError):
        return []
    on_screen: list = []
    fallback: list = []
    for row in range(_SHEET_SCAN_ROWS):
        for col in range(_SHEET_SCAN_COLS):
            cell = _safe(lambda r=row, c=col: table.get_accessible_at(acc, r, c))
            if cell is None:
                continue
            fallback.append(cell)
            _pos, size = _extents(cell, keep_zero=True)
            width = (size or (0, 0))[0] or 0
            height = (size or (0, 0))[1] or 0
            if width > 0 and height > 0:
                on_screen.append(cell)
                if len(on_screen) >= _SHEET_KEEP:
                    return on_screen
    if on_screen:
        return on_screen
    return fallback[:_SHEET_KEEP]


def _sheet_window_cells(acc) -> list:
    """Cells inside the table's on-screen box, from the Table interface.

    A Calc sheet reports about a million rows and ``INT_MAX`` children.
    ``get_child_at_index`` is row-major, so the first children are row 1
    and a cap of 96 stops at row 6. The visible rows and columns are the
    ones whose boxes meet the table's screen rectangle. When those boxes
    do not advance, ``get_accessible_at_point`` on the same rectangle
    names the span. A sheet that reports neither a box nor a hit keeps
    the first 96 addresses.
    """
    screen = _screen_size()
    box = _sheet_view_box(acc, screen)
    if box is not None:
        rows, cols = _table_dimensions(acc)
        n_rows = rows if isinstance(rows, int) and not isinstance(rows, bool) and rows > 0 else 0
        n_cols = cols if isinstance(cols, int) and not isinstance(cols, bool) and cols > 0 else 0
        if n_rows and n_cols:
            span = _sheet_span_from_extents(acc, box, n_rows, n_cols)
            if span is not None:
                found = _sheet_cells_in_span(acc, span, screen, box, trust_missing=False)
                if found:
                    return found
            span = _sheet_span_from_hits(acc, box)
            if span is not None:
                found = _sheet_cells_in_span(acc, span, screen, box, trust_missing=True)
                if found:
                    return found
    return _sheet_fallback_cells(acc)


def _publish_sheet_cells(cells: list) -> list:
    """Cells the snapshot walk can keep, banded once they pass the sheet cap.

    A flat list is cut at 96 and at 200 walked children, so a 29-row window
    would still end at row 6. Each band is a table of at most
    ``SHEET_MAX_CHILDREN`` address cells. A window of 64×32 is 22 bands,
    which is under the dense cap, so the lower rows stay. The explicit
    synthetic label distinguishes these snapshot groups from native tables.
    """
    if len(cells) <= SHEET_MAX_CHILDREN:
        return cells
    items = []
    for cell in cells:
        pos, size = _extents(cell)
        if pos is None or size is None or float(size[0]) <= 0 or float(size[1]) <= 0:
            continue
        items.append((cell, pos, size))
    if len(items) <= SHEET_MAX_CHILDREN:
        return [cell for cell, _pos, _size in items] or cells[:SHEET_MAX_CHILDREN]
    return [
        _OnScreenBand(
            items[start:start + SHEET_MAX_CHILDREN], role="table",
            name="Visible cells (synthetic viewport band)",
        )
        for start in range(0, len(items), SHEET_MAX_CHILDREN)
    ]


def _unescape_formula(text: str) -> str:
    """Undo LibreOffice's ``\\:``, ``\\;``, and ``\\\\`` in a Formula attribute."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in {":", ";", "\\"}:
            out.append(text[index + 1])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _formula_key(text: str) -> str:
    """Case-folded formula text with whitespace removed and ``;`` folded to ``,``.

    A leading ``=`` is optional. Whitespace inside quotes stays, so a space
    in a string is still a difference. A range colon stays a colon. The
    semicolon is LibreOffice's argument separator.
    """
    raw = _unescape_formula(str(text).strip())
    if raw.startswith("="):
        raw = raw[1:]
    out: list[str] = []
    quote = ""
    for char in raw:
        if quote:
            out.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            out.append(char)
            continue
        if char.isspace():
            continue
        if char == ";":
            out.append(",")
            continue
        out.append(char.casefold())
    return "".join(out)


def formulas_match(got: str | None, wanted: str | None) -> bool:
    """True when both strings are the same formula after Calc's rewriting."""
    if not got or not wanted:
        return False
    return _formula_key(got) == _formula_key(wanted)


def _formula_needs_editor(formula: str | None, wanted: str) -> bool:
    """True when the Formula attribute cannot be the whole request.

    LibreOffice escapes a range colon as ``\\:``. AT-SPI then splits the
    attribute on that colon, so ``=SUM(A1:A3)`` arrives as ``SUM(A1\\``.
    The open cell editor still has the formula.
    """
    if not str(wanted).lstrip().startswith("=") or not formula:
        return False
    if formula.endswith("\\"):
        return True
    return ":" in wanted and ":" not in formula


def _numeric_texts_match(shown: str | None, wanted: str) -> bool:
    """True when both sides are numbers and Calc's display equals the request."""
    if shown is None or not str(wanted).strip() or str(wanted).lstrip().startswith("="):
        return False
    from a11y_computer_use.outcome import display_numbers_match

    return display_numbers_match(str(shown), wanted)


def committed_sheet_formula(acc, app: str = "soffice.bin") -> str | None:
    """Formula text from the cell editor, then Escape so the cell stays committed.

    Used when the Formula attribute was cut at a range colon. ``None`` when
    the editor does not open. Escape cancels the edit of an unchanged formula.
    """
    from a11y_computer_use.drivers import _linux_input

    grab_focus(acc)
    _linux_input.press_chord("f2")
    names = [app]
    if app != "soffice.bin":
        names.append("soffice.bin")
    if "soffice" not in names:
        names.append("soffice")
    text = None
    try:
        for _attempt in range(10):
            time.sleep(0.05)
            for name in names:
                text = sheet_editor_text(name)
                if text:
                    return text
        return None
    finally:
        try:
            _linux_input.press_chord("escape")
        except Exception:
            pass


def sheet_cell_matches(acc, value: str) -> bool:
    """True when the cell shows ``value``, the same formula, or the same number.

    The formula attribute omits a leading ``=``. Case, whitespace, and
    LibreOffice's ``;`` argument separator do not make a different formula.
    ``1.50`` and ``1.5`` are the same number. A formula request is not
    compared to the number the cell computes. A range formula whose
    attribute was cut at ``:`` is not a match here; the editor read is
    separate. Newline, CRLF, U+2028, U+2029, and a paragraph boundary
    compare as the same break. A missing or extra character does not.
    """
    from a11y_computer_use.outcome import typed_breaks_match

    shown = _full_text(acc)
    if typed_breaks_match(shown, value):
        return True
    formula = _sheet_formula(acc)
    if formula and formulas_match(formula, value):
        return True
    return _numeric_texts_match(shown, value)


def sheet_outcome_text(app: str, requested: str, cell=None) -> str | None:
    """Read-back for a Calc type or set_value, or None to keep the snapshot value.

    An open cell editor's paragraph is the in-progress type. A committed
    formula is that formula, not the number the cell displays. A snapshot
    value that already equals the request is left alone.
    """
    if not requested or not libreoffice_app(app or ""):
        return None
    # The focused node during a type is often the editor paragraph, not the
    # cell. The paragraph is what holds the characters before Return.
    editor = sheet_editor_text(app)
    if editor:
        normalized = _field_text_for_type(editor, requested)
        if normalized and _typed_visible("", editor, requested):
            return normalized
    sheet = cell is not None and is_sheet_cell(cell)
    if not sheet:
        return None
    shown = _full_text(cell)
    if shown == requested:
        return None
    if sheet_cell_matches(cell, requested):
        # A formula, or a number Calc rewrote (``1,200`` shows as ``1200``).
        # The snapshot value stays the display. The outcome quotes the request.
        if _sheet_formula(cell) or _numeric_texts_match(shown, requested):
            return requested
        return None
    formula = _sheet_formula(cell)
    if not _formula_needs_editor(formula, requested):
        return None
    opened = committed_sheet_formula(cell, app)
    if opened and formulas_match(opened, requested):
        return requested
    return None


def _find_cell_editor(node, budget: list[int]):
    """The ``Cell A1`` group opened while a cell is being edited.

    The name is matched on any role: LibreOffice 24.2 exposes it as a
    group, and 25.2 as a panel. Menus, toolbars, status bars, and the
    sheet table are not descended: their contents cannot be the cell editor.
    A Calc table's child count is huge, and the editor is a sibling of the
    table, not a cell.
    """
    if budget[0] <= 0 or node is None:
        return None
    if _CELL_EDITOR_PANEL.match(_node_name(node)):
        return node
    role = _role_name(node)
    if role in {"table", "menu", "menu bar", "popup menu", "tool bar", "status bar"}:
        return None
    budget[0] -= 1
    count = _child_count(node)
    if count > 40:
        count = 40
    for index in range(count):
        child = _child_at(node, index)
        found = _find_cell_editor(child, budget)
        if found is not None:
            return found
    return None


def _cache_masks():
    """``(none, undefined)`` for ``set_cache_mask``.

    ``none`` is ``0`` when the typelib has no ``Cache`` enum, which is the
    same value as ``Atspi.Cache.NONE``. ``undefined`` is missing on a test
    double; the caller then leaves the mask alone after the read.
    """
    none = 0
    undefined = None
    try:
        cache = getattr(_atspi(), "Cache", None)
    except Exception:
        return none, undefined
    if cache is None:
        return none, undefined
    member = getattr(cache, "NONE", None)
    if member is not None:
        none = member
    return none, getattr(cache, "UNDEFINED", None)


def _without_child_cache(node, fn, *, preserve_properties=False):
    """Run ``fn`` without trusting this application's child cache.

    ``clear_cache_single`` sets ``cached_properties`` to none and leaves the
    children array in place. The next D-Bus call reenters the event loop.
    A cache event then sets the children flag again, and
    ``get_child_at_index`` returns that stale array. LibreOffice opens the
    cell editor as a new sibling of the sheet. The pre-edit children do not
    contain it, so the grid never sends Return. While that editor is open
    every other cell's text interface stays empty, and A1 reads back as a
    mismatch. ``set_cache_mask(NONE)`` makes the cache test fail even when
    the flag comes back. The desktop default is restored afterwards.
    The operation-scoped getter cache is invalidated around the walk too,
    since it can retain child counts and handles read before the edit. The
    mask change and walk share the event thread, including direct native calls.
    Chromium snapshot walks set ``preserve_properties`` to retain native
    caching of roles, names, states and interfaces. Only CHILDREN is bypassed
    there; disabling every property adds D-Bus round trips across the page.
    Calc callers retain the full cache bypass needed during editor mutations.
    A node without ``set_cache_mask`` is a test double and is walked as it is.
    """
    from a11y_computer_use.drivers import _atspi_events

    def read_uncached():
        app = _call_first(node, ("get_application", "getApplication"))
        target = app if app is not None else node
        setter = getattr(target, "set_cache_mask", None)
        if setter is None:
            return fn()
        mask, undefined = _cache_masks()
        if preserve_properties:
            try:
                cache = _atspi().Cache
                mask = int(cache.DEFAULT) & ~int(cache.CHILDREN)
            except (AttributeError, ImportError):
                pass
        try:
            _drop_read_cache()
            setter(mask)
        except Exception:
            return fn()
        try:
            return fn()
        finally:
            _drop_read_cache()
            if undefined is not None:
                try:
                    setter(undefined)
                except Exception:
                    pass

    return _atspi_events.run(read_uncached)


def _fresh_sheet_root(app, scope):
    """Refresh desktop children before locating Calc's application/window."""
    try:
        desktop = _call_first(_atspi(), ("get_desktop",), 0)
    except ImportError:
        desktop = None
    return _without_child_cache(desktop, lambda: find_root(app, scope))


def sheet_editor_text(app: str) -> str | None:
    """Paragraph text of the open cell editor, or None.

    Only LibreOffice. The panel is a sibling of the sheet, named ``Cell F1``,
    and its paragraph is the in-progress string. Absent when no edit is open.
    The walk does not trust the child cache: the editor is created when the
    edit starts, and a stale children array still lists the sheet alone.
    """
    if not libreoffice_app(app):
        return None
    try:
        from a11y_computer_use.schema import Scope

        root = _fresh_sheet_root(app, Scope.APP)
    except Exception:
        return None
    if root is None:
        return None

    def read():
        panel = _find_cell_editor(root, [160])
        if panel is None:
            return None
        count = min(_child_count(panel), 8)
        for index in range(count):
            child = _child_at(panel, index)
            if child is None:
                continue
            text = _full_text(child)
            if text:
                return text
        return None

    return _without_child_cache(root, read)


def _cell_editor_is_secure(app: str) -> bool | None:
    """Whether the open Calc cell editor is a password field.

    ``None`` when no editor is open, or ``app`` is not LibreOffice. The
    editor is a panel or group named ``Cell A1`` and a paragraph under it.
    Neither is a password field. ``True`` only when that panel or its text
    child really has the password-text role.
    """
    if not libreoffice_app(app):
        return None
    try:
        from a11y_computer_use.schema import Scope

        root = _fresh_sheet_root(app, Scope.APP)
    except Exception:
        return None
    if root is None:
        return None

    def read():
        panel = _find_cell_editor(root, [160])
        if panel is None:
            return None
        if is_secure(panel):
            return True
        count = min(_child_count(panel), 8)
        for index in range(count):
            child = _child_at(panel, index)
            if child is not None and is_secure(child):
                return True
        return False

    return _without_child_cache(root, read)


def _selected_sheet_cell(app: str):
    """The selected Calc cell, or None.

    The selection call can throw. That is None, not an error, and the
    sheet's cells are not indexed.
    """
    if not libreoffice_app(app):
        return None
    try:
        from a11y_computer_use.schema import Scope

        root = find_root(app, Scope.WINDOW)
    except Exception:
        return None
    if root is None:
        return None
    table = _find_spreadsheet(root, [80])
    if table is None:
        return None
    iface = _call_first(table, ("get_selection_iface", "get_selection"))
    if iface is None:
        return None
    return _call_first(iface, ("get_selected_child", "getSelectedChild"), 0)


def selected_sheet_text(app: str) -> str | None:
    """Text of the selected Calc cell, or None.

    The cell editor is a sibling of the sheet. After Return the editor
    closes and this text is the committed value. Empty text and a selection
    that throws are None.
    """
    cell = _selected_sheet_cell(app)
    if cell is None:
        return None
    text = _full_text(cell)
    if text:
        return text
    return None


def _find_spreadsheet(node, budget: list[int]):
    """The Calc grid, without indexing cells or unrelated toolbar contents."""
    if budget[0] <= 0 or node is None:
        return None
    if _spreadsheet_table(node):
        return node
    role = _role_name(node)
    if role in {"table", "menu", "menu bar", "popup menu", "tool bar", "status bar"}:
        return None
    budget[0] -= 1
    count = min(_child_count(node), 40)
    for index in range(count):
        found = _find_spreadsheet(_child_at(node, index), budget)
        if found is not None:
            return found
    return None


def spreadsheet_open(app: str) -> bool:
    """True when ``app`` is LibreOffice and a Calc grid is in the window."""
    if not libreoffice_app(app):
        return False
    try:
        from a11y_computer_use.schema import Scope

        root = find_root(app, Scope.WINDOW)
    except Exception:
        return False
    if root is None:
        return False
    return _find_spreadsheet(root, [80]) is not None


def poll_sheet_type(
    read_editor,
    read_cell,
    read_focused,
    before: str | None,
    text: str,
    *,
    sheet: bool,
) -> str | None:
    """Poll until ``text`` shows in the cell editor, the cell, or focus.

    The editor opens after the first key, so an early empty read is not the
    answer. A prefix is not returned into the settle check: ``4`` would
    otherwise end the poll before ``42`` publishes. On a sheet the cell is
    read once after the editor misses, because that value updates when the
    editor is not what holds the characters. The last editor or cell string
    is what a mismatch shows. A Writer paragraph has no sheet, so each read
    is the focused text and the ordinary settle applies.
    """
    latest: str | None = None

    def read():
        nonlocal latest
        editor = read_editor()
        if editor and _typed_visible(before, editor, text):
            return editor
        if editor:
            latest = editor
        if not sheet:
            focused = read_focused()
            if focused:
                latest = focused
            return focused
        return None

    found = _poll_typed_text(read, before, text)
    if _typed_visible(before, found, text):
        return found
    if sheet:
        cell = read_cell()
        if cell and _typed_visible(before, cell, text):
            return cell
        if cell:
            latest = cell
    return latest if latest is not None else found


def sheet_type_grid(text: str) -> list[list[str]] | None:
    """Rows and cells a Calc type walks, or None for a single cell.

    ``\\t`` moves right and ``\\n`` moves down. A trailing newline only
    leaves the last row; it is not an extra empty row of cells. A blank
    between tabs is a cell that was cleared.
    """
    if "\t" not in text and "\n" not in text:
        return None
    body = text.replace("\r\n", "\n")
    if body.endswith("\n"):
        body = body[:-1]
    if body == "":
        return None
    return [row.split("\t") for row in body.split("\n")]


def _column_index(letters: str) -> int:
    column = 0
    for char in letters:
        column = column * 26 + (ord(char) - 64)
    return column - 1


def _column_letters(index: int) -> str:
    number = index + 1
    letters = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def shift_sheet_address(origin: str, columns: int, rows: int) -> str | None:
    """``origin`` moved ``columns`` right and ``rows`` down. None when it is not an address."""
    match = _SHEET_ADDRESS.match(origin or "")
    if match is None:
        return None
    letters = "".join(char for char in origin if char.isalpha())
    row_number = int("".join(char for char in origin if char.isdigit()))
    column = _column_index(letters) + columns
    row = row_number - 1 + rows
    if column < 0 or row < 0:
        return None
    return f"{_column_letters(column)}{row + 1}"


def _classify_sheet_cell(cell, wanted: str) -> tuple[str, str | None]:
    """``match``, ``editor``, ``waiting``, or ``differ``, plus the text to show.

    A truncated range formula is ``editor``: the Formula attribute is not
    the whole request, and F2 is the caller's job, outside any poll. A
    formula request is not the number the cell displays. An empty cell
    that should hold text is ``waiting``, because the value can still be
    on its way. A missing cell is the same wait: LibreOffice omits it
    until the edit commits. A cell that already shows different text is
    ``differ``. Missing or extra characters are not a match.
    """
    if cell is None:
        return "waiting", None
    shown = _full_text(cell) or ""
    formula = _sheet_formula(cell)
    if wanted == "":
        if shown.strip() == "" and not formula:
            return "match", shown
        return "differ", _sheet_formula_text(cell) or shown
    if sheet_cell_matches(cell, wanted):
        return "match", shown
    if _formula_needs_editor(formula, wanted):
        return "editor", _sheet_formula_text(cell) or shown
    if shown.strip() == "" and not formula:
        return "waiting", shown
    return "differ", _sheet_formula_text(cell) or shown


def _sheet_table(app: str):
    try:
        from a11y_computer_use.schema import Scope

        root = _fresh_sheet_root(app, Scope.WINDOW)
    except Exception:
        return None
    if root is None:
        return None
    return _without_child_cache(root, lambda: _find_spreadsheet(root, [80]))


def _cell_for_address(app: str, address: str):
    parts = shift_sheet_address(address, 0, 0)
    if parts is None:
        return None
    table = _sheet_table(app)
    if table is None:
        return None
    letters = "".join(char for char in address if char.isalpha())
    row = int("".join(char for char in address if char.isdigit())) - 1
    column = _column_index(letters)
    try:
        Atspi = _atspi()
    except Exception:
        return None
    return _safe(lambda: Atspi.Table.get_accessible_at(table, row, column))


def _scan_sheet_grid(app: str, origin: str, grid: list[list[str]]):
    """The first unresolved cell, or the formula cells that still need F2.

    ``("match", None)`` when every cell matches without the editor.
    ``("editor", [(address, wanted, cell), ...])`` when the only gaps are
    range formulas cut at the colon. An earlier cut formula is unresolved,
    so a later hard difference stays behind it until F2 runs. ``("waiting",
    ...)`` is an empty cell that may still be publishing. ``("differ",
    address, wanted, actual)`` is the first cell that already disagrees.
    """
    editors = []
    for row_offset, row in enumerate(grid):
        for column_offset, wanted in enumerate(row):
            address = shift_sheet_address(origin, column_offset, row_offset)
            cell = _cell_for_address(app, address) if address else None
            kind, actual = _classify_sheet_cell(cell, wanted)
            if kind == "match":
                continue
            if kind == "editor":
                editors.append((address, wanted, cell))
                continue
            if editors:
                return ("editor", editors, kind, address, wanted, actual)
            if kind == "waiting":
                return ("waiting", address, wanted, actual)
            return ("differ", address, wanted, actual)
    if editors:
        return ("editor", editors)
    return ("match", None)


_SHEET_GRID_WAIT_S = 3.0
_SHEET_GRID_PAUSE_S = 0.05
_SHEET_GRID_COMMIT_PAUSE_S = 0.25


def _commit_open_sheet_editor(app: str) -> bool:
    """Return once when a cell editor is open. True when that key was sent.

    LibreOffice leaves the text interface of every other cell empty while
    the editor is open, and F2 would read this editor instead of the cell
    it was aimed at. Return commits the editor. The caret then moves the
    way Enter does in Calc. A grid that already ended on Enter has no
    editor, so nothing is sent.
    """
    if not sheet_editor_text(app):
        return False
    from a11y_computer_use.drivers import _linux_input

    _linux_input.press_chord("return")
    return True


def sheet_grid_landing(origin: str, rows: int) -> str | None:
    """The cell Enter selects after ``rows`` rows of a grid from ``origin``.

    Tab moves right. Enter commits the cell and returns to the column where
    that row of the grid started, one row down. After the last typed row,
    that is the cell after the last typed cell in the grid's flow. A 5-row
    grid from A1 lands on A6. A 2-row grid from C3 lands on C5.
    """
    if rows < 0:
        return None
    return shift_sheet_address(origin, 0, rows)


def _last_typed_sheet_address(origin: str, grid: list[list[str]]) -> str | None:
    if not grid or not grid[-1]:
        return None
    return shift_sheet_address(origin, len(grid[-1]) - 1, len(grid) - 1)


def _sheet_caret_address(app: str) -> str | None:
    """The active Calc cell, from focus, then from the selection."""
    focused = None
    try:
        focused = _focused_sheet_address(app)
    except Exception:
        focused = None
    if sheet_address(focused):
        return focused
    try:
        selected = selected_sheet_address(app)
    except Exception:
        selected = None
    return sheet_address(selected)


def _grid_caret_landing(app: str, origin: str, grid: list[list[str]]) -> str | None:
    """Where the caret goes after the grid is verified.

    LibreOffice's Enter move wins when it has already left the last typed
    cell. Otherwise the landing is the origin column on the next row. The
    formula read focuses the cell it opens; the caller puts the caret back
    here so the next type does not replace that cell.
    """
    computed = sheet_grid_landing(origin, len(grid))
    last = _last_typed_sheet_address(origin, grid)
    try:
        seen = _sheet_caret_address(app)
    except Exception:
        seen = None
    if seen and seen != last:
        return seen
    return computed


def _place_sheet_caret(app: str, address: str | None) -> bool:
    """Select ``address`` without opening its editor.

    False when ``address`` is missing. Already sitting on that cell is
    success and does not grab focus again. This is not a write.
    """
    if not address:
        return False
    try:
        current = _sheet_caret_address(app)
    except Exception:
        current = None
    if current == address:
        return True
    cell = _cell_for_address(app, address)
    if cell is None:
        return False
    return grab_focus(cell)


def sheet_grid_check(app: str, origin: str, text: str):
    """None when every typed cell matches. Otherwise ``(address, wanted, actual)``.

    The rectangle starts at ``origin``, the cell that was active before the
    keys. Comparison is per cell: formula text, or Calc's numeric display.
    A formula is not accepted as the number it computes, and a Formula
    attribute cut at ``:`` is not a prefix match. A missing cell, or a cell
    that is still empty, is read again until the committed value is
    published or the wait ends. A cell that shows different text is a
    mismatch. Missing or extra characters are not ignored. An editor left
    open on the last cell is committed once, with Return, before those
    reads. F2 runs once, after that wait, and only for formulas the
    attribute cut at ``:``. After that read the caret is put back on the
    cell Enter moves to: the cell after the last typed cell in the grid's
    flow. The next type does not replace a cell the grid just filled.
    """
    grid = sheet_type_grid(text)
    if grid is None or not origin:
        return (origin or "", text, None)
    editors: list = []
    waiting = None
    pending = None
    committed = False
    deadline = time.monotonic() + _SHEET_GRID_WAIT_S
    while True:
        # Reading another cell while Calc's editor is open can time out and
        # make libatspi mark the application hung. Commit before any cell
        # reads so the native connection remains usable for verification.
        if not committed and _commit_open_sheet_editor(app):
            committed = True
            time.sleep(_SHEET_GRID_COMMIT_PAUSE_S)
        scanned = _scan_sheet_grid(app, origin, grid)
        kind = scanned[0]
        if kind == "match":
            return None
        if kind == "differ":
            _status, address, wanted, actual = scanned
            return (address, wanted, actual)
        if kind == "editor" and len(scanned) == 2:
            editors = scanned[1]
            waiting = None
            pending = None
            break
        if kind == "editor":
            # Cut formulas come first. Keep polling while a later cell is
            # still empty; do not open the editor inside this loop.
            _status, editors, later, address, wanted, actual = scanned
            if later == "waiting":
                waiting = (address, wanted, actual)
                pending = None
            else:
                waiting = None
                pending = (address, wanted, actual)
                break
        else:
            _status, address, wanted, actual = scanned
            waiting = (address, wanted, actual)
            editors = []
            pending = None
        if time.monotonic() >= deadline:
            break
        time.sleep(_SHEET_GRID_PAUSE_S)
    landing = _grid_caret_landing(app, origin, grid)
    try:
        for address, wanted, cell in editors:
            opened = committed_sheet_formula(cell, app) if cell is not None else None
            if opened and formulas_match(opened, wanted):
                continue
            return (address, wanted, opened)
        if pending is not None:
            return pending
        if waiting is not None:
            return waiting
        return None
    finally:
        if editors:
            _place_sheet_caret(app, landing)


def sheet_address(title: str | None) -> str | None:
    """A Calc address such as ``B4``, or None when ``title`` is not one."""
    text = (title or "").strip()
    if _SHEET_ADDRESS.match(text):
        return text
    return None


def _focused_sheet_address(app: str) -> str | None:
    """Address of the focused or selected visible Calc cell, or None.

    LibreOffice 24 leaves ``Selection`` empty while the active cell is the
    focused table cell. That address is the one the Name Box shows.
    """
    try:
        from a11y_computer_use.schema import Scope

        root = find_root(app, Scope.WINDOW)
    except Exception:
        return None
    if root is None:
        return None
    table = _find_spreadsheet(root, [80])
    if table is None:
        return None
    focused = None
    selected = None
    for cell in _sheet_window_cells(table):
        name = sheet_address(_node_name(cell))
        if name is None:
            continue
        _enabled, is_focused, _checked, is_selected, *_rest = _state_flags(cell)
        if is_focused and focused is None:
            focused = name
        if is_selected and selected is None:
            selected = name
    return focused or selected


def sheet_click_verdict(app: str, address: str) -> tuple[str, str] | None:
    """``(outcome, evidence)`` for a click aimed at a Calc cell, or None.

    Confirmed only when the active cell's address is ``address``. The
    address is the selection, or the focused visible cell when the
    selection interface is empty. That is the Name Box. A click that
    leaves a different cell active is partial, not confirmed. A window
    that is not a Calc sheet is None, so other clicks keep their own check.
    """
    wanted = sheet_address(address)
    if wanted is None or not libreoffice_app(app):
        return None
    try:
        sheet = spreadsheet_open(app)
    except Exception:
        return None
    if not sheet:
        # Scope.WINDOW follows the active window. A Calc modal replaces
        # the sheet root, so lack of a grid here is not evidence that this
        # was a non-Calc click. Do not let dialog/tree changes confirm it.
        try:
            from a11y_computer_use.schema import Scope

            root = find_root(app, Scope.WINDOW)
            if root is not None and _role_name(root) in {"dialog", "alert"}:
                name = _node_name(root) or "unnamed dialog"
                return "partial", (
                    f"active dialog {name!r}; the selected cell {wanted} "
                    "could not be verified"
                )
        except Exception:
            pass
        return None
    latest: str | None = None
    for attempt in range(6):
        try:
            selected = selected_sheet_address(app)
        except Exception:
            selected = None
        if selected == wanted:
            return "confirmed", f"the selected cell is {wanted}"
        if sheet_address(selected):
            # A published address that is not the target is the answer.
            # A focused cell must not confirm over it.
            latest = selected
        elif attempt in (1, 4):
            try:
                focused = _focused_sheet_address(app)
            except Exception:
                focused = None
            if focused == wanted:
                return "confirmed", f"the selected cell is {wanted}"
            if sheet_address(focused):
                latest = focused
        if attempt + 1 < 6:
            time.sleep(0.05)
    if latest and latest != wanted:
        return "partial", f"the selected cell is {latest}, not {wanted}"
    return "partial", f"the selected cell is not {wanted}"


def live_sheet_cell(acc, address: str):
    """Fresh ``RawNode`` of an issued Calc cell handle, or None.

    The handle has to be alive and SHOWING, still named ``address``, and a
    child of the Calc grid. The read is the snapshot's own accessor, so the
    box goes through the same LibreOffice placement as a full walk. Any
    failed read is None, which keeps the full-tree resolution.
    """
    wanted = sheet_address(address)
    if wanted is None or acc is None:
        return None
    try:
        if accessible_gone(acc) or not _state_has(acc, "SHOWING"):
            return None
        if _node_name(acc) != wanted:
            return None
        parent = _parent_of(acc)
        if parent is None or not _spreadsheet_table(parent):
            return None
        accessor = ATSPIAccessor()
        accessor.libreoffice = True
        raw = accessor.read(acc)
    except Exception:
        return None
    if raw is None or (raw.title or "") != wanted:
        return None
    return raw


def selected_sheet_address(app: str) -> str | None:
    """The selected Calc cell's address, or None.

    ``Selection.get_selected_child`` is one round-trip. The cell does not
    have to be the focused accessible: while the grid holds focus, Down
    still moves this address.
    """
    cell = _selected_sheet_cell(app)
    if cell is None:
        return None
    name = _node_name(cell)
    if _SHEET_ADDRESS.match(name):
        return name
    return None


def focused_type_selection(app: str) -> tuple:
    """``(readable text, selection span)`` for the focused node.

    The span is in that readable text. A contenteditable word selection is
    the word, not the paragraph object that contains it. A collapsed caret
    is ``(text, None)``.
    """
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        return (None, None)
    if truncated or acc is None:
        return (None, None)
    try:
        text = _readable_text(acc)
    except Exception:
        text = None
    try:
        span = _selection_span(acc, text)
    except Exception:
        span = None
    return (text, span)


def retry_evidence(action, target, focus):
    """Read retry signals with the native application child cache bypassed."""
    from a11y_computer_use.schema import Click, Element, KeyChord

    formatting = isinstance(action, KeyChord) and action.chord.casefold() in {
        "ctrl+b", "ctrl+i", "ctrl+u", "cmd+b", "cmd+i", "cmd+u",
    }
    ref_click = isinstance(action, Click) and isinstance(action.target, Element)
    if target is None or not (formatting or ref_click):
        return None
    return _without_child_cache(target, lambda: _retry_evidence(action, target, focus))


def _retry_evidence(action, target, focus):
    """Scoped evidence for formatting and inert labels; unknown elsewhere.

    No Collection query, whole-window traversal, hit test, or focus-discovery
    walk is allowed here. Unsupported signals remain None, never an empty success.
    The two reads share a 0.1 s traversal budget (native IPC can still fail).
    """
    from functools import cache

    from a11y_computer_use.outcome import RetryEvidence
    from a11y_computer_use.schema import Click, Element, KeyChord

    if target is None:
        return None
    deadline = time.monotonic() + 0.04
    formatting = isinstance(action, KeyChord) and action.chord.casefold() in {
        "ctrl+b", "ctrl+i", "ctrl+u", "cmd+b", "cmd+i", "cmd+u",
    }
    element = action.target if isinstance(action, Click) and isinstance(action.target, Element) else None
    outline = (element is not None and element.note == "activates on double-click"
               and action.count == 1 and not action.modifiers)
    try:
        _drop_cached_properties(target)
        if is_secure(target):
            return None
        if not formatting and not outline and (element is None or element.role not in {"AXStaticText", "AXButton"}):
            # No readable no-op signal exists for this click. In a Chromium
            # page that is unknown, and the browser cache can answer while
            # renderer input is queued, so say so (re-observe, never retry).
            if (element is not None and _chromium_app(target)
                    and any(_role_name(node) in {"document web", "document frame"}
                            for node in _ancestor_chain(target))):
                from a11y_computer_use.drivers._page_health import UNKNOWN
                return RetryEvidence(reason=UNKNOWN)
            return None
        if time.monotonic() >= deadline:
            return None
        if outline:
            states = _call_first(target, ("get_state_set",))
            flags = _call_first(states, ("get_states",))
            if flags is None:
                return None
            selected = any("SELECTED" in str(flag) for flag in flags)
            return RetryEvidence(effect=(target, "outline_selection", selected))
        if formatting:
            if not _state_has(target, "FOCUSED"):
                return RetryEvidence(toggle=True, reason="the focused text owner could not be resolved")
            api = _atspi().Text
            caret = api.get_caret_offset(target)
            selections = api.get_n_selections(target)
            if type(caret) is not int or caret < 0 or type(selections) is not int or selections < 0:
                return RetryEvidence(toggle=True)
            if selections:
                span = _selection_bounds(api.get_selection(target, 0))
                if span is None:
                    return RetryEvidence(toggle=True)
                caret = span[0]
            attributes, start, end = api.get_attribute_run(target, caret, False)
            attribute = {"b": "weight", "i": "style", "u": "underline"}[action.chord[-1].lower()]
            value = attributes.get(attribute)
            if value is None or not start <= caret < end or time.monotonic() >= deadline:
                return RetryEvidence(toggle=True, reason="formatting attributes could not be read")
            return RetryEvidence(
                toggle=True, effect=(target, caret, attribute, value),
                effect_reason="the selected text formatting attribute changed",
            )
        # Read actual capabilities, not snapshot role/layout heuristics. GTK
        # labels can be buttons; web buttons can expose readable inert content.
        parent = _call_first(target, ("get_parent", "getParent"))
        application = _call_first(target, ("get_application", "getApplication"))
        if parent is None or application is None:
            return None

        @cache
        def identity(node):
            if time.monotonic() >= deadline:
                raise TimeoutError
            _drop_cached_properties(node)
            name = _call_first(node, ("get_name", "getName"))
            role = _call_first(node, ("get_role_name", "getRoleName"))
            states = _call_first(node, ("get_state_set",))
            flags = _call_first(states, ("get_states",))
            if name is None or role is None or flags is None:
                raise ValueError("unreadable identity/state")
            if role in {"canvas", "drawing area"} or role == "image" and not name:
                raise ValueError("opaque signal")
            return node, role, name, tuple(str(flag) for flag in flags)

        @cache
        def child_count(node):
            count = _call_first(node, ("get_child_count", "getChildCount"))
            if type(count) is not int or count < 0:
                raise ValueError("unreadable child count")
            return count

        @cache
        def content(node):
            # No Text/Value interface is a readable absence, distinct from a
            # supported interface whose native read failed.
            if is_secure(node):
                raise ValueError("secure signal")
            interfaces = _call_first(node, ("get_interfaces",))
            if interfaces is None:
                raise ValueError("unreadable capabilities")
            text = _full_text(node) if "Text" in interfaces else ()
            value = ()
            if "Value" in interfaces:
                value_iface = _call_first(node, ("get_value_iface",))
                value = _call_first(value_iface, ("get_current_value",))
            if text is None or value is None:
                raise ValueError("unreadable text/value")
            return text, value

        if identity(parent)[1] in {"frame", "dialog", "alert", "application"}:
            return None  # A parent probe must not become a whole-window walk.
        parent_count = child_count(parent)
        window_count = child_count(application)
        if parent_count > 8 or window_count > 8:
            return None
        windows = []
        for index in range(window_count):
            node = _call_first(application, ("get_child_at_index", "getChildAtIndex"), index)
            if node is None:
                return None
            windows.append(identity(node))
        ancestors = []
        owner = parent
        for _ in range(16):
            row = identity(owner)
            ancestors.append((row, child_count(owner)))
            if any(owner == window[0] for window in windows):
                break
            owner = _call_first(owner, ("get_parent", "getParent"))
            if owner is None:
                return None
        else:
            return None
        # Scope to the parent subtree and the ancestor chain, never walk the
        # app/window to discover focus. Any truncated subtree stays unknown.
        siblings = []
        container = parent
        # Unnamed layout wrappers are not a semantic boundary. Include their
        # nearest named container/document, stopping before the app window.
        for row, _count in ancestors:
            if row[1] in {"frame", "dialog", "alert"}:
                break
            container = row[0]
            if row[2] or row[1] in {"document web", "document frame"}:
                break
        container_rows = []
        count = child_count(container)
        if count > 32:
            return None
        for index in range(count):
            child = _call_first(container, ("get_child_at_index", "getChildAtIndex"), index)
            if child is None:
                return None
            # Sibling subtree membership needs native identity and child
            # count; selection/state/text reads belong to the target parent.
            # Do not expand unrelated document controls merely for context.
            container_rows.append((child, child_count(child)))
        pending = [parent]
        while pending:
            node = pending.pop()
            if len(siblings) >= 32:
                return None
            row = identity(node)
            count = child_count(node)
            siblings.append((row, count))
            if count > 32 - len(pending) - len(siblings):
                return None
            for index in range(count):
                child = _call_first(node, ("get_child_at_index", "getChildAtIndex"), index)
                if child is None:
                    return None
                pending.append(child)
        documents = []
        for row, _count in ancestors:
            if row[1] in {"document web", "document frame"}:
                iface = _call_first(row[0], ("get_document_iface",))
                attributes = _call_first(iface, ("get_document_attributes",))
                if attributes is None:
                    return None
                documents.append((row, tuple(sorted(attributes.items() if isinstance(attributes, dict) else attributes))))
        # Re-resolve focus among already scoped nodes (a click may have moved
        # it since the public snapshot). A cached focused node must still read
        # FOCUSED; an unresolved owner cannot establish a no-op.
        candidates = [row for row, _count in siblings]
        candidates += [row for row, _count in ancestors]
        focus_identity = next((row for row in candidates if any("FOCUSED" in flag for flag in row[3])), None)
        if focus_identity is None and focus is not None:
            row = identity(focus)
            if any("FOCUSED" in flag for flag in row[3]):
                focus_identity = row
        if focus_identity is None:
            return None
        state = identity(target)
        text, value = content(target)
        if time.monotonic() >= deadline:
            return None
        return RetryEvidence(
            context=(focus_identity, tuple(windows), (parent, parent_count)),
            signals=(("target_identity", target), ("target_states", state[3]),
                     ("target_name", state[2]), ("target_value", value),
                     ("target_text", text), ("parent_selection", tuple(siblings)),
                     ("container_children", (tuple(container_rows), tuple(siblings))), ("document", (tuple(documents), tuple(ancestors), tuple(windows)))),
        )
    except Exception:
        return None


def writer_key_state(
    app: str, previous: tuple | None = None, *, structure: bool = False,
    selection: bool = False,
) -> tuple | None:
    """Read Writer or Impress/Draw evidence, optionally Writer sibling selections.

    Reuse the native handle while it remains focused in the same window.
    Missing native offsets are unknown, not fabricated caret positions.
    """
    if not libreoffice_app(app):
        return None
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    if root is None or (previous is not None and root != previous[0]):
        return None
    acc = previous[2] if previous is not None and not structure else None
    if acc is not None:
        _call_first(acc, ("clear_cache", "clearCache"))
    if acc is None or not _state_has(acc, "FOCUSED"):
        acc, truncated = _focused_node(app)
        if truncated or acc is None:
            return None
    document = writer_document_for(acc)
    if document is None:
        document = _drawing_document_of(acc)
        if document is not None:
            # A focused frame alone is not text evidence.
            focused_object = acc
            if _role_name(acc) not in {"paragraph", "heading"}:
                frame = _drawing_frame(document)
                paragraphs = _frame_paragraphs(frame) if frame is not None else []
                acc = next((node for node in paragraphs if (
                    _state_has(node, "FOCUSED") or _state_has(node, "ACTIVE")
                )), None)
            if acc is None:
                if previous is not None and document != previous[1]:
                    return None
                iface = _call_first(document, ("get_selection_iface", "get_selection"))
                count = _call_first(iface, ("get_n_selected_children", "getNSelectedChildren"))
                if type(count) is not int or not 0 <= count <= 256:
                    return None
                selected = tuple(
                    _call_first(iface, ("get_selected_child", "getSelectedChild"), index)
                    for index in range(count)
                )
                if any(node is None for node in selected) or is_secure(focused_object):
                    return None
                return (root, document, focused_object, selected)
            if _editable_iface(acc) is None:
                return None
            # Paste and undo can split frames too; always read sibling metadata.
            structure = True
    if document is None or (previous is not None and document != previous[1]) or is_secure(acc):
        return None
    _call_first(acc, ("clear_cache", "clearCache"))
    Text = _atspi().Text
    count = _safe(lambda: Text.get_character_count(acc))
    caret = _safe(lambda: Text.get_caret_offset(acc))
    nselections = _safe(lambda: Text.get_n_selections(acc))
    if any(type(value) is not int or value < 0 for value in (count, caret, nselections)):
        return None
    if caret > count or nselections > 256:
        return None
    selections = []
    for index in range(nselections):
        span = _selection_bounds(_safe(lambda: Text.get_selection(acc, index)))
        if span is None or not 0 <= span[0] <= span[1] <= count:
            return None
        selections.append(span)
    text = _full_text(acc)
    if text is None:
        return None
    state = (root, document, acc, count, caret, tuple(selections), text)
    if selection and writer_document_for(acc) == document and (
        count == 0 or previous is not None and len(previous) == 8
    ):
        # Empty paragraphs cannot expose a selection. Read complete sibling
        # ranges; missing or oversized sibling lists are not evidence.
        parent = _parent_of(acc)
        if parent is None:
            return None
        _call_first(parent, ("clear_cache", "clearCache"))
        children = _call_first(parent, ("get_child_count", "getChildCount"))
        if type(children) is not int or not 0 <= children <= 256:
            return None
        ranges = []
        for index in range(children):
            child = _call_first(parent, ("get_child_at_index", "getChildAtIndex"), index)
            if child is None:
                return None
            if _role_name(child) != "paragraph":
                continue
            if is_secure(child) or writer_document_for(child) != document:
                return None
            _call_first(child, ("clear_cache", "clearCache"))
            size = _safe(lambda: Text.get_character_count(child))
            number = _safe(lambda: Text.get_n_selections(child))
            if type(size) is not int or size < 0 or type(number) is not int or not 0 <= number <= 256:
                return None
            spans = []
            for selected in range(number):
                span = _selection_bounds(_safe(lambda: Text.get_selection(child, selected)))
                if span is None or not 0 <= span[0] <= span[1] <= size:
                    return None
                spans.append(span)
            ranges.append((child, tuple(spans)))
        return state + (tuple(ranges),)
    if not structure:
        return state
    # Paragraph splitting can preserve text/caret and even reuse the handle.
    # Read native sibling metadata without enumerating the document subtree.
    parent = _parent_of(acc)
    if parent is None:
        return None
    _call_first(parent, ("clear_cache", "clearCache"))
    children = _call_first(parent, ("get_child_count", "getChildCount"))
    index = _call_first(acc, ("get_index_in_parent", "getIndexInParent"))
    if type(children) is not int or type(index) is not int or not 0 <= index < children:
        return None
    return state + ((parent, children, index),)


def sheet_key_state(app: str, previous: tuple | None = None) -> tuple | None:
    """Read a bounded Calc selection, then reread those exact native cells.

    No truncated selection is used as evidence. Missing Text, changed windows,
    editors and dialogs retain the ordinary outcome fallback.
    """
    if not libreoffice_app(app):
        return None
    from a11y_computer_use.schema import Scope

    root = find_root(app, Scope.WINDOW)
    table = _find_spreadsheet(root, [80])
    if table is None:
        return None
    focused, truncated = _focused_node(app)
    if truncated or focused is None or _role_name(focused) not in {"table", "table cell"}:
        return None
    if previous is not None:
        old_table, records = previous
        if table != old_table:
            return None
        cells = [record[0] for record in records]
    else:
        iface = _call_first(table, ("get_selection_iface", "get_selection"))
        count = _call_first(iface, ("get_n_selected_children", "getNSelectedChildren"))
        if not isinstance(count, int) or count < 0 or count > 256:
            return None
        cells = [
            _call_first(iface, ("get_selected_child", "getSelectedChild"), index)
            for index in range(count)
        ]
        if not cells:
            cells = [cell for cell in _sheet_window_cells(table) if _state_flags(cell)[1]]
            if len(cells) != 1:
                return None
    result = []
    for index, cell in enumerate(cells):
        if cell is None or _role_name(cell) != "table cell" or is_secure(cell):
            return None
        address = sheet_address(_node_name(cell))
        text = _full_text(cell)
        if address is None or text is None:
            return None
        if previous is not None and address != records[index][1]:
            return None
        result.append((cell, address, text))
    return (table, tuple(result))


def _chromium_page_key_state(acc) -> tuple | None:
    """Read a native page anchor and window metadata without walking the page.

    The first child remains the scroll anchor even when it moves off screen.
    Missing geometry is unknown, not evidence of scrolling.
    """
    if acc is None or not _chromium_app(acc):
        return None
    document = window = None
    for node in _ancestor_chain(acc):
        role = _role_name(node)
        if document is None and role in {"document web", "document frame"}:
            document = node
        if role in {"frame", "window", "dialog"}:
            window = node
            break
    if document is None or window is None:
        return None
    # Clear only these nodes, not a recursively populated page cache.
    for node in (document, window):
        _call_first(node, ("clear_cache",))
    child = _child_at(document, 0)
    if child is None:
        return None
    _call_first(child, ("clear_cache",))
    rect = _raw_rect(child, "WINDOW")
    count = _call_first(window, ("get_child_count", "get_childCount"))
    name = _call_first(window, ("get_name", "getName"))
    if (
        rect is None or not all(math.isfinite(value) for value in rect)
        or rect[2] <= 0 or rect[3] <= 0
        or not isinstance(count, int) or count < 0 or name is None
    ):
        return None
    return (_ancestor_key(document), _ancestor_key(window), _ancestor_key(child), rect, name, count)


def focused_key_evidence(app: str, *, include_page: bool = False) -> tuple:
    """``(text, caret, selection start, selection end, cell address)``.

    Text is the focused node's text. An open Calc cell editor replaces it,
    because the grid's own text stays empty while the digits are in the
    editor. The cell address is the selected sheet cell, falling back to
    the focused visible cell when Calc exposes an empty Selection. Missing
    pieces are None. This is not a password check. With include_page, a sixth
    item carries scoped Chromium document/window evidence when available.
    """
    text = None
    caret = start = end = None
    try:
        acc, truncated = _focused_node(app)
    except Exception:
        acc, truncated = None, True
    if acc is not None and not truncated:
        try:
            text = _readable_text(acc)
        except Exception:
            text = None
        try:
            Atspi = _atspi()
            count = _safe(lambda: Atspi.Text.get_character_count(acc))
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                nchars = count
            else:
                nchars = len(text or "")
            caret, start, end = _caret_and_selection(acc, nchars)
        except Exception:
            caret = start = end = None
    if libreoffice_app(app):
        try:
            editor = sheet_editor_text(app)
        except Exception:
            editor = None
        if editor:
            text = editor
    cell = None
    if libreoffice_app(app):
        try:
            cell = selected_sheet_address(app)
        except Exception:
            cell = None
        if cell is None:
            try:
                cell = _focused_sheet_address(app)
            except Exception:
                cell = None
    result = (text, caret, start, end, cell)
    if include_page and acc is not None and not truncated:
        try:
            page = _chromium_page_key_state(acc)
        except Exception:
            page = None
        if page is not None:
            return (*result, page)
    return result


def control_kind(acc) -> str | None:
    """``combo``, ``value``, or None when ``set_text`` is the writer.

    A select-style combo is set through its own items or its own entry.
    An editable input combobox stays on ``set_text``. A spin button,
    slider, or other Value-interface control is set through current value.
    A plain text role stays on ``set_text``. A scroll bar's Value drives
    pixel scroll and is not a ``set_value`` target.

    Qt publishes Value on labels and empty text as well. Those are not
    numeric targets. A Qt slider, spin button, or progress bar is a numeric
    target when its minimum and maximum are a real range; sliders/spins
    reporting 0..0 instead have unknown bounds. A non-Qt
    control keeps the previous rule: a spin button, a slider, a number
    input, or any other non-text role that exposes a range.
    """
    role = _role_name(acc)
    if role == "combo box":
        return None if editable_entry(acc) else "combo"
    if role in {"list box", "list"} and _collect_options(acc):
        return "combo"
    # A missing role is a fake or a node we cannot classify. Do not probe
    # Value: that import is gi, and a plain text write must stay on set_text.
    if role == "scroll bar" or role == "":
        return None
    if _qt_app(acc):
        if role in _RANGE_ROLE_NAMES:
            span = _value_range(acc)
            if span is not None and (_sane_range(*span) or _qt_unknown_value_range(acc, span)):
                return "value"
        return None
    # A Chrome date segment is a spin button whose Value interface is not the
    # segment text. set_text types the segment and reads valuetext.
    if _chrome_date_segment(acc):
        return None
    if role in {"spin button", "slider"} or _number_input(acc):
        return "value"
    if role in _TEXT_ROLE_NAMES or is_sheet_cell(acc):
        # A spreadsheet cell's Value interface is the numeric range of a
        # double, including 0.0 on an empty cell. The text is written by
        # typing, not by Value.set_current_value. Checked before
        # ``_value_range``, which imports gi.
        return None
    # A Draw shape panel has no Value interface. The bridge still answers
    # minimum and maximum with an uninitialized double. That is not a range.
    if _value_range(acc) is not None:
        return "value"
    return None


def _collect_options(acc) -> list[tuple[str, object, object, int]]:
    """(label, node, parent, index) for each choice under ``acc``.

    Enumerate every child: GTK font menus routinely have hundreds of items,
    and the flattened order is also the combo's Selection model index.
    """
    found: list[tuple[str, object, object, int]] = []

    def walk(node, depth: int) -> None:
        if depth > 5:
            return
        count = _child_count(node)
        for index in range(count):
            child = _child_at(node, index)
            if child is None:
                continue
            role = _role_name(child)
            if role in _OPTION_ROLE_NAMES:
                label = _node_name(child)
                if not label:
                    raw = _direct_text(child)
                    label = _strip_objects(raw) if isinstance(raw, str) else ""
                found.append((label, child, node, index))
                continue
            if role in _TEXT_ROLE_NAMES or role in {"label", "static", "push button"}:
                continue
            if depth == 0 or role in _OPTION_CONTAINER_NAMES:
                walk(child, depth + 1)

    walk(acc, 0)
    return found


def _combo_entry(acc):
    """The editable entry that belongs to this combo, not some other focused field."""
    count = min(_child_count(acc), 12)
    for index in range(count):
        child = _child_at(acc, index)
        if child is None:
            continue
        role = _role_name(child)
        if role not in {"entry", "text"}:
            continue
        if _qt_app(acc) and not any(
            _state_has(child, state) for state in ("ENABLED", "SENSITIVE", "FOCUSABLE")
        ):
            continue
        # A stale client child list can hand back a different field (Firefox
        # once returned the focused Notes textarea for a <select>); writing
        # it reported "set" on the wrong target. The entry must name this
        # combo as its parent.
        parent = _parent_of(child)
        acc_key = _atspi_wrapper_key(acc)
        if parent is not acc and (acc_key is None or _atspi_wrapper_key(parent) != acc_key):
            continue
        if _editable_iface(child) is not None or role in {"entry", "text"}:
            return child
    return None


def _label_texts(acc) -> set[str]:
    """Folded LABELLED_BY strings, with and without a trailing colon."""
    raw = _labelled_by_name(acc).strip()
    if not raw:
        return set()
    names = {raw.casefold()}
    stripped = _strip_label_colon(raw)
    if stripped:
        names.add(stripped.casefold())
    return names


def _sibling_entry_text(acc) -> str | None:
    """Text of an entry that sits beside the combo, not inside it.

    LibreOffice's GTK combo hides the stock combo and shows a button. The
    editable text, when there is one, is a sibling entry. A label sibling is
    the caption, not the value.
    """
    parent = _parent_of(acc)
    if parent is None:
        return None
    captions = _label_texts(acc)
    count = min(_child_count(parent), 8)
    for index in range(count):
        child = _child_at(parent, index)
        if child is None or child is acc:
            continue
        if _role_name(child) not in {"entry", "text"}:
            continue
        label = _option_label(child).strip()
        if label and label.casefold() not in captions:
            return label
    return None


def _descendant_combo_value(acc) -> str | None:
    """A selected descendant, or the one text the combo itself displays.

    The active menu item is already ``_selected_option_text``. This is the
    rest: a selected table cell (LibreOffice's popup is a tree, not a menu),
    or a single label/entry under the combo when nothing is selected. Menu
    rows that are only listed are not the value. Several different texts are
    not one value.
    """
    captions = _label_texts(acc)
    selected: list[str] = []
    plain: list[str] = []

    def take(child, into: list[str]) -> None:
        label = _option_label(child).strip()
        if not label or label.casefold() in captions or label in into:
            return
        into.append(label)

    def walk(node, depth: int) -> None:
        if depth > 4 or selected:
            return
        count = min(_child_count(node), 32)
        for index in range(count):
            child = _child_at(node, index)
            if child is None:
                continue
            role = _role_name(child)
            if role in {"menu", "popup menu"}:
                continue
            if _state_has(child, "SELECTED"):
                take(child, selected)
                if selected:
                    return
            elif role in {"entry", "text", "label", "static"}:
                take(child, plain)
                continue
            if role in _OPTION_CONTAINER_NAMES or role in {
                "table", "tree table", "tree", "viewport", "filler", "panel",
                "scroll pane", "box",
            }:
                walk(child, depth + 1)
                if selected:
                    return

    walk(acc, 0)
    if selected:
        return selected[0]
    if len(plain) == 1:
        return plain[0]
    return None


def _choice_value(acc, role: str, role_name: str) -> tuple[bool, str | None]:
    """(handled, value) for a combo or list.

    An editable combo's value is its own entry's text, including when that
    text is empty. A non-editable combo uses the active option, then its own
    text, then a sibling entry or a displayed descendant. Neither falls
    through to the Value interface. The label relation is the title, not the
    value.
    """
    if role not in _CHOICE_ROLES and role_name not in _CHOICE_ROLE_NAMES:
        return False, None
    entry = _combo_entry(acc)
    if entry is not None:
        text = _full_text(entry)
        if isinstance(text, str):
            return True, (_strip_objects(text) or None)
        return True, None
    # Linux Qt stores the current item in the accessible name. That name is
    # the value. The label relation, applied in the snapshot title, is the
    # widget's name. The Value interface on the combo is uninitialized.
    if _qt_app(acc):
        current = _node_name(acc)
        if current:
            return True, current
    selected = _selected_option_text(acc)
    if selected:
        return True, selected
    if role_name != "combo box" and role != "AXComboBox":
        return True, None
    shown = _direct_text(acc)
    cleaned = _strip_objects(shown).strip()
    if cleaned and cleaned.casefold() not in _label_texts(acc):
        return True, cleaned
    sibling = _sibling_entry_text(acc)
    if sibling:
        return True, sibling
    return True, _descendant_combo_value(acc)


def _set_entry_contents(entry, value: str) -> bool:
    """Replace ``entry`` via EditableText. No focus change and no keystrokes.

    Keystrokes would land in whichever widget is focused, which is how a combo
    write changed a different field. ``set_text_contents`` writes this entry.
    """
    eti = _editable_iface(entry)
    if eti is None:
        return False
    if _full_text(entry) == value and _qt_text_count_matches(entry, value):
        return True
    _call_first(eti, ("set_text_contents",), value, default=False)
    if _confirm_text(entry, value):
        return True
    current = _full_text(entry)
    if current:
        _select_range(entry, len(current))
        _call_first(eti, ("delete_text",), 0, len(current), default=False)
        if _full_text(entry) not in ("", None):
            return False
    _call_first(eti, ("set_text_contents",), value, default=False)
    if _confirm_text(entry, value):
        return True
    if _full_text(entry) not in ("", None):
        return False
    length = None
    for name in ("insert_text", "insertText"):
        method = getattr(eti, name, None)
        if method is not None:
            length = _insert_length(method, value, entry)
            break
    if length is None:
        return False
    _call_first(eti, ("insert_text", "insertText"), 0, value, length, default=False)
    return _confirm_text(entry, value)


def _do_action_named(acc, names: frozenset[str]) -> bool:
    action = _action_iface(acc)
    if action is None:
        return False
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    for index in range(int(count)):
        raw = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw in names and _call_first(action, ("do_action", "doAction"), index, default=False):
            return True
    return False


def _chromium_control(acc) -> bool:
    """True when this node belongs to Chromium. A fake without a toolkit is not."""
    try:
        return _chromium_app(acc)
    except Exception:
        return False


def _popup_open(acc) -> bool:
    """Whether the choice popup is up.

    Chrome's select sets EXPANDED, sometimes a beat after the call that
    opened it. A child menu that is SHOWING or EXPANDED counts too, so a
    popup that does not flip the combo's own state is still closed.
    """
    if _state_has(acc, "EXPANDED"):
        return True
    # Qt keeps the combo's list SHOWING in the tree while the popup is closed.
    # EXPANDED on the combo is the popup. Treating that list as open would
    # send Escape on every set and then still see the list as showing.
    if _qt_app(acc):
        return False
    count = min(_child_count(acc), 12)
    for index in range(count):
        child = _child_at(acc, index)
        if child is None:
            continue
        if _role_name(child) not in {"menu", "popup menu", "list box", "list"}:
            continue
        if _state_has(child, "EXPANDED") or _state_has(child, "SHOWING") or _state_has(child, "VISIBLE"):
            return True
    return False


def _gecko_popup_open(acc) -> bool:
    """Whether a Firefox select popup is actually open.

    The option menu stays VISIBLE while the select is collapsed. Treating
    that as open would send Escape and undo a selection that already landed.
    EXPANDED on the combo, or SHOWING on the menu, is the open popup.
    """
    if _state_has(acc, "EXPANDED"):
        return True
    if _in_dialog(acc):
        # Inside a modal <dialog> or ARIA dialog Firefox 158 keeps the
        # collapsed select's option menu SHOWING. Only EXPANDED is the open
        # popup there; an Escape for a closed popup dismisses the dialog (#1150).
        return False
    count = min(_child_count(acc), 8)
    for index in range(count):
        child = _child_at(acc, index)
        if child is None:
            continue
        if _role_name(child) not in {"menu", "popup menu"}:
            continue
        if _state_has(child, "SHOWING") or _state_has(child, "EXPANDED"):
            return True
    return False


def _in_dialog(acc) -> bool:
    """Whether a dialog or alert encloses ``acc`` inside its window.

    Escape that reaches such a dialog closes it, so a popup close must not
    send one unless the popup itself is open and takes the key.
    """
    node = acc
    for _ in range(32):
        parent = _parent_of(node)
        if parent is None or parent is node:
            return False
        role = _role_name(parent)
        if role in {"dialog", "alert", "file chooser"}:
            return True
        if role in {"frame", "window", "application"}:
            return False
        node = parent
    return False


def _close_combo_popup(acc) -> None:
    """Close a popup this call opened. A combo that is not expanded is left alone.

    The close is repeated briefly. Chrome can mark the select EXPANDED after
    ``select_child`` returns, and one Escape is not always enough. Collapse
    is tried before Escape so a toolkit that has the action does not also
    receive a key. A collapsed Firefox select is left alone: its menu stays
    VISIBLE, and Escape would undo a selection that already landed.
    """
    gecko = _gecko_app(acc)
    if gecko and not _gecko_popup_open(acc):
        return
    is_open = _gecko_popup_open if gecko else _popup_open
    for attempt in range(4):
        if not is_open(acc):
            # Chrome can set EXPANDED after select_child has already returned.
            # One beat covers that. A GTK combo that is not expanded stays put.
            if attempt > 0 or not _chromium_control(acc):
                return
            time.sleep(0.05)
            continue
        _do_action_named(acc, frozenset({"collapse", "close", "hide"}))
        if is_open(acc) and _x11_keys_available():
            from a11y_computer_use.drivers import _linux_input

            _linux_input.press_chord("Escape")
        if not is_open(acc):
            return
        time.sleep(0.04)


def _chrome_select_owner(acc):
    """The Chromium ``<select>`` that owns this menu item, or None.

    The option is a menu item under that combo box. An application menu
    item, a Firefox select, and a GTK combo are not this control, so a
    later Escape cannot land on them.
    """
    if acc is None:
        return None
    if _role_name(acc) not in {"menu item", "check menu item", "radio menu item"}:
        return None
    if not _chromium_control(acc):
        return None
    node = acc
    for _ in range(8):
        parent = _parent_of(node)
        if parent is None or parent is node:
            return None
        role = _role_name(parent)
        if role == "combo box":
            return parent
        if role in {
            "application", "frame", "window", "dialog",
            "document web", "document frame", "menu bar",
        }:
            return None
        node = parent
    return None


def finish_chrome_select_option_click(acc) -> None:
    """Close the select popup after its option action succeeded.

    ``set_value`` closes with the combo's collapse action, then Escape
    while that popup is still open. The same close runs here, and only on
    the select that owns this option. A popup that is already collapsed is
    left alone, so Escape is not sent. A popup that stays expanded is
    ``popup_open``.
    """
    combo = _chrome_select_owner(acc)
    if combo is None:
        return
    _close_combo_popup(combo)
    if _state_has(combo, "EXPANDED"):
        label = _option_label(acc) or "the option"
        raise _text_mismatch(
            "popup_open",
            f"the combo popup is still open after clicking {label!r}",
            expected=label,
        )


def _gtk_combo_option_owner(acc):
    """The GTK combo that owns this menu item, or None.

    The option's parent menu belongs to the combo. An application menu
    item, a Chromium select, and a Firefox or Qt combo are not this
    control, so a later Escape cannot land on them.
    """
    if acc is None:
        return None
    if _role_name(acc) not in {"menu item", "check menu item", "radio menu item"}:
        return None
    try:
        if _chromium_control(acc) or _gecko_app(acc) or _qt_app(acc):
            return None
    except Exception:
        return None
    node = acc
    for _ in range(8):
        parent = _parent_of(node)
        if parent is None or parent is node:
            return None
        role = _role_name(parent)
        if role == "combo box":
            return parent if _gtk_combo(parent) else None
        if role in {
            "application", "frame", "dialog", "menu bar",
            "document web", "document frame",
        }:
            return None
        node = parent
    return None


def finish_gtk_combo_option_click(acc) -> None:
    """Close a GTK combo popup after its option action succeeded.

    The option's click usually closes the menu. A popup that stays open is
    closed the way ``set_value`` closes one. A popup that is still open
    after that is ``popup_open``. An application menu item has no combo
    owner, so Escape is not sent.
    """
    combo = _gtk_combo_option_owner(acc)
    if combo is None:
        return
    if not _popup_open(combo):
        return
    _close_combo_popup(combo)
    if _popup_open(combo):
        label = _option_label(acc) or "the option"
        raise _text_mismatch(
            "popup_open",
            f"the combo popup is still open after clicking {label!r}",
            expected=label,
        )


def _combo_active_label(acc) -> str | None:
    """The combo's active item, not a popup row that is only highlighted.

    GTK's combo Selection child is ``gtk_combo_box_get_active``. A menu's
    own Selection can mark a different row SELECTED without changing that.
    When the toolkit has no ``get_selected_child``, a SELECTED child of the
    combo itself is the active item. A SELECTED row nested in the popup is not.
    """
    iface = _selection_iface(acc)
    if iface is not None:
        child = _call_first(iface, ("get_selected_child", "getSelectedChild"), 0)
        if child is not None:
            label = _option_label(child)
            if label:
                return label
    for label, node, parent, _index in _collect_options(acc):
        if parent is acc and label and _state_has(node, "SELECTED"):
            return label
    shown = _full_text(acc)
    if isinstance(shown, str):
        cleaned = _strip_objects(shown)
        if cleaned:
            return cleaned
    return None


def _qt_combo_current(acc) -> str | None:
    """Qt's current combo item. On Linux that is the accessible name."""
    name = _node_name(acc)
    return name or None


def _qt_combo_shows(combo, label: str) -> bool:
    """Whether Qt's current item is ``label``. The name can trail the click."""
    for attempt in range(8):
        if _qt_combo_current(combo) == label:
            return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def _open_qt_popup(combo) -> None:
    """Show the Qt combo popup. A combo that is already expanded is left open."""
    if _state_has(combo, "EXPANDED"):
        return
    _do_action_named(combo, frozenset({"showmenu", "press", "show", "open"}))
    for _attempt in range(8):
        if _state_has(combo, "EXPANDED"):
            return
        time.sleep(0.05)


def _activate_qt_option(combo, label: str, node) -> None:
    """Choose ``label`` on a Qt combo through its list popup.

    The combo has no Selection interface. ``Toggle`` on a list item returns
    true and leaves the current item alone. Opening the popup and clicking
    the item's center is what changes ``currentText``. The popup closes
    itself when that click lands. The caller reads the name back.
    """
    if _qt_combo_current(combo) == label:
        return
    _open_qt_popup(combo)
    target = _option_named(combo, label) or node
    _click_center(target)
    _qt_combo_shows(combo, label)


def _combo_landed(acc, entry, value: str) -> bool:
    if entry is not None:
        text = _full_text(entry)
        return text is not None and text.replace(_OBJECT_REPLACEMENT, "") == value
    if _qt_app(acc):
        if _qt_combo_current(acc) == value:
            return True
        return _selected_option_text(acc) == value
    if _chromium_control(acc):
        # The same text the snapshot shows: the SELECTED option's name. Chrome
        # keeps the combobox name as the aria-label and its text as U+FFFC.
        # The selected menu item is the value, and it can arrive a beat late.
        return _choice_shows(acc, value)
    if _gecko_app(acc):
        # Firefox's selected option is a menu item. The combo has no Selection
        # child and its own text is empty. The selected item's name is the value.
        return _gecko_choice_shows(acc, value)
    return _combo_active_label(acc) == value


def _option_named(acc, label: str):
    """The option node named ``label`` after a popup has been opened, or None."""
    for item_label, node, _parent, _index in _collect_options(acc):
        if item_label == label:
            return node
    return None


def _click_center(node) -> None:
    """One left click at the node's screen center, when it has a box."""
    if not _x11_keys_available():
        return
    pos, size = _extents(node)
    if pos is None or size is None:
        return
    width, height = size
    if width <= 0 or height <= 0:
        return
    try:
        from a11y_computer_use.drivers import _linux_input

        _linux_input.click(int(pos[0] + width / 2), int(pos[1] + height / 2))
    except Exception:
        return


_WEB_OPTION_ACTIONS = frozenset({"select", "click", "press", "activate"})


def _choice_shows(combo, label: str, *, wait: bool = True) -> bool:
    """Whether the selected-option text is ``label``.

    Chrome's option ``select`` action updates the menu item's SELECTED state
    and the DOM together. A busy renderer can publish that state a beat
    after the action returns, so a check that follows an action waits. The
    wait is bounded and stops on the first match. A check before any action
    does not wait.
    """
    attempts = 6 if wait else 1
    for attempt in range(attempts):
        if _selected_option_text(combo) == label:
            return True
        if attempt + 1 < attempts:
            time.sleep(0.05)
    return False


def _menu_selection(combo):
    """Selection interface that can choose an option, and the node that owns it.

    A Chrome ``<select>`` combobox has no Selection interface. The child menu
    does, and ``select_child`` on that menu does not change the HTML value.
    The interface is still tried after the option action, for a toolkit that
    implements it. ``(None, None)`` when neither node has one.
    """
    iface = _selection_iface(combo)
    if iface is not None:
        return combo, iface
    count = min(_child_count(combo), 6)
    for index in range(count):
        child = _child_at(combo, index)
        if child is None:
            continue
        if _role_name(child) not in {"menu", "popup menu", "list box", "list"}:
            continue
        child_iface = _selection_iface(child)
        if child_iface is not None:
            return child, child_iface
    return None, None


def _activate_web_option(combo, label: str, node, index: int) -> None:
    """Select a Chrome ``<select>`` option and leave the popup closable.

    The option's own ``select`` action is what changes the HTML value. The
    combobox has no Selection interface, and ``select_child`` on its menu
    returns false and leaves the value alone. ``click`` / ``press`` /
    ``activate`` are accepted for a tree that names the action that way.
    An option that is already selected is not touched, so the popup is not
    opened for the current value. A click at the option's center is the last
    try, after the popup has been opened so the item has a box.
    """
    if _choice_shows(combo, label, wait=False):
        return
    target = _option_named(combo, label) or node
    _do_action_named(target, _WEB_OPTION_ACTIONS)
    if _choice_shows(combo, label):
        return
    _owner, iface = _menu_selection(combo)
    if iface is not None:
        _call_first(iface, ("select_child", "selectChild"), index, default=False)
    if _choice_shows(combo, label):
        return
    if not _popup_open(combo):
        _do_action_named(combo, frozenset({"press", "show", "open"}))
    target = _option_named(combo, label) or target
    _do_action_named(target, _WEB_OPTION_ACTIONS)
    if _choice_shows(combo, label):
        return
    _click_center(target)
    _choice_shows(combo, label)


def _gecko_selected(combo) -> str | None:
    """Firefox's selected option, after a cache drop when the listener is up."""
    _refresh_event_cache(combo)
    return _selected_option_text(combo)


def _gecko_choice_shows(combo, label: str, *, wait: bool = True) -> bool:
    """Whether Firefox's selected option text is ``label``.

    The selected menu item can update a beat after the key. The wait is
    bounded and stops on the first match. Each read drops the event cache
    first: ``event_main`` would otherwise keep the previous SELECTED state.
    """
    attempts = 6 if wait else 1
    for attempt in range(attempts):
        if _gecko_selected(combo) == label:
            return True
        if attempt + 1 < attempts:
            time.sleep(0.05)
    return False


def _activate_gecko_option(combo, label: str, node, options) -> None:
    """Choose ``label`` on a Firefox ``<select>``.

    The option's ``select`` action is tried first. On this toolkit that
    action does not change a collapsed select. Focus plus Up or Down then
    moves the selection by the option index, which is what a keyboard user
    does, and the selected option is read back. The popup is not opened:
    opening it and then sending Escape puts the previous value back.
    """
    if _gecko_selected(combo) == label:
        return
    target = _option_named(combo, label) or node
    _do_action_named(target, frozenset({"select", "click", "press", "activate"}))
    if _gecko_choice_shows(combo, label, wait=False):
        return
    if not _x11_keys_available():
        return
    grab_focus(combo)
    labels = [item[0] for item in options if item[0]]
    current = _gecko_selected(combo)
    if current in labels and label in labels and current != label:
        delta = labels.index(label) - labels.index(current)
        chord = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            _chord_into_target(combo, chord)
            if _gecko_selected(combo) == label:
                return
    if _gecko_selected(combo) == label:
        return
    _type_into_target(combo, label)


def _activate_combo_option(combo, options, match) -> None:
    """Choose ``match`` on the combo, not on its popup menu.

    ``Selection.select_child`` on a GTK combo is the model index and calls
    ``gtk_combo_box_set_active``. The same call on the popup only highlights
    the row. A click on the item is the fallback that activates it. Chrome's
    select uses its own path: the snapshot's selected-option text is the
    read-back, and the popup is not opened when that text is already the value.
    """
    label, node, _parent, index = match
    if _chromium_control(combo):
        model_index = next((i for i, item in enumerate(options) if item[1] is node), index)
        _activate_web_option(combo, label, node, model_index)
        return
    if _gecko_app(combo):
        _activate_gecko_option(combo, label, node, options)
        return
    if _qt_app(combo):
        _activate_qt_option(combo, label, node)
        return
    if _combo_active_label(combo) == label:
        return
    model_index = next(i for i, item in enumerate(options) if item[1] is node)
    iface = _selection_iface(combo)
    if iface is not None:
        _call_first(iface, ("select_child", "selectChild"), model_index, default=False)
    if _combo_active_label(combo) == label:
        return
    if not _state_has(combo, "EXPANDED"):
        _do_action_named(combo, frozenset({"press", "show", "open"}))
    _do_action_named(node, frozenset({"click", "press", "activate"}))


def _qt_quick_display(acc):
    """A Qt Quick combo's disabled display Text, never an editable entry."""
    if _role_name(acc) != "combo box" or not _qt_app(acc):
        return None
    for index in range(_child_count(acc)):
        child = _child_at(acc, index)
        if child is not None and _role_name(child) == "text" and not any(
            _state_has(child, state) for state in ("ENABLED", "SENSITIVE", "FOCUSABLE")
        ):
            return child
    return None


def _set_qt_quick_combo(acc, display, value: str, *, run=lambda fn: fn()) -> None:
    """Wait for native popup readiness, then select and verify the value.

    Driver callers marshal each probe separately so the event loop remains
    free to process popup creation between probes.
    """
    if run(lambda: _full_text(display)) == value:
        return
    frame = run(lambda: _frame_ancestor(acc))

    def menus():
        found = {}

        def walk(node, depth):
            if depth > 5:
                return
            _call_first(node, ("clear_cache", "clearCache"))
            for index in range(_child_count(node)):
                child = _child_at(node, index)
                if child is None:
                    continue
                role = _role_name(child)
                if role in {"menu", "popup menu", "list", "list box", "dialog"}:
                    # Qt Quick can expose its popup as a sibling dialog.
                    # Refresh that container too: clearing only its parent
                    # leaves cached SHOWING/children from before opening.
                    _call_first(child, ("clear_cache", "clearCache"))
                    if _state_has(child, "SHOWING"):
                        found[_ancestor_key(child)] = child
                elif node is acc or (node is not frame and role not in _TEXT_ROLE_NAMES):
                    walk(child, depth + 1)

        walk(acc, 0)
        combo_has_menu = bool(found)
        if frame is not None:
            walk(frame, 0)
        # Some bridges put the visible rows directly beneath the combo.
        if not combo_has_menu and _state_has(acc, "EXPANDED") and any(
            _state_has(node, "SHOWING") for _, node, *_ in _collect_options(acc)
        ):
            found[_ancestor_key(acc)] = acc
        return found

    before = set(run(menus))
    run(lambda: _click_center(acc))
    popup = None
    # Readiness has its own bounded deadline, rather than a fixed delay after
    # input. Stop immediately once a newly shown popup has published its rows.
    deadline = time.monotonic() + _MENU_DIALOG_WAIT_S
    while True:
        def probe():
            opened = [menu for key, menu in menus().items() if key not in before]
            if len(opened) != 1:
                return opened, []
            return opened, _collect_options(opened[0])

        opened, options = run(probe)
        if len(opened) > 1:
            break
        if len(opened) == 1 and options:
            popup = opened[0]
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    if popup is None:
        raise _text_mismatch("popup_missing", "no unambiguous Qt combo popup appeared", expected=value)
    try:
        matches = [node for label, node, *_rest in options if label == value]
        if not matches:
            labels = [label for label, *_rest in options]
            listed = ", ".join(labels[:64])[:2048]
            if len(labels) > 64 or len(", ".join(labels[:64])) > 2048:
                listed += " (list truncated)"
            raise ValueError(f"value {value!r} is not one of: {listed}")
        if len(matches) != 1:
            raise _text_mismatch("ambiguous_option", "multiple Qt combo options match", expected=value)
        run(lambda: _click_center(matches[0]))

        def committed():
            _refresh_event_cache(display)
            _refresh_event_cache(popup)
            closed = not _state_has(popup, "EXPANDED" if popup is acc else "SHOWING")
            return _full_text(display) == value and closed

        for attempt in range(8):
            if run(committed):
                return
            if attempt < 7:
                time.sleep(0.05)
        raise _text_mismatch("text_mismatch", f"the combo selection did not commit {value!r}", expected=value)
    finally:
        def still_open():
            _refresh_event_cache(popup)
            return _state_has(popup, "EXPANDED" if popup is acc else "SHOWING")

        if run(still_open) and _x11_keys_available():
            from a11y_computer_use.drivers import _linux_input

            _linux_input.press_chord("Escape")


def set_combo_value(acc, value: str) -> None:
    """Choose ``value`` on this combo or list, or write its own entry.

    An editable combo is written with ``set_text_contents`` on its own entry.
    That sends no keystrokes, so a different focused field is left untouched.
    A non-editable combo is set through its own Selection. An unknown option
    raises ValueError before any selection, and the message lists the options.
    A popup this call opened is closed. The call raises when the read-back is
    not ``value``. A highlighted popup row is not a successful read-back.
    """
    display = _qt_quick_display(acc)
    if display is not None and _combo_entry(acc) is None:
        _set_qt_quick_combo(acc, display, value)
        return
    if _gecko_app(acc):
        _refresh_event_cache(acc)
    entry = _combo_entry(acc)
    options = _collect_options(acc)
    if entry is None and not options:
        _do_action_named(acc, frozenset({"press", "show", "open"}))
        options = _collect_options(acc)
        entry = _combo_entry(acc)
    try:
        if entry is not None:
            if not _set_entry_contents(entry, value):
                raise _text_mismatch(
                    "text_mismatch",
                    f"the combo entry read back does not match {value!r}",
                    expected=value,
                )
        else:
            match = next((item for item in options if item[0] == value), None)
            if match is None:
                labels = [label for label, *_rest in options if label]
                # Bound the diagnostic, independently of the choices searched.
                shown: list[str] = []
                size = 0
                for label in labels[:64]:
                    added = len(label) + (2 if shown else 0)
                    if size + added > 2048:
                        break
                    shown.append(label)
                    size += added
                listed = ", ".join(shown)
                if len(shown) < len(labels):
                    raise ValueError(
                        f"value {value!r} is not an available option; "
                        f"showing first {len(shown)} of {len(labels)} options "
                        f"(list truncated): {listed}"
                    )
                raise ValueError(f"value {value!r} is not one of: {listed}")
            _activate_combo_option(acc, options, match)
    finally:
        _close_combo_popup(acc)
    if _state_has(acc, "EXPANDED"):
        raise _text_mismatch(
            "popup_open",
            "the combo popup is still open",
            expected=value,
        )
    if not _combo_landed(acc, entry, value):
        raise _text_mismatch(
            "text_mismatch",
            f"the value read back does not match {value!r}",
            expected=value,
        )


def _minimum_increment(acc) -> float | None:
    """The Value interface's step, or None when it is missing or not positive."""
    Atspi = _atspi()
    got = _safe(lambda: Atspi.Value.get_minimum_increment(acc))
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return None
    step = float(got)
    if not math.isfinite(step) or step <= 0:
        return None
    return step


def _snap_spin(acc, number: float, low: float) -> float:
    """Round a GTK spin button onto its step.

    Setting 4.6 on an integer spin leaves the adjustment at 4.6 while the
    text shows 5. The value written is the nearest step, so the adjustment
    and the text agree. A Chrome number input is not a spin adjustment and
    keeps the number it was given. A spin whose step is missing and whose
    text is an integer uses a step of 1.
    """
    if _role_name(acc) != "spin button" or _number_input(acc):
        return number
    step = _minimum_increment(acc)
    if step is None:
        shown = _full_text(acc)
        if not isinstance(shown, str) or not shown.strip().lstrip("+-").isdigit():
            return number
        step = 1.0
    snapped = low + round((number - low) / step) * step
    return float(format(snapped, ".10g"))


def _number_text(acc) -> str | None:
    """The number field's text, stripped. None when the read failed.

    An empty Chrome number input is ``""``. Its Value interface still reports
    the minimum (0.0 when min is 0). That 0.0 is not a value the field holds.
    """
    raw = _full_text(acc)
    if raw is None:
        return None
    return raw.strip()


def _number_field_is_empty(acc) -> bool:
    """True for an ``<input type=number>`` whose text is empty or unreadable."""
    if not _number_input(acc):
        return False
    return _number_text(acc) in ("", None)


def _number_text_matches(acc, value: str) -> bool:
    """True when the field's text parses as ``value`` and is not empty.

    An empty field does not match ``0``, even when the Value interface reads
    0.0. The poll is bounded and stops on the first match.
    """
    wanted = _parse_number(value)
    for attempt in range(8):
        shown = _number_text(acc)
        if shown:
            try:
                if _numbers_match(_parse_number(shown), wanted):
                    return True
            except ValueError:
                if shown == str(value).strip():
                    return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def _fill_empty_number(acc, value: str) -> bool:
    """Type ``value`` into an empty Chrome number input.

    ``Value.set_current_value`` does not change an empty input that has a
    minimum and a maximum: the DOM stays ``""`` and the Value interface stays
    at the minimum. A click that focuses the field, then the digits, is what
    fills it. Success is the text, not the Value interface.
    """
    grab_focus(acc)
    _click_center(acc)
    if not _x11_keys_available():
        return False
    _type_into_target(acc, value)
    return _number_text_matches(acc, value)


# Chrome publishes a cleared number input after the key events, and a click
# that just selected a list row can still own the caret when the clear starts.
# Four 20ms reads (the generic text confirm) return before that DOM update.
# These tries click the field and poll the text. A field that never empties
# is still a failure.
_NUMBER_CLEAR_TRIES = 3
_NUMBER_CLEAR_WAIT_S = 0.8


def _clear_number_input(acc) -> bool:
    """Make an ``<input type=number>`` empty. True only when its text is ``""``.

    A toolkit with EditableText is cleared through ``set_text``. Chrome's
    spin button has no EditableText (probed on Chrome 154: role ``spin
    button``, ``text-input-type=number``, no editable interface, text ``"3"``).
    ``set_text`` then focuses and sends ctrl+a, BackSpace, and gives up after
    about 80ms. Those keys miss the field when a list row still has the
    caret, and a busy renderer can publish the empty value after that window.
    A click at the field's center places the caret. ctrl+a, BackSpace, and
    Delete are sent, and the text is polled until it is empty. A failed read
    is not an empty field.
    """
    if _editable_iface(acc) is not None and set_text(acc, ""):
        if _number_text(acc) == "":
            return True
    if not _x11_keys_available():
        return False

    for _attempt in range(_NUMBER_CLEAR_TRIES):
        grab_focus(acc)
        _click_center(acc)
        _chord_into_target(acc, "ctrl+a")
        _chord_into_target(acc, "backspace")
        _chord_into_target(acc, "delete")
        deadline = time.monotonic() + _NUMBER_CLEAR_WAIT_S
        while True:
            if _number_text(acc) == "":
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return False


_TRACKED_FIELD_ROLES = frozenset({
    "entry", "password text", "combo box", "spin button",
})


def _is_tracked_field(acc) -> bool:
    """An editable control whose text a leaked keystroke would change."""
    role = _role_name(acc)
    if role in _TRACKED_FIELD_ROLES:
        return True
    if role in {"text", "section", "paragraph"} and (
        _state_has(acc, "EDITABLE") or _state_has(acc, "FOCUSABLE")
    ):
        return True
    if _number_input(acc):
        return True
    tag = str(_get_attributes(acc).get("tag") or "").lower()
    return tag in {"input", "textarea"}


def _under_target(node, target) -> bool:
    """True when ``node`` is ``target`` or sits inside it."""
    current = node
    target_sid = _stable_id(target) or ""
    target_role = _role_name(target)
    for _ in range(10):
        if current is None:
            return False
        if current is target:
            return True
        sid = _stable_id(current) or ""
        if target_sid and sid == target_sid and _role_name(current) == target_role:
            return True
        current = _parent_of(current)
    return False


def _field_walk_root(acc):
    """The document or frame that holds ``acc`` and its sibling fields."""
    node = acc
    document = None
    frame = acc
    for _ in range(24):
        parent = _parent_of(node)
        if parent is None:
            break
        role = _role_name(parent)
        if role == "application":
            break
        if role in {"document web", "document frame"}:
            document = parent
        if role in {"frame", "window", "dialog"}:
            frame = parent
        node = parent
    return document or frame


def _collect_peer_fields(acc) -> dict[str, str]:
    """Text of editable fields in the same document, excluding ``acc``.

    The walk is bounded. Keys are role, name, and id in tree order so a
    later read of the same tree lines up. The target and its descendants
    are not peers: a partial write there is not another field changing.
    """
    root = _field_walk_root(acc)
    counts: dict[str, int] = {}
    found: dict[str, str] = {}
    queue = [root]
    seen: set[int] = set()
    examined = 0
    while queue and examined < 250:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        examined += 1
        if not _under_target(node, acc) and _is_tracked_field(node):
            base = f"{_role_name(node)}|{_node_name(node)}|{_stable_id(node) or ''}"
            index = counts.get(base, 0)
            counts[base] = index + 1
            text = _full_text(node)
            if text is not None:
                found[f"{base}#{index}"] = text
        count = _child_count(node)
        if count <= 0 or count > 5000:
            continue
        for index in range(min(count, 40)):
            child = _child_at(node, index)
            if child is not None:
                queue.append(child)
    return found


def peer_field_texts(acc) -> dict[str, str]:
    """Peer field text before a set_value. Empty when the tree cannot be read."""
    try:
        return _collect_peer_fields(acc)
    except Exception:
        return {}


def changed_peer_fields(acc, before: dict) -> list[str]:
    """Peers whose text differs from ``before``. A failed reread is not a leak."""
    if not before:
        return []
    try:
        after = _collect_peer_fields(acc)
    except Exception:
        return []
    if not after:
        return []
    changed: list[str] = []
    for key, old in before.items():
        if key not in after or after[key] == old:
            continue
        new = after[key]
        changed.append(f"{key}: {old!r} -> {new!r}"[:180])
    return changed


# ``88%`` or ``50 %``. A bare ``50`` is not a percent literal. A narrow
# no-break space is the same gap as a normal space.
_PERCENT_LITERAL = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*%$"
)
# Inkscape draws an integer percent. The log2 of that percent can be a
# fraction of a percent away from the rounded text.
_PERCENT_DISPLAY_TOLERANCE = 0.51


def _percent_literal(value: str) -> float | None:
    """The number in ``50%`` or ``50 %``. None when the text has no percent sign."""
    text = str(value).replace("\u00a0", " ").replace("\u202f", " ").strip()
    match = _PERCENT_LITERAL.match(text)
    if match is None:
        return None
    try:
        number = float(match.group("number"))
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


def _finite_current_value(acc) -> float | None:
    """The Value interface's current value, or None when it is not a finite number."""
    got = _safe(lambda: _atspi().Value.get_current_value(acc))
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return None
    number = float(got)
    if not math.isfinite(number):
        return None
    return number


def _pow2_percent(log_value: float) -> float | None:
    """``100 * 2**log_value``, or None when that power is not finite."""
    try:
        percent = math.pow(2.0, float(log_value)) * 100.0
    except OverflowError:
        return None
    if not math.isfinite(percent):
        return None
    return percent


def _percent_value_unit(acc) -> tuple[str, float] | None:
    """How a percent label relates to the current Value, plus that value.

    ``log2`` means the text is the percent and the Value is ``log2`` of
    that percent as a fraction (Inkscape's zoom spin: ``88%`` is about
    ``-0.184``). ``percent`` means the Value is already that percent.
    ``unmatched`` means the text is a percent and neither relationship
    holds, so a percent literal must not be converted by guesswork.
    None means the text is not a percent, or the current value could not
    be read.
    """
    shown = _number_text(acc)
    if not shown:
        return None
    percent = _percent_literal(shown)
    if percent is None:
        return None
    current = _finite_current_value(acc)
    if current is None:
        return None
    displayed = _pow2_percent(current)
    if displayed is not None and abs(displayed - percent) <= _PERCENT_DISPLAY_TOLERANCE:
        return "log2", current
    if abs(percent - current) <= _PERCENT_DISPLAY_TOLERANCE:
        return "percent", current
    return "unmatched", current


def _clamp_near_bounds(number: float, low: float, high: float, *, open_upper: bool) -> float:
    """Pull a conversion that landed an ulp outside the published bounds back in."""
    if number < low and low - number <= 1e-6:
        return low
    if not open_upper and number > high and number - high <= 1e-6:
        return high
    return number


def _log_percent_range_message(value: str, low: float, high: float, *, open_upper: bool) -> str:
    """The percent a log2 control can show, and the Value scale underneath it."""
    low_pct = _pow2_percent(low)
    low_text = "?" if low_pct is None else f"{_format_bound(low_pct)}%"
    if open_upper:
        return (
            f"value {value!r} is below {low_text} "
            f"(value scale minimum {_format_bound(low)})"
        )
    high_pct = _pow2_percent(high)
    high_text = "?" if high_pct is None else f"{_format_bound(high_pct)}%"
    return (
        f"value {value!r} is outside {low_text}..{high_text} "
        f"(value scale {_format_bound(low)}..{_format_bound(high)})"
    )


def _unmatched_percent_message(
    acc, value: str, low: float, high: float, *, open_upper: bool,
) -> str:
    """A percent was asked of a control whose text and Value do not agree."""
    shown = _number_text(acc) or ""
    current = _finite_current_value(acc)
    current_text = "unreadable" if current is None else _format_bound(current)
    if open_upper:
        span = f"minimum {_format_bound(low)}"
    else:
        span = f"{_format_bound(low)}..{_format_bound(high)}"
    return (
        f"value {value!r} is a percent; the control shows {shown.strip()!r} "
        f"which does not match its value {current_text}; valid range is {span}"
    )


def _format_held_percent(percent: float) -> str:
    """The percent a log2 control holds after the toolkit rounds the write."""
    if abs(percent - round(percent)) <= 0.05:
        return f"{int(round(percent))}%"
    return f"{_format_bound(percent)}%"


def _write_log_percent(acc, value: str, written: float, before: float) -> bool | str:
    """Write a log2 zoom and read it back. No step snap and no keystrokes.

    An exact landing returns True, so the caller reports the requested
    percent. A toolkit that moved the value by at most one step returns
    the percent that landing represents. A value that did not move is a
    mismatch, including when the old value was already within one step.
    """
    Atspi = _atspi()
    _safe(lambda: Atspi.Value.set_current_value(acc, written), False)
    got = None
    for attempt in range(_TEXT_CONFIRM_POLLS):
        got = _safe(lambda: Atspi.Value.get_current_value(acc))
        if (
            isinstance(got, (int, float))
            and not isinstance(got, bool)
            and _numbers_match(float(got), written)
        ):
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    if isinstance(got, (int, float)) and not isinstance(got, bool):
        step = _minimum_increment(acc)
        moved = not _numbers_match(float(got), before)
        if step is not None and moved and abs(float(got) - written) <= step:
            held = _pow2_percent(float(got))
            if held is not None:
                return _format_held_percent(held)
    raise _text_mismatch(
        "text_mismatch",
        f"the value read back {got!r} does not match {value!r}",
        expected=value,
        actual=got,
    )


def _apply_log_percent(acc, value: str, low: float, high: float, before: float, *, open_upper: bool):
    """Convert a percent onto a log2 Value. None lets the bare number through.

    A percent literal (``50%``, ``50 %``) is ``log2(percent / 100)``. A bare
    number inside the Value range is not a percent: the caller writes it
    unchanged, including the spin step. A bare number outside that range is
    a percent when the conversion fits, and the same percent bounds when it
    does not. The conversion is not snapped to the step. Snapping ``50%``
    onto Inkscape's tenth would land near ``48.5%``.
    """
    literal = _percent_literal(value)
    if literal is None:
        try:
            number = _parse_number(value)
        except ValueError:
            return None
        if not _outside_range(number, low, high, open_upper=open_upper):
            return None
        percent = number
    else:
        percent = literal
    if not math.isfinite(percent) or percent <= 0:
        raise ValueError(_log_percent_range_message(value, low, high, open_upper=open_upper))
    try:
        written = math.log(percent / 100.0) / math.log(2.0)
    except ValueError:
        raise ValueError(_log_percent_range_message(value, low, high, open_upper=open_upper)) from None
    written = _clamp_near_bounds(written, low, high, open_upper=open_upper)
    if _outside_range(written, low, high, open_upper=open_upper):
        raise ValueError(_log_percent_range_message(value, low, high, open_upper=open_upper))
    return _write_log_percent(acc, value, written, before)


# ToolZoomSlider::scaleFunc/scaleFuncInv in Xournal++ 1.2.6:
# https://github.com/xournalpp/xournalpp/blob/v1.2.6/src/core/gui/toolbarMenubar/ToolZoomSlider.cpp
_XOURNAL_ZOOM_OFFSET = 0.20753


def _apply_xournal_percent(acc, value: str, low: float, high: float) -> bool | None:
    """Convert Xournal++ zoom and verify both Value and percent label.

    Xournal++ truncates the displayed percent and gates document updates on
    slider interaction. A raw-only change must not be reported as success.
    None preserves other controls and in-range raw writes.
    """
    if _role_name(acc) != "slider":
        return None
    app = _application_of(acc)
    if app is None or _node_name(app).casefold() not in {"xournalpp", "xournal++"}:
        return None
    shown = _percent_literal(_node_name(acc))
    before = _finite_current_value(acc)
    if shown is None or before is None:
        return None
    try:
        displayed = 100 * (math.exp(before) + _XOURNAL_ZOOM_OFFSET)
    except OverflowError:
        return None
    if not shown <= displayed < shown + 1:
        return None
    percent = _percent_literal(value)
    if percent is None:
        try:
            number = _parse_number(value)
        except ValueError:
            return None
        if low <= number <= high:
            return None
        percent = number
    try:
        minimum = 100 * (math.exp(low) + _XOURNAL_ZOOM_OFFSET)
        maximum = 100 * (math.exp(high) + _XOURNAL_ZOOM_OFFSET)
    except OverflowError:
        return None
    if (percent <= 100 * _XOURNAL_ZOOM_OFFSET
            or not minimum - 1e-6 <= percent <= maximum + 1e-6):
        raise ValueError(
            f"value {value!r} is outside {_format_bound(minimum)}%..{_format_bound(maximum)}% "
            f"(value scale {_format_bound(low)}..{_format_bound(high)})"
        )
    written = _clamp_near_bounds(
        math.log(percent / 100 - _XOURNAL_ZOOM_OFFSET), low, high, open_upper=False,
    )
    Atspi = _atspi()
    _safe(lambda: Atspi.Value.set_current_value(acc, written), False)
    got = label = None
    for attempt in range(_TEXT_CONFIRM_POLLS):
        got = _finite_current_value(acc)
        label = _percent_literal(_node_name(acc))
        if (got is not None and _numbers_match(got, written)
                and label is not None and abs(label - percent) < 1):
            return True
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    raise _text_mismatch(
        "text_mismatch", "Xournal++ zoom did not confirm the requested percent",
        expected=value, actual={"value": got, "percent": label},
    )


def set_numeric_value(acc, value: str) -> bool | str:
    """Set the Value interface's current value.

    Qt Quick sliders/spins reporting 0..0 have unknown bounds: write the
    requested number without step guesses and require matching read-back.
    A non-number or a number outside minimum..maximum raises ValueError before
    the write. The message includes the minimum and maximum. The current value
    is read back and must match. False means this control has no Value
    interface, or the bounds are an uninitialized double. The text is not
    parsed in that case, so a word is not rejected as a number and the
    garbage minimum is not reported. The caller may use ``set_text``.
    An empty string on an ``<input type=number>`` clears the field. When the
    control has EditableText, ``set_text`` clears it. Chrome's number input
    has no EditableText: a click places the caret, then ctrl+a, BackSpace,
    and Delete are sent, and the text is polled until it is empty. A GTK spin
    button is snapped to its step; the return value is that step's text when
    it differs from what was asked, so the caller reports the value held.
    An empty Chrome number input is filled by typing. A read-back of 0.0 from
    the Value interface is not success while the text is still empty.
    Xournal++ zoom uses its offset natural-log scale for percent input;
    both the raw Value and displayed percent must confirm the write.
    A control whose text is a percent and whose Value is ``log2`` of that
    percent (Inkscape's status zoom) accepts ``50%`` and a bare number that
    falls outside the Value range as that percent, converted with
    ``log2(percent / 100)`` and not snapped to the step. A bare number inside
    the Value range is still that scale. A percent the control cannot
    represent names the percent range and the Value scale. When the text is
    a percent but it matches neither the Value nor ``log2`` of the Value, a
    percent literal is refused and the Value range is named. No keystrokes
    are sent for any of these.
    """
    _refuse_gecko_date_segment_replace(acc)
    if value == "" and _number_input(acc):
        if _clear_number_input(acc):
            return True
        raise _text_mismatch(
            "text_mismatch",
            "the number field could not be cleared",
            expected="",
        )
    # A missing interface returns before any number check. Reading minimum
    # on that node is the uninitialized double, and parsing the text would
    # report that double as the minimum.
    if not _value_interface_present(acc):
        return False
    span = _raw_value_bounds(acc)
    if span is not None and _uninitialized_bounds(*span):
        return False
    low = high = None
    open_upper = False
    unknown_range = _qt_unknown_value_range(acc, span)
    if span is not None and not unknown_range:
        low, high = span
        # Chrome reports maximum 0 on <input type=number min=0> with no max.
        # A maximum that is not above the minimum is not a bound.
        open_upper = high <= low
        if not open_upper:
            handled = _apply_xournal_percent(acc, value, low, high)
            if handled is not None:
                return handled
        # Some sliders (Xournal++ zoom) expose the percent only as their
        # name. That label does not establish a conversion for the raw scale.
        # Explain the mismatch before parsing, but preserve in-range raw input.
        if _role_name(acc) == "slider":
            shown = _node_name(acc)
            percent = _percent_literal(shown)
            if percent is not None and _outside_range(percent, low, high, open_upper=open_upper):
                literal = _percent_literal(value)
                try:
                    requested = _parse_number(value) if literal is None else literal
                except ValueError:
                    requested = None
                if literal is not None or (
                    requested is not None
                    and _outside_range(requested, low, high, open_upper=open_upper)
                ):
                    span_text = (
                        f"minimum {_format_bound(low)}" if open_upper
                        else f"{_format_bound(low)}..{_format_bound(high)}"
                    )
                    raise ValueError(
                        f"value {value!r} cannot be interpreted as a percent; "
                        f"the slider shows {shown!r} but uses a non-percent scale "
                        f"with raw range {span_text}. The percent conversion is unknown; "
                        "supply a raw value within that range"
                    )
        unit = _percent_value_unit(acc)
        if unit is not None:
            kind, before = unit
            if kind == "log2":
                # None means the text is an in-range bare number. That write
                # stays on the Value scale, step snap included.
                handled = _apply_log_percent(
                    acc, value, low, high, before, open_upper=open_upper,
                )
                if handled is not None:
                    return handled
            elif kind == "percent":
                literal = _percent_literal(value)
                if literal is not None:
                    value = _format_bound(literal)
            elif kind == "unmatched" and _percent_literal(value) is not None:
                raise ValueError(
                    _unmatched_percent_message(
                        acc, value, low, high, open_upper=open_upper,
                    )
                )
    try:
        number = _parse_number(value)
    except ValueError:
        if low is None or high is None:
            raise ValueError(f"value {value!r} is not a number") from None
        if open_upper:
            raise ValueError(
                f"value {value!r} is not a number; minimum is {_format_bound(low)}"
            ) from None
        raise ValueError(
            f"value {value!r} is not a number; valid range is {_format_bound(low)}..{_format_bound(high)}"
        ) from None
    if span is None:
        return False
    if not unknown_range:
        if _outside_range(number, low, high, open_upper=open_upper):
            raise ValueError(_range_message(value, low, high, open_upper=open_upper))
        number = _snap_spin(acc, number, low)
        if _outside_range(number, low, high, open_upper=open_upper):
            raise ValueError(_range_message(value, low, high, open_upper=open_upper))
    if _number_field_is_empty(acc):
        if _fill_empty_number(acc, value):
            return True
        shown = _number_text(acc)
        raise _text_mismatch(
            "text_mismatch",
            f"the value read back {shown!r} does not match {value!r}",
            expected=value,
            actual=shown if shown is not None else "",
        )
    Atspi = _atspi()
    _safe(lambda: Atspi.Value.set_current_value(acc, number), False)
    got = None
    for attempt in range(_TEXT_CONFIRM_POLLS):
        got = _safe(lambda: Atspi.Value.get_current_value(acc))
        if isinstance(got, (int, float)) and not isinstance(got, bool) and _numbers_match(float(got), number):
            # An empty number field reports the minimum. That is not the text.
            if _number_input(acc) and _number_text(acc) in ("", None):
                break
            if _numbers_match(float(got), _parse_number(value)):
                return True
            return _format_bound(float(got))
        if attempt + 1 < _TEXT_CONFIRM_POLLS:
            time.sleep(_TEXT_CONFIRM_PAUSE_S)
    raise _text_mismatch(
        "text_mismatch",
        f"the value read back {got!r} does not match {value!r}",
        expected=value,
        actual="" if _number_input(acc) and _number_text(acc) in ("", None) else got,
    )


def _selection_iface(acc):
    return _call_first(acc, ("get_selection_iface", "get_selection"))


def _child_index(parent, child) -> int | None:
    child_count = _child_count(parent)
    index = _call_first(child, ("get_index_in_parent", "getIndexInParent"))
    if type(index) is int and 0 <= index < child_count:
        kid = _child_at(parent, index)
        if kid is child or kid == child:
            return index
    count = min(child_count, 500)
    for index in range(count):
        kid = _child_at(parent, index)
        if kid is None:
            continue
        if kid is child or kid == child:
            return index
    return None


def _node_contains(node, target, depth: int = 0) -> bool:
    if depth > 6:
        return False
    count = min(_child_count(node), 64)
    for index in range(count):
        child = _child_at(node, index)
        if child is None:
            continue
        if child is target or child == target:
            return True
        if _node_contains(child, target, depth + 1):
            return True
    return False


def _index_of_containing_child(parent, target) -> int | None:
    """Index of the direct child of ``parent`` that is or contains ``target``."""
    count = min(_child_count(parent), 500)
    for index in range(count):
        kid = _child_at(parent, index)
        if kid is None:
            continue
        if kid is target or kid == target or _node_contains(kid, target):
            return index
    return None


def _selection_parent(acc):
    """(parent, child index, row node) when ``acc`` sits in a Selection container.

    A scroll pane or filler between the row and the list still counts. The
    index is that row's index under the selection parent. A menu is not a
    selection target: menu items are activated, not selected as list rows.
    """
    origin = _role_name(acc)
    if origin not in _ROW_ROLE_NAMES and origin not in _OPTION_ROLE_NAMES:
        return None, None, None
    node = acc
    for _ in range(8):
        parent = _parent_of(node)
        if parent is None:
            return None, None, None
        parent_role = _role_name(parent)
        if parent_role in {"menu", "popup menu", "menu bar"}:
            return None, None, None
        if _selection_iface(parent) is not None and parent_role in (
            _LIST_ROLES | {"tree", "tree table", "table"}
        ):
            # node is the direct child reached through native parent links;
            # acc may be several cells below it in a GTK TreeView.
            index = _child_index(parent, node)
            if index is None:
                index = _index_of_containing_child(parent, acc)
            return parent, index, acc
        node = parent
    return None, None, None


def _first_press_action(acc) -> str:
    action = _action_iface(acc)
    if action is None:
        return ""
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    for index in range(int(count)):
        raw = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw in _PRESS_ACTION_NAMES or raw in _PICK_ACTION_NAMES:
            return raw
    return ""


def _row_selected(node) -> bool:
    return _state_has(node, "SELECTED")


def _xml_roles(acc) -> set[str]:
    """``xml-roles`` tokens. Empty when the node has no attributes."""
    try:
        attrs = _get_attributes(acc)
    except Exception:
        return set()
    return {
        part.strip().lower()
        for part in str(attrs.get("xml-roles") or "").split()
        if part.strip()
    }


def _choice_text(value: str | None) -> str:
    """Option text with NBSP folded to space and the edges stripped."""
    if value is None:
        return ""
    return (_norm_nbsp(str(value)) or "").strip()


def _is_combobox_node(acc) -> bool:
    """True for a combo box, including an input whose ARIA role is combobox."""
    if acc is None:
        return False
    try:
        if accessible_gone(acc):
            return False
    except Exception:
        return False
    if _role_name(acc) in {"combo box", "combo-box"}:
        return True
    return "combobox" in _xml_roles(acc)


def _listbox_option(acc) -> bool:
    """True for an option in a list or list box, not a menu item.

    A GTK tree cell is not one. A menu item inside a combo popup is not one
    either: that click is an activation, and this check is only for the row
    selection read-back.
    """
    role = _role_name(acc)
    xml = _xml_roles(acc)
    if role not in {"list item", "list box item"} and "option" not in xml:
        return False
    parent, _index, _target = _selection_parent(acc)
    if parent is not None:
        parent_role = _role_name(parent)
        return parent_role in {"list", "list box"} or "listbox" in _xml_roles(parent)
    if role in {"menu item", "check menu item", "radio menu item"}:
        return False
    node = acc
    for _ in range(6):
        node = _parent_of(node)
        if node is None:
            return False
        parent_role = _role_name(node)
        if parent_role in {"menu", "popup menu", "menu bar"}:
            return False
        if parent_role in {"list", "list box"} or "listbox" in _xml_roles(node):
            return True
    return False


def _relation_combobox(acc):
    """A combobox related to ``acc``, or None.

    ``aria-controls`` is CONTROLLED_BY on the listbox and CONTROLLER_FOR on
    the combobox. ``aria-owns`` is the node-parent pair. Either direction
    counts.
    """
    if acc is None:
        return None
    for kind in (
        "controlled_by", "controller_for", "node_child_of", "node_parent_of",
        "embedded_by", "embeds",
    ):
        try:
            targets = _relation_targets(acc, kind)
        except Exception:
            continue
        for target in targets:
            if _is_combobox_node(target):
                return target
    return None


def _combobox_under(node, depth: int):
    """A combobox at ``node`` or within ``depth`` levels of children."""
    if node is None or depth < 0:
        return None
    if _is_combobox_node(node):
        return node
    if depth == 0:
        return None
    count = min(_child_count(node), 8)
    for index in range(count):
        found = _combobox_under(_child_at(node, index), depth - 1)
        if found is not None:
            return found
    return None


def _nearby_combobox(listbox):
    """A combobox that shares ``listbox``'s parent, preferring an open one.

    The input and the list are often siblings, or the input sits one or two
    groups under that parent (a label around the input). Several comboboxes
    on the page resolve to the expanded one.
    """
    if listbox is None:
        return None
    parent = _parent_of(listbox)
    if parent is None:
        return None
    linked = _relation_combobox(parent)
    if linked is not None:
        return linked
    candidates = []
    count = min(_child_count(parent), 24)
    for index in range(count):
        child = _child_at(parent, index)
        if child is None:
            continue
        found = _combobox_under(child, 3)
        if found is not None and all(found is not item for item in candidates):
            candidates.append(found)
    expanded = [item for item in candidates if _popup_open(item)]
    if len(expanded) == 1:
        return expanded[0]
    if len(candidates) == 1:
        return candidates[0]
    if expanded:
        return expanded[0]
    return None


def _focused_combobox(acc):
    """The focused combobox in ``acc``'s application, when Collection can say.

    A listbox option click often leaves focus on the input. None when the
    application has no Collection interface, which is also the hermetic fake.
    """
    app = _call_first(acc, ("get_application", "getApplication"))
    if app is None:
        return None
    try:
        Atspi = _atspi()
        focused_state = getattr(getattr(Atspi, "StateType", None), "FOCUSED", None)
    except Exception:
        return None
    if focused_state is None:
        return None
    try:
        found, node = _focused_via_collection(app, Atspi, focused_state)
    except Exception:
        return None
    if not found or node is None:
        return None
    if _is_combobox_node(node):
        return node
    current = node
    for _ in range(6):
        parent = _parent_of(current)
        if parent is None or parent is current:
            break
        if _is_combobox_node(parent):
            return parent
        current = parent
    return None


def _combobox_owner(acc):
    """The combobox this option belongs to, or None.

    An ancestor combo, a controls/owns relation, a sibling input, then the
    focused combobox. A plain listbox has none of these.
    """
    node = acc
    seen: set[int] = set()
    listbox = None
    for _ in range(8):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        if node is not acc and _is_combobox_node(node):
            return node
        linked = _relation_combobox(node)
        if linked is not None:
            return linked
        role = _role_name(node)
        if (
            listbox is None
            and node is not acc
            and (role in {"list", "list box"} or "listbox" in _xml_roles(node))
        ):
            listbox = node
        parent = _parent_of(node)
        if parent is None or parent is node:
            break
        node = parent
    nearby = _nearby_combobox(listbox)
    if nearby is not None:
        return nearby
    return _focused_combobox(acc)


def _combobox_text(acc) -> str | None:
    """The value a snapshot shows for this combobox.

    The input's own text wins, and an empty input is ``""``, not a missing
    read. U+FFFC is Chromium's placeholder for each option, so a string that
    is only those characters falls through to the entry or the selected
    option. None means the read failed.
    """
    if acc is None:
        return None
    try:
        if accessible_gone(acc):
            return None
    except Exception:
        return None
    text = _full_text(acc)
    if isinstance(text, str) and _OBJECT_REPLACEMENT not in text:
        return _choice_text(text)
    if isinstance(text, str):
        cleaned = _choice_text(_strip_objects(text))
        if cleaned:
            return cleaned
    entry = _combo_entry(acc)
    if entry is not None and entry is not acc:
        entry_text = _full_text(entry)
        if isinstance(entry_text, str) and _OBJECT_REPLACEMENT not in entry_text:
            return _choice_text(entry_text)
        if isinstance(entry_text, str):
            cleaned = _choice_text(_strip_objects(entry_text))
            if cleaned:
                return cleaned
    selected = _selected_option_text(acc)
    if selected:
        return _choice_text(selected)
    return None


def _option_marked_selected(acc) -> bool:
    """True when the option is SELECTED or ``aria-selected`` is true.

    A destroyed option is not selected. The attribute covers a tree that
    publishes aria-selected without the AT-SPI state.
    """
    if acc is None:
        return False
    try:
        if accessible_gone(acc):
            return False
    except Exception:
        return False
    if _row_selected(acc):
        return True
    try:
        attrs = _get_attributes(acc)
    except Exception:
        return False
    for key in ("aria-selected", "selected"):
        if str(attrs.get(key) or "").strip().lower() == "true":
            return True
    return False


def _aria_list_option(acc) -> bool:
    """A web listbox option, not a GTK list row.

    Chromium and Firefox list items count. So does an option whose list
    publishes ``xml-roles`` of listbox or option. A GTK tree or list does
    not, so a row click there still has to move SELECTED.
    """
    if acc is None or not _listbox_option(acc):
        return False
    if "option" in _xml_roles(acc):
        return True
    node = acc
    for _ in range(6):
        try:
            if _chromium_app(node) or _gecko_app(node):
                return True
        except Exception:
            pass
        roles = _xml_roles(node)
        if "listbox" in roles or "combobox" in roles or "option" in roles:
            return True
        parent = _parent_of(node)
        if parent is None or parent is node:
            return False
        if _role_name(parent) in {"frame", "window", "application", "menu", "popup menu", "menu bar"}:
            return False
        node = parent
    return False


def combobox_option_context(acc) -> dict | None:
    """Before-click reading of the combobox that owns this option, or None.

    None when this is not a web listbox option or no combobox owns it. The
    reading is taken before the click. An ARIA option's row disappears when
    the list closes, and the comparison still has the old value.
    """
    try:
        if not _aria_list_option(acc):
            return None
        combo = _combobox_owner(acc)
        if combo is None:
            return None
        _call_first(combo, ("clear_cache", "clearCache"))
        return {
            "combo": combo,
            "label": _choice_text(_option_label(acc)),
            "text": _combobox_text(combo),
            "name": _choice_text(_node_name(combo)),
            "expanded": bool(_popup_open(combo)),
            "selected": _choice_text(_selected_option_text(combo)),
            "marked": _option_marked_selected(acc),
        }
    except Exception:
        return None


def _combobox_commit_signals(ctx: dict, option) -> bool:
    """True when the combobox now shows this option, or the option became selected.

    A value or name that changed to the option counts. So does aria-selected
    arriving on the option. A popup that was open and is now closed counts
    when the collapsed combobox's text or name is the option, including when
    that text was already the option and the click only closed the list.
    """
    label = _choice_text(ctx.get("label"))
    if not label:
        return False
    if option is not None and _option_marked_selected(option) and not ctx.get("marked"):
        return True
    combo = ctx.get("combo")
    if combo is None:
        return False
    try:
        if accessible_gone(combo):
            return False
    except Exception:
        return False
    _call_first(combo, ("clear_cache", "clearCache"))
    shown = _combobox_text(combo)
    name = _choice_text(_node_name(combo))
    before_text = _choice_text(ctx.get("text"))
    before_name = _choice_text(ctx.get("name"))
    before_selected = _choice_text(ctx.get("selected"))
    if shown is not None and _choice_text(shown) == label and before_text != label:
        return True
    if name == label and before_name != label:
        return True
    selected = _choice_text(_selected_option_text(combo))
    if selected == label and before_selected != label:
        return True
    if ctx.get("expanded") and not _popup_open(combo) and (
        _choice_text(shown) == label or name == label
    ):
        return True
    return False


def combobox_option_committed(ctx, option) -> bool:
    """True when the click committed this option and the reading stays put.

    A one-read flicker is not a commit. A combobox that still shows its old
    value, with the popup still open and the option not selected, is not a
    commit. None ``ctx`` is not a listbox option and is not a commit.
    """
    if not ctx or not _choice_text(ctx.get("label")):
        return False
    for attempt in range(8):
        try:
            landed = _combobox_commit_signals(ctx, option)
        except Exception:
            landed = False
        if landed:
            time.sleep(0.04)
            try:
                if _combobox_commit_signals(ctx, option):
                    return True
            except Exception:
                pass
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def chromium_list_row(acc) -> bool:
    """True when ``acc`` is an option in a Chromium list or list box.

    The option's ``select`` action toggles, and ``Selection.select_child``
    issued while that action is still landing cancels it. A plain click at
    the row center is the path that selects the row. GTK tables are not
    Chromium lists and stay on Selection.
    """
    parent, _index, target = _selection_parent(acc)
    if parent is None or target is None:
        return False
    if _role_name(parent) not in {"list box", "list"}:
        return False
    try:
        return bool(_chromium_app(parent))
    except Exception:
        return False


def chromium_select_option_parent(acc):
    """Native HTML size/multiple select only; ARIA lists keep exact hit tests."""
    if acc is None or not _chromium_app(acc):
        return None
    if _role_name(acc) != "list item" or _get_attributes(acc).get("tag") != "option":
        return None
    parent = _parent_of(acc)
    if (parent is not None and _role_name(parent) == "list box"
            and _get_attributes(parent).get("tag") == "select"):
        return parent
    return None


def chromium_select_selection(parent):
    """Complete native selected identities, or None for an unreadable set."""
    _drop_read_cache()
    _drop_cached_properties(parent)
    iface = _selection_iface(parent)
    count = _call_first(iface, ("get_n_selected_children", "getNSelectedChildren"))
    if type(count) is not int or not 0 <= count <= _MAX_CHILDREN_FETCH:
        return None
    selected = set()
    for index in range(count):
        child = _call_first(iface, ("get_selected_child", "getSelectedChild"), index)
        if child is None:
            return None
        selected.add(_ancestor_key(child))
    return frozenset(selected) if len(selected) == count else None


def _chromium_select_option_at_point(acc, hit, x: int, y: int) -> bool:
    """Resolve a native select hit using fresh, nonoverlapping option boxes.

    Blink's settled hit test returns the select itself, not its option.
    Require that exact parent hit first; never apply this to arbitrary web
    containers, sibling hits, missing hits, or inferred row geometry.
    """
    parent = chromium_select_option_parent(acc)
    if parent is None or _ancestor_key(hit) != _ancestor_key(parent):
        return False
    _drop_read_cache()
    _drop_cached_properties(parent)
    _drop_cached_properties(acc)

    def contains(node):
        pos, size = _extents(node)
        return (pos is not None and size is not None
                and pos[0] <= x < pos[0] + size[0]
                and pos[1] <= y < pos[1] + size[1])

    if not _state_has(parent, "SHOWING") or not contains(parent):
        return False
    if not _state_has(acc, "SHOWING") or not contains(acc):
        return False
    count = _child_count(parent)
    if not 0 < count <= _MAX_CHILDREN_FETCH:
        return False
    matches = []
    for index in range(count):
        child = _child_at(parent, index)
        if child is None:
            return False
        _drop_cached_properties(child)
        if _get_attributes(child).get("tag") != "option":
            return False
        pos, size = _extents(child)
        if pos is None or size is None:
            return False
        if pos[0] <= x < pos[0] + size[0] and pos[1] <= y < pos[1] + size[1]:
            matches.append(_ancestor_key(child))
    return matches == [_ancestor_key(acc)]


def selected_option_names(acc) -> list[str]:
    """Names of the selected options in ``acc``'s list, in child order."""
    parent, _index, _target = _selection_parent(acc)
    if parent is None:
        return []
    names: list[str] = []
    count = min(_child_count(parent), 50)
    for index in range(count):
        child = _child_at(parent, index)
        if child is None or not _row_selected(child):
            continue
        label = _node_name(child)
        if label:
            names.append(label)
    return names


def restore_selection(acc, names: list[str]) -> None:
    """Select each name in ``names`` that a failed click deselected.

    ``select_child`` adds on a Chrome list box. It is not preceded by the
    option's ``select`` action, which would toggle the row back off. Names
    that are still selected are left alone. A list that has no Selection
    interface is unchanged.
    """
    wanted = [name for name in names if name]
    if not wanted:
        return
    parent, _index, _target = _selection_parent(acc)
    if parent is None:
        return
    iface = _selection_iface(parent)
    if iface is None:
        return
    for _attempt in range(6):
        pending = set(wanted)
        count = min(_child_count(parent), 50)
        for index in range(count):
            child = _child_at(parent, index)
            if child is None:
                continue
            label = _node_name(child)
            if label not in pending:
                continue
            if _row_selected(child):
                pending.discard(label)
                continue
            _call_first(iface, ("select_child", "selectChild"), index, default=False)
            if _row_selected(child):
                pending.discard(label)
        if not pending:
            return
        time.sleep(0.05)


def row_becomes_selected(acc) -> bool:
    """True when the row is selected and still selected after a short beat.

    A coordinate click's SELECTED state can trail the button release. The
    wait is a handful of reads and stops on the first stable selection.
    """
    _parent, _index, target = _selection_parent(acc)
    node = target or acc
    for attempt in range(8):
        if _row_selected(node):
            time.sleep(0.04)
            if _row_selected(node):
                return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def _row_settled(node) -> bool:
    """True when ``node`` is selected and still selected after a short beat.

    Chrome's listbox click action can report success and set SELECTED for one
    read, then leave the HTML selection unchanged. A success has to outlast
    that beat. Hermetic fakes patch ``time.sleep`` so the second read is
    immediate.
    """
    if not _row_selected(node):
        return False
    time.sleep(0.04)
    return _row_selected(node)


def _qt_grid(acc):
    """The Qt ``table`` that contains ``acc``, or None for a tree item.

    A ``QTableWidget`` cell's parent is ``table``. A ``QTreeWidget`` item is
    also a ``table cell``, and its parent is ``tree``. The tree item stays
    on Toggle: that selects the row and does not expand it.
    """
    if not _qt_app(acc):
        return None
    node = acc
    for _ in range(8):
        parent = _parent_of(node)
        if parent is None:
            return None
        role = _role_name(parent)
        if role in {"tree", "tree table"}:
            return None
        if role == "table":
            return parent
        node = parent
    return None


def gtk_icon_view_item(acc) -> bool:
    """GTK icon-view images expose activate, which means open, not click."""
    parent = _parent_of(acc)
    return (
        "gtk" in _toolkit_name(acc)
        and _role_name(acc) in {"image", "icon"}
        and parent is not None
        and _role_name(parent) == "layered pane"
    )


def click_gtk_icon_view_item(acc) -> bool:
    """Send one pointer click and confirm exclusive icon selection."""
    parent = _parent_of(acc)
    count = _child_count(parent)
    pos, size = _extents(acc)
    if (
        not _x11_keys_available() or pos is None or size is None
        or size[0] <= 0 or size[1] <= 0 or not 0 < count <= 500
    ):
        return False
    try:
        from a11y_computer_use.drivers import _linux_input

        _linux_input.click(int(pos[0] + size[0] / 2), int(pos[1] + size[1] / 2))
    except Exception:
        return False
    for attempt in range(8):
        selected = [_child_at(parent, index) for index in range(count)]
        if all(child is not None for child in selected):
            selected = [child for child in selected if _state_has(child, "SELECTED")]
            if len(selected) == 1 and selected[0] == acc:
                return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def qt_table_cell(acc) -> bool:
    """True for a cell in a Qt grid, not a tree item and not a GTK cell."""
    try:
        return _role_name(acc) == "table cell" and _qt_grid(acc) is not None
    except Exception:
        return False


def _same_cell(left, right) -> bool:
    if left is right or left == right:
        return True
    name = _node_name(left)
    return bool(name) and name == _node_name(right)


def _qt_cell_exclusive(table, acc) -> bool:
    """True when ``acc`` is the only selected cell and it is focused.

    On Qt 6.4 a pointer click sets ``SELECTED`` and ``FOCUSED`` on that
    cell and clears the others. ``FOCUSED`` is the current cell:
    ``currentItem`` is not its own AT-SPI state. ``Toggle`` sets
    ``SELECTED`` without ``FOCUSED`` and leaves the other cells selected.
    """
    if not _state_has(acc, "SELECTED") or not _state_has(acc, "FOCUSED"):
        return False
    count = min(_child_count(table), 500)
    saw = False
    for index in range(count):
        child = _child_at(table, index)
        if child is None or _role_name(child) != "table cell":
            continue
        if not _state_has(child, "SELECTED"):
            continue
        if _same_cell(child, acc):
            saw = True
            continue
        return False
    return saw


def activate_qt_table_cell(acc) -> bool:
    """Select only this Qt grid cell and make it the current cell.

    ``Toggle`` and ``Selection.select_child`` add the cell and leave the
    current cell unchanged. A click at the cell's center is the mouse
    click. True only when this cell is selected and focused and no other
    cell in the table is selected. A miss is false, so the caller does not
    report the click as confirmed.
    """
    table = _qt_grid(acc)
    if table is None:
        return False
    if _qt_cell_exclusive(table, acc):
        return True
    if not _x11_keys_available():
        return False
    _click_center(acc)
    for attempt in range(8):
        if _qt_cell_exclusive(table, acc):
            return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def _aria_bool(acc, key: str) -> bool | None:
    """``true`` or ``false`` from an object attribute, else None."""
    raw = str(_get_attributes(acc).get(key) or "").strip().lower()
    if raw in {"true", "false"}:
        return raw == "true"
    return None


def _row_toggle_state(acc) -> tuple[bool | None, bool | None]:
    """``(expanded, checked)`` for a row that can toggle, else None per side.

    An ARIA treeitem publishes ``EXPANDABLE`` while collapsed and ``EXPANDED``
    after it opens. ``aria-expanded`` is that bit when the state set has not
    been filled in. A checkable row uses ``CHECKED`` the same way. A row that
    is neither is ``(None, None)`` and is not a toggle.
    """
    expanded: bool | None = None
    if _state_has(acc, "EXPANDABLE") or _state_has(acc, "EXPANDED"):
        expanded = _state_has(acc, "EXPANDED")
    else:
        flag = _aria_bool(acc, "aria-expanded")
        if flag is not None:
            expanded = flag
    checked: bool | None = None
    if _state_has(acc, "CHECKABLE") or _state_has(acc, "CHECKED"):
        checked = _state_has(acc, "CHECKED")
    else:
        flag = _aria_bool(acc, "aria-checked")
        if flag is not None:
            checked = flag
    return expanded, checked


def _row_can_toggle(state: tuple[bool | None, bool | None]) -> bool:
    return state[0] is not None or state[1] is not None


def _row_toggle_committed(before: tuple[bool | None, bool | None], acc) -> bool:
    """True when expanded or checked flipped and the new state stayed.

    Chrome's ``doAction("click")`` returns before ``EXPANDED`` updates. The
    read in the same turn still sees the collapsed treeitem, and a second
    click then flips ``aria-expanded`` back. This waits through that beat.
    The new state has to survive one more read. A row that cannot toggle is
    false with no wait. Hermetic fakes patch ``time.sleep``.
    """
    if not _row_can_toggle(before):
        return False
    for attempt in range(8):
        if _row_toggle_state(acc) != before:
            time.sleep(0.04)
            if _row_toggle_state(acc) != before:
                return True
        if attempt + 1 < 8:
            time.sleep(0.05)
    return False


def row_toggle_state(acc) -> tuple[bool | None, bool | None]:
    """Expanded and checked bits to compare after ``select_contained_row``.

    Read this before that call. The click action inside it returns before
    Chrome publishes the new ``EXPANDED`` state.
    """
    _parent, _index, target = _selection_parent(acc)
    if target is None:
        return None, None
    return _row_toggle_state(target)


def row_toggle_blocks_pointer(acc) -> bool:
    """True when a center click would activate a toggle row a second time.

    ``select_contained_row`` already performed the click or toggle action
    when this is true. A pointer click is another activation. An ARIA
    treeitem's handler flips ``aria-expanded``, so the second click
    collapses the node the first click opened, and ``SELECTED`` never
    appears when the item has no ``aria-selected``. A list option that is
    not expandable or checkable still uses the pointer fallback. A GTK
    cell whose action is expand or activate is not this case: that action
    is not performed here.
    """
    _parent, _index, target = _selection_parent(acc)
    if target is None or not _row_can_toggle(_row_toggle_state(target)):
        return False
    return _first_press_action(acc) in _SELECTING_ACTION_NAMES


def row_toggle_committed(before: tuple[bool | None, bool | None], acc) -> bool:
    """True when the row's expanded or checked state flipped and stayed.

    ``before`` is ``row_toggle_state`` from before the click action. Chrome
    publishes the new state a beat after ``doAction`` returns.
    """
    _parent, _index, target = _selection_parent(acc)
    if target is None:
        return False
    return _row_toggle_committed(before, target)


_FOLDER_VIEW_ROLES = frozenset({"page tab", "page tab list"})
_PATH_ENTRY_ROLES = frozenset({"entry", "text", "editable text", "combo box", "combo-box"})
_PATH_BUTTON_ROLES = frozenset({"push button", "toggle button"})
_PATH_COMMANDS = frozenset({
    "back", "forward", "home", "open parent", "up", "reload", "search",
    "search for files...", "recent", "menubar", "undo", "redo",
    "zoom out", "zoom in", "normal size", "icon view", "list view",
    "compact view", "split view", "open terminal here",
})
_FOLDER_LOCATION_WAIT_S = 1.2


def _raw_action_names(acc) -> list[str]:
    """AT-SPI action names, lowercased, in the order the toolkit lists them."""
    action = _action_iface(acc)
    if action is None:
        return []
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    names = []
    for index in range(int(count)):
        raw = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw:
            names.append(raw)
    return names


def do_named_action(acc, name: str) -> bool:
    """Perform the action named ``name``. False when the row has no such action.

    This does not perform an earlier expand or edit action. ``do_press``
    would, and that is not this row's activation.
    """
    action = _action_iface(acc)
    if action is None:
        return False
    wanted = (name or "").lower()
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    for index in range(int(count)):
        raw = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if raw == wanted:
            return bool(_call_first(action, ("do_action", "doAction"), index, default=False))
    return False


def _pathish(text: str) -> bool:
    value = (text or "").strip()
    return value.startswith("/") or value.startswith("~") or value.startswith("file://")


def _hold_new(node, visited: list) -> bool:
    """True when ``node`` is new. The list keeps that accessible alive.

    An ``id()`` set does not. AT-SPI drops a wrapper once nothing references
    it, and the next child can be born at that same address. The walk then
    treats the new child as already seen and never reads it. Thunar's
    location entry is several panels under a toolbar of empty panels, so it
    is one of those later children.
    """
    if node is None or any(node is item for item in visited):
        return False
    visited.append(node)
    return True


def _toolbars_under(frame) -> list:
    """Toolbars under ``frame``, without walking the folder's table.

    The menu bar's toolbar is not always the one that holds the location.
    """
    found = []
    queue = [frame]
    seen = 0
    visited: list = []
    while queue and seen < 80 and len(found) < 4:
        node = queue.pop(0)
        if not _hold_new(node, visited):
            continue
        seen += 1
        role = _role_name(node)
        if role == "tool bar" and node is not frame:
            found.append(node)
            continue
        if role in _LIST_ROLES and node is not frame:
            continue
        count = _raw_child_count(node)
        for index in range(min(max(count, 0), 32)):
            child = _child_at(node, index)
            if child is not None:
                queue.append(child)
    return found


def _place_in_toolbar(toolbar) -> tuple[str, str] | None:
    """``(text, kind)`` from one toolbar, or None when it has no location.

    The location entry sits several panels under the toolbar. A path-bar
    crumb is a checked button that is not Back, Home, or a view switch.
    """
    entry = None
    crumbs: list[str] = []
    queue = [toolbar]
    seen = 0
    visited: list = []
    while queue and seen < 80 and entry is None:
        node = queue.pop(0)
        if not _hold_new(node, visited):
            continue
        seen += 1
        role = "" if node is toolbar else _role_name(node)
        if node is not toolbar:
            if role in _PATH_ENTRY_ROLES:
                text = (_node_plain_text(node) or "").strip()
                if _pathish(text):
                    entry = text
                    break
            if role in _PATH_BUTTON_ROLES:
                name = _node_name(node).strip()
                if name and name.casefold() not in _PATH_COMMANDS and _state_has(node, "CHECKED"):
                    crumbs.append(name)
            if role in _LIST_ROLES:
                continue
        count = _raw_child_count(node)
        for index in range(min(max(count, 0), 32)):
            child = _child_at(node, index)
            if child is not None:
                queue.append(child)
    if entry:
        return entry, "location"
    if crumbs:
        return " / ".join(crumbs), "path bar"
    return None


def folder_place(acc) -> tuple[str, str] | None:
    """``(text, kind)`` for the location entry or the path bar.

    ``kind`` is ``location`` or ``path bar``. A path entry wins over path
    buttons. Back, Home, and the view switches are not crumbs. None when
    the window has neither.
    """
    frame = _frame_ancestor(acc)
    if frame is None:
        return None
    path = None
    for bar in _toolbars_under(frame):
        reading = _place_in_toolbar(bar)
        if reading is None:
            continue
        if reading[1] == "location":
            return reading
        if path is None:
            path = reading
    return path


def folder_location(acc) -> str | None:
    """Text of the location entry or path bar, or None."""
    place = folder_place(acc)
    if place is None:
        return None
    return place[0]


def _inside_folder_view(acc) -> bool:
    """True when ``acc`` sits in the folder view's tab, not the side pane."""
    node = acc
    for _ in range(16):
        if node is None:
            return False
        role = _role_name(node)
        if role in _FOLDER_VIEW_ROLES:
            return True
        if role in {"split pane", "frame", "application"}:
            return False
        node = _parent_of(node)
    return False


def _in_side_pane(acc) -> bool:
    """True when a split pane contains ``acc`` above the folder view's tab."""
    node = acc
    for _ in range(16):
        if node is None:
            return False
        role = _role_name(node)
        if role in _FOLDER_VIEW_ROLES:
            return False
        if role == "split pane":
            return True
        if role in {"frame", "application"}:
            return False
        node = _parent_of(node)
    return False


def navigating_folder_row(acc) -> bool:
    """A side-pane row, or a tree or list row, that opens a folder.

    The folder view sits in a tab. A single click there selects the file.
    The side pane is the table, tree, or list in the split pane outside
    that tab, and the window has a location entry or a path bar. A tree or
    list outside the tab is the same kind of row when that bar is present.
    A table with no location bar is not one of these rows: selecting it is
    the click.
    """
    parent, _index, target = _selection_parent(acc)
    if parent is None or target is None or _inside_folder_view(acc):
        return False
    parent_role = _role_name(parent)
    if _in_side_pane(acc):
        if parent_role not in _LIST_ROLES:
            return False
    elif parent_role not in {"tree", "tree table", "list", "list box"}:
        return False
    return folder_location(acc) is not None


def _gtk_outline_parent(acc):
    """The tree or tree table that holds ``acc``.

    A plain table is not an outline. The child's index is not read here.
    That read walks every sibling, and a spreadsheet table has hundreds of
    them. A snapshot of those cells must not pay for that walk.
    """
    node = acc
    try:
        for _ in range(8):
            parent = _parent_of(node)
            if parent is None:
                return None
            role = _role_name(parent)
            if role in {"menu", "popup menu", "menu bar"}:
                return None
            if role in {"tree", "tree table"} and _selection_iface(parent) is not None:
                return parent
            if role == "table" and _selection_iface(parent) is not None:
                return None
            node = parent
    except Exception:
        return None
    return None


def _outline_visible_bounds(acc, position, size):
    """Intersect a GTK outline cell with its measured tree and scroll pane.

    GtkTextCellAccessible can report the full label width beyond the pane.
    Its un-clipped centre then belongs to a sibling editor, not the row.
    Only native rectangles contribute to the clickable intersection.
    """
    tree = _gtk_outline_parent(acc)
    boxes = [_extents(tree)] if tree is not None else []
    viewport = _viewport_box(acc)
    if viewport is not None:
        boxes.append(viewport)
    for pos, extent in boxes:
        if pos is None or extent is None:
            continue
        clipped = _intersect_box(position, size, pos, extent)
        if clipped is None:
            return (-1.0, -1.0), (-1.0, -1.0)
        position, size = clipped
    return position, size


def geany_symbol_destination(acc) -> tuple[int, list] | None:
    """Read a Geany symbol's reported line and the window's native status bars.

    These are observation handles only. No selection, activation, or caret
    movement is performed to make a pointer double-click succeed.
    """
    if _application_name(acc).casefold() != "geany" or not outline_double_click_note(acc):
        return None
    match = re.search(r"\[(\d+)\]$", _node_name(acc).strip())
    if match is None:
        return None
    frame = _frame_ancestor(acc)
    # Visit shallow status/editor nodes before descending into populated
    # menus and symbol trees, which can exhaust the bounded walk.
    pending = deque([frame] if frame is not None else [])
    statuses = []
    for _ in range(512):
        if not pending:
            break
        node = pending.popleft()
        if _role_name(node) == "status bar":
            statuses.append(node)
        elif _role_name(node) not in {"menu bar", "menu", "menu item", "popup menu"}:
            pending.extend(_direct_children(node))
    return int(match.group(1)), statuses


def geany_symbol_arrived(destination: tuple[int, list], *, evidence: dict | None = None) -> bool:
    """Verify the native status or focused editor caret, never the row label."""
    line, statuses = destination
    if evidence is not None:
        evidence.clear()
        evidence.update(expected_line=line, status_lines=[], caret_lines=[])
    for status in statuses:
        # Text is the live message; the accessible name may still contain
        # the previous caret line. Never let that older label win the match.
        message = _direct_text(status) or _node_name(status)
        match = re.search(r"\bline:\s*(\d+)\s*/", message)
        if evidence is not None:
            evidence["status_lines"].append(int(match.group(1)) if match is not None else None)
        if match is not None and int(match.group(1)) == line:
            return True
    # Geany refreshes its status label asynchronously. The focused editor's
    # native caret is independent evidence of the requested navigation; a
    # selected symbol row or an unfocused document is not that evidence.
    frames = [_frame_ancestor(status) for status in statuses]
    pending = deque(frame for frame in frames if frame is not None)
    for _ in range(512):
        if not pending:
            break
        node = pending.popleft()
        if _gtk_multiline_document(node) and _state_has(node, "FOCUSED"):
            if is_secure(node):
                continue
            offset = _safe(lambda: _atspi().Text.get_caret_offset(node))
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                continue
            prefix = _safe(lambda: _atspi().Text.get_text(node, 0, offset))
            if isinstance(prefix, str) and len(prefix) == offset:
                caret_line = prefix.count("\n") + 1
                if evidence is not None:
                    evidence["caret_lines"].append(caret_line)
                return caret_line == line
        # Menus contain neither document carets nor caret status. Their
        # descendants can exhaust the budget before a deeply nested editor.
        if _role_name(node) not in {"menu bar", "menu", "menu item", "popup menu"}:
            pending.extend(_direct_children(node))
    return False


def gtk_table_selection_note(acc) -> str:
    """Warn about selection-only GTK table cells without assuming activation.

    GtkTreeView's activate-on-single-click property is not exposed over
    AT-SPI. An advertised activate action does not mean Selection emits
    row-activated. Inspect ancestors only; never enumerate table cells.
    """
    if acc is None or _role_name(acc) not in {"table cell", "tree cell"}:
        return ""
    if "gtk" not in _toolkit_name(acc) or _qt_app(acc) or _chromium_app(acc) or _gecko_app(acc):
        return ""
    if "activate" not in _raw_action_names(acc) or _first_press_action(acc) in _SELECTING_ACTION_NAMES:
        return ""
    node = acc
    for _ in range(8):
        parent = _parent_of(node)
        if parent is None:
            break
        role = _role_name(parent)
        if role in {"tree", "tree table", "menu", "popup menu", "menu bar"}:
            break
        if role == "table":
            if _selection_iface(parent) is not None:
                return "single click selects only; activation may require count=2"
            break
        node = parent
    return ""


def outline_double_click_note(acc) -> str:
    """``activates on double-click`` when a single click only selects the row.

    A GTK tree or tree-table cell with no action interface, or with an
    ``activate`` action that the single-click path does not run. A folder
    row keeps its own navigation. Empty for every other node. This does not
    perform the double-click or the activate action.
    """
    if acc is None:
        return ""
    try:
        if _qt_app(acc) or "gtk" not in _toolkit_name(acc):
            return ""
        if _chromium_app(acc) or _gecko_app(acc):
            return ""
    except Exception:
        return ""
    parent = _gtk_outline_parent(acc)
    if parent is None:
        return ""
    try:
        if navigating_folder_row(acc):
            return ""
    except Exception:
        return ""
    names = _raw_action_names(acc)
    if not names:
        return "activates on double-click"
    if "activate" not in names:
        return ""
    # select_contained_row performs a click or press action. expand, edit,
    # and a later activate are not that click: the row is only selected.
    if _first_press_action(acc) in _SELECTING_ACTION_NAMES:
        return ""
    return "activates on double-click"


def navigation_gesture(acc) -> str | None:
    """How this row opens a folder, or None when a click only selects.

    ``pointer`` is a real click at the row. A GTK shortcut cell lists
    expand, edit, then activate; that activate action reports success and
    leaves the folder where it was, and Return on the selected row does
    too, so neither is the row's activation. ``activate`` is a tree or list
    row whose own action is activate, with no expand or edit ahead of it.
    ``return`` is a row that has no box and no activate action, where
    Return on the selected row is the activation.
    """
    if not navigating_folder_row(acc):
        return None
    names = _raw_action_names(acc)
    if "expand or contract" in names or "edit" in names:
        return "pointer"
    if "activate" in names:
        return "activate"
    try:
        origin, size = _extents(acc)
    except Exception:
        origin, size = None, None
    if origin is None or size is None or size[0] <= 0 or size[1] <= 0:
        return "return"
    return "pointer"


def folder_navigation_verdict(acc, before: str) -> tuple[str, str]:
    """Confirm a folder row only when the location or path bar changes.

    ``before`` is ``folder_location`` from before the click. A row that
    only becomes selected keeps the previous folder, and that click is
    partial.
    """
    deadline = time.monotonic() + _FOLDER_LOCATION_WAIT_S
    while True:
        place = folder_place(acc)
        if place is not None and place[0] != before:
            kind = "path bar" if place[1] == "path bar" else "location"
            return "confirmed", f"the {kind} changed"
        if time.monotonic() >= deadline:
            return "partial", "the row was selected but the location did not change"
        time.sleep(0.05)


def select_contained_row(acc) -> bool | None:
    """Select a row or cell inside a Selection container.

    None means this node is not such a row, so the caller uses ``do_press``.
    True means the row is selected and stayed selected, or an ARIA combobox
    option committed its value and the list closed. False means it is a
    selection row and it is still not selected: the caller clicks the
    on-screen center, then checks again. A GTK cell's expand/edit/activate
    action is not used. A Chrome option's click is, and the selection is
    checked afterwards. A click action that returns without leaving the row
    selected is not success, unless the combobox that owns the option now
    shows it.
    """
    parent, index, target = _selection_parent(acc)
    if parent is None or target is None:
        return None
    choice = combobox_option_context(acc)
    action = _first_press_action(acc)
    if action in _SELECTING_ACTION_NAMES:
        do_press(acc)
        if _row_settled(target):
            return True
        if combobox_option_committed(choice, target):
            return True
    iface = _selection_iface(parent)
    if iface is not None and index is not None:
        _call_first(iface, ("select_child", "selectChild"), index, default=False)
        if _row_settled(target):
            return True
        if combobox_option_committed(choice, target):
            return True
    return False


def row_is_selected(acc) -> bool:
    """Whether the selection row under ``acc`` is selected. False for other nodes."""
    _parent, _index, target = _selection_parent(acc)
    if target is None:
        return False
    return _row_settled(target)


# Chrome's contenteditable ignores EditableText and a short ctrl+a/BackSpace.
# A click is not required: focus plus select-all, BackSpace, and Delete, then
# a poll, is what empties it. Three tries cover a renderer that publishes
# the empty value late. A field that still has words is a failure.
_CONTENT_CLEAR_TRIES = 3
_CONTENT_CLEAR_POLLS = 12


def _chromium_contenteditable(acc) -> bool:
    """True for a Chrome contenteditable, not an input, a textarea, or GTK.

    The live node is an ``entry`` whose tag is ``div`` and whose xml-roles
    is ``textbox``. A number input stays on the number-clear path. A GTK
    entry is not a Chromium app.
    """
    if not _chromium_app(acc) or _number_input(acc):
        return False
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or "").lower()
    if tag in {"input", "textarea", "select"}:
        return False
    xml = set(str(attrs.get("xml-roles") or "").split())
    role = _role_name(acc)
    if "textbox" in xml and tag not in {"input", "textarea"}:
        return True
    return tag in {"div", "span", "p", "pre"} and role in {
        "entry", "text", "section", "paragraph", "panel", "document text",
    }


def _refuse_model_editor_replace(acc) -> None:
    """Refuse known Chromium model editors before any replace mutation.

    The clear-and-type path can delete their paragraphs, then mistake the
    remaining object placeholder for uncleared text and never insert. Native
    text/selection restoration cannot restore the editor model
    or its formatting. Match exact class tokens reported by AT-SPI, including
    empty editors, rather than treating every ARIA textbox as a model editor.
    """
    classes = set(str(_get_attributes(acc).get("class") or "").split())
    if not classes & {"ql-editor", "ck-editor__editable"}:
        return
    if not _chromium_contenteditable(acc):
        return
    raise ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "set_value cannot safely replace this rich-text editor's document model; "
        "nothing was changed. Focus the editor, select the intended text with "
        "keyboard shortcuts, then use type to replace the selection; verify the result",
        detail={"reason": "rich_text_replace_unsafe", "unchanged": True},
    )


def _clear_contenteditable(acc) -> bool:
    """Empty a Chrome contenteditable. True only when the read is blank.

    Focus, select all, BackSpace, then Delete. The read is polled. A newline,
    a space, or NULL counts as empty. Words that are still there are a
    failure: the previous text is put back when this call changed it, and
    the caller reports ``text_mismatch``. An input, a textarea, and a GTK
    field do not use this path.
    """
    if _content_is_blank(acc):
        return True
    if not _x11_keys_available():
        return False

    original = _full_text(acc)
    for _attempt in range(_CONTENT_CLEAR_TRIES):
        grab_focus(acc)
        _chord_into_target(acc, "ctrl+a")
        _chord_into_target(acc, "backspace")
        _chord_into_target(acc, "delete")
        for poll in range(_CONTENT_CLEAR_POLLS):
            if _content_is_blank(acc):
                return True
            if poll + 1 < _CONTENT_CLEAR_POLLS:
                time.sleep(0.05)
    if not _content_is_blank(acc):
        _restore_text(acc, original)
    return False


def _set_contenteditable_by_keys(acc, text: str) -> bool:
    """Replace a Chrome contenteditable that has no EditableText.

    The clear is the same one ``set_value("")`` uses: focus, select-all,
    BackSpace, and Delete, then a poll. A newline from ``<br>`` counts as
    empty. The new string is typed only after that clear. True only when
    the readable text matches, including one trailing newline. A failed
    type puts the original text back. An input, a textarea, and a GTK
    field do not use this path.
    """
    if text == "":
        return _clear_contenteditable(acc)
    if _shown_matches(acc, text):
        return True
    original = _full_text(acc)
    if not _clear_contenteditable(acc):
        return False
    _type_into_target(acc, text)
    if _confirm_text_landed(acc, text):
        return True
    _restore_text(acc, original)
    return False


_TEXT_POINT_STOP = frozenset({
    "frame", "window", "application", "dialog", "alert", "desktop frame",
})


def _text_point_readable(acc) -> bool:
    """True when ``acc`` might expose AT-SPI Text. A bare object does not."""
    if acc is None:
        return False
    return any(
        hasattr(acc, name)
        for name in ("get_role_name", "get_text", "text", "get_editable_text_iface", "get_editable_text")
    )


def _read_text_point(acc):
    """(caret, ranges) for ``acc``, or None when it has no Text interface.

    A missing method is not a caret. Offsets come from ``get_caret_offset``
    and ``get_selection``. No keys are sent.
    """
    if not _text_point_readable(acc):
        return None
    try:
        Atspi = _atspi()
    except Exception:
        return None
    caret = _safe(lambda: Atspi.Text.get_caret_offset(acc))
    count = _safe(lambda: Atspi.Text.get_n_selections(acc))
    caret_ok = isinstance(caret, int) and not isinstance(caret, bool) and caret >= 0
    count_ok = isinstance(count, int) and not isinstance(count, bool) and count >= 0
    if not caret_ok and not count_ok:
        return None
    ranges: list[tuple[int, int]] = []
    if count_ok and int(count) > 0:
        for index in range(min(int(count), 8)):
            bounds = _selection_bounds(
                _safe(lambda index=index: Atspi.Text.get_selection(acc, index))
            )
            if bounds is None:
                continue
            start, end = int(bounds[0]), int(bounds[1])
            if end < start:
                start, end = end, start
            ranges.append((start, end))
    return (int(caret) if caret_ok else None, tuple(ranges))


def _capture_text_points(acc) -> list:
    """Caret and selections on ``acc`` and the document ancestors above it.

    ctrl+a on a Firefox paragraph selects the document web, not only the
    paragraph. The frame, window, and application are not text targets.
    """
    saved = []
    node = acc
    seen: set[int] = set()
    for _depth in range(12):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        role = _role_name(node)
        if node is not acc and role in _TEXT_POINT_STOP:
            break
        point = _read_text_point(node)
        if point is not None:
            saved.append((node, point[0], point[1]))
        if role in _TEXT_POINT_STOP:
            break
        node = _parent_of(node)
    return saved


def _restore_text_points(saved) -> None:
    """Put a captured caret and selection back. No keys.

    Current selections are removed first, including a select-all that
    landed on the document. The saved ranges are written back, then the
    caret. A node whose Text methods are missing is left as it is.
    """
    if not saved:
        return
    try:
        Atspi = _atspi()
    except Exception:
        return
    for acc, caret, ranges in saved:
        count = _safe(lambda acc=acc: Atspi.Text.get_n_selections(acc))
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            for index in range(int(count) - 1, -1, -1):
                _safe(lambda acc=acc, index=index: Atspi.Text.remove_selection(acc, index))
        for slot, (start, end) in enumerate(ranges):
            placed = _safe(
                lambda acc=acc, slot=slot, start=start, end=end: Atspi.Text.set_selection(
                    acc, slot, start, end,
                )
            )
            if not placed:
                _safe(lambda acc=acc, start=start, end=end: Atspi.Text.add_selection(acc, start, end))
        if caret is not None:
            _safe(lambda acc=acc, caret=caret: Atspi.Text.set_caret_offset(acc, caret))


def _shown_is(acc, text: str) -> bool:
    """True when a fresh snapshot read equals ``text``.

    ``None`` is a failed read, not an empty field. An empty request still
    accepts the blank contenteditable newline.
    """
    shown = _full_text(acc)
    if not isinstance(shown, str):
        return False
    if text == "":
        return _text_is_blank(shown)
    return (shown == text or _texts_match(shown, text)) and _qt_text_count_matches(acc, text)


def _replace_editable_text(acc, text: str) -> bool:
    """Replace ``acc`` through EditableText. No key and no selection.

    One ``set_text_contents``. The caller polls the cell: Writer can
    rebuild the paragraph, so a delete or ctrl+a aimed at this object
    would hit the document or the line that was just written.
    """
    eti = _editable_iface(acc)
    if eti is None:
        return False
    _call_first(eti, ("set_text_contents",), text, default=False)
    return _shown_is(acc, text)


def _refuse_drawing_multiline_value(acc, text: str) -> None:
    """Reject paragraph-splitting replacements before any mutation."""
    if ("\n" in text or "\r" in text) and drawing_text_editable(acc):
        # LibreOffice splits the paragraph but can leave the frame's child
        # list stale. The old paragraph cannot verify the complete write.
        # Refuse before even capturing/restoring caret or selection state.
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "Multiline replacement of an Impress or Draw paragraph is unsupported; "
            "no text was changed",
            detail={
                "platform": "linux",
                "reason": "multiline_value_for_paragraph",
                "outcome": "refused",
                "next": ["keyboard"],
                "hint": "Use keyboard editing for paragraph breaks, then verify "
                        "all paragraphs in the text frame.",
            },
        )


def _replace_drawing_paragraph(acc, text: str) -> bool:
    """Replace an Impress or Draw paragraph. No selection and no keys.

    ``set_text`` preflights paragraph breaks before calling this writer.

    One ``set_text_contents``. Success is ``Text.get_text(0, -1)`` equaling
    ``text``, the same read a snapshot uses, polled the way other
    EditableText replaces are. A miss is false and the caller raises
    ``text_mismatch``. Selecting the paragraph is not the fallback: that
    select is what a browser paragraph must not do.
    """
    eti = _editable_iface(acc)
    if eti is None:
        return False
    _call_first(eti, ("set_text_contents",), text, default=False)
    return _confirm_text(acc, text)


def _attribute_multiline(attrs: dict | None) -> bool:
    """True when ``multiline`` or ``aria-multiline`` is true.

    A false or absent attribute is not multi-line. One true key is enough
    when the other key is false. ``STATE_MULTI_LINE`` is checked by the caller.
    """
    for key in ("multiline", "aria-multiline"):
        flag = (attrs or {}).get(key)
        if isinstance(flag, bool):
            if flag:
                return True
            continue
        if str(flag or "").strip().lower() in {"true", "1", "yes"}:
            return True
    return False


def _has_text_interface(acc) -> bool:
    """True when ``acc`` exposes AT-SPI Text or EditableText."""
    if acc is None:
        return False
    if _editable_iface(acc) is not None:
        return True
    count = _safe(lambda: _atspi().Text.get_character_count(acc))
    return isinstance(count, int) and not isinstance(count, bool)


def _gecko_contenteditable(acc) -> bool:
    """True for a Firefox contenteditable that is not an entry.

    Gecko exposes ``<div contenteditable>`` as a section with
    ``STATE_EDITABLE`` and a text interface. An input, a textarea, a
    paragraph, and a select are not this node. Chromium's roleless
    contenteditable stays on its own path.
    """
    if acc is None or not _gecko_app(acc):
        return False
    if _role_name(acc) != "section":
        return False
    attrs = _get_attributes(acc)
    tag = str(attrs.get("tag") or "").lower()
    xml = set(str(attrs.get("xml-roles") or "").split())
    if "combobox" in xml:
        return False
    if tag not in _CONTENTEDITABLE_TAGS and "textbox" not in xml:
        return False
    if _state_has(acc, "READ_ONLY") or not _state_has(acc, "EDITABLE"):
        return False
    return _has_text_interface(acc)


def _gecko_contenteditable_role(node, role: str, role_str: str, attrs: dict, state_editable: bool) -> str:
    """Text role for an editable Gecko contenteditable, else ``role``.

    ``STATE_EDITABLE`` plus a text interface is the editor. ``MULTI_LINE``,
    or a multiline attribute, is a textarea. A single-line editor is a text
    field. A section that is not editable, or that has no text interface,
    stays ``role``. Chromium is unchanged.
    """
    if role_str != "section" or not state_editable or node is None:
        return role
    if not _gecko_app(node):
        return role
    tag = str((attrs or {}).get("tag") or "").lower()
    xml = set(str((attrs or {}).get("xml-roles") or "").split())
    if "combobox" in xml:
        return role
    if tag not in _CONTENTEDITABLE_TAGS and "textbox" not in xml:
        return role
    if _state_has(node, "READ_ONLY") or not _has_text_interface(node):
        return role
    if _state_has(node, "MULTI_LINE") or _attribute_multiline(attrs):
        return "AXTextArea"
    return "AXTextField"


def _wait_until_content_blank(acc) -> bool:
    """Poll until the readable contenteditable text is blank."""
    for attempt in range(16):
        if _content_is_blank(acc):
            return True
        if attempt + 1 < 16:
            time.sleep(0.05)
    return False


def _replace_gecko_contenteditable_by_keys(acc, text: str, original: str | None) -> bool:
    """Replace a Firefox contenteditable with the focused typing path.

    ``grab_focus`` runs first. A later read has to show keyboard focus on
    this node or a descendant before any chord or character is sent. A miss
    raises ``focus_lost`` and types nothing. The new string is typed only
    after the field is blank, through the same ``_type_into_target`` path
    ``type(ref)`` uses. Success is the readable text, where one trailing
    newline is the empty ``<br>``. A failed type puts ``original`` back.
    """
    if not _x11_keys_available():
        return False
    grab_focus(acc)
    if not _confirm_focus_on_target(acc):
        raise _focus_lost_error("focus is not on the target; nothing was typed")
    if _shown_matches(acc, text):
        return True
    if not _content_is_blank(acc):
        _chord_into_target(acc, "ctrl+a")
        _chord_into_target(acc, "backspace")
        _chord_into_target(acc, "delete")
        if not _wait_until_content_blank(acc):
            _restore_text(acc, original)
            return False
    if text == "":
        return _content_is_blank(acc) or _shown_matches(acc, "")
    _type_into_target(acc, text)
    if _confirm_text_landed(acc, text):
        return True
    _restore_text(acc, original)
    return False


def _set_gecko_contenteditable(acc, text: str) -> bool:
    """Replace a Firefox contenteditable and require the read-back.

    EditableText is tried first and is not trusted: Firefox can return
    success and leave the DOM unchanged. The read-back is the readable
    text, including one trailing newline. When that read is not ``text``,
    the fallback is ``_replace_gecko_contenteditable_by_keys``. No Text
    selection is made: selecting a section can select the page.
    """
    if _shown_matches(acc, text):
        return True
    original = _readable_text(acc)
    eti = _editable_iface(acc)
    if eti is not None:
        _call_first(eti, ("set_text_contents",), text, default=False)
        if _confirm_text_landed(acc, text):
            return True
    return _replace_gecko_contenteditable_by_keys(acc, text, original)


def set_text(acc, text: str, *, force: bool = False) -> bool:
    """Replace the element's whole text via AT-SPI EditableText.

    A false return restores the caret and text selection captured on this
    node and its document ancestors before the write. That restore sends
    no keys. Success leaves the caret where the write put it.
    """
    _refuse_gecko_date_segment_replace(acc)
    _refuse_chromium_tab_text(acc, text)
    _refuse_drawing_multiline_value(acc, text)
    _refuse_model_editor_replace(acc)
    saved = _capture_text_points(acc)
    try:
        ok = _set_text_body(acc, text, force=force)
    except Exception:
        _restore_text_points(saved)
        raise
    if not ok:
        _restore_text_points(saved)
    return ok if getattr(ok, "private_text_verified", False) is True else bool(ok)


def _thunar_location_entry(acc) -> bool:
    """Identify Thunar's toolbar entry without including rename/search dialogs."""
    if _role_name(acc) not in {"text", "entry"} or "gtk" not in _toolkit_name(acc):
        return False
    app = _call_first(acc, ("get_application", "getApplication"))
    if str(_call_first(app, ("get_name",), default="")).casefold() != "thunar":
        return False
    node = _parent_of(acc)
    for _ in range(8):
        if node is None:
            break
        role = _role_name(node)
        if role == "tool bar":
            return True
        if role in {"dialog", "frame", "application"}:
            break
        node = _parent_of(node)
    return False


def _set_text_body(acc, text: str, *, force: bool = False) -> bool:
    """Replace the element's whole text via AT-SPI EditableText.

    GTK's ``set_text_contents`` replaces; private bounded verification handles
    empty GTK 4 sentinel reads without publishing their contents. On other
    fields the snapshot read then equals
    ``text``, so nothing is deleted. Chromium's web fields implement that
    call as an insert and the editable-text adaptor still returns true. The
    success check is ``Text.get_text(0, -1)``, the same read a snapshot uses,
    not ``get_text(0, character_count)``. When they disagree, the snapshot
    text is selected and deleted; if it is still there, X11 sends ctrl+a and
    BackSpace. The new string is written only after that, and this returns
    True only when the snapshot read equals ``text``. A toolkit whose text
    cannot be read at all is trusted when ``set_text_contents`` returned
    true, so a replace is not refused just because ``Text.get_text`` failed.
    When that call returned false and the snapshot read is NULL (Chromium's
    empty entry), the field is typed on X11 and this returns true only when
    a later snapshot read equals ``text``. A number input is not typed: focusing
    it can make the empty field read back as 0. Focus that never arrives
    raises ``focus_lost`` and does not report a write.
    A field with no EditableText is cleared and typed on X11; that also
    returns True only when the snapshot read equals ``text``. On Qt the
    character count has to equal that string too: ``GetText`` stops at an
    embedded NUL, so the text alone can match while the widget is longer.
    On Firefox, when EditableText returns success and the snapshot read is
    still not ``text``, the field is focused and the value is typed, and
    success is still that read-back. A Firefox contenteditable section is
    not cleared through Text selection. EditableText is tried, then the
    readable text has to match, including one trailing newline. Otherwise
    focus is confirmed and the same key path ``type(ref)`` uses replaces
    the text. Unconfirmed focus types nothing. Chromium and GTK keep the previous
    tail: keys are sent only when the snapshot read is already empty,
    because focusing a Chrome number input can make an empty field read
    back as 0. When the snapshot read is left blank and the new text cannot
    be verified, the text from before the call is put back, so a
    contenteditable is not left empty. A Chromium field that still reads a
    value (an empty number input reads 0) is not focused in order to restore
    it.     An empty string on a Chrome contenteditable is focus, select-all,
    BackSpace, and Delete, then a poll. A newline left by ``<br>`` is empty.
    Words that remain are ``text_mismatch``, and the previous text stays.
    An input, a textarea, and a GTK field do not use that clear.
    A Chrome contenteditable with no EditableText, including a section that
    has no textbox role, uses that same clear and then types the new string.
    Success is the readable text, where one trailing newline is the ``<br>``.
    A Chrome date, time, or month segment is typed and checked against
    ``valuetext``. The Value interface is not that check.
    A browser paragraph, panel, document, or select-style combo is not replaced.
    Firefox exposes EditableText on those nodes, and selecting one selects
    the page. This returns false before any selection or key. An Impress
    or Draw paragraph with ``STATE_EDITABLE`` and EditableText is replaced
    with ``set_text_contents`` and no selection. ``force`` is the Writer
    table-cell path: one EditableText replace, with no selection and no
    keys. The cell writer polls until the new paragraph is the value.
    """
    if is_secure(acc):
        return False
    if math_command_editable(acc):
        if not _replace_editable_text(acc, text):
            return False
        target = _utf16_length(text) if _qt_app(acc) else len(text)
        Atspi = _atspi()
        _move_caret(Atspi, acc, target)
        if _safe(lambda: Atspi.Text.get_caret_offset(acc)) != target:
            raise _text_mismatch(
                "caret_not_advanced", "the text was replaced but the caret could not be advanced",
                unchanged=False,
            )
        return True
    if drawing_text_editable(acc):
        return _replace_drawing_paragraph(acc, text)
    if not force and _blocks_text_replace(acc):
        return False
    if text_visibility_unknown(acc):
        if _write_unknown_entry(acc, text, insert=False):
            # Boolean success only; carry privacy through the driver to receipts.
            return _PrivateTextCount(1)
        raise _text_mismatch("text_mismatch", "private text write could not be verified",
                             private_text=True)
    if force:
        return _replace_editable_text(acc, text)
    # AbiWord's GtkEntry buffer is not its dialog model: EditableText can
    # echo the requested spacing while OK discards it. We cannot observe
    # that model through AT-SPI, so refuse before changing the buffer rather
    # than claim a committed value or send an unverified activation key.
    if _role_name(acc) in {"text", "entry"} and "gtk" in _toolkit_name(acc):
        app = _call_first(acc, ("get_application", "getApplication"))
        name = _call_first(app, ("get_name",), default="") or ""
        # GtkColorChooser parses its hex entry on activation/focus-out,
        # not EditableText replacement; the buffer is not the selected colour.
        entry_name = _call_first(acc, ("get_name",), default="") or ""
        if str(entry_name).strip().casefold() in {"color name", "colour name"} and re.fullmatch(
            r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", _full_text(acc) or ""
        ):
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "GTK colour entry text does not verify the selected colour; "
                "focus the field, select all, type the colour, then key Return; "
                "verify the preview colour before pressing Select",
                detail={"reason": "commit_unverified"},
            )
        if str(name).casefold() == "eog" and re.fullmatch(
            r"\s*\d+(?:[.,]\d+)?\s*%\s*", _full_text(acc) or ""
        ):
            # A matching entry buffer does not verify the image scale.
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "Eye of GNOME zoom entry text does not verify applied zoom; "
                "use the zoom UI and verify the displayed image scale",
                detail={"reason": "commit_unverified"},
            )
        if str(name).casefold() == "abiword":
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "AbiWord entry values cannot be confirmed as committed; "
                "use focused typing and verify the value after closing and reopening the dialog",
                detail={"reason": "commit_unverified"},
            )
    if _thunar_location_entry(acc):
        # EditableText changes the displayed buffer but not Thunar's path
        # completion model. Real input updates both, leaving Return to the
        # caller. Even equal buffer text must be retyped to repair that model.
        if not _x11_keys_available() or _full_text(acc) is None:
            return False
        _x11_select_all_and_delete(acc)
        if not _wait_until_gone(acc):
            return False
        _type_into_target(acc, text)
        return _confirm_text(acc, text)
    if is_writer_document(acc):
        return _set_writer_document_text(acc, text)
    if _chrome_date_segment(acc):
        return _set_chrome_date_segment(acc, text)
    if text == "" and _chromium_contenteditable(acc):
        return _clear_contenteditable(acc)
    if _gecko_contenteditable(acc):
        return _set_gecko_contenteditable(acc, text)
    eti = _editable_iface(acc)
    if eti is None:
        if _chromium_contenteditable(acc):
            return _set_contenteditable_by_keys(acc, text)
        return _replace_with_keys(acc, text)
    original = _full_text(acc)
    wrote = bool(_call_first(eti, ("set_text_contents",), text, default=False))
    current = _full_text(acc)
    if _verification_matches(acc, text) and _qt_text_count_matches(acc, text):
        return True
    if current is None:
        if wrote:
            return True
        # Chromium's empty entry: set_text_contents returns false and
        # get_text(0, -1) is NULL. Type only when the field still reads
        # blank, and only accept the write when that read comes back as
        # ``text``. A number input is left alone.
        if (
            not _number_input(acc)
            and _text_is_gone(acc)
            and _x11_keys_available()
        ):
            grab_focus(acc)
            _type_into_target(acc, text)
            if _confirm_text(acc, text):
                return True
        return False
    if not _clear_text(acc, eti, current):
        return False
    if not _call_first(eti, ("set_text_contents",), text, default=False):
        insert = None
        for name in ("insert_text", "insertText"):
            insert = getattr(eti, name, None)
            if insert is not None:
                break
        length = _insert_length(insert, text, acc) if insert is not None else len(text)
        if not _call_first(eti, ("insert_text", "insertText"), 0, text, length, default=False):
            if _text_is_gone(acc) and _x11_keys_available():
                _type_into_target(acc, text)
            else:
                return False
    if _confirm_text(acc, text):
        return True
    # Firefox returns true from set_text_contents and insert_text and the DOM
    # stays empty. Key events land after the field is focused. Chromium is
    # not this path: focusing an empty number input can make the read-back 0.
    # A contenteditable that does not confirm is not left empty: the clear
    # already removed "Hello world", so the original is typed back.
    if _gecko_app(acc):
        if _focus_and_replace(acc, text):
            return True
        _restore_text(acc, original)
        return False
    if _text_is_gone(acc) and _x11_keys_available():
        _type_into_target(acc, text)
        if _confirm_text(acc, text):
            return True
    # Only a blank read is restored. A Chromium number input that reads 0
    # after a failed clear is not blank, and focusing it to put the old
    # digits back is what makes the empty field read 0.
    if (
        _chromium_contenteditable(acc)
        and _role_name(acc) in _CONTAINER_TEXT_ROLES
        and _set_contenteditable_by_keys(acc, text)
    ):
        return True
    if _text_is_gone(acc):
        _restore_text(acc, original)
    return False


def scroll_to(acc) -> bool:
    """Reveal ``acc`` via AT-SPI (`Component.scroll_to ANYWHERE`) — no cursor move."""
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return False
    stype = getattr(getattr(Atspi, "ScrollType", None), "ANYWHERE", 0)
    return bool(_call_first(comp, ("scroll_to",), stype, default=False))


def scroll_to_edge(acc, edge: str) -> bool:
    """Reveal ``acc`` on one AT-SPI edge. No wheel and no pointer move.

    ``edge`` is an ``Atspi.ScrollType`` member name. ``TOP_EDGE`` puts the
    row at the top of its scroller. False when the binding has no such
    member, the accessible has no component, or ``scroll_to`` is missing.
    ``scroll_to`` still uses ``ANYWHERE`` and is a different call.
    """
    Atspi = _atspi()
    comp = _component(acc)
    if comp is None:
        return False
    scroll_type = getattr(getattr(Atspi, "ScrollType", None), edge, None)
    if scroll_type is None:
        return False
    return bool(_call_first(comp, ("scroll_to",), scroll_type, default=False))


# Pixel scroll walks a few ancestors and their immediate children looking for
# scroll bars. A text buffer can report thousands of children; cap the walk so
# one pixel scroll cannot turn into a full-tree D-Bus crawl.
_MAX_SCROLL_ANCESTORS = 16
_MAX_SCROLL_NODES = 48
_SCROLL_BAR_ROLE = "scroll bar"


# bus name -> monotonic time before which another reconnect is refused.
# A dialog flood wedges the current connection. Replacing it once recovers
# the following read. Further -1 counts in that same walk must not open a
# connection per node. A later action (Ctrl+L in the file chooser) can wedge
# the replacement too; that is a new failure and gets its own connection
# once the short same-walk block has passed. A replacement that is itself
# still failing extends the block so a dead app cannot reconnect per node.
_revive_block_until: dict[str, float] = {}
_last_revive_key: str | None = None


def _gobject_pointer(obj) -> int | None:
    """Address of a PyGObject, or None for a synthetic test double."""
    cap = getattr(obj, "__gpointer__", None)
    if cap is None:
        return None
    try:
        import ctypes

        api = ctypes.pythonapi
        api.PyCapsule_GetPointer.restype = ctypes.c_void_p
        api.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        api.PyCapsule_GetName.restype = ctypes.c_char_p
        api.PyCapsule_GetName.argtypes = [ctypes.py_object]
        return int(api.PyCapsule_GetPointer(cap, api.PyCapsule_GetName(cap)) or 0) or None
    except Exception:
        return None


def _a11y_bus_address() -> str | None:
    """The session's accessibility bus address, or None when it cannot be read."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress",
            None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1000, None,
        )
        address = reply.get_child_value(0).get_string()
    except Exception:
        return None
    return address or None


# Leaf menu actions that open Gtk.Dialog.run() must not be sent on the
# connection later reads use. The first such action wedges libatspi's private
# socket; revive switches reads onto the shared accessibility bus, and that
# snapshot still shows the dialog. The next leaf DoAction on that shared bus
# holds its dispatch lock for the whole modal loop, so the following snapshot
# waits out the reply and comes back as one opaque region. The leaf is sent
# on the private socket instead, with a short timeout, and reads stay on the
# bus that was already in place.
_MENU_LEAF_TIMEOUT_MS = 400
_MENU_DIALOG_WAIT_S = 2.0
_DIALOG_ROLES = frozenset({
    "dialog", "file chooser", "alert", "color chooser", "font chooser",
})
_private_socket_address_cache: dict[str, str] = {}
_private_action_bus: dict[str, int] = {}


def _application_view(acc):
    """The ``AtspiApplication`` struct for ``acc``, or None for a test double."""
    pointer = _gobject_pointer(acc)
    if not pointer:
        return None
    try:
        import ctypes
    except Exception:
        return None

    class _GObject(ctypes.Structure):
        _fields_ = [
            ("g_class", ctypes.c_void_p),
            ("ref_count", ctypes.c_uint),
            ("pad", ctypes.c_uint),
            ("qdata", ctypes.c_void_p),
        ]

    class _AtspiObject(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("app", ctypes.c_void_p),
            ("path", ctypes.c_char_p),
        ]

    class _AtspiApplication(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("hash", ctypes.c_void_p),
            ("bus_name", ctypes.c_char_p),
            ("bus", ctypes.c_void_p),
        ]

    try:
        obj = _AtspiObject.from_address(pointer)
        if not obj.app:
            return None
        app = _AtspiApplication.from_address(obj.app)
        bus_name = app.bus_name or b""
        if not bus_name.startswith(b":"):
            return None
        return app
    except Exception:
        return None


def _open_registered_connection(address: str) -> int | None:
    """A private dbus connection registered on ``address``, or None."""
    if not address:
        return None
    try:
        import ctypes
    except Exception:
        return None

    class _DBusError(ctypes.Structure):
        _fields_ = [
            ("name", ctypes.c_char_p),
            ("message", ctypes.c_char_p),
            ("dummy", ctypes.c_uint),
            ("padding", ctypes.c_void_p),
        ]

    try:
        lib = ctypes.CDLL("libdbus-1.so.3")
        lib.dbus_error_init.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_error_is_set.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_error_is_set.restype = ctypes.c_int
        lib.dbus_error_free.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_connection_open_private.argtypes = [ctypes.c_char_p, ctypes.POINTER(_DBusError)]
        lib.dbus_connection_open_private.restype = ctypes.c_void_p
        lib.dbus_bus_register.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DBusError)]
        lib.dbus_bus_register.restype = ctypes.c_int
        err = _DBusError()
        lib.dbus_error_init(ctypes.byref(err))
        conn = lib.dbus_connection_open_private(address.encode(), ctypes.byref(err))
        if not conn or lib.dbus_error_is_set(ctypes.byref(err)):
            if lib.dbus_error_is_set(ctypes.byref(err)):
                lib.dbus_error_free(ctypes.byref(err))
            return None
        lib.dbus_error_init(ctypes.byref(err))
        if not lib.dbus_bus_register(conn, ctypes.byref(err)):
            if lib.dbus_error_is_set(ctypes.byref(err)):
                lib.dbus_error_free(ctypes.byref(err))
            return None
        return int(conn)
    except Exception:
        return None


def _private_socket_address(bus_name: str) -> str | None:
    """The app's private AT-SPI socket, from ``GetApplicationBusAddress``."""
    cached = _private_socket_address_cache.get(bus_name)
    if cached:
        return cached
    a11y = _a11y_bus_address()
    if not a11y:
        return None
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Gio, GLib

        conn = Gio.DBusConnection.new_for_address_sync(
            a11y,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        reply = conn.call_sync(
            bus_name, "/org/a11y/atspi/accessible/root",
            "org.a11y.atspi.Application", "GetApplicationBusAddress",
            None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1000, None,
        )
        address = reply.get_child_value(0).get_string()
    except Exception:
        return None
    if not address:
        return None
    _private_socket_address_cache[bus_name] = address
    return address


def _private_action_connection(bus_name: str) -> int | None:
    """A connection to the app's private socket, reused until a leaf times out."""
    cached = _private_action_bus.get(bus_name)
    if cached:
        return cached
    address = _private_socket_address(bus_name)
    if not address:
        return None
    conn = _open_registered_connection(address)
    if conn:
        _private_action_bus[bus_name] = conn
    return conn


@contextmanager
def _leaf_action_bus(acc):
    """Point ``acc``'s app at its private socket for one action, then restore.

    The connection that was already installed stays the one later reads use.
    When the private socket cannot be opened, the action uses the current bus.
    """
    app = _application_view(acc)
    if app is None:
        yield
        return
    saved = app.bus
    bus_name = (app.bus_name or b"").decode("utf-8", "replace")
    private = _private_action_connection(bus_name)
    swapped = bool(private) and private != saved
    if swapped:
        app.bus = private
    try:
        yield
    finally:
        if swapped:
            app.bus = saved


def _drop_private_action_bus(acc) -> None:
    app = _application_view(acc)
    if app is None:
        return
    bus_name = (app.bus_name or b"").decode("utf-8", "replace")
    _private_action_bus.pop(bus_name, None)


def dispatch_menu_leaf(node) -> str:
    """Press a menu leaf without waiting out a modal dialog's D-Bus reply.

    Returns ``ok``, ``dispatched``, or ``refused``. A real accessible is
    invoked on the app's private socket with a 400ms reply budget. A fake
    tree used in tests has no D-Bus connection and is pressed directly.
    """
    from a11y_computer_use.drivers import _linux_menus

    if _gobject_pointer(node) is None:
        start = time.monotonic()
        failed = False
        try:
            result = _linux_menus._invoke_press(node)
        except Exception:
            failed = True
            result = False
        return _linux_menus.classify_leaf_press(
            result is True, failed=failed, elapsed=time.monotonic() - start,
        )

    Atspi = None
    try:
        Atspi = _atspi()
        Atspi.set_timeout(_MENU_LEAF_TIMEOUT_MS, 0)
    except Exception:
        Atspi = None
    start = time.monotonic()
    failed = False
    result = False
    try:
        with _leaf_action_bus(node):
            try:
                result = _linux_menus._invoke_press(node)
            except Exception:
                failed = True
                result = False
    finally:
        if Atspi is not None:
            try:
                Atspi.set_timeout(300, 0)
            except Exception:
                pass
    elapsed = time.monotonic() - start
    outcome = _linux_menus.classify_leaf_press(
        result is True, failed=failed, elapsed=elapsed,
    )
    if outcome == "dispatched":
        _drop_private_action_bus(node)
    return outcome


def _is_dialog_role(role: str) -> bool:
    return role in _DIALOG_ROLES or role.endswith(" dialog")


def _menu_dialog_open(acc) -> bool:
    """True when a dialog is the app itself, a child, or one level under that."""
    count = _raw_child_count(acc)
    if count <= 0 or call_went_unanswered():
        return False
    for index in range(min(int(count), 24)):
        child = _child_at(acc, index)
        if child is None:
            continue
        if _is_dialog_role(_role_name(child)):
            return True
        nested = _raw_child_count(child)
        if nested <= 0 or call_went_unanswered():
            continue
        for sub_index in range(min(int(nested), 16)):
            sub = _child_at(child, sub_index)
            if sub is not None and _is_dialog_role(_role_name(sub)):
                return True
    return False


def await_menu_dialog(acc, *, dispatched: bool = False) -> None:
    """Wait until a dialog opened by the leaf is readable, or the budget ends.

    A healthy app with no dialog (Save, Undo) returns after one look. A
    wedged child count replaces the app bus once, then polls for the window.
    ``dispatched`` is a leaf whose reply timed out; that poll runs even when
    the first read still answers, because the dialog may still be mapping.
    """
    if _gobject_pointer(acc) is None:
        return
    deadline = time.monotonic() + (_MENU_DIALOG_WAIT_S if dispatched else 0.0)
    while True:
        count = _raw_child_count(acc)
        wedged = count < 0 or call_went_unanswered()
        if wedged and _revive_application(acc):
            deadline = max(deadline, time.monotonic() + _MENU_DIALOG_WAIT_S)
        if _menu_dialog_open(acc):
            return
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def _revive_application(acc) -> bool:
    """Replace a wedged per-app D-Bus connection with a fresh accessibility-bus one.

    libatspi moves each app onto a private socket. A ``DoAction`` that opens a
    ``Gtk.Dialog.run()`` nested loop (a file chooser, or any dialog the app
    runs modally) leaves that socket stuck: the click itself returns, and the
    next read on the same connection waits out the timeout. Child count comes
    back -1, so the snapshot root is the application group with no children,
    or the app disappears from name search entirely. A new connection to the
    shared accessibility bus reads the same tree at once. The old connection
    is left open: unreffing a socket that still has a watch is a use-after-free.
    Returns True when the pointer was replaced.
    """
    pointer = _gobject_pointer(acc)
    if not pointer:
        return False
    try:
        import ctypes
    except Exception:
        return False

    class _GObject(ctypes.Structure):
        _fields_ = [
            ("g_class", ctypes.c_void_p),
            ("ref_count", ctypes.c_uint),
            ("pad", ctypes.c_uint),
            ("qdata", ctypes.c_void_p),
        ]

    class _AtspiObject(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("app", ctypes.c_void_p),
            ("path", ctypes.c_char_p),
        ]

    class _AtspiApplication(ctypes.Structure):
        _fields_ = [
            ("parent", _GObject),
            ("hash", ctypes.c_void_p),
            ("bus_name", ctypes.c_char_p),
            ("bus", ctypes.c_void_p),
        ]

    class _DBusError(ctypes.Structure):
        _fields_ = [
            ("name", ctypes.c_char_p),
            ("message", ctypes.c_char_p),
            ("dummy", ctypes.c_uint),
            ("padding", ctypes.c_void_p),
        ]

    try:
        obj = _AtspiObject.from_address(pointer)
        path = obj.path or b""
        if not path.startswith(b"/org/a11y/atspi/") or not obj.app:
            return False
        app = _AtspiApplication.from_address(obj.app)
        bus_name = app.bus_name or b""
        if not bus_name.startswith(b":"):
            return False
        key = bus_name.decode("utf-8", "replace")
        now = time.monotonic()
        if now < _revive_block_until.get(key, 0.0):
            return False
        # Hold the block across clear_cache so a re-entrant -1 does not
        # open a second connection before this one has been tried.
        _revive_block_until[key] = now + 0.5
        address = _a11y_bus_address()
        if not address:
            return False
        lib = ctypes.CDLL("libdbus-1.so.3")
        lib.dbus_error_init.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_error_is_set.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_error_is_set.restype = ctypes.c_int
        lib.dbus_error_free.argtypes = [ctypes.POINTER(_DBusError)]
        lib.dbus_connection_open_private.argtypes = [ctypes.c_char_p, ctypes.POINTER(_DBusError)]
        lib.dbus_connection_open_private.restype = ctypes.c_void_p
        lib.dbus_bus_register.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DBusError)]
        lib.dbus_bus_register.restype = ctypes.c_int
        err = _DBusError()
        lib.dbus_error_init(ctypes.byref(err))
        conn = lib.dbus_connection_open_private(address.encode(), ctypes.byref(err))
        if not conn or lib.dbus_error_is_set(ctypes.byref(err)):
            if lib.dbus_error_is_set(ctypes.byref(err)):
                lib.dbus_error_free(ctypes.byref(err))
            return False
        lib.dbus_error_init(ctypes.byref(err))
        if not lib.dbus_bus_register(conn, ctypes.byref(err)):
            if lib.dbus_error_is_set(ctypes.byref(err)):
                lib.dbus_error_free(ctypes.byref(err))
            return False
        app.bus = conn
        global _last_revive_key
        _last_revive_key = key
        _call_first(acc, ("clear_cache", "clearCache"))
        return True
    except Exception:
        return False


def _raw_child_count(acc) -> int:
    _drop_cached_properties(acc)
    try:
        return int(_call_first(acc, ("get_child_count", "get_childCount"), default=0) or 0)
    except (TypeError, ValueError):
        return 0


def _child_count(acc) -> int:
    """Child count, replacing a wedged app bus once when the read returns -1.

    -1 is libatspi's D-Bus failure, not an empty parent. ``range(-1)`` is
    empty, which is how a file chooser or a ``Dialog.run()`` dialog became a
    single disabled group.
    """
    count = _raw_child_count(acc)
    if count < 0 and _revive_application(acc):
        count = _raw_child_count(acc)
        key = _last_revive_key
        if key:
            # A working replacement may be wedged by the next action, so the
            # block stays short. A replacement that still reads -1 is a dead
            # app: keep the block long enough to cover the rest of the walk.
            _revive_block_until[key] = time.monotonic() + (2.0 if count < 0 else 0.05)
    return count


# A GTK tree of a few hundred rows is not a Calc grid. Calc's sheet is a
# million rows, or it sits under a spreadsheet document. Past this size the
# index walk and the sheet sampler stay in charge.
_GTK_TABLE_MAX_ROWS = 100_000
_GTK_TABLE_MAX_COLS = 256
# ``find`` looks up a name through Table.get_accessible_at. The window is
# enough for a scrolled tree of a few hundred rows and stays short of a
# sheet-sized scan.
_GTK_SEEK_MAX_ROWS = 4096
_GTK_SEEK_MAX_COLS = 8
_OFFSCREEN_SENTINEL = -2_000_000_000


def _plain_table_size(node) -> tuple[int | None, int | None]:
    """``(rows, columns)`` from the accessible, then the Table interface."""

    def _as_int(value) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return int(value)

    rows = _as_int(_call_first(node, ("get_n_rows",)))
    cols = _as_int(_call_first(node, ("get_n_columns", "get_n_cols")))
    if rows is not None and cols is not None:
        return rows, cols
    return _table_dimensions(node)


def _table_cell_at(node, row: int, col: int):
    """The cell at ``(row, col)``, from the accessible or the Table interface."""
    cell = _call_first(node, ("get_accessible_at",), row, col)
    if cell is not None:
        return cell
    try:
        table = getattr(_atspi(), "Table", None)
    except ImportError:
        return None
    if table is None:
        return None
    return _safe(lambda: table.get_accessible_at(node, row, col))


def _row_at_child_index(node, index: int) -> int | None:
    """Table row for a child index. None for a header (row -1) or a miss."""
    row = _call_first(node, ("get_row_at_index",), index)
    if row is None:
        try:
            table = getattr(_atspi(), "Table", None)
        except ImportError:
            table = None
        if table is not None:
            row = _safe(lambda: table.get_row_at_index(node, index))
    if isinstance(row, bool) or not isinstance(row, int) or row < 0:
        return None
    return int(row)


def _gtk_row_table(node) -> bool:
    """True for a GTK-sized data table whose cells do not fit the child cap.

    A short tree stays on the index walk. Qt, Chromium, Firefox, and a Calc
    sheet stay on the walks they already had. A few hundred GTK rows would
    otherwise be read as a spreadsheet (the first rows only) or from child 0.
    """
    if _role_name(node) not in {"table", "tree table"}:
        return False
    if _qt_app(node) or _chromium_app(node) or _gecko_app(node):
        return False
    parent = _call_first(node, ("get_parent", "getParent"))
    if parent is not None and "spreadsheet" in _role_name(parent):
        return False
    rows, cols = _plain_table_size(node)
    if rows is None or cols is None or rows <= 0 or cols <= 0:
        return False
    if rows >= _GTK_TABLE_MAX_ROWS or cols >= _GTK_TABLE_MAX_COLS:
        return False
    return rows * cols > MAX_CHILDREN


def _overlaps_screen(pos, size, screen: tuple[int, int]) -> bool:
    """True when the rect meets the screen and is not the GTK off-screen sentinel."""
    if pos is None or size is None:
        return False
    x, y = float(pos[0]), float(pos[1])
    width, height = float(size[0]), float(size[1])
    if width <= 0 or height <= 0 or x <= _OFFSCREEN_SENTINEL or y <= _OFFSCREEN_SENTINEL:
        return False
    screen_w, screen_h = screen
    if screen_w <= 0 or screen_h <= 0:
        return False
    return x < screen_w and y < screen_h and x + width > 0 and y + height > 0


def _on_screen_item(acc, screen: tuple[int, int]):
    """``(accessible, position, size)`` when ``acc`` overlaps the screen."""
    pos, size = _extents(acc)
    if not _overlaps_screen(pos, size, screen):
        return None
    return (acc, pos, size)


def _hit_at(comp, x: float, y: float):
    try:
        coord = getattr(getattr(_atspi(), "CoordType", None), "SCREEN", 0)
    except ImportError:
        coord = 0
    return _call_first(
        comp, ("get_accessible_at_point", "getAccessibleAtPoint"), int(x), int(y), coord,
    )


def _row_for_hit(table, hit) -> int | None:
    """Table row of a hit-test result. None when the hit is a header or a miss.

    The row index is the child index of the node whose parent is the table.
    A text child inside a cell is not that node: its own index is not a row.
    """
    node = hit
    for _depth in range(4):
        if node is None:
            return None
        parent = _parent_of(node)
        if parent is table:
            index = _call_first(node, ("get_index_in_parent", "getIndexInParent"))
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                return None
            return _row_at_child_index(table, int(index))
        node = parent
    return None


def _row_from_edge(table, comp, x: float, start: float, stop: float, step: float) -> int | None:
    """First table row hit walking from ``start`` toward ``stop``.

    Column headers occupy the top of the box and answer no row. A short
    walk covers that band and the empty padding under the last painted row.
    """
    yy = float(start)
    limit = float(stop)
    for _step in range(48):
        if step > 0 and yy >= limit:
            return None
        if step < 0 and yy <= limit:
            return None
        row = _row_for_hit(table, _hit_at(comp, x, yy))
        if row is not None:
            return row
        yy += step
    return None


def _box_overlaps(pos, size, origin, span) -> bool:
    """True when the two rectangles meet and neither is the off-screen sentinel."""
    if pos is None or size is None or origin is None or span is None:
        return False
    x, y = float(pos[0]), float(pos[1])
    width, height = float(size[0]), float(size[1])
    ox, oy = float(origin[0]), float(origin[1])
    ow, oh = float(span[0]), float(span[1])
    if width <= 0 or height <= 0 or ow <= 0 or oh <= 0:
        return False
    if x <= _OFFSCREEN_SENTINEL or y <= _OFFSCREEN_SENTINEL:
        return False
    if ox <= _OFFSCREEN_SENTINEL or oy <= _OFFSCREEN_SENTINEL:
        return False
    return x < ox + ow and x + width > ox and y < oy + oh and y + height > oy


def _intersect_box(pos, size, origin, span):
    """The overlapping rectangle, or None when the overlap is not a real box."""
    if not _box_overlaps(pos, size, origin, span):
        return None
    x, y = float(pos[0]), float(pos[1])
    width, height = float(size[0]), float(size[1])
    ox, oy = float(origin[0]), float(origin[1])
    ow, oh = float(span[0]), float(span[1])
    left = max(x, ox)
    top = max(y, oy)
    right = min(x + width, ox + ow)
    bottom = min(y + height, oy + oh)
    if right - left <= 1 or bottom - top <= 1:
        return None
    return (left, top), (right - left, bottom - top)


def _viewport_box(node):
    """Nearest scroll pane or viewport ancestor that has a real box.

    None when the table is not inside one. A GTK tree whose scroll pane is
    the table's own box is that box, so the span does not move.
    """
    current = _parent_of(node)
    for _depth in range(8):
        if current is None:
            return None
        if _role_name(current) in {"scroll pane", "viewport"}:
            pos, size = _extents(current)
            if (
                pos is not None
                and size is not None
                and float(size[0]) > 1
                and float(size[1]) > 1
            ):
                return pos, size
        current = _parent_of(current)
    return None


def _reported_showing(acc) -> bool | None:
    """SHOWING when a state set was reported. None when that is not evidence.

    An empty set is not evidence: a GTK fake leaves ``states`` empty, and a
    missing bit there is not a hidden cell. A set that names other states
    and omits SHOWING is not painted.
    """
    try:
        names = _state_names(acc)
    except Exception:
        return None
    if not names:
        return None
    return "SHOWING" in names


def _painted_table_item(acc, screen: tuple[int, int]):
    """``(accessible, position, size)`` when ``acc`` is painted in the viewport.

    Overlapping the screen is not enough. A Swing row below the scroll pane
    still has screen coordinates. A reported state set without SHOWING is
    the same row. An empty state set is not that evidence.
    """
    item = _on_screen_item(acc, screen)
    if item is None:
        return None
    if _reported_showing(acc) is False:
        return None
    view = _viewport_box(acc)
    if view is not None and not _box_overlaps(item[1], item[2], view[0], view[1]):
        return None
    return item


def _gtk_visible_span(table, pos, size, rows: int) -> tuple[int, int] | None:
    """Inclusive row range whose cells overlap the viewport, then the screen.

    The table's own box can be the whole body. Rows under that box and still
    on the display are not painted when a scroll pane clips them.
    """
    comp = _component(table)
    if comp is None or pos is None or size is None:
        return None
    view = _viewport_box(table)
    if view is not None:
        clipped = _intersect_box(pos, size, view[0], view[1])
        if clipped is None:
            return None
        pos, size = clipped
    screen = _screen_size()
    clipped = _intersect_box(pos, size, (0.0, 0.0), (float(screen[0]), float(screen[1])))
    if clipped is None:
        return None
    pos, size = clipped
    x = float(pos[0])
    y = float(pos[1])
    width = float(size[0])
    height = float(size[1])
    if width <= 1 or height <= 1:
        return None
    inset = min(40.0, max(8.0, width / 4.0))
    if width > 48:
        inset = min(inset, width - 20.0)
    sample_x = x + max(1.0, inset)
    first = _row_from_edge(table, comp, sample_x, y + 1, y + height, 4)
    if first is None:
        return None
    last = _row_from_edge(table, comp, sample_x, y + height - 2, y, -4)
    if last is None:
        last = first
    if last < first:
        first, last = last, first
    return first, min(last, rows - 1)


class _HeaderRect:
    """Screen box captured before the next column header is requested."""

    def __init__(self, x: float, y: float, width: float, height: float) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _FrozenColumnHeader:
    """A column header whose name and screen box are fixed at read time.

    Java ATK's ``Table.get_column_header`` objects share one native header.
    Asking for the next column rewrites the name on every object already
    returned. The snapshot reads names later, so each column would show the
    last name. The box is captured in the same moment: a later read can
    move too. Role, states, and actions still come from the live object.
    """

    def __init__(self, header, name: str, rect: _HeaderRect | None) -> None:
        self._header = header
        self._name = name
        self._rect = rect

    def get_name(self):
        return self._name

    def getName(self):
        return self._name

    def get_component_iface(self):
        if self._rect is None:
            return _call_first(self._header, ("get_component_iface", "get_component"))
        return self

    def get_component(self):
        return self.get_component_iface()

    def get_extents(self, _coord=None):
        if self._rect is not None:
            return self._rect
        comp = _call_first(self._header, ("get_component_iface", "get_component"))
        if comp is None:
            return None
        return _call_first(comp, ("get_extents", "getExtents"), _coord)

    def __getattr__(self, item):
        return getattr(self._header, item)


def _freeze_column_header(header):
    """Copy the header name and screen box before another column is requested."""
    got = _call_first(header, ("get_name", "getName"), default="")
    name = "" if got is None else str(got)
    rect = None
    try:
        pos, size = _extents(header)
    except Exception:
        pos, size = None, None
    if pos is not None and size is not None:
        rect = _HeaderRect(float(pos[0]), float(pos[1]), float(size[0]), float(size[1]))
    return _FrozenColumnHeader(header, name, rect)


def _gtk_column_headers(table, cols: int) -> list:
    """Column headers, from the Table interface or the leading header children.

    Each Table-interface header is frozen as soon as it is returned. Java
    ATK reuses the native header state, so the next ``get_column_header``
    would otherwise rename the columns already collected.
    """
    found = []
    seen: set[int] = set()
    for col in range(max(0, cols)):
        header = _call_first(table, ("get_column_header",), col)
        if header is None or id(header) in seen:
            continue
        seen.add(id(header))
        found.append(_freeze_column_header(header))
    if found:
        return found
    for index in range(min(_raw_child_count(table), max(cols, 0) + 2)):
        child = _child_at(table, index)
        if child is None or id(child) in seen:
            continue
        if "header" not in _role_name(child):
            break
        seen.add(id(child))
        found.append(child)
    return found


def _matches_seek(acc, needle: str) -> bool:
    """Case-insensitive substring of the accessible name, as ``find`` matches a title."""
    name = str(_call_first(acc, ("get_name",), default="") or "")
    if not name.strip():
        name = str(_call_first(acc, ("get_description",), default="") or "")
    haystack = name.replace("\u00a0", " ").casefold()
    return needle.replace("\u00a0", " ").casefold() in haystack


def _child_label(node) -> str:
    """The row's own name, or the first named descendant one level down.

    A Chromium table row is unnamed. The cell under it carries ``Row 001``.
    """
    if node is None:
        return ""
    name = _node_name(node)
    if name:
        return name
    count = _child_count(node)
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return ""
    for index in range(min(int(count), 3)):
        child = _child_at(node, index)
        label = _node_name(child) if child is not None else ""
        if label:
            return label
    return ""


def child_labels(node, limit: int | None = None) -> list[str]:
    """Document-order labels of ``node``'s children, capped at the seek window."""
    count = _child_count(node)
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return []
    cap = _GTK_SEEK_MAX_ROWS if limit is None else int(limit)
    labels = []
    for index in range(min(int(count), cap)):
        labels.append(_child_label(_child_at(node, index)))
    return labels


def _seek_hit(node, needle: str):
    """``node`` or one direct child whose name contains ``needle``.

    A named miss is not opened past that one level. The cell name is the
    row text; the button inside a named cell is that one child.
    """
    if node is None:
        return None
    if _matches_seek(node, needle):
        return node
    count = _child_count(node)
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return None
    for index in range(min(int(count), 4)):
        child = _child_at(node, index)
        if child is not None and _matches_seek(child, needle):
            return child
    return None


def _contains_needle(node, needle: str, depth: int = 0) -> bool:
    """True when ``node`` or a close descendant is named ``needle``.

    Used only to decide that a painted row is already on screen, so ``find``
    does not scan the rest of a long table.
    """
    if node is None or depth > 3:
        return False
    if _matches_seek(node, needle):
        return True
    count = _child_count(node)
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return False
    for index in range(min(int(count), 6)):
        if _contains_needle(_child_at(node, index), needle, depth + 1):
            return True
    return False


def _visible_tree_matches(rows, needle: str) -> bool:
    """True when a painted row or a cell under it already contains ``needle``."""
    if _gtk_rows_match(rows, needle):
        return True
    for acc, _pos, _size in rows:
        if _contains_needle(acc, needle):
            return True
    return False


def _table_seek_cell(node, needle: str):
    """``(cell, row, rows)`` for the first cell whose name contains ``needle``.

    ``Table.get_accessible_at`` addresses a row the snapshot never walks.
    None when no cell in the seek window matches. A long HTML table is not
    a spreadsheet: 300 rows still scan. Calc never reaches this from the
    Chromium document walk.
    """
    rows, cols = _plain_table_size(node)
    if rows is None or rows <= 0:
        counted = _child_count(node)
        rows = int(counted) if isinstance(counted, int) and not isinstance(counted, bool) else 0
        cols = 1
    if rows <= 0:
        return None
    cols = cols if isinstance(cols, int) and not isinstance(cols, bool) and cols > 0 else 1
    total = max(rows, 1)
    for row in range(min(rows, _GTK_SEEK_MAX_ROWS)):
        for col in range(min(cols, _GTK_SEEK_MAX_COLS)):
            cell = _table_cell_at(node, row, col)
            if cell is None and col == 0:
                cell = _child_at(node, row)
            found = _seek_hit(cell, needle)
            if found is not None:
                return found, row, total
    return None


_SEEK_SKIP_ROLES = frozenset({
    "document web", "document frame", "application", "frame", "window",
    "panel", "filler", "section", "grouping", "scroll pane", "viewport",
    "desktop frame", "dialog",
})
_SEEK_NODE_BUDGET = 8000
_SEEK_CHILD_CAP = 400


def _content_documents(root, limit: int = 4) -> list:
    """Up to ``limit`` document nodes under ``root``, chrome only until then."""
    found: list = []
    queue = [root]
    seen: set[object] = set()
    examined = 0
    while queue and len(found) < limit and examined < 300:
        node = queue.pop(0)
        if node is None:
            continue
        key = _frame_cache_key(node)
        if key in seen:
            continue
        seen.add(key)
        examined += 1
        role = _role_name(node)
        if role in {"document web", "document frame"}:
            found.append(node)
            continue
        count = _child_count(node)
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            continue
        for index in range(min(int(count), 30)):
            queue.append(_child_at(node, index))
    return found


def _seek_document_tree(node, needle: str, budget: list[int], seen: set[object]):
    """The first match in document order, or None.

    A table is scanned through the Table interface and not by walking every
    row as a child, so a 300-row table below the fold is still reached.
    Other nodes are searched children-first, so a cell wins over the
    container whose value is the whole table. Identity is the AT-SPI object
    path: ``id()`` is reused when a proxy is freed, and that collision
    skipped the table at the end of a long document.
    """
    if node is None or budget[0] <= 0:
        return None
    key = _frame_cache_key(node)
    if key in seen:
        return None
    seen.add(key)
    budget[0] -= 1
    role = _role_name(node)
    if role in {"table", "tree table"}:
        return _table_seek_cell(node, needle)
    count = _child_count(node)
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        for index in range(min(int(count), _SEEK_CHILD_CAP)):
            if budget[0] <= 0:
                return None
            found = _seek_document_tree(_child_at(node, index), needle, budget, seen)
            if found is not None:
                return found
    if role not in _SEEK_SKIP_ROLES and _matches_seek(node, needle):
        # Zero rows marks a document-text hit, not an indexed table hit.
        return node, 0, 0
    return None


def reveal_text_in_document(root, needle: str, *, offscreen: list | None = None) -> bool:
    """Scroll a document-order match for ``needle`` onto the screen.

    The walk includes nodes the pruner will drop, so a table that starts
    below the viewport is still searched. A match that is already on screen
    is left where it is. False when nothing in the seek window matches.
    Non-table text uses one TOP_LEFT request; Chromium PDF geometry may
    update later. ``offscreen`` retains those hits for snapshot attachment
    if pruning still drops them, without claiming that scrolling completed.
    """
    text = needle.strip() if isinstance(needle, str) else ""
    if not text or root is None:
        return False
    budget = [_SEEK_NODE_BUDGET]
    seen: set[object] = set()
    hit = None
    documents = _content_documents(root)
    for doc in documents:
        hit = _seek_document_tree(doc, text, budget, seen)
        if hit is not None:
            break
    if hit is None:
        hit = _seek_document_tree(root, text, budget, seen)
    if hit is None:
        return False
    cell, row, rows = hit
    screen = _screen_size()
    if _on_screen_item(cell, screen) is not None:
        return True
    if rows > 0:
        _bring_cell_on_screen(cell, screen, row, rows)
    else:
        # Chromium PDF scrolling is asynchronous. Do not stack ANYWHERE,
        # TOP_EDGE and a scrollbar write against the same stale extents.
        # A document-order text hit has no table row fraction either.
        scroll_to_edge(cell, "TOP_LEFT")
        if offscreen is not None:
            offscreen.append((cell, _parent_of(cell)))
    _invalidate_tree_cache(root)
    return True


def _reveal_chromium_table_match(node, needle: str) -> bool:
    """Scroll a Chromium table cell named ``needle`` into view.

    The snapshot lists the on-screen band only, so a row below that band
    is missing with nothing for ``find`` to match. ``Table.get_accessible_at``
    still addresses it. False when no cell in the seek window matches.
    """
    found = _table_seek_cell(node, needle)
    if found is None:
        return False
    cell, row, rows = found
    _bring_cell_on_screen(cell, _screen_size(), row, rows)
    _invalidate_tree_cache(node)
    return True


def _gtk_rows_match(rows, needle: str) -> bool:
    return any(_matches_seek(acc, needle) for acc, _pos, _size in rows)


def _scroll_vertical_bar_to_row(cell, row: int, rows: int) -> bool:
    """Move an ancestor's vertical bar so ``row`` is in the scrolled window.

    GTK tree cells report ``scroll_to`` as unsupported, including cells that
    are already on screen. The scroll pane's bar is a Value in pixels. A
    fraction of that range puts the row in the window. A bar with no range
    is left alone.
    """
    if rows <= 1:
        return False
    fraction = max(0.0, min(1.0, float(row) / float(rows - 1)))
    for bar in _collect_scrollbars(cell):
        if _bar_axis(bar) != "vertical":
            continue
        minimum = _value_bound(bar, ("get_minimum_value", "getMinimumValue"), 0.0)
        maximum = _value_bound(bar, ("get_maximum_value", "getMaximumValue"), 0.0)
        if maximum <= minimum:
            continue
        _write_value(bar, minimum + (maximum - minimum) * fraction)
        return True
    return False


def _bring_cell_on_screen(cell, screen: tuple[int, int], row: int, rows: int) -> bool:
    """Scroll ``cell`` into view. True when its box then overlaps the screen."""
    scroll_to(cell)
    if _on_screen_item(cell, screen) is not None:
        return True
    scroll_to_edge(cell, "TOP_EDGE")
    if _on_screen_item(cell, screen) is not None:
        return True
    _scroll_vertical_bar_to_row(cell, row, rows)
    for _attempt in range(8):
        if _on_screen_item(cell, screen) is not None:
            return True
        time.sleep(0.05)
    return _on_screen_item(cell, screen) is not None


def _cell_matches_needle(cell, needle: str) -> bool:
    """True when the cell, or an unnamed cell's child, is named ``needle``.

    A GTK cell carries the row text on itself. Opening every child of a
    miss is what made a 10k TreeView find exceed the event-thread budget.
    Swing leaves the cell unnamed and puts the text on a child. That is
    the only miss that looks one level down.
    """
    if cell is None:
        return False
    name = str(_call_first(cell, ("get_name", "getName"), default="") or "")
    folded = needle.replace("\u00a0", " ").casefold()
    haystack = name.replace("\u00a0", " ").casefold()
    if name.strip():
        return folded in haystack
    return _seek_hit(cell, needle) is not None


def _bring_gtk_cell_into_view(cell, screen: tuple[int, int], row: int, rows: int) -> bool:
    """Scroll ``cell`` into the viewport. True when it is then painted.

    On screen but below the scroll pane is not painted. ``scroll_to`` and
    the vertical bar are the same calls a GTK tree already uses. A cell
    that stays outside the viewport is not given a coordinate.
    """
    scroll_to(cell)
    if _painted_table_item(cell, screen) is not None:
        return True
    scroll_to_edge(cell, "TOP_EDGE")
    if _painted_table_item(cell, screen) is not None:
        return True
    _scroll_vertical_bar_to_row(cell, row, rows)
    for _attempt in range(8):
        if _painted_table_item(cell, screen) is not None:
            return True
        time.sleep(0.05)
    return _painted_table_item(cell, screen) is not None


def _reveal_named_cell(cell, screen: tuple[int, int], row: int, rows: int):
    """Scroll ``cell`` on screen when AT-SPI can. The same cell either way.

    A cell that stays off screen is still the match. Its extents are left
    alone, including the GTK off-screen sentinel.
    """
    if cell is None:
        return None
    if _on_screen_item(cell, screen) is None and rows > 0:
        _bring_cell_on_screen(cell, screen, row, rows)
    return cell


def _seek_unshown_children(node, needle: str, screen: tuple[int, int]):
    """A non-showing child whose name contains ``needle``.

    Used when the Table interface exposes no cell. GTK still lists those
    cells as children and parks them at the off-screen sentinel. On-screen
    children are already in the painted span.
    """
    count = _child_count(node)
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return None
    rows, cols = _plain_table_size(node)
    if isinstance(rows, int) and not isinstance(rows, bool) and rows > 0:
        col_span = cols if isinstance(cols, int) and not isinstance(cols, bool) and cols > 0 else 1
        cap = min(count, rows * min(col_span, _GTK_SEEK_MAX_COLS) + 64)
    else:
        cap = min(count, _GTK_TABLE_MAX_ROWS)
    total = rows if isinstance(rows, int) and not isinstance(rows, bool) and rows > 0 else cap
    for index in range(cap):
        child = _child_at(node, index)
        if child is None or _on_screen_item(child, screen) is not None:
            continue
        found = _seek_hit(child, needle)
        if found is None:
            continue
        row = _row_at_child_index(node, index)
        if row is None:
            row = min(index, max(total - 1, 0))
        return _reveal_named_cell(found, screen, row, total)
    return None


def _reveal_gtk_table_match(node, needle: str, screen: tuple[int, int]):
    """The Table cell named ``needle``. None when no cell matches.

    The scan uses ``Table.get_accessible_at``, which addresses a row the
    on-screen walk never visits. The row cap is the table's own size, not
    the short seek window used for a document table. When that interface
    exposes no cell, non-showing children are searched instead. A match is
    scrolled on screen when AT-SPI can do it. A cell that stays off screen
    is still returned, with whatever extents the toolkit reported.
    """
    rows, cols = _plain_table_size(node)
    if rows is None or cols is None or rows <= 0:
        return _seek_unshown_children(node, needle, screen)
    col_limit = min(cols, _GTK_SEEK_MAX_COLS)
    saw_cell = False
    for row in range(min(rows, _GTK_TABLE_MAX_ROWS)):
        for col in range(col_limit):
            cell = _table_cell_at(node, row, col)
            if cell is None:
                continue
            saw_cell = True
            if not _cell_matches_needle(cell, needle):
                continue
            if _painted_table_item(cell, screen) is None:
                _bring_gtk_cell_into_view(cell, screen, row, rows)
                refreshed = _table_cell_at(node, row, col)
                if refreshed is not None:
                    cell = refreshed
            return cell
    if saw_cell:
        return None
    return _seek_unshown_children(node, needle, screen)


def _visible_gtk_table_cells(node, screen: tuple[int, int]) -> list | None:
    """On-screen headers and body cells, or None when the span cannot be read.

    None keeps the index walk, which is how a header-only file chooser still
    gains its body through ``_with_table_body``.
    """
    pos, size = _extents(node)
    if not _overlaps_screen(pos, size, screen):
        return None
    rows, cols = _plain_table_size(node)
    if rows is None or cols is None:
        return None
    span = _gtk_visible_span(node, pos, size, rows)
    if span is None:
        return None
    first, last = span
    out = []
    view = _viewport_box(node)
    for header in _gtk_column_headers(node, cols):
        item = _on_screen_item(header, screen)
        if item is None:
            continue
        if view is not None and not _box_overlaps(item[1], item[2], view[0], view[1]):
            continue
        out.append(item)
    for row in range(first, last + 1):
        for col in range(cols):
            cell = _table_cell_at(node, row, col)
            if cell is None:
                continue
            item = _painted_table_item(cell, screen)
            if item is not None:
                out.append(item)
    return out or None


def _gtk_table_rows_for_snapshot(
    node, seek: str | None, offscreen: list | None = None,
) -> list | None:
    """On-screen cells of a large GTK table, or None to keep the index walk.

    ``seek`` is the ``find`` text. When no painted cell contains it, the
    matching Table cell is scrolled into view and the span is read again.
    A cell that stays off screen is appended to ``offscreen`` as
    ``(cell, table)``. Its bounds are not replaced. A snapshot with no
    seek does not move the tree and does not record those cells.
    """
    if not _gtk_row_table(node):
        return None
    screen = _screen_size()
    painted = _visible_gtk_table_cells(node, screen)
    if not painted:
        return None
    needle = seek.strip() if isinstance(seek, str) else ""
    if needle and not _visible_tree_matches(painted, needle):
        match = _reveal_gtk_table_match(node, needle, screen)
        if match is not None:
            refreshed = _visible_gtk_table_cells(node, screen)
            if refreshed:
                painted = refreshed
            if not _visible_tree_matches(painted, needle):
                item = _painted_table_item(match, screen)
                if item is not None:
                    painted = [*painted, item]
                elif offscreen is not None:
                    offscreen.append((match, node))
    return painted


def _first_large_gtk_table(root):
    """The first large GTK row table under ``root``.

    The walk stops at a table: a 10k TreeView exposes every cell as a child,
    and those cells are not another table. None when the application is not
    GTK or no table of that size is within the walk.
    """
    if root is None or "gtk" not in _toolkit_name(root):
        return None
    queue = [root]
    seen: set[int] = set()
    examined = 0
    while queue and examined < 300:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        examined += 1
        role = _role_name(node)
        if role in {"table", "tree table"}:
            if _gtk_row_table(node):
                return node
            continue
        count = _child_count(node)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            continue
        for index in range(min(int(count), 40)):
            queue.append(_child_at(node, index))
    return None


def _large_gtk_table_from(acc):
    """The large GTK table ``acc`` is, or the one under it, or an ancestor."""
    if acc is None or "gtk" not in _toolkit_name(acc):
        return None
    node = acc
    seen: set[int] = set()
    for _depth in range(8):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        if _role_name(node) in {"table", "tree table"} and _gtk_row_table(node):
            return node
        node = _parent_of(node)
    return _first_large_gtk_table(acc)


def reveal_gtk_named_cell(root, needle: str):
    """``(moved, on_screen)`` for a named cell in a large GTK table.

    None when the application is not GTK or no cell in the table model
    contains ``needle``. The scan uses ``Table.get_accessible_at`` up to the
    GTK table cap, past the shorter document seek window. A cell already in
    the painted span is left where it is: ``moved`` is false. An off-screen
    cell is scrolled with AT-SPI ``scroll_to`` or the vertical bar's row
    index. ``moved`` is true only when that scroll puts the cell on screen.
    Extents are the cell's own; a cell that stays off screen is
    ``(False, False)``.
    """
    text = needle.strip() if isinstance(needle, str) else ""
    if not text or root is None or "gtk" not in _toolkit_name(root):
        return None
    table = _first_large_gtk_table(root)
    if table is None:
        return None
    rows, cols = _plain_table_size(table)
    if rows is None or cols is None or rows <= 0 or cols <= 0:
        return None
    cols = min(int(cols), _GTK_SEEK_MAX_COLS)
    screen = _screen_size()
    pos, size = _extents(table)
    span = _gtk_visible_span(table, pos, size, rows)
    if span is not None:
        first, last = span
        for row in range(first, last + 1):
            for col in range(cols):
                cell = _table_cell_at(table, row, col)
                if cell is not None and _matches_seek(cell, text):
                    return False, _on_screen_item(cell, screen) is not None
    limit = min(int(rows), _GTK_TABLE_MAX_ROWS)
    for row in range(limit):
        for col in range(cols):
            cell = _table_cell_at(table, row, col)
            if cell is None or not _matches_seek(cell, text):
                continue
            if _on_screen_item(cell, screen) is not None:
                return False, True
            brought = _bring_cell_on_screen(cell, screen, row, rows)
            if brought:
                _invalidate_tree_cache(table)
                return True, True
            return False, False
    return None


def scroll_gtk_viewport_page(acc, direction: int) -> bool:
    """Move a large GTK table's vertical bar by one painted viewport.

    False unless the accessible belongs to GTK and a large row table can be
    found. ``direction`` is positive to go down. The step is the painted
    body-row count as a fraction of ``rows - 1``, applied to the bar's
    range. A line scroll does not come through here. False when the span
    or the bar cannot be read, or the value does not change.
    """
    if acc is None or isinstance(direction, bool) or not isinstance(direction, int) or direction == 0:
        return False
    table = _large_gtk_table_from(acc)
    if table is None:
        return False
    rows, _cols = _plain_table_size(table)
    if rows is None or rows <= 1:
        return False
    pos, size = _extents(table)
    span = _gtk_visible_span(table, pos, size, rows)
    if span is None:
        return False
    visible = max(1, span[1] - span[0] + 1)
    sign = 1.0 if direction > 0 else -1.0
    fraction = float(visible) / float(rows - 1)
    for bar in _collect_scrollbars(table):
        if _bar_axis(bar) != "vertical":
            continue
        minimum = _value_bound(bar, ("get_minimum_value", "getMinimumValue"), 0.0)
        maximum = _value_bound(bar, ("get_maximum_value", "getMaximumValue"), 0.0)
        if maximum <= minimum:
            continue
        current = _read_value(bar)
        if current is None:
            continue
        target = min(max(current + (maximum - minimum) * fraction * sign, minimum), maximum)
        if abs(target - current) < 1e-6:
            continue
        _write_value(bar, target)
        updated = _read_value(bar)
        if updated is not None and abs(updated - current) > 1e-3:
            _invalidate_tree_cache(table)
            return True
    return False


def _owning_large_table(node):
    """``(cell, table)`` when ``node`` is inside a cell of a large table.

    A short table, a spreadsheet, and a node that is not in a cell are
    ``(None, None)``. The published click point stays in those cases.
    """
    current = node
    for _depth in range(6):
        if current is None:
            return None, None
        role = _role_name(current)
        if role in {"table cell", "tree cell"} or role.endswith(" cell"):
            parent = _parent_of(current)
            for _up in range(4):
                if parent is None:
                    return None, None
                parent_role = _role_name(parent)
                if parent_role in {"table", "tree table"}:
                    try:
                        large = _gtk_row_table(parent)
                    except Exception:
                        large = False
                    if not large:
                        return None, None
                    return current, parent
                parent = _parent_of(parent)
            return None, None
        current = _parent_of(current)
    return None, None


def _row_index_of_cell(table, cell) -> int | None:
    index = _call_first(cell, ("get_index_in_parent", "getIndexInParent"))
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    return _row_at_child_index(table, int(index))


def reveal_unpainted_table_click(node):
    """Live center of a table cell that was not painted, after scrolling it.

    None when ``node`` is not an unpainted cell of a large table: the
    caller keeps the published point. A cell that stays outside the
    viewport raises ``not_visible``. No coordinate is invented.
    """
    if node is None:
        return None
    try:
        cell, table = _owning_large_table(node)
    except Exception:
        return None
    if cell is None or table is None:
        return None
    screen = _screen_size()
    if (
        _painted_table_item(node, screen) is not None
        or _painted_table_item(cell, screen) is not None
    ):
        return None
    rows, _cols = _plain_table_size(table)
    total = int(rows) if isinstance(rows, int) and not isinstance(rows, bool) and rows > 0 else 1
    row = _row_index_of_cell(table, cell)
    if row is None:
        row = 0
    _bring_gtk_cell_into_view(cell, screen, row, total)
    painted = _painted_table_item(node, screen) or _painted_table_item(cell, screen)
    if painted is None:
        raise ComputerUseError(
            ErrorCode.NOT_VISIBLE,
            "the table cell is not visible and could not be scrolled into view; the click was not sent",
            detail={
                "reason": "not_visible",
                "outcome": "refused",
                "next": ["ref"],
                "evidence": "the cell is outside the viewport and scrolling did not paint it",
            },
        )
    _acc, pos, size = painted
    return (
        int(round(float(pos[0]) + float(size[0]) / 2.0)),
        int(round(float(pos[1]) + float(size[1]) / 2.0)),
    )


_TOP_LEVEL_WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert"})


def _window_lacks_showing_and_visible(node) -> bool:
    """True when ``node`` reported states and none of them are on screen.

    An empty or missing state set is false: a node that never reported
    states is not treated as hidden.
    """
    sset = _call_first(node, ("get_state_set",))
    if sset is None:
        return False
    names = getattr(sset, "names", None)
    if isinstance(names, (set, frozenset, list, tuple)):
        folded = {str(item).upper() for item in names}
        if not folded:
            return False
        return "SHOWING" not in folded and "VISIBLE" not in folded
    return not _state_has(node, "SHOWING") and not _state_has(node, "VISIBLE")


def _wraps_showing_browser_document(node, depth: int = 0) -> bool:
    """True when a frame still holds the page the user can see.

    Firefox parents the showing ``document web`` under a frame whose own
    state set is only ENABLED. That frame is the page. A hidden dialog
    whose children are ordinary controls is not. The walk is bounded and
    uses the raw child list, so it does not call back into the filter.
    """
    if node is None or depth > 5:
        return False
    count = _child_count(node)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return False
    for index in range(min(count, 12)):
        child = _child_at(node, index)
        if child is None:
            continue
        role = _role_name(child)
        if (
            role in {"document web", "internal frame", "document frame"}
            and _state_has(child, "SHOWING")
        ):
            return True
        if _wraps_showing_browser_document(child, depth + 1):
            return True
    return False


def _hidden_window(node) -> bool:
    """True for a frame, window, or dialog that is not on screen.

    FeatherPad keeps its Replace dialog in the tree as a frame named
    Replacement. The state set is only ENABLED, and the box is a strip at
    the parent origin, so bounds alone would keep it. A window that is
    SHOWING or VISIBLE stays. An empty or missing state set stays too: a
    node that never reported states is not treated as hidden. Other roles
    are not this case. A frame that still wraps a SHOWING browser document
    stays, because that document is the page.
    """
    if node is None or _role_name(node) not in _TOP_LEVEL_WINDOW_ROLES:
        return False
    if not _window_lacks_showing_and_visible(node):
        return False
    return not _wraps_showing_browser_document(node)


def _without_hidden_windows(kids: list) -> list:
    """Drop nested frames and windows that are not showing and not visible."""
    return [child for child in kids if not _hidden_window(child)]


def _with_table_body(node, kids: list) -> list:
    """Append a GTK table's body cells when the children are only headers.

    A file chooser's file list is a table whose AT-SPI children are the
    column headers. ``Table.get_n_rows`` is the file rows. Cells already
    present as children are not added again. A table that already exposes
    row children is unchanged, and a table with no rows stays header-only.

    Every ordinary snapshot walk returns through here, so a hidden frame
    (FeatherPad's Replace dialog) is removed before the table check. A
    showing or visible window is kept. A control that is not a window is
    kept even when it is not showing.
    """
    kids = _without_hidden_windows(kids)
    if _role_name(node) not in {"table", "tree table"}:
        return kids
    rows = _call_first(node, ("get_n_rows",), default=None)
    if isinstance(rows, bool) or not isinstance(rows, int) or rows <= 0:
        return kids
    if kids and any("header" not in _role_name(child) for child in kids):
        return kids
    seen = {id(child) for child in kids}
    body = list(kids)
    for row in range(min(rows, _MAX_CHILDREN_FETCH)):
        cell = _call_first(node, ("get_accessible_at",), row, 0)
        if cell is None or id(cell) in seen:
            continue
        seen.add(id(cell))
        body.append(cell)
    return body


def _child_at(acc, index: int):
    return _call_first(acc, ("get_child_at_index", "getChildAtIndex"), index)


def terminal_ancestor(acc) -> bool:
    """Whether an accessible is inside a terminal, before AX role mapping.

    VTE's terminal role maps to AXTextArea. Inspect the native role so an
    ordinary multiline editor is never mistaken for a command prompt.
    """
    seen = set()
    for _ in range(64):
        if acc is None or id(acc) in seen:
            return False
        seen.add(id(acc))
        if _role_name(acc) == "terminal":
            return True
        acc = _parent_of(acc)
    return False


def command_launcher_ancestor(acc, app: str) -> bool:
    """Identify desktop-shell run prompts, including GNOME's Alt+F2 dialog."""
    from a11y_computer_use.drivers import _linux_system

    if app.casefold() not in _linux_system._COMMAND_DIALOG_HOSTS:
        return False
    seen = set()
    for _ in range(64):
        if acc is None or id(acc) in seen:
            return False
        seen.add(id(acc))
        if _role_name(acc) in {"dialog", "frame", "window"}:
            attrs = _get_attributes(acc)
            identities = {str(attrs.get(key, "")).casefold()
                          for key in ("id", "class", "window-role", "xml-roles")}
            if (identities & _linux_system._COMMAND_DIALOG_IDS
                    or _node_name(acc).casefold() in _linux_system._COMMAND_DIALOG_TITLES):
                return True
        acc = _parent_of(acc)
    return False


def _parent_of(acc):
    memo = getattr(_read_cache_local, "walk_parents", None)
    key = _atspi_wrapper_key(acc) if memo is not None else None
    if key is not None and key in memo:
        return memo[key]
    _drop_cached_properties(acc)
    parent = _call_first(acc, ("get_parent", "getParent"))
    if key is not None:
        memo[key] = parent  # Values pin the parent wrapper for this walk.
    return parent


@contextmanager
def walk_parent_memo():
    """Reuse each reported parent for one read-only snapshot traversal.

    Every walked node re-read its parent for the application lookup and
    again for modal ancestry; the Options dialog paid ~3 get_parent calls
    per cell. Only stable bus-name/object-path identities are memoized, and
    any mutator drops the memo, so ancestry is never reused across input.
    """
    previous = getattr(_read_cache_local, "walk_parents", None)
    _read_cache_local.walk_parents = {} if previous is None else previous
    try:
        yield
    finally:
        _read_cache_local.walk_parents = previous


def _is_scroll_bar(acc) -> bool:
    return _role_name(acc) == _SCROLL_BAR_ROLE


def _node_name(acc) -> str:
    _drop_cached_properties(acc)
    return (_call_first(acc, ("get_name", "getName"), default="") or "").strip()


def top_window_title(acc) -> str:
    """Name of the outermost frame, window, or dialog above ``acc``.

    Chrome's window title is that name. The page title is a prefix of it.
    An empty string means no frame name was readable.
    """
    if acc is None:
        return ""
    node = acc
    seen: set[int] = set()
    title = ""
    for _ in range(24):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        if _role_name(node) in {"frame", "window", "dialog"}:
            name = _node_name(node)
            if name:
                title = name
        parent = _parent_of(node)
        if parent is None or parent is node:
            break
        node = parent
    return title


def _is_list_marker(name: str) -> bool:
    """True when ``name`` is a list bullet, not the row's text.

    Chromium's default ``<ul>`` exposes the marker as a static named "•"
    and the row text as its sibling. A one-character name that is not a
    letter or digit is that marker. ``ITEM-001`` is a row.
    """
    text = name.strip()
    if text in _LIST_MARKERS:
        return True
    return len(text) == 1 and not text.isalnum()


# Chromium's first GetAccessibleAtPoint answer is an approximate hit test of
# the accessibility bounds, which stay stale across a wheel. That call starts
# a layout hit test in the renderer. A later call at the same point returns
# the layout row once it is cached. These waits cover that gap. A row that
# never changes is the answer, not a timeout success.
_HIT_TRIES = 3
_HIT_PAUSE_S = 0.05
# The 0.4.12 retest still showed the pre-wheel head 1.5s after a 3-line scroll,
# and a different wrong head about 2.7s later. Polling this long is what lets
# the snapshot head leave the stale row. A head that never changes is not success.
_HEAD_POLLS = 8
_HEAD_PAUSE_S = 0.4
# Pixels inside the list. A still grab is 0. The 0.4.12 retest measured 6.677
# where the list moved and 0 where it did not. The 0.4.13 retest's own
# mean_abs was 0.0 on a scroll whose list later measured 6.13 and 5.90.
# This threshold still sits between a still grab and those moves. A single
# difference above it is not a successful scroll.
_PAGE_MOVE_MEAN = 1.0
# Uniform rows move the list and change few pixels. The 0.4.25 retest of a
# fixed-height list reported page_unchanged at mean_abs about 0.5 to 0.7
# while the painted head had moved, then sent a wheel or a track click as
# well. A grab above this floor, with a new on-screen head, is that move.
# A grab at or under it is still when the head did not move by about the
# requested lines. The 0.4.28 retest moved five rows and stayed under this
# floor, then clicked the track: five rows plus one page. A far hit-test
# name on a still grab is not that step.
_UNIFORM_ROW_MEAN = 0.4
# The grab taken in the same turn as the wheel can still be the pre-paint
# frame. 0.4.13 compared that one pair and returned page_unchanged, so the
# snapshot never advanced. Resample until the pixels change. A box that
# stays at or under the threshold for the whole window is unchanged.
_PAINT_POLLS = 12
_PAINT_PAUSE_S = 0.15
# The top of the list box can be a clipped row. On the 0.4.12 retest the
# snapshot started at ITEM-009 while the screen head was ITEM-010, and
# scroll_to_find showed a clipped row above the visible run. Hit-test
# samples start below this sliver so that edge is not the head. It is not
# an OCR read. A row whose own top is the list's top is fully on screen:
# 0.4.17 and 0.4.18 required that top to clear these 8px, and the snapshot
# then started at the next row.
_CLIP_SLIVER_PX = 8
_LIST_ROLES = frozenset({"list", "list box", "table", "tree", "tree table"})
_MAX_LIST_CONTAINERS = 4
# Hit-test points in ``_probe_visible_rows`` when the child walk has not
# already named the rows. Not a cap on the rows snapshot and find list.
# 0.4.22 through 0.4.26 also stopped the on-screen walk at this count, so a
# 520px list of 18px rows (about 29 painted) published ITEM-001 through
# ITEM-016 and left the rest out with no elision marker.
_MAX_ROW_SAMPLES = 16
# One requested line is one content row. The 0.4.26 retest stepped dy=5 by
# 15 rows on a fixed-height list and by 14 on overflow-y:auto, which is
# five times the old three-rows-per-line pace. The step still stays inside
# the rows already on screen, so the next snapshot overlaps this one.
_ROWS_PER_LINE = 1
# Chromium draws a list marker as its own static ("•") beside the row text.
# The list item's name is empty. Treating the marker as the head leaves the
# head unchanged after a real move, so the line step is rejected and a wheel
# follows. A marker is not a row.
_LIST_MARKERS = frozenset({
    "•", "●", "◦", "▪", "▫", "▸", "‣", "·", "○", "■", "□", "∙", "◉",
})
_ROW_SEQUENCE_CAP = 240
# Nodes examined while looking for the on-screen head. Rows parked above
# the viewport do not count as a reason to stop: the 0.4.16 cap of 40
# could end before the first visible row, and a wrapper whose top is above
# the line was not opened even when it held that row.
_VISIBLE_WALK_CAP = _MAX_CHILDREN_FETCH
_VISIBLE_DESCEND = 4
_SKIP_ROW_ROLES = frozenset({
    "scroll bar", "separator", "menu bar", "tool bar", "status bar",
})
# Rows last confirmed for a list box. A later hit test is not installed over
# these unless a wheel's pixels moved and the head below the sliver changed,
# or the confirmed head's own top has moved above that line.
_SHOWN: dict[tuple, list] = {}
# A one-pixel change in the reported list origin still names the same box.
# A content-height list that scrolls moves its origin by thousands of pixels
# (y=120 to about y=-4847). Those two boxes overlap, and the overlap must
# not resurrect the rows saved at the old origin.
_BOX_JITTER_PX = 4


def _chromium_app(acc) -> bool:
    """True when ``acc`` belongs to Chromium. GTK and other toolkits are not."""
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    toolkit = (_call_first(app, ("get_toolkit_name", "getToolkitName"), default="") or "").lower()
    if "chrom" in toolkit:
        return True
    name = (_call_first(app, ("get_name", "getName"), default="") or "").lower()
    return "chrom" in name


def _reports_multiline(node, attrs: dict) -> bool:
    """True when ``node`` publishes multi-line text.

    ``STATE_MULTI_LINE`` is the AT-SPI fact. ``multiline`` and
    ``aria-multiline`` are the same fact when a bridge publishes them as
    attributes. A false or absent attribute is not multi-line. A true
    attribute is, even when the other key is false.
    """
    if _state_has(node, "MULTI_LINE"):
        return True
    for key in ("multiline", "aria-multiline"):
        flag = (attrs or {}).get(key)
        if isinstance(flag, bool):
            if flag:
                return True
            continue
        if str(flag or "").strip().lower() in {"true", "1", "yes"}:
            return True
    return False


def _gecko_multiline_entry(node, attrs: dict) -> bool:
    """True for a Firefox ``entry`` that is a ``<textarea>``.

    Gecko exposes ``<input>`` and ``<textarea>`` as AT-SPI role ``entry``.
    The textarea carries ``STATE_MULTI_LINE`` or a multiline attribute.
    A single-line input does not. Chromium and GTK are not this check.
    """
    if not _reports_multiline(node, attrs):
        return False
    return _gecko_app(node)


def _gecko_app(acc) -> bool:
    """True when ``acc`` belongs to Firefox. Chromium and GTK are not."""
    if acc is None:
        return False
    app = _call_first(acc, ("get_application", "getApplication")) or acc
    toolkit = (_call_first(app, ("get_toolkit_name", "getToolkitName"), default="") or "").lower()
    if "gecko" in toolkit:
        return True
    name = (_call_first(app, ("get_name", "getName"), default="") or "").lower()
    return name == "firefox" or name.startswith("firefox ")


def _frame_hierarchy_showing(node) -> bool:
    """Whether ``node`` or a document/frame ancestor has STATE_SHOWING.

    Firefox background tabs and the preloaded New Tab page are VISIBLE and
    carry the content area's bounds. They are not SHOWING. The active tab's
    document web, its internal frame, and the scroll pane around that frame
    are SHOWING.
    """
    # Parent reads can return fresh GI proxies. Keep them alive and use
    # native identity so a recycled Python id cannot hide the active page.
    for current in _ancestor_chain(node, 8):
        role = _role_name(current)
        if role in {"document web", "document frame", "internal frame", "scroll pane"}:
            if _state_has(current, "SHOWING"):
                return True
        if role in {"frame", "window", "application"}:
            break
    return False


def _selected_tab_name(node) -> str | None:
    """The selected page tab in ``node``'s top-level frame, or None.

    The parent walk uses `_ancestor_chain`. ``id()`` of a short-lived
    parent wrapper is not the identity of the next ancestor.
    """
    frame = None
    for current in _ancestor_chain(node, 16):
        role = _role_name(current)
        if role in {"frame", "window"}:
            frame = current
            break
    if frame is None:
        return None
    return _selected_tab_under(frame, 0)


def _selected_tab_under(node, depth: int) -> str | None:
    if node is None or depth > 5:
        return None
    role = _role_name(node)
    if role == "page tab" and _state_has(node, "SELECTED"):
        label = _node_name(node)
        return label or None
    if depth and role in {"document web", "internal frame", "document frame"}:
        return None
    count = min(_child_count(node), 30)
    for index in range(count):
        found = _selected_tab_under(_child_at(node, index), depth + 1)
        if found:
            return found
    return None


def _names_match_tab(title: str, tab: str) -> bool:
    """Whether a document title is the selected tab's label.

    Equality is the match. A longer title that starts with a tab label of at
    least 8 characters still matches a truncated tab. ``New Tab`` does not
    match ``Form Probe``.
    """
    left = title.strip().casefold()
    right = tab.strip().casefold()
    if not left or not right:
        return True
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= 8 and long.startswith(short)


def _enclosing_tab_document(doc):
    """The tab's own document web, not an iframe document nested under it.

    Firefox gives a child frame the ``<title>`` of the page inside the
    frame. That name is not the selected tab. The title match belongs to
    the outermost document web under the window. An iframe's document web
    has that outer document as an ancestor.
    """
    outer = doc
    for current in _ancestor_chain(doc, 24):
        role = _role_name(current)
        if role in {"frame", "window", "application"}:
            break
        if role == "document web":
            outer = current
    return outer


def _gecko_web_document_on_screen(doc) -> bool:
    """True when this document web is the one the user can see.

    The frame hierarchy has to be SHOWING. When the window has a selected
    page tab, the tab's own document title has to be that tab. A preloaded
    New Tab that is not the selected tab is not on screen, and neither is a
    background tab whose frame is only VISIBLE. A child frame's document
    keeps its own ``<title>``. That title is not compared to the tab: the
    match uses the enclosing tab document, so a titled iframe in the active
    tab stays, and one in a background tab does not.
    """
    if not _frame_hierarchy_showing(doc):
        return False
    tab = _selected_tab_name(doc)
    if not tab:
        return True
    owner = _enclosing_tab_document(doc)
    return _names_match_tab(_node_name(owner), tab)


def _has_browser_frame(node, depth: int = 0) -> bool:
    """True when ``node`` is or directly wraps a browser document frame."""
    if node is None or depth > 2:
        return False
    role = _role_name(node)
    if role in {"internal frame", "document web"}:
        return True
    count = min(_child_count(node), 6)
    for index in range(count):
        if _has_browser_frame(_child_at(node, index), depth + 1):
            return True
    return False


def _hidden_gecko_browser(node) -> bool:
    """True when ``node`` is a Firefox document the user is not looking at.

    A scroll pane that is not SHOWING and wraps an internal frame is a
    background tab or the preloaded New Tab browser. An internal frame
    without SHOWING is the same. A document web is hidden when its frame
    hierarchy is not SHOWING, or when the tab that encloses it is not the
    selected tab. An iframe document whose title differs from the tab is
    not hidden for that reason.
    """
    role = _role_name(node)
    if role == "scroll pane":
        if _state_has(node, "SHOWING"):
            return False
        return _has_browser_frame(node)
    if role == "internal frame":
        if _state_has(node, "SHOWING"):
            return False
        # A frame inside the showing page is an iframe, not a background tab.
        return not _nested_in_document(node)
    if role == "document web":
        return not _gecko_web_document_on_screen(node)
    return False


def accessible_gone(acc) -> bool:
    """True when ``acc`` is missing or Firefox has destroyed it.

    A background-tab node is still alive: it has a role and is not DEFUNCT.
    A node removed from the document is DEFUNCT, or it no longer answers
    with a role. That one is gone, not merely hidden.
    """
    if acc is None:
        return True
    try:
        if _state_has(acc, "DEFUNCT"):
            return True
    except Exception:
        return True
    try:
        role = _role_name(acc)
    except Exception:
        return True
    return not bool(role)


# A disposed JOptionPane button stays in the tree without SHOWING. A table
# cell that is merely scrolled off is the same missing bit and is not gone.
_JAVA_DIALOG_CONTROL_ROLES = frozenset({
    "push button", "toggle button", "check box", "radio button",
})


def target_vanished(acc) -> bool:
    """True when a click or type must not claim the accessible is still there.

    DEFUNCT is gone on every toolkit. A handle that never reported a role
    is not this case. Java ATK keeps a disposed dialog button in the tree
    without SHOWING; a button whose state set was reported and does not
    include SHOWING is that control. A table cell without SHOWING is still
    in the table. GTK and a fake with no toolkit are not refused for a
    missing SHOWING bit. An empty state set is not evidence.
    """
    if acc is None:
        return False
    try:
        java = _java_app(acc)
    except Exception:
        java = False
    try:
        # Java needs a fresh DEFUNCT bit. Every other toolkit keeps the
        # cached state: dropping it here clears a Chrome editor's text.
        if _state_flag(acc, "DEFUNCT", drop_cache=java):
            return True
    except Exception:
        return True
    if not java:
        return False
    try:
        role = _role_name(acc)
    except Exception:
        return False
    if role not in _JAVA_DIALOG_CONTROL_ROLES:
        return False
    names = _state_names(acc)
    if not names:
        return False
    return "SHOWING" not in names


def _misses_primary_screen(pos, size) -> bool:
    """True when a SCREEN box has no overlap with the primary display.

    The pruner drops that box, so a scrolled-off control leaves the snapshot
    while its accessible is still alive. A partial overlap stays.
    """
    x, y = float(pos[0]), float(pos[1])
    width, height = float(size[0]), float(size[1])
    if width <= 0 or height <= 0:
        return True
    screen_w, screen_h = _screen_size()
    right = min(x + width, float(screen_w))
    bottom = min(y + height, float(screen_h))
    return right <= max(x, 0.0) or bottom <= max(y, 0.0)


def offscreen_extents(acc):
    """SCREEN box of an alive accessible that misses the display.

    ``None`` when the node is gone, sits in a hidden Firefox document, or
    its box still intersects the primary screen. A scrolled-off button is
    this case: not DEFUNCT, and the pruner dropped it because the box misses
    every display. A control that is still on screen is not, even when its
    title no longer matches. A node with no box is off-screen only when it
    is also not SHOWING; a SHOWING node whose component failed to answer
    stays ``None`` so a real stale ref is not reported as off-screen.
    """
    if accessible_gone(acc) or hidden_web_target(acc):
        return None
    pos, size = _extents(acc)
    if pos is None or size is None:
        if _state_has(acc, "SHOWING"):
            return None
        return (0.0, 0.0), (0.0, 0.0)
    if not _misses_primary_screen(pos, size):
        return None
    return pos, size


def _chromium_document_hidden(acc) -> bool:
    """True when ``acc`` sits in a Chromium document that is not the selected tab.

    The active tab's document is SHOWING and its title is the selected page
    tab. Chrome also drops a background tab's document out of the frame and
    leaves STATE_SHOWING set on that orphan, so a missing frame ancestor is
    the same case. The tab strip sits outside the document, so a click on the
    tab itself is not this case. An element scrolled out of the showing
    document is not this case either: that document is still the selected tab.

    Parents are visited through `_ancestor_chain`. A seen-set of ``id()``
    stops early when a later panel is allocated at the address of a wrapper
    the walk has already dropped, which is the Electron tree between the
    document and the frame.
    """
    document = None
    reached_frame = False
    for node in _ancestor_chain(acc, 32):
        role = _role_name(node)
        if role in {"document web", "document frame"}:
            document = node
            if not _state_has(node, "SHOWING"):
                return True
        if role in {"frame", "window", "application"}:
            reached_frame = True
            break
    if document is None:
        return False
    if not reached_frame:
        return True
    tab = _selected_tab_name(document)
    title = _node_name(document)
    if tab and title and not _names_match_tab(title, tab):
        return True
    return False


def hidden_named_target(root, ax_role: str, title: str) -> bool:
    """True when a live node of ``ax_role`` and ``title`` sits in a hidden document.

    The snapshot prunes that document, so a ref into it fails to rematch and
    would otherwise be reported as stale. A match that is showing is not this
    case: the element really left the tree the ref was issued against. A
    DEFUNCT node is skipped. GTK trees are not walked. Firefox and Chromium are.
    """
    if root is None or not ax_role or not title or not (_gecko_app(root) or _chromium_app(root)):
        return False
    hidden = False
    showing = False
    queue = [root]
    seen: set[int] = set()
    examined = 0
    while queue and examined < 400:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        examined += 1
        if accessible_gone(node):
            continue
        mapped = _ROLE.get(_role_name(node), "")
        if mapped == ax_role and _node_name(node) == title:
            if hidden_web_target(node):
                hidden = True
            else:
                showing = True
            if hidden and showing:
                break
        count = min(_child_count(node), 40)
        for index in range(count):
            queue.append(_child_at(node, index))
    return hidden and not showing


def chromium_toggle_state(acc) -> tuple[bool, bool] | None:
    """Fresh checked/pressed bits, or None when a page toggle is unreadable.

    Unlike general observation flags, missing states must not become False:
    that would turn a failed read after a click into evidence of unchecking.
    """
    if acc is None or _role_name(acc) not in {"check box", "radio button", "toggle button"}:
        return None
    if not _chromium_app(acc):
        return None
    for node in _ancestor_chain(acc):
        role = _role_name(node)
        if role in {"document web", "document frame"}:
            break
        if role in {"frame", "window", "application"}:
            return None
    else:
        return None
    return _click_state_bits(acc, ("CHECKED", "PRESSED"))


def gtk_dialog_toggle_state(acc) -> tuple[bool, bool] | None:
    """Fresh selection/check state of a visible GTK dialog control only."""
    role = _role_name(acc) if acc is not None else ""
    if role not in {"page tab", "check box", "radio button"}:
        return None
    if _toolkit_name(acc) != "gtk":
        return None
    for node in _ancestor_chain(acc):
        ancestor_role = _role_name(node)
        if ancestor_role == "dialog":
            break
        if ancestor_role in {"frame", "window", "application", "document web", "document frame"}:
            return None
    else:
        return None
    bits = ("SELECTED", "CHECKED") if role == "page tab" else ("CHECKED", "PRESSED")
    return _click_state_bits(acc, bits, require_showing=True)


def _click_state_bits(acc, bits: tuple[str, str], *, require_showing: bool = False):
    """Read native bits without treating a failed state read as False."""
    try:
        _drop_cached_properties(acc)
        states = acc.get_state_set()
        if states is None:
            return None
        names = getattr(states, "names", None)
        if isinstance(names, (set, frozenset, list, tuple)):
            names = {str(name).upper() for name in names}
            has = lambda name: name in names
        else:
            state_type = _atspi().StateType
            has = lambda name: bool(states.contains(getattr(state_type, name)))
        if has("DEFUNCT"):
            return None
        if require_showing and not has("SHOWING"):
            return None
        return tuple(has(bit) for bit in bits)
    except Exception:
        return None


def chromium_page_button(acc) -> bool:
    """Whether this is a native Chromium push button inside a web document."""
    # AXButton also represents native toggle buttons. Keep those on the
    # existing click path; their advertised toggle is not a click/press action.
    if acc is None or _role_name(acc) not in {"button", "push button"} or not _chromium_app(acc):
        return False
    for node in _ancestor_chain(acc):
        role = _role_name(node)
        if role in {"document web", "document frame"}:
            return True
        if role in {"frame", "window", "application"}:
            break
    return False


def chromium_button_click_action(acc):
    """Return one exact click/press action; never a toggle, menu, or focus action."""
    action = _action_iface(acc)
    if action is None:
        return None
    count = _call_first(action, ("get_n_actions", "get_nActions"), default=0) or 0
    for index in range(int(count)):
        name = (_call_first(action, ("get_action_name", "get_name"), index, default="") or "").lower()
        if name in {"click", "press"}:
            return action, index, name
    return None


def notification_window(acc):
    """Capture the notification owner before activation can outlive the bubble."""
    for node in _ancestor_chain(acc):
        role = _role_name(node)
        if role == "notification":
            return node
        if role in {"frame", "window", "dialog"}:
            return node if _application_name(acc).casefold() == "xfce4-notifyd" else None
    return None


def notification_pointer_target(acc, window, x: int, y: int) -> bool:
    """Require fresh visibility, geometry and identity for a bubble pointer click.

    Keep the original window even if expiry severs the target's parent link.
    Missing native replies are refusal, never evidence that old pixels are safe.
    """
    try:
        for node in (window, acc):
            _drop_cached_properties(node)
            states = _call_first(node, ("get_state_set",))
            if (states is None
                    or _state_flag(node, "DEFUNCT", drop_cache=False, states=states)
                    or not _state_flag(node, "SHOWING", drop_cache=False, states=states)):
                return False
            pos, size = _extents(node)
            if (pos is None or size is None
                    or not (pos[0] <= x < pos[0] + size[0]
                            and pos[1] <= y < pos[1] + size[1])):
                return False
        return fallback_pointer_target(acc, x, y)
    except Exception:
        return False


def fallback_pointer_target(acc, x: int, y: int) -> bool:
    """Hit-test a ref from its owning popup/window, never from the target itself.

    Qt popups are not hit-testable through the desktop or their owner's frame.
    Require the popup itself to remain showing, and descend only native hits.
    Missing hits and cycles are refusals, not evidence from overlapping bounds.
    """
    root = next((node for node in _ancestor_chain(acc)
                 if _role_name(node) in {"popup menu", "menu", "frame", "window", "dialog", "notification"}), None)
    if root is None or root is acc or not _state_has(root, "SHOWING"):
        return False
    target_key = _ancestor_key(acc)
    node = root
    seen: set[object] = set()
    pinned = []
    for _ in range(32):
        key = _ancestor_key(node)
        if key in seen:
            return False
        seen.add(key)
        pinned.append(node)
        comp = _component(node)
        if comp is None:
            return False
        hit = _hit_at(comp, x, y)
        if hit is None:
            return False
        if _ancestor_key(hit) == target_key:
            return True
        node = hit
    return False


def chromium_pointer_target(acc, x: int, y: int) -> bool | None:
    """Verify a Chromium page ref against the document's native hit test.

    None means this is not Chromium page content. False includes unavailable
    hit tests: extents and SHOWING can both remain stale after CSS transforms.
    Compare native identity, never names or overlapping rectangles. A text
    descendant of the requested control is a valid hit.
    """
    if acc is None or not _chromium_app(acc):
        return None
    document = None
    for node in _ancestor_chain(acc):
        role = _role_name(node)
        if role in {"document web", "document frame"}:
            document = node
            break
        if role in {"frame", "window", "application"}:
            break
    if document is None:
        return None
    target_key = _ancestor_key(acc)
    node = document
    seen: set[object] = set()
    pinned = []
    for _ in range(32):
        key = _ancestor_key(node)
        if key in seen:
            return False
        seen.add(key)
        pinned.append(node)
        comp = _component(node)
        if comp is None:
            return False
        hit = _hit_at(comp, x, y)
        if hit is None:
            return False
        if any(_ancestor_key(parent) == target_key for parent in _ancestor_chain(hit)):
            return True
        if _chromium_select_option_at_point(acc, hit, x, y):
            return True
        node = hit
    return False


def chromium_clipped_target(acc) -> bool:
    """A VISIBLE Chromium page node whose native state says it is not painted.

    CSS overflow clipping can leave plausible on-screen bounds even when the
    document and containing region are SHOWING. Empty state sets and browser
    chrome are not evidence of this condition.
    """
    if acc is None or not _chromium_app(acc):
        return False
    states = _state_names(acc)
    if "VISIBLE" not in states or "SHOWING" in states:
        return False
    # Scrolling also clears SHOWING. Preserve the ordinary off-screen
    # pruning/expand path and NOT_VISIBLE stale-ref diagnosis in that case.
    try:
        pos, size = _extents(acc)
    except ImportError:
        pos, size = None, None
    if pos is not None and size is not None and _misses_primary_screen(pos, size):
        return False
    for node in _ancestor_chain(acc, 32):
        role = _role_name(node)
        if role in {"document web", "document frame"}:
            return _state_has(node, "SHOWING")
        if role in {"frame", "window", "application"}:
            break
    return False


def hidden_web_target(acc) -> bool:
    """True for hidden browser documents or clipped Chromium page content.

    False for GTK, and for browser chrome (the tab strip, the toolbar).
    A click on a link in a background tab is this case: the link's document
    is not the selected, showing one. Firefox and Chromium both keep that
    document alive.
    """
    if acc is None:
        return False
    if _chromium_app(acc) and not _gecko_app(acc):
        return chromium_clipped_target(acc) or _chromium_document_hidden(acc)
    if not _gecko_app(acc):
        return False
    node = acc
    seen: set[int] = set()
    for _ in range(32):
        if node is None or id(node) in seen:
            return False
        seen.add(id(node))
        role = _role_name(node)
        if role == "document web":
            return not _gecko_web_document_on_screen(node)
        if role == "internal frame" and not _state_has(node, "SHOWING"):
            return True
        parent = _parent_of(node)
        if parent is None or parent is node:
            break
        node = parent
    return False


def _list_containers(root, limit: int = _MAX_LIST_CONTAINERS) -> list:
    """List, table, and tree nodes under ``root``, bounded."""
    # Discovery precedes the snapshot walk. Chromium can answer this role
    # query in one call instead of fetching every toolbar/document child
    # twice. Keep the walk for bridges without Collection (or failed reads).
    try:
        coll = _call_first(root, ("get_collection_iface", "get_collection"))
        if coll is not None:
            Atspi = _atspi()
            mt = Atspi.CollectionMatchType
            rule = Atspi.MatchRule.new(
                Atspi.StateSet.new([]), mt.NONE, {}, mt.NONE,
                [Atspi.Role.LIST, Atspi.Role.LIST_BOX, Atspi.Role.TABLE,
                 Atspi.Role.TREE, Atspi.Role.TREE_TABLE],
                mt.ANY, [], mt.NONE, False,
            )
            hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, limit, True)
            if hits is not None:
                roots = ([root] if _role_name(root) in _LIST_ROLES
                         and _extents(root)[0] is not None else [])
                return (roots + [hit for hit in hits if _extents(hit)[0] is not None])[:limit]
    except Exception:
        pass
    found: list = []
    queue = [root]
    seen: set[int] = set()
    while queue and len(found) < limit and len(seen) < 80:
        node = queue.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if _role_name(node) in _LIST_ROLES and _extents(node)[0] is not None:
            found.append(node)
        count = min(_child_count(node), 30)
        for index in range(count):
            queue.append(_child_at(node, index))
    return found


def _point_in_extents(acc, x: int, y: int) -> bool | None:
    """Whether ``acc``'s own screen box covers ``(x, y)``.

    None when the accessible has no box. False when it has a box and the
    point is outside it. Covering the point is not enough: the 0.4.15 retest
    still published ITEM-001 at y=-2, and a row that tall still covers a
    sample inside the list. ``_top_above_line`` is what drops that row.
    """
    pos, size = _extents(acc)
    if pos is None or size is None:
        return None
    left, top = pos
    width, height = size
    if width <= 0 or height <= 0:
        return False
    return (left - 1) <= x <= (left + width + 1) and (top - 1) <= y <= (top + height + 1)


def _clip_sliver(height: float) -> float:
    """Pixels of clipped row at the top of a list that are not a head.

    8 when the list is tall enough to have an edge distinct from its rows.
    1 when the box is only a few pixels tall.
    """
    if height > _CLIP_SLIVER_PX * 2:
        return float(_CLIP_SLIVER_PX)
    return 1.0


# Chromium's page, as opposed to the browser chrome around it. The 0.4.21
# retest's document group was 1271 by 709 on an 800px screen, so about three
# rows sit between the screen top and the page. Those rows are not painted.
_PAGE_ANCESTOR_ROLES = frozenset({"document web", "document frame", "html container"})


def _page_edges(container) -> tuple[float, float, float, float] | None:
    """Screen box of the document that contains ``container``, if it has one.

    None when no document ancestor has a box. The nearest document wins.
    """
    node = _parent_of(container)
    seen: set[int] = set()
    for _ in range(8):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if _role_name(node) in _PAGE_ANCESTOR_ROLES:
            pos, size = _extents(node)
            if pos is None or size is None or size[1] < 4 or size[0] < 1:
                return None
            left = float(pos[0])
            top = float(pos[1])
            return left, top, left + float(size[0]), top + float(size[1])
        node = _parent_of(node)
    return None


def _intersect_edges(
    box: tuple[float, float, float, float],
    other: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """The overlap of two ``(left, top, right, bottom)`` boxes, or None."""
    left = max(box[0], other[0])
    top = max(box[1], other[1])
    right = min(box[2], other[2])
    bottom = min(box[3], other[3])
    if right - left < 1 or bottom - top < 4:
        return None
    return left, top, right, bottom


# Ancestors that are not an overflow wrapper around a list. The document
# clip is separate: treating it as the wrapper would step a body-scroll
# page with the overflow bar. The frame is the window.
_SCROLLPORT_STOP_ROLES = _PAGE_ANCESTOR_ROLES | frozenset({
    "frame", "window", "application", "dialog", "alert",
})


def _overflow_ancestor_edges(container) -> tuple[float, float, float, float] | None:
    """Screen box of the overflow wrapper around ``container``, or None.

    A plain list inside ``overflow:auto`` does not scroll. The wrapper is
    shorter than the list, and the list extends outside it. Rows in that
    band are hidden by the wrapper. The nearest such ancestor wins. The
    document and the frame are not it. None when the list is its own
    scrollport, which is the fixed-height overflow list.
    """
    pos, size = _extents(container)
    if pos is None or size is None:
        return None
    list_left = float(pos[0])
    list_top = float(pos[1])
    list_width = float(size[0])
    list_height = float(size[1])
    list_right = list_left + list_width
    list_bottom = list_top + list_height
    node = _parent_of(container)
    seen: set[int] = set()
    for _ in range(8):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if _role_name(node) in _SCROLLPORT_STOP_ROLES:
            return None
        ppos, psize = _extents(node)
        node = _parent_of(node)
        if ppos is None or psize is None:
            continue
        width = float(psize[0])
        height = float(psize[1])
        if width < 4 or height < 4 or height >= list_height - 8:
            continue
        left = float(ppos[0])
        top = float(ppos[1])
        right = left + width
        bottom = top + height
        if list_top >= top - 1 and list_bottom <= bottom + 1:
            continue
        overlap = min(right, list_right) - max(left, list_left)
        if overlap < min(width, list_width) * 0.5:
            continue
        return left, top, right, bottom
    return None


def list_scrolls_with_document(container) -> bool:
    """True when a Chromium list is body content and the document scrolls.

    A short table can be a little taller than an 800px screen and still
    be body content. Its box is not a scrollport. After the document
    moves, the top sits above the page or the bottom still runs past it.
    The list bar does not move that table, and a wheel judged as a
    scrollport then reports the page unchanged and the search turns
    around before the last row. A list more than twice the screen is a
    virtualized viewport and keeps the list wheel. A fixed-height
    overflow list sits inside the document. No document edges means the
    caller keeps the list's own bar.
    """
    if container is None or _role_name(container) not in _LIST_ROLES:
        return False
    if not _chromium_app(container):
        return False
    if _overflow_ancestor_edges(container) is not None:
        return False
    pos, size = _extents(container)
    if pos is None or size is None or float(size[1]) < 4:
        return False
    page = _page_edges(container)
    if page is None:
        return False
    top = float(pos[1])
    bottom = top + float(size[1])
    if bottom <= page[3] + 8 and top >= page[1] - 8:
        return False
    _sw, sh = _screen_size()
    if sh > 0 and float(size[1]) > sh * 2:
        return False
    return True


def _page_node(container):
    """The document accessible that contains ``container``, or None."""
    node = _parent_of(container)
    seen: set[int] = set()
    for _ in range(8):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if _role_name(node) in _PAGE_ANCESTOR_ROLES:
            pos, size = _extents(node)
            if pos is None or size is None or size[1] < 4 or size[0] < 1:
                return None
            return node
        node = _parent_of(node)
    return None


def scroll_container_kind(container) -> str:
    """How ``container`` scrolls: ``document``, ``overflow``, or ``viewport``.

    ``document`` is body content: a document ancestor is the scroll
    container, not the list. ``overflow`` is a shorter wrapper around the
    list. ``viewport`` is a list that is its own scrollport. The document
    is not assumed for every table.
    """
    if container is None:
        return "viewport"
    if _overflow_ancestor_edges(container) is not None:
        return "overflow"
    if list_scrolls_with_document(container):
        return "document"
    return "viewport"


def scroll_container(acc):
    """The scrollable ancestor of ``acc``.

    Returns ``(node, kind)``. ``kind`` is ``document``, ``overflow``, or
    ``viewport``. The node is the document when the target is body content,
    and the list when an overflow wrapper or the list itself scrolls.
    ``(None, None)`` when ``acc`` is not inside a Chromium list or document.
    """
    if acc is None:
        return None, None
    if _role_name(acc) in _PAGE_ANCESTOR_ROLES and _chromium_app(acc):
        return acc, "document"
    container = list_container(acc)
    if container is None:
        container = list_with_overflow_ancestor(acc)
    if container is None:
        return None, None
    kind = scroll_container_kind(container)
    if kind == "document":
        page = _page_node(container)
        return (page if page is not None else container), kind
    return container, kind


def scroll_offset(acc) -> float | None:
    """Vertical scroll offset of the container that scrolls ``acc``.

    The current value of that container's vertical bar. None when no bar
    can be read, so a caller cannot claim the page was unchanged.
    """
    if acc is None:
        return None
    node, _kind = scroll_container(acc)
    if node is None:
        node = acc
    seen: set[int] = set()
    current = node
    for _ in range(8):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        bar = _viewport_vertical_bar(current)
        if bar is not None:
            found = _bar_range(bar)
            if found is not None:
                return float(found[0])
        role = _role_name(current)
        if role in ("frame", "window", "application"):
            return None
        current = _parent_of(current)
    return None


def overflow_ancestor_box(container) -> tuple[int, int, int, int] | None:
    """``(x, y, width, height)`` of the overflow wrapper, or None.

    See ``_overflow_ancestor_edges``. The caller photographs this box and
    steps the list it scrolls. A body-scroll document is not this box.
    """
    edges = _overflow_ancestor_edges(container)
    if edges is None:
        return None
    left, top, right, bottom = edges
    if right - left < 1 or bottom - top < 4:
        return None
    return int(left), int(top), int(right - left), int(bottom - top)


def list_with_overflow_ancestor(root):
    """A Chromium list under ``root`` that an ancestor wrapper scrolls.

    None when no list has that wrapper, so a body-scroll document keeps
    the wheel. The first list in the bounded walk wins. A fixed-height
    overflow list whose own box is the scrollport is not returned: its
    parent is the document, and that is not this wrapper.
    """
    if root is None or not _chromium_app(root):
        return None
    for container in _list_containers(root):
        if _overflow_ancestor_edges(container) is not None:
            return container
    return None


def _viewport_edges(container) -> tuple[float, float, float, float] | None:
    """Edges of the painted page inside a list: left, top, right, bottom.

    The head line is this top. An overflow list that already sits on the
    page keeps its own top, so a row flush with that top stays the head and
    a row above the list stays out. A list inside a shorter ancestor is
    clipped to that wrapper first, so a row the wrapper hides is not the
    head. A content-height list is clipped to the document, then to the
    screen. The document starts below the screen top (the browser chrome).
    Rows in that band are on the screen and are not painted; they are not
    the head. A list with no document ancestor is clipped to the screen only.
    """
    pos, size = _extents(container)
    if pos is None or size is None or size[1] < 4 or size[0] < 1:
        return None
    left = float(pos[0])
    top = float(pos[1])
    right = left + float(size[0])
    bottom = top + float(size[1])
    ancestor = _overflow_ancestor_edges(container)
    if ancestor is not None:
        clipped = _intersect_edges((left, top, right, bottom), ancestor)
        if clipped is not None:
            left, top, right, bottom = clipped
    page = _page_edges(container)
    if page is not None:
        clipped = _intersect_edges((left, top, right, bottom), page)
        if clipped is not None:
            left, top, right, bottom = clipped
    sw, sh = _screen_size()
    if sw <= 0 or sh <= 0:
        return left, top, right, bottom
    screen = _intersect_edges((left, top, right, bottom), (0.0, 0.0, float(sw), float(sh)))
    if screen is None:
        return left, top, right, bottom
    return screen


def _head_line(container) -> float | None:
    """Y of the on-screen top of the list.

    A row whose own top is above this edge is outside the viewport. When the
    list's own top is on the screen, that top is the edge: the 8px sliver is
    not added. 0.4.17 and 0.4.18 added it, and a fully visible row flush with
    the list then failed the check, so the snapshot started at the next row.
    When the list's own top is above the page, the edge is the document top,
    not the screen top. Rows between those two lines are browser chrome.
    """
    edges = _viewport_edges(container)
    if edges is None:
        return None
    return edges[1]


def list_wheel_point(container) -> tuple[int, int] | None:
    """Where a line-scroll wheel lands on a Chromium list, when a wheel is sent.

    The center of the first painted row, clamped into the painted page.
    None when ``container`` is not a Chromium list, so a document group keeps
    its own wheel point. No hit test: the row walk is enough, and a list
    with no named rows is wheeled just below its clipped top edge. The
    0.4.22 overflow list was wheeled here and did not move; a viewport list
    uses ``scroll_viewport_by_lines`` before this point.
    """
    if _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return None
    edges = _viewport_edges(container)
    if edges is None:
        return None
    left, top, right, bottom = edges
    rows = _in_view_named_rows(container)
    if rows:
        _acc, (x, y), (width, height) = rows[0]
        cx = int(x + width / 2)
        cy = int(y + height / 2)
    else:
        cx = int((left + right) / 2)
        cy = int(top + _clip_sliver(bottom - top) + 1)
    cx = min(max(cx, int(left)), max(int(left), int(right) - 1))
    cy = min(max(cy, int(top)), max(int(top), int(bottom) - 1))
    return cx, cy


def _top_above_line(acc, line: float) -> bool:
    """True when ``acc``'s own top is above ``line``.

    False when the accessible has no box. Callers pass the list's top edge.
    A row at y=-2 is above a list at y=100, including when its height still
    covers a sample inside the list. The 0.4.15 check kept that row because
    the box covered the point. A row whose top is the list's top is not
    above the line.
    """
    pos, size = _extents(acc)
    if pos is None or size is None:
        return False
    return float(pos[1]) < line - 1


def _on_screen_row(top: float, bottom: float, box_y: float, box_b: float, sliver: float) -> bool:
    """True when a row is on screen inside the list, not the clipped edge.

    The row's own top is on or below the list's top, within 1px, and its
    bottom is below the top sliver. A fully visible row that starts on the
    list's top is included. A box that only fills that sliver is the clipped
    edge. A row whose top is above the list is the parked row (y=-2 on the
    0.4.16 retest) and is not included.
    """
    if top < box_y - 1 or top > box_b:
        return False
    return min(bottom, box_b) > box_y + sliver


def _vertical_span(acc) -> tuple[float, float] | None:
    """``(top, bottom)`` of ``acc``, or None when it has no positive box."""
    pos, size = _extents(acc)
    if pos is None or size is None:
        return None
    width = float(size[0])
    height = float(size[1])
    if width <= 0 or height <= 0:
        return None
    top = float(pos[1])
    return top, top + height


def _child_vertical_span(node, index: int) -> tuple[float, float] | None:
    """Span of child ``index``, skipping scroll bars and separators.

    None when that child is missing, has no box, or is not a row.
    """
    child = _child_at(node, index)
    if child is None or _role_name(child) in _SKIP_ROW_ROLES:
        return None
    return _vertical_span(child)


def _first_index_reaching(node, line: float, count: int) -> int:
    """First child index that is not entirely above ``line``.

    0 when ``count`` fits in ``_VISIBLE_WALK_CAP``, when the ends have no
    box or are not top-to-bottom, or when child 0 already reaches the
    line. A short list keeps the walk that starts at child 0. On the
    0.4.31 live list a 520px scroller held 2000 ``list item`` children.
    With ITEM-0201 at the top of the box the walk spent the cap on the
    rows above the viewport, so the snapshot stopped at ITEM-0224. With
    ITEM-0226 at the top it stopped at ITEM-0237. Past child 250 it
    listed nothing. The cap still bounds how many children are read
    after this index. A run that is not top-to-bottom is not skipped.
    """
    if count <= 0 or count <= _VISIBLE_WALK_CAP:
        return 0
    first = _child_vertical_span(node, 0)
    last_index = count - 1
    last = _child_vertical_span(node, last_index)
    if last is None:
        for index in range(count - 2, max(-1, count - 6), -1):
            last = _child_vertical_span(node, index)
            if last is not None:
                last_index = index
                break
    if first is None or last is None or last[0] < first[0] - 1:
        return 0
    # ``bottom < line - 1`` is the walk's "entirely above" test.
    if first[1] >= line - 1:
        return 0
    lo = 0
    hi = last_index
    while lo < hi:
        mid = (lo + hi) // 2
        span = _child_vertical_span(node, mid)
        if span is None or span[0] < first[0] - 1:
            return 0
        if span[1] < line - 1:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _child_scan_range(node, line: float, limit: int) -> range:
    """Child indexes to read, at most ``limit``, from the first that reaches ``line``.

    ``limit`` is the remaining node budget. A short child list starts at
    0, which is the previous walk.
    """
    count = _child_count(node)
    if count <= 0 or limit <= 0:
        return range(0)
    start = _first_index_reaching(node, line, count)
    return range(start, min(count, start + limit))


def _content_ends_span(container) -> float | None:
    """Distance from the first child's top to the last row's top.

    None when an end has no box or the run is not top-to-bottom. A scroll
    bar at the end is not the last row. The on-screen window is not this
    distance: a fractional bar scaled by that window steps past the rows
    in between.
    """
    count = _child_count(container)
    if count < 2:
        return None
    first = _child_vertical_span(container, 0)
    last = None
    for index in range(count - 1, max(-1, count - 6), -1):
        last = _child_vertical_span(container, index)
        if last is not None:
            break
    if first is None or last is None or last[0] < first[0] - 1:
        return None
    return last[0] - first[0]


def _settle_hit(comp, x: int, y: int, coord):
    """The accessible at ``(x, y)`` that covers that point.

    Chromium's first answer can be the pre-scroll row. A row that does not
    cover ``(x, y)`` is ignored. A row that covers the point can still start
    above the list; the probe drops that one. Two covering answers with the
    same name are the result. Python identity is not the check: a new
    wrapper for the same row is still that row.
    """
    previous_name = None
    for attempt in range(_HIT_TRIES):
        hit = _call_first(
            comp, ("get_accessible_at_point", "getAccessibleAtPoint"), x, y, coord
        )
        if hit is not None and _point_in_extents(hit, x, y) is False:
            hit = None
        if hit is not None:
            name = _node_name(hit)
            if previous_name is not None and name and name == previous_name:
                return hit
            previous_name = name
        if attempt + 1 < _HIT_TRIES:
            time.sleep(_HIT_PAUSE_S)
    return None


def _expandable_tree_item(acc) -> bool:
    """A treeitem row, including one whose box is as tall as the tree.

    Chrome sizes an expanded ``role=treeitem`` to the tree once its children
    are showing. The visible-row walk treats a box that tall as a wrapper and
    records the inner static text instead, so the row leaves the snapshot and
    nothing remains that can collapse it. The treeitem stays the row.
    """
    if acc is None or _role_name(acc) != "tree item":
        return False
    if _state_has(acc, "EXPANDABLE") or _state_has(acc, "EXPANDED"):
        return True
    return "treeitem" in _xml_roles(acc)


def _in_view_named_rows(container) -> list[tuple]:
    """Named rows under ``container`` that are on screen in the list.

    Each entry is ``(accessible, (x, y), (width, height))``. Every on-screen
    row is included. ``_MAX_ROW_SAMPLES`` is not applied here: it only
    limits hit-test points, and stopping this walk at 16 left painted rows
    out of the snapshot. The walk reads ``container`` itself. A row entirely
    above the list is skipped and the scan continues, including past the
    first forty such rows. A wrapper whose top is above the list is opened
    when its box still covers the list, which is where the on-screen rows
    sit. A node tall enough to be that wrapper is not itself a row. A row
    is on screen when its own top is on or below the list's top and it
    extends below the clipped edge. The rectangle is the node's own box
    clipped to the on-screen part of the list, so the snapshot y is not the
    pre-scroll y. A row at the content origin of a list whose top is above
    the screen is not on screen. Nodes examined are still bounded by
    ``_VISIBLE_WALK_CAP``, counted from the first child whose box reaches
    the viewport. A list longer than that cap is not read from child 0:
    the rows above the viewport would use up the cap, and the on-screen
    rows past it would be missing. A run that is not top-to-bottom is
    still read from child 0.
    """
    if _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return []
    # libatspi answers the child walk from the cache filled before the
    # scroll. An upward step then still lists the lower rows, the head
    # looks unchanged, and the line step is undone in favour of a track
    # click that jumps over the row being sought.
    _invalidate_tree_cache(container)
    pos, size = _extents(container)
    edges = _viewport_edges(container)
    if pos is None or size is None or edges is None:
        return []
    box_x, box_y, box_r, box_b = edges
    full_h = float(size[1])
    sliver = _clip_sliver(box_b - box_y)
    found: list[tuple] = []
    seen = 0

    def walk(node, depth: int) -> None:
        nonlocal seen
        if node is None or node is container or seen >= _VISIBLE_WALK_CAP:
            return
        seen += 1
        npos, nsize = _extents(node)
        if npos is None or nsize is None:
            if depth > 0:
                for index in _child_scan_range(node, box_y, _VISIBLE_WALK_CAP - seen):
                    if seen >= _VISIBLE_WALK_CAP:
                        break
                    walk(_child_at(node, index), depth - 1)
            return
        top = float(npos[1])
        height = float(nsize[1])
        left = float(npos[0])
        width = float(nsize[0])
        if width <= 0 or height <= 0:
            return
        bottom = top + height
        if bottom < box_y - 1 or top > box_b or left + width < box_x or left > box_r:
            return
        role = _role_name(node)
        # An expanded treeitem fills the tree. It is still the row.
        spans = height > full_h * 0.9 and not _expandable_tree_item(node)
        in_view = _on_screen_row(top, bottom, box_y, box_b, sliver)
        name = _node_name(node)
        if name and _is_list_marker(name):
            if depth > 0 and top < box_b:
                for index in _child_scan_range(node, box_y, _VISIBLE_WALK_CAP - seen):
                    if seen >= _VISIBLE_WALK_CAP:
                        break
                    walk(_child_at(node, index), depth - 1)
            return
        if (
            in_view
            and name
            and not spans
            and role not in _SKIP_ROW_ROLES
            and role not in _LIST_ROLES
        ):
            row_top = max(top, box_y)
            row_left = max(left, box_x)
            row_height = max(1.0, min(height, box_b - row_top))
            row_width = max(1.0, min(width, box_r - row_left))
            found.append((node, (row_left, row_top), (row_width, row_height)))
            return
        if depth > 0 and top < box_b:
            for index in _child_scan_range(node, box_y, _VISIBLE_WALK_CAP - seen):
                if seen >= _VISIBLE_WALK_CAP:
                    break
                walk(_child_at(node, index), depth - 1)

    for index in _child_scan_range(container, box_y, _VISIBLE_WALK_CAP):
        child = _child_at(container, index)
        if child is None:
            continue
        cpos, csize = _extents(child)
        if (
            found
            and cpos is not None
            and csize is not None
            and float(cpos[1]) > box_b
        ):
            break
        walk(child, _VISIBLE_DESCEND)
    found.sort(key=lambda item: (item[1][1], item[1][0]))
    return found


def _row_top(acc) -> float:
    """Screen y of ``acc``, or 0 when it has no box."""
    pos, _size = _extents(acc)
    if pos is None:
        return 0.0
    return float(pos[1])


def _named_rows_in_order(container) -> list:
    """Named rows under ``container``, including those below the viewport.

    Each entry is the accessible, ordered by screen y. A recorded row is
    not opened. A scroll bar is skipped. A list marker is not a row; the
    walk continues into it for the row text. A node taller than the list is
    a wrapper and is opened. Each direct child is visited on its own, so a
    marker plus a label on an early row does not stop the walk before
    ITEM-100. This does not change which rows the snapshot lists; it only
    chooses a row for ``scroll_viewport_by_lines`` to reveal.
    A list longer than ``_ROW_SEQUENCE_CAP`` is not read from child 0.
    The sequence starts a few rows above the viewport, so a line step
    can still see the previous head, and it still stops at that cap.
    The child cache is cleared first. A line step that compares the old
    head with the new one otherwise still sees the rows from before the
    scroll, and an upward step looks like it never left that head.
    """
    _pos, size = _extents(container)
    if size is None:
        return []
    _invalidate_tree_cache(container)
    full_h = float(size[1])
    found: list = []

    def descend(node, depth: int, budget: list[int]) -> None:
        if depth <= 0:
            return
        count = _child_count(node)
        # A wrapper that holds the whole list is one child of the list.
        # Sharing this call's eight-node budget across those rows stops
        # before the viewport. Each row gets its own budget, and the
        # read starts near the viewport rather than at child 0.
        if count > _ROW_SEQUENCE_CAP:
            edges = _viewport_edges(container)
            line = edges[1] if edges is not None else 0.0
            start = _first_index_reaching(node, line, count)
            start = max(0, start - 40)
            for index in range(start, min(count, start + _ROW_SEQUENCE_CAP)):
                if len(found) >= _ROW_SEQUENCE_CAP:
                    return
                walk(_child_at(node, index), depth - 1, [8])
            return
        for index in range(count):
            if budget[0] <= 0 or len(found) >= _ROW_SEQUENCE_CAP:
                return
            walk(_child_at(node, index), depth - 1, budget)

    def walk(node, depth: int, budget: list[int]) -> None:
        if node is None or node is container or len(found) >= _ROW_SEQUENCE_CAP or budget[0] <= 0:
            return
        budget[0] -= 1
        role = _role_name(node)
        if role in _SKIP_ROW_ROLES or role in _LIST_ROLES:
            return
        npos, nsize = _extents(node)
        if npos is None or nsize is None or float(nsize[0]) <= 0 or float(nsize[1]) <= 0:
            descend(node, depth, budget)
            return
        if float(nsize[1]) > full_h * 0.9:
            descend(node, depth, budget)
            return
        name = _node_name(node)
        if not name or _is_list_marker(name):
            descend(node, depth, budget)
            return
        found.append(node)

    count = _child_count(container)
    if count <= _ROW_SEQUENCE_CAP:
        indexes = range(count)
    else:
        edges = _viewport_edges(container)
        line = edges[1] if edges is not None else 0.0
        start = _first_index_reaching(container, line, count)
        # Forty children above the viewport cover the previous head of
        # a line step. The cap still bounds the read.
        start = max(0, start - 40)
        indexes = range(start, min(count, start + _ROW_SEQUENCE_CAP))
    for index in indexes:
        if len(found) >= _ROW_SEQUENCE_CAP:
            break
        # Eight nodes cover a list item, its marker, and its label. The
        # budget is per row so row 100 is still a scroll_to target.
        walk(_child_at(container, index), _VISIBLE_DESCEND, [8])
    found.sort(key=_row_top)
    return found


def scroll_viewport_by_lines(container, dy: int) -> bool:
    """Reveal a row a few lines from the on-screen head. No wheel.

    False when ``container`` is not a Chromium list, when no further row
    exists, or when ``scroll_to`` is unavailable. True means ``scroll_to``
    was called. The caller still requires the list pixels to move. One
    line is one content row. The step is not larger than the rows already
    on screen, so a later snapshot still overlaps this one. Already being
    on the first or last row returns False and does not call ``scroll_to``.
    """
    if not int(dy) or _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return False
    ordered = _named_rows_in_order(container)
    if len(ordered) < 2:
        return False
    names = _row_names(_in_view_named_rows(container))
    if not names:
        return False
    head = names[0]
    index = next((i for i, acc in enumerate(ordered) if _node_name(acc) == head), None)
    if index is None:
        return False
    step = min(abs(int(dy)) * _ROWS_PER_LINE, max(1, len(names) - 1))
    target = index + step if int(dy) > 0 else index - step
    target = max(0, min(len(ordered) - 1, target))
    if target == index:
        return False
    return scroll_to_edge(ordered[target], "TOP_EDGE")


def _child_scrollbars(node) -> list:
    """Scroll bars that are direct children of ``node``, including late ones.

    A fixed-height list can hold every row and put the bar after them.
    ``_collect_scrollbars`` only peeks at the first children of a long
    parent, which misses that bar. This walk is only the direct children,
    capped, and it does not replace ``_collect_scrollbars``.
    """
    found: list = []
    count = min(_child_count(node), _VISIBLE_WALK_CAP)
    for index in range(count):
        child = _child_at(node, index)
        if _is_scroll_bar(child):
            found.append(child)
    return found


def _bar_range(bar) -> tuple[float, float, float] | None:
    """``(current, minimum, maximum)`` for a vertical scroll bar with a range."""
    if _bar_axis(bar) != "vertical":
        return None
    current = _read_value(bar)
    if current is None:
        return None
    minimum = _value_bound(bar, ("get_minimum_value", "getMinimumValue"), current)
    maximum = _value_bound(bar, ("get_maximum_value", "getMaximumValue"), current)
    if maximum <= minimum:
        return None
    return current, minimum, maximum


def _bar_sits_on_list(bar, origin, size) -> bool:
    """True when ``bar`` overlaps the list, or sits on its right edge.

    A sibling of the list is the overflow bar only when it lines up with
    that box. A document bar elsewhere is not.
    """
    pos, bar_size = _extents(bar)
    if pos is None or bar_size is None:
        return False
    left = float(origin[0])
    top = float(origin[1])
    right = left + float(size[0])
    bottom = top + float(size[1])
    bx = float(pos[0])
    by = float(pos[1])
    bh = float(bar_size[1])
    if by + bh < top or by > bottom:
        return False
    return left - 1 <= bx <= right + 32


def _viewport_vertical_bar(container):
    """The overflow list's vertical bar, or a sibling that sits on the list.

    None when the only bars are elsewhere. Direct children win, so a bar
    inside the list is not replaced by a document bar on the parent.
    """
    for bar in _child_scrollbars(container):
        if _bar_range(bar) is not None:
            return bar
    parent = _parent_of(container)
    if parent is None:
        return None
    origin, size = _extents(container)
    if origin is None or size is None:
        return None
    for bar in _child_scrollbars(parent):
        if _bar_range(bar) is None or not _bar_sits_on_list(bar, origin, size):
            continue
        return bar
    return None


def _viewport_bar_delta(container, dy: int, span: float) -> float | None:
    """Signed bar step for one line scroll, or None when the scale is unknown.

    One line is one content row, and not more than the rows already on
    screen. The 0.4.23 overflow rows were 28px apart, so five lines is
    five rows. When the named rows extend past the viewport,
    that extent is the scroll range: a bar on that scale (pixels) is
    stepped by the pixel distance, and a smaller bar (a fraction, or one
    unit per row) is stepped by the same distance as a fraction of the
    extent. A bar that does not show rows past the viewport is stepped
    only when its range is already in pixels. Guessing a fraction there
    would jump to the end.
    """
    rows = _in_view_named_rows(container)
    if len(rows) < 2:
        return None
    pitch = abs(float(rows[1][1][1]) - float(rows[0][1][1]))
    if pitch < 1:
        pitch = max(1.0, float(rows[0][2][1]))
    step_rows = min(abs(int(dy)) * _ROWS_PER_LINE, max(1, len(rows) - 1))
    pixel_step = step_rows * pitch
    if pixel_step <= 0 or span <= 0:
        return None
    ordered = _named_rows_in_order(container)
    if len(ordered) < 2:
        return None
    tops = [_row_top(acc) for acc in ordered]
    row_span = max(tops) - min(tops)
    # A long list's ordered window is the viewport, not the content.
    # Scaling a fractional bar by that window jumps. The ends are the
    # content height. A list that fits in the sequence cap keeps the
    # span of the rows it already collected.
    if _child_count(container) > _ROW_SEQUENCE_CAP:
        extent = _content_ends_span(container)
        if extent is not None and extent > row_span:
            row_span = extent
    _origin, size = _extents(container)
    if size is None:
        return None
    viewport_h = float(size[1])
    if row_span > viewport_h:
        if span >= row_span * 0.5:
            magnitude = pixel_step * (span / row_span)
        else:
            magnitude = pixel_step / row_span * span
    elif span >= viewport_h and span >= pixel_step:
        magnitude = pixel_step
    else:
        return None
    if magnitude <= 0:
        return None
    return magnitude if int(dy) > 0 else -magnitude


def nudge_viewport_scrollbar(container, dy: int) -> tuple[str, object]:
    """Step the overflow list's vertical bar. No wheel and no ``scroll_to``.

    Returns ``("moved", undo)`` when the value changed by the requested
    step. ``undo`` writes the previous value. ``"absent"`` means this list
    has no vertical bar, so the caller may try ``scroll_to``. ``"rejected"``
    means a bar is there and this step is not a scroll: the bar is already
    at that end, the write jumped past the request, or the bar's scale is
    not one this function will guess. A jump is undone here. The caller
    still requires the list pixels and the on-screen head to change, and
    undoes a write that does not. This does not call ``_collect_scrollbars``
    or ``_nudge_scrollbar``.
    """
    if not int(dy) or _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return "absent", None
    bar = _viewport_vertical_bar(container)
    if bar is None:
        return "absent", None
    found = _bar_range(bar)
    if found is None:
        return "rejected", None
    current, minimum, maximum = found
    span = maximum - minimum
    delta = _viewport_bar_delta(container, int(dy), span)
    if delta is None:
        return "rejected", None
    target = min(max(current + float(delta), minimum), maximum)
    expected = target - current
    floor = 1e-4 if span <= 2 else 0.5
    if abs(expected) < floor:
        return "rejected", None
    _write_value(bar, target)
    updated = _read_value(bar)
    if updated is None:
        _write_value(bar, current)
        return "rejected", None
    actual = updated - current
    tolerance = max(span * 0.02, 1e-4) if span <= 2 else max(1.0, abs(expected) * 0.05)
    jumped = abs(actual) > abs(expected) + tolerance
    wrong_way = (expected > 0 and actual < 0) or (expected < 0 and actual > 0)
    if jumped or wrong_way or abs(actual) < floor:
        _write_value(bar, current)
        return "rejected", None

    def undo() -> None:
        _write_value(bar, current)

    return "moved", undo


def viewport_track_point(container, dy: int) -> tuple[int, int] | None:
    """A point on the overflow list's vertical track. No value write.

    Downward is the lower track. Upward is the upper track. The bar's own
    box is used when that bar sits on the list. Otherwise the point is
    inside the list, in the right-hand gutter, where the track is drawn.
    The arrow buttons at the ends of a classic bar are not the point.
    None when ``container`` is not a Chromium list or has no box. The
    caller still requires the painted rows to change.
    """
    if not int(dy) or _role_name(container) not in _LIST_ROLES or not _chromium_app(container):
        return None
    box: tuple[int, int, int, int] | None = None
    bar = _viewport_vertical_bar(container)
    if bar is not None:
        pos, size = _extents(bar)
        if (
            pos is not None
            and size is not None
            and float(size[0]) >= 1
            and float(size[1]) >= 8
        ):
            box = (int(pos[0]), int(pos[1]), int(size[0]), int(size[1]))
    if box is None:
        ancestor = overflow_ancestor_box(container)
        list_box = ancestor if ancestor is not None else list_screen_box(container)
        if list_box is None:
            return None
        x, y, width, height = list_box
        if width < 16 or height < 16:
            return None
        gutter = min(14, width // 5)
        box = (x + width - gutter, y, gutter, height)
    bx, by, bw, bh = box
    margin = min(max(bh // 8, 4), max(bh // 3, 1))
    if int(dy) > 0:
        y = by + bh - margin
    else:
        y = by + margin
    y = min(max(y, by + 1), by + max(bh - 2, 1))
    x = bx + max(bw // 2, 0)
    x = min(max(x, bx), bx + max(bw - 1, 0))
    return int(x), int(y)


def _probe_visible_rows(container) -> list[tuple]:
    """Rows on screen inside ``container``.

    Each entry is ``(accessible, (x, y), (width, height))``. Children whose
    own tops are on or below the on-screen top of the list win over the hit
    test: the 0.4.15 retest kept ITEM-001 at y=-2 because that box still
    covered the sample. A hit whose top is above that edge is dropped even
    when it covers the sample. Otherwise the rectangle is the span of
    samples that hit that accessible. Samples that hit the container itself
    are skipped. Samples start below the clipped edge so that edge is not
    the head. The samples cover the on-screen part of the list, not the
    content origin: a list at y=-4847 is not sampled there.
    """
    edges = _viewport_edges(container)
    if edges is None:
        return []
    box_x, box_y, box_r, box_b = edges
    top = int(box_y)
    bottom = int(box_b)
    sliver = int(_clip_sliver(box_b - box_y))
    child_rows = _in_view_named_rows(container)
    if len(_row_names(child_rows)) >= 2:
        return child_rows
    comp = _component(container)
    if comp is None:
        return []
    coord = getattr(getattr(_atspi(), "CoordType", None), "SCREEN", 0)
    x = int((box_x + box_r) / 2)
    step = max(12, int((bottom - top) // 12) or 12)
    grouped: list[list] = []
    # Skip the clipped sliver at the top edge. A hit there is the row above
    # the on-screen head, which is what 0.4.12 published as ITEM-009.
    y = top + sliver
    samples = 0
    while y < bottom and samples < _MAX_ROW_SAMPLES:
        samples += 1
        hit = _settle_hit(comp, x, y, coord)
        if (
            hit is not None
            and hit is not container
            and not _top_above_line(hit, float(top))
        ):
            if grouped and grouped[-1][0] is hit:
                grouped[-1][2] = y + step
            else:
                grouped.append([hit, y, y + step])
        y += step
    rows = []
    for acc, y0, y1 in grouped:
        height = max(1, min(y1, bottom) - y0)
        rows.append((acc, (float(box_x), float(y0)), (float(box_r - box_x), float(height))))
    return rows


def _row_names(probed) -> tuple[str, ...]:
    names: list[str] = []
    for acc, _pos, _size in probed:
        name = _node_name(acc)
        if name and (not names or names[-1] != name):
            names.append(name)
    return tuple(names)


def _list_key(container) -> tuple | None:
    """Stable identity of a list box. Not ``id()``: a live walk wraps a new
    Python object each time, while the box itself does not move when the
    rows scroll.
    """
    pos, size = _extents(container)
    if pos is None:
        return None
    return (
        _role_name(container),
        _node_name(container),
        int(pos[0]),
        int(pos[1]),
        int(size[0]),
        int(size[1]),
    )


def reset_shown_rows() -> None:
    """Drop confirmed list rows. Tests call this so one list cannot leak
    into the next."""
    _SHOWN.clear()


def _saved_for(container) -> list | None:
    """Rows last confirmed for this list, including when its box jitters.

    The exact box is the first choice. A one-pixel change in the reported
    extents must not miss that entry and fall through to the cached
    children: after a scroll those children are the pre-scroll row parked
    above the viewport. A content-height list whose origin has moved by
    more than a few pixels is a different box: the old rows are not it.
    """
    key = _list_key(container)
    if key is None:
        return None
    found = _SHOWN.get(key)
    if found is not None:
        return found
    role, name, x, y, width, height = key
    best = None
    best_area = 0
    for saved_key, rows in _SHOWN.items():
        if len(saved_key) != 6 or saved_key[0] != role or saved_key[1] != name:
            continue
        sx, sy, sw, sh = saved_key[2], saved_key[3], saved_key[4], saved_key[5]
        if abs(sx - x) > _BOX_JITTER_PX or abs(sy - y) > _BOX_JITTER_PX:
            continue
        overlap_w = min(x + width, sx + sw) - max(x, sx)
        overlap_h = min(y + height, sy + sh) - max(y, sy)
        area = max(0, overlap_w) * max(0, overlap_h)
        if area > best_area:
            best = rows
            best_area = area
    return best


def saved_rows(container) -> list | None:
    """Rows last confirmed for ``container``, or None when nothing is saved."""
    return _saved_for(container)


def _known_head_above(container, rows) -> bool:
    """True when the first row's own top is above the list's top edge.

    False when that row has no box. A stored sample rectangle is not the
    check: the accessible's live extents are. A row flush with the list
    is not above the edge.
    """
    line = _head_line(container)
    if line is None or not rows:
        return False
    return _top_above_line(rows[0][0], line)


def commit_shown_rows(container, rows) -> None:
    """Remember ``rows`` as the on-screen contents of ``container``.

    Fewer than two named rows are not a page, and are not stored. A head
    whose own top is above the list is not stored either.
    """
    key = _list_key(container)
    if key is None or len(_row_names(rows or ())) < 2:
        return
    if _known_head_above(container, rows):
        return
    _SHOWN[key] = list(rows)


def row_head(rows) -> str | None:
    """The first named row, which is the snapshot head."""
    names = _row_names(rows or ())
    return names[0] if names else None


def shown_line_step(container, before_head: str | None, rows, dy: int) -> bool:
    """True when the head moved by at most one content row per requested line.

    The 0.4.28 retest asked for five lines. The list moved five rows, and
    the grab stayed at or under the uniform-row floor, so the driver
    treated the step as no move and clicked the track. Five rows plus that
    page is the jump of 31 on a fixed-height list and 18 on overflow:auto,
    and the next window skipped the rows in between. A move of about
    ``dy`` rows is the line step. A page is not. A hit-test name that is
    not a row within that count is not, so a still page that names a far
    row stays still.
    """
    if not int(dy) or not before_head or not rows:
        return False
    after = row_head(rows)
    if after is None or after == before_head:
        return False
    names: list[str] = []
    for acc in _named_rows_in_order(container):
        name = _node_name(acc)
        if name and (not names or names[-1] != name):
            names.append(name)
    try:
        delta = names.index(after) - names.index(before_head)
    except ValueError:
        return False
    limit = abs(int(dy)) * _ROWS_PER_LINE
    if int(dy) > 0:
        return 1 <= delta <= limit
    return -limit <= delta <= -1


def row_names(rows) -> tuple[str, ...]:
    return _row_names(rows or ())


def list_container(acc):
    """The Chromium list, table, or tree that contains ``acc``.

    ``acc`` may be that list or a row inside it. The walk uses parents.
    None when no such list is found, or when the list's application is not
    Chromium. A coordinate target has no accessible and is not checked.
    """
    if acc is None:
        return None
    node = acc
    seen: set[int] = set()
    for _ in range(8):
        if node is None or id(node) in seen:
            return None
        seen.add(id(node))
        if _role_name(node) in _LIST_ROLES and _extents(node)[0] is not None:
            return node if _chromium_app(node) else None
        node = _parent_of(node)
    return None


def list_screen_box(container) -> tuple[int, int, int, int] | None:
    """Screen box of the list widget, ``(x, y, width, height)``.

    This is the list's own extents, not a row's. None when the list has
    no box.
    """
    pos, size = _extents(container)
    if pos is None:
        return None
    return (int(pos[0]), int(pos[1]), int(size[0]), int(size[1]))


def _shown_or_probe(container) -> list:
    """Rows for ``container``.

    Rows whose own tops are on or below the list's top come first. A saved
    head is not returned ahead of them: the 0.4.16 retest kept ITEM-001 at
    y=-2 for 12 seconds after the pixels had moved. A saved window is used
    only when this list has no such rows, and only while its head is not
    above the list. A later hit test does not replace a head that is still
    inside the list. A probe whose head is above the list is not stored.
    """
    live = _in_view_named_rows(container)
    if len(_row_names(live)) >= 2 and not _known_head_above(container, live):
        key = _list_key(container)
        if key is not None:
            _SHOWN[key] = list(live)
        return live
    saved = _saved_for(container)
    if saved is not None and not _known_head_above(container, saved):
        return saved
    key = _list_key(container)
    rows = _probe_visible_rows(container)
    if _known_head_above(container, rows):
        rows = []
    if key is not None and len(_row_names(rows)) >= 2:
        _SHOWN[key] = rows
        return rows
    if saved is not None and not _known_head_above(container, saved):
        return saved
    return rows


def ensure_shown_rows(container) -> list | None:
    """Confirmed rows, probing and storing them when the list has none yet."""
    saved = saved_rows(container)
    if saved is not None and len(_row_names(saved)) >= 2:
        return saved
    commit_shown_rows(container, _probe_visible_rows(container))
    return saved_rows(container)


def wait_for_shown_rows(container, previous_head: str | None) -> list | None:
    """Rows whose head differs from ``previous_head`` on two probes in a row.

    The head is the first row whose own top is on or below the list's top
    and which extends below the clipped edge. A row above the list is not
    a head, even when its box covers the sample or it is the first cached
    child. The 0.4.16 retest moved the
    pixels and still published that child (ITEM-001 at y=-2, and ITEM-013
    at y=-26). None when no on-screen head ever leaves ``previous_head``.
    The caller does not treat that as a scroll and does not install the
    off-screen row.
    """
    last: str | None = None
    for attempt in range(_HEAD_POLLS):
        rows = _probe_visible_rows(container)
        names = _row_names(rows)
        head = names[0] if len(names) >= 2 else None
        if head is not None and head == last and head != previous_head:
            return rows
        last = head
        if attempt + 1 < _HEAD_POLLS:
            time.sleep(_HEAD_PAUSE_S)
    return None


# Descendant names collected for one line-scroll signature. Chrome's row
# label is often a nested static text, not the direct child's name. The walk
# stops at the first list of four or more names so a window full of toolbar
# buttons is not what gets compared.
_LIST_NAME_LIMIT = 48
_LIST_DEPTH = 4
_LIST_ANCESTORS = 5


def _drop_cached_properties(acc) -> None:
    """Forget one node's cached properties while the event listener is up.

    ``event_main`` answers name, state, parent, and child reads from the
    client cache. A combo's current item, a SELECTED or CHECKED flag, and a
    document's parent change on the bus without replacing the accessible, so
    the next getter would still see the previous value. ``clear_cache_single``
    clears only this object. The recursive ``clear_cache`` walks every
    descendant and stays on the snapshot-root and Firefox combo paths. A node
    without ``clear_cache_single`` is a test double and is left alone. With
    the listener off the cache is not trusted and this does nothing.
    """
    if acc is None:
        return
    method = getattr(acc, "clear_cache_single", None)
    if method is None or getattr(_reply_local, "dropping_properties", False):
        return
    from a11y_computer_use.drivers import _atspi_events

    if not _atspi_events.listening():
        return

    def drop() -> None:
        # The guard is thread-local. It has to be set on the thread that
        # actually calls libatspi, which is the event thread when the loop
        # is up.
        _reply_local.dropping_properties = True
        try:
            method()
        except Exception:
            return
        finally:
            _reply_local.dropping_properties = False

    _atspi_events.run(drop)


def _invalidate_tree_cache(acc) -> None:
    """Drop the AT-SPI child cache on ``acc`` and its ancestors.

    libatspi answers ``get_child_at_index`` from that cache once it has been
    filled. A line scroll reads the list before the wheel, so the read after
    the wheel can still be the pre-wheel rows. A later snapshot, or a fresh
    process, fetches the children again and shows the move.
    """
    node = acc
    seen: set[int] = set()
    for _ in range(_MAX_SCROLL_ANCESTORS):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        _call_first(node, ("clear_cache", "clearCache"))
        node = _parent_of(node)


# Firefox's select is a combo, a menu, and menu items. Two levels reaches
# the item whose SELECTED state the read-back uses. A wider walk would clear
# a whole document on every option check.
_EVENT_CACHE_DEPTH = 2
_EVENT_CACHE_CHILD_CAP = 64


def _clear_cached_descendants(acc, depth: int) -> None:
    """``clear_cache`` on descendants, ``depth`` levels down."""
    if acc is None or isinstance(depth, bool) or not isinstance(depth, int) or depth <= 0:
        return
    count = _child_count(acc)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return
    children: list[object] = []
    for index in range(min(count, _EVENT_CACHE_CHILD_CAP)):
        child = _child_at(acc, index)
        if child is not None:
            children.append(child)
    for child in children:
        _clear_cached_descendants(child, depth - 1)
        _call_first(child, ("clear_cache", "clearCache"))


def _refresh_event_cache(acc) -> None:
    """Drop cached children and states while the event listener is up.

    ``event_main`` answers child and state reads from the cache. Firefox's
    selected option updates on the bus and not in that cache, so a combo
    read-back would still show the previous option. Clearing the combo alone
    leaves the menu item's SELECTED flag cached. With the listener off the
    cache is not trusted and this does nothing.
    """
    from a11y_computer_use.drivers import _atspi_events

    if acc is None or not _atspi_events.listening():
        return
    _clear_cached_descendants(acc, _EVENT_CACHE_DEPTH)
    _invalidate_tree_cache(acc)


def _descendant_names(acc) -> tuple[str, ...]:
    """Names under ``acc``, not including ``acc`` itself."""
    names: list[str] = []
    queue: list[tuple[object, int]] = []
    count = min(_child_count(acc), _LIST_NAME_LIMIT)
    for index in range(count):
        child = _child_at(acc, index)
        if child is not None:
            queue.append((child, _LIST_DEPTH))
    seen = 0
    while queue and len(names) < _LIST_NAME_LIMIT and seen < _LIST_NAME_LIMIT:
        node, remaining = queue.pop(0)
        seen += 1
        name = _node_name(node)
        if name:
            names.append(name)
        if remaining <= 1:
            continue
        child_count = min(_child_count(node), _LIST_NAME_LIMIT - seen)
        for index in range(child_count):
            child = _child_at(node, index)
            if child is not None:
                queue.append((child, remaining - 1))
    return tuple(names)


def list_signature(acc) -> tuple[str, ...] | None:
    """Names of the nearest visible list under ``acc``.

    None when fewer than two named nodes are visible. A text area with no
    such list is not judged a failure just because its own name stays put.
    The child cache is cleared first. Descendant names are included, so a
    row label that lives on a nested static text counts. A longer list
    further up replaces a short one: two scroll-bar labels must not hide
    the rows. Four or more names stops the walk.
    """
    if acc is None:
        return None
    _invalidate_tree_cache(acc)
    best: tuple[str, ...] | None = None
    node = acc
    seen: set[int] = set()
    for _ in range(_LIST_ANCESTORS):
        if node is None or id(node) in seen:
            break
        seen.add(id(node))
        names = _descendant_names(node)
        if len(names) >= 2 and (best is None or len(names) > len(best)):
            best = names
            if len(best) >= 4:
                break
        node = _parent_of(node)
    return best


def _collect_scrollbars(start) -> list:
    """Scroll bars on ``start`` and its ancestors, nearest first.

    GTK puts the bars on the scroll pane, beside the viewport that holds the
    text, so the pane is an ancestor of the content and the bars are its
    children. A busy window is only peeked at (first children), which is
    enough to see a scroll pane sitting next to a toolbar."""
    found: list = []
    seen: set[int] = set()
    visits = 0

    def consider(acc) -> None:
        nonlocal visits
        if acc is None or visits >= _MAX_SCROLL_NODES:
            return
        marker = id(acc)
        if marker in seen:
            return
        seen.add(marker)
        visits += 1
        if _is_scroll_bar(acc):
            found.append(acc)

    node = start
    for _ in range(_MAX_SCROLL_ANCESTORS):
        if node is None or visits >= _MAX_SCROLL_NODES:
            break
        consider(node)
        count = _child_count(node)
        limit = count if count <= 16 else 12
        for i in range(limit):
            if visits >= _MAX_SCROLL_NODES:
                break
            child = _child_at(node, i)
            consider(child)
            if child is None or _is_scroll_bar(child):
                continue
            nested = _child_count(child)
            if 0 < nested <= 8:
                for j in range(nested):
                    if visits >= _MAX_SCROLL_NODES:
                        break
                    consider(_child_at(child, j))
        node = _parent_of(node)
    return found


def _state_contains(acc, name: str) -> bool:
    Atspi = _atspi()
    sset = _call_first(acc, ("get_state_set",))
    if sset is None:
        return False
    member = getattr(getattr(Atspi, "StateType", None), name, None)
    if member is None:
        return False
    return bool(_safe(lambda: sset.contains(member), False))


def _bar_axis(acc) -> str | None:
    """'vertical' or 'horizontal' from AT-SPI state, else the bar's extents."""
    vertical = _state_contains(acc, "VERTICAL")
    horizontal = _state_contains(acc, "HORIZONTAL")
    if vertical and not horizontal:
        return "vertical"
    if horizontal and not vertical:
        return "horizontal"
    _position, size = _extents(acc)
    if not size:
        return None
    width, height = size
    if height > width:
        return "vertical"
    if width > height:
        return "horizontal"
    return None


def _value_call(names: tuple[str, ...], acc, *args):
    """Call the first `Atspi.Value` class method in ``names`` (binding-tolerant)."""
    value_iface = getattr(_atspi(), "Value", None)
    if value_iface is None:
        return None
    for name in names:
        method = getattr(value_iface, name, None)
        if method is not None:
            if name.startswith("set"):
                _drop_read_cache()
            return _safe(lambda m=method: m(acc, *args))
    return None


def _read_value(acc) -> float | None:
    raw = _value_call(("get_current_value", "getCurrentValue"), acc)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _value_bound(acc, names: tuple[str, ...], fallback: float) -> float:
    raw = _value_call(names, acc)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _write_value(acc, value: float) -> None:
    _value_call(("set_current_value", "setCurrentValue"), acc, float(value))


def _nudge_scrollbar(acc, delta: int, *, current: float) -> bool:
    """Set ``acc``'s value by ``delta`` and require the read-back to match.

    A zero-range bar (nothing to scroll) is not a success: the caller tries
    the next bar. A bar already at the limit we know about reports success
    without moving. A toolkit that clamps short of the reported maximum
    (GTK's maximum is often ``upper``, while the visible end is
    ``upper - page_size``) is a success only if one more pixel will not move.
    A write that jumps farther than the request (a wheel notch, a page) is
    undone and rejected.
    """
    minimum = _value_bound(acc, ("get_minimum_value", "getMinimumValue"), current)
    maximum = _value_bound(acc, ("get_maximum_value", "getMaximumValue"), current)
    if minimum > maximum:
        minimum, maximum = maximum, minimum
    target = min(max(current + float(delta), minimum), maximum)
    expected = target - current
    if abs(expected) < 0.5 and abs(float(delta)) >= 1 and (maximum - minimum) < 1.0:
        return False
    _write_value(acc, target)
    updated = _read_value(acc)
    if updated is None:
        _write_value(acc, current)
        return False
    actual = updated - current
    tolerance = max(1.0, abs(expected) * 0.05)
    if abs(actual - expected) <= tolerance:
        return True
    undershoot = abs(actual) + 0.5 < abs(expected) and (
        (delta > 0 and actual > 0.5) or (delta < 0 and actual < -0.5)
    )
    if undershoot and _stuck_at_limit(acc, updated, delta):
        return True
    if abs(actual) >= 0.5:
        _write_value(acc, current)
    return False


def _stuck_at_limit(acc, updated: float, delta: int) -> bool:
    """True when one more pixel from ``updated`` does not move the bar.

    If the probe moves, ``updated`` is written back and this returns False;
    the caller then restores the pre-nudge value. A short write stays only
    when that extra pixel does not move."""
    step = 1.0 if delta > 0 else -1.0
    _write_value(acc, updated + step)
    probed = _read_value(acc)
    if probed is None:
        _write_value(acc, updated)
        return False
    if abs(probed - updated) >= 0.5:
        _write_value(acc, updated)
        return False
    return True


def _apply_axis(bars, axis: str, delta: int) -> tuple[object, float] | None:
    """Nudge the nearest ``axis`` bar. Returns (bar, previous value) on success."""
    for bar in bars:
        if _bar_axis(bar) != axis:
            continue
        before = _read_value(bar)
        if before is None:
            continue
        if _nudge_scrollbar(bar, delta, current=before):
            return bar, before
    return None


def scroll_by_pixels(acc, *, dx: int = 0, dy: int = 0) -> bool:
    """Move ``acc``'s scroll bars by ``dx``/``dy`` via AT-SPI Value.

    Positive ``dy`` increases the vertical bar (content moves up). Positive
    ``dx`` increases the horizontal bar (content moves left). GTK scrolled
    windows expose that value in pixels, so a write of +3 that reads back as
    +3 is a 3-pixel scroll. Returns False when a requested axis has no bar,
    or when the value that reads back is not the requested delta. Does not
    send wheel events. On a partial failure the bars already written are put
    back.
    """
    dx, dy = int(dx), int(dy)
    if dx == 0 and dy == 0:
        return True
    if acc is None:
        return False
    bars = _collect_scrollbars(acc)
    applied: list[tuple[object, float]] = []
    for axis, delta in (("vertical", dy), ("horizontal", dx)):
        if not delta:
            continue
        done = _apply_axis(bars, axis, delta)
        if done is None:
            for prev, old in reversed(applied):
                _write_value(prev, old)
            return False
        applied.append(done)
    return True


def _accessible_at_point(x: int, y: int):
    """Hit-test the desktop, falling back to the native owner's showing frame.

    The registry desktop and application roots need not expose Component.
    Do not walk arbitrary background apps or guess between overlapping frames.
    """
    from a11y_computer_use.drivers import _linux_system

    Atspi = _atspi()
    desktop = _safe(lambda: Atspi.get_desktop(0))
    if desktop is None:
        return None
    found = _deepest_at_point(desktop, int(x), int(y), 0, set())
    if found is not None and found is not desktop:
        return found
    pid = _linux_system.pid_at_point(x, y)
    if pid is None:
        return None
    frames = []
    for index in range(_child_count(desktop)):
        app = _child_at(desktop, index)
        if pid_of(app) != pid:
            continue
        for frame in _fresh_application_children(app):
            if not _state_has(frame, "SHOWING"):
                continue
            rect = _raw_rect(frame, "SCREEN")
            if rect is None:
                continue
            left, top, width, height = rect
            if left <= x < left + width and top <= y < top + height:
                frames.append(frame)
    if len(frames) != 1:
        return None
    return _deepest_at_point(frames[0], int(x), int(y), 0, set())


def _deepest_at_point(acc, x: int, y: int, depth: int, seen: set[int]):
    if acc is None or depth > 24:
        return None
    marker = id(acc)
    if marker in seen:
        return None if depth == 0 else acc
    seen.add(marker)
    comp = _component(acc)
    if comp is None:
        return None if depth == 0 else acc
    coord = getattr(getattr(_atspi(), "CoordType", None), "SCREEN", 0)
    child = _call_first(
        comp, ("get_accessible_at_point", "getAccessibleAtPoint"), x, y, coord
    )
    if child is None or id(child) in seen:
        return None if depth == 0 else acc
    nested = _deepest_at_point(child, x, y, depth + 1, seen)
    return nested if nested is not None else child


def scroll_at_point(x: int, y: int, *, dx: int = 0, dy: int = 0) -> bool:
    """Pixel-scroll whatever accessible is under screen point ``(x, y)``.

    Hit-tests with ``Component.get_accessible_at_point`` and then applies
    `scroll_by_pixels`. Returns False when the point hits nothing that
    exposes a scroll bar. The Linux driver then tries a DOM scroll or a
    wheel at the element box, and keeps the step only when the position
    changed.
    """
    acc = _accessible_at_point(int(x), int(y))
    if acc is None:
        return False
    return scroll_by_pixels(acc, dx=dx, dy=dy)


def text_range_rect(acc, start: int, end: int, expected: str):
    """Read a SCREEN range only when every character is inside the visible widget.

    Wrapped ranges are refused: their enclosing rectangle can centre on blank
    space. Missing native geometry never falls back to the widget centre.
    """
    if acc is None or is_secure(acc) or not _state_has(acc, "SHOWING"):
        return None
    try:
        Atspi = _atspi()
        raw = _full_text(acc)
        if not isinstance(raw, str) or start < 0 or end <= start or raw[start:end] != expected:
            return None
        widget = _raw_rect(acc, "SCREEN")
        coord = Atspi.CoordType.SCREEN
        rect = Atspi.Text.get_range_extents(acc, start, end, coord)
        chars = [Atspi.Text.get_character_extents(acc, i, coord) for i in range(start, end)]
        if widget is None or rect is None or not chars:
            return None
        x, y, width, height = widget
        def inside(r):
            return (r is not None and all(math.isfinite(v) for v in (r.x, r.y, r.width, r.height))
                    and r.width > 0 and r.height > 0 and x <= r.x and y <= r.y
                    and r.x + r.width <= x + width and r.y + r.height <= y + height)
        if not inside(rect) or not all(inside(r) for r in chars):
            return None
        cx, cy = rect.x + rect.width / 2, rect.y + rect.height / 2
        if not all(r.y == chars[0].y for r in chars):
            return None
        if not any(r.x <= cx < r.x + r.width and r.y <= cy < r.y + r.height for r in chars):
            return None
        return rect.x, rect.y, rect.width, rect.height
    except Exception:
        return None


def chromium_row_identity(acc, *, fresh: bool = False, cache: dict | None = None) -> tuple:
    """Bind pooled Chromium rows and descendants to observed semantics.

    Cache ancestry only within one snapshot walk. Action-time reads invalidate
    both getter and native caches. Values, geometry and container summaries
    are mutable state and do not identify a row.
    """
    if acc is None or (cache is None and not _chromium_app(acc)):
        return ()
    if fresh:
        _drop_read_cache()
    refresh = fresh or cache is None
    pending = []
    tail, has_row = (), False
    # Snapshot reads already share a getter cache. The ordinary ancestry
    # helpers refresh native properties on every name/parent lookup, undoing
    # that sharing for every node in the window. Only act-time checks need
    # those invalidations; walk the same target ancestry without them here.
    def ancestors():
        node = acc
        pinned = []
        seen = set()
        for _ in range(32):
            if node is None:
                break
            key = _ancestor_key(node)
            if key in seen:
                break
            seen.add(key)
            pinned.append(node)
            yield node, key
            node = (_parent_of(node) if refresh else
                    _call_first(node, ("get_parent", "getParent")))

    for node, key in ancestors():
        if cache is not None and key in cache:
            tail, has_row = cache[key]
            break
        if refresh:
            _drop_cached_properties(node)
        role = _role_name(node)
        if role in {"document web", "document frame", "application", "frame"}:
            break
        is_row = role in {"list item", "list box item", "table row", "tree item"}
        pending.append((key, node, role, is_row))
    for key, node, role, is_row in reversed(pending):
        tail = ((key, node, role),) + tail
        has_row = has_row or is_row
        if cache is not None:
            cache[key] = (tail, has_row)
    if not has_row:
        return ()
    parts = []
    for key, node, role in tail:
        part_key = ("row-part", key)
        if cache is not None and part_key in cache:
            parts.append(cache[part_key])
            continue
        attrs = _get_attributes(node)
        # Never read a slider's drawn value or an editor's current text.
        named = role in {"list item", "list box item", "table row", "tree item",
                         "static", "label", "push button", "link"}
        name = ((_node_name(node) if refresh else
                 (_call_first(node, ("get_name", "getName"), default="") or "").strip())
                if named else "")
        part = (role, name, tuple((attr, attrs.get(attr)) for attr in (
            "rowindex", "aria-rowindex", "posinset", "aria-posinset"
        )), key)
        parts.append(part)
        if cache is not None:
            cache[part_key] = part
    return tuple(parts)


def read_text_selections(
    root: object, *, budget: float = 0.15,
) -> tuple[tuple[object, int, int, str], ...] | None:
    """Read native ranges and text in a caller-scoped subtree, without input.

    Return (native owner, start, end, text) rows, an empty tuple for a
    readable empty selection, or None for incomplete/unavailable reads.
    The deadline bounds traversal between D-Bus calls, not a hung call.
    Secure subtrees and masked text sentinels are never read at offsets.
    """
    deadline = time.monotonic() + budget
    try:
        api = _atspi().Text
    except Exception:
        return None
    # A ref may address static text inside a protected editor. Recheck
    # its native ancestors rather than trusting only the issued ref flag.
    ancestor = root
    ancestors = set()
    for _ in range(32):
        if ancestor is None:
            break
        if time.monotonic() >= deadline:
            return None
        key = _frame_cache_key(ancestor)
        if key in ancestors or is_secure(ancestor):
            return None
        ancestors.add(key)
        if _role_name(ancestor).startswith("document"):
            break
        ancestor = _parent_of(ancestor)
    else:
        return None
    pending = [root]
    seen = set()
    rows = []
    readable = False
    while pending:
        if time.monotonic() >= deadline:
            return None
        node = pending.pop()
        key = _frame_cache_key(node)
        if key in seen:
            continue
        seen.add(key)
        if is_secure(node):
            continue
        count = None
        try:
            count = api.get_n_selections(node)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("invalid selection count")
            readable = True
            for index in range(count):
                if time.monotonic() >= deadline:
                    return None
                span = _selection_bounds(api.get_selection(node, index))
                if span is None or span[0] < 0 or span[1] < span[0]:
                    return None
                start, end = span
                if end == start:
                    continue
                # An empty full-text sentinel is also used by masked GTK4
                # entries; never bypass it using positive-offset reads.
                if not api.get_text(node, 0, -1):
                    return None
                text = api.get_text(node, start, end)
                if not isinstance(text, str) or len(text) != end - start:
                    return None
                rows.append((node, start, end, text))
        except Exception:
            # Containers need not implement Text. A Text implementation
            # failing after reporting selections is not an empty selection.
            if isinstance(count, int) and count > 0:
                return None
        count = None
        n = _call_first(node, ("get_child_count",), default=0) or 0
        for index in range(n):
            if time.monotonic() >= deadline:
                return None
            child = _call_first(node, ("get_child_at_index",), index)
            if child is not None:
                pending.append(child)
    return tuple(rows) if readable else None


# Modal boundaries are independent of pointer occlusion: semantic AT-SPI actions
# bypass the overlay, so a successful doAction is not evidence of reachability.
def _chromium_modal_parent(acc):
    """Electron omits MODAL, but disables its transient dialog's parent.

    Combine the active X11 dialog relationship with the bridge's loss of
    RESIZABLE on the parent. A transient child alone is not a modal boundary.
    Keep this read operation-local; closing the child must restore input.
    """
    if (_role_name(acc) not in {"frame", "window"} or not _chromium_app(acc)
            or not _state_has(acc, "SHOWING") or not _state_has(acc, "ACTIVE")):
        return None
    key = ("chromium_modal_parent", ())
    cached = _lookup_read_cache(acc, key)
    if cached is not _CACHE_MISS:
        return cached
    from a11y_computer_use.drivers import _linux_system

    application = _call_first(acc, ("get_application", "getApplication"))
    parent = None
    if application is not None:
        for i in range(max(0, _child_count(application))):
            candidate = _child_at(application, i)
            if candidate is None:
                continue
            if (_ancestor_key(candidate) == _ancestor_key(acc)
                    or _role_name(candidate) not in {"frame", "window"}
                    or not _state_has(candidate, "SHOWING")
                    or _state_has(candidate, "ACTIVE")
                    or _call_first(candidate, ("get_state_set", "getState")) is None
                    or _state_has(candidate, "RESIZABLE")):
                continue
            pid = pid_of(acc)
            if pid and pid == pid_of(candidate) and _linux_system.active_transient_dialog(
                    pid=pid,
                    title=_call_first(acc, ("get_name", "getName"), default=""),
                    parent_title=_call_first(candidate, ("get_name", "getName"), default="")):
                parent = candidate
                break
    _store_read_cache(acc, key, parent)
    return parent


def is_modal(acc, *, attrs: dict | None = None) -> bool:
    """A showing ARIA or native modal, using reported state only."""
    if _role_name(acc) not in {"dialog", "alert", "window", "frame", "file chooser"}:
        return False
    if not _state_has(acc, "SHOWING"):
        return False
    attrs = _get_attributes(acc) if attrs is None else attrs
    return (_state_has(acc, "MODAL") or
            str(attrs.get("modal", attrs.get("aria-modal", ""))).lower() == "true"
            or _chromium_modal_parent(acc) is not None)


def _modal_collection(root, *, split: bool = False):
    """Reported dialog descendants, or None when this Collection cannot reply."""
    coll = _call_first(root, ("get_collection_iface", "get_collection"))
    if coll is not None:
        try:
            Atspi = _atspi()
            mt = Atspi.CollectionMatchType
            roles = [getattr(Atspi.Role, name) for name in
                     ("DIALOG", "ALERT", "WINDOW", "FRAME", "FILE_CHOOSER")
                     if hasattr(Atspi.Role, name)]
            rule = Atspi.MatchRule.new(Atspi.StateSet.new([]), mt.NONE,
                                      {}, mt.NONE, roles, mt.ANY, [], mt.NONE, False)
            if not split:
                return list(coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 0, True))
            # Split a wide native collection at a reported child handle. Each
            # query covers one half, including the pivot's descendants; no
            # row sampling, truncated result, or longer D-Bus timeout is used.
            count = _child_count(root)
            if count < 2:
                return None
            pivot = _child_at(root, count // 2)
            if pivot is None:
                return None
            tree = Atspi.CollectionTreeTraversalType
            before = coll.get_matches_to(
                pivot, rule, Atspi.CollectionSortOrder.CANONICAL,
                tree.RESTRICT_CHILDREN, True, 0, True,
            )
            after = coll.get_matches_from(
                pivot, rule, Atspi.CollectionSortOrder.CANONICAL,
                tree.RESTRICT_SIBLING, 0, True,
            )
            return [*before, pivot, *after]
        except Exception:
            return None
    return None


def _native_modal_parent(modal):
    """Reported owner of a native sibling modal, never inferred from overlap."""
    parent = _chromium_modal_parent(modal)
    if parent is not None:
        return parent
    chain = list(_ancestor_chain(modal))
    # Embedded/page dialogs already have an ancestor boundary. Do not apply
    # native window ownership to browser document modals.
    if any(_role_name(node) in {"frame", "window", "document web", "document frame"}
           for node in chain[1:]):
        return None
    key = ("native_modal_parent", ())
    cached = _lookup_read_cache(modal, key)
    if cached is not _CACHE_MISS:
        return cached
    parents = _relation_targets(modal, "subwindow_of")
    parents = [node for node in parents if _role_name(node) in {"frame", "window", "dialog"}
               and _ancestor_key(node) != _ancestor_key(modal)]
    parent = parents[0] if len(parents) == 1 else None
    if parent is None:
        application = next((node for node in chain if _role_name(node) == "application"), None)
        pid = pid_of(modal)
        if application is not None and pid:
            from a11y_computer_use.drivers import _linux_system
            title = _linux_system.transient_parent_title(pid=pid, title=_node_name(modal))
            if title:
                matches = [node for node in _fresh_application_children(application)
                           if _role_name(node) in {"frame", "window", "dialog"}
                           and _node_name(node) == title and pid_of(node) == pid]
                if len(matches) == 1:
                    parent = matches[0]
    _store_read_cache(modal, key, parent)
    return parent


def _modal_in_window(modal, root) -> bool:
    """Exclude only a positively resolved, different native parent window."""
    parent = _native_modal_parent(modal)
    if parent is None:
        return True
    chain = list(_ancestor_chain(root))
    keys = {_ancestor_key(node) for node in chain}
    if _ancestor_key(modal) in keys or _ancestor_key(parent) in keys:
        return True
    return not any(_role_name(node) in {"frame", "window"} for node in chain)


def snapshot_top_level_modals(root) -> list:
    """Inspect only the root and its application's immediate children.

    Recursive Collection queries materialize virtual GTK rows. Descendant
    modals are collected by ATSPIAccessor.read during the existing pruned walk.
    Never run an additional subtree search for snapshot modal discovery.
    """
    application = next((node for node in _ancestor_chain(root)
                        if _role_name(node) == "application"), None)
    candidates = [root]
    if application is not None and _ancestor_key(application) != _ancestor_key(root):
        if _chromium_app(root):
            # Preserve the outer snapshot's CHILDREN-cache bypass.
            candidates.extend(child for i in range(max(0, _child_count(application)))
                              if (child := _child_at(application, i)) is not None)
        else:
            candidates.extend(_fresh_application_children(application))
    return [node for node in candidates if is_modal(node) and _modal_in_window(node, root)]


def active_modals(root, *, fallback: bool = True, point=None) -> list:
    """Fresh modal descendants before pruning, including native modal windows.

    Collection avoids walking whole web pages merely to discover dialogs. The
    fallback is bounded and refuses input when it cannot establish a boundary.
    """
    if root is None:
        return []
    application = next((node for node in _ancestor_chain(root)
                        if _role_name(node) == "application"), None)
    # Calc's Collection materializes its virtual sheet and wedges later reads.
    # Native dialogs are structural descendants/siblings, never sheet cells.
    libreoffice = _libreoffice_accessible(root)
    # At input time, inspect structural containers first. A recursive window
    # query can overload Chromium on a wide page before its reply times out.
    # Delegate wide subtrees to two complete native queries instead.
    if libreoffice:
        candidates = None
    elif fallback:
        candidates = _cached_modal_collection(root)
    else:
        candidates = _modal_collection(root)
    if candidates is None and not fallback and not libreoffice:
        candidates = []
    if candidates is None:
        candidates = []
        queue = deque([root])
        seen = set()
        pinned = []
        while queue:
            node = queue.popleft()
            key = _ancestor_key(node)
            if key in seen:
                continue
            seen.add(key)
            pinned.append(node)
            if len(seen) > 4096:
                raise ComputerUseError(ErrorCode.UNSUPPORTED,
                    "Cannot establish the active modal boundary; no input was sent",
                    detail={"reason": "modal_boundary_unavailable"})
            candidates.append(node)
            if (_role_name(node) in {"tool bar", "menu bar", "status bar"}
                    and not any(_role_name(parent) in {"document web", "document frame"}
                                for parent in _ancestor_chain(node))):
                # Native modal windows are structural siblings, not toolbar
                # controls or lazily populated menu models.
                continue
            if point is not None and _role_name(node) in {"document web", "document frame"}:
                box = _raw_rect(node, "SCREEN")
                if box is not None and box[2] > 0 and box[3] > 0:
                    x, y = point
                    left, top, width, height = box
                    if not (left <= x < left + width and top <= y < top + height):
                        # Page descendants cannot cover browser chrome. Still
                        # inspect structural siblings for native browser dialogs.
                        continue
            if libreoffice and _role_name(node) in {
                "table", "document spreadsheet", "document text",
                "tool bar", "menu bar", "status bar",
            }:
                # Native Office dialogs are structural siblings of document
                # content. Walking Writer paragraphs adds a whole-document
                # scan to every scoped key, just as Calc materializes cells.
                continue
            if (_role_name(node) in {"document web", "document frame"}
                    and (_gecko_app(node) or _chromium_app(node))):
                # Query all dialog descendants at the document boundary for
                # both engines instead of walking page content.
                descendants = _modal_collection(node)
                if descendants is not None:
                    candidates.extend(descendants)
                    continue
                # Failed Collection is not evidence of no modal: retain the
                # bounded traversal and refusal on exhaustion.
            if (_role_name(node) in {"table", "tree table"}
                    and "gtk" in _toolkit_name(node)
                    and not _chromium_app(node) and not _gecko_app(node)):
                # Native GTK TreeView cells are model data, not containers
                # for modal windows. GTK dialogs are structural siblings
                # checked by this walk and the application-child probe.
                # Collection here materializes the cold model; split queries
                # are not supported by all GTK bridges either.
                continue
            count = _child_count(node)
            # Amortize remote child/role reads before reaching the safety
            # budget. Even a 200-row web list otherwise adds hundreds of
            # synchronous D-Bus calls to each input. Query only this subtree,
            # in complete halves, so a modal in its final row is not missed.
            if not libreoffice and count > min(64, 4096 - len(seen) - len(queue)):
                descendants = _modal_collection(node, split=True)
                if descendants is None and _chromium_app(node):
                    # When Chromium cannot answer a split query on a large
                    # table, ask the same scoped Collection for its complete
                    # result before falling back
                    # to bounded enumeration. Neither failure proves absence.
                    descendants = _modal_collection(node)
                if descendants is not None:
                    candidates.extend(descendants)
                    continue
            if count < 0 or count > 4096 - len(seen) - len(queue):
                raise ComputerUseError(ErrorCode.UNSUPPORTED,
                    "Cannot establish the active modal boundary; no input was sent",
                    detail={"reason": "modal_boundary_unavailable"})
            queue.extend(child for i in range(max(0, count))
                         if (child := _child_at(node, i)) is not None)
    if (application is not None
            and _ancestor_key(application) != _ancestor_key(root)):
        if not fallback and _chromium_app(root):
            # The Chromium snapshot already bypasses CHILDREN for this app.
            # A nested mask bypass would restore DEFAULT mid-traversal (#967).
            candidates.extend(child for i in range(max(0, _child_count(application)))
                              if (child := _child_at(application, i)) is not None)
        else:
            candidates.extend(_fresh_application_children(application))
    candidates.insert(0, root)
    found = {}
    for node in candidates:
        key = _ancestor_key(node)
        if key not in found and is_modal(node) and _modal_in_window(node, root):
            found[key] = node
    return list(found.values())


def modal_document(modal):
    """The reported web document owning a page modal, if any."""
    return next((node for node in _ancestor_chain(modal)
                 if _role_name(node) in {"document web", "document frame"}), None)


def _modal_chain(acc, cache):
    """Reuse reported ancestry only during one read-only snapshot traversal."""
    if cache is None:
        return list(_ancestor_chain(acc))
    chain = []
    keys = []
    seen = set()
    node = acc
    while node is not None and len(chain) < 32:
        key = _ancestor_key(node)
        if key in seen:
            break
        seen.add(key)
        if key in cache:
            chain.extend(cache[key][:32 - len(chain)])
            break
        chain.append(node)
        keys.append(key)
        node = _parent_of(node)
    # Cache only complete acyclic paths; bounded/cyclic ancestry must be
    # re-read from each target, just like the uncached guard.
    if node is None or (_ancestor_key(node) in cache and len(chain) < 32):
        # Values retain wrappers, including fallback id() identities. Only
        # the newly read prefix needs entries: cached ancestors already have
        # theirs, and re-keying the whole chain made each node O(depth).
        for index, ancestor_key in enumerate(keys):
            cache[ancestor_key] = chain[index:]
    return chain


def outside_modals(acc, modals: list, *, cache=None) -> bool:
    """True unless acc belongs to every applicable modal (nested modals included)."""
    if not modals:
        return False
    result_key = ("outside_modals", id(acc), tuple(id(node) for node in modals))
    if cache is not None and result_key in cache:
        return cache[result_key][2]
    result = _outside_modals(acc, modals, cache=cache)
    if cache is not None:
        cache[result_key] = (acc, tuple(modals), result)  # Pin all wrapper identities.
    return result


def _chain_facts(acc, chain, cache):
    """Ancestor keys and nearest frame/window of one chain, memoized per walk.

    Every Options-dialog cell repeated the full ancestor key set and frame
    role scan (O(depth) remote role reads per node). Facts derive from the
    parent's facts when the parent chain is already known, so a snapshot
    traversal reads each ancestor's role once.
    """
    if cache is None or not chain:
        return ({_ancestor_key(node) for node in chain},
                next((node for node in chain if _role_name(node) in {"frame", "window"}), None))
    key = _ancestor_key(acc)
    facts_key = ("chain_facts", key)
    hit = cache.get(facts_key)
    if hit is not None and hit[0] is acc:
        return hit[1], hit[2]
    # Only reuse a parent's facts when this chain is exactly node + its chain.
    parent_facts = None
    if len(chain) > 1:
        parent_hit = cache.get(("chain_facts", _ancestor_key(chain[1])))
        if parent_hit is not None and parent_hit[3] == len(chain) - 1:
            parent_facts = parent_hit
    if parent_facts is not None:
        parents = parent_facts[1] | {key}
        frame = acc if _role_name(acc) in {"frame", "window"} else parent_facts[2]
    else:
        parents = frozenset(_ancestor_key(node) for node in chain)
        frame = next((node for node in chain if _role_name(node) in {"frame", "window"}), None)
    if len(chain) < 32:  # Bounded chains are re-read, like _modal_chain.
        cache[facts_key] = (acc, parents, frame, len(chain))  # Pin acc for id() keys.
    return parents, frame


def _outside_modals(acc, modals, *, cache):
    chain = _modal_chain(acc, cache)
    parents, frame = _chain_facts(acc, chain, cache)
    # Reported containment already proves membership, including nested
    # modals. Window/document classification is needed only for an outside
    # target; doing it for every Options-dialog cell repeats remote reads.
    outside = [modal for modal in modals if _ancestor_key(modal) not in parents]
    if not outside:
        return False
    for modal in outside:
        context_key = ("modal_context", id(modal))
        context = cache.get(context_key) if cache is not None else None
        if context is None:
            modal_chain = _modal_chain(modal, cache)
            context = (modal, _native_modal_parent(modal),
                       next((node for node in modal_chain
                             if _role_name(node) in {"document web", "document frame"}), None),
                       next((node for node in modal_chain
                             if _role_name(node) in {"frame", "window"}), None))
            if cache is not None:
                cache[context_key] = context
        _, native_parent, document, owner = context
        if (native_parent is not None and frame is not None
                and _ancestor_key(frame) not in {
                    _ancestor_key(native_parent), _ancestor_key(modal)}):
            continue
        if (document is not None and _ancestor_key(document) not in parents
                and (frame is not None or any(
                    _role_name(node) in {"document web", "document frame"} for node in chain))):
            # A page cannot make browser chrome or another document inert.
            # Missing hits/ancestry do not prove a target is browser chrome.
            continue
        if (owner is not None and frame is not None
                and _ancestor_key(owner) != _ancestor_key(modal)
                and _ancestor_key(owner) != _ancestor_key(frame)):
            continue
        if _ancestor_key(modal) not in parents:
            return True
    return False


# Accessed on the native event thread. Without a listener, retain discovery
# only within the immediate action, never across actions.
_modal_discoveries: dict = {}
_modal_generation: int | None = None


def _remember_modal_collection(root, candidates, generation) -> None:
    """Retain a complete role query only under the subscribed boundary epoch."""
    from a11y_computer_use.drivers import _atspi_events

    global _modal_generation
    if (generation is None or not _atspi_events.modal_cache_ready()
            or generation != _atspi_events.modal_epoch()):
        return
    # A fresh query supersedes derived discoveries even if its event has not
    # arrived yet. Eviction is safe: other windows will simply rediscover.
    _modal_discoveries.clear()
    _modal_generation = generation
    _modal_discoveries[("collection", _ancestor_key(root))] = (root, list(candidates))


def _cached_modal_collection(root):
    """Complete dialog-role candidates, or None when discovery must run again."""
    from a11y_computer_use.drivers import _atspi_events

    if (not _atspi_events.modal_cache_ready()
            or _modal_generation != _atspi_events.modal_epoch()):
        return None
    result = _modal_discoveries.get(("collection", _ancestor_key(root)))
    return list(result[1]) if result is not None else None


def input_modals(root, cache: dict | None = None, *, point=None) -> list:
    """Reuse discovery until a window/document/structural event invalidates it."""
    from a11y_computer_use.drivers import _atspi_events

    global _modal_generation
    if cache is None:
        cache = getattr(_read_cache_local, "input_modals", None)
    generation = _atspi_events.modal_epoch() if _atspi_events.modal_cache_ready() else None
    if generation != _modal_generation or generation is None:
        _modal_discoveries.clear()
        _modal_generation = generation
    root_key = _ancestor_key(root)
    complete_key = (root_key, None)
    complete_action_key = (generation, complete_key)
    if cache is not None and complete_action_key in cache:
        return cache[complete_action_key][1]
    if generation is not None and complete_key in _modal_discoveries:
        result = _modal_discoveries[complete_key]
        if cache is not None:
            cache[complete_action_key] = result
        return result[1]
    key = (root_key, point)
    action_key = (generation, key)
    if cache is not None and action_key in cache:
        return cache[action_key][1]
    if generation is not None and key in _modal_discoveries:
        result = _modal_discoveries[key]
    else:
        result = (root, active_modals(root) if point is None else active_modals(root, point=point))
        if generation is not None:
            if len(_modal_discoveries) >= 64:
                _modal_discoveries.clear()  # Eviction forces rediscovery.
            _modal_discoveries[key] = result
    if cache is not None:
        cache[action_key] = result  # Pin fallback id() owners.
    return result[1]


def modal_dismiss_hit(acc, root, x: int, y: int, *, modal_cache=None) -> bool:
    """Recognize measured web backdrop/empty hits; reject control-role hits.

    Accessible hit tests can return the document for an unexposed backdrop.
    A clickable generic sibling is eligible only when its reported rectangle
    encloses the modal. Missing hits/geometry and native dialogs fail closed.
    """
    modals = input_modals(root, modal_cache, point=(x, y))
    if not modals or acc is None or _point_in_extents(acc, x, y) is not True:
        return False
    chain = list(_ancestor_chain(acc))
    documents = {"document web", "document frame"}
    doc = next((n for n in chain if _role_name(n) in documents), None)
    if doc is None:
        return False
    doc_key = _ancestor_key(doc)
    modal_bounds = []
    for modal in modals:
        bounds = _raw_rect(modal, "SCREEN")
        if bounds is None or bounds[2] <= 0 or bounds[3] <= 0:
            return False
        modal_bounds.append(bounds)
        if not any(_ancestor_key(n) == doc_key for n in _ancestor_chain(modal)):
            return False
        if _point_in_extents(modal, x, y) is not False:
            return False
    generic = {"section", "panel", "filler", "unknown", "redundant object"}
    for node in chain:
        role = _role_name(node)
        if role not in generic | documents:
            return False
        if (_state_has(node, "EDITABLE")
                or (role not in documents and _state_has(node, "FOCUSABLE"))):
            return False
        if _raw_action_names(node):
            # A generic onclick backdrop is actionable too. Require measured
            # coverage of every modal; ordinary background controls stay blocked.
            box = _raw_rect(node, "SCREEN")
            if box is None:
                return False
            left, top, width, height = box
            for mx, my, mw, mh in modal_bounds:
                if not (width > 0 and height > 0 and mw > 0 and mh > 0
                        and left <= mx and top <= my
                        and mx + mw <= left + width and my + mh <= top + height):
                    return False
        if _ancestor_key(node) == doc_key:
            return True
    return False


def refuse_outside_modal(acc, root=None, *, modal_cache: dict | None = None, point=None) -> bool:
    """Refuse before input when the addressed accessible is outside a modal."""
    if root is None:
        chain = list(_ancestor_chain(acc))
        if chain and _parent_of(chain[-1]) is not None:
            raise ComputerUseError(ErrorCode.UNSUPPORTED,
                "Cannot establish the target window's modal boundary; no input was sent",
                detail={"reason": "modal_boundary_unavailable"})
        # Include sibling native dialogs in the application, but no other apps.
        # Native top-level dialogs (including LibreOffice startup tips) can
        # belong directly to the application, without a frame ancestor. The
        # desktop is above that application and must never become our scope.
        application = next((node for node in chain
                            if _role_name(node) == "application"), None)
        root = next((node for node in chain if _role_name(node) in {"frame", "window"}),
                    application if application is not None else chain[-1] if chain else None)
        if chain and not any(_role_name(node) in {
                "frame", "window", "application", "document web", "document frame"} for node in chain):
            # An explicit ref may also lose its Chrome wrapper ancestry. Do
            # not mistake that orphan for a window with no active modals.
            from a11y_computer_use.drivers import _linux_system
            from a11y_computer_use.schema import Scope
            root = find_root(_linux_system.frontmost_app_id(), Scope.WINDOW)
            box = _raw_rect(acc, "SCREEN")
            if point is None and box is not None and box[2] > 0 and box[3] > 0:
                point = (box[0] + box[2] / 2, box[1] + box[3] / 2)
    modals = input_modals(root, modal_cache, point=point)
    point_blocked = False
    if point is not None:
        # Chrome can return an ancestry-less address-bar text object or no hit.
        # Only measured document geometry can exclude its modal for that point.
        applicable = []
        for modal in modals:
            document = modal_document(modal)
            box = _raw_rect(document, "SCREEN") if document is not None else None
            if box is not None and box[2] > 0 and box[3] > 0:
                x, y = point
                left, top, width, height = box
                if not (left <= x < left + width and top <= y < top + height):
                    continue
                # An approximate hit with browser ancestry cannot authorize
                # input at a point inside the page behind its modal.
                if not any(_ancestor_key(n) == _ancestor_key(modal)
                           for n in _ancestor_chain(acc)):
                    point_blocked = True
            applicable.append(modal)
        modals = applicable
    if point_blocked or outside_modals(acc, modals):
        names = ", ".join(repr(_node_name(node)) for node in modals)
        raise ComputerUseError(ErrorCode.UNSUPPORTED,
            f"outside_active_modal: target is outside {names}; no input was sent",
            detail={"reason": "outside_active_modal", "outcome": "refused",
                    "hint": "Use a control inside the active modal. A single plain left click on its backdrop or empty outside area may dismiss it; background controls remain blocked.",
                    "next": (["Use browser Back (alt+Left) or focus the address bar (ctrl+l) to leave the page."]
                             if all(modal_document(node) is not None for node in modals) else [])})

    return bool(modals)
