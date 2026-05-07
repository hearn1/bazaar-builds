import json

from automated_builds_pipeline import smoke
from automated_builds_pipeline.sources.base import SourceFetchResult
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
