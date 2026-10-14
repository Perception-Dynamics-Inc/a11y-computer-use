"""Grant identity and launch resolution. No display and no AT-SPI."""

from __future__ import annotations

import pytest

from a11y_computer_use import app_identity, safety
from a11y_computer_use.app_identity import matching_stored_key, resolve_launch, same_app
from a11y_computer_use.safety import PermissionStore, Tier, check_action
from a11y_computer_use.schema import Click, ComputerUseError, ErrorCode, Point

_CLICK = Click(target=Point(display_id=1, x=1, y=1))


def test_alias_and_case_share_a_grant(tmp_path) -> None:
    store = PermissionStore(tmp_path / "p.json")
    store.set_tier("thunar", Tier.FULL)
    store.set_tier("xfce4-terminal", Tier.CLICK)
    store.set_tier("soffice", Tier.CLICK)
    store.set_tier("mousepad", Tier.FULL)
    store.set_tier("google-chrome", Tier.CLICK)
    assert store.get_tier("Files") is Tier.FULL
    assert store.get_tier("FILES") is Tier.FULL
    assert store.get_tier("nautilus") is Tier.FULL
    assert store.get_tier("xfce4-terminal") is Tier.CLICK
    assert store.get_tier("Terminal") is None
    assert store.get_tier("gnome-terminal") is None
    assert store.get_tier("xterm") is None
    assert store.get_tier("Text Editor") is Tier.FULL
    assert store.get_tier("gedit") is Tier.FULL
    assert store.get_tier("gnome-text-editor") is Tier.FULL
    assert store.get_tier("chrome") is Tier.CLICK
    assert store.get_tier("chromium") is Tier.CLICK
    assert store.get_tier("chromium-browser") is Tier.CLICK
    assert store.get_tier("google-chrome-stable") is Tier.CLICK
    store.set_tier("gnome-terminal", Tier.FULL)
    assert store.get_tier("gnome-terminal-server") is Tier.FULL
    assert store.get_tier("org.gnome.Terminal") is Tier.FULL
    assert store.get_tier("xterm") is None
    assert store.get_tier("Terminal") is None
    assert store.get_tier("libreoffice calc") is Tier.CLICK
    assert store.get_tier("LibreOffice Calc") is Tier.CLICK
    assert store.get_tier("libreoffice writer") is Tier.CLICK
    assert store.get_tier("libreoffice-impress") is Tier.CLICK
    assert store.get_tier("soffice.bin") is Tier.CLICK
    assert same_app("chrome", "notchrome") is False
    assert same_app("chrome", "google-chrome") is True
    assert same_app("xterm", "gnome-terminal") is False
    assert same_app("gnome-terminal", "gnome-terminal-server") is True
    assert same_app("Files", "profiles") is False
    assert matching_stored_key("Mousepad", ["mousepad"]) == "mousepad"


def test_permission_text_does_not_tell_the_model_to_ask(tmp_path) -> None:
    from a11y_computer_use.server import refusal_text

    decision = check_action(_CLICK, "unknown", store=PermissionStore(tmp_path / "p.json"))
    assert decision.verdict is safety.Verdict.NEEDS_PERMISSION
    assert "ask the user" not in decision.reason
    text = refusal_text(decision)
    assert "ask the user" not in text
    assert "grant_app(app='unknown', tier='click')" in text


