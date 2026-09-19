"""Reflex layer: slot extraction, the two routers, and execution through the
gate, all hermetic (a fake driver, a fake urlopen, no microphone)."""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from a11y_computer_use import reflex, safety, server
from a11y_computer_use.schema import ComputerUseError, Display, ErrorCode
from tests.test_agent import tiny_png

APPS = ["Google Chrome", "Safari", "Notes", "Photo Booth", "Figma", "Krita", "Terminal", "TextEdit"]
DEMO = [
    "All right. Can you open up the Notes app for me and um once you're there, can you create a new note?",
    "And um inside this new note, let's make the title say, 'Hello'. Great, great.",
    "Okay. Um let's move on and can you open up the Arc browser?",
    "And once you're there, can you Google search Norbert Wiener?",
    "Um now can you open up x.com? Nice, nice.",
    "Okay. Um now can you open up the Photo Booth? And let's take a picture of me.",
]
EXPECTED = [
    [("open_app", {"app": "Notes"}), ("new_document", {})],
    [("set_title", {"text": "Hello"})],
    [("open_app", {"app": "Google Chrome"})],  # Arc is not installed: the installed browser
    [("web_search", {"query": "Norbert Wiener"})],
    [("open_url", {"url": "x.com"})],
    [("open_app", {"app": "Photo Booth"}), ("take_photo", {})],
]


@pytest.fixture
def ctx() -> reflex.ReflexContext:
    return reflex.ReflexContext(apps=list(APPS))


# --- normalisation and extraction ---------------------------------------------


def test_split_strips_fillers_and_keeps_urls_and_quotes() -> None:
    assert reflex.split_commands(DEMO[0]) == ["open up the Notes app", "create a new note"]
    assert reflex.split_commands("Um now can you open up x.com? Nice, nice.") == ["open up x.com"]
    assert reflex.split_commands("go to x dot com") == ["go to x.com"]
    assert reflex.split_commands("type 'hello. world' and then take a picture") == [
        'type "hello. world"', "take a picture"]
    assert reflex.split_commands("Cool, awesome. Thank you.") == []


def test_extract_app_aliases_installed_names_and_open_phrases() -> None:
    assert reflex.extract_app("open up chrome", APPS) == "Google Chrome"
    assert reflex.extract_app("switch to the photo booth", APPS) == "Photo Booth"
    assert reflex.extract_app("open figma", APPS) == "Figma"
    assert reflex.extract_app("open up Krita please", APPS) == "Krita"
    assert reflex.extract_app("launch Zed", APPS) == "Zed"
    assert reflex.extract_app("create a new note", APPS) is None


def test_extract_quoted_prefers_quotes_then_trailing_words() -> None:
    assert reflex.extract_quoted("make the title say 'Hello World'") == "Hello World"
    assert reflex.extract_quoted('type "a, b. c"') == "a, b. c"
    assert reflex.extract_quoted("type hello there") == "hello there"
    assert reflex.extract_quoted("create a new note") is None


def test_extract_query_keeps_case_and_drops_browser_suffix() -> None:
    assert reflex.extract_query("Google search Norbert Wiener") == "Norbert Wiener"
    assert reflex.extract_query("search for ANSI escape codes in chrome") == "ANSI escape codes"
    assert reflex.extract_query("look up the weather") == "the weather"
    assert reflex.extract_query("open Notes") is None


def test_extract_url_handles_schemes_dots_and_spoken_dots() -> None:
    assert reflex.extract_url("open up x.com") == "x.com"
    assert reflex.extract_url("go to https://example.org/a?b=1") == "https://example.org/a?b=1"
    assert reflex.extract_url("open news dot ycombinator dot com") == "news.ycombinator.com"
    assert reflex.extract_url("open Notes") is None


def test_extract_menu_path() -> None:
    assert reflex.extract_menu_path("press File > Take Photo in Photo Booth") == "File > Take Photo"
    assert reflex.extract_menu_path("in Photo Booth press File>Take Photo") == "File > Take Photo"
    assert reflex.extract_menu_path("open Notes") is None


