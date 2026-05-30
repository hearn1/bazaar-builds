"""Live source health smoke checks for source drift defense."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from automated_builds_pipeline.pipeline import _bazaar_builds_category_url, _coach_builds_dir, _names_file
from automated_builds_pipeline.sources import bazaar_builds_net, bazaardb, mobalytics
from automated_builds_pipeline.sources.base import SKIPPED, UNHEALTHY, FetchOptions, SourceFetchResult

SOURCE_BAZAARDB = "bazaardb"
SOURCE_MOBALYTICS_META = "mobalytics_meta_builds"
SOURCE_MOBALYTICS_ARTICLES = "mobalytics_build_articles"
SOURCE_BAZAAR_BUILDS_NET = "bazaar_builds_net"
REQUIRED_SOURCES = frozenset({SOURCE_BAZAARDB, SOURCE_MOBALYTICS_META, SOURCE_BAZAAR_BUILDS_NET})

Fetcher = Callable[[FetchOptions], SourceFetchResult]


def run_smoke(
    *,
    output_path: Path,
    tracker_repo: Path,
    hero: str = "Karnok",
    article_slugs: Optional[list[str]] = None,
    expected_patch_label: Optional[str] = None,
    timeout_seconds: int = 30,
    fetchers: Optional[dict[str, Fetcher]] = None,
) -> dict[str, object]:
    generated_at = _utc_now()
    options = FetchOptions(
        observed_at=generated_at,
        expected_patch_label=expected_patch_label,
        timeout_seconds=timeout_seconds,
        catalog_dir=str(_coach_builds_dir(tracker_repo)),
        names_file=str(_names_file(tracker_repo)),
        article_slugs=list(article_slugs or []),
    )
    active_fetchers = fetchers or _default_fetchers(hero)
    sources = [_source_row(source, fetcher(options), required=source in REQUIRED_SOURCES) for source, fetcher in active_fetchers.items()]
    summary: dict[str, object] = {
        "schema_version": 1,
        "generated_at": generated_at,
        "sources": sources,
        "required_sources_unhealthy": [
            row["source"] for row in sources if row["required"] and row["status"] == UNHEALTHY
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def should_fail(summary: dict[str, object]) -> bool:
    return bool(summary.get("required_sources_unhealthy"))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run live source health smoke checks.")
    parser.add_argument("--output", type=Path, required=True, help="Path to write the source health JSON artifact.")
    parser.add_argument("--tracker-repo", type=Path, required=True, help="Read-only bazaar_coach checkout.")
    parser.add_argument("--hero", default="Karnok", help="Representative hero for hero-scoped sources.")
    parser.add_argument("--article-slug", action="append", default=[], help="Mobalytics build article slug to check. Repeatable.")
    parser.add_argument("--expected-bazaardb-patch-label", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    summary = run_smoke(
        output_path=args.output,
        tracker_repo=args.tracker_repo,
        hero=args.hero,
        article_slugs=args.article_slug,
        expected_patch_label=args.expected_bazaardb_patch_label,
        timeout_seconds=args.timeout_seconds,
    )
    print(args.output)
    return 1 if should_fail(summary) else 0


def _default_fetchers(hero: str) -> dict[str, Fetcher]:
    return {
        SOURCE_BAZAARDB: lambda options: bazaardb.fetch_meta(hero, options),
        SOURCE_MOBALYTICS_META: lambda options: mobalytics.fetch_meta_builds(hero, options),
        SOURCE_MOBALYTICS_ARTICLES: lambda options: mobalytics.fetch_build_articles(options.article_slugs, options),
        SOURCE_BAZAAR_BUILDS_NET: lambda options: bazaar_builds_net.fetch_builds(hero, _bazaar_builds_category_url(hero), options),
    }


def _source_row(source: str, result: SourceFetchResult, *, required: bool) -> dict[str, object]:
    return {
        "source": source,
        "status": result.status,
        "window_id": result.observation.window_id,
        "details": list(result.details),
        "required": required,
        "item_count": len(result.observation.items),
        "checked_at": result.observation.observed_at,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