def test_resolve_launch_rewrites_labels_and_rejects_unknown_names() -> None:
    entries = [
        {"id": "org.gnome.Nautilus", "name": "Files", "exec": "nautilus", "wm_class": "org.gnome.Nautilus"},
        {"id": "org.gnome.Terminal", "name": "Terminal", "exec": "gnome-terminal", "wm_class": "Gnome-terminal"},
        {"id": "libreoffice-calc", "name": "LibreOffice Calc", "exec": "soffice", "wm_class": "libreoffice-calc"},
    ]
    files = resolve_launch(
        "Files", granted=["thunar"], entries=entries, path_lookup=lambda _name: None,
    )
    assert files.resolved is True
    assert files.launch_name == "thunar"
    assert files.gate_key == "thunar"

    terminal = resolve_launch(
        "Terminal", granted=["xterm"], entries=entries, path_lookup=lambda _name: None,
    )
    assert terminal.launch_name == "xterm"
    assert terminal.gate_key == "xterm"

    calc = resolve_launch(
        "libreoffice calc", granted=["soffice"], entries=entries, path_lookup=lambda _name: None,
    )
    assert calc.launch_name == "soffice"
    assert calc.gate_key == "soffice"
    assert calc.argv == ("soffice", "--calc")
    assert files.argv is None
    assert terminal.argv is None

    ungranted = resolve_launch(
        "Files", granted=[], entries=entries, path_lookup=lambda _name: None,
    )
    assert ungranted.resolved is True
    assert ungranted.launch_name == "nautilus"
    assert ungranted.gate_key == "nautilus"

    missing = resolve_launch(
        "not-a-real-app-zz9", granted=["soffice"], entries=entries, path_lookup=lambda _name: None,
    )
    assert missing.resolved is False
    assert missing.argv is None


    editor_entries = [
        {"id": "org.xfce.mousepad", "name": "Text Editor", "exec": "mousepad", "wm_class": "mousepad"},
    ]
    editor = resolve_launch(
        "Text Editor", granted=["gedit"], entries=editor_entries, path_lookup=lambda _name: None,
    )
    assert editor.resolved is True
    assert editor.launch_name == "gedit"
    assert editor.gate_key == "gedit"

    other_terminal = resolve_launch(
        "gnome-terminal", granted=["xterm"], entries=entries, path_lookup=lambda _name: "/usr/bin/gnome-terminal",
    )
    assert other_terminal.gate_key != "xterm"
    assert same_app(other_terminal.gate_key or "", "xterm") is False

    # CI installs xterm and no desktop file named Terminal. The grant alone resolves.
    bare = resolve_launch(
        "Terminal", granted=["pcmanfm", "xterm"], entries=[], path_lookup=lambda _name: None,
    )
    assert bare.resolved is True
    assert bare.launch_name == "xterm"
    assert bare.gate_key == "xterm"

    ambiguous = resolve_launch(
        "Terminal", granted=["xterm", "kitty"], entries=[], path_lookup=lambda _name: None,
    )
    assert ambiguous.resolved is False


def test_display_name_resolves_to_the_program_whatever_the_grant_spells() -> None:
    """``Mousepad`` launches ``mousepad`` when the grant is ``Mousepad``, ``mousepad``, or both.

    The grant is the gate. It does not choose a different program. ``Text Editor``
    is that file's GenericName. A grant for a different app does not become the gate.
    """
    entries = [{
        "id": "org.xfce.mousepad",
        "name": "Mousepad",
        "generic_name": "Text Editor",
        "exec": "mousepad",
        "wm_class": "mousepad",
    }]

    def lookup(name: str) -> str | None:
        return "/usr/bin/mousepad" if name == "mousepad" else None

    both = resolve_launch(
        "Mousepad", granted=["mousepad", "Mousepad"], entries=entries, path_lookup=lookup,
    )
    display = resolve_launch(
        "Mousepad", granted=["Mousepad"], entries=entries, path_lookup=lookup,
    )
    lower = resolve_launch(
        "Mousepad", granted=["mousepad"], entries=entries, path_lookup=lookup,
    )
    assert both.launch_name == display.launch_name == lower.launch_name == "mousepad"
    assert both.resolved is True and display.resolved is True and lower.resolved is True
    assert both.gate_key == "Mousepad"
    assert display.gate_key == "Mousepad"
    assert lower.gate_key == "mousepad"
    assert both.argv is None

    editor = resolve_launch(
        "Text Editor", granted=["mousepad"], entries=entries, path_lookup=lookup,
    )
    assert editor.launch_name == "mousepad"
    assert editor.gate_key == "mousepad"
    assert editor.resolved is True

    titled = resolve_launch(
        "Text Editor", granted=["Mousepad"], entries=entries, path_lookup=lookup,
    )
    assert titled.launch_name == "mousepad"
    assert titled.gate_key == "Mousepad"

    other = resolve_launch(
        "Text Editor", granted=["gedit"], entries=entries, path_lookup=lookup,
    )
    assert other.launch_name == "mousepad"
    assert other.gate_key == "mousepad"
    assert other.gate_key != "gedit"

    missing = resolve_launch(
        "Not a Desktop App", granted=["gedit"], entries=entries, path_lookup=lookup,
    )
    assert missing.resolved is False
    assert missing.gate_key is None

    # The executable's own spelling stays, even when a desktop Name differs by case.
    kept = resolve_launch(
        "mousepad", granted=["Mousepad"], entries=entries, path_lookup=lambda _name: None,
    )
    assert kept.launch_name == "mousepad"
    assert kept.gate_key == "Mousepad"

    off_path = resolve_launch(
        "Text Editor", granted=["Mousepad"], entries=entries, path_lookup=lambda _name: None,
    )
    assert off_path.launch_name == "org.xfce.mousepad"
    assert off_path.gate_key == "Mousepad"

    by_id = resolve_launch(
        "org.xfce.mousepad", granted=["mousepad"], entries=entries, path_lookup=lookup,
    )
    assert by_id.launch_name == "org.xfce.mousepad"
    assert by_id.gate_key == "mousepad"


