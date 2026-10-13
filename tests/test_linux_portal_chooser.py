"""Portal observations are bound to one requesting app's active chooser."""
from types import SimpleNamespace

import pytest

from a11y_computer_use.drivers import _atspi, _linux_system
from a11y_computer_use.schema import Scope


class Accessible:
    def __init__(self, name, children=(), pid=300, active=True):
        self.name, self.children, self.pid, self.active = name, children, pid, active

    def get_name(self):
        return self.name

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_state_set(self):
        return SimpleNamespace(contains=lambda state: self.active)


@pytest.mark.parametrize("scope", [Scope.APP, Scope.WINDOW])
@pytest.mark.parametrize("duplicate", [False, True])
def test_portal_root_is_one_dialog_never_shared_backend(monkeypatch, scope, duplicate):
    chooser = Accessible("Open File")
    sibling = Accessible("Other app's secrets")
    frames = [chooser, sibling]
    if duplicate:
        frames.append(Accessible("Open File"))
    backend = Accessible("xdg-desktop-portal-gtk", frames)
    desktop = Accessible("desktop", [backend])
    row = {"window_id": 30, "pid": 300, "app": "chrome", "title": "Open File"}
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(
        get_desktop=lambda index: desktop, StateType=SimpleNamespace(ACTIVE=1)))
    monkeypatch.setattr(_atspi, "pid_of", lambda node: node.pid)
    monkeypatch.setattr(_linux_system, "pids_matching", lambda app: {200})
    monkeypatch.setattr(_linux_system, "active_window", lambda: row)
    monkeypatch.setattr(_linux_system, "windows", lambda: [row])
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "xdg-desktop-portal-gtk")
    assert _atspi.find_root("chrome", scope) is (None if duplicate else chooser)
    monkeypatch.setattr(_atspi, "_application_of", lambda node: backend)
    assert _atspi.showing_toplevels(chooser) == [chooser]


def test_portal_pid_cannot_bypass_requesting_app_pointer_recheck(monkeypatch):
    from a11y_computer_use.server import _linux_point_matches_snapshot_pid
    from a11y_computer_use.schema import Point

    runtime = SimpleNamespace(driver=SimpleNamespace(name="linux"), _current=SimpleNamespace(pid=300))
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: 300)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "xdg-desktop-portal-gtk")
    assert not _linux_point_matches_snapshot_pid(runtime, Point(0, 20, 20))
