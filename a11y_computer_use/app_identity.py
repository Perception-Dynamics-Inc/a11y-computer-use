"""App identity for grants and launch resolution.

A grant stored under one name covers the same app asked for under another:
case, a desktop-file id, an executable basename, a WM_CLASS, or a known
alias. ``Files`` is nautilus or thunar, ``Terminal`` is gnome-terminal or
xterm, and ``libreoffice calc`` is soffice. Calc, Writer, and Impress labels
start ``localc``, ``lowriter``, or ``loimpress`` when that program is on
PATH, and otherwise ``soffice`` or ``libreoffice`` with the module flag.
Alias matches are exact tokens. They are not substrings, so ``Files`` does
not match an unrelated name that merely contains those letters.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Sequence

_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({
        "files",
        "nautilus",
        "thunar",
        "nemo",
        "dolphin",
        "pcmanfm",
        "org.gnome.nautilus",
    }),
    frozenset({
        "terminal",
        "gnome-terminal",
        "gnome-terminal-server",
        "xfce4-terminal",
        "xterm",
        "konsole",
        "kitty",
        "alacritty",
        "tilix",
        "org.gnome.terminal",
    }),
    frozenset({
        "libreoffice calc",
        "libreoffice-calc",
        "libreoffice writer",
        "libreoffice-writer",
        "libreoffice impress",
        "libreoffice-impress",
        "libreoffice",
        "soffice",
        "soffice.bin",
        "localc",
        "lowriter",
        "loimpress",
        "calc",
        "writer",
        "impress",
    }),
)

#: Labels a person says that are not the executable. A launch of one of these
#: is started as the granted or installed binary. Calc, Writer, and Impress
#: also select that module's program or flag.
_LABELS = frozenset({
    "files",
    "terminal",
    "libreoffice calc",
    "libreoffice-calc",
    "calc",
    "libreoffice writer",
    "libreoffice-writer",
    "writer",
    "libreoffice impress",
    "libreoffice-impress",
    "impress",
})

# Dedicated wrapper, then the flag passed to soffice or libreoffice.
_OFFICE_MODULES: dict[str, tuple[str, str]] = {
    "calc": ("localc", "--calc"),
    "libreoffice-calc": ("localc", "--calc"),
    "libreoffice calc": ("localc", "--calc"),
    "writer": ("lowriter", "--writer"),
    "libreoffice-writer": ("lowriter", "--writer"),
    "libreoffice writer": ("lowriter", "--writer"),
    "impress": ("loimpress", "--impress"),
    "libreoffice-impress": ("loimpress", "--impress"),
    "libreoffice impress": ("loimpress", "--impress"),
}


def normalize(name: str) -> str:
    """Case-folded name with the surrounding and repeated space removed."""
    return " ".join(str(name).strip().casefold().split())


def tokens(name: str) -> frozenset[str]:
    """The name, its basename, and the desktop id without ``.desktop``."""
    text = normalize(name)
    if not text:
        return frozenset()
    found = {text}
    base = text.replace("\\", "/").rsplit("/", 1)[-1]
    found.add(base)
    if base.endswith(".desktop"):
        found.add(base[: -len(".desktop")])
    if base.endswith(".bin"):
        found.add(base[: -len(".bin")])
    return frozenset(item for item in found if item)


def identity_keys(name: str) -> frozenset[str]:
    """Every token that refers to the same app as ``name``."""
    own = tokens(name)
    if not own:
        return frozenset()
    for group in _GROUPS:
        if own & group:
            return frozenset(own | group)
    return own


def shares_alias(left: str, right: str) -> bool:
    """True when both names sit in the same alias group."""
    left_keys = tokens(left)
    right_keys = tokens(right)
    if not left_keys or not right_keys:
        return False
    for group in _GROUPS:
        if (left_keys & group) and (right_keys & group):
            return True
    return False


def same_app(left: str, right: str) -> bool:
    """True when the two names are the same app, including by alias."""
    return bool(identity_keys(left) & identity_keys(right))


def matching_stored_key(name: str, stored: Sequence[str]) -> str | None:
    """The stored key that names ``name``.

    An exact stored string wins. Otherwise the first stored key in the same
    identity set (case, desktop id, or alias group) wins.
    """
    if name in stored:
        return name
    wanted = identity_keys(name)
    if not wanted:
        return None
    for key in stored:
        if identity_keys(key) & wanted:
            return key
    return None


def load_desktop_entries() -> list[dict[str, str]]:
    """Installed desktop files as ``id``, ``name``, ``exec``, and ``wm_class``.

    This reads files only. It does not open a display. A missing applications
    directory is an empty list.
    """
    try:
        from a11y_computer_use.drivers._linux_system import _application_dirs
    except Exception:
        return []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in _application_dirs():
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if not filename.endswith(".desktop"):
                    continue
                path = os.path.join(dirpath, filename)
                if path in seen:
                    continue
                seen.add(path)
                entry = _read_desktop_entry(path, filename)
                if entry is not None:
                    found.append(entry)
    return found


def _read_desktop_entry(path: str, filename: str) -> dict[str, str] | None:
    try:
        if os.path.getsize(path) > 262144:
            return None
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    name = ""
    program = ""
    wm_class = ""
    in_entry = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_entry = stripped == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key == "Name" and not name:
            name = value.strip()
        elif key == "StartupWMClass" and not wm_class:
            wm_class = value.strip()
        elif key == "Exec" and not program:
            program = _exec_basename(value.strip())
    stem = filename[: -len(".desktop")]
    if not any((stem, name, program, wm_class)):
        return None
    return {"id": stem, "name": name, "exec": program, "wm_class": wm_class}


def _exec_basename(exec_line: str) -> str:
    try:
        parts = shlex.split(exec_line, posix=True)
    except ValueError:
        parts = exec_line.split()
    program = ""
    for part in parts:
        if part and not (part.startswith("%") and len(part) <= 2):
            program = part
            break
    return os.path.basename(program) if program else ""


class LaunchResolution:
    """How to start ``name`` and which grant key to check.

    ``argv`` is set for a Calc, Writer, or Impress label: the dedicated
    wrapper, or ``soffice``/``libreoffice`` plus the module flag. Other
    launches leave it unset and start ``launch_name`` alone.
    """

    def __init__(
        self,
        launch_name: str,
        gate_key: str | None,
        *,
        resolved: bool,
        argv: tuple[str, ...] | None = None,
    ) -> None:
        self.launch_name = launch_name
        self.gate_key = gate_key
        self.resolved = resolved
        self.argv = argv


def _lookup_program(lookup: Callable[[str], str | None], name: str) -> str | None:
    try:
        found = lookup(name)
    except OSError:
        return None
    return found or None


def _office_host(lookup: Callable[[str], str | None], hosts: Sequence[str]) -> str | None:
    """``soffice`` or ``libreoffice`` for a module flag.

    A program on PATH wins. A host already chosen for this launch is used
    when the lookup finds nothing, so a grant or desktop exec of ``soffice``
    still carries ``--calc`` in a test that stubs PATH.
    """
    named: list[str] = []
    for host in hosts:
        base = os.path.basename(normalize(host))
        if base in {"soffice", "libreoffice"} and base not in named:
            named.append(base)
    for candidate in ("soffice", "libreoffice"):
        found = _lookup_program(lookup, candidate)
        if found:
            return found
        if candidate in named:
            return candidate
    return None


def _office_module_argv(
    name: str,
    lookup: Callable[[str], str | None],
    hosts: Sequence[str],
) -> tuple[str, ...] | None:
    """Argv for a Calc, Writer, or Impress label, or None for any other name.

    ``localc`` (and the writer and impress wrappers) already pass the module
    flag. Otherwise the command is ``soffice`` or ``libreoffice`` plus
    ``--calc``, ``--writer``, or ``--impress``.
    """
    spec = _OFFICE_MODULES.get(normalize(name))
    if spec is None:
        return None
    dedicated, flag = spec
    found = _lookup_program(lookup, dedicated)
    if found:
        return (found,)
    host = _office_host(lookup, hosts)
    if host is None:
        return None
    return (host, flag)


def resolve_launch(
    name: str,
    *,
    granted: Sequence[str] = (),
    installed_bundle: str | None = None,
    running: str | None = None,
    entries: Sequence[dict] | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
) -> LaunchResolution:
    """Resolve ``name`` to a launch id and a grant key.

    ``resolved`` is false when nothing on PATH, no desktop file, no installed
    bundle, no running comm, and no grant names this app. The caller reports
    that as ``app_not_found`` and lists ``granted``, instead of asking for a
    permission grant of the raw string.
    """
    import shutil

    lookup = path_lookup or shutil.which
    candidates: list[str] = [name]
    if installed_bundle:
        candidates.append(installed_bundle)
    if running:
        candidates.append(running)

    on_path = False
    if name and os.sep not in name and not name.endswith(".desktop"):
        try:
            on_path = bool(lookup(name))
        except OSError:
            on_path = False
    elif name and os.sep in name:
        try:
            on_path = bool(lookup(name)) or os.path.isfile(name)
        except OSError:
            on_path = os.path.isfile(name)
    if on_path:
        candidates.append(os.path.basename(name))

    matched: list[dict] = []
    catalog = list(entries) if entries is not None else load_desktop_entries()
    for entry in catalog:
        fields = (
            str(entry.get("id") or ""),
            str(entry.get("name") or ""),
            str(entry.get("exec") or ""),
            str(entry.get("wm_class") or ""),
        )
        if any(field and same_app(name, field) for field in fields):
            matched.append(entry)
            candidates.extend(field for field in fields if field)

    hit = matching_stored_key(name, granted)
    if hit is None:
        for candidate in candidates:
            hit = matching_stored_key(candidate, granted)
            if hit is not None:
                break

    launch_name = name
    label = normalize(name) in _LABELS
    if label:
        concrete = ""
        if hit and normalize(hit) not in _LABELS:
            concrete = hit
        if not concrete and running and normalize(running) not in _LABELS:
            concrete = running
        if not concrete:
            for entry in matched:
                exe = str(entry.get("exec") or "")
                if exe and (hit is None or same_app(exe, hit)):
                    concrete = exe
                    break
        if not concrete and matched:
            concrete = str(matched[0].get("exec") or matched[0].get("id") or "")
        if concrete:
            launch_name = concrete
    elif hit and normalize(hit) == normalize(name) and hit != name and not on_path:
        launch_name = hit

    resolved = bool(on_path or matched or installed_bundle or running or hit)
    gate_key = hit or (launch_name if resolved else None)
    hosts = [launch_name]
    if hit:
        hosts.append(hit)
    if running:
        hosts.append(running)
    for entry in matched:
        exe = str(entry.get("exec") or "")
        if exe:
            hosts.append(exe)
    argv = _office_module_argv(name, lookup, hosts)
    if argv:
        launch_name = os.path.basename(argv[0])
        resolved = True
        if not gate_key:
            gate_key = launch_name
    return LaunchResolution(launch_name, gate_key, resolved=resolved, argv=argv)