def test_browser_falls_back_to_installed_browser(ctx) -> None:
    assert ctx.browser() == "Google Chrome"
    assert reflex.ReflexContext(apps=["Safari"]).browser() == "Safari"
    assert reflex.ReflexContext(apps=[]).browser() == "Safari"
    assert reflex.ReflexContext(apps=APPS, default_browser="Safari").browser() == "Safari"
    assert reflex.resolve_browser("Arc", ctx) == "Google Chrome"
    assert reflex.resolve_browser("Notes", ctx) == "Notes"
    assert reflex.resolve_browser("Arc", reflex.ReflexContext(apps=["Arc", "Safari"])) == "Arc"


# --- LocalRouter --------------------------------------------------------------


def test_local_router_routes_the_six_demo_utterances(ctx) -> None:
    router = reflex.LocalRouter()
    for utterance, expected in zip(DEMO, EXPECTED):
        got = []
        for clause in reflex.split_commands(utterance):
            result = router.route(clause, ctx)
            got.append((result.skill, result.slots))
            assert result.backend == "local"
            assert result.latency_ms < 50
        assert got == expected, utterance


@pytest.mark.parametrize("text, skill", [
    ("take a screenshot", "screenshot"),
    ("snap a selfie", "take_photo"),
    ("type hello world", "type_text"),
    ("in Photo Booth press File > Take Photo", "menu_item"),
    ("press File > Take Photo in Photo Booth", "menu_item"),
    ("open a new tab", "new_document"),
    ("go to github.com", "open_url"),
    ("search for Wiener in Safari", "web_search"),
])
def test_local_router_other_skills(ctx, text: str, skill: str) -> None:
    result = reflex.LocalRouter().route(text, ctx)
    assert result.skill == skill
    if skill == "web_search":
        assert result.slots == {"query": "Wiener", "browser": "Safari"}
    if skill == "menu_item":
        assert result.slots == {"path": "File > Take Photo", "app": "Photo Booth"}


def test_local_router_declines_unknown_commands(ctx) -> None:
    result = reflex.LocalRouter().route("what is the meaning of life", ctx)
    assert result.skill is None and result.reason == "no skill matched"


# --- JevRouter ----------------------------------------------------------------


class _Response:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        pass


def _fake_urlopen(answer: dict, calls: list):
    def urlopen(request, timeout=None):
        calls.append((request, timeout))
        return _Response(answer)
    return urlopen


def _jev_answer(choice: str, confidence: float = 0.93, clarify: float = 0.05) -> dict:
    return {"answers": {"skill": {"choice": choice, "confidence": confidence,
                                  "probabilities": {choice: confidence}},
                        "needs_clarification": {"noul": clarify}},
            "usage": {"total_tokens": 240}, "model": "jev-latest"}


def test_jev_router_sends_one_choice_over_the_skills(ctx, monkeypatch) -> None:
    monkeypatch.delenv(reflex.API_KEY_VAR, raising=False)
    calls: list = []
    router = reflex.JevRouter("k-test", urlopen=_fake_urlopen(_jev_answer("open_app"), calls))
    result = router.route("open up the Notes app", ctx, frontmost="com.apple.finder")
    assert (result.skill, result.slots, result.backend) == ("open_app", {"app": "Notes"}, "jev")
    assert result.confidence == pytest.approx(0.93)
    request, timeout = calls[0]
    assert timeout == 2.0
    assert request.full_url == reflex.JEV_URL
    assert request.get_header("Authorization") == "Bearer k-test"
    body = json.loads(request.data)
    assert body["model"] == reflex.JEV_MODEL
    assert body["state"]["transcript"] == "open up the Notes app"
    assert body["state"]["frontmost_app"] == "com.apple.finder"
    assert body["state"]["installed_apps"] == APPS
    choice = body["questions"]["skill"]
    assert choice["type"] == "choice"
    assert set(choice["criteria"]) == {s.name for s in reflex.BUILTIN_SKILLS} | {"none"}
    assert body["questions"]["needs_clarification"]["type"] == "noul"


