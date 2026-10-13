"""scroll_to_find must not type the query unless a find bar is real.

Synthetic snapshots and a fake driver. Not a live Swing window and not a
live Chrome page. A click grant audits a denial and sends no key. A Swing
table is not a find host, so a full grant sends no key either. A browser
that does not show the bar after Ctrl+F does not receive the query. A
verified bar is typed, audited, and closed. An editor whose value only
grew by the query is not the match.
"""

from __future__ import annotations

import json
from pathlib import Path

from a11y_computer_use import safety, server
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.schema import Bounds, Display, Element, Scope, Snapshot

_APP = "swingapp"
_QUERY = "ROW-150"


def _element(ref: str, role: str, title: str, value: str | None, y: int) -> Element:
    return Element(
        ref=ref, role=role, title=title, value=value,
        bounds=Bounds(0, 40, y, 160, 22), snapshot_id="s", parent="table",
    )


def _table(*rows: Element) -> Snapshot:
    window = Element(
        ref="win", role="AXWindow", title="cuajava", value=None,
        bounds=Bounds(0, 0, 0, 1000, 800), snapshot_id="s",
    )
    table = Element(
        ref="table", role="AXTable", title="Rows", value=None,
        bounds=Bounds(0, 20, 40, 600, 400), snapshot_id="s", parent="win",
    )
    return Snapshot(
        snapshot_id="s", scope=Scope.WINDOW, app=_APP, pid=7, created_at=0.0,
        displays=(Display(0, 1000, 800, 1.0, True),),
        elements=(window, table, *rows),
    )


class _FindDriver:
    """Records find keystrokes. ``resolves_apps`` keeps the test off the OS."""

    resolves_apps = True
    name = "test"

    def __init__(self, snap: Snapshot, *, supports: bool, bar_after_ctrl_f: bool = False) -> None:
        self.snap = snap
        self.supports = supports
        self.bar_after_ctrl_f = bar_after_ctrl_f
        self.bar = False
        self.revealed = False
        self.chords: list[str] = []
        self.typed: list[str] = []
        self.scrolls: list[int] = []
        self.focus_checks = 0
        self.support_checks = 0
        self.cell = "ROW-005"

    def ensure_trusted(self) -> None:
        return None

    def frontmost_app(self) -> tuple[str, int]:
        return _APP, 7

    def snapshot(self, _scope, _app) -> Snapshot:
        if self.revealed:
            return _table(_element("hit", "AXCell", _QUERY, None, 120))
        value = self.cell
        return _table(_element("cell", "AXTextField", "ROW-005", value, 80))

    def scroll(self, _target, **kw) -> None:
        self.scrolls.append(int(kw.get("dy") or 0))

    def key_chord(self, chord: str, **_kw) -> None:
        self.chords.append(chord)
        if chord == "ctrl+f" and self.bar_after_ctrl_f:
            self.bar = True
        if chord == "Return" and self.bar_after_ctrl_f:
            self.revealed = True
        if chord == "Escape":
            self.bar = False

    def type_text(self, text: str, **_kw) -> None:
        self.typed.append(text)
        self.chords.append(f"type:{text}")
        self.cell = f"{self.cell}{text}"

    def supports_in_page_find(self, _app: str) -> bool:
        self.support_checks += 1
        return self.supports

    def find_bar_focused(self, _app: str) -> bool:
        self.focus_checks += 1
        return self.bar


def _runtime(tmp_path: Path, driver: _FindDriver, tier: safety.Tier) -> server.Runtime:
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier(_APP, tier)
    return server.Runtime(
        store=store,
        audit=safety.AuditLog(tmp_path / "audit"),
        driver=driver,
        allowed_domains="",
        blocked_domains="",
    )


