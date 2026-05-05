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
        global_freeze=FreezeWindow("2026-05-06T12:00:00Z", "curator review"),
        hero_freezes={"karnok": FreezeWindow("2026-05-07T12:00:00Z", "hero-specific")},
        notes=["watch Pygmalien changes"],
    )

    save_state(state, path)
    loaded = load_state(path)

    assert loaded.global_freeze == state.global_freeze
    assert loaded.hero_freezes["karnok"] == state.hero_freezes["karnok"]
    assert loaded.notes == ["watch Pygmalien changes"]


def test_state_missing_required_field_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 1, "notes": []}), encoding="utf-8")

    with pytest.raises(StateError, match="hero_freezes must be an object"):
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
        (CuratorState(global_freeze=FreezeWindow("2026-05-06T12:00:00Z"), hero_freezes={}), "Karnok", True, "global"),
        (CuratorState(global_freeze=FreezeWindow("2026-05-04T12:00:00Z"), hero_freezes={}), "Karnok", False, None),
        (CuratorState(hero_freezes={"karnok": FreezeWindow("2026-05-06T12:00:00Z")}), "Karnok", True, "hero"),
        (CuratorState(hero_freezes={"karnok": FreezeWindow("2026-05-04T12:00:00Z")}), "Karnok", False, None),
    ],
)
def test_freeze_active_for_global_and_per_hero_future_and_past(state, hero, expected_active, expected_scope):
    status = state.freeze_status(hero, NOW)

    assert status.active is expected_active
    assert status.scope == expected_scope