def test_display_name_prefers_name_over_an_earlier_generic_name() -> None:
    """``Name=Mousepad`` wins over another file's ``GenericName=Mousepad``.

    The other file's grant does not become the gate for the Name match.
    """
    entries = [
        {
            "id": "org.example.Notes",
            "name": "Notes",
            "generic_name": "Mousepad",
            "exec": "notes",
            "wm_class": "notes",
        },
        {
            "id": "org.xfce.mousepad",
            "name": "Mousepad",
            "generic_name": "Text Editor",
            "exec": "mousepad",
            "wm_class": "mousepad",
        },
    ]
    picked = resolve_launch(
        "Mousepad", granted=["notes"], entries=entries, path_lookup=lambda _name: None,
    )
    assert picked.launch_name == "mousepad"
    assert picked.gate_key == "mousepad"
    assert picked.gate_key != "notes"


def test_office_module_labels_launch_the_wrapper_or_the_flag() -> None:
    entries = [
        {"id": "libreoffice-calc", "name": "LibreOffice Calc", "exec": "libreoffice", "wm_class": "libreoffice-calc"},
    ]

    def lookup(name: str) -> str | None:
        return {
            "localc": "/usr/bin/localc",
            "loimpress": "/usr/bin/loimpress",
        }.get(name)

    calc = resolve_launch(
        "LibreOffice-Calc", granted=["soffice"], entries=entries, path_lookup=lookup,
    )
    assert calc.launch_name == "localc"
    assert calc.gate_key == "soffice"
    assert calc.argv == ("/usr/bin/localc",)

    spaced = resolve_launch(
        "libreoffice calc", granted=["soffice"], entries=entries, path_lookup=lookup,
    )
    assert spaced.argv == ("/usr/bin/localc",)

    short = resolve_launch("calc", granted=["soffice"], entries=[], path_lookup=lookup)
    assert short.launch_name == "localc"
    assert short.argv == ("/usr/bin/localc",)
    assert short.gate_key == "soffice"

    writer = resolve_launch(
        "libreoffice writer", granted=["soffice"], entries=entries, path_lookup=lookup,
    )
    assert writer.launch_name == "soffice"
    assert writer.gate_key == "soffice"
    assert writer.argv == ("soffice", "--writer")

    impress = resolve_launch(
        "Impress", granted=["soffice"], entries=entries, path_lookup=lookup,
    )
    assert impress.launch_name == "loimpress"
    assert impress.gate_key == "soffice"
    assert impress.argv == ("/usr/bin/loimpress",)

    desktop = resolve_launch(
        "libreoffice-calc", granted=[], entries=entries, path_lookup=lambda _name: None,
    )
    assert desktop.resolved is True
    assert desktop.launch_name == "libreoffice"
    assert desktop.gate_key == "libreoffice"
    assert desktop.argv == ("libreoffice", "--calc")

    on_path = resolve_launch(
        "writer", granted=[], entries=[], path_lookup=lambda name: "/usr/bin/soffice" if name == "soffice" else None,
    )
    assert on_path.argv == ("/usr/bin/soffice", "--writer")
    assert on_path.launch_name == "soffice"
    assert on_path.gate_key == "soffice"

    bare = resolve_launch(
        "libreoffice", granted=["soffice"], entries=entries, path_lookup=lookup,
    )
    assert bare.argv is None
    assert bare.launch_name == "libreoffice"

    direct = resolve_launch("localc", granted=["soffice"], entries=entries, path_lookup=lookup)
    assert direct.argv is None
    assert direct.launch_name == "localc"

    missing = resolve_launch("writer", granted=[], entries=[], path_lookup=lambda _name: None)
    assert missing.resolved is False
    assert missing.argv is None


