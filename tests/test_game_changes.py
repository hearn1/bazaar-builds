from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from automated_builds_pipeline.game_changes import (
    GameChangeSignalError,
    load_default_signals,
    load_signals,
    parse_signals,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_signals(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def valid_payload():
    return {
        "schema_version": 1,
        "signals": [
            {
                "id": "removed-card-2026-05-01",
                "type": "removed_card",
                "item": "Old Support",
                "effective_date": "2026-05-01",
                "source_url": "https://example.test/patch-notes",
                "note": "Old Support was removed from the live item pool.",
                "patch": "0.3.1",
                "metadata": {"source": "patch_notes"},
            }
        ],
    }


def _make_signal(**overrides):
    """Build a minimal valid signal dict with optional field overrides."""
    base = {
        "id": "test-signal-2026-06-01",
        "type": "removed_card",
        "item": "Test Item",
        "effective_date": "2026-06-01",
        "source_url": "https://example.test/source",
        "note": "Test note.",
    }
    base.update(overrides)
    return base


def _make_doc(*signals):
    return {"schema_version": 1, "signals": list(signals)}


def test_loads_valid_signal_file(tmp_path):
    path = tmp_path / "signals.json"
    write_signals(path, valid_payload())

    signals = load_signals(path)

    assert len(signals.signals) == 1
    signal = signals.signals[0]
    assert signal.id == "removed-card-2026-05-01"
    assert signal.type == "removed_card"
    assert signal.item == "Old Support"
    assert signal.effective_date == date(2026, 5, 1)
    assert signal.effective_date_iso == "2026-05-01"
    assert signal.source_url == "https://example.test/patch-notes"
    assert signal.note == "Old Support was removed from the live item pool."
    assert signal.patch == "0.3.1"
    assert signal.metadata == {"source": "patch_notes"}


def test_missing_required_field_fails_closed():
    payload = valid_payload()
    del payload["signals"][0]["note"]

    with pytest.raises(GameChangeSignalError, match="missing required fields: note"):
        parse_signals(payload)


def test_future_schema_fails_closed():
    payload = valid_payload()
    payload["schema_version"] = 2

    with pytest.raises(GameChangeSignalError, match="unsupported schema_version 2"):
        parse_signals(payload)


def test_unknown_signal_type_fails_closed():
    payload = valid_payload()
    payload["signals"][0]["type"] = "balance_vibes"

    with pytest.raises(GameChangeSignalError, match="unsupported type: balance_vibes"):
        parse_signals(payload)


def test_unknown_record_field_rejected():
    payload = valid_payload()
    payload["signals"][0]["extra"] = "nope"

    with pytest.raises(GameChangeSignalError, match="unknown fields: extra"):
        parse_signals(payload)


def test_metadata_must_be_object_when_present():
    payload = valid_payload()
    payload["signals"][0]["metadata"] = ["patch_notes"]

    with pytest.raises(GameChangeSignalError, match="metadata must be an object"):
        parse_signals(payload)


def test_default_missing_path_returns_empty_set(tmp_path):
    signals = load_default_signals(repo_root=tmp_path)

    assert signals.signals == ()


def test_explicit_missing_path_fails_closed(tmp_path):
    with pytest.raises(GameChangeSignalError, match="does not exist"):
        load_signals(tmp_path / "game_changes" / "signals.json")


def test_explicit_malformed_json_fails_closed(tmp_path):
    path = tmp_path / "signals.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(GameChangeSignalError, match="Malformed game-change signal JSON"):
        load_signals(path)


def test_global_matching_applies_to_any_hero():
    payload = valid_payload()
    payload["signals"].append(
        {
            "id": "watchlist-2026-05-02",
            "type": "watchlist",
            "item": "Global Item",
            "effective_date": "2026-05-02",
            "source_url": "https://example.test/watchlist",
            "note": "Needs monitoring.",
        }
    )

    signals = parse_signals(payload)

    matches = signals.matching(hero="Karnok", item="Global Item")
    assert [signal.id for signal in matches] == ["watchlist-2026-05-02"]


def test_hero_specific_matching_filters_other_heroes():
    payload = valid_payload()
    payload["signals"][0]["hero"] = "Karnok"

    signals = parse_signals(payload)

    assert [signal.id for signal in signals.matching(hero="Karnok", item="Old Support")] == [
        "removed-card-2026-05-01"
    ]
    assert signals.matching(hero="Vanessa", item="Old Support") == []
    assert signals.for_hero("Karnok")[0].hero == "Karnok"


def test_renamed_card_preserves_replacement_metadata():
    payload = {
        "schema_version": 1,
        "signals": [
            {
                "id": "rename-2026-05-03",
                "type": "renamed_card",
                "item": "Old Name",
                "replacement_item": "New Name",
                "effective_date": "2026-05-03",
                "source_url": "https://example.test/rename",
                "note": "Old Name was renamed to New Name.",
            }
        ],
    }

    signals = parse_signals(payload)
    signal = signals.for_item("Old Name")[0]

    assert signal.replacement_item == "New Name"
    assert signal.to_dict()["replacement_item"] == "New Name"


def test_source_refs_and_notes_preserved_in_serialized_form():
    signals = parse_signals(valid_payload())

    serialized = signals.signals[0].to_dict()

    assert serialized["source_url"] == "https://example.test/patch-notes"
    assert serialized["note"] == "Old Support was removed from the live item pool."
    assert serialized["effective_date"] == "2026-05-01"


# ---- valid parse for every signal type ----


@pytest.mark.parametrize(
    "signal_type",
    ["removed_card", "renamed_card", "explicit_invalidation", "major_nerf", "watchlist"],
)
def test_valid_parse_for_every_signal_type(signal_type):
    signal = _make_signal(type=signal_type, id=f"{signal_type}-2026-06-01")
    signals = parse_signals(_make_doc(signal))

    assert len(signals.signals) == 1
    s = signals.signals[0]
    assert s.type == signal_type
    assert s.item == "Test Item"
    assert s.source_url == "https://example.test/source"
    assert s.note == "Test note."
    assert s.effective_date_iso == "2026-06-01"
    assert s.to_dict()["type"] == signal_type


# ---- metadata preservation ----


def test_metadata_recommended_keys_survive_parse_and_serialization():
    metadata = {
        "source_kind": "patch_notes",
        "source_title": "Patch 1.2 Notes",
        "source_accessed_at": "2026-06-01T10:00:00Z",
        "confidence": "high",
        "status": "confirmed",
        "uncertainty_note": "None identified.",
    }
    signal = _make_signal(metadata=metadata)
    signals = parse_signals(_make_doc(signal))

    s = signals.signals[0]
    assert isinstance(s.metadata, dict)
    assert s.metadata == metadata
    assert s.to_dict()["metadata"] == metadata


# ---- rename coverage ----


def test_renamed_card_without_replacement_item_is_accepted():
    signal = _make_signal(type="renamed_card")
    signals = parse_signals(_make_doc(signal))

    assert signals.signals[0].replacement_item is None


# ---- empty signals ----


def test_empty_signals_array_parses_successfully():
    signals = parse_signals({"schema_version": 1, "signals": []})

    assert signals.signals == ()


# ---- required field enforcement (table-driven) ----


@pytest.mark.parametrize("field", ["id", "type", "item", "effective_date", "source_url", "note"])
def test_each_missing_required_field_fails_closed(field):
    signal = _make_signal()
    del signal[field]

    with pytest.raises(GameChangeSignalError):
        parse_signals(_make_doc(signal))


@pytest.mark.parametrize("field", ["id", "item", "source_url", "note"])
def test_empty_required_string_fails_closed(field):
    signal = _make_signal(**{field: ""})

    with pytest.raises(GameChangeSignalError, match=f"{field} must be a non-empty string"):
        parse_signals(_make_doc(signal))


# ---- unknown fields fail closed ----


def test_unknown_top_level_field_rejected():
    doc = {"schema_version": 1, "signals": [], "extra_field": True}

    with pytest.raises(GameChangeSignalError, match="unknown top-level fields: extra_field"):
        parse_signals(doc)


# ---- date validation ----


@pytest.mark.parametrize("bad_date", ["2026-99-99", "not-a-date"])
def test_invalid_effective_date_fails_closed(bad_date):
    signal = _make_signal(effective_date=bad_date)

    with pytest.raises(GameChangeSignalError, match="effective_date must be an ISO date"):
        parse_signals(_make_doc(signal))


# ---- malformed document shapes ----


def test_document_is_list_fails_closed():
    with pytest.raises(GameChangeSignalError, match="signal document must be a JSON object"):
        parse_signals([])


def test_signals_key_missing_fails_closed():
    with pytest.raises(GameChangeSignalError, match="signals must be a list"):
        parse_signals({"schema_version": 1})


def test_signals_not_list_fails_closed():
    with pytest.raises(GameChangeSignalError, match="signals must be a list"):
        parse_signals({"schema_version": 1, "signals": {"key": "value"}})


def test_signal_record_not_object_fails_closed():
    with pytest.raises(GameChangeSignalError, match="must be an object"):
        parse_signals({"schema_version": 1, "signals": ["not-an-object"]})


@pytest.mark.parametrize("field", ["hero", "replacement_item", "patch"])
def test_empty_optional_string_fails_closed(field):
    signal = _make_signal(**{field: ""})

    with pytest.raises(GameChangeSignalError, match=f"{field} must be a non-empty string when present"):
        parse_signals(_make_doc(signal))


# ---- matching helpers ----


def test_matching_invalid_signal_type_filter_raises():
    signals = parse_signals({"schema_version": 1, "signals": []})

    with pytest.raises(GameChangeSignalError, match="Unknown signal type filter"):
        signals.matching(signal_types={"invalid_type"})


def test_for_item_matching_is_case_insensitive():
    signal = _make_signal(item="Shiny Sword")
    signals = parse_signals(_make_doc(signal))

    assert len(signals.for_item("shiny sword")) == 1
    assert len(signals.for_item("SHINY SWORD")) == 1
    assert signals.for_item("Other Item") == []


# ---- real fixture: Burning Rage → Burning Infernal (Patch 15.0) ----
# First source-backed renamed_card in the live signals file.
# Skills are not tracked by the evaluator, so this is parser-level only;
# it also serves as a regression anchor for the name-reuse hazard (the
# same patch added a new Karnok skill also called "Burning Rage").


def test_real_signals_file_loads_burning_rage_rename():
    signals = load_default_signals(repo_root=REPO_ROOT)

    matches = signals.for_item("Burning Rage")
    assert len(matches) == 1
    sig = matches[0]
    assert sig.id == "renamed-burning-rage-2026-06-03"
    assert sig.type == "renamed_card"
    assert sig.item == "Burning Rage"
    assert sig.replacement_item == "Burning Infernal"
    assert sig.effective_date == date(2026, 6, 3)
    assert sig.patch == "15.0"
    assert sig.hero is None
    assert sig.source_url == "https://playthebazaar.com/patch-notes"


def test_real_signals_burning_rage_metadata_is_complete():
    signals = load_default_signals(repo_root=REPO_ROOT)
    sig = signals.for_item("Burning Rage")[0]

    assert sig.metadata is not None
    assert sig.metadata["source_kind"] == "official_patch_notes"
    assert sig.metadata["source_title"] == "Patch 15.0: Summer Sparks"
    assert sig.metadata["confidence"] == "medium"
    assert sig.metadata["source_excerpt"] == "Burning Rage — Renamed to Burning Infernal"
    assert "uncertainty_note" in sig.metadata


def test_global_renamed_card_applies_to_any_hero():
    # Global scope (no hero) means the signal matches any hero query.
    sig = _make_signal(type="renamed_card", replacement_item="New Name")
    sig.pop("hero", None)
    parsed = parse_signals(_make_doc(sig))

    assert parsed.matching(hero="Karnok", item="Test Item") != []
    assert parsed.matching(hero="Vanessa", item="Test Item") != []
    assert parsed.matching(hero="Jules", item="Test Item") != []


def test_global_renamed_card_full_metadata_round_trips():
    metadata = {
        "source_kind": "official_patch_notes",
        "source_title": "Patch 15.0: Summer Sparks",
        "source_published_at": "2026-06-03",
        "source_accessed_at": "2026-06-06",
        "confidence": "medium",
        "curator": "hearn1",
        "status": "proposed",
        "source_excerpt": "Burning Rage — Renamed to Burning Infernal",
        "affected_scope": "global",
        "old_item": "Burning Rage",
        "new_item": "Burning Infernal",
        "uncertainty_note": "Name reused in same patch for a different card.",
    }
    signal = _make_signal(
        type="renamed_card",
        item="Burning Rage",
        replacement_item="Burning Infernal",
        patch="15.0",
        metadata=metadata,
    )
    signal.pop("hero", None)
    parsed = parse_signals(_make_doc(signal))

    sig = parsed.signals[0]
    assert sig.hero is None
    assert sig.replacement_item == "Burning Infernal"
    assert sig.patch == "15.0"
    assert sig.metadata == metadata
    serialized = sig.to_dict()
    assert serialized["replacement_item"] == "Burning Infernal"
    assert serialized["patch"] == "15.0"
    assert serialized["metadata"] == metadata
    assert "hero" not in serialized
