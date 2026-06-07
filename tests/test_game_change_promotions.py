"""Tests for automated_builds_pipeline.game_change_promotions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automated_builds_pipeline.game_change_promotions import (
    PromotionError,
    promote_candidate_signals,
    run,
)
from automated_builds_pipeline.game_changes import parse_signals

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "game_change_promotions"


# ---- helpers ----


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_candidate_doc(signals: list[dict]) -> dict:
    return {"schema_version": 1, "signals": signals}


def _make_existing_doc(signals: list[dict]) -> dict:
    return {"schema_version": 1, "signals": signals}


def _base_proposed_signal(**overrides) -> dict:
    base: dict = {
        "id": "proposed-2026-06-01-crystal-blade-removed-card",
        "type": "removed_card",
        "item": "Crystal Blade",
        "effective_date": "2026-06-01",
        "source_url": "https://example.test/patch-notes",
        "note": "Crystal Blade was removed.",
        "metadata": {
            "status": "proposed",
            "curator": "manual",
            "source_file": "tests/fixtures/source.md",
            "source_sha256": "a" * 64,
            "source_kind": "patch_notes",
            "source_title": "Patch 15.1 Notes",
            "source_accessed_at": "2026-06-01T10:00:00Z",
            "confidence": "high",
        },
    }
    base.update(overrides)
    return base


def _base_reviewed_signal(**overrides) -> dict:
    base: dict = {
        "id": "reviewed-2026-05-01-iron-fist-major-nerf",
        "type": "major_nerf",
        "item": "Iron Fist",
        "effective_date": "2026-05-01",
        "source_url": "https://example.test/older-notes",
        "note": "Iron Fist was nerfed.",
        "metadata": {
            "status": "reviewed",
            "curator": "hearn1",
            "reviewed_at": "2026-05-02",
            "confidence": "high",
        },
    }
    base.update(overrides)
    return base


def _promote(tmp_path, *, candidate_signals=None, existing_signals=None, selected_ids=None, **kwargs):
    if candidate_signals is None:
        candidate_signals = [_base_proposed_signal()]
    if existing_signals is None:
        existing_signals = []
    if selected_ids is None:
        selected_ids = ["proposed-2026-06-01-crystal-blade-removed-card"]
    candidate_file = _write_json(tmp_path / "candidates.json", _make_candidate_doc(candidate_signals))
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc(existing_signals))
    return promote_candidate_signals(
        candidate_file=candidate_file,
        signals_file=signals_file,
        selected_ids=selected_ids,
        curator=kwargs.pop("curator", "hearn1"),
        reviewed_at=kwargs.pop("reviewed_at", "2026-06-06"),
        **kwargs,
    )


# ---- fixture files are valid ----


def test_fixture_candidate_file_is_valid():
    raw = json.loads((FIXTURE_DIR / "candidate_signals.json").read_text(encoding="utf-8"))
    parse_signals(raw)


def test_fixture_existing_file_is_valid():
    raw = json.loads((FIXTURE_DIR / "existing_signals.json").read_text(encoding="utf-8"))
    parse_signals(raw)


def test_fixture_files_produce_valid_output():
    doc = promote_candidate_signals(
        candidate_file=FIXTURE_DIR / "candidate_signals.json",
        signals_file=FIXTURE_DIR / "existing_signals.json",
        selected_ids=["proposed-2026-06-01-crystal-blade-removed-card"],
        curator="hearn1",
        reviewed_at="2026-06-06",
    )

    parse_signals(doc)
    assert len(doc["signals"]) == 2  # 1 existing + 1 promoted


# ---- selected candidate is promoted ----


def test_selected_candidate_is_promoted(tmp_path):
    doc = _promote(tmp_path)

    ids = [s["id"] for s in doc["signals"]]
    assert "proposed-2026-06-01-crystal-blade-removed-card" in ids


def test_unselected_candidates_not_promoted(tmp_path):
    watchlist = {
        **_base_proposed_signal(),
        "id": "proposed-2026-06-01-shadow-dagger-watchlist",
        "type": "watchlist",
        "item": "Shadow Dagger",
        "metadata": {
            **_base_proposed_signal()["metadata"],
            "confidence": "medium",
            "uncertainty_note": "Not conclusive.",
        },
    }
    doc = _promote(
        tmp_path,
        candidate_signals=[_base_proposed_signal(), watchlist],
        selected_ids=["proposed-2026-06-01-crystal-blade-removed-card"],
    )

    ids = [s["id"] for s in doc["signals"]]
    assert "proposed-2026-06-01-crystal-blade-removed-card" in ids
    assert "proposed-2026-06-01-shadow-dagger-watchlist" not in ids


# ---- metadata promotion ----


def test_promoted_status_is_reviewed(tmp_path):
    doc = _promote(tmp_path)
    assert doc["signals"][0]["metadata"]["status"] == "reviewed"


def test_promoted_curator_is_set(tmp_path):
    doc = _promote(tmp_path, curator="hearn1")
    assert doc["signals"][0]["metadata"]["curator"] == "hearn1"


def test_promoted_reviewed_at_is_set(tmp_path):
    doc = _promote(tmp_path, reviewed_at="2026-06-06")
    assert doc["signals"][0]["metadata"]["reviewed_at"] == "2026-06-06"


def test_provenance_fields_preserved(tmp_path):
    doc = _promote(tmp_path)
    meta = doc["signals"][0]["metadata"]
    assert meta["source_file"] == "tests/fixtures/source.md"
    assert meta["source_sha256"] == "a" * 64
    assert meta["source_kind"] == "patch_notes"
    assert meta["source_title"] == "Patch 15.1 Notes"
    assert meta["source_accessed_at"] == "2026-06-01T10:00:00Z"


# ---- ordering and validation ----


def test_existing_signals_precede_promoted(tmp_path):
    doc = _promote(tmp_path, existing_signals=[_base_reviewed_signal()])

    assert doc["signals"][0]["id"] == "reviewed-2026-05-01-iron-fist-major-nerf"
    assert doc["signals"][1]["id"] == "proposed-2026-06-01-crystal-blade-removed-card"


def test_output_validates_through_parse_signals(tmp_path):
    doc = _promote(tmp_path, existing_signals=[_base_reviewed_signal()])
    parse_signals(doc)


# ---- failure: missing selected ID ----


def test_missing_selected_id_raises(tmp_path):
    with pytest.raises(PromotionError, match="not found"):
        _promote(tmp_path, selected_ids=["proposed-does-not-exist"])


def test_missing_selected_id_does_not_write(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))
    output = tmp_path / "out.json"

    rc = run([
        "--candidate-file", str(candidate_file),
        "--signals-file", str(signals_file),
        "--select", "proposed-does-not-exist",
        "--curator", "hearn1",
        "--reviewed-at", "2026-06-06",
        "--output", str(output),
    ])

    assert rc != 0
    assert not output.exists()


# ---- failure: duplicate signal ID ----


def test_duplicate_signal_id_fails(tmp_path):
    candidate = _base_proposed_signal()
    existing = {
        **candidate,
        "metadata": {**candidate["metadata"], "status": "reviewed", "curator": "hearn1"},
    }
    with pytest.raises(PromotionError, match="already exists"):
        _promote(
            tmp_path,
            candidate_signals=[candidate],
            existing_signals=[existing],
        )


# ---- failure: duplicate dedupe key ----


def test_duplicate_dedupe_key_fails(tmp_path):
    candidate = _base_proposed_signal()
    # Same (type, hero, item, effective_date) but different ID
    existing = {
        **candidate,
        "id": "different-id-same-key",
        "metadata": {**candidate["metadata"], "status": "reviewed", "curator": "hearn1"},
    }
    with pytest.raises(PromotionError, match="duplicate"):
        _promote(
            tmp_path,
            candidate_signals=[candidate],
            existing_signals=[existing],
        )


# ---- failure: non-proposed status ----


def test_non_proposed_status_fails(tmp_path):
    signal = _base_proposed_signal()
    signal["metadata"] = {**signal["metadata"], "status": "reviewed"}
    with pytest.raises(PromotionError, match="status"):
        _promote(tmp_path, candidate_signals=[signal])


# ---- failure: unresolved missing_metadata ----


def test_missing_metadata_blocks_by_default(tmp_path):
    signal = _base_proposed_signal()
    signal["metadata"] = {**signal["metadata"], "missing_metadata": ["source_kind"]}
    with pytest.raises(PromotionError, match="missing_metadata"):
        _promote(tmp_path, candidate_signals=[signal])


def test_allow_missing_metadata_promotes_despite_missing(tmp_path):
    signal = _base_proposed_signal()
    signal["metadata"] = {**signal["metadata"], "missing_metadata": ["source_kind"]}
    doc = _promote(tmp_path, candidate_signals=[signal], allow_missing_metadata=True)

    meta = doc["signals"][0]["metadata"]
    assert meta["status"] == "reviewed"
    assert "missing_metadata" in meta  # preserved since fields are still absent


# ---- failure: watchlist without uncertainty_note ----


def test_watchlist_without_uncertainty_note_fails(tmp_path):
    signal = {
        **_base_proposed_signal(),
        "id": "proposed-2026-06-01-shadow-dagger-watchlist",
        "type": "watchlist",
        "item": "Shadow Dagger",
        "metadata": {**_base_proposed_signal()["metadata"], "confidence": "medium"},
    }
    with pytest.raises(PromotionError, match="uncertainty_note"):
        _promote(
            tmp_path,
            candidate_signals=[signal],
            selected_ids=["proposed-2026-06-01-shadow-dagger-watchlist"],
        )


# ---- failure: low confidence without uncertainty_note ----


def test_low_confidence_without_uncertainty_note_fails(tmp_path):
    signal = _base_proposed_signal()
    signal["metadata"] = {**signal["metadata"], "confidence": "low"}
    with pytest.raises(PromotionError, match="uncertainty_note"):
        _promote(tmp_path, candidate_signals=[signal])


# ---- failure: missing or invalid confidence ----


def test_missing_confidence_fails(tmp_path):
    signal = _base_proposed_signal()
    meta = {**signal["metadata"]}
    del meta["confidence"]
    signal["metadata"] = meta
    with pytest.raises(PromotionError, match="confidence"):
        _promote(tmp_path, candidate_signals=[signal])


def test_invalid_confidence_fails(tmp_path):
    signal = _base_proposed_signal()
    signal["metadata"] = {**signal["metadata"], "confidence": "very_high"}
    with pytest.raises(PromotionError, match="confidence"):
        _promote(tmp_path, candidate_signals=[signal])


# ---- CLI: missing --select ----


def test_no_select_returns_nonzero(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))
    output = tmp_path / "out.json"

    rc = run([
        "--candidate-file", str(candidate_file),
        "--signals-file", str(signals_file),
        "--curator", "hearn1",
        "--reviewed-at", "2026-06-06",
        "--output", str(output),
    ])

    assert rc != 0
    assert not output.exists()


# ---- CLI: missing required args (argparse exits) ----


def test_missing_curator_exits(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))

    with pytest.raises(SystemExit) as exc_info:
        run([
            "--candidate-file", str(candidate_file),
            "--signals-file", str(signals_file),
            "--select", "proposed-2026-06-01-crystal-blade-removed-card",
            "--reviewed-at", "2026-06-06",
            "--output", str(tmp_path / "out.json"),
        ])

    assert exc_info.value.code != 0


def test_missing_reviewed_at_exits(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))

    with pytest.raises(SystemExit) as exc_info:
        run([
            "--candidate-file", str(candidate_file),
            "--signals-file", str(signals_file),
            "--select", "proposed-2026-06-01-crystal-blade-removed-card",
            "--curator", "hearn1",
            "--output", str(tmp_path / "out.json"),
        ])

    assert exc_info.value.code != 0


# ---- CLI: overwrite guard ----


def test_existing_output_not_overwritten_without_force(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))
    output = tmp_path / "out.json"
    output.write_text("{}", encoding="utf-8")

    rc = run([
        "--candidate-file", str(candidate_file),
        "--signals-file", str(signals_file),
        "--select", "proposed-2026-06-01-crystal-blade-removed-card",
        "--curator", "hearn1",
        "--reviewed-at", "2026-06-06",
        "--output", str(output),
    ])

    assert rc != 0
    assert output.read_text(encoding="utf-8") == "{}"


def test_overwrite_with_force(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))
    output = tmp_path / "out.json"
    output.write_text("{}", encoding="utf-8")

    rc = run([
        "--candidate-file", str(candidate_file),
        "--signals-file", str(signals_file),
        "--select", "proposed-2026-06-01-crystal-blade-removed-card",
        "--curator", "hearn1",
        "--reviewed-at", "2026-06-06",
        "--output", str(output),
        "--force",
    ])

    assert rc == 0
    doc = json.loads(output.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert len(doc["signals"]) == 1


# ---- CLI: writes only requested output ----


def test_writes_only_requested_output(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))
    output = tmp_path / "out.json"

    rc = run([
        "--candidate-file", str(candidate_file),
        "--signals-file", str(signals_file),
        "--select", "proposed-2026-06-01-crystal-blade-removed-card",
        "--curator", "hearn1",
        "--reviewed-at", "2026-06-06",
        "--output", str(output),
    ])

    assert rc == 0
    assert output.exists()
    assert not (tmp_path / "pipeline_state.json").exists()


def test_does_not_modify_signals_file(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_doc = _make_existing_doc([_base_reviewed_signal()])
    signals_file = _write_json(tmp_path / "signals.json", signals_doc)
    original_content = signals_file.read_text(encoding="utf-8")
    output = tmp_path / "out.json"

    run([
        "--candidate-file", str(candidate_file),
        "--signals-file", str(signals_file),
        "--select", "proposed-2026-06-01-crystal-blade-removed-card",
        "--curator", "hearn1",
        "--reviewed-at", "2026-06-06",
        "--output", str(output),
    ])

    assert signals_file.read_text(encoding="utf-8") == original_content


def test_no_tmp_file_after_successful_write(tmp_path):
    candidate_file = _write_json(
        tmp_path / "candidates.json", _make_candidate_doc([_base_proposed_signal()])
    )
    signals_file = _write_json(tmp_path / "signals.json", _make_existing_doc([]))
    output = tmp_path / "out.json"

    rc = run([
        "--candidate-file", str(candidate_file),
        "--signals-file", str(signals_file),
        "--select", "proposed-2026-06-01-crystal-blade-removed-card",
        "--curator", "hearn1",
        "--reviewed-at", "2026-06-06",
        "--output", str(output),
    ])

    assert rc == 0
    assert not output.with_suffix(".tmp").exists()