def _rows(tmp_path: Path) -> list[dict]:
    audit = tmp_path / "audit"
    return [
        json.loads(line)
        for path in audit.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _search(runtime: server.Runtime, text: str = _QUERY) -> str:
    return runtime.scroll_to_find(_APP, text=text, max_scrolls=4, budget_s=15)


def test_click_grant_audits_the_denial_and_types_nothing(tmp_path: Path) -> None:
    """A click grant would open a bar on this host. No key is sent.

    If type_text ran, the selected field would become ROW-005ROW-150 and
    this test would fail. The denial names the full tier and the click grant.
    """
    driver = _FindDriver(_table(), supports=True, bar_after_ctrl_f=True)
    with _runtime(tmp_path, driver, safety.Tier.CLICK) as runtime:
        out = _search(runtime)
    assert out.startswith("not found after")
    assert driver.typed == []
    assert driver.chords == []
    assert driver.cell == "ROW-005"
    denies = [row for row in _rows(tmp_path) if row["action"] == "typetext"]
    assert denies
    assert all(row["result"] == "deny" for row in denies)
    reason = denies[0]["decision"]["reason"]
    assert "full" in reason
    assert "click" in reason
    assert not any(
        row["action"] == "keychord"
        and row["params"].get("chord") == "ctrl+f"
        and row["result"] == "ok"
        for row in _rows(tmp_path)
    )


def test_swing_host_sends_no_find_keystrokes_at_full_tier(tmp_path: Path) -> None:
    """Java, GTK, and Qt do not open a find bar. The cell is left alone."""
    driver = _FindDriver(_table(), supports=False)
    with _runtime(tmp_path, driver, safety.Tier.FULL) as runtime:
        out = _search(runtime)
    assert out.startswith("not found after")
    assert driver.support_checks >= 1
    assert driver.typed == []
    assert driver.chords == []
    assert driver.focus_checks == 0
    assert driver.cell == "ROW-005"
    assert not any(row["action"] == "typetext" and row["result"] == "ok" for row in _rows(tmp_path))
    assert not any(row["action"] == "keychord" for row in _rows(tmp_path))


def test_unverified_find_bar_does_not_receive_the_query(tmp_path: Path) -> None:
    """Ctrl+F is sent, the bar never focuses, and the query is not typed.

    Escape closes a browser bar the focus probe missed. The poll asks twelve
    times. A Swing cell is not this case: Swing is not a find host.
    """
    driver = _FindDriver(_table(), supports=True, bar_after_ctrl_f=False)
    with _runtime(tmp_path, driver, safety.Tier.FULL) as runtime:
        out = _search(runtime)
    assert out.startswith("not found after")
    assert driver.focus_checks == 12
    assert driver.chords[0] == "ctrl+f"
    assert "Escape" in driver.chords
    assert driver.typed == []
    assert driver.cell == "ROW-005"
    assert not any(item.startswith("type:") for item in driver.chords)
    rows = _rows(tmp_path)
    assert any(
        row["action"] == "keychord" and row["params"].get("chord") == "ctrl+f" and row["result"] == "ok"
        for row in rows
    )
    assert any(
        row["action"] == "keychord" and row["params"].get("chord") == "Escape" and row["result"] == "ok"
        for row in rows
    )
    assert not any(row["action"] == "typetext" for row in rows)


def test_verified_find_bar_is_typed_audited_and_closed(tmp_path: Path) -> None:
    """A bar that takes focus receives the query. The audit stores a count.

    The row is returned only after Return, from a later snapshot. The typed
    characters are not in the audit row.
    """
    driver = _FindDriver(_table(), supports=True, bar_after_ctrl_f=True)
    with _runtime(tmp_path, driver, safety.Tier.FULL) as runtime:
        out = _search(runtime)
    assert "found after 1 scroll" in out
    assert "found after 0" not in out
    assert _QUERY in out
    assert driver.chords[:3] == ["ctrl+f", f"type:{_QUERY}", "Return"]
    assert "Escape" in driver.chords
    assert driver.scrolls == []
    rows = _rows(tmp_path)
    assert any(
        row["action"] == "keychord" and row["params"].get("chord") == "ctrl+f" and row["result"] == "ok"
        for row in rows
    )
    assert any(
        row["action"] == "keychord" and row["params"].get("chord") == "Return" and row["result"] == "ok"
        for row in rows
    )
    assert any(
        row["action"] == "keychord" and row["params"].get("chord") == "Escape" and row["result"] == "ok"
        for row in rows
    )
    typed = [row for row in rows if row["action"] == "typetext" and row["result"] == "ok"]
    assert len(typed) == 1
    assert typed[0]["params"]["text"] == safety.REDACTED
    assert typed[0]["params"]["chars"] == len(_QUERY)
    assert _QUERY not in json.dumps(typed[0])


def test_an_editor_that_only_grew_by_the_query_is_not_found(tmp_path: Path) -> None:
    """ROW-005ROW-150 is the cell the tool must not report as the row.

    The value already contains the query. Nothing is typed. A combo box is
    the same kind of editor.
    """
    snap = _table(
        _element("cell", "AXTextField", "ROW-005", "ROW-005ROW-150", 80),
        _element("combo", "AXComboBox", "Item", "note-ROW-150", 110),
    )
    driver = _FindDriver(snap, supports=False)
    driver.snapshot = lambda _scope, _app: snap  # type: ignore[method-assign]
    with _runtime(tmp_path, driver, safety.Tier.FULL) as runtime:
        out = _search(runtime)
    assert out.startswith("not found after")
    assert driver.typed == []
    assert driver.chords == []


def test_a_real_cell_and_an_exact_field_value_are_found_without_typing(tmp_path: Path) -> None:
    """A visible row is the match. The find path is not used to invent it."""
    cell = _table(_element("hit", "AXCell", _QUERY, None, 120))
    driver = _FindDriver(cell, supports=True, bar_after_ctrl_f=True)
    driver.snapshot = lambda _scope, _app: cell  # type: ignore[method-assign]
    with _runtime(tmp_path, driver, safety.Tier.FULL) as runtime:
        out = _search(runtime)
    assert "found after 0 scroll" in out
    assert _QUERY in out
    assert driver.typed == []
    assert driver.chords == []

    exact = _table(_element("field", "AXTextField", "Item", _QUERY, 120))
    exact_driver = _FindDriver(exact, supports=True, bar_after_ctrl_f=True)
    exact_driver.snapshot = lambda _scope, _app: exact  # type: ignore[method-assign]
    audit = tmp_path / "exact"
    store = safety.PermissionStore(tmp_path / "exact-permissions.json")
    store.set_tier(_APP, safety.Tier.FULL)
    with server.Runtime(
        store=store, audit=safety.AuditLog(audit), driver=exact_driver,
        allowed_domains="", blocked_domains="",
    ) as runtime:
        exact_out = _search(runtime)
    assert "found after 0 scroll" in exact_out
    assert exact_driver.typed == []
    assert exact_driver.chords == []


def test_find_bar_chrome_is_not_the_match(tmp_path: Path) -> None:
    """The Find field's value is the query. A click grant still types nothing."""
    snap = _table(_element("find", "AXTextField", "Find", _QUERY, 80))
    driver = _FindDriver(snap, supports=True, bar_after_ctrl_f=True)
    driver.snapshot = lambda _scope, _app: snap  # type: ignore[method-assign]
    with _runtime(tmp_path, driver, safety.Tier.CLICK) as runtime:
        out = _search(runtime)
    assert out.startswith("not found after")
    assert driver.typed == []
    assert driver.chords == []


class _Node:
    def __init__(self, name: str, role: str = "entry", attrs: dict | None = None,
                 toolkit: str = "", app_name: str | None = None) -> None:
        self._name = name
        self._role = role
        self._attrs = {} if attrs is None else attrs
        self._toolkit = toolkit
        self._app_name = name if app_name is None else app_name

    def get_name(self) -> str:
        return self._name

    def get_role_name(self) -> str:
        return self._role

    def get_attributes(self) -> dict:
        return dict(self._attrs)

    def get_toolkit_name(self) -> str:
        return self._toolkit

    def get_application(self) -> "_Node":
        return self


class _App(_Node):
    def __init__(self, toolkit: str, name: str) -> None:
        super().__init__(name, role="application", toolkit=toolkit, app_name=name)

    def get_name(self) -> str:
        return self._app_name


def test_focused_find_field_rejects_a_cell_and_a_page_control() -> None:
    """Chrome's Find entry counts. A cell, a button, and a tagged input do not."""
    assert _atspi.focused_find_field(None) is False
    assert _atspi.focused_find_field(_Node("Find", "entry")) is True
    assert _atspi.focused_find_field(_Node("Find", "text")) is True
    assert _atspi.focused_find_field(_Node("Find", "paragraph")) is True
    assert _atspi.focused_find_field(_Node("Find in page", "frame")) is True
    assert _atspi.focused_find_field(_Node("Find", "push button")) is False
    assert _atspi.focused_find_field(_Node("Find", "table cell")) is False
    assert _atspi.focused_find_field(_Node("Find", "entry", {"tag": "input"})) is False
    assert _atspi.focused_find_field(_Node("Find", "entry", {"html-tag": "input"})) is False
    assert _atspi.focused_find_field(_Node("ROW-005", "text")) is False
    assert _atspi.focused_find_field(_Node("ROW-005", "table cell")) is False


def test_only_chromium_and_firefox_support_in_page_find() -> None:
    """Swing, GTK, Qt, and an empty toolkit do not. A failed read does not."""
    assert _atspi.app_supports_in_page_find(None) is False
    assert _atspi.app_supports_in_page_find(_App("Chromium", "Google Chrome")) is True
    assert _atspi.app_supports_in_page_find(_App("", "Chromium")) is True
    assert _atspi.app_supports_in_page_find(_App("Gecko", "Firefox")) is True
    assert _atspi.app_supports_in_page_find(_App("", "Firefox")) is True
    assert _atspi.app_supports_in_page_find(_App("Java", "java")) is False
    assert _atspi.app_supports_in_page_find(_App("", "java")) is False
    assert _atspi.app_supports_in_page_find(_App("gtk", "gedit")) is False
    assert _atspi.app_supports_in_page_find(_App("Qt", "cuqttable")) is False

    class _Boom:
        def get_toolkit_name(self) -> str:
            raise RuntimeError("bus down")

        def get_name(self) -> str:
            raise RuntimeError("bus down")

        def get_application(self) -> "_Boom":
            return self

    assert _atspi.app_supports_in_page_find(_Boom()) is False