def test_launch_passes_office_module_argv_to_a_linux_driver(tmp_path, monkeypatch) -> None:
    from a11y_computer_use import server

    launched: list[tuple] = []

    class _D:
        resolves_apps = False
        name = "linux"

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident, *, argv=None):
            launched.append((ident, argv))
            return {
                "pid": 9, "proc": None, "identifier": ident,
                "names": [ident, "soffice", "soffice.bin"], "is_launcher": False,
            }

        def windows(self):
            title = "Untitled 1 — LibreOffice Writer" if launched and launched[-1][0] == "soffice" else (
                "Untitled 1 — LibreOffice Calc"
            )
            return [{"window_id": 4, "app": "soffice.bin", "title": title, "pid": 9}]

    def which(name):
        return "/usr/bin/localc" if name == "localc" else None

    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setattr(
        app_identity, "load_desktop_entries",
        lambda: [{
            "id": "libreoffice-calc", "name": "LibreOffice Calc",
            "exec": "soffice", "wm_class": "libreoffice-calc",
        }],
    )
    monkeypatch.setattr(server, "_installed_bundle_id", lambda _ident: None)
    monkeypatch.setattr(
        server, "_running_app",
        lambda _ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("soffice", Tier.CLICK)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    runtime.APP_LAUNCH_WAIT_S = 1
    text = runtime.app("launch", "libreoffice-calc")
    assert launched == [("localc", ("/usr/bin/localc",))]
    assert "LibreOffice Calc" in text
    assert "first window: 'LibreOffice'" not in text

    written = runtime.app("launch", "libreoffice writer")
    assert launched[-1] == ("soffice", ("soffice", "--writer"))
    assert "LibreOffice Writer" in written


def _office_title_runtime(tmp_path, monkeypatch, titles: list[str], which):
    """A Linux runtime whose window list yields ``titles`` in order, then repeats the last."""
    from a11y_computer_use import server

    seen = {"n": 0}

    class _D:
        resolves_apps = False
        name = "linux"

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident, *, argv=None):
            return {
                "pid": 9, "proc": None, "identifier": ident,
                "names": [ident, "soffice", "soffice.bin"], "is_launcher": False,
            }

        def windows(self):
            seen["n"] += 1
            title = titles[min(seen["n"] - 1, len(titles) - 1)]
            return [{"window_id": 4, "app": "soffice.bin", "title": title, "pid": 9}]

    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setattr(app_identity, "load_desktop_entries", lambda: [])
    monkeypatch.setattr(server, "_installed_bundle_id", lambda _ident: None)
    monkeypatch.setattr(
        server, "_running_app",
        lambda _ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("soffice", Tier.CLICK)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    runtime.APP_LAUNCH_WAIT_S = 1
    runtime.OFFICE_DOCUMENT_WAIT_S = 2
    runtime._title_reads = seen
    return runtime


def test_launch_waits_past_the_libreoffice_start_center_for_calc(tmp_path, monkeypatch) -> None:
    """The first frame is ``LibreOffice 24.2``. The document title is the result."""
    runtime = _office_title_runtime(
        tmp_path, monkeypatch,
        ["LibreOffice 24.2", "LibreOffice 24.2", "Untitled 1 - LibreOffice Calc"],
        lambda name: "/usr/bin/localc" if name == "localc" else None,
    )
    text = runtime.app("launch", "libreoffice-calc")
    assert "first window: 'Untitled 1 - LibreOffice Calc'" in text
    assert "LibreOffice 24.2" not in text


def test_launch_reports_the_start_center_when_calc_never_opens(tmp_path, monkeypatch) -> None:
    """A module launch that stays on the Start Center is not reported as Calc."""
    runtime = _office_title_runtime(
        tmp_path, monkeypatch,
        ["LibreOffice 24.2"],
        lambda name: "/usr/bin/localc" if name == "localc" else None,
    )
    runtime.OFFICE_DOCUMENT_WAIT_S = 0.3
    text = runtime.app("launch", "libreoffice-calc")
    assert "first window: 'LibreOffice 24.2'" in text
    assert "Calc" not in text


def test_launch_waits_past_the_start_center_for_writer(tmp_path, monkeypatch) -> None:
    runtime = _office_title_runtime(
        tmp_path, monkeypatch,
        ["LibreOffice 24.2", "Untitled 1 - LibreOffice Writer"],
        lambda name: "/usr/bin/soffice" if name in {"soffice", "libreoffice"} else None,
    )
    text = runtime.app("launch", "libreoffice-writer")
    assert "first window: 'Untitled 1 - LibreOffice Writer'" in text
    assert "LibreOffice 24.2" not in text


def test_launch_reads_calc_when_the_tip_dialog_closes(tmp_path, monkeypatch) -> None:
    """The sheet title can replace the Start Center title as the tip closes."""
    from a11y_computer_use import server

    class _El:
        def __init__(self, role, title):
            self.role = role
            self.title = title
            self.focused = title == "OK"

    class _Snap:
        def __init__(self, elements):
            self.elements = elements

    phase = {"pressed": False}

    class _D:
        resolves_apps = False
        name = "linux"

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident, *, argv=None):
            return {
                "pid": 9, "proc": None, "identifier": ident,
                "names": ["localc", "soffice.bin"], "is_launcher": False,
            }

        def windows(self):
            title = "Untitled 1 - LibreOffice Calc" if phase["pressed"] else "LibreOffice 24.2"
            return [{"window_id": 4, "app": "soffice.bin", "title": title, "pid": 9}]

        def snapshot(self, _scope, _app):
            if phase["pressed"]:
                return _Snap([_El("AXCell", "A1")])
            return _Snap([
                _El("AXDialog", "Tip of the Day: 1/225"),
                _El("AXButton", "OK"),
            ])

        def press_element(self, _el):
            phase["pressed"] = True
            return True

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/localc" if name == "localc" else None)
    monkeypatch.setattr(app_identity, "load_desktop_entries", lambda: [])
    monkeypatch.setattr(server, "_installed_bundle_id", lambda _ident: None)
    monkeypatch.setattr(
        server, "_running_app",
        lambda _ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("soffice", Tier.CLICK)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    runtime.APP_LAUNCH_WAIT_S = 1
    runtime.OFFICE_DOCUMENT_WAIT_S = 0.3
    text = runtime.app("launch", "libreoffice-calc")
    assert "first window: 'Untitled 1 - LibreOffice Calc'" in text
    assert "dismissed 'Tip of the Day: 1/225'" in text
    assert "LibreOffice 24.2" not in text


def test_launch_of_bare_libreoffice_does_not_wait_for_a_module_title(tmp_path, monkeypatch) -> None:
    """A suite launch has no module. The Start Center title is the first window."""
    runtime = _office_title_runtime(
        tmp_path, monkeypatch,
        ["LibreOffice 24.2"],
        lambda name: "/usr/bin/libreoffice" if name == "libreoffice" else None,
    )
    runtime.OFFICE_DOCUMENT_WAIT_S = 30
    text = runtime.app("launch", "libreoffice")
    assert "first window: 'LibreOffice 24.2'" in text
    # One read records the windows already open, and one reads the new frame.
    # A module wait would poll for OFFICE_DOCUMENT_WAIT_S.
    assert runtime._title_reads["n"] == 2


def test_launch_dismisses_the_tip_of_the_day_and_reports_it(tmp_path, monkeypatch) -> None:
    """Synthetic. The OK press is the driver's. No LibreOffice process."""
    from a11y_computer_use import server

    class _El:
        def __init__(self, role, title, focused=False):
            self.role = role
            self.title = title
            self.focused = focused

    class _Snap:
        def __init__(self, elements):
            self.elements = elements

    pressed: list[str] = []
    shots = {"n": 0}

    class _D:
        resolves_apps = False
        name = "linux"

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident, *, argv=None):
            return {
                "pid": 9, "proc": None, "identifier": ident,
                "names": ["soffice", "soffice.bin"], "is_launcher": False,
            }

        def windows(self):
            return [{
                "window_id": 4, "app": "soffice.bin",
                "title": "Untitled 1 — LibreOffice Calc", "pid": 9,
            }]

        def snapshot(self, _scope, _app):
            shots["n"] += 1
            if shots["n"] == 1:
                return _Snap([
                    _El("AXDialog", "Tip of the Day: 1/225"),
                    _El("AXButton", "Next Tip"),
                    _El("AXButton", "OK", focused=True),
                ])
            return _Snap([_El("AXCell", "A1")])

        def press_element(self, el):
            pressed.append(el.title)
            return True

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/localc" if name == "localc" else None)
    monkeypatch.setattr(app_identity, "load_desktop_entries", lambda: [])
    monkeypatch.setattr(server, "_installed_bundle_id", lambda _ident: None)
    monkeypatch.setattr(
        server, "_running_app",
        lambda _ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("soffice", Tier.CLICK)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    runtime.APP_LAUNCH_WAIT_S = 1
    text = runtime.app("launch", "libreoffice-calc")
    assert pressed == ["OK"]
    assert "LibreOffice Calc" in text
    assert "dismissed 'Tip of the Day: 1/225'" in text

    quiet = {"pressed": False}

    class _Quiet(_D):
        def snapshot(self, _scope, _app):
            return _Snap([_El("AXCell", "A1")])

        def press_element(self, _el):
            quiet["pressed"] = True
            return True

    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_Quiet())
    runtime.APP_LAUNCH_WAIT_S = 1
    plain = runtime.app("launch", "libreoffice-calc")
    assert "dismissed" not in plain
    assert quiet["pressed"] is False



