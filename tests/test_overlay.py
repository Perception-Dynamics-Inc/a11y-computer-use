"""Overlay colour parsing. The Cocoa drawing/window code is GUI and eyeballed
via `overlay.demo()`; here we cover the one piece of pure logic — the
A11Y_COMPUTER_USE_OVERLAY_RGBA env override — which must never raise (a bad value
just falls back to the brand colour)."""

from __future__ import annotations

import pytest

overlay = pytest.importorskip("a11y_computer_use.overlay")


def test_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A11Y_COMPUTER_USE_OVERLAY_RGBA", raising=False)
    assert overlay._resolve_rgba() == overlay.BRAND_RGBA


def test_float_triplet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_OVERLAY_RGBA", "0.1,0.2,0.3")
    assert overlay._resolve_rgba() == (0.1, 0.2, 0.3, 1.0)


def test_byte_triplet_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_OVERLAY_RGBA", "255,128,0")
    assert overlay._resolve_rgba() == (1.0, 128 / 255, 0.0, 1.0)


def test_rgba_quadruple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_OVERLAY_RGBA", "0.1,0.2,0.3,0.5")
    assert overlay._resolve_rgba() == (0.1, 0.2, 0.3, 0.5)


@pytest.mark.parametrize("bad", ["nope", "1,2", "1,2,3,4,5", "", "a,b,c"])
def test_bad_values_fall_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("A11Y_COMPUTER_USE_OVERLAY_RGBA", bad)
    assert overlay._resolve_rgba() == overlay.BRAND_RGBA
