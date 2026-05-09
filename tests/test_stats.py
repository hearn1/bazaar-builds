import json

import pytest

from automated_builds_pipeline.stats import (
    HeroStats,
    ItemWindowEvidence,
    StatsError,
    WindowObservation,
    append_window,
    load_stats,
    save_stats,
    stats_path,
)


def test_round_trip_write_and_read(tmp_path):
    stats = HeroStats(hero="Karnok")
    append_window(
        stats,
        "bazaar_builds_net",
        WindowObservation(
            window_id="bazaar_builds_net:2026-W18",
            observed_at="2026-05-05T12:00:00Z",
            artifact_ref="artifacts/karnok.json",
            items=[
                ItemWindowEvidence(
                    item="Hunting Knife",
                    phase="late",
                    archetype="Axe",
                    appearances=5,
                    sample_count=7,
                    frequency=0.714,
                    archetypes_seen=["Axe"],
                    evidence_refs=["artifacts/karnok.json"],
                )
            ],
        ),
    )

    save_stats(stats, tmp_path)
    loaded = load_stats("Karnok", tmp_path)

    assert loaded.hero == "Karnok"
    assert loaded.last_window_id("bazaar_builds_net") == "bazaar_builds_net:2026-W18"
    history = loaded.item_history("Hunting Knife", "bazaar_builds_net")
    assert len(history) == 1
    assert history[0].appearances == 5
    assert history[0].sample_count == 7
    assert history[0].archetype == "Axe"


def test_stats_sidecar_serializes_source_observations_without_semantic_conclusions(tmp_path):
    stats = HeroStats(hero="Karnok")
    append_window(
        stats,
        "bazaardb",
        WindowObservation(
            window_id="bazaardb:2026-W18",
            observed_at="2026-05-05T12:00:00Z",
            items=[ItemWindowEvidence(item="Hunting Knife", rank=1, evidence_refs=["artifacts/karnok.json"])],
        ),
    )

    save_stats(stats, tmp_path)
    payload = json.loads(stats_path("Karnok", tmp_path).read_text(encoding="utf-8"))
    serialized = json.dumps(payload)

    assert "llm_classification" not in serialized
    assert "semantic_classification" not in serialized
    assert "classification_mode" not in serialized
    assert "classification_pending" not in serialized


def test_first_run_missing_file_returns_empty_stats(tmp_path):
    stats = load_stats("Karnok", tmp_path)

    assert stats.hero == "Karnok"
    assert stats.items == {}
    assert stats.source_window_count("bazaardb") == 0


def test_retention_drops_oldest_windows(tmp_path):
    stats = HeroStats(hero="Karnok")
    stats.retention_windows["bazaar_builds_net"] = 2

    for week in ("2026-W16", "2026-W17", "2026-W18"):
        items = [ItemWindowEvidence(item="Hunting Knife", appearances=1)]
        if week == "2026-W16":
            items.append(ItemWindowEvidence(item="Old News", appearances=1))
        append_window(
            stats,
            "bazaar_builds_net",
            WindowObservation(
                window_id=f"bazaar_builds_net:{week}",
                observed_at="2026-05-05T12:00:00Z",
                items=items,
            ),
        )

    history = stats.item_history("Hunting Knife", "bazaar_builds_net")
    assert [row.window_id for row in history] == [
        "bazaar_builds_net:2026-W17",
        "bazaar_builds_net:2026-W18",
    ]
    assert stats.source_window_count("bazaar_builds_net") == 2
    assert stats.item_history("Old News", "bazaar_builds_net") == []


def test_atomic_failure_before_rename_leaves_existing_file_untouched(tmp_path):
    stats = HeroStats(hero="Karnok")
    append_window(
        stats,
        "bazaardb",
        WindowObservation(
            window_id="bazaardb:Apr 29",
            observed_at="2026-05-05T12:00:00Z",
            items=[ItemWindowEvidence(item="Hunting Knife", rank=4)],
        ),
    )
    save_stats(stats, tmp_path)
    original = stats_path("Karnok", tmp_path).read_text(encoding="utf-8")

    append_window(
        stats,
        "bazaardb",
        WindowObservation(
            window_id="bazaardb:May 06",
            observed_at="2026-05-12T12:00:00Z",
            items=[ItemWindowEvidence(item="Hunting Knife", rank=3)],
        ),
    )

    def fail_before_replace(tmp_path_arg, target_path_arg):
        assert tmp_path_arg.exists()
        assert target_path_arg.exists()
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        save_stats(stats, tmp_path, _before_replace=fail_before_replace)

    assert stats_path("Karnok", tmp_path).read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_per_source_append_does_not_mutate_other_source_history():
    stats = HeroStats(hero="Karnok")
    append_window(
        stats,
        "mobalytics_meta_builds",
        WindowObservation(
            window_id="mobalytics_meta_builds:v537",
            observed_at="2026-05-05T12:00:00Z",
            items=[ItemWindowEvidence(item="Hunting Knife", present=True)],
        ),
    )
    before = [row.to_dict() for row in stats.item_history("Hunting Knife", "mobalytics_meta_builds")]

    append_window(
        stats,
        "bazaardb",
        WindowObservation(
            window_id="bazaardb:Apr 29",
            observed_at="2026-05-05T12:00:00Z",
            items=[ItemWindowEvidence(item="Hunting Knife", present=False)],
        ),
    )

    after = [row.to_dict() for row in stats.item_history("Hunting Knife", "mobalytics_meta_builds")]
    assert after == before
    assert stats.item_history("Hunting Knife", "bazaardb")[0].present is False


def test_refuses_to_load_higher_schema_version(tmp_path):
    path = stats_path("Karnok", tmp_path)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "hero": "Karnok",
                "generated_at": "2026-05-05T12:00:00Z",
                "retention_windows": {},
                "source_windows": {},
                "items": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StatsError, match="newer than supported"):
        load_stats("Karnok", tmp_path)
