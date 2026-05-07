from __future__ import annotations

import json

from automated_builds_pipeline.sources.base import FetchOptions
from automated_builds_pipeline.sources.mobalytics import (
    fetch_build_articles,
    parse_build_article_fixture,
    parse_meta_builds_fixture,
    parse_meta_builds_html,
    parse_meta_builds_state,
)
from automated_builds_pipeline.stats import HeroStats, append_window, load_stats, save_stats


def test_mobalytics_meta_fixture_extracts_hero_build_items(sample_dir, load_json):
    data = load_json(sample_dir / "mobalytics" / "meta-builds-preloaded-state-builds.json")

    result = parse_meta_builds_fixture(data, "Karnok", FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "healthy"
    assert result.observation.window_id == "mobalytics_meta_builds:v537"
    rows = {(item.archetype, item.item) for item in result.observation.items}
    assert ("Self-Slow", "Flying Squirrel") in rows
    assert ("Anaconda", "Karst") in rows
    assert all(item.metadata["build_title"].endswith("(Karnok)") for item in result.observation.items)


def test_mobalytics_meta_live_shape_skips_non_document_rows(sample_dir, load_json):
    state = load_json(sample_dir / "mobalytics" / "meta-builds-preloaded-state-builds-2026-05-06.json")

    result = parse_meta_builds_state(state, "Karnok", FetchOptions(observed_at="2026-05-06T23:00:00Z"))

    assert result.status == "healthy"
    assert result.observation.window_id == "mobalytics_meta_builds:v540"
    rows = {(item.archetype, item.item) for item in result.observation.items}
    assert ("Self-Slow", "Flying Squirrel") in rows
    assert ("Anaconda", "Karst") in rows
    assert all(item.metadata["build_title"].endswith("(Karnok)") for item in result.observation.items)


def test_mobalytics_missing_preloaded_state_is_unhealthy():
    result = parse_meta_builds_html("<html></html>", "Karnok", FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "unhealthy"
    assert result.details == ["preloaded_state_missing_or_invalid"]


def test_mobalytics_missing_document_path_is_unhealthy():
    state = {
        "theBazaarState": {
            "apollo": {
                "graphqlV2": {
                    "queries": [
                        {
                            "state": {
                                "data": [
                                    {"game": {"settings": {"banners": {"takeovers": []}}}},
                                    None,
                                ]
                            }
                        }
                    ]
                }
            }
        }
    }

    result = parse_meta_builds_state(state, "Karnok", FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "unhealthy"
    assert result.details == ["document_path_missing"]


def test_mobalytics_missing_version_is_unhealthy():
    state = {
        "theBazaarState": {
            "apollo": {
                "graphqlV2": {
                    "queries": [
                        {
                            "state": {
                                "data": [
                                    {
                                        "game": {
                                            "documents": {
                                                "userGeneratedDocumentBySlug": {
                                                    "data": {"content": []}
                                                }
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }
    }
    html = f"<script>window.__PRELOADED_STATE__={json.dumps(state)};</script>"

    result = parse_meta_builds_html(html, "Karnok", FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "unhealthy"
    assert "document_version_missing" in result.details


def test_mobalytics_article_fixture_extracts_phase_sections(sample_dir, load_json):
    data = load_json(sample_dir / "mobalytics" / "tortuga-vanessa-kripp-build.json")

    result = parse_build_article_fixture(data, FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "healthy"
    assert result.observation.window_id == "mobalytics_build_articles:tortuga-vanessa-kripp"
    piano = next(item for item in result.observation.items if item.item == "Piano")
    assert piano.metadata["section_title"] == "End Game Build Breakdown"


def test_mobalytics_build_articles_without_slugs_is_skipped():
    result = fetch_build_articles([], FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "skipped"
    assert result.details == ["no_article_slugs_configured"]


def test_mobalytics_round_trips_into_stats_sidecar(sample_dir, load_json, tmp_path):
    data = load_json(sample_dir / "mobalytics" / "meta-builds-preloaded-state-builds.json")
    result = parse_meta_builds_fixture(data, "Karnok", FetchOptions(observed_at="2026-05-05T12:00:00Z"))
    stats = HeroStats(hero="Karnok")

    append_window(stats, "mobalytics_meta_builds", result.observation)
    save_stats(stats, tmp_path)
    loaded = load_stats("Karnok", tmp_path)

    assert loaded.last_window_id("mobalytics_meta_builds") == "mobalytics_meta_builds:v537"
    assert loaded.item_history("Flying Squirrel", "mobalytics_meta_builds")
