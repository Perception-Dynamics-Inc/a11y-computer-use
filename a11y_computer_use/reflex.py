"""Reflex layer: instant voice-to-action commands that never plan, only pick.

The agent loop (`agent.py`) lets a model plan over observations; that is right
for long tasks and wrong for "open Notes" said out loud, where the whole budget
is a few hundred milliseconds. This module is the other end of the scale:

* a **skill** is a fixed, fast recipe over the gated `Runtime` (app focus and
  launch, key chords, typing, menu presses) with deterministic slot extraction
  from the transcript; no model ever generates text or coordinates;
* a **router** picks one skill for one utterance: `LocalRouter` (regexes, no
  network, well under a millisecond) or `JevRouter` (TypeSafe's System One
  model answering one Choice over the skills; 0.8 to 2.4 s per call as
  measured from this network, with a 2 s timeout and a local fallback);
* the **runner** splits an utterance into clauses, routes and executes each,
  and reports the timeline per command: speech endpointing, routing, action,
  and the bounded post-check, separately, as measured.

Every skill goes through the same safety gate as every other tool: a per-app
grant is still required and refusals come back immediately as text.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from a11y_computer_use.schema import ComputerUseError

#: A post-check never blocks the next command for longer than this.
CHECK_BUDGET_S = 0.3

# --------------------------------------------------------------------------- #
# transcript normalisation
# --------------------------------------------------------------------------- #

_FILLERS = (
    r"\bum+\b", r"\buh+\b", r"\bokay\b", r"\bok\b", r"\balright\b", r"\ball right\b", r"\bgreat\b",
    r"\bnice\b", r"\bcool\b", r"\bawesome\b", r"\bthank you\b", r"\bthanks\b", r"\bplease\b",
    r"\bfor me\b", r"\bonce you'?re there\b", r"\bcan you\b", r"\bcould you\b", r"\bwould you\b",
    r"\blet'?s\b", r"\bnow\b", r"\bjust\b", r"\bgo ahead and\b", r"\bmove on and\b",
)
_QUOTED_RE = re.compile(r'"([^"]+)"')
_FILLER_RE = re.compile("|".join(_FILLERS), re.IGNORECASE)
_VERBS = r"open|launch|start|create|make|type|write|search|google|take|snap|go|switch|press|click|inside|look|visit|enter|say|bring"
_SPLIT_RE = re.compile(r"\s*(?:[.?!;]+|\band then\b|\bthen\b|,?\s+and[\s,]+(?=(?:" + _VERBS + r")\b))\s*",
                       re.IGNORECASE)
_TLDS = "com|org|net|io|ai|dev|app|co|gov|edu|me|xyz"
_PROTECT_URL_RE = re.compile(r"\b([a-z0-9-]+)(?:\.|\s+dot\s+)(" + _TLDS + r")\b", re.IGNORECASE)


def clean(text: str) -> str:
    """Strip filler words, normalise quotes and whitespace; keep the case."""
    cleaned = _FILLER_RE.sub(" ", text)
    cleaned = re.sub(r"[\"“”‘’]", '"', cleaned)
    cleaned = re.sub(r"(\w)'(?=\w)", "\\1\u2019", cleaned)  # keep apostrophes inside words
    cleaned = cleaned.replace("'", '"').replace("\u2019", "'")
    cleaned = re.sub(r"\s*,(?:\s*,)*\s*", ", ", cleaned)  # "and , create" -> "and, create"
    return re.sub(r"\s+", " ", cleaned).strip(" ,")


def normalize(text: str) -> str:
    """`clean` plus lower-case, for pattern matching."""
    return clean(text).lower()


def split_commands(text: str) -> list[str]:
    """Split one utterance into command clauses.

    Fillers go first (so "and um once you're there, can you create" splits on
    the "and"), URLs are protected from the sentence split ("x.com" is one
    token, "x dot com" becomes "x.com"), and quoted phrases stay whole.
    """
    cleaned = clean(text)
    quoted: list[str] = []

    def keep(match: re.Match) -> str:
        quoted.append(match.group(0))
        return f"\x00{len(quoted) - 1}\x00"

    protected = _QUOTED_RE.sub(keep, cleaned)
    protected = _PROTECT_URL_RE.sub(lambda m: f"{m.group(1)}\x01{m.group(2)}", protected)
    parts = []
    for part in _SPLIT_RE.split(protected):
        part = part.strip(" ,")
        if len(part) <= 2:
            continue
        part = part.replace("\x01", ".")
        part = re.sub(r"\x00(\d+)\x00", lambda m: quoted[int(m.group(1))], part)
        parts.append(part)
    return parts


# --------------------------------------------------------------------------- #
# installed apps and slot extraction
# --------------------------------------------------------------------------- #

_APP_FOLDERS = ("/Applications", os.path.expanduser("~/Applications"), "/System/Applications",
                "/System/Applications/Utilities")
_ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "photobooth": "Photo Booth",
    "photo booth": "Photo Booth", "the notes app": "Notes", "notes app": "Notes", "notes": "Notes",
    "arc browser": "Arc", "arc": "Arc", "safari": "Safari", "terminal": "Terminal",
    "textedit": "TextEdit", "text edit": "TextEdit", "finder": "Finder", "calendar": "Calendar",
    "messages": "Messages", "mail": "Mail", "music": "Music", "photos": "Photos", "figma": "Figma",
    "krita": "Krita", "telegram": "Telegram", "preview": "Preview", "settings": "System Settings",
    "system settings": "System Settings",
}
_BROWSERS = ("Arc", "Google Chrome", "Safari", "Firefox", "Brave Browser")
_BROWSER_BUNDLES = {"company.thebrowser.Browser": "Arc", "com.google.Chrome": "Google Chrome",
                    "com.apple.Safari": "Safari", "org.mozilla.firefox": "Firefox",
                    "com.brave.Browser": "Brave Browser"}
_URL_RE = re.compile(r"(https?://\S+)|\b([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:" + _TLDS + r"))\b(?:/\S*)?")


def installed_apps(folders: Sequence[str] = _APP_FOLDERS) -> list[str]:
    """Display names of installed applications (cached per process)."""
    global _INSTALLED_CACHE
    if _INSTALLED_CACHE is not None:
        return _INSTALLED_CACHE
    names: set[str] = set()
    for folder in folders:
        try:
            for entry in os.listdir(folder):
                if entry.endswith(".app"):
                    names.add(entry[:-4])
        except OSError:
            continue
    _INSTALLED_CACHE = sorted(names, key=len, reverse=True)
    return _INSTALLED_CACHE


_INSTALLED_CACHE: list[str] | None = None


def reset_installed_cache() -> None:
    global _INSTALLED_CACHE
    _INSTALLED_CACHE = None


def extract_app(text: str, apps: Sequence[str] | None = None) -> str | None:
    """The app named in ``text``: an alias, an installed app name, or an
    'open X' phrase; longest match wins; None when nothing app-like appears."""
    low = normalize(text)
    for alias in sorted(_ALIASES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(alias) + r"\b", low):
            return _ALIASES[alias]
    for name in (apps if apps is not None else installed_apps()):
        if len(name) >= 3 and re.search(r"\b" + re.escape(name.lower()) + r"\b", low):
            return name
    match = re.search(r"\b(?:open|launch|start|switch to|go to)\s+(?:up\s+)?(?:the\s+)?([a-z0-9][a-z0-9 ]{1,30}?)(?:\s+(?:app|browser))?\s*$", low)
    if match:
        return match.group(1).strip().title()
    return None


def extract_quoted(text: str) -> str | None:
    """A quoted phrase, or the words after say/type/write/titled."""
    match = _QUOTED_RE.search(clean(text))
    if match:
        return match.group(1).strip()
    match = re.search(r"\b(?:say|says|type|write|titled|called|named)\s+(.+?)\s*$", clean(text), re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def extract_query(text: str) -> str | None:
    """The search terms, case preserved, without a trailing 'in Chrome'."""
    match = re.search(r"\b(?:google search|google|search for|search|look up|lookup)\s+(?:for\s+)?(.+?)\s*$",
                      clean(text), re.IGNORECASE)
    if not match:
        return None
    query = match.group(1).strip().strip('"')
    query = re.sub(r"\s+(?:in|on|with)\s+(?:the\s+)?(?:" + "|".join(re.escape(a) for a in _ALIASES) + r")\s*$", "",
                   query, flags=re.IGNORECASE)
    return query or None


def resolve_browser(app: str | None, ctx: "ReflexContext") -> str | None:
    """A browser that is not installed ('open Arc' on a Mac without Arc) maps
    to the browser the context would use; other apps pass through."""
    if app in _BROWSERS and ctx.apps and app not in ctx.apps:
        return ctx.browser()
    return app


def extract_url(text: str) -> str | None:
    """A URL or bare host: 'x.com', 'news dot ycombinator dot com', 'https://...'."""
    spoken = re.sub(r"\s*\bdot\b\s*", ".", text.lower())
    match = _URL_RE.search(spoken)
    if not match:
        return None
    if match.group(1):
        return match.group(1).rstrip(".,")
    return match.group(0).rstrip(".,")


_MENU_SEGMENT = r"[A-Z][\w.\u2026]*(?: [A-Z][\w.\u2026]*)*"
_MENU_PATH_RE = re.compile(r"(" + _MENU_SEGMENT + r"(?:\s*>\s*" + _MENU_SEGMENT + r")+)"
                           r"(?:\s+in\s+(?:the\s+)?[A-Z][\w ]*)?\s*$")


def extract_menu_path(text: str) -> str | None:
    """'File > Take Photo' from 'press File > Take Photo in Photo Booth';
    segments are Title Case words, so app names and verbs around them stay out."""
    match = _MENU_PATH_RE.search(text.strip())
    return re.sub(r"\s*>\s*", " > ", match.group(1)).strip() if match else None


def title_case_app(name: str) -> str:
    return _ALIASES.get(name.lower(), name)


# --------------------------------------------------------------------------- #
# context, results
# --------------------------------------------------------------------------- #


@dataclass
class ReflexContext:
    """What the router and skills know besides the transcript."""

    apps: Sequence[str] = field(default_factory=lambda: installed_apps())
    default_browser: str | None = None
    recent: list[str] = field(default_factory=list)

    def browser(self, runtime: object | None = None) -> str:
        if self.default_browser:
            return self.default_browser
        if runtime is not None:
            try:
                front = runtime._frontmost()  # type: ignore[attr-defined]
                if front in _BROWSER_BUNDLES:
                    return _BROWSER_BUNDLES[front]
            except Exception:  # noqa: BLE001 - best effort
                pass
        for name in _BROWSERS:
            if name in self.apps:
                return name
        return "Safari"


@dataclass
class RouteResult:
    skill: str | None
    slots: dict[str, str]
    confidence: float | None
    latency_ms: float
    backend: str
    reason: str = ""


@dataclass
class CommandResult:
    transcript: str
    skill: str | None
    slots: dict[str, str]
    ok: bool
    outcome: str
    route_ms: float
    act_ms: float
    check_ms: float
    backend: str
    stt_ms: float | None = None
    check: str | None = None

    def timeline(self) -> str:
        stt = f"[stt {self.stt_ms:.0f} ms] " if self.stt_ms is not None else ""
        args = " ".join(f"{k}={v!r}" for k, v in self.slots.items())
        state = "ok" if self.ok else "refused/failed"
        check = f" ({self.check})" if self.check else ""
        return (f"{stt}[route {self.route_ms:.0f} ms {self.backend}] [act {self.act_ms:.0f} ms] "
                f"{self.skill or 'unrouted'} {args} -> {state}: {self.outcome}{check}")

    def to_dict(self) -> dict[str, object]:
        return {
            "transcript": self.transcript, "skill": self.skill, "slots": self.slots, "ok": self.ok,
            "outcome": self.outcome, "route_ms": round(self.route_ms, 2), "act_ms": round(self.act_ms, 2),
            "check_ms": round(self.check_ms, 2), "backend": self.backend, "stt_ms": self.stt_ms,
            "check": self.check,
        }


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #

Executor = Callable[[object, dict[str, str], ReflexContext], str]
Checker = Callable[[object, dict[str, str], ReflexContext], str | None]
Extractor = Callable[[str, ReflexContext], dict[str, str] | None]


@dataclass
class Skill:
    name: str
    description: str
    tier: str
    extract: Extractor
    execute: Executor
    check: Checker | None = None
    patterns: tuple[re.Pattern, ...] = ()


def _app_running(runtime: object, name: str) -> tuple[bool, str | None, int | None]:
    """(running, bundle, pid) for ``name``; a not-running app is (False, None, None)."""
    try:
        running, bundle = runtime._resolve_app(name)  # type: ignore[attr-defined]
    except ComputerUseError:
        return False, None, None
    try:
        pid = int(running.processIdentifier()) if running is not None else None
    except Exception:  # noqa: BLE001 - not an NSRunningApplication
        pid = None
    return True, bundle, pid


def _bring(runtime: object, name: str) -> str:
    running, _bundle, _pid = _app_running(runtime, name)
    verb = "focus" if running else "launch"
    return runtime.app(verb, name)  # type: ignore[attr-defined]


def _frontmost_is(runtime: object, name: str) -> str | None:
    """Post-check: is ``name`` in front, by the frontmost app or, on macOS, by
    the WindowServer's stacking order (NSWorkspace can lag a switch by a beat)."""
    try:
        running, bundle, pid = _app_running(runtime, name)
        if not running or bundle is None:
            return f"{name} not running"
        front = runtime._frontmost()  # type: ignore[attr-defined]
        if front == bundle:
            return f"{name} frontmost"
        if pid is not None:
            from a11y_computer_use.server import _top_window_pid

            if _top_window_pid() == pid:
                return f"{name} frontmost (top window)"
        return f"frontmost is {front}"
    except Exception as exc:  # noqa: BLE001 - a check never raises
        return f"check failed: {exc}"


