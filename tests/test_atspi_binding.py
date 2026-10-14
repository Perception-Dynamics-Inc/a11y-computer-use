"""Lazy AT-SPI binding reuse, without GI or an accessibility bus."""

from types import ModuleType, SimpleNamespace
import sys

import pytest

from a11y_computer_use.drivers import _atspi, _dbus_guard


@pytest.fixture
def binding(monkeypatch):
    calls = []
    api = SimpleNamespace(
        init=lambda: calls.append("init") or 0,
        set_timeout=lambda *args: calls.append(("timeout", args)),
        is_initialized=lambda: True,
        exit=lambda: calls.append("exit"),
    )
    gi = ModuleType("gi")
    gi.require_version = lambda *args: calls.append(("require", args))
    repository = ModuleType("gi.repository")
    repository.Atspi = api
    gi.repository = repository
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setattr(_atspi, "_inited", False)
    monkeypatch.setattr(_atspi, "_atspi_binding", None)
    monkeypatch.setattr(_atspi, "_safe", lambda fn, *args, **kwargs: fn())
    monkeypatch.setattr(_dbus_guard, "install", lambda: calls.append("guard"))
    return gi, api, calls


def test_repeated_native_probes_resolve_the_typelib_only_once(binding):
    """Calc's repeated state/geometry probes must not enumerate GI versions."""
    _gi, api, calls = binding
    for _ in range(10000):
        assert _atspi._atspi() is api
    assert calls == [("require", ("Atspi", "2.0")), "guard", "init", ("timeout", (300, 0))]


def test_binding_reuse_preserves_bus_reinitialization(binding):
    """The real reconnect path must exit/init and restore the same timeout."""
    _gi, api, calls = binding
    assert _atspi._atspi() is api
    calls.clear()
    assert _dbus_guard.reconnect()
    assert _atspi._atspi() is api
    assert calls == ["exit", ("require", ("Atspi", "2.0")), "guard", "init", ("timeout", (300, 0))]


def test_failed_typelib_resolution_is_retried(binding):
    """A missing binding must not become a permanent cached failure."""
    gi, api, calls = binding
    require = gi.require_version

    def unavailable(*args):
        raise ValueError("typelib unavailable")

    gi.require_version = unavailable
    with pytest.raises(ImportError, match="typelib is not available"):
        _atspi._atspi()
    assert _atspi._atspi_binding is None
    assert not _atspi._inited
    gi.require_version = require
    assert _atspi._atspi() is api
    assert calls == [("require", ("Atspi", "2.0")), "guard", "init", ("timeout", (300, 0))]


def test_missing_gi_still_reports_the_dependency_error(binding, monkeypatch):
    """Dependency isolation must also work after another probe initialized GI."""
    _gi, api, calls = binding
    assert _atspi._atspi() is api
    calls.clear()
    monkeypatch.setitem(sys.modules, "gi", None)
    with pytest.raises(ImportError, match="PyGObject is not installed"):
        _atspi._atspi()
    assert calls == []
