"""The platform driver seam — selection + protocol conformance.

The macOS backend's real behavior is covered by the observe/act/server suites
(it delegates to those modules, which those tests already exercise); here we
pin the seam itself: OS selection, that both backends satisfy the `Driver`
contract, and that Windows is an honest, mapped stub.
"""

from __future__ import annotations

import sys

import pytest

from computeruse import drivers
from computeruse.drivers.base import Driver
from computeruse.drivers.windows import WindowsDriver  # schema-only; safe on any OS

IS_MACOS = sys.platform == "darwin"

# The macOS backend imports pyobjc (act/capture), so only import it on macOS —
# this file must collect on a Windows CI runner too.
if IS_MACOS:
    from computeruse.drivers.macos import MacOSDriver

_METHODS = (
    "ensure_trusted", "snapshot", "resolve_ref", "press_element", "scroll_into_view",
    "click", "drag", "scroll", "type_text", "key_chord", "wait_for",
    "screenshot", "zoom_region", "frontmost_app", "app_at_point", "running_apps",
    "launch_app", "activate_app", "windows", "read_clipboard", "write_clipboard",
)


_BACKENDS = [WindowsDriver] + ([MacOSDriver] if IS_MACOS else [])


def test_get_driver_selects_the_current_os() -> None:
    d = drivers.get_driver()
    assert d.name == drivers.current_platform()


def test_get_driver_by_name() -> None:
    assert drivers.get_driver("windows").name == "windows"  # safe on any OS
    if IS_MACOS:  # importing the macOS backend needs pyobjc
        assert drivers.get_driver("macos").name == "macos"


def test_get_driver_unknown_platform_raises() -> None:
    with pytest.raises(NotImplementedError):
        drivers.get_driver("plan9")


def test_current_platform_is_a_known_id() -> None:
    plat = drivers.current_platform()
    assert plat == ("macos" if IS_MACOS else plat)
    assert isinstance(plat, str) and plat


@pytest.mark.parametrize("cls", _BACKENDS)
def test_backend_satisfies_the_protocol(cls) -> None:
    d = cls()
    assert isinstance(d, Driver)  # runtime_checkable structural conformance
    for method in _METHODS:
        assert callable(getattr(d, method)), f"{cls.__name__} missing {method}"


def test_windows_backend_stubs_name_their_native_api() -> None:
    # snapshot is now implemented (UIA → the shared engine); the input/capture
    # ops are still stubs and each names the native API it maps to.
    d = WindowsDriver()
    with pytest.raises(NotImplementedError) as ei:
        d.type_text("hi")
    assert "SendInput" in str(ei.value)
    with pytest.raises(NotImplementedError) as ei:
        d.screenshot()
    assert "DXGI" in str(ei.value)
    # the permission probe is a benign no-op (Windows uses integrity, not TCC)
    assert d.ensure_trusted() is None
