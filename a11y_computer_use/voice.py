"""Voice input for the reflex layer: on-device speech on macOS, or text.

Three listeners share one contract, ``listen(on_utterance, stop)``, where
``on_utterance(text, endpoint_ms)`` is called once per committed utterance:

* `SpeechListener`: Apple's Speech framework through pyobjc, streaming partial
  results from an `AVAudioEngine` input tap, on-device when the recogniser
  supports it. An utterance is committed after ``silence_s`` without a change
  in the partial transcript, or on a final result; the wait is reported as the
  endpoint latency.
* `PushToTalkListener`: the deterministic fallback; press Return to start and
  Return again to commit what was heard in between.
* `TextListener`: a transcript, or a file with one utterance per line, so the
  whole pipeline runs without a microphone.

Microphone and Speech Recognition are TCC permissions attached to the
responsible app (your terminal, or the host that spawned the process).
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterable

OnUtterance = Callable[[str, float | None], None]

AUTH_STATUS = {0: "not_determined", 1: "denied", 2: "restricted", 3: "authorized"}


def speech_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import AVFoundation  # noqa: F401
        import Speech  # noqa: F401
    except ImportError:
        return False
    return True


def authorization_status() -> dict[str, str]:
    """Speech Recognition and Microphone authorization, as strings."""
    if not speech_available():
        return {"speech": "unavailable", "microphone": "unavailable"}
    import AVFoundation
    import Speech

    speech = AUTH_STATUS.get(int(Speech.SFSpeechRecognizer.authorizationStatus()), "unknown")
    mic = AUTH_STATUS.get(int(AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_("soun")), "unknown")
    return {"speech": speech, "microphone": mic}


def request_authorization(timeout_s: float = 60.0) -> dict[str, str]:
    """Prompt for both permissions (the system dialogs) and wait for answers."""
    if not speech_available():
        return authorization_status()
    import AVFoundation
    import Speech

    done = threading.Event()
    Speech.SFSpeechRecognizer.requestAuthorization_(lambda _status: done.set())
    done.wait(timeout_s)
    done.clear()
    AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_("soun", lambda _ok: done.set())
    done.wait(timeout_s)
    return authorization_status()


class TextListener:
    """Feed utterances from a string or a file; no audio involved."""

    def __init__(self, source: str | Iterable[str]) -> None:
        self.source = source

    def utterances(self) -> list[str]:
        if isinstance(self.source, str):
            try:
                with open(self.source, encoding="utf-8") as fh:
                    lines = [line.strip() for line in fh]
                return [line for line in lines if line and not line.startswith("#")]
            except OSError:
                return [self.source]
        return [line for line in self.source if line.strip()]

    def listen(self, on_utterance: OnUtterance, stop: threading.Event) -> None:
        for text in self.utterances():
            if stop.is_set():
                return
            on_utterance(text, None)


class SpeechListener:
    """On-device streaming recognition with silence endpointing."""

    def __init__(self, locale: str = "en-US", silence_s: float = 0.5, on_device: bool = True,
                 partial: Callable[[str], None] | None = None) -> None:
        self.locale, self.silence_s, self.on_device, self.partial = locale, silence_s, on_device, partial
        self._lock = threading.Lock()
        self._text = ""
        self._changed_at: float | None = None
        self._final = False
        self._error: str | None = None
        self._request = None
        self._task = None

    # -- recognition session ------------------------------------------------

    def _new_request(self, recognizer):
        import Speech

        request = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
        request.setShouldReportPartialResults_(True)
        if self.on_device and recognizer.supportsOnDeviceRecognition():
            request.setRequiresOnDeviceRecognition_(True)
        with self._lock:
            self._text, self._changed_at, self._final, self._error = "", None, False, None
            self._request = request

        def handler(result, error):
            with self._lock:
                if error is not None and not self._text:
                    self._error = str(error)
                if result is not None:
                    text = str(result.bestTranscription().formattedString() or "")
                    if text != self._text:
                        self._text, self._changed_at = text, time.monotonic()
                        if self.partial:
                            self.partial(text)
                    if result.isFinal():
                        self._final = True

        self._task = recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)
        return request

    def listen(self, on_utterance: OnUtterance, stop: threading.Event) -> None:
        if not speech_available():
            raise RuntimeError("the Speech framework is not available; use --text or --push-to-talk")
        import AVFoundation
        import Speech
        from Foundation import NSLocale, NSRunLoop, NSDate

        status = authorization_status()
        if status["speech"] != "authorized" or status["microphone"] != "authorized":
            status = request_authorization()
        if status["speech"] != "authorized" or status["microphone"] != "authorized":
            raise RuntimeError(f"speech permissions: {status}; grant Speech Recognition and Microphone "
                               "to the responsible app in System Settings > Privacy & Security")
        recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(
            NSLocale.localeWithLocaleIdentifier_(self.locale))
        if recognizer is None or not recognizer.isAvailable():
            raise RuntimeError(f"no speech recognizer available for {self.locale}")
        engine = AVFoundation.AVAudioEngine.alloc().init()
        node = engine.inputNode()
        fmt = node.outputFormatForBus_(0)
        request = self._new_request(recognizer)

        def tap(buffer, _when):
            req = self._request
            if req is not None:
                req.appendAudioPCMBuffer_(buffer)

        node.installTapOnBus_bufferSize_format_block_(0, 1024, fmt, tap)
        engine.prepare()
        ok, error = engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"audio engine failed to start: {error}")
        try:
            while not stop.is_set():
                NSRunLoop.currentRunLoop().runMode_beforeDate_("kCFRunLoopDefaultMode",
                                                                NSDate.dateWithTimeIntervalSinceNow_(0.05))
                with self._lock:
                    text, changed_at, final, error_text = self._text, self._changed_at, self._final, self._error
                if error_text and not text:
                    with self._lock:
                        self._error = None
                    request = self._new_request(recognizer)
                    continue
                if text and changed_at is not None and (final or time.monotonic() - changed_at >= self.silence_s):
                    endpoint_ms = (time.monotonic() - changed_at) * 1000
                    request.endAudio()
                    if self._task is not None:
                        self._task.cancel()
                    request = self._new_request(recognizer)
                    on_utterance(text, endpoint_ms)
        finally:
            request.endAudio()
            if self._task is not None:
                self._task.cancel()
            node.removeTapOnBus_(0)
            engine.stop()


class PushToTalkListener(SpeechListener):
    """Return starts a segment, Return commits it (deterministic endpointing)."""

    def listen(self, on_utterance: OnUtterance, stop: threading.Event) -> None:
        import AVFoundation
        import Speech
        from Foundation import NSLocale, NSRunLoop, NSDate

        if not speech_available():
            raise RuntimeError("the Speech framework is not available; use --text")
        status = authorization_status()
        if status["speech"] != "authorized" or status["microphone"] != "authorized":
            status = request_authorization()
        if status["speech"] != "authorized" or status["microphone"] != "authorized":
            raise RuntimeError(f"speech permissions: {status}")
        recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(
            NSLocale.localeWithLocaleIdentifier_(self.locale))
        engine = AVFoundation.AVAudioEngine.alloc().init()
        node = engine.inputNode()
        fmt = node.outputFormatForBus_(0)
        node.installTapOnBus_bufferSize_format_block_(0, 1024, fmt, lambda buf, _w: self._request is not None
                                                      and self._request.appendAudioPCMBuffer_(buf))
        engine.prepare()
        ok, error = engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"audio engine failed to start: {error}")
        try:
            while not stop.is_set():
                print("press Return to talk, then Return again to commit (q to quit)", file=sys.stderr)
                if input().strip().lower() == "q":
                    return
                request = self._new_request(recognizer)
                started = time.monotonic()
                input()
                request.endAudio()
                committed = time.monotonic()
                deadline = committed + 2.0
                while time.monotonic() < deadline:
                    NSRunLoop.currentRunLoop().runMode_beforeDate_("kCFRunLoopDefaultMode",
                                                                    NSDate.dateWithTimeIntervalSinceNow_(0.05))
                    with self._lock:
                        if self._final:
                            break
                with self._lock:
                    text = self._text
                    self._request = None
                if self._task is not None:
                    self._task.cancel()
                if text:
                    on_utterance(text, (time.monotonic() - committed) * 1000)
                else:
                    print(f"(nothing recognised in {time.monotonic() - started:.1f}s)", file=sys.stderr)
        finally:
            node.removeTapOnBus_(0)
            engine.stop()
