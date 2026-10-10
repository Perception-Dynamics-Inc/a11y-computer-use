"""Hermetic checks for the optional local grounding adapter. No weights are loaded."""

from __future__ import annotations

import pytest

from a11y_computer_use.agent.grounding import (
    HOLO_MODEL_ID,
    GroundingUnavailable,
    HoloLocalGrounding,
    parse_grounding_text,
)


def test_parse_image_pixels_and_the_normalized_grid() -> None:
    pixels = parse_grounding_text('{"point": [10, 20], "space": "image"}', 2000, 1000)
    assert pixels is not None
    assert (pixels.x, pixels.y) == (10, 20)
    assert pixels.box is None

    scaled = parse_grounding_text('{"point": [500, 250], "space": "norm1000"}', 2000, 1000)
    assert scaled is not None
    assert (scaled.x, scaled.y) == (1000, 250)

    implied = parse_grounding_text('{"point": [500, 500]}', 2000, 2000)
    assert implied is not None
    assert (implied.x, implied.y) == (1000, 1000)

    small = parse_grounding_text('{"point": [40, 12]}', 200, 80)
    assert small is not None
    assert (small.x, small.y) == (40, 12)

    box = parse_grounding_text('{"box": [0, 0, 100, 40], "space": "image"}', 200, 80)
    assert box is not None
    assert box.box == (0, 0, 100, 40)
    assert (box.x, box.y) == (50, 20)


def test_holo_adapter_accepts_only_its_model_id() -> None:
    model = HoloLocalGrounding()
    assert model.model_id == HOLO_MODEL_ID
    with pytest.raises(ValueError):
        HoloLocalGrounding("some/other-model")


def test_holo_ground_reports_a_missing_local_stack(monkeypatch) -> None:
    def missing():
        raise GroundingUnavailable(
            "local grounding needs torch and transformers, which are not installed"
        )

    monkeypatch.setattr(
        "a11y_computer_use.agent.grounding._torch_stack",
        missing,
    )
    with pytest.raises(GroundingUnavailable, match="torch"):
        HoloLocalGrounding().ground(b"not a png", "click the red square")
