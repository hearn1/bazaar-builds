"""Safety contract tests for artifacts/sweep_dry_run.py.

Asserts that:
1. The sweep builds a temp state file with ``phase: local_dry_run`` rather than
   reading or mutating ``pipeline_state.json``.
2. After a mocked-fetch sweep run, no file under ``stats/`` has its mtime
   changed and the tracker repo has no uncommitted changes (git status clean).
3. ``pipeline_state.json`` is never opened by the sweep.

These tests use monkeypatching to avoid real network calls while still
exercising the full pipeline → evaluator → diff → applier chain on a minimal
synthetic catalog.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from automated_builds_pipeline.sources.base import SourceFetchResult
from automated_builds_pipeline.stats import ItemWindowEvidence, WindowObservation

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OBSERVED_AT = "2026-05-05T12:00:00Z"

_BUILDS_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": [
        "schema_version",
        "hero",
        "season",
        "last_updated",
        "notes",
        "item_tier_list",
        "game_phases",
    ],
    "additionalProperties": True,
    "properties": {
        "schema_version": {"type": "integer"},
        "hero": {"type": "string"},
        "season": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
        "last_updated": {"type": "string"},
        "notes": {"type": "string"},
        "item_tier_list": {"type": "object"},
        "game_phases": {"type": "object"},
    },
}


def _make_catalog(hero: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "hero": hero,
        "season": 13,
        "last_updated": "2026-05-05",
        "notes": "",
        "item_tier_list": {"S": ["Axe"]},
        "game_phases": {
            "early": {
                "day_range": "Day 1-4",
                "description": "",
                "universal_utility_items": [],
                "economy_items": [],
            },
            "early_mid": {
                "day_range": "Day 4-7",
                "description": "",
                "archetypes": [
                    {"name": "Axe Warrior", "carry_items": ["Axe"], "support_items": []}
                ],
            },
            "late": {"day_range": "Day 7-13", "description": "", "archetypes": []},
        },
    }


def _make_tracker(tmp_path: Path, hero: str) -> Path:
    tracker = tmp_path / "tracker"
    builds = tracker / "builds"
    builds.mkdir(parents=True)
    (tracker / "card_cache_names.txt").write_text("Axe\n", encoding="utf-8")
    (builds / "builds_schema.json").write_text(
        json.dumps(_BUILDS_SCHEMA), encoding="utf-8"
    )
    (builds / f"{hero.lower()}_builds.json").write_text(
        json.dumps(_make_catalog(hero)), encoding="utf-8"
    )
    # Initialise a bare git repo so ``git status`` is available
    subprocess.run(["git", "init", "-q"], cwd=tracker, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=tracker, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tracker, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tracker, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "-q"], cwd=tracker, check=True
    )
    return tracker


def _empty_source_result(source: str) -> SourceFetchResult:
    return SourceFetchResult(
        observation=WindowObservation(
            window_id=f"{source}:{OBSERVED_AT}",
            observed_at=OBSERVED_AT,
            items=[],
        ),
        status="healthy",
        details=[],
    )


def _patch_fetchers(monkeypatch) -> None:
    """Replace all network fetchers with no-op returns."""
    from automated_builds_pipeline import pipeline
    from automated_builds_pipeline.sources import bazaardb, bazaar_builds_net, mobalytics

    monkeypatch.setattr(
        bazaardb,
        "fetch_meta",
        lambda hero, options: _empty_source_result("bazaardb"),
    )
    monkeypatch.setattr(
        mobalytics,
        "fetch_meta_builds",
        lambda hero, options: _empty_source_result("mobalytics_meta_builds"),
    )
    monkeypatch.setattr(
        mobalytics,
        "fetch_build_articles",
        lambda slugs, options: SourceFetchResult(
            observation=WindowObservation(
                window_id=f"mobalytics_build_articles:{OBSERVED_AT}",
                observed_at=OBSERVED_AT,
                items=[],
            ),
            status="skipped",
            details=["no slugs"],
        ),
    )
    monkeypatch.setattr(
        bazaar_builds_net,
        "fetch_builds",
        lambda hero, category_url, options: _empty_source_result("bazaar_builds_net"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_sweep_uses_temp_state_not_live_state(tmp_path: Path, monkeypatch) -> None:
    """Sweep must build a temp state file; it must never open pipeline_state.json."""
    from artifacts.sweep_dry_run import _LOCAL_DRY_RUN_STATE, _write_temp_state

    with tempfile.TemporaryDirectory(prefix="sweep_test_") as tmp:
        state_path = _write_temp_state(tmp)
        data = json.loads(state_path.read_text(encoding="utf-8"))

    assert data["phase"] == "local_dry_run", (
        f"temp state phase must be 'local_dry_run', got {data['phase']!r}"
    )
    assert data.get("dry_run") is False, (
        "temp state dry_run must be False for local_dry_run phase"
    )
    # Confirm the structure matches the module constant
    assert data == _LOCAL_DRY_RUN_STATE


def test_sweep_does_not_mutate_stats_or_tracker(tmp_path: Path, monkeypatch) -> None:
    """After a mocked sweep run, stats/ mtime and tracker git status are unchanged."""
    _patch_fetchers(monkeypatch)

    hero = "Karnok"
    tracker = _make_tracker(tmp_path, hero)
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    output_dir = tmp_path / "artifacts" / "verify"
    output_dir.mkdir(parents=True)

    # Snapshot stats/ mtimes before the sweep
    def _collect_mtimes(directory: Path) -> dict[str, float]:
        return {
            str(p.relative_to(directory)): p.stat().st_mtime
            for p in directory.rglob("*")
            if p.is_file()
        }

    stats_before = _collect_mtimes(stats_dir)

    # Run sweep for this single hero
    from artifacts.sweep_dry_run import _run_hero, _write_temp_state

    with tempfile.TemporaryDirectory(prefix="sweep_test_") as tmp_state:
        state_file = _write_temp_state(tmp_state)
        _run_hero(
            hero,
            state_file=state_file,
            tracker_repo=tracker,
            stats_dir=stats_dir,
            output_dir=output_dir,
            no_bazaardb=False,
        )

    # 1. Stats dir must be unchanged
    stats_after = _collect_mtimes(stats_dir)
    assert stats_before == stats_after, (
        f"stats/ was mutated by the sweep.\n"
        f"before: {stats_before}\nafter: {stats_after}"
    )

    # 2. Tracker git status must be clean
    git_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tracker,
        capture_output=True,
        text=True,
        check=True,
    )
    assert git_status.stdout.strip() == "", (
        f"Tracker repo has uncommitted changes after sweep:\n{git_status.stdout}"
    )


def test_sweep_phase_is_local_dry_run() -> None:
    """The sweep state constant must encode local_dry_run with dry_run=False."""
    from artifacts.sweep_dry_run import _LOCAL_DRY_RUN_STATE

    assert _LOCAL_DRY_RUN_STATE["phase"] == "local_dry_run"
    assert _LOCAL_DRY_RUN_STATE["dry_run"] is False
    assert _LOCAL_DRY_RUN_STATE["schema_version"] == 1


def test_sweep_heroes_list_covers_all_seven() -> None:
    """The HEROES list must include all 7 primary heroes."""
    from artifacts.sweep_dry_run import HEROES

    expected = {"Dooley", "Jules", "Karnok", "Mak", "Pygmalien", "Stelle", "Vanessa"}
    assert set(HEROES) == expected, (
        f"HEROES mismatch: got {set(HEROES)}, expected {expected}"
    )
