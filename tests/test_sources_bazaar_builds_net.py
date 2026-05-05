from __future__ import annotations

from datetime import date

import bazaar_build_enricher as enricher
from automated_builds_pipeline.sources.base import FetchOptions
from automated_builds_pipeline.sources.bazaar_builds_net import (
    load_summary_fixture,
    result_from_summary,
    summary_from_records,
)
from automated_builds_pipeline.stats import HeroStats, append_window, load_stats, save_stats


def test_bazaar_builds_net_fixture_undated_records_are_unhealthy(sample_dir):
    result = load_summary_fixture(
        sample_dir / "bazaar_builds_net" / "karnok_smoke_test_with_posts.json",
        "Karnok",
        FetchOptions(observed_at="2026-05-05T12:00:00Z"),
    )

    assert result.status == "unhealthy"
    assert "all_retained_records_undated" in result.details
    assert result.observation.items


def test_bazaar_builds_net_dated_summary_extracts_item_evidence():
    records = [
        enricher.BuildRecord(
            url="https://bazaar-builds.net/anaconda-karnok/",
            title="Anaconda Karnok 10-5 Build",
            date="2026-05-05",
            hero="Karnok",
            tag="Anaconda Build",
            items=["Anaconda", "Karst", "Hunting Hawk"],
            fetch_status="http_200",
        ),
        enricher.BuildRecord(
            url="https://bazaar-builds.net/anaconda-karnok-2/",
            title="Anaconda Karnok 10-4 Build",
            date="2026-05-04",
            hero="Karnok",
            tag="Anaconda Build",
            items=["Anaconda", "Karst"],
            fetch_status="http_200",
        ),
    ]

    result = result_from_summary(
        summary_from_records(records, since=date(2026, 4, 5)),
        "Karnok",
        FetchOptions(observed_at="2026-05-05T12:00:00Z"),
    )

    assert result.status == "healthy"
    assert result.observation.window_id == "bazaar_builds_net:2026-W19"
    karst = next(item for item in result.observation.items if item.item == "Karst")
    assert karst.appearances == 2
    assert karst.sample_count == 2
    assert karst.frequency == 1.0
    assert karst.archetype == "Anaconda Build"


def test_category_text_format_dates_are_parsed():
    html = """
    <article><a href="/anaconda-karnok-10-5-build/">Anaconda Karnok 10-5 Build</a><div>May 5, 2026</div></article>
    <article><a href="/self-slow-karnok-10-0-build/">Self-Slow Karnok 10-0 Build</a><div>May 1, 2026</div></article>
    """

    records = enricher.extract_category_records("https://bazaar-builds.net/category/builds/karnok-builds/", html, "Karnok", 10)

    assert [record.date for record in records] == ["2026-05-05", "2026-05-01"]


def test_json_ld_date_is_propagated_when_json_ld_has_no_items(monkeypatch):
    html = """
    <script type="application/ld+json">
    {"@graph":[{"@type":"Article","headline":"Anaconda Karnok 10-5 Build","datePublished":"2026-05-05T09:13:45+00:00","url":"https://bazaar-builds.net/anaconda-karnok/"}]}
    </script>
    <body>Items: Anaconda, Karst</body>
    """

    monkeypatch.setattr(enricher, "fetch_url", lambda url, timeout: (html, "http_200", None))
    record = enricher.BuildRecord(url="https://bazaar-builds.net/anaconda-karnok/", hero="Karnok")

    enriched = enricher.enrich_post(record, {"Anaconda", "Karst"}, 5)

    assert enriched.date == "2026-05-05"
    assert {"Anaconda", "Karst"} <= set(enriched.items)


def test_bazaar_builds_net_round_trips_into_stats_sidecar(tmp_path):
    records = [
        enricher.BuildRecord(
            url="https://bazaar-builds.net/anaconda-karnok/",
            title="Anaconda Karnok 10-5 Build",
            date="2026-05-05",
            hero="Karnok",
            tag="Anaconda Build",
            items=["Anaconda", "Karst"],
            fetch_status="http_200",
        )
    ]
    result = result_from_summary(summary_from_records(records), "Karnok", FetchOptions(observed_at="2026-05-05T12:00:00Z"))
    stats = HeroStats(hero="Karnok")

    append_window(stats, "bazaar_builds_net", result.observation)
    save_stats(stats, tmp_path)
    loaded = load_stats("Karnok", tmp_path)

    assert loaded.last_window_id("bazaar_builds_net") == "bazaar_builds_net:2026-W19"
    assert loaded.item_history("Anaconda", "bazaar_builds_net")[0].present is True

