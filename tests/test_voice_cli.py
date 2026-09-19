"""`a11y-computer-use voice --text`: the timeline and JSON output through a fake
driver, and the text listener, hermetic on every OS."""

from __future__ import annotations

import json
import threading

import pytest

from a11y_computer_use import cli, reflex, safety, server, voice
from tests.test_reflex import APPS, DEMO, ReflexDriver


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    driver = ReflexDriver()
    store = safety.PermissionStore(tmp_path / "perm.json")
    for app in ("Notes", "Google Chrome", "Photo Booth", "Finder"):
        store.set_tier(app, safety.Tier.FULL)

    runtime_cls = server.Runtime

    def make(**_kw):
        return runtime_cls(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    monkeypatch.setattr(server, "Runtime", make)
    monkeypatch.setattr(reflex, "installed_apps", lambda folders=None: list(APPS))
    return driver


def test_voice_text_prints_one_timeline_per_command(fake_runtime, capsys) -> None:
    assert cli.main(["voice", "--text", DEMO[0], "--router", "local"]) == 0
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("[route ") and " local] [act " in lines[0]
    assert "open_app app='Notes' -> ok: focused Notes (Notes frontmost)" in lines[0]
    assert "new_document  -> ok: pressed cmd+n" in lines[1]
    assert "2/2 commands ok" in err
    assert fake_runtime.calls == [("focus", "Notes"), ("key", "cmd+n")]


def test_voice_text_file_and_json(fake_runtime, capsys, tmp_path) -> None:
    transcript = tmp_path / "demo.txt"
    transcript.write_text("# comment\n" + "\n".join(DEMO) + "\n")
    assert cli.main(["voice", "--text", str(transcript), "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [row["skill"] for row in rows] == ["open_app", "new_document", "set_title", "open_app",
                                              "web_search", "open_url", "open_app", "take_photo"]
    assert all(row["ok"] and row["backend"] == "local" for row in rows)
    assert rows[4]["slots"] == {"query": "Norbert Wiener"}
    assert rows[0]["stt_ms"] is None


def test_voice_text_unrouted_exits_one(fake_runtime, capsys) -> None:
    assert cli.main(["voice", "--text", "what is love"]) == 1
    assert "unrouted" in capsys.readouterr().out


def test_voice_grant_needs_apps(fake_runtime, capsys) -> None:
    assert cli.main(["voice", "--text", "open Notes", "--grant", "full"]) == 2
    assert "--apps" in capsys.readouterr().err


def test_voice_grant_sets_tiers_before_running(fake_runtime, capsys) -> None:
    assert cli.main(["voice", "--text", "open Notes", "--grant", "full",
                     "--apps", 'Notes,"Photo Booth"']) == 0
    err = capsys.readouterr().err
    assert "granted Notes tier full" in err and "granted Photo Booth tier full" in err


def test_voice_jev_without_key_says_so_and_routes_locally(fake_runtime, capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(reflex.API_KEY_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["voice", "--text", "open Notes", "--router", "jev"]) == 0
    out, err = capsys.readouterr()
    assert "TYPESAFE_API_KEY is not set" in err
    assert "local-fallback] [act" in out


def test_split_apps() -> None:
    assert cli._split_apps('Notes, Arc,"Photo Booth",') == ["Notes", "Arc", "Photo Booth"]
    assert cli._split_apps(None) == []


def test_text_listener_from_string_and_file(tmp_path) -> None:
    heard: list = []
    voice.TextListener("open Notes").listen(lambda t, ms: heard.append((t, ms)), threading.Event())
    assert heard == [("open Notes", None)]
    path = tmp_path / "t.txt"
    path.write_text("# skip\n\nopen Notes\ntake a picture\n")
    heard.clear()
    voice.TextListener(str(path)).listen(lambda t, ms: heard.append(t), threading.Event())
    assert heard == ["open Notes", "take a picture"]
    stop = threading.Event()
    stop.set()
    heard.clear()
    voice.TextListener(str(path)).listen(lambda t, ms: heard.append(t), stop)
    assert heard == []


def test_authorization_status_is_strings() -> None:
    status = voice.authorization_status()
    assert set(status) == {"speech", "microphone"}
    assert all(v in set(voice.AUTH_STATUS.values()) | {"unavailable", "unknown"} for v in status.values())
