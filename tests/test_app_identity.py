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
    assert store.get_tier("Files") is Tier.FULL
    assert store.get_tier("FILES") is Tier.FULL
    assert store.get_tier("nautilus") is Tier.FULL
    assert store.get_tier("Terminal") is Tier.CLICK
    assert store.get_tier("gnome-terminal") is Tier.CLICK
    assert store.get_tier("xterm") is Tier.CLICK
    assert store.get_tier("libreoffice calc") is Tier.CLICK
    assert store.get_tier("LibreOffice Calc") is Tier.CLICK
    assert store.get_tier("libreoffice writer") is Tier.CLICK
    assert store.get_tier("libreoffice-impress") is Tier.CLICK
    assert store.get_tier("soffice.bin") is Tier.CLICK
    assert same_app("chrome", "notchrome") is False
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
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    runtime.APP_LAUNCH_WAIT_S = 1
    text = runtime.app("launch", "Files")
    assert "needs_permission" not in text
    assert "ask the user" not in text
    assert launched == ["thunar"]

    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "not-a-real-app-zz9")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "soffice" in exc.value.message
    assert "thunar" in exc.value.message
    assert "needs_permission" not in exc.value.message
    assert "ask the user" not in exc.value.message
