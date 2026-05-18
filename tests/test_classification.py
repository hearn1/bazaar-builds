from __future__ import annotations

import pytest

from automated_builds_pipeline.classification import validate_classifications


def test_unknown_items_are_coerced_to_invalid():
    result = validate_classifications(
        [
            {"item": "Pufferfish", "classification": "carry", "confidence": "high", "rationale": "known"},
            {"item": "Invented Thing", "classification": "carry", "confidence": "high", "rationale": "unknown"},
        ],
        {"Pufferfish"},
    )

    assert result[0].classification == "carry"
    assert result[1].classification == "invalid"
    assert result[1].surface == "suppressed"


@pytest.mark.parametrize(
    ("confidence", "surface"),
    [("high", "top_line"), ("medium", "weaker_signal"), ("low", "suppressed")],
)
def test_confidence_filter_surface_flags(confidence, surface):
    result = validate_classifications(
        [{"item": "Pufferfish", "classification": "support", "confidence": confidence, "rationale": "x"}],
        {"Pufferfish"},
    )

    assert result[0].surface == surface
