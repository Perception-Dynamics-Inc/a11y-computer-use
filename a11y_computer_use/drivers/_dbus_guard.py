"""Keep a libatspi D-Bus mistake from exiting the process.

LibreOffice's registration calls ``GetApplicationBusAddress``. libatspi
(at-spi2-core 2.52) handles the reply in ``handle_get_bus_address`` by opening
a private socket and, when the application already has one, dropping it with
``dbus_connection_unref`` and no ``dbus_connection_close``. The first
replacement releases a reference on the shared accessibility bus. A second
replacement, which happens while Calc is still registering, drops the last
reference on a private socket that is still connected and still has a GLib
watch. libdbus then writes "The last reference on a connection was dropped"
and frees the socket under that watch. That is a use-after-free. With
``DBUS_FATAL_WARNINGS=1`` the same check aborts. A shared ``dbus_bus_get``
connection also exits the process on disconnect (``exit-on-disconnect``
defaults to true).

None of those paths raise a Python exception. This module:

* forces ``DBUS_FATAL_WARNINGS=0`` before the first libdbus warning
* turns exit-on-disconnect off on connections libatspi opens
* keeps the last reference on a still-connected socket

Closing that socket disconnects the app: Firefox's document then never
appears. Freeing it under the GLib watch is the use-after-free. Holding the
last reference does neither. The connection object leaks once per
replacement, which is the same choice ``_revive_application`` already makes.

``call_with_reconnect`` then reconnects once and either returns the retried
call or a retryable ``timeout`` (``reason=bus_disconnected``).
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections.abc import Callable
from typing import TypeVar

from a11y_computer_use.schema import ComputerUseError, ErrorCode

_T = TypeVar("_T")

_TLS = threading.local()
_LOCK = threading.Lock()

_installed = False
_patched = False
_refcount_ok = False
_layout_ok = False
_drops = 0
_held = 0
_in_reconnect = False

_private: set[int] = set()
_shared: set[int] = set()
_keepalive: list[object] = []
_patched_symbols: list[str] = []
_install_error = ""

# dbus 1.14.10 amd64: the refcount is the int at offset 0, and bit 0 of the
# byte at 0x100 is set on a dbus_bus_get connection. Confirmed against a live
# session bus before either fact is trusted.
_REFCOUNT_OFFSET = 0
_SHARED_OFFSET = 0x100

_lib = None
_libc = None
_real_unref = None
_real_ref = None
_real_close = None
_real_open_private = None
_real_bus_get = None
_real_is_connected = None
_real_set_exit = None
_real_get_exit = None
_unref_hook = None
_open_hook = None
_bus_hook = None

_BUS_TEXT = (
    "disconnected",
    "not connected",
    "connection was dropped",
    "accessibility bus",
)


def drops() -> int:
    """How many last-reference drops the hook has caught in this process."""
    return _drops


def status() -> dict[str, object]:
    """What ``install`` managed to patch. Tests read this from a child process."""
    return {
        "installed": _installed,
        "patched": _patched,
        "refcount_ok": _refcount_ok,
        "layout_ok": _layout_ok,
        "symbols": list(_patched_symbols),
        "drops": _drops,
        "held": _held,
        "error": _install_error,
    }


def install() -> bool:
    """Patch libatspi's dbus imports. Idempotent. False when there is nothing to patch.

    Safe to call on macOS and Windows, and when the accessibility libraries
    are not installed: the process is unchanged and later calls are no-ops.
    """
    global _installed, _patched, _install_error
    if not sys.platform.startswith("linux"):
        return False
    # The fatal-warning flag is cached on the first libdbus warning. Set the
    # variable before any connection is opened.
    os.environ["DBUS_FATAL_WARNINGS"] = "0"
    if _installed:
        return _patched
    with _LOCK:
        if _installed:
            return _patched
        try:
            _load()
            _probe_layout()
            _patched_now = _patch_libatspi()
        except Exception as exc:
            _install_error = f"{type(exc).__name__}: {exc}"
            _installed = True
            return False
        _patched = _patched_now
        _installed = True
        return _patched


def call_with_reconnect(fn: Callable[[], _T]) -> _T:
    """Run ``fn``. On a bus failure, reconnect once and run it again.

    Nested calls let the outermost one reconnect, so a snapshot retries the
    whole walk instead of retrying a single read against a disposed accessible.
    Off Linux this is ``fn()``.
    """
    if not sys.platform.startswith("linux"):
        return fn()
    install()
    depth = getattr(_TLS, "depth", 0) + 1
    _TLS.depth = depth
    start = _drops
    try:
        return fn()
    except (KeyboardInterrupt, SystemExit):
        raise
    except ComputerUseError as exc:
        if depth != 1 or exc.detail.get("reason") == "bus_disconnected" or _drops == start:
            raise
        return _retry(fn, exc)
    except Exception as exc:
        if depth != 1 or (_drops == start and not _is_bus_exception(exc)):
            raise
        return _retry(fn, exc)
    finally:
        _TLS.depth = depth - 1


def reconnect() -> bool:
    """Drop the current AT-SPI connection and open a new one. False if it cannot."""
    global _in_reconnect
    if _in_reconnect:
        return False
    _in_reconnect = True
    try:
        try:
            from a11y_computer_use.drivers import _atspi

            Atspi = _atspi._atspi()
        except Exception:
            return False
        try:
            if bool(Atspi.is_initialized()):
                Atspi.exit()
        except Exception:
            return False
        _atspi._inited = False
        try:
            _atspi._atspi()
        except Exception:
            return False
        return True
    finally:
        _in_reconnect = False


def bus_disconnected(exc: BaseException) -> ComputerUseError:
    """A retryable timeout the caller can show without losing the server."""
    text = f"{type(exc).__name__}: {exc}"
    if len(text) > 300:
        text = text[:300]
    return ComputerUseError(
        ErrorCode.TIMEOUT,
        "the accessibility bus dropped; retry the snapshot",
        detail={
            "reason": "bus_disconnected",
            "retryable": True,
            "error": text,
            "hint": "retry the snapshot",
        },
    )


def _exercise_private_unref(address: str) -> dict[str, object]:
    """Open a private socket and drop its last reference through the hook.

    ``tests/test_dbus_guard.py`` runs this in a child process. A correct
    hook keeps the last reference, so libdbus does not warn and the process
    does not abort even when ``DBUS_FATAL_WARNINGS`` started as ``1``.
    """
    install()
    if _unref_hook is None or _open_hook is None:
        raise RuntimeError(f"dbus hook is not installed: {status()}")
    before = _held
    conn = _open_hook(address.encode(), None)
    if not conn:
        raise RuntimeError("dbus_connection_open_private failed")
    _unref_hook(conn)
    result: dict[str, object] = {"held": _held - before, "drops": _drops, "status": status()}
    if _bus_hook is not None and _real_get_exit is not None:
        session = _bus_hook(0, None)
        if session:
            result["session_exit_on_disconnect"] = int(_real_get_exit(session))
            if _refcount_ok and _refcount(session) > 1:
                _real_unref(session)
    return result


def _retry(fn: Callable[[], _T], exc: BaseException) -> _T:
    if not reconnect():
        raise bus_disconnected(exc) from exc
    try:
        return fn()
    except (KeyboardInterrupt, SystemExit):
        raise
    except ComputerUseError:
        raise
    except Exception as retry_exc:
        if _is_bus_exception(retry_exc):
            raise bus_disconnected(retry_exc) from retry_exc
        raise


def _is_bus_exception(exc: BaseException) -> bool:
    if isinstance(exc, (ComputerUseError, ImportError, KeyboardInterrupt, SystemExit)):
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    return any(word in text for word in _BUS_TEXT)


def _load() -> None:
    global _lib, _libc
    global _real_unref, _real_ref, _real_close, _real_open_private, _real_bus_get
    global _real_is_connected, _real_set_exit, _real_get_exit
    _lib = ctypes.CDLL("libdbus-1.so.3")
    _libc = ctypes.CDLL(None, use_errno=True)
    _real_unref = _lib.dbus_connection_unref
    _real_ref = _lib.dbus_connection_ref
    _real_close = _lib.dbus_connection_close
    _real_open_private = _lib.dbus_connection_open_private
    _real_bus_get = _lib.dbus_bus_get
    _real_is_connected = _lib.dbus_connection_get_is_connected
    _real_set_exit = _lib.dbus_connection_set_exit_on_disconnect
    _real_unref.argtypes = [ctypes.c_void_p]
    _real_unref.restype = None
    _real_ref.argtypes = [ctypes.c_void_p]
    _real_ref.restype = ctypes.c_void_p
    _real_close.argtypes = [ctypes.c_void_p]
    _real_close.restype = None
    _real_open_private.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    _real_open_private.restype = ctypes.c_void_p
    _real_bus_get.argtypes = [ctypes.c_int, ctypes.c_void_p]
    _real_bus_get.restype = ctypes.c_void_p
    _real_is_connected.argtypes = [ctypes.c_void_p]
    _real_is_connected.restype = ctypes.c_int
    _real_set_exit.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _real_set_exit.restype = None
    try:
        _real_get_exit = _lib.dbus_connection_get_exit_on_disconnect
        _real_get_exit.argtypes = [ctypes.c_void_p]
        _real_get_exit.restype = ctypes.c_int
    except AttributeError:
        _real_get_exit = None
    _libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    _libc.mprotect.restype = ctypes.c_int


def _probe_layout() -> None:
    """Learn the refcount and the shared-connection bit from a live connection."""
    global _refcount_ok, _layout_ok
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not address:
        return
    session = _real_bus_get(0, None)
    if not session:
        return
    _refcount_ok = _learn_refcount(session)
    shared_bit = _shared_bit(session) if _refcount_ok else None
    _set_exit(session, False)
    private = _real_open_private(address.encode(), None)
    private_bit = None
    if private:
        if not _refcount_ok:
            _refcount_ok = _learn_refcount(private)
        private_bit = _shared_bit(private)
        _real_close(private)
        _real_unref(private)
    if shared_bit is True and private_bit is False:
        _layout_ok = True
    if _refcount(session) > 1:
        _real_unref(session)


def _learn_refcount(conn: int) -> bool:
    before = _refcount(conn)
    if before < 1 or before > 1000:
        return False
    _real_ref(conn)
    after = _refcount(conn)
    _real_unref(conn)
    return after == before + 1


def _refcount(conn: int) -> int:
    return int(ctypes.c_int.from_address(conn + _REFCOUNT_OFFSET).value)


def _shared_bit(conn: int) -> bool | None:
    try:
        return bool(ctypes.c_ubyte.from_address(conn + _SHARED_OFFSET).value & 1)
    except Exception:
        return None


def _set_exit(conn: int, enabled: bool) -> None:
    _real_set_exit(conn, 1 if enabled else 0)


def _tag(conn: int, *, shared: bool) -> None:
    if shared:
        _shared.add(conn)
        _private.discard(conn)
    else:
        _private.add(conn)
        _shared.discard(conn)


def _should_intercept(conn: int) -> bool:
    """True when this connection was opened through the hook or the layout is known."""
    return conn in _private or conn in _shared or _layout_ok


def _on_unref(conn) -> None:
    """libatspi's dbus_connection_unref. Do not free a socket that is still connected.

    A last unref of a connected socket is what prints "the last reference on
    a connection was dropped" and then frees the fd under its GLib watch.
    Closing it first avoids that crash and also drops the app: Firefox's
    document never comes back. One extra reference makes this unref a no-op.
    """
    global _held
    if not conn:
        return
    conn = int(conn)
    if conn in _busy():
        _real_unref(conn)
        return
    _busy().add(conn)
    try:
        connected = bool(_real_is_connected(conn))
        refs = _refcount(conn) if _refcount_ok else 2
        if connected and _refcount_ok and refs <= 1 and _should_intercept(conn):
            _held += 1
            _real_ref(conn)
            _real_unref(conn)
            return
        if refs <= 1:
            _private.discard(conn)
            _shared.discard(conn)
        _real_unref(conn)
    finally:
        _busy().discard(conn)


def _on_open_private(address, error):
    global _refcount_ok
    conn = _real_open_private(address, error)
    if conn:
        _set_exit(conn, False)
        _tag(conn, shared=False)
        if not _refcount_ok:
            _refcount_ok = _learn_refcount(conn)
    return conn


def _on_bus_get(bus_type, error):
    global _refcount_ok
    conn = _real_bus_get(bus_type, error)
    if conn:
        _set_exit(conn, False)
        _tag(conn, shared=True)
        if not _refcount_ok:
            _refcount_ok = _learn_refcount(conn)
    return conn


def _busy() -> set[int]:
    found = getattr(_TLS, "busy", None)
    if found is None:
        found = set()
        _TLS.busy = found
    return found


def _patch_libatspi() -> bool:
    """Point libatspi's dbus GOT entries at the hooks. False if none were found."""
    global _unref_hook, _open_hook, _bus_hook
    ctypes.CDLL("libatspi.so.0")
    found = _libatspi_image()
    if not found:
        return False
    base, phdr, phnum = found
    wanted = {
        "dbus_connection_unref": _bind_unref(),
        "dbus_connection_open_private": _bind_open(),
        "dbus_bus_get": _bind_bus(),
    }
    slots = _jump_slots(base, phdr, phnum, set(wanted))
    patched = False
    for name, slot in slots.items():
        hook = wanted[name]
        hook_addr = ctypes.cast(hook, ctypes.c_void_p).value
        current = ctypes.c_uint64.from_address(slot).value
        real = _symbol_address(name)
        if real is None or current != real:
            continue
        if not _poke(slot, int(hook_addr)):
            continue
        _patched_symbols.append(name)
        patched = True
        if name == "dbus_connection_unref":
            _unref_hook = hook
        elif name == "dbus_connection_open_private":
            _open_hook = hook
        elif name == "dbus_bus_get":
            _bus_hook = hook
    return patched and _unref_hook is not None


