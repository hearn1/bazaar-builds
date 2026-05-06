from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from automated_builds_pipeline.state import (
    CuratorState,
    FreezeWindow,
    StateError,
    load_state,
    save_state,
)


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def test_state_round_trip_preserves_notes(tmp_path):
    path = tmp_path / "state.json"
    state = CuratorState(
        phase="local_dry_run",
        dry_run=True,
        patch_label="13.4",
        expected_bazaardb_patch_label="Apr 29",
        global_freeze=FreezeWindow("2026-05-06"),
        hero_freezes={"karnok": FreezeWindow("2026-05-07", "hero-specific")},
        notes="watch Pygmalien changes",
    )

    save_state(state, path)
    loaded = load_state(path)

    assert loaded.global_freeze == state.global_freeze
    assert loaded.hero_freezes["karnok"] == state.hero_freezes["karnok"]
    assert loaded.notes == "watch Pygmalien changes"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "phase": "local_dry_run",
        "dry_run": True,
        "patch_label": "13.4",
        "expected_bazaardb_patch_label": "Apr 29",
        "freeze_removals_until": "2026-05-06",
        "hero_freezes": {
            "karnok": {
                "freeze_removals_until": "2026-05-07",
                "notes": "hero-specific",
            }
        },
        "notes": "watch Pygmalien changes",
    }


def test_state_missing_required_field_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 1, "notes": ""}), encoding="utf-8")

    with pytest.raises(StateError, match="phase must be a non-empty string"):
        load_state(path)


def test_state_schema_version_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema_version": 2, "hero_freezes": {}, "notes": []}),
        encoding="utf-8",
    )

    with pytest.raises(StateError, match="newer than supported"):
        load_state(path)


@pytest.mark.parametrize(
    ("state", "hero", "expected_active", "expected_scope"),
    [
        (CuratorState(global_freeze=FreezeWindow("2026-05-06"), hero_freezes={}), "Karnok", True, "global"),
        (CuratorState(global_freeze=FreezeWindow("2026-05-04"), hero_freezes={}), "Karnok", False, None),
        (CuratorState(hero_freezes={"karnok": FreezeWindow("2026-05-06")}), "Karnok", True, "hero"),
        (CuratorState(hero_freezes={"karnok": FreezeWindow("2026-05-04")}), "Karnok", False, None),
    ],
)
def test_freeze_active_for_global_and_per_hero_future_and_past(state, hero, expected_active, expected_scope):
    status = state.freeze_status(hero, NOW)

    assert status.active is expected_active
    assert status.scope == expected_scope
