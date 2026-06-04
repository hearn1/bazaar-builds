from __future__ import annotations

import json
from datetime import date

import pytest

from automated_builds_pipeline.game_changes import (
    GameChangeSignalError,
    load_default_signals,
    load_signals,
    parse_signals,
)


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