# -- extractors ---------------------------------------------------------------

def _x_app(text: str, ctx: ReflexContext) -> dict[str, str] | None:
    app = resolve_browser(extract_app(text, ctx.apps), ctx)
    return {"app": app} if app else None


def _x_none(_text: str, _ctx: ReflexContext) -> dict[str, str] | None:
    return {}


def _x_text(text: str, _ctx: ReflexContext) -> dict[str, str] | None:
    phrase = extract_quoted(text)
    return {"text": phrase} if phrase else None


def _x_query(text: str, ctx: ReflexContext) -> dict[str, str] | None:
    query = extract_query(text)
    if not query:
        return None
    slots = {"query": query}
    app = resolve_browser(extract_app(text, ctx.apps), ctx)
    if app in _BROWSERS:
        slots["browser"] = app
    return slots


def _x_url(text: str, ctx: ReflexContext) -> dict[str, str] | None:
    url = extract_url(text)
    if not url:
        return None
    slots = {"url": url}
    app = resolve_browser(extract_app(text, ctx.apps), ctx)
    if app in _BROWSERS:
        slots["browser"] = app
    return slots


def _x_menu(text: str, ctx: ReflexContext) -> dict[str, str] | None:
    path = extract_menu_path(text)
    if not path:
        return None
    app = extract_app(text.replace(path, " "), ctx.apps)
    return {"path": path, "app": app or ""}


