"""Platform driver selection — one backend per OS behind a shared contract.

    from a11y_computer_use.drivers import get_driver
    driver = get_driver()            # the current OS's backend
    driver = get_driver("windows")   # force one (e.g. for a contract test)

The Runtime holds a driver and routes every platform operation through it, so
supporting a new OS is "implement `Driver`", never "touch the core".
"""

from __future__ import annotations

import sys

from a11y_computer_use.drivers.base import Driver


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
    """Return the backend for ``name`` (default: ``$A11Y_COMPUTER_USE_DRIVER`` or the
    current OS).

    Backends are imported lazily so `drivers` stays import-safe on every OS
    (the macOS backend pulls in pyobjc; the Windows one will pull in UIA). The
    ``A11Y_COMPUTER_USE_DRIVER`` override lets ``a11y_computer_use serve`` run the
    OS-independent ``browser`` backend without a code change.
    """
    import os

    target = name or os.environ.get("A11Y_COMPUTER_USE_DRIVER") or current_platform()
    if target == "macos":
        from a11y_computer_use.drivers.macos import MacOSDriver

        return MacOSDriver()
    if target == "windows":
        from a11y_computer_use.drivers.windows import WindowsDriver

        return WindowsDriver()
    if target == "linux":
        from a11y_computer_use.drivers.linux import LinuxDriver

        return LinuxDriver()
    if target == "browser":
        # Cross-platform, OS-independent backend: a11y-first control of a running
        # Chromium over the DevTools Protocol. Selected explicitly, never by OS.
        from a11y_computer_use.drivers.browser import BrowserDriver

        return BrowserDriver()
    raise NotImplementedError(
        f"no a11y-computer-use driver for platform {target!r}; supported: macos, windows, linux, browser"
    )


__all__ = ["Driver", "get_driver", "current_platform"]
