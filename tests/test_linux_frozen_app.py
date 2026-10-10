"""A stopped application fails the snapshot quickly, and the desktop keeps the rest.

Hermetic: no AT-SPI bus. The live SIGSTOP/SIGCONT case is
``test_linux_frozen_gtk_app_is_app_not_responding``.
"""

from __future__ import annotations

import time

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, RawNode, build_snapshot
from a11y_computer_use.schema import ComputerUseError, Display, ErrorCode, Scope
from a11y_computer_use.server import error_text


def _geometry():
    return (DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),)


class _Acc:
    def read(self, node):
        return node[0]

    def children(self, node):
        return node[1]


def _tree(title: str):
    button = (
        RawNode(
            role="AXButton", title="Save", position=(10.0, 10.0), size=(80.0, 30.0),
            actions=("AXPress",),
        ),
        [],
    )
    return (
        RawNode(role="AXWindow", title=title, position=(0.0, 0.0), size=(400.0, 300.0)),
        [button],
    )


def _snap(title: str, app: str, pid: int):
    return build_snapshot(
        _tree(title), _Acc(), scope=Scope.APP, app=app, pid=pid, geometry=_geometry(),
    )


def _slow():
    time.sleep(0.26)
    raise RuntimeError("unanswered")


def test_two_unanswered_calls_name_the_app_and_pid() -> None:
    started = time.monotonic()
    with pytest.raises(ComputerUseError) as caught:
        with _atspi.app_reply_watch("mousepad", 4242):
            _atspi._safe(_slow, default=None)
            _atspi._safe(_slow, default=None)
            _atspi._safe(_slow, default=None)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, elapsed
    err = caught.value
    assert err.code is ErrorCode.APP_NOT_RESPONDING
    assert err.message == "mousepad did not answer accessibility queries (pid 4242)"
    assert err.detail["app"] == "mousepad"
    assert err.detail["pid"] == 4242
    rendered = error_text(err)
    assert "screen_text" not in rendered
    assert "custom-drawn" not in rendered
    assert rendered.startswith("app_not_responding:")


def test_one_slow_call_does_not_fail_and_an_unwatched_call_stays_a_default() -> None:
    with _atspi.app_reply_watch("gedit", 7) as watch:
        assert _atspi._safe(_slow, default="fallback") == "fallback"
        assert watch.slow_calls == 1
    assert _atspi._safe(_slow, default="fallback") == "fallback"


def test_desktop_collection_keeps_apps_that_answer() -> None:
    calls = {"mousepad": 0}

    def mousepad():
        calls["mousepad"] += 1
        _atspi._safe(_slow, default=None)
        calls["mousepad"] += 1
        _atspi._safe(_slow, default=None)
        calls["mousepad"] += 1
        return "mousepad-tree"

    started = time.monotonic()
    kept = _atspi.collect_app_snapshots([
        ("gedit", 11, lambda: "gedit-tree"),
        ("mousepad", 22, mousepad),
        ("firefox", 33, lambda: "firefox-tree"),
    ])
    assert time.monotonic() - started < 2.0
    assert kept == ["gedit-tree", "firefox-tree"]
    assert calls["mousepad"] == 2


def test_desktop_snapshot_returns_the_other_app_and_a_frozen_target_errors(monkeypatch) -> None:
    from a11y_computer_use.drivers.linux import LinuxDriver

    gedit_root = object()
    mouse_root = object()
    gedit = _snap("gedit", "gedit", 11)
    firefox = _snap("firefox", "firefox", 33)

    def roots():
        return _atspi.DesktopApplications([("gedit", gedit_root), ("mousepad", mouse_root)], 0)

    def build(root, accessor, **kwargs):
        del accessor, kwargs
        if root is mouse_root:
            _atspi._safe(_slow, default=None)
            _atspi._safe(_slow, default=None)
            raise AssertionError("the frozen walk continued")
        if root is gedit_root:
            return gedit
        return firefox

    monkeypatch.setattr(_atspi, "desktop_application_roots", roots)
    monkeypatch.setattr(_atspi, "pid_of", lambda root: 22 if root is mouse_root else 11)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    monkeypatch.setattr(observe, "build_snapshot", build)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_root_for_snapshot", lambda app, scope: (app, mouse_root))

    started = time.monotonic()
    with pytest.raises(ComputerUseError) as caught:
        driver.snapshot(Scope.WINDOW, "mousepad")
    assert time.monotonic() - started < 2.0
    assert caught.value.code is ErrorCode.APP_NOT_RESPONDING
    assert caught.value.detail["app"] == "mousepad"
    assert caught.value.detail["pid"] == 22
    assert "screen_text" not in error_text(caught.value)

    desktop = driver.snapshot(Scope.DISPLAY, "")
    titles = [el.title for el in desktop.elements]
    assert "gedit" in titles
    assert "Save" in titles
    assert desktop.scope is Scope.DISPLAY
    assert desktop.app is None
    refs = [el.ref for el in desktop.elements]
    assert refs == [f"e{i}" for i in range(1, len(refs) + 1)]
    assert len(refs) == len(set(refs))


def test_linux_empty_snapshot_does_not_hint_screen_text(tmp_path, monkeypatch) -> None:
    from a11y_computer_use import safety, server
    from a11y_computer_use.schema import Bounds, Element, Snapshot

    empty = Snapshot(
        snapshot_id="snap-empty",
        scope=Scope.WINDOW,
        app="mousepad",
        pid=1,
        created_at=0.0,
        displays=(Display(0, 1280, 800, 1.0, True),),
        elements=(
            Element(
                ref="e1", role="AXWindow", title="mousepad", value=None,
                bounds=Bounds(0, 0, 0, 100, 100), snapshot_id="snap-empty",
            ),
        ),
    )

    class Driver:
        name = "linux"

        def ensure_trusted(self) -> None:
            return None

        def snapshot(self, scope, app):
            del scope, app
            return empty

    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("mousepad", safety.Tier.READ)
    runtime = server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=Driver(),
    )
    monkeypatch.setattr(runtime, "_resolve_app", lambda app: (None, app))
    text = runtime.desktop_snapshot("mousepad")
    assert "mousepad" in text
    assert "screen_text" not in text
    assert "custom-drawn" not in text


def test_rematch_after_a_recovered_tree_still_finds_the_button() -> None:
    """The freeze error does not change stale-ref matching.

    A button that is gone is still stale_ref. The same button in a later
    tree still resolves.
    """
    before = _snap("notes", "mousepad", 5)
    button = next(el for el in before.elements if el.title == "Save")
    gone = _snap("notes", "mousepad", 5)
    # Drop the button by rebuilding a window-only tree under the same anchors
    # the matcher will reject.
    window_only = build_snapshot(
        (RawNode(role="AXWindow", title="notes", position=(0.0, 0.0), size=(400.0, 300.0)), []),
        _Acc(),
        scope=Scope.APP,
        app="mousepad",
        pid=5,
        geometry=_geometry(),
    )
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(before, button.ref, window_only)
    assert caught.value.code is ErrorCode.STALE_REF
    found = observe.rematch_ref(before, button.ref, gone)
    assert found.title == "Save"
