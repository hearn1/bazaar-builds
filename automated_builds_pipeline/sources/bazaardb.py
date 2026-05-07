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
SECTION_HEADER_MAP = {header.casefold(): header for header in SECTION_HEADERS}
RUN_TEXT_RE = re.compile(r"(?P<runs>\d[\d,]*)\s+runs?\s*[·\-\u00b7]\s*(?P<pct>\d+(?:\.\d+)?)%?", re.IGNORECASE)
RELATIVE_FRESHNESS_RE = re.compile(r"^\d+\s*(?:m|min|h|hr|d|day|w|week)s?\s+ago$", re.IGNORECASE)


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
            headless_result = _render_meta_page(playwright, options, headless=True)
            if headless_result["status"] == "html":
                html = str(headless_result["html"])
            elif headless_result["detail"] == "cloudflare_challenge_not_cleared" and options.allow_headed_fallback:
                headed_result = _render_meta_page(playwright, options, headless=False)
                if headed_result["status"] != "html":
                    return _result("bazaardb:unknown", options, "unhealthy", [str(headed_result["detail"])])
                html = str(headed_result["html"])
            else:
                return _result("bazaardb:unknown", options, "unhealthy", [str(headless_result["detail"])])
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
    patch_identity = data.get("patch_identity") if isinstance(data.get("patch_identity"), dict) else {}
    patch_label = _clean(data.get("patch_label")) or _clean(patch_identity.get("footer_database_patch")) or "unknown"
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
        support_groups = []
        if isinstance(group.get("supporting_sections"), list):
            support_groups.extend(group["supporting_sections"])
        for key, rows in group.items():
            if str(key).startswith("supporting_") and isinstance(rows, list):
                support_groups.append({"label": key.replace("_", " "), "items": rows})
        for support_group in support_groups:
            if not isinstance(support_group, dict):
                continue
            section = _clean(support_group.get("label")).upper() or "SUPPORTING ITEMS"
            rows = support_group.get("items", [])
            if not isinstance(rows, list):
                continue
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


def _render_meta_page(playwright: Any, options: FetchOptions, *, headless: bool) -> dict[str, Any]:
    browser = playwright.chromium.launch(headless=headless)
    page = browser.new_page()
    try:
        page.goto(options.source_url or META_URL, wait_until="domcontentloaded", timeout=options.timeout_seconds * 1000)
        _wait_for_real_page_landmark(page, options.timeout_seconds * 1000)
        text = page.locator("body").inner_text(timeout=5000)
        title = page.title()
        if _is_challenge(text) or _is_challenge(title):
            return {"status": "unhealthy", "detail": "cloudflare_challenge_not_cleared"}
        if not _has_real_page_landmark(text, title):
            return {"status": "unhealthy", "detail": "content_landmark_missing"}
        return {"status": "html", "html": page.content()}
    finally:
        browser.close()


def _wait_for_real_page_landmark(page: Any, timeout_ms: int) -> None:
    try:
        page.wait_for_function(
            """
            () => {
              const text = document.body?.innerText || "";
              const title = document.title || "";
              const haystack = `${title}\n${text}`.toLowerCase();
              if (haystack.includes("performing security verification") || haystack.includes("cf-challenge")) {
                return true;
              }
              const hasIdentity = haystack.includes("meta stats");
              const hasShape =
                haystack.includes("archetypes") ||
                haystack.includes("total runs") ||
                haystack.includes("avg wins") ||
                haystack.includes("most recent numbered patch") ||
                haystack.includes("core items") ||
                haystack.includes("supporting items") ||
                haystack.includes("popular skills");
              return hasIdentity && hasShape;
            }
            """,
            timeout=timeout_ms,
        )
    except Exception:
        return


def _extract_items_from_tokens(tokens: list[_Token], artifact_ref: Optional[str], patch_url: Optional[str]) -> list[ItemWindowEvidence]:
    items: list[ItemWindowEvidence] = []
    current_archetype = "Unknown"
    current_section = ""
    section_rank = 0
    recent_imgs: list[str] = []
    recent_text: list[str] = []
    for token in tokens:
        if token.kind == "text":
            section = SECTION_HEADER_MAP.get(token.value.casefold())
            if section:
                current_section = section
                section_rank = 0
                recent_imgs = []
                recent_text = []
                continue
            recent_text.append(token.value)
            recent_text = recent_text[-5:]
            match = RUN_TEXT_RE.search(" ".join(recent_text))
            if match and recent_imgs:
                appearances = int(match.group("runs").replace(",", ""))
                frequency = float(match.group("pct")) / 100.0
                item = recent_imgs[-1]
                section_rank += 1
                recent_text = []
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
            recent_text = []
    return items


def _extract_patch(links: list[tuple[str, str]], html: str) -> tuple[Optional[str], Optional[str]]:
    patch_url: Optional[str] = None
    for href, text in links:
        if "patch" in href.casefold():
            patch_url = href
            if text and not _is_relative_freshness(text):
                return text, href
    footer_match = re.search(
        r"Database\s+based\s+on\s+patch(?:\s|<[^>]+>)*(?P<label>\d+(?:\.\d+)?\s*\([^)]+\))",
        html,
        re.IGNORECASE,
    )
    if footer_match:
        return _clean_patch_label(footer_match.group("label")), patch_url
    text_match = re.search(
        r"Database\s+based\s+on\s+patch\s+(?P<label>\d+(?:\.\d+)?\s*\([^)]+\))",
        _strip_tags(html),
        re.IGNORECASE,
    )
    if text_match:
        return _clean_patch_label(text_match.group("label")), patch_url
    for href, text in links:
        if "patch" in href.casefold() and text:
            return text, href
    match = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\b", html)
    return (match.group(0), patch_url) if match else (None, patch_url)


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
    return (
        "enable javascript and cookies to continue" in haystack
        or "cf-challenge" in haystack
        or "performing security verification" in haystack
        or ("just a moment" in haystack and "cloudflare" in haystack)
    )


def _has_real_page_landmark(text: str, title: str = "") -> bool:
    haystack = f"{title}\n{text or ''}".casefold()
    has_identity = "meta stats" in haystack
    has_shape = any(
        landmark in haystack
        for landmark in (
            "total runs",
            "avg wins",
            "most recent numbered patch",
            "archetypes",
            "core items",
            "supporting items",
            "popular skills",
        )
    )
    return has_identity and has_shape


def _is_relative_freshness(text: str) -> bool:
    return bool(RELATIVE_FRESHNESS_RE.match(_clean(text)))


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


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


def _clean_patch_label(value: Any) -> str:
    label = _clean(value)
    label = re.sub(r"\(\s+", "(", label)
    label = re.sub(r"\s+\)", ")", label)
    return label


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

