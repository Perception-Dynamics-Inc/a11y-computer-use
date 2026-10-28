"""Input-method awareness for XTEST typing (#1219).

An application that talks to an IBus engine (``XMODIFIERS=@im=ibus``, GTK/Qt
IBus modules) routes every key event through the engine. With ``libpinyin`` or
``m17n:ru:translit`` active, ``hello world 42`` typed as ASCII keystrokes
reaches the page as ``合理咯我人类的42`` or ``хелло ворлд 42``. Keystrokes for
characters outside the layout use a spare keycode and pass through, so only
the ASCII path is affected.

While text is typed, the global engine is switched to a pass-through ``xkb:``
engine and the user's engine is restored afterwards. ``xkb:*`` engines are
what IBus uses for a plain layout, so nothing is transformed. If the switch
cannot be made and the text has ASCII in it, nothing is typed: the call is
refused as ``ime_active`` naming the engine, before any key is sent, so no
transformed text is left in the field. Switching engines discards an open
composition (uncommitted preedit), so when the IBus panel shows one the call is
refused as ``ime_composition_open`` before the switch and before any key: the
preedit is left as it was. The panel is read until it has shown no composition for
a settle period, so a preedit it has not published yet is not missed. When the panel
cannot be read at all, the call is refused as ``ime_composition_unknown`` (set
``A11Y_IME_COMPOSITION_UNKNOWN=proceed`` to type anyway). When the engine itself
cannot be read (the query times out or fails), ASCII text is refused as
``ime_unknown`` before any key (#1270).

Scope: IBus only. fcitx5 and others are not switched or detected.
``A11Y_IME_BYPASS=0`` turns the switch off.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

_log = logging.getLogger(__name__)

PASSTHROUGH_ENGINE = "xkb:us::eng"
_TIMEOUT_S = 2.0
# The panel learns of a preedit a beat after the keys that made it (the key goes
# application -> ibus-daemon -> engine -> panel -> AT-SPI), so one idle read is
# not proof. "No composition" has to hold for _SETTLE_S of consecutive reads, and
# an unreadable panel is retried for up to _BUDGET_S before it is called unknown.
_SETTLE_S = 0.25
_BUDGET_S = 1.5
_POLL_S = 0.03


@dataclass(frozen=True)
class ActiveIme:
    framework: str
    engine: str

    def __str__(self) -> str:
        return f"{self.framework} engine {self.engine}"


_state = threading.local()


def _enabled() -> bool:
    return os.environ.get("A11Y_IME_BYPASS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _ibus_binary() -> str | None:
    return shutil.which("ibus")


def _ibus(*args: str) -> subprocess.CompletedProcess | None:
    binary = _ibus_binary()
    if binary is None:
        return None
    try:
        return subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# What ``ibus engine`` prints when there is no engine to transform keys: none
# set, or no daemon to connect to (it then also logs IBUS-CRITICAL on stderr).
_NO_ENGINE_REPLIES = ("No engine is set.", "Can't connect to IBus.")


class _Unreadable:
    def __repr__(self) -> str:
        return "UNREADABLE"


UNREADABLE = _Unreadable()


def _engine_query() -> str | None | _Unreadable:
    """The IBus global engine name; None when there is none; UNREADABLE otherwise.

    None only for a missing ``ibus`` binary or one of ``_NO_ENGINE_REPLIES``. A
    query that times out, cannot be run, or answers with anything else leaves the
    engine unknown (#1270): a pinyin engine behind it would still transform keys.
    """
    if _ibus_binary() is None:
        return None
    done = _ibus("engine")
    if done is None:
        return UNREADABLE
    out = (done.stdout or "").strip()
    if out in _NO_ENGINE_REPLIES or (done.stderr or "").strip().endswith(_NO_ENGINE_REPLIES[1]):
        return None
    if done.returncode != 0 or not out or any(ch.isspace() for ch in out):
        return UNREADABLE
    return out


def _current_engine() -> str | None:
    """The IBus global engine name, or None (none set, or it could not be read)."""
    name = _engine_query()
    return name if isinstance(name, str) else None


def active_ime() -> ActiveIme | None:
    """The active IBus engine when it can transform keystrokes, else None.

    ``xkb:*`` engines are a plain keyboard layout and pass keys through.
    """
    if not _enabled():
        return None
    return _ime_from(_current_engine())


def _ime_from(name: str | None | _Unreadable) -> ActiveIme | None:
    if not isinstance(name, str) or name.startswith("xkb:"):
        return None
    return ActiveIme("ibus", name)


def needs_passthrough(text: str | None) -> bool:
    """True when typing ``text`` goes through the engine as ASCII keystrokes.

    Characters outside ASCII use a spare keycode and pass an engine unchanged.
    ``None`` means the text is not known, so assume it does.
    """
    return text is None or any(0x20 <= ord(ch) < 0x7F for ch in text)


def _refuse(ime: ActiveIme):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"the active input method ({ime}) changes typed text and could not be switched "
        "to a pass-through engine; nothing was typed and the field was not changed. Switch the input method to a plain "
        "keyboard layout (for example 'ibus engine xkb:us::eng') and retry",
        detail={
            "platform": "linux",
            "reason": "ime_active",
            "ime": {"framework": ime.framework, "engine": ime.engine},
            "typed": False,
        },
    )


def _refuse_unreadable():
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        "the IBus input-method engine could not be read, so typed text might be changed "
        "by it (for example into pinyin); nothing was typed and the field was not "
        "changed. Check that ibus-daemon is responsive ('ibus engine'), or switch to a "
        "plain keyboard layout (for example 'ibus engine xkb:us::eng') and retry",
        detail={
            "platform": "linux",
            "reason": "ime_unknown",
            "ime": {"framework": "ibus", "engine": None},
            "typed": False,
        },
    )


def _composition_open() -> bool | None:
    """True when the IBus panel shows an uncommitted composition, None if unknown."""
    try:
        from a11y_computer_use.drivers import _atspi

        return _atspi.ime_composition_open()
    except Exception:
        return None


def _panel_absent() -> bool:
    """True when a clean desktop read lists no IBus panel (#1220)."""
    try:
        from a11y_computer_use.drivers import _atspi

        return bool(_atspi.ime_panel_absent())
    except Exception:
        return False


def _composition_read() -> str:
    """``open``, ``clear``, ``absent`` (no panel on a clean read) or ``unreadable``."""
    state = _composition_open()
    if state is True:
        return "open"
    if state is False:
        return "clear"
    return "absent" if _panel_absent() else "unreadable"


def composition_state() -> bool | None:
    """Whether an IBus composition is open: True, False, or None when unknown.

    True as soon as one read of the panel shows it. False only when every read
    over ``_SETTLE_S`` shows none, so a preedit the panel has not published yet
    is waited for rather than missed. None when the panel could not be read
    cleanly for ``_BUDGET_S``, or sooner when every read over ``_SETTLE_S``
    cleanly lists no panel at all (#1220): a disabled panel will not appear by
    waiting, and a failed read still gets the whole budget.
    """
    deadline = time.monotonic() + _BUDGET_S
    since: float | None = None
    last: str | None = None
    reads = 0
    while True:
        state = _composition_read()
        now = time.monotonic()
        if state == "open":
            return True
        if state in ("clear", "absent"):
            if state != last:
                since, reads = now, 0
            reads += 1
            if now - since >= _SETTLE_S and reads >= 3:
                return False if state == "clear" else None
        else:
            since, reads = None, 0
        last = state
        if now >= deadline:
            return None
        time.sleep(_POLL_S)


def _unknown_allowed() -> bool:
    return os.environ.get("A11Y_IME_COMPOSITION_UNKNOWN", "").strip().lower() == "proceed"


def _refuse_composition_unknown(ime: ActiveIme):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"cannot tell whether an input-method composition is open ({ime}): the IBus "
        "panel could not be read, and switching the engine would discard an open "
        "composition; nothing was typed and the field was not changed. Switch the input "
        "method to a plain keyboard layout (for example 'ibus engine xkb:us::eng') and "
        "retry, or set A11Y_IME_COMPOSITION_UNKNOWN=proceed to type without this check",
        detail={
            "platform": "linux",
            "reason": "ime_composition_unknown",
            "ime": {"framework": ime.framework, "engine": ime.engine},
            "typed": False,
        },
    )


def _check_composition(ime: ActiveIme) -> None:
    """Raise unless it is known that no composition is open."""
    state = composition_state()
    if state is True:
        raise _refuse_composition(ime)
    if state is None and not _unknown_allowed():
        raise _refuse_composition_unknown(ime)


def require_no_composition() -> None:
    """Raise before a destructive key when a composition is open or cannot be ruled out.

    A no-op inside ``ascii_passthrough``, which has already checked, and without
    a transforming engine.
    """
    if getattr(_state, "depth", 0):
        return
    ime = active_ime()
    if ime is not None:
        _check_composition(ime)


def _refuse_composition(ime: ActiveIme):
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        f"an input-method composition is open ({ime}): uncommitted text is in the field "
        "and switching the engine to type would discard it; nothing was typed and the "
        "composition was left as it was. Commit it (Enter or pick a candidate) or cancel "
        "it (Escape), then retry",
        detail={
            "platform": "linux",
            "reason": "ime_composition_open",
            "ime": {"framework": ime.framework, "engine": ime.engine},
            "typed": False,
        },
    )


