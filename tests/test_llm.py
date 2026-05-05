from __future__ import annotations

from pathlib import Path

import pytest

from automated_builds_pipeline.llm import LLMClassifier, validate_classifications


class TransientError(Exception):
    status_code = 429


class FakeMessages:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return payload


class FakeClient:
    def __init__(self, payloads):
        self.messages = FakeMessages(payloads)


def response(items):
    return {"content": [{"type": "tool_use", "name": "classify_items", "input": {"items": items}}]}


@pytest.fixture
def names_file(tmp_path: Path) -> Path:
    path = tmp_path / "card_cache_names.txt"
    path.write_text("Pufferfish\nYo-Yo\n", encoding="utf-8")
    return path


def test_prompt_assembly_uses_rules_block_and_evidence(names_file):
    client = FakeClient([response([{"item": "Pufferfish", "classification": "core", "confidence": "high", "rationale": "bazaardb confirms"}])])
    classifier = LLMClassifier(known_items_path=names_file, client=client)

    result = classifier.classify_archetype(
        "Karnok",
        "early",
        "Axe",
        {"core_items": ["Yo-Yo"]},
        [{"item": "Pufferfish", "classification_ceiling": "carry_core_support"}],
        {"source": "bazaardb"},
        "Pufferfish is the engine.",
    )

    call = client.messages.calls[0]
    assert "You classify Bazaar build items for one hero archetype." in call["system"][0]["text"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert '"item": "Pufferfish"' in call["messages"][0]["content"]
    assert '"mobalytics_description": "Pufferfish is the engine."' in call["messages"][0]["content"]
    assert result[0].classification == "core"
    assert result[0].surface == "top_line"


def test_retry_on_429_then_parses_structured_output(monkeypatch, names_file):
    monkeypatch.setattr("automated_builds_pipeline.llm.time.sleep", lambda _: None)
    client = FakeClient([TransientError(), response([{"item": "Yo-Yo", "classification": "support", "confidence": "medium", "rationale": "role inferred"}])])
    classifier = LLMClassifier(known_items_path=names_file, client=client)

    result = classifier.classify_archetype("Karnok", None, "Axe", {}, [{"item": "Yo-Yo"}], {}, None)

    assert len(client.messages.calls) == 2
    assert result[0].surface == "weaker_signal"


def test_non_transient_error_surfaces(names_file):
    client = FakeClient([ValueError("bad response")])
    classifier = LLMClassifier(known_items_path=names_file, client=client)

    with pytest.raises(ValueError, match="bad response"):
        classifier.classify_archetype("Karnok", None, "Axe", {}, [{"item": "Yo-Yo"}], {}, None)


def test_unknown_items_are_coerced_to_invalid(names_file):
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