# -- executors ----------------------------------------------------------------

def _e_open_app(runtime: object, slots: dict[str, str], _ctx: ReflexContext) -> str:
    return _bring(runtime, slots["app"])


def _c_open_app(runtime: object, slots: dict[str, str], _ctx: ReflexContext) -> str | None:
    return _frontmost_is(runtime, slots["app"])


def _e_new_document(runtime: object, _slots: dict[str, str], _ctx: ReflexContext) -> str:
    return runtime.key("cmd+n")  # type: ignore[attr-defined]


def _e_type_text(runtime: object, slots: dict[str, str], _ctx: ReflexContext) -> str:
    return runtime.type_text(slots["text"])  # type: ignore[attr-defined]


def _browser_open(runtime: object, url: str, browser: str) -> str:
    """Bring the browser up, open a new tab (cmd+t focuses its address bar in
    Chrome, Arc, Safari, Firefox and Brave) and load ``url`` there, so the
    tab the person was on is left alone."""
    _bring(runtime, browser)
    runtime.key("cmd+t")  # type: ignore[attr-defined]
    runtime.type_text(url)  # type: ignore[attr-defined]
    runtime.key("return")  # type: ignore[attr-defined]
    return f"opened {url} in a new {browser} tab"


def _e_web_search(runtime: object, slots: dict[str, str], ctx: ReflexContext) -> str:
    browser = slots.get("browser") or ctx.browser(runtime)
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(slots["query"])
    return _browser_open(runtime, url, browser)