def _bind_unref():
    global _unref_hook
    if _unref_hook is None:
        _unref_hook = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(_on_unref)
        _keepalive.append(_unref_hook)
    return _unref_hook


def _bind_open():
    global _open_hook
    if _open_hook is None:
        _open_hook = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p)(_on_open_private)
        _keepalive.append(_open_hook)
    return _open_hook


def _bind_bus():
    global _bus_hook
    if _bus_hook is None:
        _bus_hook = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)(_on_bus_get)
        _keepalive.append(_bus_hook)
    return _bus_hook


def _symbol_address(name: str) -> int | None:
    fn = getattr(_lib, name, None)
    if fn is None:
        return None
    return ctypes.cast(fn, ctypes.c_void_p).value


def _poke(addr: int, value: int) -> bool:
    page = os.sysconf("SC_PAGESIZE") or 4096
    start = addr & ~(int(page) - 1)
    if _libc.mprotect(ctypes.c_void_p(start), ctypes.c_size_t(page), 1 | 2) != 0:
        return False
    ctypes.c_uint64.from_address(addr).value = value
    _libc.mprotect(ctypes.c_void_p(start), ctypes.c_size_t(page), 1)
    return True


class _Phdr(ctypes.Structure):
    _fields_ = [
        ("p_type", ctypes.c_uint32),
        ("p_flags", ctypes.c_uint32),
        ("p_offset", ctypes.c_uint64),
        ("p_vaddr", ctypes.c_uint64),
        ("p_paddr", ctypes.c_uint64),
        ("p_filesz", ctypes.c_uint64),
        ("p_memsz", ctypes.c_uint64),
        ("p_align", ctypes.c_uint64),
    ]


