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
``A11Y_IME_COMPOSITION_UNKNOWN=proceed`` to type anyway).

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


def _current_engine() -> str | None:
    """The IBus global engine name, or None (no IBus, no daemon, none set)."""
    done = _ibus("engine")
    if done is None or done.returncode != 0:
        return None
    name = done.stdout.strip()
    # "No engine is set." and other prose are not engine names.
    if not name or any(ch.isspace() for ch in name):
        return None
    return name


def active_ime() -> ActiveIme | None:
    """The active IBus engine when it can transform keystrokes, else None.

    ``xkb:*`` engines are a plain keyboard layout and pass keys through.
    """
    if not _enabled():
        return None
    name = _current_engine()
    if name is None or name.startswith("xkb:"):
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


def _composition_open() -> bool | None:
    """True when the IBus panel shows an uncommitted composition, None if unknown."""
    try:
        from a11y_computer_use.drivers import _atspi

        return _atspi.ime_composition_open()
    except Exception:
        return None


def composition_state() -> bool | None:
    """Whether an IBus composition is open: True, False, or None when unknown.

    True as soon as one read of the panel shows it. False only when every read
    over ``_SETTLE_S`` shows none, so a preedit the panel has not published yet
    is waited for rather than missed. None when the panel could not be read
    cleanly for ``_BUDGET_S``.
    """
    deadline = time.monotonic() + _BUDGET_S
    clean_since: float | None = None
    reads = 0
    while True:
        state = _composition_open()
        reads += 1
        now = time.monotonic()
        if state is True:
            return True
        if state is False:
            if clean_since is None:
                clean_since = now
            if now - clean_since >= _SETTLE_S and reads >= 3:
                return False
        else:
            clean_since = None
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
    ime = active_ime()
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
