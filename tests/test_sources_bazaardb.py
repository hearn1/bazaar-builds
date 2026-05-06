from __future__ import annotations

from automated_builds_pipeline.sources.bazaardb import parse_meta_fixture, parse_meta_html
from automated_builds_pipeline.sources.base import FetchOptions
from automated_builds_pipeline.stats import HeroStats, append_window, load_stats, save_stats


def test_bazaardb_fixture_extracts_patch_items_and_archetypes(sample_dir):
    result = parse_meta_fixture(
        sample_dir / "bazaardb" / "meta-page-sample.json",
        FetchOptions(observed_at="2026-05-05T12:00:00Z"),
    )

    assert result.status == "healthy"
    assert result.patch_label == "Apr 29"
    assert result.observation.window_id == "bazaardb:Apr 29"
    names = {item.item for item in result.observation.items}
    assert {"Flying Potion", "Caustic Solvent", "Potion Distillery"} <= names
    solvent = next(item for item in result.observation.items if item.item == "Caustic Solvent")
    assert solvent.appearances == 176
    assert solvent.frequency == 0.35
    assert solvent.archetype


def test_bazaardb_cloudflare_challenge_is_unhealthy():
    html = "<html><body>Enable JavaScript and cookies to continue</body></html>"

    result = parse_meta_html(html, FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "unhealthy"
    assert result.details == ["cloudflare_challenge_not_cleared"]


def test_bazaardb_missing_patch_is_unhealthy():
    html = "<html><body><h2>CORE ITEMS</h2><img alt='Anaconda'><span>12 runs · 40%</span></body></html>"

    result = parse_meta_html(html, FetchOptions(observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "unhealthy"
    assert "patch_label_missing" in result.details


def test_bazaardb_expected_patch_mismatch_is_unhealthy():
    html = "<a href='/patch-notes/apr-29'>Apr 29</a><h2>CORE ITEMS</h2><img alt='Anaconda'><span>12 runs · 40%</span>"

    result = parse_meta_html(html, FetchOptions(expected_patch_label="May 06", observed_at="2026-05-05T12:00:00Z"))

    assert result.status == "unhealthy"
    assert result.patch_label == "Apr 29"
    assert "expected_patch_mismatch" in result.details


def test_bazaardb_round_trips_into_stats_sidecar(sample_dir, tmp_path):
    result = parse_meta_fixture(
        sample_dir / "bazaardb" / "meta-page-sample.json",
        FetchOptions(observed_at="2026-05-05T12:00:00Z"),
    )
    stats = HeroStats(hero="Mak")

    append_window(stats, "bazaardb", result.observation)
    save_stats(stats, tmp_path)
    loaded = load_stats("Mak", tmp_path)

    assert loaded.last_window_id("bazaardb") == "bazaardb:Apr 29"
    assert loaded.item_history("Caustic Solvent", "bazaardb")[0].rank == 1

