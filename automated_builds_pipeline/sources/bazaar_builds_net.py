"""Wrap bazaar_build_enricher output as source-window evidence."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

import bazaar_build_enricher as enricher
from automated_builds_pipeline.stats import ItemWindowEvidence, WindowObservation
from automated_builds_pipeline.sources.base import FetchOptions, SourceFetchResult


def fetch_builds(hero: str, category_url: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    args = argparse.Namespace(
        category_url=category_url,
        hero=hero,
        days=options.days,
        since=options.since,
        limit=30,
        manual=[],
        fetch_posts=True,
        timeout=options.timeout_seconds,
        output=None,
        catalog_dir=Path(options.catalog_dir) if options.catalog_dir else enricher._DEFAULT_TRACKER_ROOT,
        names_file=Path(options.names_file) if options.names_file else enricher._DEFAULT_TRACKER_ROOT / "card_cache_names.txt",
    )
    summary = enricher.run(args)
    return result_from_summary(summary, hero, options)


def result_from_summary(summary: dict[str, Any], hero: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    observed = options.observed_at or _utc_now()
    window_id = f"bazaar_builds_net:{_iso_week(observed)}"
    details = _health_details(summary)
    status = "healthy" if not details else "unhealthy"
    items = _items_from_summary(summary, options.artifact_ref)
    observation = WindowObservation(
        window_id=window_id,
        observed_at=observed,
        artifact_ref=options.artifact_ref,
        health_status=status,
        details=details,
        items=items,
    )
    return SourceFetchResult(observation=observation, status=status, details=details)


def load_summary_fixture(path: Path, hero: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    return result_from_summary(json.loads(path.read_text(encoding="utf-8")), hero, options)


def _items_from_summary(summary: dict[str, Any], artifact_ref: Optional[str]) -> list[ItemWindowEvidence]:
    items: list[ItemWindowEvidence] = []
    for group in summary.get("groups", []):
        if not isinstance(group, dict):
            continue
        archetype = str(group.get("tag") or "")
        sample_count = group.get("sample_count") if isinstance(group.get("sample_count"), int) else None
        for rank, row in enumerate(group.get("item_frequencies", []), start=1):
            if not isinstance(row, dict) or not row.get("item"):
                continue
            items.append(
                ItemWindowEvidence(
                    item=str(row["item"]),
                    archetype=archetype,
                    appearances=row.get("count") if isinstance(row.get("count"), int) else None,
                    sample_count=sample_count,
                    frequency=float(row["frequency"]) if isinstance(row.get("frequency"), (int, float)) else None,
                    rank=rank,
                    archetypes_seen=[archetype],
                    evidence_refs=[artifact_ref] if artifact_ref else [],
                    metadata={"date_range": group.get("date_range") or {}},
                )
            )
    return items


def _health_details(summary: dict[str, Any]) -> list[str]:
    details: list[str] = []
    if summary.get("known_item_count") == 0:
        details.append("known_items_empty")
    records = [row for row in summary.get("records", []) if isinstance(row, dict)]
    if not isinstance(summary.get("source"), dict) or not summary.get("source", {}).get("category_url"):
        details.append("category_identity_missing")
    if records and all(not row.get("date") for row in records):
        details.append("all_retained_records_undated")
    if any(row.get("items") and not row.get("date") for row in records):
        details.append("retained_item_record_undated")
    if any(row.get("fetch_status") == "not_attempted" for row in records):
        details.append("post_fetches_not_attempted")
    since = summary.get("filters", {}).get("since") if isinstance(summary.get("filters"), dict) else None
    if since:
        since_date = enricher.parse_date(since)
        for row in records:
            record_date = enricher.parse_date(row.get("date"))
            if record_date and since_date and record_date < since_date:
                details.append("date_filter_not_obeyed")
                break
    return sorted(set(details))


def summary_from_records(records: list[enricher.BuildRecord], hero: str = "Karnok", since: Optional[date] = None) -> dict[str, Any]:
    known_items = {item for record in records for item in record.items}
    return enricher.build_summary(
        records,
        category_url="https://bazaar-builds.net/category/builds/karnok-builds/",
        hero=hero,
        since=since,
        days=30,
        known_items=known_items or {"Anaconda"},
        warnings=[],
    )


def _iso_week(observed_at: str) -> str:
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
    except ValueError:
        parsed = datetime.now(timezone.utc).date()
    year, week, _ = parsed.isocalendar()
    return f"{year}-W{week:02d}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

