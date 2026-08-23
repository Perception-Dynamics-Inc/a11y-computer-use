"""Platform driver selection — one backend per OS behind a shared contract.

    from computeruse.drivers import get_driver
    driver = get_driver()            # the current OS's backend
    driver = get_driver("windows")   # force one (e.g. for a contract test)

The Runtime holds a driver and routes every platform operation through it, so
supporting a new OS is "implement `Driver`", never "touch the core".
"""

from __future__ import annotations

import sys

from computeruse.drivers.base import Driver


def current_platform() -> str:
    """"macos" | "windows" | "linux" | the raw ``sys.platform`` otherwise."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def get_driver(name: str | None = None) -> Driver:
    """Return the backend for ``name`` (default: the current OS).

    Backends are imported lazily so `drivers` stays import-safe on every OS
    (the macOS backend pulls in pyobjc; the Windows one will pull in UIA).
    """
    target = name or current_platform()
    if target == "macos":
        from computeruse.drivers.macos import MacOSDriver

        return MacOSDriver()
    if target == "windows":
        from computeruse.drivers.windows import WindowsDriver

        return WindowsDriver()
    if target == "linux":
        from computeruse.drivers.linux import LinuxDriver

        return LinuxDriver()
    raise NotImplementedError(
        f"no computerUse driver for platform {target!r}; supported: macos, windows, linux"
    )


__all__ = ["Driver", "get_driver", "current_platform"]