def _e_open_url(runtime: object, slots: dict[str, str], ctx: ReflexContext) -> str:
    browser = slots.get("browser") or ctx.browser(runtime)
    url = slots["url"]
    if not url.startswith("http"):
        url = "https://" + url
    return _browser_open(runtime, url, browser)


def _c_browser(runtime: object, slots: dict[str, str], ctx: ReflexContext) -> str | None:
    return _frontmost_is(runtime, slots.get("browser") or ctx.browser(runtime))


def _e_take_photo(runtime: object, _slots: dict[str, str], _ctx: ReflexContext) -> str:
    _bring(runtime, "Photo Booth")
    try:
        return runtime.menu("Photo Booth", "File > Take Photo")  # type: ignore[attr-defined]
    except ComputerUseError:
        return runtime.key("cmd+return")  # type: ignore[attr-defined]


def _e_screenshot(runtime: object, _slots: dict[str, str], _ctx: ReflexContext) -> str:
    text, _image = runtime.screenshot()  # type: ignore[attr-defined]
    return str(text).splitlines()[0] if text else "screenshot taken"


def _e_menu_item(runtime: object, slots: dict[str, str], _ctx: ReflexContext) -> str:
    app = slots.get("app") or runtime._frontmost()  # type: ignore[attr-defined]
    return runtime.menu(app, slots["path"])  # type: ignore[attr-defined]


