"""Refresh current-shape source samples without mutating pipeline state."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from automated_builds_pipeline.pipeline import _bazaar_builds_category_url, _names_file
from automated_builds_pipeline.sources import bazaar_builds_net, bazaardb, mobalytics
from automated_builds_pipeline.sources.base import FetchOptions, SourceFetchResult

SOURCE_BAZAARDB = "bazaardb"
SOURCE_MOBALYTICS_META = "mobalytics_meta_builds"
SOURCE_MOBALYTICS_ARTICLES = "mobalytics_build_articles"
SOURCE_BAZAAR_BUILDS_NET = "bazaar_builds_net"
ALL_SOURCES = (
    SOURCE_BAZAARDB,
    SOURCE_MOBALYTICS_META,
    SOURCE_MOBALYTICS_ARTICLES,
    SOURCE_BAZAAR_BUILDS_NET,
)


@dataclass(frozen=True)
class RefreshResult:
    source: str
    paths: list[Path]


RefreshFn = Callable[[Path, FetchOptions, str], RefreshResult]


def refresh_samples(
    *,
    sources: Optional[Iterable[str]],
    output_root: Path,
    tracker_repo: Path,
    hero: str = "Karnok",
    article_slugs: Optional[list[str]] = None,
    timeout_seconds: int = 30,
    refreshers: Optional[dict[str, RefreshFn]] = None,
) -> list[Path]:
    selected = _selected_sources(sources)
    observed_at = _utc_now()
    options = FetchOptions(
        observed_at=observed_at,
        timeout_seconds=timeout_seconds,
        catalog_dir=str(tracker_repo),
        names_file=str(_names_file(tracker_repo)),
        article_slugs=list(article_slugs or []),
    )
    active_refreshers = refreshers or _default_refreshers(hero)
    written: list[Path] = []
    for source in selected:
        result = active_refreshers[source](output_root / source, options, observed_at)
        written.extend(result.paths)
    return written


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh research source-shape samples.")
    parser.add_argument(
        "--source",
        action="append",
        choices=ALL_SOURCES,
        help="Source to refresh. Repeatable. Defaults to all configured live sources.",
    )
    parser.add_argument("--output-root", type=Path, default=Path("research") / "samples")
    parser.add_argument("--tracker-repo", type=Path, default=Path("../bazaar_tracker"))
    parser.add_argument("--hero", default="Karnok", help="Representative hero for hero-scoped sources.")
    parser.add_argument("--article-slug", action="append", default=[], help="Mobalytics build article slug to sample. Repeatable.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    paths = refresh_samples(
        sources=args.source,
        output_root=args.output_root,
        tracker_repo=args.tracker_repo,
        hero=args.hero,
        article_slugs=args.article_slug,
        timeout_seconds=args.timeout_seconds,
    )
    for path in paths:
        print(path.resolve())
    return 0


def _selected_sources(sources: Optional[Iterable[str]]) -> list[str]:
    selected = list(sources or ALL_SOURCES)
    unknown = [source for source in selected if source not in ALL_SOURCES]
    if unknown:
        raise ValueError(f"unknown sources: {', '.join(unknown)}")
    return selected


def _default_refreshers(hero: str) -> dict[str, RefreshFn]:
    return {
        SOURCE_BAZAARDB: lambda output_dir, options, observed_at: _refresh_bazaardb(output_dir, options, observed_at, hero),
        SOURCE_MOBALYTICS_META: lambda output_dir, options, observed_at: _write_result_sample(
            output_dir,
            SOURCE_MOBALYTICS_META,
            observed_at,
            "http_preloaded_state",
            mobalytics.META_BUILDS_URL,
            mobalytics.fetch_meta_builds(hero, options),
        ),
        SOURCE_MOBALYTICS_ARTICLES: lambda output_dir, options, observed_at: _write_result_sample(
            output_dir,
            SOURCE_MOBALYTICS_ARTICLES,
            observed_at,
            "http_preloaded_state",
            mobalytics.ARTICLE_BASE_URL,
            mobalytics.fetch_build_articles(options.article_slugs, options),
        ),
        SOURCE_BAZAAR_BUILDS_NET: lambda output_dir, options, observed_at: _write_result_sample(
            output_dir,
            SOURCE_BAZAAR_BUILDS_NET,
            observed_at,
            "wordpress_category_with_posts",
            _bazaar_builds_category_url(hero),
            bazaar_builds_net.fetch_builds(hero, _bazaar_builds_category_url(hero), options),
        ),
    }


def _refresh_bazaardb(output_dir: Path, options: FetchOptions, observed_at: str, hero: str) -> RefreshResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        result = bazaardb.fetch_meta(hero, options)
        return _write_result_sample(output_dir, SOURCE_BAZAARDB, observed_at, "playwright_rendered_dom", bazaardb.META_URL, result, extra_details=[f"html_capture_unavailable:{exc}"])

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(observed_at)
    try:
        with sync_playwright() as playwright:
            rendered = bazaardb._render_meta_page(playwright, options, headless=True)
            if rendered["status"] != "html" and rendered.get("detail") == "cloudflare_challenge_not_cleared" and options.allow_headed_fallback:
                rendered = bazaardb._render_meta_page(playwright, options, headless=False)
    except Exception as exc:
        result = bazaardb.fetch_meta(hero, options)
        return _write_result_sample(output_dir, SOURCE_BAZAARDB, observed_at, "playwright_rendered_dom", bazaardb.META_URL, result, extra_details=[f"html_capture_failed:{type(exc).__name__}"])

    if rendered["status"] != "html":
        result = bazaardb.fetch_meta(hero, options)
        return _write_result_sample(output_dir, SOURCE_BAZAARDB, observed_at, "playwright_rendered_dom", bazaardb.META_URL, result, extra_details=[f"html_capture_failed:{rendered.get('detail')}"])

    html = str(rendered["html"])
    html_path = output_dir / f"bazaardb-rendered-dom-{stamp}.html"
    text_path = output_dir / f"bazaardb-rendered-text-{stamp}.txt"
    html_path.write_text(html, encoding="utf-8")
    text_path.write_text(_html_text(html), encoding="utf-8")
    result = bazaardb.parse_meta_html(html, FetchOptions(**{**options.__dict__, "artifact_ref": str(html_path)}))
    summary = _result_payload(SOURCE_BAZAARDB, observed_at, "playwright_rendered_dom", bazaardb.META_URL, result)
    summary["sample_files"] = [str(html_path), str(text_path)]
    json_path = _write_json(output_dir, SOURCE_BAZAARDB, observed_at, summary)
    return RefreshResult(SOURCE_BAZAARDB, [html_path, text_path, json_path])


def _write_result_sample(
    output_dir: Path,
    source: str,
    observed_at: str,
    fetch_method: str,
    source_url: str,
    result: SourceFetchResult,
    *,
    extra_details: Optional[list[str]] = None,
) -> RefreshResult:
    payload = _result_payload(source, observed_at, fetch_method, source_url, result)
    if extra_details:
        payload["details"] = list(payload["details"]) + extra_details
    path = _write_json(output_dir, source, observed_at, payload)
    return RefreshResult(source, [path])


def _result_payload(source: str, observed_at: str, fetch_method: str, source_url: str, result: SourceFetchResult) -> dict[str, object]:
    items = result.observation.items
    archetypes = sorted({item.archetype for item in items if item.archetype})
    return {
        "schema_version": 1,
        "generated_at": observed_at,
        "source": source,
        "source_url": source_url,
        "fetch_method": fetch_method,
        "status": result.status,
        "details": list(result.details),
        "window_id": result.observation.window_id,
        "item_count": len(items),
        "archetype_count": len(archetypes),
        "archetype_sample": archetypes[:20],
        "item_sample": [
            {
                "item": item.item,
                "archetype": item.archetype,
                "rank": item.rank,
                "appearances": item.appearances,
                "sample_count": item.sample_count,
                "frequency": item.frequency,
                "metadata": item.metadata,
            }
            for item in items[:50]
        ],
    }


def _write_json(output_dir: Path, source: str, observed_at: str, payload: dict[str, object]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{source}-shape-summary-{_stamp(observed_at)}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _html_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip() + "\n"


def _stamp(observed_at: str) -> str:
    return observed_at.replace(":", "").replace("-", "").replace("+0000", "Z").replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
