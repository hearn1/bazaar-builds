from __future__ import annotations

import json
from unittest.mock import patch

from automated_builds_pipeline.sources.base import FetchOptions
from automated_builds_pipeline.sources.mobalytics import (
    fetch_build_articles,
    parse_build_article_fixture,
    parse_build_article_state,
    parse_meta_builds_fixture,
    parse_meta_builds_html,
    parse_meta_builds_state,
    slugs_from_meta_builds_state,
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


# --- #132: slug auto-discovery from meta-builds state ---


def test_slugs_from_meta_builds_state_extracts_hero_article_slugs(sample_dir, load_json):
    """Spike conclusion: meta-builds PRELOADED_STATE embeds article links in the
    descriptionTheBazaarBoardCreator rich-text; we extract slug path segments."""
    state = load_json(sample_dir / "mobalytics" / "meta-builds-preloaded-state-builds-2026-05-06.json")

    karnok_slugs = slugs_from_meta_builds_state(state, "Karnok")

    assert "self-slow-karnok-kripp" in karnok_slugs
    assert "karst-enrage-karnok-kripp" in karnok_slugs
    # Only karnok slugs — no cross-hero contamination.
    assert all("karnok" in s for s in karnok_slugs)


def test_slugs_from_meta_builds_state_filters_by_hero(sample_dir, load_json):
    state = load_json(sample_dir / "mobalytics" / "meta-builds-preloaded-state-builds-2026-05-06.json")

    vanessa_slugs = slugs_from_meta_builds_state(state, "Vanessa")

    assert all("vanessa" in s for s in vanessa_slugs)
    # No karnok slugs in vanessa result.
    assert not any("karnok" in s for s in vanessa_slugs)


def test_slugs_from_meta_builds_state_deduplicates():
    """Same URL appearing twice in rich-text yields one slug."""
    state = {
        "theBazaarState": {
            "apollo": {
                "graphqlV2": {
                    "queries": [
                        {
                            "state": {
                                "data": [
                                    {
                                        "foo": [
                                            {"type": "link", "url": "/the-bazaar/builds/frozen-karnok-kripp"},
                                            {"type": "link", "url": "/the-bazaar/builds/frozen-karnok-kripp"},
                                            {"type": "link", "url": "/the-bazaar/builds/slash-karnok-kripp"},
                                        ]
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }
    }

    slugs = slugs_from_meta_builds_state(state, "Karnok")

    assert slugs == ["frozen-karnok-kripp", "slash-karnok-kripp"]


def test_slugs_from_meta_builds_state_returns_empty_for_empty_state():
    assert slugs_from_meta_builds_state({}, "Karnok") == []


def test_meta_builds_result_carries_meta_state(sample_dir, load_json):
    """parse_meta_builds_state attaches meta_state so pipeline can auto-discover slugs."""
    state = load_json(sample_dir / "mobalytics" / "meta-builds-preloaded-state-builds-2026-05-06.json")

    result = parse_meta_builds_state(state, "Karnok", FetchOptions(observed_at="2026-05-06T23:00:00Z"))

    assert result.status == "healthy"
    assert result.meta_state is state


def test_meta_builds_unhealthy_result_has_no_meta_state():
    result = parse_meta_builds_html("<html></html>", "Karnok", FetchOptions(observed_at="2026-05-05T12:00:00Z"))
    assert result.status == "unhealthy"
    assert result.meta_state is None


# --- #132: per-slug resilience in fetch_build_articles ---


def _make_article_html(slug: str, items: list[str]) -> str:
    """Build a minimal PRELOADED_STATE page for a build article.

    Uses the same ``userGeneratedDocumentBySlug`` path as the real Mobalytics
    article pages so that ``parse_build_article_state`` can find the grid nodes.
    """
    from automated_builds_pipeline.sources.mobalytics import GRID_TYPENAME

    cards = [{"title": item, "slug": item.lower().replace(" ", "-")} for item in items]
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
                                                    "data": {
                                                        "content": [
                                                            {
                                                                "__typename": GRID_TYPENAME,
                                                                "data": {
                                                                    "title": "End Game",
                                                                    "items": cards,
                                                                },
                                                            }
                                                        ]
                                                    }
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
    return f"<script>window.__PRELOADED_STATE__={json.dumps(state)};</script>"


def test_fetch_build_articles_one_bad_slug_keeps_healthy_when_others_succeed():
    """Per-slug resilience: a failing slug records a detail but source stays healthy
    if at least one slug succeeds."""
    good_html = _make_article_html("good-slug", ["Pufferfish", "Shark"])
    responses = {
        "https://mobalytics.gg/the-bazaar/builds/bad-slug": (None, "http_404"),
        "https://mobalytics.gg/the-bazaar/builds/good-slug": (good_html, ""),
    }

    def fake_http_get(url, timeout):
        return responses[url]

    with patch("automated_builds_pipeline.sources.mobalytics._http_get", side_effect=fake_http_get):
        result = fetch_build_articles(
            ["bad-slug", "good-slug"],
            FetchOptions(observed_at="2026-05-05T12:00:00Z"),
        )

    assert result.status == "healthy"
    items = [item.item for item in result.observation.items]
    assert "Pufferfish" in items
    assert "Shark" in items
    assert any("slug_failed:bad-slug" in d for d in result.details)


def test_fetch_build_articles_all_slugs_fail_is_unhealthy():
    """All slugs failing → unhealthy source."""
    def fake_http_get(url, timeout):
        return None, "http_404"

    with patch("automated_builds_pipeline.sources.mobalytics._http_get", side_effect=fake_http_get):
        result = fetch_build_articles(
            ["slug-a", "slug-b"],
            FetchOptions(observed_at="2026-05-05T12:00:00Z"),
        )

    assert result.status == "unhealthy"
    assert any("slug_failed:slug-a" in d for d in result.details)
    assert any("slug_failed:slug-b" in d for d in result.details)


def test_fetch_build_articles_window_id_excludes_failed_slugs():
    """Window ID should only include the slugs that succeeded."""
    good_html = _make_article_html("ok-slug", ["Narwhal"])
    responses = {
        "https://mobalytics.gg/the-bazaar/builds/bad-slug": (None, "http_503"),
        "https://mobalytics.gg/the-bazaar/builds/ok-slug": (good_html, ""),
    }

    def fake_http_get(url, timeout):
        return responses[url]

    with patch("automated_builds_pipeline.sources.mobalytics._http_get", side_effect=fake_http_get):
        result = fetch_build_articles(
            ["bad-slug", "ok-slug"],
            FetchOptions(observed_at="2026-05-05T12:00:00Z"),
        )

    assert result.status == "healthy"
    assert "ok-slug" in result.observation.window_id
    assert "bad-slug" not in result.observation.window_id