class _Dyn(ctypes.Structure):
    _fields_ = [("d_tag", ctypes.c_int64), ("d_val", ctypes.c_uint64)]


class _Rela(ctypes.Structure):
    _fields_ = [
        ("r_offset", ctypes.c_uint64),
        ("r_info", ctypes.c_uint64),
        ("r_addend", ctypes.c_int64),
    ]


class _Sym(ctypes.Structure):
    _fields_ = [
        ("st_name", ctypes.c_uint32),
        ("st_info", ctypes.c_uint8),
        ("st_other", ctypes.c_uint8),
        ("st_shndx", ctypes.c_uint16),
        ("st_value", ctypes.c_uint64),
        ("st_size", ctypes.c_uint64),
    ]


class _Info(ctypes.Structure):
    _fields_ = [
        ("dlpi_addr", ctypes.c_uint64),
        ("dlpi_name", ctypes.c_char_p),
        ("dlpi_phdr", ctypes.c_void_p),
        ("dlpi_phnum", ctypes.c_uint16),
    ]


def _libatspi_image() -> tuple[int, int, int] | None:
    """(load bias, phdr pointer, phdr count) for the loaded libatspi."""
    found: dict[str, int] = {}
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_Info), ctypes.c_size_t, ctypes.c_void_p)

    def _visit(info, _size, _data):
        name = info.contents.dlpi_name or b""
        if b"libatspi" in name:
            found["base"] = int(info.contents.dlpi_addr)
            found["phdr"] = int(info.contents.dlpi_phdr or 0)
            found["phnum"] = int(info.contents.dlpi_phnum)
            return 1
        return 0

    visitor = callback_type(_visit)
    _keepalive.append(visitor)
    _libc.dl_iterate_phdr.argtypes = [callback_type, ctypes.c_void_p]
    _libc.dl_iterate_phdr.restype = ctypes.c_int
    _libc.dl_iterate_phdr(visitor, None)
    if "base" not in found or not found["phdr"]:
        return None
    return found["base"], found["phdr"], found["phnum"]