def _p(*patterns: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


BUILTIN_SKILLS: tuple[Skill, ...] = (
    Skill("menu_item", "press a menu item by path, e.g. File > Export", "click", _x_menu, _e_menu_item, None,
          _p(r"\bmenu\b|\s>\s")),
    Skill("open_url", "open a website or URL in the browser", "full", _x_url, _e_open_url, _c_browser,
          _p(r"\b(open|go to|visit|load|navigate)\b.*\b(https?://|\.(?:" + _TLDS + r")\b|\bdot\b)")),
    Skill("web_search", "search the web for a query in the browser", "full", _x_query, _e_web_search, _c_browser,
          _p(r"\b(google|search|look ?up)\b")),
    Skill("take_photo", "take a picture with Photo Booth", "click", _x_none, _e_take_photo, None,
          _p(r"\b(take|snap|shoot)\b.*\b(picture|photo|selfie|pic)\b", r"\bphoto booth\b.*\b(take|picture|photo)\b")),
    Skill("screenshot", "capture the screen", "read", _x_none, _e_screenshot, None,
          _p(r"\b(screen ?shot|capture the screen)\b")),
    Skill("set_title", "make the title or first line of the current note say a phrase", "full", _x_text,
          _e_type_text, None, _p(r"\b(title|heading|name it|call it)\b.*\b(say|says|be|read)\b")),
    Skill("new_document", "create a new note, document, window, or tab in the frontmost app", "full", _x_none,
          _e_new_document, None,
          _p(r"\b(create|make|start|open)\b(?:\s+\w+){0,2}\s+new\s+(note|document|doc|file|window|tab|message)\b",
             r"^\s*new (note|document|doc|window|tab)\b")),
    Skill("type_text", "type a phrase into the focused field", "full", _x_text, _e_type_text, None,
          _p(r"\b(type|write|enter|say)\b")),
    Skill("open_app", "open or switch to an application by name", "click", _x_app, _e_open_app, _c_open_app,
          _p(r"\b(open|launch|start|switch to|go to|bring up)\b")),
)


def skill_map(skills: Sequence[Skill] = BUILTIN_SKILLS) -> dict[str, Skill]:
    return {s.name: s for s in skills}


# --------------------------------------------------------------------------- #
# routers
# --------------------------------------------------------------------------- #


class LocalRouter:
    """Regex routing over the built-in skills. No network, no model."""

    backend = "local"

    def __init__(self, skills: Sequence[Skill] = BUILTIN_SKILLS) -> None:
        self.skills = tuple(skills)

    def route(self, transcript: str, ctx: ReflexContext) -> RouteResult:
        started = time.perf_counter()
        low = normalize(transcript)
        for skill in self.skills:
            if not any(p.search(low) or p.search(transcript) for p in skill.patterns):
                continue
            slots = skill.extract(transcript, ctx)
            if slots is None:
                continue
            return RouteResult(skill.name, slots, 0.9, (time.perf_counter() - started) * 1000, self.backend)
        return RouteResult(None, {}, None, (time.perf_counter() - started) * 1000, self.backend,
                           "no skill matched")


#: A `needs_clarification` score at or above this declines the command. The
#: noul is noisy on short imperatives ("take a picture of me" scored 0.73 with
#: the skill chosen at 0.9 confidence), so only a clear signal declines.
CLARIFY_THRESHOLD = 0.85
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
API_KEY_VAR = "TYPESAFE_API_KEY"


def load_dotenv_key(name: str = API_KEY_VAR, path: str | os.PathLike[str] = ".env") -> str | None:
    """``name`` from the environment, else from a ``KEY=value`` line in ``path``."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


class JevRouter:
    """One Choice over the skills answered by Jev; slots stay deterministic.

    Falls back to `LocalRouter` on any transport failure and records which
    backend answered, so a timeline never hides a fallback.
    """

    backend = "jev"

    def __init__(self, api_key: str | None = None, *, skills: Sequence[Skill] = BUILTIN_SKILLS,
                 url: str = JEV_URL, model: str = JEV_MODEL, timeout_s: float = 2.0,
                 urlopen: Callable[..., object] = urllib.request.urlopen,
                 fallback: LocalRouter | None = None) -> None:
        self.api_key = api_key or load_dotenv_key()
        self.skills = tuple(skills)
        self.url, self.model, self.timeout_s = url, model, timeout_s
        self._urlopen = urlopen
        self.fallback = fallback if fallback is not None else LocalRouter(skills)
        self.last_error: str | None = None

    def build_request(self, transcript: str, ctx: ReflexContext, frontmost: str | None) -> dict:
        criteria = {s.name: s.description for s in self.skills}
        criteria["none"] = "no listed skill fits, or the request needs clarification"
        return {
            "model": self.model,
            "state": {
                "transcript": transcript,
                "frontmost_app": frontmost,
                "installed_apps": list(ctx.apps)[:60],
                "recent_commands": ctx.recent[-5:],
            },
            "questions": {
                "skill": {"type": "choice",
                          "instructions": "Which single skill carries out the spoken command?",
                          "criteria": criteria},
                "needs_clarification": {"type": "noul",
                                        "instructions": "The command is too vague to act on without asking."},
            },
        }

    def route(self, transcript: str, ctx: ReflexContext, frontmost: str | None = None) -> RouteResult:
        if not self.api_key:
            self.last_error = f"{API_KEY_VAR} is not set"
            result = self.fallback.route(transcript, ctx)
            result.backend = "local-fallback"
            result.reason = self.last_error
            return result
        started = time.perf_counter()
        body = json.dumps(self.build_request(transcript, ctx, frontmost)).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with self._urlopen(request, timeout=self.timeout_s) as response:  # type: ignore[attr-defined]
                raw = response.read()
            data = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code}"
            return self._fallback(transcript, ctx, started)
        except (OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
            return self._fallback(transcript, ctx, started)
        latency = (time.perf_counter() - started) * 1000
        answers = data.get("answers") or {}
        choice = (answers.get("skill") or {}).get("choice")
        confidence = (answers.get("skill") or {}).get("confidence")
        clarify = (answers.get("needs_clarification") or {}).get("noul")
        try:
            clarify_f = float(clarify) if clarify is not None else 0.0
        except (TypeError, ValueError):
            clarify_f = 0.0
        skills = skill_map(self.skills)
        if choice not in skills or clarify_f >= CLARIFY_THRESHOLD:
            return RouteResult(None, {}, float(confidence) if isinstance(confidence, (int, float)) else None,
                               latency, self.backend, f"jev chose {choice!r}; clarification {clarify_f:.2f}")
        slots = skills[choice].extract(transcript, ctx)
        if slots is None:
            return RouteResult(None, {}, None, latency, self.backend,
                               f"jev chose {choice} but no slot could be extracted from the transcript")
        return RouteResult(choice, slots, float(confidence) if isinstance(confidence, (int, float)) else None,
                           latency, self.backend)

    def _fallback(self, transcript: str, ctx: ReflexContext, started: float) -> RouteResult:
        result = self.fallback.route(transcript, ctx)
        result.latency_ms = (time.perf_counter() - started) * 1000
        result.backend = "local-fallback"
        result.reason = self.last_error or ""
        return result


def get_router(name: str, **kwargs: object) -> LocalRouter | JevRouter:
    if name == "local":
        return LocalRouter()
    if name == "jev":
        return JevRouter(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown router {name!r}; use local or jev")


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #


class ReflexRunner:
    """Route and execute utterances against a `Runtime`, measuring each stage."""

    def __init__(self, runtime: object, router: LocalRouter | JevRouter, ctx: ReflexContext | None = None,
                 *, skills: Sequence[Skill] = BUILTIN_SKILLS) -> None:
        self.runtime = runtime
        self.router = router
        self.ctx = ctx if ctx is not None else ReflexContext()
        self.skills = skill_map(skills)
        self.results: list[CommandResult] = []

    def _frontmost(self) -> str | None:
        try:
            return self.runtime._frontmost()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None

    def handle(self, transcript: str, *, stt_ms: float | None = None) -> list[CommandResult]:
        out: list[CommandResult] = []
        for clause in split_commands(transcript) or [transcript]:
            out.append(self._one(clause, stt_ms))
            stt_ms = None  # the endpointing cost belongs to the first clause only
        self.results.extend(out)
        return out

    def _one(self, clause: str, stt_ms: float | None) -> CommandResult:
        if isinstance(self.router, JevRouter):
            route = self.router.route(clause, self.ctx, self._frontmost())
        else:
            route = self.router.route(clause, self.ctx)
        if route.skill is None:
            return CommandResult(clause, None, {}, False, route.reason or "no skill matched", route.latency_ms,
                                 0.0, 0.0, route.backend, stt_ms)
        skill = self.skills[route.skill]
        started = time.perf_counter()
        try:
            outcome = skill.execute(self.runtime, route.slots, self.ctx)
            ok = True
        except ComputerUseError as exc:
            outcome, ok = f"{exc.code.value}: {exc}", False
        except Exception as exc:  # noqa: BLE001 - ActionRefused and driver errors
            outcome, ok = f"{type(exc).__name__}: {str(exc)[:160]}", False
        act_ms = (time.perf_counter() - started) * 1000
        check: str | None = None
        check_ms = 0.0
        if ok and skill.check is not None:
            started = time.perf_counter()
            check = skill.check(self.runtime, route.slots, self.ctx)
            check_ms = min((time.perf_counter() - started) * 1000, CHECK_BUDGET_S * 1000)
        self.ctx.recent.append(f"{route.skill} {route.slots}")
        return CommandResult(clause, route.skill, route.slots, ok, outcome, route.latency_ms, act_ms,
                             check_ms, route.backend, stt_ms, check)
