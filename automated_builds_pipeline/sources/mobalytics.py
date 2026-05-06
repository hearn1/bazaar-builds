"""Fetch structured Mobalytics Bazaar guide data from PRELOADED_STATE."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

from automated_builds_pipeline.stats import ItemWindowEvidence, WindowObservation
from automated_builds_pipeline.sources.base import FetchOptions, SourceFetchResult

META_BUILDS_URL = "https://mobalytics.gg/the-bazaar/guides/meta-builds"
ARTICLE_BASE_URL = "https://mobalytics.gg/the-bazaar/builds/"
BOARD_TYPENAME = "TheBazaarDocumentCmWidgetBoardCreatorV1"
GRID_TYPENAME = "NgfDocumentCmWidgetGameDataCardGridV2"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def fetch_meta_builds(hero: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    html, detail = _http_get(options.source_url or META_BUILDS_URL, options.timeout_seconds)
    if html is None:
        return _result("mobalytics_meta_builds:unknown", options, "unhealthy", [detail])
    return parse_meta_builds_html(html, hero, options)


def parse_meta_builds_html(html: str, hero: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    state = _extract_preloaded_state(html)
    if state is None:
        return _result("mobalytics_meta_builds:unknown", options, "unhealthy", ["preloaded_state_missing_or_invalid"])
    return parse_meta_builds_state(state, hero, options)


def parse_meta_builds_state(state: dict[str, Any], hero: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    document = _document_data(state)
    version = document.get("version") if isinstance(document, dict) else None
    window_id = f"mobalytics_meta_builds:v{version}" if version not in (None, "") else "mobalytics_meta_builds:unknown"
    if not isinstance(document, dict) or version in (None, ""):
        return _result(window_id, options, "unhealthy", ["document_version_missing"])

    nodes = [node for node in document.get("content", []) if isinstance(node, dict) and node.get("__typename") == BOARD_TYPENAME]
    if not nodes:
        return _result(window_id, options, "unhealthy", ["board_creator_nodes_missing"])

    retained: list[dict[str, Any]] = []
    for node in nodes:
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        title = _clean(data.get("title"))
        build_hero = _hero_from_title(title)
        if hero and build_hero and build_hero.casefold() != hero.casefold():
            continue
        cards = _dedupe(_clean(card.get("name")) for card in data.get("cards", []) if isinstance(card, dict))
        if not title or not cards:
            continue
        retained.append({"title": title, "cards": cards, "description": data.get("descriptionTheBazaarBoardCreator")})

    if not retained:
        return _result(window_id, options, "unhealthy", ["retained_builds_empty"])

    items: list[ItemWindowEvidence] = []
    for build in retained:
        archetype = _archetype_from_title(build["title"])
        for rank, item in enumerate(build["cards"], start=1):
            items.append(
                ItemWindowEvidence(
                    item=item,
                    archetype=archetype,
                    rank=rank,
                    appearances=1,
                    sample_count=1,
                    frequency=1.0,
                    archetypes_seen=[archetype],
                    evidence_refs=[options.artifact_ref] if options.artifact_ref else [],
                    metadata={
                        "build_title": build["title"],
                        "editorial_description": build["description"],
                    },
                )
            )
    return _result(window_id, options, "healthy", [], items)


def fetch_build_articles(
    article_slugs: Optional[list[str]] = None,
    options: Optional[FetchOptions] = None,
) -> SourceFetchResult:
    options = options or FetchOptions()
    slugs = list(article_slugs if article_slugs is not None else options.article_slugs)
    if not slugs:
        return _result("mobalytics_build_articles:skipped", options, "skipped", ["no_article_slugs_configured"])

    all_items: list[ItemWindowEvidence] = []
    details: list[str] = []
    for slug in slugs:
        html, detail = _http_get(urljoin(ARTICLE_BASE_URL, slug), options.timeout_seconds)
        if html is None:
            return _result(f"mobalytics_build_articles:{slug}", options, "unhealthy", [detail])
        state = _extract_preloaded_state(html)
        if state is None:
            return _result(f"mobalytics_build_articles:{slug}", options, "unhealthy", ["preloaded_state_missing_or_invalid"])
        article_items = parse_build_article_state(state, slug, options)
        if article_items.status != "healthy":
            return article_items
        all_items.extend(article_items.observation.items)
        details.extend(article_items.details)
    window_suffix = ",".join(slugs)
    return _result(f"mobalytics_build_articles:{window_suffix}", options, "healthy", details, all_items)


def parse_build_article_state(state: dict[str, Any], slug: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    document = _document_data(state)
    grids = [node for node in _walk(document) if isinstance(node, dict) and node.get("__typename") == GRID_TYPENAME]
    items: list[ItemWindowEvidence] = []
    for grid in grids:
        data = grid.get("data") if isinstance(grid.get("data"), dict) else {}
        section = _clean(data.get("title") or data.get("sectionTitle") or data.get("heading"))
        cards = data.get("items", [])
        for rank, card in enumerate([c for c in cards if isinstance(c, dict)], start=1):
            title = _clean(card.get("title") or card.get("name"))
            if title:
                items.append(
                    ItemWindowEvidence(
                        item=title,
                        archetype=slug,
                        rank=rank,
                        appearances=1,
                        sample_count=1,
                        frequency=1.0,
                        archetypes_seen=[slug],
                        metadata={"section_title": section, "slug": card.get("slug"), "hero": card.get("subTitle") or card.get("hero")},
                    )
                )
    if not items:
        return _result(f"mobalytics_build_articles:{slug}", options, "unhealthy", ["card_grid_items_missing"])
    return _result(f"mobalytics_build_articles:{slug}", options, "healthy", [], items)


def parse_build_article_fixture(data: dict[str, Any], options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    slug = _clean(data.get("slug")) or "fixture"
    items: list[ItemWindowEvidence] = []
    for grid in data.get("grids", []):
        section = _clean(grid.get("section_title")) if isinstance(grid, dict) else ""
        cards = grid.get("items", []) if isinstance(grid, dict) else []
        for rank, card in enumerate([c for c in cards if isinstance(c, dict)], start=1):
            title = _clean(card.get("title"))
            if title:
                items.append(ItemWindowEvidence(item=title, archetype=slug, rank=rank, metadata={"section_title": section, "hero": card.get("hero")}))
    status = "healthy" if items else "unhealthy"
    details = [] if items else ["card_grid_items_missing"]
    return _result(f"mobalytics_build_articles:{slug}", options, status, details, items)


def parse_meta_builds_fixture(data: dict[str, Any], hero: str, options: Optional[FetchOptions] = None, version: int = 537) -> SourceFetchResult:
    content = []
    for block in data.get("build_blocks", []):
        content.append(
            {
                "__typename": BOARD_TYPENAME,
                "data": {
                    "title": block.get("title"),
                    "cards": [{"name": item} for item in block.get("items", [])],
                    "descriptionTheBazaarBoardCreator": None,
                },
            }
        )
    state = {
        "theBazaarState": {
            "apollo": {
                "graphqlV2": {
                    "queries": [
                        {},
                        {
                            "state": {
                                "data": [
                                    {
                                        "game": {
                                            "documents": {
                                                "userGeneratedDocumentBySlug": {
                                                    "data": {
                                                        "version": version,
                                                        "content": content,
                                                    }
                                                }
                                            }
                                        }
                                    }
                                ]
                            }
                        },
                    ]
                }
            }
        }
    }
    return parse_meta_builds_state(state, hero, options)


def _extract_preloaded_state(html: str) -> Optional[dict[str, Any]]:
    marker = re.search(r"window\.__PRELOADED_STATE__\s*=", html)
    if not marker:
        return None
    start = html.find("{", marker.end())
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    end = None
    for index, char in enumerate(html[start:], start=start):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        return None
    try:
        data = json.loads(html[start:end])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _document_data(state: Any) -> dict[str, Any]:
    try:
        queries = state["theBazaarState"]["apollo"]["graphqlV2"]["queries"]
        for query in queries:
            data_rows = query.get("state", {}).get("data", [])
            for row in data_rows:
                document = row.get("game", {}).get("documents", {}).get("userGeneratedDocumentBySlug", {}).get("data")
                if isinstance(document, dict):
                    return document
    except (KeyError, TypeError, AttributeError):
        return {}
    return {}


def _result(window_id: str, options: FetchOptions, status: str, details: list[str], items: Optional[list[ItemWindowEvidence]] = None) -> SourceFetchResult:
    observation = WindowObservation(
        window_id=window_id,
        observed_at=options.observed_at or _utc_now(),
        artifact_ref=options.artifact_ref,
        health_status=status,
        details=details,
        items=items or [],
    )
    return SourceFetchResult(observation=observation, status=status, details=details)


def _http_get(url: str, timeout: int) -> tuple[Optional[str], str]:
    try:
        import requests
    except ImportError as exc:
        return None, f"requests_unavailable:{exc}"
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    except requests.RequestException as exc:
        return None, f"fetch_failed:{exc}"
    if response.status_code != 200:
        return None, f"http_{response.status_code}"
    return response.text, ""


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _hero_from_title(title: str) -> Optional[str]:
    match = re.search(r"\(([^)]+)\)\s*$", title)
    return _clean(match.group(1)) if match else None


def _archetype_from_title(title: str) -> str:
    return _clean(re.sub(r"\s*\([^)]+\)\s*$", "", title)) or title


def _dedupe(values) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
