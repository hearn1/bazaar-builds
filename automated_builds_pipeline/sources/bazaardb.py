"""Fetch bazaardb.gg patch-scoped meta evidence via Playwright-rendered DOM.

Install browser bits once with: playwright install chromium
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

from automated_builds_pipeline.stats import ItemWindowEvidence, WindowObservation
from automated_builds_pipeline.sources.base import FetchOptions, SourceFetchResult

META_URL = "https://bazaardb.gg/run/meta"
SECTION_HEADERS = ("CORE ITEMS", "SUPPORTING ITEMS", "POPULAR SKILLS")
RUN_TEXT_RE = re.compile(r"(?P<runs>\d[\d,]*)\s+runs?\s*[·\-\u00b7]\s*(?P<pct>\d+(?:\.\d+)?)%?", re.IGNORECASE)


@dataclass
class _Token:
    kind: str
    value: str


class _BazaardbHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[_Token] = []
        self.links: list[tuple[str, str]] = []
        self._current_link: Optional[dict[str, str]] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        if tag == "img" and attr.get("alt"):
            self.tokens.append(_Token("img", _clean(attr["alt"])))
        if tag == "a" and attr.get("href"):
            self._current_link = {"href": attr["href"], "text": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_link is not None:
            self.links.append((self._current_link["href"], _clean(self._current_link["text"])))
            self._current_link = None

    def handle_data(self, data: str) -> None:
        text = _clean(data)
        if not text:
            return
        if self._current_link is not None:
            self._current_link["text"] += f" {text}"
        self.tokens.append(_Token("text", text))


def fetch_meta(hero: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return _result("bazaardb:unknown", options, "unhealthy", [f"playwright_unavailable:{exc}"])

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(options.source_url or META_URL, wait_until="domcontentloaded", timeout=options.timeout_seconds * 1000)
            landmark_timeout_ms = options.timeout_seconds * 1000
            try:
                page.wait_for_selector("text=CORE ITEMS", timeout=landmark_timeout_ms)
            except PlaywrightTimeoutError:
                text = ""
                try:
                    text = page.locator("body").inner_text(timeout=5000)
                except Exception:
                    text = ""
                browser.close()
                detail = "cloudflare_challenge_not_cleared" if _is_challenge(text) else "content_landmark_missing"
                return _result("bazaardb:unknown", options, "unhealthy", [detail])
            text = page.locator("body").inner_text(timeout=5000)
            if _is_challenge(text):
                browser.close()
                return _result("bazaardb:unknown", options, "unhealthy", ["cloudflare_challenge_not_cleared"])
            if hero:
                _try_filter_hero(page, hero)
            html = page.content()
            browser.close()
    except PlaywrightTimeoutError:
        return _result("bazaardb:unknown", options, "unhealthy", ["browser_timeout"])
    except Exception as exc:
        return _result("bazaardb:unknown", options, "unhealthy", [f"browser_failed:{type(exc).__name__}"])
    return parse_meta_html(html, options)


def parse_meta_html(html: str, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    if _is_challenge(html):
        return _result("bazaardb:unknown", options, "unhealthy", ["cloudflare_challenge_not_cleared"])
    parser = _BazaardbHTMLParser()
    parser.feed(html or "")
    patch_label, patch_url = _extract_patch(parser.links, html)
    window_id = f"bazaardb:{patch_label}" if patch_label else "bazaardb:unknown"
    if not patch_label:
        return _result(window_id, options, "unhealthy", ["patch_label_missing"])
    if options.expected_patch_label and options.expected_patch_label != patch_label:
        return _result(window_id, options, "unhealthy", ["expected_patch_mismatch"], patch_label=patch_label)

    items = _extract_items_from_tokens(parser.tokens, options.artifact_ref, patch_url)
    details: list[str] = []
    if not items:
        details.append("zero_item_archetype_groups")
    if items and not any(item.appearances is not None and item.frequency is not None for item in items):
        details.append("run_frequency_context_missing")
    status = "healthy" if not details else "unhealthy"
    return _result(window_id, options, status, details, items, patch_label=patch_label)


def parse_meta_fixture(path: Path, options: Optional[FetchOptions] = None) -> SourceFetchResult:
    options = options or FetchOptions()
    data = json.loads(path.read_text(encoding="utf-8"))
    patch_label = _clean(data.get("patch_label")) or "unknown"
    items: list[ItemWindowEvidence] = []
    for archetype_index, group in enumerate(data.get("archetype_sample", []), start=1):
        core_items = [str(item) for item in group.get("core_items", []) if item]
        archetype = " / ".join(core_items) if core_items else f"archetype_{archetype_index}"
        sample_count = _int_or_none(group.get("runs"))
        for rank, item in enumerate(core_items, start=1):
            items.append(
                ItemWindowEvidence(
                    item=item,
                    archetype=archetype,
                    rank=rank,
                    appearances=sample_count,
                    sample_count=sample_count,
                    frequency=1.0 if sample_count else None,
                    archetypes_seen=[archetype],
                    evidence_refs=[options.artifact_ref] if options.artifact_ref else [],
                    metadata={"section": "CORE ITEMS", "avg_wins": group.get("avg_wins")},
                )
            )
        rank = 1
        for key, rows in group.items():
            if not str(key).startswith("supporting_") or not isinstance(rows, list):
                continue
            section = key.replace("_", " ").upper()
            for row in rows:
                item = _clean(row.get("item")) if isinstance(row, dict) else ""
                appearances = _int_or_none(row.get("runs")) if isinstance(row, dict) else None
                pct = _frequency(row.get("pct")) if isinstance(row, dict) else None
                if item:
                    items.append(
                        ItemWindowEvidence(
                            item=item,
                            archetype=archetype,
                            rank=rank,
                            appearances=appearances,
                            sample_count=sample_count,
                            frequency=pct,
                            archetypes_seen=[archetype],
                            evidence_refs=[options.artifact_ref] if options.artifact_ref else [],
                            metadata={"section": section},
                        )
                    )
                    rank += 1
    status = "healthy" if patch_label != "unknown" and items else "unhealthy"
    details = [] if status == "healthy" else ["fixture_missing_patch_or_items"]
    return _result(f"bazaardb:{patch_label}", options, status, details, items, patch_label=patch_label)


def _try_filter_hero(page: Any, hero: str) -> None:
    # We click the client-side hero filter because bazaardb has no stable hero URL param.
    # If the layout drifts, the parser still consumes the unfiltered rendered page.
    try:
        page.get_by_text("Filters", exact=True).click(timeout=2000)
        page.get_by_text(hero, exact=True).click(timeout=2000)
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        return


def _extract_items_from_tokens(tokens: list[_Token], artifact_ref: Optional[str], patch_url: Optional[str]) -> list[ItemWindowEvidence]:
    items: list[ItemWindowEvidence] = []
    current_archetype = "Unknown"
    current_section = ""
    section_rank = 0
    recent_imgs: list[str] = []
    for token in tokens:
        if token.kind == "text":
            upper = token.value.upper()
            if upper in SECTION_HEADERS:
                current_section = upper
                section_rank = 0
                recent_imgs = []
                continue
            match = RUN_TEXT_RE.search(token.value)
            if match and recent_imgs:
                appearances = int(match.group("runs").replace(",", ""))
                frequency = float(match.group("pct")) / 100.0
                item = recent_imgs[-1]
                section_rank += 1
                items.append(
                    ItemWindowEvidence(
                        item=item,
                        archetype=current_archetype,
                        appearances=appearances,
                        frequency=frequency,
                        rank=section_rank,
                        archetypes_seen=[current_archetype],
                        evidence_refs=[artifact_ref] if artifact_ref else [],
                        metadata={"section": current_section, "patch_notes_url": patch_url},
                    )
                )
        elif token.kind == "img":
            if current_section == "CORE ITEMS":
                recent_imgs.append(token.value)
                current_archetype = " / ".join(recent_imgs[-5:])
            else:
                recent_imgs.append(token.value)
    return items


def _extract_patch(links: list[tuple[str, str]], html: str) -> tuple[Optional[str], Optional[str]]:
    for href, text in links:
        if "patch" in href.casefold() and text:
            return text, href
    match = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b", html)
    return (match.group(0), None) if match else (None, None)


def _result(window_id: str, options: FetchOptions, status: str, details: list[str], items: Optional[list[ItemWindowEvidence]] = None, patch_label: Optional[str] = None) -> SourceFetchResult:
    observation = WindowObservation(
        window_id=window_id,
        observed_at=options.observed_at or _utc_now(),
        artifact_ref=options.artifact_ref,
        health_status=status,
        details=details,
        items=items or [],
    )
    return SourceFetchResult(observation=observation, status=status, details=details, patch_label=patch_label)


def _is_challenge(text: str) -> bool:
    haystack = (text or "").casefold()
    return "enable javascript and cookies to continue" in haystack or "cf-challenge" in haystack


def _frequency(value: Any) -> Optional[float]:
    raw = _clean(value).rstrip("%")
    try:
        return float(raw) / 100.0
    except ValueError:
        return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