def test_jev_router_slots_stay_deterministic_and_none_declines(ctx) -> None:
    router = reflex.JevRouter("k", urlopen=_fake_urlopen(_jev_answer("web_search"), []))
    result = router.route("Google search Norbert Wiener", ctx)
    assert result.slots == {"query": "Norbert Wiener"}
    router = reflex.JevRouter("k", urlopen=_fake_urlopen(_jev_answer("none"), []))
    assert router.route("hmm", ctx).skill is None
    router = reflex.JevRouter("k", urlopen=_fake_urlopen(_jev_answer("open_app", clarify=0.9), []))
    result = router.route("open up the Notes app", ctx)
    assert result.skill is None and "clarification 0.90" in result.reason
    router = reflex.JevRouter("k", urlopen=_fake_urlopen(_jev_answer("open_app"), []))
    result = router.route("do the thing", ctx)  # no app in the transcript
    assert result.skill is None and "no slot" in result.reason


def test_jev_router_falls_back_locally_on_timeout_and_http_errors(ctx) -> None:
    def timeout(request, timeout=None):
        raise TimeoutError("timed out")
    router = reflex.JevRouter("k", urlopen=timeout)
    result = router.route("open up the Notes app", ctx)
    assert (result.skill, result.backend) == ("open_app", "local-fallback")
    assert "TimeoutError" in result.reason and router.last_error

    def http_error(request, timeout=None):
        raise urllib.error.HTTPError(reflex.JEV_URL, 503, "unavailable", {}, io.BytesIO(b""))
    router = reflex.JevRouter("k", urlopen=http_error)
    result = router.route("take a picture", ctx)
    assert (result.skill, result.backend, result.reason) == ("take_photo", "local-fallback", "HTTP 503")