def _jump_slots(base: int, phdr: int, phnum: int, names: set[str]) -> dict[str, int]:
    """GOT addresses for ``names`` whose current value is the real dbus symbol."""
    phdrs = ctypes.cast(phdr, ctypes.POINTER(_Phdr))
    dyn_addr = 0
    for index in range(phnum):
        if phdrs[index].p_type == 2:  # PT_DYNAMIC
            dyn_addr = base + int(phdrs[index].p_vaddr)
            break
    if not dyn_addr:
        return {}
    tags: dict[int, int] = {}
    for index in range(256):
        item = _Dyn.from_address(dyn_addr + index * ctypes.sizeof(_Dyn))
        if item.d_tag == 0:
            break
        tags[int(item.d_tag)] = int(item.d_val)
    # DT_STRTAB=5, DT_SYMTAB=6, DT_SYMENT=11, DT_JMPREL=23, DT_PLTRELSZ=2
    strtab = tags.get(5, 0)
    symtab = tags.get(6, 0)
    syment = tags.get(11, ctypes.sizeof(_Sym)) or ctypes.sizeof(_Sym)
    jmprel = tags.get(23, 0)
    pltrelsz = tags.get(2, 0)
    if not (strtab and symtab and jmprel and pltrelsz):
        return {}
    slots: dict[str, int] = {}
    count = pltrelsz // ctypes.sizeof(_Rela)
    for index in range(count):
        rela = _Rela.from_address(jmprel + index * ctypes.sizeof(_Rela))
        sym_index = int(rela.r_info) >> 32
        sym = _Sym.from_address(symtab + sym_index * syment)
        raw = ctypes.cast(strtab + int(sym.st_name), ctypes.c_char_p).value or b""
        name = raw.decode("utf-8", "replace")
        if name not in names:
            continue
        slot = base + int(rela.r_offset)
        if not _mapped(base, phdrs, phnum, slot):
            continue
        slots[name] = slot
    return slots


def _mapped(base: int, phdrs, phnum: int, addr: int) -> bool:
    for index in range(phnum):
        if phdrs[index].p_type != 1:
            continue
        start = base + int(phdrs[index].p_vaddr)
        end = start + int(phdrs[index].p_memsz)
        if start <= addr < end:
            return True
    return False
