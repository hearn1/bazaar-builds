import json
from pathlib import Path

import bazaar_build_enricher as enricher
from automated_builds_pipeline import smoke
from automated_builds_pipeline.sources import bazaar_builds_net
from automated_builds_pipeline.sources.base import FetchOptions, SourceFetchResult
from automated_builds_pipeline.stats import ItemWindowEvidence, WindowObservation


OBSERVED_AT = "2026-05-05T12:00:00Z"


def source_result(source: str, status: str = "healthy", details: list[str] | None = None) -> SourceFetchResult:
    return SourceFetchResult(
        observation=WindowObservation(
            window_id=f"{source}:window",
            observed_at=OBSERVED_AT,
            items=[ItemWindowEvidence("Pufferfish")] if status == "healthy" else [],
        ),
        status=status,
        details=details or [],
    )


def test_smoke_summary_fails_for_required_unhealthy_source(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / "card_cache_names.txt").write_text("Pufferfish\n", encoding="utf-8")
    output = tmp_path / "source-health.json"
    fetchers = {
        "bazaardb": lambda options: source_result("bazaardb", "unhealthy", ["patch_label_missing"]),
        "mobalytics_meta_builds": lambda options: source_result("mobalytics_meta_builds"),
        "mobalytics_build_articles": lambda options: source_result("mobalytics_build_articles", "skipped", ["no_article_slugs_configured"]),
        "bazaar_builds_net": lambda options: source_result("bazaar_builds_net"),
    }

    summary = smoke.run_smoke(output_path=output, tracker_repo=tracker, fetchers=fetchers)

    assert smoke.should_fail(summary) is True
    assert summary["required_sources_unhealthy"] == ["bazaardb"]
    assert json.loads(output.read_text(encoding="utf-8"))["sources"][0]["details"] == ["patch_label_missing"]


def test_smoke_treats_unconfigured_articles_skipped_as_green(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    (tracker / "card_cache_names.txt").write_text("Pufferfish\n", encoding="utf-8")
    fetchers = {
        "bazaardb": lambda options: source_result("bazaardb"),
        "mobalytics_meta_builds": lambda options: source_result("mobalytics_meta_builds"),
        "mobalytics_build_articles": lambda options: source_result("mobalytics_build_articles", "skipped", ["no_article_slugs_configured"]),
        "bazaar_builds_net": lambda options: source_result("bazaar_builds_net"),
    }

    summary = smoke.run_smoke(output_path=tmp_path / "source-health.json", tracker_repo=tracker, fetchers=fetchers)

    assert smoke.should_fail(summary) is False
    article_row = next(row for row in summary["sources"] if row["source"] == "mobalytics_build_articles")
    assert article_row["status"] == "skipped"
    assert article_row["required"] is False


# --- known-items / builds-subdir tests (Fixes #108) ---

_DB_NAMES_FILE = (
    "[DB] Initialized at C:\\bazaar_tracker\\bazaar_runs.db\n"
    '["Pufferfish", "Anaconda"]\n'
)

_SAMPLE_CATALOG = {
    "schema_version": 1,
    "hero": "Karnok",
    "season": 1,
    "last_updated": "2026-01-01",
    "notes": "",
    "item_tier_list": {"S": ["Fogshroom"]},
    "game_phases": {"early": {"universal_utility_items": [], "economy_items": []}},
}


def test_load_known_items_via_summary_healthy_when_catalog_in_builds_subdir(tmp_path: Path) -> None:
    """result_from_summary reports healthy when summary has known_item_count > 0,
    which requires load_known_items to discover catalogs under builds/."""
    builds = tmp_path / "builds"
    builds.mkdir()
    (builds / "karnok_builds.json").write_text(json.dumps(_SAMPLE_CATALOG), encoding="utf-8")
    names_file = tmp_path / "card_cache_names.txt"
    names_file.write_text("", encoding="utf-8")

    # Simulate the summary that enricher.run produces when known_items is non-empty.
    known_items = enricher.load_known_items(tmp_path, names_file)
    summary = enricher.build_summary(
        records=[],
        category_url="https://bazaar-builds.net/category/builds/karnok-builds/",
        hero="Karnok",
        since=None,
        days=30,
        known_items=known_items,
        warnings=[],
    )

    assert summary["known_item_count"] > 0
    details = bazaar_builds_net._health_details(summary)
    assert "known_items_empty" not in details


def test_load_known_items_names_file_db_preamble_contributes_items(tmp_path: Path) -> None:
    """Names file with [DB] preamble is a real fallback: items parse correctly."""
    names_file = tmp_path / "card_cache_names.txt"
    names_file.write_text(_DB_NAMES_FILE, encoding="utf-8")

    items = enricher.load_known_items(tmp_path, names_file)

    assert "Pufferfish" in items
    assert "Anaconda" in items