def _set_engine(name: str) -> bool:
    done = _ibus("engine", name)
    if done is None:
        return False
    # The exit status is not trusted: read the engine back.
    return _current_engine() == name


@contextmanager
def ascii_passthrough(text: str | None = None):
    """Keep an IBus engine from transforming the keystrokes sent inside.

    ``text`` is what will be typed inside. When the engine cannot be switched
    and that text has ASCII in it, this raises the ``ime_active`` refusal
    before the body runs, so no key is sent. Re-entrant: only the outermost use
    switches and restores. Without an active transforming engine this is one
    cheap query and nothing else.
    """
    depth = getattr(_state, "depth", 0)
    if depth:
        _state.depth = depth + 1
        try:
            yield
        finally:
            _state.depth -= 1
        return
    engine = _engine_query() if _enabled() else None
    if engine is UNREADABLE and needs_passthrough(text):
        # Refuse before any key: an engine that cannot be read may be one that
        # transforms ASCII, and nothing here can switch it (#1270).
        raise _refuse_unreadable()
    ime = _ime_from(engine)
    switched = False
    if ime is not None:
        _check_composition(ime)
        switched = _set_engine(PASSTHROUGH_ENGINE)
        if not switched:
            _log.warning("could not switch %s to %s", ime, PASSTHROUGH_ENGINE)
            if needs_passthrough(text):
                raise _refuse(ime)
    _state.depth = 1
    try:
        yield
    finally:
        _state.depth = 0
        if switched and ime is not None:
            if not _set_engine(ime.engine) and not _set_engine(ime.engine):
                _log.warning("could not restore the IBus engine %s", ime.engine)
