"""Fence UI-derived text, and decide whether a browser origin is allowed.

Fences are nonce-tagged boundaries around text that came from the screen, a
page, or the clipboard. A heuristic can mark text that reads like an
instruction to the model. The text is never removed.

Domain rules are hostnames or origins. A blocked rule always denies. When
the allow list is non-empty, the origin must match it as well. An empty
policy allows every origin, which is the backward-compatible default.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlparse

from a11y_computer_use.schema import ComputerUseError, ErrorCode

_FENCE_ENV = "A11Y_COMPUTER_USE_FENCE_UNTRUSTED"
_ALLOWED_ENV = "A11Y_COMPUTER_USE_ALLOWED_DOMAINS"
_BLOCKED_ENV = "A11Y_COMPUTER_USE_BLOCKED_DOMAINS"

_FENCED = re.compile(
    r"^<untrusted nonce=([^\s>]+)( suspicious=1)?>(.*)</untrusted nonce=\1>\Z",
    re.DOTALL,
)
_CLOSER = re.compile(r"</untrusted", re.IGNORECASE)
_ESCAPED_CLOSER = re.compile(r"&lt;/untrusted", re.IGNORECASE)

# Phrases that read like instructions to the model. Matched text is marked
# and still returned in full.
_INJECTION = (
    re.compile(r"ignore\s+(?:all\s+|any\s+|the\s+)?previous\s+instructions?\b", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"(?<![\w])system\s*:", re.IGNORECASE),
)

_SCHEME_ONLY = frozenset({"file", "http", "https", "data", "about", "chrome", "blob"})


def env_flag(name: str) -> bool:
    """True when ``name`` is ``1``, ``true``, ``yes``, or ``on``."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def looks_like_injection(text: str) -> bool:
    """True when ``text`` contains a prompt-injection phrase.

    The match is a flag only. Callers keep the original characters.
    """
    return any(pattern.search(text) for pattern in _INJECTION)


def looks_like_url(value: str) -> bool:
    """True when ``value`` is absolute enough to be a navigation target."""
    text = value.strip()
    return "://" in text or text.startswith(("about:", "data:", "file:", "chrome:", "blob:"))


def escape_untrusted(text: str) -> str:
    """Escape every closing untrusted tag so it cannot end the fence early."""
    return _CLOSER.sub(lambda match: "&lt;/" + match.group(0)[2:], text)


def unescape_untrusted(text: str) -> str:
    """Reverse :func:`escape_untrusted`."""
    return _ESCAPED_CLOSER.sub(lambda match: "</" + match.group(0)[5:], text)


@dataclass(frozen=True, slots=True)
class Fenced:
    """One fenced string and whether the heuristic marked it."""

    text: str
    suspicious: bool
    nonce: str


def fence(text: str, *, nonce: str | None = None) -> Fenced:
    """Wrap ``text`` in ``<untrusted nonce=…>…</untrusted nonce=…>``.

    A nonce that already occurs in ``text`` is replaced. Any closing tag
    inside the body is escaped. Text that looks like an instruction is
    marked ``suspicious=1`` on the opening tag and is not removed. A string
    that is already fenced is returned as-is, with the flag added when the
    body matches and the tag did not carry it.
    """
    existing = _FENCED.match(text)
    if existing is not None:
        found, flag, body = existing.group(1), existing.group(2), existing.group(3)
        suspicious = flag is not None or looks_like_injection(unescape_untrusted(body))
        if suspicious and flag is None:
            text = text.replace(
                f"<untrusted nonce={found}>",
                f"<untrusted nonce={found} suspicious=1>",
                1,
            )
        return Fenced(text, suspicious, found)

    suspicious = looks_like_injection(text)
    token = nonce if nonce else _fresh_nonce(text)
    body = escape_untrusted(text)
    attrs = f"nonce={token}"
    if suspicious:
        attrs += " suspicious=1"
    wrapped = f"<untrusted {attrs}>{body}</untrusted nonce={token}>"
    return Fenced(wrapped, suspicious, token)


def unwrap(text: str) -> str | None:
    """Return the body of a fence, or None when ``text`` is not fenced."""
    match = _FENCED.match(text)
    if match is None:
        return None
    return unescape_untrusted(match.group(3))


def _fresh_nonce(text: str) -> str:
    for _ in range(8):
        token = secrets.token_hex(8)
        if token not in text:
            return token
    return secrets.token_hex(16)


