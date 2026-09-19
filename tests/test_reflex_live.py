"""Optional live checks for the reflex layer: a Jev round trip when a key is
present, and the on-device speech recogniser when the grants are in. Both
skip themselves otherwise, so the hermetic suite never touches the network
or the microphone."""

from __future__ import annotations

import os
import sys

import pytest

from a11y_computer_use import reflex, voice

pytestmark = pytest.mark.skipif(os.environ.get("A11Y_COMPUTER_USE_LIVE") != "1",
                                reason="set A11Y_COMPUTER_USE_LIVE=1 for live checks")


@pytest.mark.skipif(not reflex.load_dotenv_key(), reason="TYPESAFE_API_KEY is not set")
def test_jev_routes_a_demo_command_live() -> None:
    router = reflex.JevRouter(timeout_s=6.0)
    ctx = reflex.ReflexContext(apps=["Notes", "Google Chrome", "Photo Booth"])
    result = router.route("open up the Notes app", ctx, frontmost="com.apple.finder")
    assert result.backend in ("jev", "local-fallback"), router.last_error
    assert result.skill == "open_app" and result.slots == {"app": "Notes"}
    assert result.latency_ms > 0


@pytest.mark.skipif(sys.platform != "darwin" or not voice.speech_available(),
                    reason="Apple Speech is macOS only")
def test_speech_recognizer_is_available_when_granted() -> None:
    status = voice.authorization_status()
    if status["speech"] != "authorized" or status["microphone"] != "authorized":
        pytest.skip(f"speech grants not in: {status}")
    import Speech
    from Foundation import NSLocale

    recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(
        NSLocale.localeWithLocaleIdentifier_("en-US"))
    assert recognizer is not None and recognizer.isAvailable()
