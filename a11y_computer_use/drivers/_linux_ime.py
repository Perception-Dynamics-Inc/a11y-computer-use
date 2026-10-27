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
cannot be made the failure is remembered, and the caller's ``text_mismatch``
is reported as ``ime_active`` naming the engine.

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
# A refusal names the IME only while the typing call that could not bypass it
# is still being judged.
_FAILURE_WINDOW_S = 10.0


@dataclass(frozen=True)
class ActiveIme:
    framework: str
    engine: str

    def __str__(self) -> str:
        return f"{self.framework} engine {self.engine}"


_state = threading.local()
_failure: tuple[ActiveIme, float] | None = None


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


def recent_bypass_failure() -> ActiveIme | None:
    """The IME that a recent typing call could not bypass, or None."""
    found = _failure
    if found is None or time.monotonic() - found[1] > _FAILURE_WINDOW_S:
        return None
    return found[0]


def _set_engine(name: str) -> bool:
    done = _ibus("engine", name)
    if done is None:
        return False
    # The exit status is not trusted: read the engine back.
    return _current_engine() == name


@contextmanager
def ascii_passthrough():
    """Keep an IBus engine from transforming the keystrokes sent inside.

    Re-entrant: only the outermost use switches and restores. Without an
    active transforming engine this is one cheap query and nothing else.
    """
    global _failure
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
    _failure = None
    if ime is not None:
        switched = _set_engine(PASSTHROUGH_ENGINE)
        if not switched:
            _failure = (ime, time.monotonic())
            _log.warning("could not switch %s to %s; typed text may be transformed", ime, PASSTHROUGH_ENGINE)
    _state.depth = 1
    try:
        yield
    finally:
        _state.depth = 0
        if switched and ime is not None:
            if not _set_engine(ime.engine) and not _set_engine(ime.engine):
                _log.warning("could not restore the IBus engine %s", ime.engine)
