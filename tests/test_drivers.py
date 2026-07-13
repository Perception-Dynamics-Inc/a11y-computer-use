"""The platform driver seam — selection + protocol conformance.

The macOS backend's real behavior is covered by the observe/act/server suites
(it delegates to those modules, which those tests already exercise); here we
pin the seam itself: OS selection, that both backends satisfy the `Driver`
contract, and that Windows is an honest, mapped stub.
"""

from __future__ import annotations

import pytest

from computeruse import drivers
from computeruse.drivers.base import Driver
from computeruse.drivers.macos import MacOSDriver
from computeruse.drivers.windows import WindowsDriver

_METHODS = (
    "ensure_trusted", "snapshot", "resolve_ref", "press_element", "scroll_into_view",
    "click", "drag", "scroll", "type_text", "key_chord", "wait_for",
    "screenshot", "zoom_region", "frontmost_app", "app_at_point", "running_apps",
    "launch_app", "activate_app", "windows", "read_clipboard", "write_clipboard",
)


def test_get_driver_selects_macos_here() -> None:
    d = drivers.get_driver()
    assert d.name == "macos"
    assert isinstance(d, MacOSDriver)


def test_get_driver_by_name() -> None:
    assert drivers.get_driver("macos").name == "macos"
    assert drivers.get_driver("windows").name == "windows"


def test_get_driver_unknown_platform_raises() -> None:
    with pytest.raises(NotImplementedError):
        drivers.get_driver("plan9")


def test_current_platform_is_stable() -> None:
    assert drivers.current_platform() == "macos"  # this suite runs on macOS


@pytest.mark.parametrize("cls", [MacOSDriver, WindowsDriver])
def test_backend_satisfies_the_protocol(cls) -> None:
    d = cls()
    assert isinstance(d, Driver)  # runtime_checkable structural conformance
    for method in _METHODS:
        assert callable(getattr(d, method)), f"{cls.__name__} missing {method}"


def test_windows_backend_is_an_honest_mapped_stub() -> None:
    d = WindowsDriver()
    # each unimplemented op names the native API it maps to
    with pytest.raises(NotImplementedError) as ei:
        d.snapshot(None, "app")
    assert "UIAutomation" in str(ei.value)
    with pytest.raises(NotImplementedError) as ei:
        d.type_text("hi")
    assert "SendInput" in str(ei.value)
    # the permission probe is a benign no-op (Windows uses integrity, not TCC)
    assert d.ensure_trusted() is None
