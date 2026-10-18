"""Native identity, not matching labels, proves Chrome's twin bubble (#899)."""
from dataclasses import replace

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import ComputerUseError, Scope
from tests.test_linux_synthetic import _FakeAccessor, _geometry, _node


def _snapshot(monkeypatch, *, difference=None, merged=False):
    identities = {}

    def bubble(copy):
        buttons = []
        for i, title in enumerate(("Allow while visiting the site", "Allow this time", "Never allow")):
            button = _node("AXButton", title, actions=("AXPress",),
                           stable_id=f"view_{i}" if i else None,
                           pos=(150., 197. + 48 * i), size=(280., 40.))
            identity = (":1.42", f"/org/a11y/atspi/accessible/{150 + i}")
            if copy and difference == "object":
                identity = (identity[0], identity[1] + "0")
            if copy and difference == "bus":
                identity = (":1.43", identity[1])
            if copy and difference == "unknown":
                identity = None
            if copy and difference == "bounds":
                button = (replace(button[0], position=(151., 197. + 48 * i)), [])
            if copy and difference == "state":
                button = (replace(button[0], enabled=False), [])
            if copy and difference == "missing_bounds":
                button = (replace(button[0], position=None, size=None), [])
            identities[id(button)] = identity
            buttons.append(button)
        if copy and difference == "new_child":
            extra = _node("AXButton", "Extra", actions=("AXPress",))
            identities[id(extra)] = (":1.42", "/org/a11y/atspi/accessible/999")
            buttons.append(extra)
        dialog = _node("AXDialog", "http://localhost wants to: Know your location",
                       pos=(130., 75.), size=(320., 278.), children=buttons)
        identities[id(dialog)] = (":1.42", "/org/a11y/atspi/accessible/81"
                                  if copy and difference == "dialog_object"
                                  else "/org/a11y/atspi/accessible/80")
        return dialog

    first, second = bubble(False), bubble(True)
    frame = _node("AXWindow", "Chrome", children=[first])
    monkeypatch.setattr(_atspi, "_atspi_wrapper_key", lambda node: identities.get(id(node)))

    def build(root):
        return observe.build_snapshot(root, _FakeAccessor(), scope=Scope.WINDOW,
                                      app="chrome", pid=42, geometry=_geometry())

    if merged:
        return observe.merge_window_snapshots([build(frame), build(second)],
                                             scope=Scope.WINDOW, app="chrome", pid=42)
    return build(_node("AXApplication", "Chrome", children=[frame, second]))


@pytest.mark.parametrize("merged", [False, True], ids=["application-tree", "window-merge"])
def test_native_twin_dialog_has_one_find_match_and_resolves_both_old_refs(monkeypatch, merged):
    before = _snapshot(monkeypatch, merged=merged)
    live = _snapshot(monkeypatch, merged=merged)
    after = _atspi.deduplicate_dialog_exposures(live)
    assert len([el for el in after.elements if el.role == "AXDialog"]) == 1
    for title in ("Allow while visiting the site", "Allow this time", "Never allow"):
        old = observe.find_elements(before, text=title, role="button")
        found = observe.find_elements(after, text=title, role="button")
        assert len(old) == 2
        assert len(found) == 1
        for anchor in old:
            resolved = observe.rematch_ref(before, anchor.ref, after)
            assert resolved == found[0]
            assert observe.ax_handle_for(after.snapshot_id, resolved.ref) is not None
        assert after.element(found[0].parent).role == "AXDialog"
    assert _atspi.deduplicate_dialog_exposures(after) == after


@pytest.mark.parametrize("difference", ["object", "dialog_object", "bus", "unknown", "bounds", "missing_bounds", "new_child", "state"])
def test_unproven_or_changed_twins_are_not_collapsed(monkeypatch, difference):
    before = _snapshot(monkeypatch, difference=difference)
    assert _atspi.deduplicate_dialog_exposures(before) is before
    if difference in {"object", "bus", "unknown", "new_child"}:
        anchor = observe.find_elements(before, text="Allow this time", role="button")[0]
        with pytest.raises(ComputerUseError) as exc:
            observe.rematch_ref(before, anchor.ref, before)
        assert exc.value.detail["reason"] == "ambiguous"


def test_driver_publishes_deduplicated_snapshot(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from a11y_computer_use.drivers import linux, _linux_system

    source = _snapshot(monkeypatch)
    root = object()
    monkeypatch.setattr(linux, "_hint_pid", lambda app: 42)
    monkeypatch.setattr(_atspi, "app_reply_watch", lambda *args: nullcontext(SimpleNamespace(pid=42)))
    monkeypatch.setattr(_atspi, "chromium_snapshot_root", lambda node: node)
    monkeypatch.setattr(_atspi, "pid_of", lambda node: 42)
    monkeypatch.setattr(_atspi, "primary_geometry", _geometry)
    monkeypatch.setattr(_atspi, "note_browser_bar", lambda *args: None)
    monkeypatch.setattr(_atspi.ATSPIAccessor, "refresh_visible", lambda *args: None)
    monkeypatch.setattr(observe, "build_snapshot", lambda *args, **kwargs: source)
    monkeypatch.setattr(_linux_system, "window_cover_rows", lambda: [])
    driver = linux.LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    result = driver._snapshot_root("chrome", root, Scope.APP)
    assert len(observe.find_elements(result, text="Allow this time", role="button")) == 1


@pytest.mark.parametrize("retained_copy", [0, 1])
def test_window_document_id_and_absent_application_id_resolve_live_copy(monkeypatch, retained_copy):
    before = _snapshot(monkeypatch, merged=True)
    dialog_refs = [e.ref for e in before.elements if e.role == "AXDialog"]
    first_refs = {dialog_refs[0]}
    for item in before.elements:
        if item.parent in first_refs:
            first_refs.add(item.ref)
    before = replace(before, elements=tuple(
        replace(item, document_id=(
            "title:PERM G:asking - Permission requested, press F6 to respond - Google Chrome"
            if item.ref in first_refs else None
        )) for item in before.elements
    ))
    published = _atspi.deduplicate_dialog_exposures(before)
    assert len([e for e in published.elements if e.role == "AXDialog"]) == 1
    # Dispatch must use fresh handles even if only the other exposure survives.
    fresh = _snapshot(monkeypatch, merged=True)
    fresh = replace(fresh, elements=tuple(
        replace(item, document_id=before.element(item.ref).document_id)
        for item in fresh.elements
        if (item.ref in first_refs) == (retained_copy == 0)
    ))
    for title in ("Allow this time", "Never allow"):
        anchor = observe.find_elements(published, text=title, role="button")[0]
        resolved = observe.rematch_ref(published, anchor.ref, fresh)
        assert resolved in fresh.elements
        assert observe.ax_handle_for(fresh.snapshot_id, resolved.ref) is not None
        assert observe.ax_handle_for(fresh.snapshot_id, resolved.ref) is not observe.ax_handle_for(published.snapshot_id, anchor.ref)


def test_conflicting_nonempty_document_ids_keep_both_dialogs(monkeypatch):
    before = _snapshot(monkeypatch, merged=True)
    before = replace(before, elements=tuple(
        replace(item, document_id=f"document:{item.ref}") for item in before.elements
    ))
    assert _atspi.deduplicate_dialog_exposures(before) is before
