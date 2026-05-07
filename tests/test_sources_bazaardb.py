from __future__ import annotations

import json

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


def test_bazaardb_headless_cloudflare_verification_sample_is_unhealthy(sample_dir):
    data = json.loads((sample_dir / "bazaardb" / "meta-headless-control-2026-05-06.json").read_text(encoding="utf-8"))

    result = parse_meta_html(data["text_sample"], FetchOptions(observed_at="2026-05-06T12:00:00Z"))

    assert result.status == "unhealthy"
    assert result.details == ["cloudflare_challenge_not_cleared"]


def test_bazaardb_current_rendered_dom_extracts_footer_patch_and_items(sample_dir):
    html = (sample_dir / "bazaardb" / "meta-rendered-dom-2026-05-06.html").read_text(encoding="utf-8")

    result = parse_meta_html(html, FetchOptions(observed_at="2026-05-06T12:00:00Z"))

    assert result.status == "healthy"
    assert result.patch_label == "14.0 (May 6)"
    assert result.observation.window_id == "bazaardb:14.0 (May 6)"
    assert result.observation.items
    names = {item.item for item in result.observation.items}
    assert {"Bat", "Hunting Hawk", "Flying Squirrel"} <= names
    bat = next(item for item in result.observation.items if item.item == "Bat")
    assert bat.appearances == 6
    assert bat.frequency == 0.75
    assert bat.metadata["patch_notes_url"] == "/patchnotes"


def test_bazaardb_relative_patch_link_text_is_not_patch_label():
    html = """
    <a href="/patchnotes">4h ago</a>
    <main>
      <h1>Meta Stats</h1>
      <p>These meta stats are based on data uploaded by the community for the most recent numbered patch.</p>
      <h2>Archetypes</h2>
      <h3>Core Items</h3>
      <img alt="Wolf"><span>Wolf</span>
      <h3>Supporting Items</h3>
      <img alt="Bat"><span>Bat</span><a href="/run?cards=x,y">6 runs · 75%</a>
    </main>
    <footer>Database based on patch <span>14.0 (May 6)</span></footer>
    """

    result = parse_meta_html(html, FetchOptions(observed_at="2026-05-06T12:00:00Z"))

    assert result.status == "healthy"
    assert result.patch_label == "14.0 (May 6)"
    assert result.observation.window_id == "bazaardb:14.0 (May 6)"


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


def test_bazaardb_current_shape_summary_fixture_extracts_patch_items_and_archetypes(sample_dir):
    result = parse_meta_fixture(
        sample_dir / "bazaardb" / "meta-current-shape-summary-2026-05-06.json",
        FetchOptions(observed_at="2026-05-06T12:00:00Z"),
    )

    assert result.status == "healthy"
    assert result.patch_label == "14.0 (May 6)"
    assert result.observation.window_id == "bazaardb:14.0 (May 6)"
    names = {item.item for item in result.observation.items}
    assert {"Wolf", "Bat", "Karst", "Flying Squirrel"} <= names


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

