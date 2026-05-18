from __future__ import annotations

import json

from automated_builds_pipeline.sources.bazaardb import (
    _exception_summary,
    _is_real_patch_label,
    _launch_chromium_browser,
    parse_meta_fixture,
    parse_meta_html,
)
from automated_builds_pipeline.sources.base import FetchOptions
from automated_builds_pipeline.stats import HeroStats, append_window, load_stats, save_stats


def test_bazaardb_fixture_extracts_patch_items_and_archetypes(sample_dir):
    result = parse_meta_fixture(
        sample_dir / "bazaardb" / "meta-page-sample.json",
        FetchOptions(observed_at="2026-05-05T12:00:00Z"),
    )

    # "Apr 29" is a bare-date fallback (no patch identity): still healthy and
    # evidence-bearing, but window_id is tagged :nopatch so readiness Gate 1
    # excludes it, and the non-fatal detail records why.
    assert result.status == "healthy"
    assert result.patch_label == "Apr 29"
    assert result.observation.window_id == "bazaardb:Apr 29:nopatch"
    assert "patch_label_fallback_nopatch" in result.observation.details
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
    assert {"Wolf", "Flying Squirrel", "Spear", "Bat", "Hunting Hawk"} <= names
    assert "Inspired Rage" not in names
    wolf = next(
        item
        for item in result.observation.items
        if item.item == "Wolf" and item.metadata["section"] == "CORE ITEMS"
    )
    spear = next(
        item
        for item in result.observation.items
        if item.item == "Spear" and item.metadata["section"] == "CORE ITEMS"
    )
    assert wolf.archetype == "Wolf / Flying Squirrel / Spear"
    assert wolf.appearances == 8
    assert wolf.sample_count == 8
    assert wolf.frequency == 1.0
    assert spear.archetype == wolf.archetype
    assert spear.appearances == 8
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


def test_is_real_patch_label_distinguishes_patch_from_date_fallback():
    assert _is_real_patch_label("14.1 (May 12)") is True
    assert _is_real_patch_label("14.0 (Hotfix May 7)") is True
    assert _is_real_patch_label("14 (May 6)") is True
    # Bare month/day fallback carries no patch identity.
    assert _is_real_patch_label("May 14") is False
    assert _is_real_patch_label("Apr 29") is False
    assert _is_real_patch_label("unknown") is False
    assert _is_real_patch_label(None) is False
    assert _is_real_patch_label("") is False


def test_bazaardb_date_fallback_patch_is_healthy_but_nopatch_tagged():
    # Only the bare-date fallback (last resort in _extract_patch) resolves here:
    # no patch link text, no "Database based on patch" footer.
    html = """
    <main>
      <h1>Meta Stats</h1>
      <p>These meta stats are based on data uploaded by the community.</p>
      <h2>Archetypes</h2>
      <h3>Core Items</h3>
      <img alt="Wolf"><span>Wolf</span>
      <h3>Supporting Items</h3>
      <img alt="Bat"><span>Bat</span><a href="/run?cards=x,y">6 runs · 75%</a>
    </main>
    <footer>Snapshot taken May 14</footer>
    """

    result = parse_meta_html(html, FetchOptions(observed_at="2026-05-14T12:00:00Z"))

    assert result.status == "healthy"
    assert result.patch_label == "May 14"
    assert result.observation.window_id == "bazaardb:May 14:nopatch"
    assert "patch_label_fallback_nopatch" in result.observation.details
    # Non-fatal: evidence is still captured.
    assert result.observation.items


def test_bazaardb_browser_launch_falls_back_to_installed_chrome():
    class FakeChromium:
        def __init__(self):
            self.calls = []

        def launch(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("channel") == "chrome":
                return "browser"
            raise RuntimeError("Executable doesn't exist at missing-managed-chromium")

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    playwright = FakePlaywright()

    assert _launch_chromium_browser(playwright, headless=True) == "browser"
    assert playwright.chromium.calls == [
        {"headless": True},
        {"headless": True, "channel": "chrome"},
    ]


def test_bazaardb_browser_launch_error_preserves_actionable_detail():
    class FakeChromium:
        def launch(self, **kwargs):
            raise RuntimeError(f"launch failed for {kwargs.get('channel') or 'managed'}\nsecond line")

    class FakePlaywright:
        chromium = FakeChromium()

    try:
        _launch_chromium_browser(FakePlaywright(), headless=False)
    except RuntimeError as exc:
        summary = _exception_summary(exc)
    else:
        raise AssertionError("expected launch failure")

    assert "chromium_launch_failed" in summary
    assert "managed:RuntimeError:launch failed for managed" in summary
    assert "chrome:RuntimeError:launch failed for chrome" in summary
    assert "msedge:RuntimeError:launch failed for msedge" in summary


def test_bazaardb_round_trips_into_stats_sidecar(sample_dir, tmp_path):
    result = parse_meta_fixture(
        sample_dir / "bazaardb" / "meta-page-sample.json",
        FetchOptions(observed_at="2026-05-05T12:00:00Z"),
    )
    stats = HeroStats(hero="Mak")

    append_window(stats, "bazaardb", result.observation)
    save_stats(stats, tmp_path)
    loaded = load_stats("Mak", tmp_path)

    assert loaded.last_window_id("bazaardb") == "bazaardb:Apr 29:nopatch"
    assert loaded.item_history("Caustic Solvent", "bazaardb")[0].rank == 1