def parse_domain_list(value: str | object | None) -> tuple[str, ...]:
    """Split a comma-separated string or a sequence into domain rules."""
    if value is None:
        return ()
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = []
        try:
            items = list(value)  # type: ignore[arg-type]
        except TypeError:
            items = [value]
        for item in items:
            parts.extend(str(item).split(","))
    return tuple(part.strip() for part in parts if part.strip())


@dataclass(frozen=True, slots=True)
class DomainPolicy:
    """Allow and block lists for browser origins.

    ``blocked`` wins. A non-empty ``allowed`` list requires a match. Both
    empty means every origin is allowed.
    """

    allowed: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()

    @classmethod
    def resolve(
        cls,
        allowed: str | object | None,
        blocked: str | object | None,
    ) -> DomainPolicy:
        """Build a policy. ``None`` reads the process environment.

        An explicit empty string or empty sequence does not read the
        environment, so a caller can override a set variable with no rules.
        """
        if allowed is None:
            allowed = os.environ.get(_ALLOWED_ENV, "")
        if blocked is None:
            blocked = os.environ.get(_BLOCKED_ENV, "")
        return cls(parse_domain_list(allowed), parse_domain_list(blocked))

    @property
    def empty(self) -> bool:
        return not self.allowed and not self.blocked

    def allows(self, url: str) -> bool:
        """True when ``url`` may be navigated to or acted on."""
        if self.empty:
            return True
        if not url or not str(url).strip():
            return True
        if any(_rule_matches(url, rule) for rule in self.blocked):
            return False
        if self.allowed and not any(_rule_matches(url, rule) for rule in self.allowed):
            return False
        return True

    def check(self, url: str) -> None:
        """Raise `ErrorCode.DOMAIN_BLOCKED` when ``url`` is not allowed."""
        if self.allows(url):
            return
        raise ComputerUseError(
            ErrorCode.DOMAIN_BLOCKED,
            f"{url} is not an allowed origin",
            detail={
                "url": url,
                "allowed": list(self.allowed),
                "blocked": list(self.blocked),
            },
        )


def _origin(url: str) -> tuple[str, str]:
    parsed = urlparse(url.strip())
    if not parsed.scheme and not parsed.hostname:
        parsed = urlparse("//" + url.strip())
    return (parsed.scheme or "").lower(), (parsed.hostname or "").lower()


def _parse_rule(rule: str) -> tuple[str | None, str | None]:
    raw = rule.strip().lower()
    if not raw:
        return None, None
    if "://" in raw:
        parsed = urlparse(raw)
        return parsed.scheme or None, parsed.hostname
    bare = raw.rstrip("/")
    if bare.endswith(":") and ":" not in bare[:-1]:
        token = bare[:-1]
        return (token, None) if token else (None, None)
    if bare in _SCHEME_ONLY:
        return bare, None
    host = bare
    if host.startswith("*."):
        host = host[2:]
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end != -1 else host
    else:
        head, sep, tail = host.rpartition(":")
        if sep and tail.isdigit():
            host = head
    host = host.strip().strip(".")
    return None, host or None


def _is_ip(host: str) -> bool:
    if ":" in host:
        return True
    parts = host.split(".")
    return len(parts) == 4 and all(part.isdigit() for part in parts)


def _host_matches(host: str, rule_host: str) -> bool:
    host = host.lower().rstrip(".")
    rule = rule_host.lower().strip().strip(".")
    if rule.startswith("*."):
        rule = rule[2:]
    if host == rule:
        return True
    if _is_ip(host) or _is_ip(rule):
        return False
    return host.endswith("." + rule)


def _rule_matches(url: str, rule: str) -> bool:
    scheme, host = _origin(url)
    rule_scheme, rule_host = _parse_rule(rule)
    if rule_scheme is None and rule_host is None:
        return False
    if rule_scheme is not None and rule_scheme != scheme:
        return False
    if rule_host is not None:
        if not host or not _host_matches(host, rule_host):
            return False
    return True


__all__ = [
    "DomainPolicy",
    "Fenced",
    "escape_untrusted",
    "env_flag",
    "fence",
    "looks_like_injection",
    "looks_like_url",
    "parse_domain_list",
    "unescape_untrusted",
    "unwrap",
]
