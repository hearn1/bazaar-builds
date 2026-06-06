"""
Cross-repo catalog contract test.

Proves that a bazaar-builds-generated diff can be:
1. Applied by the guarded applier to a temp copy of a bazaar_coach catalog.
2. Validated against the coach-owned builds_schema.json.
3. Written to a temp catalog file.
4. Loaded and validated by bazaar_coach scorer.

No source fetchers, Playwright, PR creation, stats sidecar writes, or
pipeline_state.json reads/writes occur in this test.  All catalog mutations
happen under tmp_path.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from automated_builds_pipeline.applier import (
    apply_proposed_changes,
    ensure_supported_schema_version,
    serialize_catalog,
    validate_catalog,
)

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "contract"
_TARGET_HERO = "Karnok"
_TARGET_PHASE = "early_mid"
_TARGET_ARCHETYPE = "Axe"
_TARGET_ITEM = "Honey Badger"
_TARGET_BUCKET = "support_items"


def _coach_checkout() -> Path:
    env = os.environ.get("BAZAAR_COACH_CHECKOUT")
    if env:
        path = Path(env)
        if path.exists():
            return path
        pytest.skip(
            f"BAZAAR_COACH_CHECKOUT={env!r} does not exist"
        )

    base = Path(__file__).resolve().parents[2]
    for name in ("bazaar_coach", "bazaar_tracker"):
        path = base / name
        if path.exists():
            return path

    pytest.skip(
        "bazaar_coach checkout not available; "
        "set BAZAAR_COACH_CHECKOUT or place the checkout at ../bazaar_coach"
    )


def test_generated_catalog_is_consumable_by_coach(tmp_path, monkeypatch):
    # ── Arrange ───────────────────────────────────────────────────────────────
    coach_root = _coach_checkout()
    builds_src = coach_root / "builds"

    temp_builds = tmp_path / "coach_builds"
    temp_builds.mkdir()
    shutil.copy2(builds_src / "builds_schema.json", temp_builds / "builds_schema.json")
    shutil.copy2(builds_src / "karnok_builds.json", temp_builds / "karnok_builds.json")

    schema = json.loads((temp_builds / "builds_schema.json").read_text(encoding="utf-8"))
    base_catalog = json.loads((temp_builds / "karnok_builds.json").read_text(encoding="utf-8"))
    diff_json = json.loads(
        (_FIXTURE_DIR / "karnok_archetype_update_diff.json").read_text(encoding="utf-8")
    )

    # ── Act: applier boundary ─────────────────────────────────────────────────
    ensure_supported_schema_version(base_catalog)
    validate_catalog(base_catalog, schema)

    result = apply_proposed_changes(base_catalog, diff_json)

    assert result.changed is True, (
        f"Expected at least one applied change; skipped: {result.skipped}"
    )

    archetypes = result.catalog["game_phases"][_TARGET_PHASE]["archetypes"]
    target_arch = next((a for a in archetypes if a["name"] == _TARGET_ARCHETYPE), None)
    assert target_arch is not None, (
        f"Archetype {_TARGET_ARCHETYPE!r} not found after apply"
    )
    assert _TARGET_ITEM in target_arch[_TARGET_BUCKET], (
        f"{_TARGET_ITEM!r} not found in "
        f"{_TARGET_ARCHETYPE!r}/{_TARGET_PHASE}/{_TARGET_BUCKET}"
    )

    validate_catalog(result.catalog, schema)

    temp_catalog_path = temp_builds / "karnok_builds.json"
    temp_catalog_path.write_text(serialize_catalog(result.catalog), encoding="utf-8")

    # ── Act: coach scorer boundary ────────────────────────────────────────────
    # Ensure scorer and its local dependencies are loaded from the coach checkout.
    for mod_name in ("scorer", "app_paths", "db", "card_cache", "board_state", "content_manifest"):
        sys.modules.pop(mod_name, None)

    monkeypatch.syspath_prepend(str(coach_root))
    scorer = importlib.import_module("scorer")

    monkeypatch.setattr(scorer, "BUILD_GUIDE_DIR", temp_builds)
    scorer._load_builds_schema.cache_clear()
    scorer._load_builds_cached.cache_clear()
    monkeypatch.setattr(
        scorer, "_writable_builds_path", lambda hero=None: tmp_path / "unused_writable.json"
    )
    monkeypatch.setattr(
        scorer, "_user_builds_path", lambda hero=None: tmp_path / "unused_user.json"
    )

    # ── Assert: schema and scorer accept the generated catalog ─────────────────
    ok, err = scorer.validate_builds_catalog(result.catalog)
    assert ok, f"scorer.validate_builds_catalog rejected the generated catalog: {err}"

    loaded = scorer.load_builds(_TARGET_HERO)

    assert loaded["hero"] == _TARGET_HERO
    assert scorer.has_build_catalog(loaded), (
        "scorer.load_builds returned empty/stub catalog — "
        "validation may have rejected it or the file was not found"
    )

    loaded_archetypes = loaded["game_phases"][_TARGET_PHASE]["archetypes"]
    loaded_arch = next(
        (a for a in loaded_archetypes if a["name"] == _TARGET_ARCHETYPE), None
    )
    assert loaded_arch is not None, (
        f"Archetype {_TARGET_ARCHETYPE!r} not found in loaded catalog"
    )
    assert _TARGET_ITEM in loaded_arch[_TARGET_BUCKET], (
        f"{_TARGET_ITEM!r} not found in loaded catalog "
        f"{_TARGET_ARCHETYPE!r}/{_TARGET_PHASE}/{_TARGET_BUCKET} — "
        "scorer may have loaded the bundled catalog instead of the temp file"
    )