def test_launch_uses_the_granted_alias_and_unknown_names_are_not_permission_errors(
    tmp_path, monkeypatch,
) -> None:
    from a11y_computer_use import server

    launched: list[str] = []

    class _D:
        resolves_apps = False
        name = "fake"

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident):
            launched.append(ident)
            return {
                "pid": 9, "proc": None, "identifier": ident,
                "names": [ident], "is_launcher": False,
            }

        def windows(self):
            return [{"window_id": 1, "app": "thunar", "title": "Home", "pid": 9}]

    monkeypatch.setattr(app_identity, "load_desktop_entries", lambda: [])
    monkeypatch.setattr(server, "_installed_bundle_id", lambda _ident: None)
    monkeypatch.setattr(
        server, "_running_app",
        lambda _ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("thunar", Tier.CLICK)
    store.set_tier("soffice", Tier.CLICK)
    store.set_tier("xterm", Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    runtime.APP_LAUNCH_WAIT_S = 1
    text = runtime.app("launch", "Files")
    assert "needs_permission" not in text
    assert "ask the user" not in text
    assert launched == ["thunar"]
    terminal = runtime.app("launch", "Terminal")
    assert "needs_permission" not in terminal
    assert launched == ["thunar", "xterm"]

    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "not-a-real-app-zz9")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "soffice" in exc.value.message
    assert "thunar" in exc.value.message
    assert "needs_permission" not in exc.value.message
    assert "ask the user" not in exc.value.message


def test_installed_bundle_keeps_requested_name_when_linux_program_is_on_path() -> None:
    resolved = resolve_launch(
        "Krita", granted=["org.krita"], entries=[], installed_bundle="org.krita",
        path_lookup=lambda name: "/usr/bin/krita" if name == "krita" else None,
    )
    assert resolved.resolved is True
    assert resolved.launch_name == "Krita"
    assert resolved.gate_key == "org.krita"