def test_jev_router_without_a_key_routes_locally(ctx, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(reflex.API_KEY_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    router = reflex.JevRouter(urlopen=lambda *a, **k: pytest.fail("must not call the network"))
    assert router.api_key is None
    result = router.route("open up the Notes app", ctx)
    assert (result.skill, result.backend) == ("open_app", "local-fallback")
    assert reflex.API_KEY_VAR in result.reason


def test_load_dotenv_key_reads_env_then_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(reflex.API_KEY_VAR, raising=False)
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nTYPESAFE_API_KEY=\"from-file\"\n")
    assert reflex.load_dotenv_key(path=env) == "from-file"
    monkeypatch.setenv(reflex.API_KEY_VAR, "from-env")
    assert reflex.load_dotenv_key(path=env) == "from-env"
    assert reflex.load_dotenv_key(path=tmp_path / "missing") == "from-env"


def test_get_router_names() -> None:
    assert isinstance(reflex.get_router("local"), reflex.LocalRouter)
    assert isinstance(reflex.get_router("jev", api_key="k"), reflex.JevRouter)
    with pytest.raises(ValueError):
        reflex.get_router("gpt")


# --- execution through the gate ----------------------------------------------


class ReflexDriver:
    """Records the fast-path calls the skills make; apps are ids, no OS."""

    name = "fake"
    resolves_apps = True

    def __init__(self) -> None:
        self.front = "Finder"
        self.calls: list = []
        self.menu_fails = False

    def ensure_trusted(self) -> None:
        pass

    def frontmost_app(self):
        return self.front, 1

    def running_apps(self):
        return [{"id": self.front, "frontmost": True}]

    def launch_app(self, identifier) -> None:
        self.calls.append(("launch", identifier))
        self.front = identifier

    def activate_app(self, identifier) -> str:
        self.calls.append(("focus", identifier))
        self.front = identifier
        return identifier

    def key_chord(self, chord, **kw):
        self.calls.append(("key", chord))

    def type_text(self, text, **kw):
        self.calls.append(("type", text))

    def menu_press(self, app, path):
        self.calls.append(("menu", app, path))
        if self.menu_fails:
            raise ComputerUseError(ErrorCode.UNSUPPORTED, "no menu bar")
        return path.split(">")[-1].strip()

    def screenshot(self, display_id=None):
        self.calls.append(("screenshot",))
        return SimpleNamespace(png=tiny_png(), display=Display(1, 8, 4, 1.0, True))

    def windows(self):
        return []


@pytest.fixture
def runner(tmp_path):
    store = safety.PermissionStore(tmp_path / "perm.json")
    for app in ("Notes", "Google Chrome", "Photo Booth", "Finder"):
        store.set_tier(app, safety.Tier.FULL)
    driver = ReflexDriver()
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    ctx = reflex.ReflexContext(apps=list(APPS))
    return reflex.ReflexRunner(runtime, reflex.LocalRouter(), ctx), driver


def test_runner_executes_the_demo_with_fast_paths_only(runner) -> None:
    run, driver = runner
    results = [r for utterance in DEMO for r in run.handle(utterance, stt_ms=412.0)]
    assert [(r.skill, r.ok) for r in results] == [
        ("open_app", True), ("new_document", True), ("set_title", True), ("open_app", True),
        ("web_search", True), ("open_url", True), ("open_app", True), ("take_photo", True)]
    assert driver.calls == [
        ("focus", "Notes"), ("key", "cmd+n"), ("type", "Hello"), ("focus", "Google Chrome"),
        ("focus", "Google Chrome"), ("key", "cmd+t"),
        ("type", "https://www.google.com/search?q=Norbert+Wiener"), ("key", "return"),
        ("focus", "Google Chrome"), ("key", "cmd+t"), ("type", "https://x.com"), ("key", "return"),
        ("focus", "Photo Booth"), ("focus", "Photo Booth"), ("menu", "Photo Booth", "File > Take Photo")]
    first = results[0]
    assert first.stt_ms == 412.0 and results[1].stt_ms is None  # endpointing counted once
    assert first.check == "Notes frontmost" and first.check_ms <= reflex.CHECK_BUDGET_S * 1000
    line = first.timeline()
    assert line.startswith("[stt 412 ms] [route ") and "local] [act " in line
    assert "open_app app='Notes' -> ok: focused Notes (Notes frontmost)" in line
    assert results[4].outcome == "opened https://www.google.com/search?q=Norbert+Wiener in a new Google Chrome tab"
    assert run.ctx.recent[-1] == "take_photo {}"
    assert set(results[0].to_dict()) >= {"transcript", "skill", "slots", "ok", "outcome", "route_ms",
                                          "act_ms", "check_ms", "backend", "stt_ms", "check"}


def test_runner_reports_a_refused_tier_instead_of_raising(tmp_path) -> None:
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("Notes", safety.Tier.READ)  # cmd+n is a full-tier action
    driver = ReflexDriver()
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    run = reflex.ReflexRunner(runtime, reflex.LocalRouter(), reflex.ReflexContext(apps=list(APPS)))
    results = run.handle("open Notes and create a new note")
    assert [(r.skill, r.ok) for r in results] == [("open_app", False), ("new_document", False)]
    assert "ActionRefused" in results[0].outcome
    assert driver.calls == []  # refused before any input
    assert "-> refused/failed:" in results[0].timeline()
    assert results[0].act_ms >= 0


def test_runner_take_photo_falls_back_to_the_shutter_chord(runner) -> None:
    run, driver = runner
    driver.menu_fails = True
    (result,) = run.handle("take a picture")
    assert result.ok and driver.calls[-1] == ("key", "cmd+return")


def test_runner_unrouted_utterance_is_reported_not_executed(runner) -> None:
    run, driver = runner
    (result,) = run.handle("what time is it")
    assert result.skill is None and not result.ok and driver.calls == []
    assert result.timeline().endswith("unrouted  -> refused/failed: no skill matched")


def test_runner_screenshot_is_read_tier(runner) -> None:
    run, driver = runner
    (result,) = run.handle("take a screenshot")
    assert result.ok and driver.calls == [("screenshot",)]
    assert reflex.skill_map()["screenshot"].tier == "read"
