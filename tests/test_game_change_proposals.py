"""Tests for automated_builds_pipeline.game_change_proposals."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from automated_builds_pipeline.game_change_proposals import build_candidate_signals, run
from automated_builds_pipeline.game_changes import SIGNAL_TYPES, parse_signals

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "game_change_proposals"


# ---- helpers ----


def _write_source(tmp_path: Path, content: str = "# Patch notes\nSome text.") -> Path:
    p = tmp_path / "source.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_candidates(tmp_path: Path, payload: dict, name: str = "candidates.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _base_payload(**overrides) -> dict:
    base: dict = {
        "source_url": "https://example.test/patch-notes",
        "source_title": "Patch 15.1 Notes",
        "source_kind": "patch_notes",
        "source_accessed_at": "2026-06-01T10:00:00Z",
        "effective_date": "2026-06-01",
        "patch": "15.1",
        "confidence": "high",
        "candidates": [
            {
                "type": "removed_card",
                "item": "Crystal Blade",
                "note": "Crystal Blade was removed.",
            }
        ],
    }
    base.update(overrides)
    return base


# ---- generates strict-compatible candidate output ----


def test_generates_strict_compatible_output(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(tmp_path, _base_payload())

    doc = build_candidate_signals(source, cfile)

    assert doc["schema_version"] == 1
    assert isinstance(doc["signals"], list)
    assert len(doc["signals"]) == 1
    parsed = parse_signals(doc)
    assert parsed.signals[0].metadata["status"] == "proposed"
    assert parsed.signals[0].metadata["curator"] == "manual"


def test_fixture_files_produce_valid_output():
    source = FIXTURE_DIR / "manual_patch_notes.md"
    cfile = FIXTURE_DIR / "manual_candidates.json"

    doc = build_candidate_signals(source, cfile)

    assert doc["schema_version"] == 1
    parsed = parse_signals(doc)
    assert all(s.metadata["status"] == "proposed" for s in parsed.signals)


# ---- all signal types ----


@pytest.mark.parametrize("signal_type", sorted(SIGNAL_TYPES))
def test_all_signal_types_supported(tmp_path, signal_type):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(
            candidates=[{"type": signal_type, "item": "Test Item", "note": f"Note for {signal_type}."}]
        ),
    )

    doc = build_candidate_signals(source, cfile)

    assert len(doc["signals"]) == 1
    assert doc["signals"][0]["type"] == signal_type
    parse_signals(doc)


def test_fixture_includes_all_signal_types():
    cfile = FIXTURE_DIR / "manual_candidates.json"
    source = FIXTURE_DIR / "manual_patch_notes.md"

    doc = build_candidate_signals(source, cfile)

    types_in_output = {s["type"] for s in doc["signals"]}
    assert types_in_output == SIGNAL_TYPES


# ---- rename includes replacement item ----


def test_rename_includes_replacement_item(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(
            candidates=[
                {
                    "type": "renamed_card",
                    "item": "Old Name",
                    "replacement_item": "New Name",
                    "note": "Old Name was renamed to New Name.",
                }
            ]
        ),
    )

    doc = build_candidate_signals(source, cfile)

    signal = doc["signals"][0]
    assert signal["replacement_item"] == "New Name"
    parsed = parse_signals(doc)
    assert parsed.signals[0].replacement_item == "New Name"


# ---- uncertainty surfaced ----


def test_uncertainty_is_surfaced(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(
            candidates=[
                {
                    "type": "watchlist",
                    "item": "Shadow Dagger",
                    "note": "Stats may change.",
                    "confidence": "low",
                    "uncertainty_note": "Not yet conclusive.",
                }
            ]
        ),
    )

    doc = build_candidate_signals(source, cfile)

    meta = doc["signals"][0]["metadata"]
    assert meta["confidence"] == "low"
    assert meta["uncertainty_note"] == "Not yet conclusive."


def test_top_level_confidence_propagates_to_signal(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(tmp_path, _base_payload(confidence="medium"))

    doc = build_candidate_signals(source, cfile)

    assert doc["signals"][0]["metadata"]["confidence"] == "medium"


def test_candidate_confidence_overrides_top_level(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(
            confidence="high",
            candidates=[
                {
                    "type": "removed_card",
                    "item": "Crystal Blade",
                    "note": "Removed.",
                    "confidence": "low",
                }
            ],
        ),
    )

    doc = build_candidate_signals(source, cfile)

    assert doc["signals"][0]["metadata"]["confidence"] == "low"


# ---- missing optional metadata surfaced ----


def test_missing_optional_metadata_listed(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        {
            "source_url": "https://example.test/patch",
            "effective_date": "2026-06-01",
            "candidates": [{"type": "removed_card", "item": "Old Sword", "note": "Removed."}],
        },
    )

    doc = build_candidate_signals(source, cfile)

    meta = doc["signals"][0]["metadata"]
    assert "missing_metadata" in meta
    assert set(meta["missing_metadata"]) >= {"source_kind", "source_title", "source_accessed_at", "confidence"}
    parse_signals(doc)


def test_present_optional_metadata_not_flagged_as_missing(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(tmp_path, _base_payload())

    doc = build_candidate_signals(source, cfile)

    meta = doc["signals"][0]["metadata"]
    assert "missing_metadata" not in meta


# ---- missing required field fails and does not write output ----


@pytest.mark.parametrize(
    "drop_key",
    ["source_url", "effective_date"],
)
def test_missing_top_level_required_field_fails(tmp_path, drop_key):
    source = _write_source(tmp_path)
    payload = _base_payload()
    del payload[drop_key]
    cfile = _write_candidates(tmp_path, payload)

    with pytest.raises(ValueError, match=drop_key):
        build_candidate_signals(source, cfile)


@pytest.mark.parametrize("drop_key", ["note", "item"])
def test_missing_candidate_required_field_fails(tmp_path, drop_key):
    source = _write_source(tmp_path)
    candidate = {"type": "removed_card", "item": "Test Item", "note": "Test note."}
    del candidate[drop_key]
    cfile = _write_candidates(tmp_path, _base_payload(candidates=[candidate]))

    with pytest.raises(ValueError):
        build_candidate_signals(source, cfile)


def test_unsupported_type_fails(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(candidates=[{"type": "balance_vibes", "item": "Test", "note": "Test."}]),
    )

    with pytest.raises(ValueError, match="unsupported type"):
        build_candidate_signals(source, cfile)


@pytest.mark.parametrize(
    "drop_key",
    ["source_url", "effective_date", "note", "item"],
)
def test_cli_missing_field_exits_nonzero_and_does_not_write(tmp_path, drop_key):
    source = _write_source(tmp_path)
    payload = _base_payload()

    if drop_key in ("source_url", "effective_date"):
        del payload[drop_key]
    else:
        candidate = {"type": "removed_card", "item": "Test Item", "note": "Test note."}
        del candidate[drop_key]
        payload["candidates"] = [candidate]

    cfile = _write_candidates(tmp_path, payload)
    output = tmp_path / "out.json"

    rc = run(["--source-file", str(source), "--candidate-file", str(cfile), "--output", str(output)])

    assert rc != 0
    assert not output.exists()


# ---- no overwrite without force ----


def test_no_overwrite_without_force(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(tmp_path, _base_payload())
    output = tmp_path / "out.json"
    output.write_text("{}", encoding="utf-8")

    rc = run(["--source-file", str(source), "--candidate-file", str(cfile), "--output", str(output)])

    assert rc != 0
    assert output.read_text(encoding="utf-8") == "{}"


def test_overwrite_with_force(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(tmp_path, _base_payload())
    output = tmp_path / "out.json"
    output.write_text("{}", encoding="utf-8")

    rc = run(
        ["--source-file", str(source), "--candidate-file", str(cfile), "--output", str(output), "--force"]
    )

    assert rc == 0
    doc = json.loads(output.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1


# ---- writes only requested output ----


def test_writes_only_requested_output(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(tmp_path, _base_payload())
    output = tmp_path / "out.json"

    rc = run(["--source-file", str(source), "--candidate-file", str(cfile), "--output", str(output)])

    assert rc == 0
    assert output.exists()
    assert not (tmp_path / "game_changes" / "signals.json").exists()
    assert not (tmp_path / "pipeline_state.json").exists()


# ---- no live URL fetch required ----


def test_no_live_url_fetch_required(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path, _base_payload(source_url="https://example.test/totally-fake-url-1234567890")
    )
    output = tmp_path / "out.json"

    rc = run(["--source-file", str(source), "--candidate-file", str(cfile), "--output", str(output)])

    assert rc == 0
    doc = json.loads(output.read_text(encoding="utf-8"))
    assert doc["signals"][0]["source_url"] == "https://example.test/totally-fake-url-1234567890"


# ---- ID generation ----


def test_stable_candidate_ids(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(
            effective_date="2026-06-01",
            candidates=[{"type": "removed_card", "item": "Crystal Blade", "note": "Removed."}],
        ),
    )

    doc = build_candidate_signals(source, cfile)

    assert doc["signals"][0]["id"] == "proposed-2026-06-01-crystal-blade-removed-card"


def test_duplicate_ids_get_counter_suffix(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(
            candidates=[
                {"type": "removed_card", "item": "Same Item", "note": "First."},
                {"type": "removed_card", "item": "Same Item", "note": "Second."},
            ]
        ),
    )

    doc = build_candidate_signals(source, cfile)

    ids = [s["id"] for s in doc["signals"]]
    assert ids[0] == "proposed-2026-06-01-same-item-removed-card"
    assert ids[1] == "proposed-2026-06-01-same-item-removed-card-2"


# ---- source file metadata ----


def test_source_sha256_in_metadata(tmp_path):
    content = "# Patch notes"
    source = _write_source(tmp_path, content)
    cfile = _write_candidates(tmp_path, _base_payload())

    doc = build_candidate_signals(source, cfile)

    expected_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert doc["signals"][0]["metadata"]["source_sha256"] == expected_sha


def test_source_file_path_in_metadata(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(tmp_path, _base_payload())

    doc = build_candidate_signals(source, cfile)

    assert "source_file" in doc["signals"][0]["metadata"]
    assert str(source) in doc["signals"][0]["metadata"]["source_file"]


# ---- patch propagation ----


def test_patch_propagates_to_all_signals(tmp_path):
    source = _write_source(tmp_path)
    cfile = _write_candidates(
        tmp_path,
        _base_payload(
            patch="15.1",
            candidates=[
                {"type": "removed_card", "item": "Item A", "note": "Removed."},
                {"type": "watchlist", "item": "Item B", "note": "Watchlist."},
            ],
        ),
    )

    doc = build_candidate_signals(source, cfile)

    assert all(s["patch"] == "15.1" for s in doc["signals"])
